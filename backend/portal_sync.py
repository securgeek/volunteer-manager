"""Sync client: the scheduler's side of the portal API.

The scheduler is always the client. Nothing here listens, and the portal is never given a
callback URL — which is why the portal can live on an internet-facing host while the scheduler
does not, and why there is no inbound hole to punch. The price is that new signups arrive when
we ask for them, so `heartbeat` is polled and is deliberately cheap (one row read).

Three jobs:
  * poll `heartbeat`, pull only the streams whose rev moved
  * push the schedule snapshot, branding, settings, and volunteer changes
  * keep cursors and health in `portal_sync_state`, so the Admin UI can show sync is alive

If PORTAL_SYNC_URL is unset the task never starts, so an existing deployment with no portal
behaves exactly as it did before.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import db as _db

log = logging.getLogger("portal_sync")

# Where to reach the portal's sync API — often an internal address. Distinct from the
# portal's own PORTAL_PUBLIC_URL, which is what volunteers see in a magic link.
PORTAL_SYNC_URL = (os.getenv("PORTAL_SYNC_URL") or os.getenv("PORTAL_BASE_URL", "")).rstrip("/")
SYNC_KEY_ID     = os.getenv("SYNC_KEY_ID", "")
SYNC_SECRET     = os.getenv("SYNC_SECRET", "").encode()
EMAIL_HMAC_KEY  = os.getenv("EMAIL_HMAC_KEY", "").encode()
SYNC_INTERVAL   = int(os.getenv("PORTAL_SYNC_INTERVAL", "30"))
SYNC_VERIFY_TLS = os.getenv("PORTAL_SYNC_VERIFY_TLS", "1") != "0"
PUSH_DEBOUNCE   = float(os.getenv("PORTAL_PUSH_DEBOUNCE", "2"))

HMAC_KEY_CHECK = b"bsc-hmac-key-check"

@dataclass
class _SyncState:
    """Module state for the sync loop: one instance, ``_state``.

    ``healthy`` and ``key_match`` exist so health is logged on transition, not per cycle: a
    line every 30 seconds saying "nothing changed" would bury the one line that matters.
    Current state lives in portal_sync_state and is surfaced by PortalStatus.vue, which is the
    right place to look for "is it working now".
    """

    push_event: Optional[asyncio.Event] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    last_status: dict = field(default_factory=lambda: {"configured": False})
    healthy: Optional[bool] = None
    key_match: Optional[bool] = None
    # Held by every reconcile and by a purge, so a pull already under way can't put back a
    # volunteer that's being purged (purge_volunteers).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_state = _SyncState()


def configured() -> bool:
    """Whether the portal link is set up: the URL and both sync keys are present.

    Returns:
        True when `PORTAL_SYNC_URL`, `SYNC_KEY_ID` and `SYNC_SECRET` are all set.
    """
    return bool(PORTAL_SYNC_URL and SYNC_KEY_ID and SYNC_SECRET)


def missing_settings() -> list:
    """Names of the unset variables that keep sync off; empty when configured.

    PORTAL_SYNC_URL is the name compose passes. PORTAL_BASE_URL is still read as a fallback
    for running outside compose, but a PORTAL_BASE_URL line in .env never reaches the
    container, so it is never the name to suggest.
    """
    return [name for name, val in (("PORTAL_SYNC_URL", PORTAL_SYNC_URL),
                                   ("SYNC_KEY_ID", SYNC_KEY_ID),
                                   ("SYNC_SECRET", SYNC_SECRET)) if not val]


# ── signing ───────────────────────────────────────────────────────────────────

def _sign(method: str, target: str, body: bytes) -> dict:
    """Headers for one signed request. Nonce is per-request; the portal rejects a repeat."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = "\n".join([method.upper(), target, ts, nonce, body_hash])
    sig = hmac.new(SYNC_SECRET, msg.encode(), hashlib.sha256).hexdigest()
    return {
        "x-sync-key-id": SYNC_KEY_ID,
        "x-sync-timestamp": ts,
        "x-sync-nonce": nonce,
        "x-sync-signature": sig,
    }


def email_hmac(email: str) -> Optional[str]:
    """The portal's lookup index for an address (portal security.email_hmac), or None without
    a key. Normalisation must match the portal's exactly: strip and lowercase, nothing else.

    Args:
        email: The address, as typed.

    Returns:
        The hex HMAC-SHA256, or `None` when `EMAIL_HMAC_KEY` isn't set.
    """
    if not EMAIL_HMAC_KEY:
        return None
    return hmac.new(EMAIL_HMAC_KEY, email.strip().lower().encode(), hashlib.sha256).hexdigest()


def hmac_key_fp() -> Optional[str]:
    """Our own HMAC key fingerprint, for comparison against the portal's."""
    if not EMAIL_HMAC_KEY:
        return None
    return hmac.new(EMAIL_HMAC_KEY, HMAC_KEY_CHECK, hashlib.sha256).hexdigest()


