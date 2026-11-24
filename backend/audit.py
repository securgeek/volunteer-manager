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

Each event also becomes a row in `audit_events` (when `configure(..., to_db=True)`, as in
`main.py`), for the admin site's Audit Log and the alert rules. The row is written on its own
pooled connection with its own commit, so a request that rolls back still leaves its row.
The file is the complete record; the table is pruned to `AUDIT_RETENTION_DAYS` (default 365,
0 keeps everything) by `prune()`, which `main.py` runs at startup and daily.

A failure to write either is logged and swallowed: the audit log must never be the reason a
request fails.
"""
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import Request

import db
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
    "auth.login.throttled": Event("medium", "Password sign-in refused: too many failures"),
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
    "user.audit_access": Event("medium", "Gave or took away audit log access"),
    "audit.view": Event("info", "Opened the audit log"),
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
    # Hostile-looking requests (security_signals.py), from the backend or pulled from the
    # portal (source "portal").
    "security.attack_signature": Event("high", "Attack patterns in a request"),
    "security.suspicious_client": Event("high", "Unusual run of refused or malformed requests"),
    "security.sync_auth_fail": Event("critical", "Bad signature on the portal's sync API"),
    "portal.anomaly": Event("medium", "Input the volunteer pages can't produce"),
    # Alerts (alerts.py). Their own events are never fed back into the rules.
    "alert.fired": Event("medium", "A security alert rule fired"),
    "alert.mailed": Event("info", "A security alert was emailed"),
    "alert.mail_fail": Event("medium", "A security alert could not be emailed"),
    "alert.test": Event("info", "Sent a test alert"),
}


@dataclass
class _AuditConfig:
    """Module state: where the log goes. Both off by default (tests, scripts)."""

    path: str | None = None
    to_db: bool = False
    # Called with each record (alerts.observe). A list, not an import: alerts.py calls
    # audit() itself, so audit.py can't import it.
    observers: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


_config = _AuditConfig()

_INSERT_SQL = (
    "INSERT INTO audit_events (ts, source, event, severity, actor, user_id, ip, method, path, "
    "message, data) VALUES ((?::timestamptz AT TIME ZONE 'UTC'), ?, ?, ?, ?, ?, ?, ?, ?, ?, "
    "?::jsonb);"
)
# A retention of 0 days keeps everything: the flag makes the DELETE match nothing.
_PRUNE_SQL = (
    "DELETE FROM audit_events WHERE ? > 0 "
    "AND ts < (NOW() AT TIME ZONE 'UTC') - make_interval(days => ?);"
)


def configure(path: str | None, to_db: bool = False) -> None:
    """Set where the audit log goes. `main.py` calls it with `--audit` before uvicorn starts.

    Args:
        path: The JSONL file to append to, or None for no file.
        to_db: Also write each event to the `audit_events` table.
    """
    _config.path = path
    _config.to_db = to_db


def add_observer(fn) -> None:
    """Have `fn(record)` called with every audit record, after the file write and before the
    row is stored. A failure in `fn` is logged and swallowed. Adding one twice is a no-op.

    Args:
        fn: A callable taking the record dict.
    """
    if fn not in _config.observers:
        _config.observers.append(fn)


def remove_observer(fn) -> None:
    """Stop calling `fn` (tests).

    Args:
        fn: A callable passed to `add_observer`.
    """
    if fn in _config.observers:
        _config.observers.remove(fn)


def configured_to_db() -> bool:
    """Whether events also go to the `audit_events` table (`configure(..., to_db=True)`).

    Returns:
        True when they do.
    """
    return _config.to_db


def retention_days() -> int:
    """`AUDIT_RETENTION_DAYS`: how long `audit_events` keeps a row. 0 keeps them all.

    Returns:
        The number of days, 365 when unset, and 365 (with a warning) when it isn't a whole
        number of zero or more.
    """
    raw = os.getenv("AUDIT_RETENTION_DAYS", "365").strip()
    if raw.isdigit():
        return int(raw)
    log.warning("audit: AUDIT_RETENTION_DAYS=%r isn't a whole number of days; using 365", raw)
    return 365


def prune(conn: "db.PgClient") -> int:
    """Delete `audit_events` rows older than `retention_days()`, and commit.

    Args:
        conn: A database connection.

    Returns:
        How many rows went.
    """
    days = retention_days()
    cur = conn.execute(_PRUNE_SQL, (days, days))
    conn.commit()
    return cur.rowcount


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


def build_record(event: str, request: Request | None, s_id: str | None, message: str,
                 who: str | None = None, *, severity: str | None = None, **data) -> dict:
    """The audit record for one event, before it's written. Separate from `audit()` so the
    record's shape can be tested without a file.

    Args:
        event: A code from `EVENTS`.
        request: The request, for the client IP, method and path. None for an event with no
            request behind it, such as an alert raised by a background task.
        s_id: The caller's session id; its cached username and user id go on the record.
        message: What happened, in words.
        who: A label for a caller with no session, such as an export API key. It wins over
            `s_id` as the actor.
        severity: Overrides the registry's severity for this one record (an `alert.fired`
            carries its alert's severity). Must be one of SEVERITIES.
        **data: Structured details (a target username, a key id, counts). Values must be
            JSON-serializable or have a useful `str()`.

    Returns:
        The record, ready for `json.dumps`.
    """
    if severity not in SEVERITIES:
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
        "source": "backend",
        "actor": _clip(actor),
        "user_id": user_id,
        "ip": (client_ip(request) or "unknown") if request else None,
        "method": request.method if request else None,
        "path": _clip(request.url.path) if request else None,
        "message": _clip(message),
        "data": {k: _clip(v) for k, v in data.items()},
    }
    if event not in EVENTS:
        record["unregistered"] = True
    return record


def _no_nul(value):
    """`value` with every NUL character replaced by U+FFFD, through dicts and lists.

    Postgres can't store NUL in TEXT or JSONB. The file keeps the exact text, escaped.
    """
    if isinstance(value, str):
        return value.replace("\x00", "\ufffd")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def _store(record: dict) -> None:
    """Insert one record into `audit_events` on a connection of its own, and commit. Logs and
    swallows a failure.

    Args:
        record: A record from `build_record`, already through a JSON round trip (so every
            value is plain JSON).
    """
    rec = _no_nul(record)
    try:
        conn = db.get_conn()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Deliberately broad: no pool (bad DB_* settings, database down) raises several
        # kinds, and none of them may fail the request being audited.
        log.error("audit: no database connection for %s event: %s", rec["event"], exc)
        return
    try:
        conn.execute(_INSERT_SQL, (
            rec["ts"], rec.get("source", "backend"), rec["event"], rec["severity"], rec["actor"],
            rec["user_id"], rec["ip"],
            rec["method"], rec["path"], rec["message"], json.dumps(rec["data"])))
        conn.commit()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Deliberately broad, for the same reason: the row is a copy of the file's line.
        log.error("audit: could not store %s event: %s", rec["event"], exc)
    finally:
        db.put_conn(conn)


def audit(event: str, request: Request | None, s_id: str | None, message: str,
          who: str | None = None, *, severity: str | None = None, **data) -> None:
    """Append one event to the audit log. Never raises.

    Args:
        event: A code from `EVENTS`, as a string literal (test_audit.py checks).
        request: The request, for the client IP, method and path.
        s_id: The caller's session id, or None for an unauthenticated caller.
        message: What happened, in words. Caller-supplied text is safe here, but prefer
            putting it in `data` so it can be filtered on.
        who: A label for a caller with no session, such as an export API key.
        severity: Overrides the registry's severity; see `build_record`.
        **data: Structured details; see `build_record`.
    """
    if not (_config.path or _config.to_db or _config.observers):
        return
    if event not in EVENTS:
        log.warning("audit: unregistered event code %r", event)
    try:
        record = build_record(event, request, s_id, message, who, severity=severity, **data)
    except (TypeError, ValueError) as exc:
        log.error("audit: could not build %s event: %s", event, exc)
        return
    _emit(record)


def _emit(record: dict) -> None:
    """Write one record to the file, show it to the observers, and store it."""
    event = record["event"]
    try:
        # ensure_ascii: every non-ASCII character is written as \uXXXX. Raw, U+2028/U+2029
        # and U+0085 are line breaks to some readers (Python's splitlines among them), and
        # bidi overrides such as U+202E reorder text in a terminal. jq decodes them all.
        line = json.dumps(record, ensure_ascii=True, default=str)
    except (TypeError, ValueError) as exc:
        log.error("audit: could not encode %s event: %s", event, exc)
        return
    if _config.path:
        try:
            with _config.lock, open(_config.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            log.error("audit: could not write %s event: %s", event, exc)
    record = json.loads(line)               # plain JSON values from here on
    # Observers (alerts.py) see the event before it's stored, so a rule asking "has this
    # user signed in from here before?" doesn't find the event itself.
    for observer in list(_config.observers):
        try:
            observer(record)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Deliberately broad: an alert rule's bug must not fail the request it watches.
            log.error("audit: observer %s failed on %s: %s",
                      getattr(observer, "__name__", observer), event, exc)
    if _config.to_db:
        _store(record)


_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


def ingest(raw: dict, source: str) -> bool:
    """Record an event that happened elsewhere (the portal's security events, pulled by
    portal_sync), exactly as a local one: file, alert rules, table.

    Everything in `raw` is treated as untrusted: the timestamp must be ISO 8601 UTC, the
    severity one of SEVERITIES (else the registry's, else "info"), text is cut to MAX_TEXT,
    and `data` must be an object. An unknown event code is recorded and flagged, as locally.

    Args:
        raw: `{ts, event, severity, ip, method, path, message, data}`.
        source: Where it came from, e.g. `"portal"`.

    Returns:
        False when it was malformed and dropped, else True.
    """
    event, ts, data = raw.get("event"), raw.get("ts"), raw.get("data") or {}
    if not isinstance(event, str) or not event or not isinstance(ts, str) \
            or not _TS.match(ts) or not isinstance(data, dict):
        log.warning("audit: dropped a malformed %s event", source)
        return False
    if not (_config.path or _config.to_db or _config.observers):
        return True
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        severity = EVENTS[event].severity if event in EVENTS else "info"
    record = {
        "ts": ts, "event": _clip(event), "severity": severity, "source": _clip(source),
        "actor": None, "user_id": None,
        "ip": _clip(raw.get("ip")) if isinstance(raw.get("ip"), str) else None,
        "method": _clip(raw.get("method")) if isinstance(raw.get("method"), str) else None,
        "path": _clip(raw.get("path")) if isinstance(raw.get("path"), str) else None,
        "message": _clip(str(raw.get("message") or "")),
        "data": {str(k)[:100]: _clip(v) for k, v in data.items()},
    }
    if event not in EVENTS:
        record["unregistered"] = True
        log.warning("audit: unregistered event code %r from %s", event, source)
    _emit(record)
    return True
