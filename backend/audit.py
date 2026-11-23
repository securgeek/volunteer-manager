"""The audit log: one JSON object per line, one line per security-relevant action.

Every write, sign-in and refusal in the backend calls `audit()` with an event code from
`EVENTS`. The line it appends to the file named by `--audit` (`logs/audit.jsonl` in the
deployment) looks like:

    {"ts": "2026-11-23T14:02:11.120Z", "event": "auth.login.fail", "severity": "low",
     "actor": null, "user_id": null, "ip": "203.0.113.9", "method": "POST",
     "path": "/api/login", "message": "Invalid login attempt", "data": {"username": "bob"}}

Each line is written with `json.dumps`, so text a caller controls (a username, a label, a
note) can never break out of its field or forge a line of its own. The free-text audit.log
this replaces wrote such text verbatim.

`EVENTS` is the registry of codes. `test_audit.py` checks that every `audit()` call in the
code names a registered code as a string literal, so a typo fails a test rather than
producing a code no alert rule or filter knows about. In production an unknown code is still
recorded, flagged `"unregistered": true`, because losing an audit line would be worse.

A failure to write is logged and swallowed: the audit log must never be the reason a request
fails.
"""
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import Request

from db import _session_cache

log = logging.getLogger(__name__)

# The longest message or data value kept, in characters. Anything longer is cut and marked.
MAX_TEXT = 2000

SEVERITIES = ("info", "low", "medium", "high", "critical")



class Event(NamedTuple):
    """One registered event code: its severity, and what it records."""

    severity: str
    description: str


# code -> Event. Severity is the event's own weight; alert rules also look at how often and
# from where events arrive.
EVENTS: dict[str, Event] = {
    # Sign-in and sessions
    "auth.login.ok": Event("info", "Signed in with a password"),
    "auth.login.fail": Event("low", "Password sign-in refused"),
    "auth.logout": Event("info", "Signed out"),
    "auth.passkey.login.ok": Event("info", "Signed in with a passkey"),
    "auth.passkey.login.fail": Event("low", "Passkey sign-in refused"),
    # Passkeys
    "passkey.register.ok": Event("medium", "Registered a passkey"),
    "passkey.register.fail": Event("low", "Passkey registration refused"),
    "passkey.remove": Event("medium", "Removed own passkey"),
    "passkey.rename": Event("info", "Renamed own passkey"),
    "passkey.clear_user": Event("medium", "Removed another user's passkeys"),
    # Passwords
    "password.change": Event("medium", "Changed own password"),
    "password.change.fail": Event("low", "Password change refused: wrong current password"),
    "password.reset.request": Event("low", "Password reset requested"),
    "password.reset.mail_fail": Event("low", "Password reset mail could not be sent"),
    "password.reset.complete": Event("medium", "Password reset completed through an emailed link"),
    "password.reset.by_admin": Event("medium", "An organizer set another user's password"),
    # Users and invites
    "user.create": Event("medium", "Created a user"),
    "user.role_change": Event("medium", "Changed a user's role"),
    "user.delete": Event("medium", "Deleted a user"),
    "user.email_set": Event("medium", "Set or cleared another user's email"),
    "user.email_change": Event("medium", "Set or cleared own email"),
    "user.email_change.fail": Event("low", "Email change refused: wrong current password"),
    "invite.create": Event("medium", "Created an organizer-site invite"),
    "invite.revoke": Event("info", "Revoked an organizer-site invite"),
    "invite.accept": Event("medium", "Accepted an organizer-site invite"),
    # First-run setup and site settings
    "setup.create_admin": Event("high", "Created the first admin account"),
    "setup.create_admin.refused": Event("medium", "First-admin setup called after setup"),
    "setup.import_schedule": Event("info", "Imported a schedule CSV"),
    "setup.import_volunteers": Event("info", "Imported a volunteer CSV"),
    "settings.change": Event("info", "Changed a site setting"),
    # Keys
    "api_key.create": Event("medium", "Created an export API key"),
    "api_key.revoke": Event("info", "Revoked an export API key"),
    "api_key.use": Event("info", "Export API call accepted"),
    "api_key.refused": Event("low", "Export API call refused"),
    "email_key.update": Event("high", "Changed the key volunteer emails are encrypted to"),
    "checkin.key_rotate": Event("medium", "Rotated the check-in key"),
    # Schedule emails
    "email_template.change": Event("info", "Changed the schedule email template or its images"),
    "email_template.download": Event("low", "Downloaded the schedule email package"),
    # Check-in and radios
    "checkin.ok": Event("info", "Checked a volunteer in"),
    "checkin.code_rejected": Event("low", "Rejected a check-in code"),
    "radio.checkout": Event("info", "Checked out a radio"),
    "radio.checkin": Event("info", "Checked in a radio"),
    # Volunteers
    "volunteer.add": Event("info", "Added a volunteer"),
    "volunteer.email_index": Event("info", "Indexed a volunteer's email"),
    "volunteer.rate": Event("info", "Rated a volunteer"),
    "volunteer.note.add": Event("info", "Added a volunteer note"),
    "volunteer.note.delete": Event("info", "Deleted a volunteer note"),
    "volunteer.drop": Event("info", "Dropped a volunteer from the schedule"),
    "volunteer.status_reset": Event("info", "Reset a volunteer's check-in status"),
    "volunteer.set_inactive": Event("info", "Made a volunteer inactive or reinstated one"),
    "volunteer.purge": Event("medium", "Purged a volunteer"),
    "volunteer.purge.fail": Event("low", "A purge failed at the portal"),
    # Portal administration
    "portal.sync_now": Event("info", "Triggered a portal sync"),
    "portal.signup.approve": Event("info", "Approved a pending signup"),
    "portal.signup.decline": Event("info", "Declined a pending signup"),
    "portal.settings": Event("info", "Changed portal settings"),
    "portal.shift_flag.keep": Event("info", "Kept a volunteer on a shift they dropped"),
    "portal.invite.create": Event("info", "Created an early-access signup invite"),
    "portal.invite.send": Event("info", "Emailed an early-access signup invite"),
    "portal.invite.send_fail": Event("low", "An early-access invite could not be sent"),
    "portal.invite.revoke": Event("info", "Revoked an early-access signup invite"),
    "portal.content": Event("info", "Changed the portal's editable content"),
    # Schedule
    "schedule.assign": Event("info", "Assigned a volunteer to a slot"),
    "schedule.clear": Event("info", "Cleared a slot"),
    "schedule.note": Event("info", "Set or cleared a slot note"),
    "schedule.structure": Event("info", "Changed the schedule's days, times or positions"),
}