async def _request(method: str, path: str, payload: Optional[dict] = None) -> Any:
    import httpx

    body = json.dumps(payload).encode() if payload is not None else b""
    headers = _sign(method, path, body)
    if payload is not None:
        headers["content-type"] = "application/json"
    async with httpx.AsyncClient(timeout=30, verify=SYNC_VERIFY_TLS) as client:
        resp = await client.request(method, f"{PORTAL_SYNC_URL}{path}",
                                    content=body, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _op_id() -> str:
    """Idempotency key. A push that times out after the portal committed can be retried."""
    return secrets.token_hex(12)


# ── cursor / health bookkeeping ───────────────────────────────────────────────

def _get_state(conn, stream: str) -> dict:
    row = conn.execute("SELECT * FROM portal_sync_state WHERE stream = ?", (stream,)).fetchone()
    return dict(row) if row else {"stream": stream, "last_rev": None,
                                  "last_ok_at": None, "last_error": None}


def cursor_of(state: dict) -> int:
    """A stream's cursor as a number. NULL means 'no cursor', which compares as 0.

    Args:
        state: A `portal_sync_state` row as a dict.
    """
    return state.get("last_rev") or 0


def _mark_ok(conn, stream: str, last_rev: Optional[int] = None) -> None:
    """Record a successful pass. last_rev stays NULL for push streams, which have no cursor —
    writing 0 there reads like 'cursor at the beginning', which is misleading in the one table
    someone reads while debugging."""
    conn.execute(
        "INSERT INTO portal_sync_state (stream, last_rev, last_ok_at, last_error, last_error_at) "
        "VALUES (?, ?, NOW() AT TIME ZONE 'UTC', NULL, NULL) "
        "ON CONFLICT(stream) DO UPDATE SET "
        "last_rev = COALESCE(?, portal_sync_state.last_rev), "
        "last_ok_at = NOW() AT TIME ZONE 'UTC', last_error = NULL, last_error_at = NULL",
        (stream, last_rev, last_rev),
    )
    conn.commit()


def _mark_error(conn, stream: str, err: str) -> None:
    """Record a failure. last_ok_at is deliberately left alone, so the UI can show both when
    it last worked and what it is failing with now."""
    conn.execute(
        "INSERT INTO portal_sync_state (stream, last_rev, last_error, last_error_at) "
        "VALUES (?, NULL, ?, NOW() AT TIME ZONE 'UTC') "
        "ON CONFLICT(stream) DO UPDATE SET last_error = excluded.last_error, "
        "last_error_at = excluded.last_error_at",
        (stream, err[:500]),
    )
    conn.commit()


def _note_outcome(status: dict) -> None:
    """Log a transition into health, plus anything this cycle actually did."""
    did = [k for k in ("volunteers_pulled", "volunteers_pushed", "settings_push",
                       "schedule_push", "branding_push") if k in status]
    if _state.healthy is not True:
        log.info("portal sync healthy: %s (every %ss)%s", PORTAL_SYNC_URL, SYNC_INTERVAL,
                 f" — did: {', '.join(did)}" if did else "")
        _state.healthy = True
    elif did:
        log.info("portal sync: %s", ", ".join(did))

    match = status.get("hmac_key_match")
    if match != _state.key_match:
        if match is False:
            log.error("portal EMAIL_HMAC_KEY does not match ours — volunteers added in the "
                      "scheduler will get a lookup hash the portal cannot match, so they will "
                      "not be able to sign in. Both services need the same value.")
        elif _state.key_match is False:
            log.info("portal EMAIL_HMAC_KEY now matches ours")
        _state.key_match = match


def _note_failure(exc: Exception) -> None:
    """Warn on the transition into failure; keep repeats at debug so a persistent outage does
    not emit a warning every interval forever. Current state is in portal_sync_state."""
    if _state.healthy is not False:
        log.warning("portal sync failing: %s", exc)
        _state.healthy = False
    else:
        log.debug("portal sync still failing: %s", exc)


# ── snapshot construction ─────────────────────────────────────────────────────

def build_snapshot(conn) -> dict:
    """Assemble the whole schedule as one payload.

    A full replace rather than a diff: the portal holds a copy of our state, and reconciling a
    diff against a copy is how copies drift. content_hash makes an unchanged push free, so we
    can re-push whenever we are unsure rather than tracking what we already sent.

    Args:
        conn: The database connection.

    Returns:
        Days, times, day_times (each with `remaining`), categories, positions and slots, plus
        a `content_hash` over all of them.
    """
    def rows(sql: str) -> list[dict]:
        return [dict(r) for r in conn.execute(sql).fetchall()]

    days = rows("SELECT day_key, day_str, event_date, 0 AS sort_order FROM days_table ORDER BY day_key")
    times = rows("SELECT time_key, time_str, 0 AS sort_order FROM times_table ORDER BY time_key")
    # Each timeslot carries `remaining`, which is what the signup form offers. Overage
    # changes it, so a new percentage changes the hash and is pushed like any other edit.
    remaining = compute_remaining(conn)
    day_times = rows("SELECT day_key, time_key FROM day_times ORDER BY day_key, time_key")
    for dt in day_times:
        dt["remaining"] = remaining.get((dt["day_key"], dt["time_key"]), 0)
    cats = rows("SELECT cat_name, sort_order, cat_color FROM categories ORDER BY sort_order")
    positions = rows("SELECT pos_key, pos_cat, pos_str FROM positions ORDER BY pos_key")
    slots = rows("SELECT day_key, time_key, pos_key, vol_id FROM schedule "
                 "ORDER BY day_key, time_key, pos_key")

    payload = {
        "days": days, "times": times, "day_times": day_times, "categories": cats,
        "positions": positions, "slots": slots,
    }
    payload["content_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    return payload


def _has_table(conn, name: str) -> bool:
    """Whether a table exists.

    Tries information_schema (Postgres) and falls back to sqlite_master, so
    compute_remaining can be exercised against a real in-memory SQLite database in tests.
    That matters: the substring-stubbed FakeDB never executes the SQL, which is exactly how the
    missing day_key predicate below went unnoticed.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", (name,)
        ).fetchone()
        return bool(row)
    # Deliberately broad: any failure here just means "not Postgres", so try SQLite next.
    except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
            ).fetchone()
            return bool(row)
        # Deliberately broad: neither catalogue answered, so there is no such table.
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            return False


OVERAGE_MAX = 200


def overage_pct(conn) -> int:
    """The organizer's overbooking allowance, a whole percentage (app_kv portal_overage_pct).

    0 until set. Clamped, so a stray value can't open the floodgates.

    Args:
        conn: The database connection.

    Returns:
        The percentage, between 0 and `OVERAGE_MAX`.
    """
    row = conn.execute("SELECT val FROM app_kv WHERE app_key = 'portal_overage_pct'").fetchone()
    try:
        pct = int(row["val"]) if row else 0
    except (TypeError, ValueError):
        pct = 0
    return max(0, min(OVERAGE_MAX, pct))


def compute_remaining(conn, pct: Optional[int] = None) -> dict:
    """How many more volunteers the signup form may take for each timeslot.

        capacity  = floor(slots in the timeslot x (1 + overage%))
        remaining = capacity - assigned there - picked it but not yet assigned there

    Overage is the deliberate overbooking against volunteers who drop out between signup and
    the event. Counting *all* slots in capacity, minus assigned ones, rather than open slots
    alone, keeps remaining steady when an organizer assigns a volunteer to the slot they
    picked: assigned goes up by one and "picked, not yet assigned" down by one.

    A pick counts until that volunteer holds an assignment on the same day AND at the same
    start time; pending and approved volunteers count, cancelled or inactive ones don't. Returns
    {(day_key, time_key): remaining}, never below 0.

    Args:
        conn: The database connection.
        pct: The overage percentage to use. `None` reads the stored one.
    """
    pct = overage_pct(conn) if pct is None else pct
    out: dict = {}
    cells = {(r["day_key"], r["time_key"]): (r["total"], r["assigned"] or 0)
             for r in conn.execute(
                 "SELECT day_key, time_key, COUNT(*) AS total, "
                 "SUM(CASE WHEN vol_id IS NULL THEN 0 ELSE 1 END) AS assigned "
                 "FROM schedule GROUP BY day_key, time_key").fetchall()}
    committed: dict = {}
    if _has_table(conn, "vol_picks"):
        for r in conn.execute(
            "SELECT vp.day_key, vp.time_key, COUNT(*) AS n FROM vol_picks vp "
            "JOIN volunteers v ON v.vol_id = vp.vol_id "
            "WHERE vp.kind = 'pref' AND v.is_counted "
            "  AND NOT EXISTS (SELECT 1 FROM schedule s WHERE s.vol_id = vp.vol_id "
            "                  AND s.day_key = vp.day_key AND s.time_key = vp.time_key) "
            "GROUP BY vp.day_key, vp.time_key"
        ).fetchall():
            committed[(r["day_key"], r["time_key"])] = r["n"]
    for cell, (total, assigned) in cells.items():
        capacity = (total * (100 + pct)) // 100
        out[cell] = max(0, capacity - assigned - committed.get(cell, 0))
    return out

def build_branding(conn) -> dict:
    """The site title, portal title and logos, as one payload for the portal.

    Args:
        conn: The database connection.

    Returns:
        `app_title`, `portal_title`, `logo`, `favicon` and `header_logo` (data URLs or `None`),
        `contact_email`, `conference_url` and `code_of_conduct_url`, plus a `content_hash`.
    """
    rows = {r["app_key"]: r["val"] for r in conn.execute(
        "SELECT app_key, val FROM app_kv "
        "WHERE app_key IN ('app_title', 'app_logo', 'app_favicon', 'app_header_logo', "
        "'portal_title', 'contact_email', 'conference_url', 'code_of_conduct_url')"
    ).fetchall()}
    # logo is the year's theme logo (the favicon is made from it); header_logo the smaller
    # one the volunteer pages show top left. A portal older than schema v4 ignores it.
    payload = {
        "app_title": rows.get("app_title"),
        "portal_title": rows.get("portal_title"),
        "logo": rows.get("app_logo"),
        "favicon": rows.get("app_favicon"),
        "header_logo": rows.get("app_header_logo"),
        # The conference's public contact address and links (setup wizard, App Settings).
        # A portal older than schema v6 ignores them.
        "contact_email": rows.get("contact_email"),
        "conference_url": rows.get("conference_url"),
        "code_of_conduct_url": rows.get("code_of_conduct_url"),
    }
    payload["content_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    return payload


def build_content(conn) -> dict:
    """The organizers' landing page, questionnaire and invite email, for the portal.

    Each part is the saved definition, or ``None`` for the portal's default. The portal checks
    them all again (portal/content.py) before storing any of it.

    Args:
        conn: The database connection.

    Returns:
        ``landing``, ``questionnaire`` and ``invite``, plus a ``content_hash``.
    """
    from routers.portal_content import stored_parts
    payload = stored_parts(conn)
    payload["content_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


async def push_content(conn) -> dict:
    """Send the editable content to the portal (`POST /api/sync/content`).

    Args:
        conn: The database connection. The `content_push` stream is marked ok on success.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status, such as a definition it
            refuses.
    """
    payload = build_content(conn)
    payload["op_id"] = _op_id()
    result = await _request("POST", "/api/sync/content", payload)
    _mark_ok(conn, "content_push")
    return result


def build_invites(conn) -> dict:
    """Every early-access invite, as hashes, for the portal (POST /api/sync/signup_invites).

    The portal replaces its list with this one, keeping its own record of uses. Timestamps go
    as UTC with a Z, which the portal normalises.

    Args:
        conn: The database connection.

    Returns:
        `{"invites": [...], "content_hash": ...}`.
    """
    rows = conn.execute(
        "SELECT invite_id, token_hash, expires_at, revoked_at FROM signup_invites "
        "ORDER BY invite_id;").fetchall()
    invites = [{"invite_id": r["invite_id"], "token_hash": r["token_hash"],
                "expires_at": r["expires_at"].isoformat() + "Z",
                "revoked_at": r["revoked_at"].isoformat() + "Z" if r["revoked_at"] else None}
               for r in rows]
    return {"invites": invites, "content_hash": hashlib.sha256(
        json.dumps(invites, sort_keys=True).encode()).hexdigest()}


# ── the pushes and pulls ──────────────────────────────────────────────────────

async def push_snapshot(conn) -> dict:
    """Send the whole schedule to the portal (`POST /api/sync/schedule`).

    Args:
        conn: The database connection. The `schedule_push` stream is marked ok on success.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status.
    """
    payload = build_snapshot(conn)
    payload["op_id"] = _op_id()
    result = await _request("POST", "/api/sync/schedule", payload)
    _mark_ok(conn, "schedule_push")
    return result


async def push_branding(conn) -> dict:
    """Send the titles and logos to the portal (`POST /api/sync/branding`).

    Args:
        conn: The database connection. The `branding_push` stream is marked ok on success.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status.
    """
    payload = build_branding(conn)
    payload["op_id"] = _op_id()
    result = await _request("POST", "/api/sync/branding", payload)
    _mark_ok(conn, "branding_push")
    return result


async def push_volunteers(conn, volunteers: list[dict], rekeys: Optional[list[dict]] = None) -> dict:
    """Send volunteer decisions to the portal (`POST /api/sync/volunteers`).

    Args:
        conn: The database connection. The `volunteers_push` stream is marked ok on success.
        volunteers: Records from `build_volunteer_push`.
        rekeys: `{"vol_id", "new_vol_id"}` renames for the portal to apply first.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status.
    """
    payload = {"op_id": _op_id(), "volunteers": volunteers, "rekeys": rekeys or []}
    result = await _request("POST", "/api/sync/volunteers", payload)
    _mark_ok(conn, "volunteers_push")
    return result


def site_origin() -> str:
    """The organizer site's origin, for links in admin mail: ADMIN_PUBLIC_URL, or
    PORTAL_PUBLIC_URL when one origin serves both (local dev). The portal checks that a link
    it's asked to mail starts with its own copy of the same value."""
    return (os.getenv("ADMIN_PUBLIC_URL", "").strip()
            or os.getenv("PORTAL_PUBLIC_URL", "").strip()).rstrip("/")


async def send_admin_mail(kind: str, to: str, link: str, **fields) -> dict:
    """Ask the portal to mail an admin invite or password reset (POST /api/sync/send_admin_mail).

    The scheduler never reaches the mail relay itself — only the portal shares a network with
    it — so admin mail goes the way volunteer mail does: through the portal, which owns the
    templates and checks the link points at our own /admin/invite or /admin/reset. Raises
    RuntimeError if sync isn't configured or the portal refuses.

    Args:
        kind: `invite` or `reset`.
        to: The recipient's address.
        link: The single-use link to put in the message.
        **fields: Anything else the portal's template takes, such as the inviter's name.

    Returns:
        The portal's reply.
    """
    if not configured():
        raise RuntimeError("portal sync isn't configured, so mail can't be sent")
    payload = {"op_id": _op_id(), "kind": kind, "to": to, "link": link, **fields}
    return await _request("POST", "/api/sync/send_admin_mail", payload)


async def push_settings(conn, settings: dict) -> dict:
    """Send portal settings the scheduler owns (`POST /api/sync/settings`).

    Args:
        conn: The database connection. The `settings_push` stream is marked ok on success.
        settings: Only the keys to change, e.g. `{"portal_signup_open": "1"}`.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status.
    """
    payload = {"op_id": _op_id(), "settings": settings}
    result = await _request("POST", "/api/sync/settings", payload)
    _mark_ok(conn, "settings_push")
    return result


# Who owns which volunteer column. The volunteer edits their own picks and answers in the
# portal (signup, then My info); the scheduler decides status, and names the row. Each side
# sends only what it owns, so an approval waiting to go out can't overwrite a volunteer's
# edit made meanwhile, and a pull can't undo an approval the portal hasn't seen yet.
VOLUNTEER_OWNED = ("time_pref", "add_time", "add_any", "answers", "cancelled_at")
SCHEDULER_OWNED = ("name", "status", "inactive_at")

# Who counts is three generated columns on volunteers (scheduler.sql), so the rule lives in
# the schema once and every query stays a static literal:
#   is_live        neither cancelled nor inactive
#   is_counted     pending or approved, and live: capacity and demand
#   is_assignable  approved and live: every picker
# Approval (status) is separate from cancelling and going inactive (scheduler.sql): a
# cancelled or inactive volunteer keeps their approval for when they're back, but meanwhile
# neither counts nor can be assigned.


def _flag_dropped_assignments(conn, vol_id: str, new_picks: set) -> None:
    """Raise a shift flag for each timeslot the volunteer just dropped while assigned to it."""
    old = {(r["day_key"], r["time_key"]) for r in conn.execute(
        "SELECT day_key, time_key FROM vol_picks WHERE vol_id = ?", (vol_id,)).fetchall()}
    for day, time in old - new_picks:
        if conn.execute("SELECT 1 FROM schedule WHERE vol_id = ? AND day_key = ? "
                        "AND time_key = ? LIMIT 1", (vol_id, day, time)).fetchone():
            conn.execute(
                "INSERT INTO shift_flags (vol_id, day_key, time_key) VALUES (?, ?, ?) "
                "ON CONFLICT (vol_id, day_key, time_key) DO UPDATE SET "
                "flagged_at = NOW() AT TIME ZONE 'UTC', resolved_at = NULL, resolved_by = NULL",
                (vol_id, day, time))


# What goes with a purged volunteer, spelled out rather than left to the foreign keys: a
# database made before a table's ON DELETE rule existed would refuse the delete instead.
_PURGE_LOCAL_SQL = (
    "DELETE FROM vol_picks WHERE vol_id = ANY(?::text[])",
    "DELETE FROM shift_flags WHERE vol_id = ANY(?::text[])",
    "DELETE FROM vol_notes WHERE vol_id = ANY(?::text[])",
    "UPDATE schedule SET vol_id = NULL WHERE vol_id = ANY(?::text[])",
    "UPDATE radio_checkouts SET vol_id = NULL WHERE vol_id = ANY(?::text[])",
)


async def purge_volunteers(conn, vol_ids: list[str]) -> list[str]:
    """Delete volunteers on the portal, then here. Returns the ids deleted here.

    Portal first: deleted here first, the next pull would bring them straight back. Each
    volunteer's pending re-key id (portal_rekey_from) goes too, since the portal may still
    know them by it. Without a portal configured there's nothing to purge there.

    Here (_PURGE_LOCAL_SQL, one transaction with the delete): picks, notes (check-ins and
    ratings too) and shift flags are deleted, and schedule slots and radio checkouts keep
    their row with vol_id NULL (radio history keeps person_name).

    Args:
        conn: A PgClient.
        vol_ids: The scheduler ids to purge.

    Returns:
        The ids whose rows were deleted here.

    Raises:
        RuntimeError: The portal call failed in any way; nothing was deleted here.
    """
    async with _state.lock:
        rows = conn.execute(
            "SELECT vol_id, portal_rekey_from FROM volunteers WHERE vol_id = ANY(?::text[])",
            (list(vol_ids),)).fetchall()
        conn.commit()
        if configured() and rows:
            portal_ids = sorted({r["vol_id"] for r in rows}
                                | {r["portal_rekey_from"] for r in rows if r["portal_rekey_from"]})
            try:
                await _request("POST", "/api/sync/purge_volunteers",
                               {"op_id": _op_id(), "vol_ids": portal_ids})
            # Deliberately broad: a refusal, a timeout and a connection error all mean the
            # same here (stop before deleting anything locally), and the caller reports it.
            except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
                raise RuntimeError(f"portal purge failed: {exc}") from exc
        for stmt in _PURGE_LOCAL_SQL:
            conn.execute(stmt, (list(vol_ids),))
        deleted = [r["vol_id"] for r in conn.execute(
            "DELETE FROM volunteers WHERE vol_id = ANY(?::text[]) RETURNING vol_id",
            (list(vol_ids),)).fetchall()]
        conn.commit()
    request_push()
    return sorted(deleted)


async def push_signup_invites(conn) -> dict:
    """Send the early-access invite list to the portal (`POST /api/sync/signup_invites`).

    Args:
        conn: The database connection. The `invites_push` stream is marked ok on success.

    Returns:
        The portal's reply.

    Raises:
        RuntimeError: When the portal answers with an error status.
    """
    payload = build_invites(conn)
    payload["op_id"] = _op_id()
    result = await _request("POST", "/api/sync/signup_invites", payload)
    _mark_ok(conn, "invites_push")
    return result


async def pull_volunteers(conn) -> int:
    """Mirror changed volunteers, and their timeslot picks, into our own tables.

    Volunteer-owned columns and picks are always taken. Scheduler-owned ones (status, name)
    are skipped while the row is portal_dirty: it holds a local decision the portal hasn't
    accepted yet, and the portal's older status must not undo it; the portal echoes the
    decision back once the push lands. The old id of a pending badge-number re-key is skipped
    entirely, or it would resurrect the pending row beside its renamed copy.

    A pick naming a timeslot we no longer run is dropped (vol_picks has a foreign key to
    day_times). Dropping a timeslot the volunteer is assigned to raises a shift flag
    (_flag_dropped_assignments) for the banner in the organizer app.

    Returns how many rows were written.

    Args:
        conn: The database connection. Committed after each page of 500.
    """
    state = _get_state(conn, "volunteers_pull")
    cursor = cursor_of(state)
    written = 0
    known = {(r["day_key"], r["time_key"])
             for r in conn.execute("SELECT day_key, time_key FROM day_times").fetchall()}
    while True:
        data = await _request("GET", f"/api/sync/volunteers?since={cursor}&limit=500")
        rows = data.get("volunteers") or []
        for v in rows:
            if conn.execute("SELECT 1 FROM volunteers WHERE portal_rekey_from = ? "
                            "AND portal_dirty", (v["vol_id"],)).fetchone():
                continue
            local = conn.execute("SELECT portal_dirty FROM volunteers WHERE vol_id = ?",
                                 (v["vol_id"],)).fetchone()
            answers = (json.dumps(v["answers"]) if isinstance(v.get("answers"), (dict, list))
                       else v.get("answers"))
            owned = (v.get("time_pref") or "", v.get("add_time") or "",
                     bool(v.get("add_any")), answers, v.get("cancelled_at") or None)
            if local and local["portal_dirty"]:
                conn.execute(
                    "UPDATE volunteers SET time_pref = ?, add_time = ?, add_any = ?, answers = ?, "
                    "cancelled_at = ? WHERE vol_id = ?", (*owned, v["vol_id"]))
            else:
                # inactive_at is ours, so it's never taken from the portal.
                conn.execute(
                    "INSERT INTO volunteers (vol_id, name, status, time_pref, add_time, add_any, "
                    "answers, cancelled_at, email_enc, email_hmac, email_key_fp, "
                    "possible_duplicate_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(vol_id) DO UPDATE SET name = excluded.name, "
                    "status = excluded.status, time_pref = excluded.time_pref, "
                    "add_time = excluded.add_time, add_any = excluded.add_any, "
                    "answers = excluded.answers, cancelled_at = excluded.cancelled_at, "
                    "email_enc = excluded.email_enc, "
                    "email_hmac = excluded.email_hmac, email_key_fp = excluded.email_key_fp, "
                    "possible_duplicate_of = excluded.possible_duplicate_of",
                    (v["vol_id"], v.get("name") or "", v.get("status") or "pending", *owned,
                     v.get("email_enc"), v.get("email_hmac"), v.get("email_key_fp"),
                     json.dumps(v["possible_duplicate_of"])
                     if isinstance(v.get("possible_duplicate_of"), (dict, list))
                     else v.get("possible_duplicate_of")),
                )
            if v.get("invite_id"):
                # The early-access invite this signup used. Keep the first use; the portal
                # already refuses a second.
                conn.execute(
                    "UPDATE signup_invites SET used_at = NOW() AT TIME ZONE 'UTC', used_by = ? "
                    "WHERE invite_id = ? AND used_at IS NULL",
                    (v.get("name") or v["vol_id"], v["invite_id"]))
            if "picks" in v:
                incoming = [p for p in v.get("picks") or []
                            if (p.get("day_key"), p.get("time_key")) in known]
                _flag_dropped_assignments(conn, v["vol_id"],
                                          {(p["day_key"], p["time_key"]) for p in incoming})
                conn.execute("DELETE FROM vol_picks WHERE vol_id = ?", (v["vol_id"],))
                for p in incoming:
                    conn.execute(
                        "INSERT INTO vol_picks (vol_id, day_key, time_key, kind, rank) "
                        "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (v["vol_id"], p["day_key"], p["time_key"], p.get("kind") or "pref",
                         p.get("rank")),
                    )
            written += 1
        conn.commit()
        cursor = data.get("cursor", cursor)
        if not data.get("more"):
            break
    _mark_ok(conn, "volunteers_pull", cursor)
    if written:
        # Open organizer browsers refresh the shift-flag banner and volunteer lists. Lazy, as
        # routers.events imports this module lazily too.
        from routers.events import broadcast
        broadcast("volunteers")
    return written


def _json_text(val):
    """A JSONB column as the text the portal stores it as."""
    if val is None or isinstance(val, str):
        return val
    return json.dumps(val)


def build_volunteer_push(conn) -> tuple[list, list]:
    """Every volunteer owed to the portal, and the re-keys to apply first.

    Only SCHEDULER_OWNED columns are sent. Picks, answers and the name parts are the
    volunteer's (the portal's upsert updates only the columns a push carries), so a push can't
    overwrite an edit the volunteer made while an approval was waiting to go out.

    Args:
        conn: The database connection.

    Returns:
        `(volunteers, rekeys)`: the `portal_dirty` rows, and the renames they need.
    """
    vols, rekeys = [], []
    for r in conn.execute(
        "SELECT vol_id, name, status, inactive_at, portal_rekey_from FROM volunteers "
        "WHERE portal_dirty ORDER BY vol_id"
    ).fetchall():
        rec = {"vol_id": r["vol_id"], **{c: r[c] for c in SCHEDULER_OWNED}}
        if rec["inactive_at"]:
            rec["inactive_at"] = rec["inactive_at"].isoformat() + "Z"
        vols.append(rec)
        if r["portal_rekey_from"]:
            rekeys.append({"vol_id": r["portal_rekey_from"], "new_vol_id": r["vol_id"]})
    return vols, rekeys


async def push_dirty_volunteers(conn) -> int:
    """Push owed volunteer changes; clear the flag on what the portal accepted.

    The clear matches on status too, so a second decision made while this push was in flight
    stays dirty and goes on the next tick instead of being marked sent.

    Args:
        conn: The database connection.

    Returns:
        How many volunteers were pushed.
    """
    vols, rekeys = build_volunteer_push(conn)
    if not vols:
        return 0
    await push_volunteers(conn, vols, rekeys)
    for v in vols:
        conn.execute(
            "UPDATE volunteers SET portal_dirty = FALSE, portal_rekey_from = NULL "
            "WHERE vol_id = ? AND status = ? AND portal_dirty",
            (v["vol_id"], v["status"]),
        )
    conn.commit()
    return len(vols)


def pubkey_fingerprint(pem: str) -> str:
    """The portal's security.pubkey_fingerprint, reimplemented: ``SHA256:<b64>`` of the SPKI
    DER, padding stripped. Compared against the heartbeat's pubkey_fp.

    Args:
        pem: A PEM public key.

    Returns:
        The fingerprint, as `SHA256:<base64>`.
    """
    der = base64.b64decode("".join(
        line.strip() for line in pem.strip().splitlines()
        if line.strip() and not line.strip().startswith("-----")))
    return "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")


def local_settings(conn) -> dict:
    """The portal settings the scheduler owns, as they should be on the portal.

    Args:
        conn: The database connection.

    Returns:
        `portal_signup_open` (`"1"` or `"0"`) and `email_pubkey` (PEM or `None`).
    """
    def kv(key):
        row = conn.execute("SELECT val FROM app_kv WHERE app_key = ?", (key,)).fetchone()
        return row["val"] if row else None
    return {"portal_signup_open": "1" if kv("portal_signup_open") == "1" else "0",
            "email_pubkey": kv("email_pubkey")}


async def sync_settings(conn, heartbeat: dict) -> Optional[list]:
    """Push signup-open and the email public key when the portal's differ. Returns what was
    pushed, or None.

    The public key matters most: the portal encrypts every signup's address to it and refuses
    to open signup without one, so a key uploaded here must reach the portal unprompted.

    Args:
        conn: The database connection.
        heartbeat: The portal's latest heartbeat, for the fingerprint of the key it holds.
    """
    want = local_settings(conn)
    remote = await _request("GET", "/api/sync/config")
    diff = {}
    if (remote.get("settings") or {}).get("portal_signup_open") != want["portal_signup_open"]:
        diff["portal_signup_open"] = want["portal_signup_open"]
    want_fp = pubkey_fingerprint(want["email_pubkey"]) if want["email_pubkey"] else None
    if heartbeat.get("pubkey_fp") != want_fp:
        diff["email_pubkey"] = want["email_pubkey"]  # None clears it there too
    if not diff:
        return None
    await push_settings(conn, diff)
    return sorted(diff)