@dataclass
class _AuditConfig:
    """Module state: where the log goes. `path` None turns auditing off (tests, scripts)."""

    path: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


_config = _AuditConfig()


def configure(path: str | None) -> None:
    """Set the audit log's file. `main.py` calls it with `--audit` before uvicorn starts.

    Args:
        path: The JSONL file to append to, or None to turn auditing off.
    """
    _config.path = path


def client_ip(request: Request) -> str:
    """The caller's address, for the audit log, rate limits and API keys' IP allow-lists.

    nginx sends a single resolved X-Forwarded-For (frontend/CLAUDE.md → Reverse proxy), taken
    from the edge's client-address header, so the first hop is the real client.

    Args:
        request: The incoming request.

    Returns:
        The first `X-Forwarded-For` hop set by nginx, else the socket peer, else `""`.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


def _clip(value):
    """`value`, cut to MAX_TEXT characters when it's text; other values pass through."""
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…[truncated]"
    return value


def _now() -> str:
    """The current time in UTC, ISO 8601 with milliseconds and a `Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def build_record(event: str, request: Request, s_id: str | None, message: str,
                 who: str | None = None, **data) -> dict:
    """The audit record for one event, before it's written. Separate from `audit()` so the
    record's shape can be tested without a file.

    Args:
        event: A code from `EVENTS`.
        request: The request, for the client IP, method and path.
        s_id: The caller's session id; its cached username and user id go on the record.
        message: What happened, in words.
        who: A label for a caller with no session, such as an export API key. It wins over
            `s_id` as the actor.
        **data: Structured details (a target username, a key id, counts). Values must be
            JSON-serializable or have a useful `str()`.

    Returns:
        The record, ready for `json.dumps`.
    """
    severity = EVENTS[event].severity if event in EVENTS else "info"
    actor, user_id = None, None
    if who:
        actor = who
    elif s_id:
        sess = _session_cache.get(s_id, {})
        actor = sess.get("username", "unknown")
        user_id = sess.get("user_id")
    record = {
        "ts": _now(),
        "event": event,
        "severity": severity,
        "actor": _clip(actor),
        "user_id": user_id,
        "ip": client_ip(request) or "unknown",
        "method": request.method,
        "path": _clip(request.url.path),
        "message": _clip(message),
        "data": {k: _clip(v) for k, v in data.items()},
    }
    if event not in EVENTS:
        record["unregistered"] = True
    return record


def audit(event: str, request: Request, s_id: str | None, message: str,
          who: str | None = None, **data) -> None:
    """Append one event to the audit log. Never raises.

    Args:
        event: A code from `EVENTS`, as a string literal (test_audit.py checks).
        request: The request, for the client IP, method and path.
        s_id: The caller's session id, or None for an unauthenticated caller.
        message: What happened, in words. Caller-supplied text is safe here, but prefer
            putting it in `data` so it can be filtered on.
        who: A label for a caller with no session, such as an export API key.
        **data: Structured details; see `build_record`.
    """
    if not _config.path:
        return
    if event not in EVENTS:
        log.warning("audit: unregistered event code %r", event)
    try:
        # ensure_ascii: every non-ASCII character is written as \uXXXX. Raw, U+2028/U+2029
        # and U+0085 are line breaks to some readers (Python's splitlines among them), and
        # bidi overrides such as U+202E reorder text in a terminal. jq decodes them all.
        line = json.dumps(build_record(event, request, s_id, message, who, **data),
                          ensure_ascii=True, default=str)
        with _config.lock, open(_config.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, TypeError, ValueError) as exc:
        log.error("audit: could not write %s event: %s", event, exc)