# ── the loop ──────────────────────────────────────────────────────────────────

def request_push() -> None:
    """Ask the loop to push soon. Safe to call from a sync endpoint, and cheap to over-call.

    Debounced, so a schedule editor applying a grid of forty slot changes produces one push
    rather than forty.
    """
    if _state.push_event is None or _state.loop is None:
        return
    _state.loop.call_soon_threadsafe(_state.push_event.set)


async def _sync_once(conn) -> dict:
    """One full reconcile. Returns a status dict for the Admin UI."""
    status: dict[str, Any] = {"configured": True, "ok": True, "errors": []}
    try:
        hb = await _request("GET", "/api/sync/heartbeat")
        status["heartbeat"] = hb
        ours = hmac_key_fp()
        status["hmac_key_match"] = (ours is None or hb.get("hmac_key_fp") == ours)

        # Pull first: a signup's picks then count in the snapshot's `remaining` on
        # this tick, not the next one.
        state = _get_state(conn, "volunteers_pull")
        if hb.get("volunteers_rev", 0) > cursor_of(state):
            status["volunteers_pulled"] = await pull_volunteers(conn)

        pushed = await push_dirty_volunteers(conn)
        if pushed:
            status["volunteers_pushed"] = pushed

        settings_pushed = await sync_settings(conn, hb)
        if settings_pushed:
            status["settings_push"] = settings_pushed

        snap_hash = hb.get("snapshot_hash")
        local = build_snapshot(conn)
        if local["content_hash"] != snap_hash:
            status["schedule_push"] = await push_snapshot(conn)

        brand = build_branding(conn)
        if brand["content_hash"] != hb.get("branding_hash"):
            status["branding_push"] = await push_branding(conn)

        # A portal older than schema v7 has no content endpoint and no content_hash.
        if "content_hash" in hb and build_content(conn)["content_hash"] != hb["content_hash"]:
            status["content_push"] = await push_content(conn)

        # A portal older than schema v5 has no invites endpoint and no invites_hash.
        if "invites_hash" in hb and build_invites(conn)["content_hash"] != hb["invites_hash"]:
            status["invites_push"] = await push_signup_invites(conn)
        _mark_ok(conn, "heartbeat")
        _note_outcome(status)
    # Deliberately broad: the sync loop must outlive any failure. The error is recorded in
    # portal_sync_state and shown in the Admin UI, and the next tick tries again.
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        status["ok"] = False
        status["errors"].append(str(exc))
        _mark_error(conn, "heartbeat", str(exc))
        _note_failure(exc)
    return status


async def sync_now() -> dict:
    """Run one reconcile immediately, on its own connection."""
    if not configured():
        return {"configured": False, "detail": f"not set: {', '.join(missing_settings())}"}
    conn = _db.get_conn()
    try:
        async with _state.lock:
            _state.last_status = await _sync_once(conn)
        return _state.last_status
    finally:
        _db.put_conn(conn)


def last_status() -> dict:
    """The result of the most recent sync pass, for the admin status page.

    Returns:
        A copy of the status dict, so the caller can't change the module's state.
    """
    return dict(_state.last_status)


async def _loop_forever():
    _state.push_event = asyncio.Event()
    log.info("portal sync loop started: %s every %ss", PORTAL_SYNC_URL, SYNC_INTERVAL)
    while True:
        await sync_now()
        try:
            # Wake early when a local write asked for a push, otherwise tick on the interval.
            await asyncio.wait_for(_state.push_event.wait(), timeout=SYNC_INTERVAL)
            _state.push_event.clear()
            await asyncio.sleep(PUSH_DEBOUNCE)
            _state.push_event.clear()
        except asyncio.TimeoutError:
            pass


def start(loop: asyncio.AbstractEventLoop) -> None:
    """Start the poller, unless no portal is configured.

    Args:
        loop: The running event loop to schedule the poller on.
    """
    if not configured():
        log.info("portal sync not configured; skipping (not set: %s)", ", ".join(missing_settings()))
        return
    _state.loop = loop
    loop.create_task(_loop_forever())
