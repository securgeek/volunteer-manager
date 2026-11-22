"""The signed sync API. The scheduler is the client; the portal is the server.

Traffic only ever flows scheduler -> portal. That direction is the point: the portal is the
internet-facing service and nothing needs to reach inward to the scheduler, so there is no
callback URL, no webhook, and no inbound hole to punch. The cost is that the scheduler learns
about new signups by asking — hence `heartbeat`, which is one row read.

Three defences, all of which have to hold:
  * a signature over method+path+timestamp+nonce+sha256(body)
  * a +/-300s timestamp window, so a captured request is worth very little for very long
  * a nonce table, so a capture is not worth replaying even inside that window
And one correctness guard: `op_id`, so a push that times out after the portal committed can
be retried without applying twice.
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request

import clock
import content
import db
import security
from config import settings
from mailer import Mailer

router = APIRouter()

# Payload key → (DELETE, INSERT, columns): complete statements, because table and column names
# can't be parameters. Each INSERT lists exactly `columns`, in order; test_portal_local.py
# checks that they agree. remaining defaults to 0 when absent, which reads as full: the safe
# direction.
SNAPSHOT_TABLES = {
    "days": (
        "DELETE FROM snapshot_days",
        "INSERT INTO snapshot_days (day_key, day_str, event_date, sort_order) "
        "VALUES (?, ?, ?, ?)",
        ("day_key", "day_str", "event_date", "sort_order")),
    "times": (
        "DELETE FROM snapshot_times",
        "INSERT INTO snapshot_times (time_key, time_str, sort_order) VALUES (?, ?, ?)",
        ("time_key", "time_str", "sort_order")),
    "day_times": (
        "DELETE FROM snapshot_day_times",
        "INSERT INTO snapshot_day_times (day_key, time_key, remaining) VALUES (?, ?, ?)",
        ("day_key", "time_key", "remaining")),
    "categories": (
        "DELETE FROM snapshot_categories",
        "INSERT INTO snapshot_categories (cat_name, sort_order, cat_color) VALUES (?, ?, ?)",
        ("cat_name", "sort_order", "cat_color")),
    "positions": (
        "DELETE FROM snapshot_positions",
        "INSERT INTO snapshot_positions (pos_key, pos_cat, pos_str) VALUES (?, ?, ?)",
        ("pos_key", "pos_cat", "pos_str")),
    "slots": (
        "DELETE FROM snapshot_slots",
        "INSERT INTO snapshot_slots (day_key, time_key, pos_key, vol_id) VALUES (?, ?, ?, ?)",
        ("day_key", "time_key", "pos_key", "vol_id")),
}

# Re-keying a volunteer renames them everywhere the portal keeps a vol_id.
_REKEY_SQL = (
    "UPDATE sessions SET vol_id = ? WHERE vol_id = ?",
    "UPDATE login_tokens SET vol_id = ? WHERE vol_id = ?",
    "UPDATE vol_picks SET vol_id = ? WHERE vol_id = ?",
    "UPDATE snapshot_slots SET vol_id = ? WHERE vol_id = ?",
)

# What a volunteers push may write. cancelled_at is deliberately absent: it's the volunteer's
# (cancel / rejoin), so a push can never set or clear it. inactive_at is the organizer's.
PUSH_COLUMNS = ("first_name", "last_name", "nickname", "name", "status", "time_pref",
                "add_time", "add_any", "email_hmac", "email_enc", "email_key_fp",
                "answers", "possible_duplicate_of", "inactive_at")

# Updates only the columns a push carries: the scheduler doesn't hold first_name / last_name /
# nickname (signup does), and must not blank them by omission. Parameters: vol_id, the
# PUSH_COLUMNS values, updated_at, rev; then one "was it sent" flag per PUSH_COLUMNS entry; then
# whether anything was sent at all (a push that carries nothing changes nothing on an existing
# row). The column lists must match PUSH_COLUMNS; test_portal_local.py checks.
_UPSERT_VOLUNTEER_SQL = (
    "INSERT INTO volunteers (vol_id, first_name, last_name, nickname, name, status, time_pref, "
    "add_time, add_any, email_hmac, email_enc, email_key_fp, answers, possible_duplicate_of, "
    "inactive_at, updated_at, rev) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(vol_id) DO UPDATE SET "
    "first_name = CASE WHEN ? THEN excluded.first_name ELSE volunteers.first_name END, "
    "last_name = CASE WHEN ? THEN excluded.last_name ELSE volunteers.last_name END, "
    "nickname = CASE WHEN ? THEN excluded.nickname ELSE volunteers.nickname END, "
    "name = CASE WHEN ? THEN excluded.name ELSE volunteers.name END, "
    "status = CASE WHEN ? THEN excluded.status ELSE volunteers.status END, "
    "time_pref = CASE WHEN ? THEN excluded.time_pref ELSE volunteers.time_pref END, "
    "add_time = CASE WHEN ? THEN excluded.add_time ELSE volunteers.add_time END, "
    "add_any = CASE WHEN ? THEN excluded.add_any ELSE volunteers.add_any END, "
    "email_hmac = CASE WHEN ? THEN excluded.email_hmac ELSE volunteers.email_hmac END, "
    "email_enc = CASE WHEN ? THEN excluded.email_enc ELSE volunteers.email_enc END, "
    "email_key_fp = CASE WHEN ? THEN excluded.email_key_fp ELSE volunteers.email_key_fp END, "
    "answers = CASE WHEN ? THEN excluded.answers ELSE volunteers.answers END, "
    "possible_duplicate_of = CASE WHEN ? THEN excluded.possible_duplicate_of "
    "ELSE volunteers.possible_duplicate_of END, "
    "inactive_at = CASE WHEN ? THEN excluded.inactive_at ELSE volunteers.inactive_at END, "
    "updated_at = excluded.updated_at, rev = excluded.rev "
    "WHERE ?"
)


# ── request authentication ────────────────────────────────────────────────────

async def verified_body(request: Request,
                        conn: sqlite3.Connection = Depends(db.get_db)) -> bytes:
    """Authenticate a sync request and return its raw body.

    Returns 401 for every failure without distinguishing them: a caller who cannot sign has
    no business learning whether it was the clock, the nonce, or the key that let them down.

    Args:
        request: The incoming request, with its signature headers.
        conn: The database connection, for the nonce record.
    """
    key_id = request.headers.get("x-sync-key-id", "")
    timestamp = request.headers.get("x-sync-timestamp", "")
    nonce = request.headers.get("x-sync-nonce", "")
    signature = request.headers.get("x-sync-signature", "")
    if not (key_id and timestamp and nonce and signature):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    now_epoch = int(clock.now().timestamp())
    if abs(now_epoch - ts) > settings.sync_skew_sec:
        raise HTTPException(status_code=401, detail="Unauthorized")

    candidates = settings.secrets_for(key_id)
    if not candidates:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.body()
    # Sign the full request target, query string included. A pull's ?since= cursor is not
    # dangerous to tamper with today, but leaving any part of the target unsigned invites a
    # later endpoint where it is.
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    if not security.verify_request(candidates, signature, request.method,
                                   target, timestamp, nonce, body):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Sweep first so the table cannot grow without bound, then claim the nonce. The INSERT
    # is the atomic part: a duplicate violates the primary key, which is the replay.
    conn.execute("DELETE FROM sync_nonces WHERE seen_at < ?",
                 (now_epoch - settings.sync_skew_sec * 2,))
    try:
        conn.execute("INSERT INTO sync_nonces (nonce, seen_at) VALUES (?, ?)", (nonce, now_epoch))
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    return body


def _json_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Body must be JSON") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    return parsed


def _idempotent(conn: sqlite3.Connection, payload: dict) -> tuple[str, Optional[dict]]:
    """Return (op_id, stored_result). A non-None result means: replay it, do nothing else."""
    op_id = payload.get("op_id")
    if not op_id or not isinstance(op_id, str):
        raise HTTPException(status_code=400, detail="op_id is required on a push")
    row = conn.execute("SELECT result FROM sync_ops WHERE op_id = ?", (op_id,)).fetchone()
    if row:
        return op_id, json.loads(row["result"])
    return op_id, None


def _record_op(conn: sqlite3.Connection, op_id: str, result: dict) -> dict:
    conn.execute("INSERT INTO sync_ops (op_id, applied_at, result) VALUES (?, ?, ?)",
                 (op_id, clock.now_iso(), json.dumps(result)))
    return result


# ── pull ──────────────────────────────────────────────────────────────────────

@router.get("/api/sync/heartbeat")
async def heartbeat(body: bytes = Depends(verified_body),
                    conn: sqlite3.Connection = Depends(db.get_db)):
    """Cheap poll target: what has moved, and do our keys agree.

    The fingerprints ride along on every heartbeat so a key mismatch surfaces as a banner in
    the organizer UI within one poll, instead of as volunteers who mysteriously cannot log in.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    revs = conn.execute(
        "SELECT (SELECT COALESCE(MAX(rev), 0) FROM volunteers) AS volunteers_rev, "
        "(SELECT COALESCE(MAX(rev), 0) FROM announcements) AS announcements_rev").fetchone()
    snap = conn.execute("SELECT snapshot_rev, content_hash FROM snapshot_meta WHERE id = 1").fetchone()
    brand = conn.execute("SELECT content_hash FROM branding WHERE id = 1").fetchone()
    content_row = conn.execute("SELECT content_hash FROM portal_content WHERE id = 1").fetchone()
    pubkey = db.kv_get(conn, "email_pubkey")
    return {
        "rev": db.current_rev(conn),
        "volunteers_rev": revs["volunteers_rev"],
        "announcements_rev": revs["announcements_rev"],
        "snapshot_rev": snap["snapshot_rev"] if snap else 0,
        "snapshot_hash": snap["content_hash"] if snap else None,
        "branding_hash": brand["content_hash"] if brand else None,
        "invites_hash": db.kv_get(conn, "signup_invites_hash"),
        # "" before the first content push, so a scheduler that knows the endpoint sends one.
        "content_hash": (content_row["content_hash"] if content_row else None) or "",
        "pubkey_fp": security.pubkey_fingerprint(pubkey) if pubkey else None,
        "hmac_key_fp": security.hmac_key_fp(settings.email_hmac_key),
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
    }


@router.get("/api/sync/volunteers")
async def pull_volunteers(since: int = 0, limit: int = 500,
                          body: bytes = Depends(verified_body),
                          conn: sqlite3.Connection = Depends(db.get_db)):
    """Volunteers changed since a cursor, oldest first, with their timeslot picks.

    Args:
        since: The cursor from the last pull. 0 for everything.
        limit: Page size, clamped to 1-1000.
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    limit = max(1, min(limit, 1000))
    rows = conn.execute(
        "SELECT * FROM volunteers WHERE rev > ? ORDER BY rev LIMIT ?", (since, limit)
    ).fetchall()
    out = []
    for r in rows:
        picks = conn.execute(
            "SELECT day_key, time_key, kind, rank FROM vol_picks WHERE vol_id = ? "
            "ORDER BY kind DESC, rank, day_key, time_key", (r["vol_id"],),
        ).fetchall()
        rec = dict(r)
        rec["picks"] = [dict(p) for p in picks]
        out.append(rec)
    return {"volunteers": out,
            "cursor": out[-1]["rev"] if out else since,
            "more": len(out) == limit}


@router.get("/api/sync/config")
async def pull_config(body: bytes = Depends(verified_body),
                      conn: sqlite3.Connection = Depends(db.get_db)):
    """Portal settings and key fingerprints. Never key material itself.

    The HMAC key is not here on purpose: both services read it from their own environment, so
    it never crosses the wire, and this fingerprint is how they confirm they agree.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    keys = ("portal_signup_open", "portal_login_disclose", "email_pubkey")
    kv = {k: db.kv_get(conn, k) for k in keys}
    pubkey = kv.pop("email_pubkey")
    return {
        "settings": kv,
        "pubkey_fp": security.pubkey_fingerprint(pubkey) if pubkey else None,
        "hmac_key_fp": security.hmac_key_fp(settings.email_hmac_key),
    }


# ── push ──────────────────────────────────────────────────────────────────────

@router.post("/api/sync/schedule")
async def push_schedule(body: bytes = Depends(verified_body),
                        conn: sqlite3.Connection = Depends(db.get_db)):
    """Replace the schedule snapshot wholesale.

    A full replace rather than a diff: the snapshot is a copy of someone else's state, and
    reconciling a diff against a copy is how copies drift. `content_hash` makes an unchanged
    push free, which is what lets the scheduler re-push freely whenever it is unsure.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored

    incoming_hash = payload.get("content_hash") or ""
    current = conn.execute("SELECT content_hash FROM snapshot_meta WHERE id = 1").fetchone()
    if incoming_hash and current and current["content_hash"] == incoming_hash:
        return _record_op(conn, op_id, {"ok": True, "unchanged": True})

    conn.execute("BEGIN IMMEDIATE")
    try:
        for key, (delete_sql, insert_sql, cols) in SNAPSHOT_TABLES.items():
            conn.execute(delete_sql)
            rows = payload.get(key) or []
            if not rows:
                continue
            conn.executemany(
                insert_sql,
                [tuple(r.get(c, 0 if c == "remaining" else None) for c in cols) for r in rows],
            )
        rev = db.next_rev(conn)
        conn.execute(
            "INSERT INTO snapshot_meta (id, snapshot_rev, content_hash, pushed_at) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "snapshot_rev = excluded.snapshot_rev, content_hash = excluded.content_hash, "
            "pushed_at = excluded.pushed_at",
            (rev, incoming_hash, clock.now_iso()),
        )
        result = _record_op(conn, op_id, {
            "ok": True, "snapshot_rev": rev,
            "counts": {k: len(payload.get(k) or []) for k in SNAPSHOT_TABLES},
        })
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


@router.post("/api/sync/volunteers")
async def push_volunteers(body: bytes = Depends(verified_body),
                          conn: sqlite3.Connection = Depends(db.get_db)):
    """Upsert volunteers from the scheduler, and apply vol_id re-keys.

    A re-key cascades to sessions, tokens, and picks so an organizer assigning a badge
    number does not silently sign that volunteer out of a session they still have open.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored

    upserts = payload.get("volunteers") or []
    rekeys = payload.get("rekeys") or []
    cols = PUSH_COLUMNS
    # These columns are NOT NULL in the schema. A SQLite DEFAULT only fills a column that is
    # *omitted*, not one explicitly given NULL — so a push that leaves out first_name would
    # fail on the constraint unless we substitute here.
    non_null = {"first_name": "", "last_name": "", "nickname": "", "name": "",
                "status": "pending", "time_pref": "", "add_time": "", "add_any": 0}

    def _val(v: dict, col: str):
        got = v.get(col)
        if got is None and col in non_null:
            return non_null[col]
        return got

    conn.execute("BEGIN IMMEDIATE")
    try:
        renamed = 0
        for rk in rekeys:
            old, new = rk.get("vol_id"), rk.get("new_vol_id")
            if not old or not new or old == new:
                continue
            cur = conn.execute("UPDATE volunteers SET vol_id = ?, rev = ? WHERE vol_id = ?",
                               (new, db.next_rev(conn), old))
            if cur.rowcount:
                for stmt in _REKEY_SQL:
                    conn.execute(stmt, (new, old))
                renamed += 1
                db.audit(conn, "sync.rekey", new, detail=f"was {old}")

        written = 0
        for v in upserts:
            vol_id = v.get("vol_id")
            if not vol_id:
                continue
            rev = db.next_rev(conn)
            # Update only what the push carries. The scheduler doesn't hold first_name /
            # last_name / nickname (signup does), and must not blank them by omission. A push
            # left with nothing to update (say, only a status the guard dropped) changes
            # nothing on an existing row.
            sent = [c in v for c in cols]
            conn.execute(
                _UPSERT_VOLUNTEER_SQL,
                (vol_id, *(_val(v, c) for c in cols), clock.now_iso(), rev, *sent, any(sent)),
            )
            if "picks" in v:
                conn.execute("DELETE FROM vol_picks WHERE vol_id = ?", (vol_id,))
                conn.executemany(
                    "INSERT OR REPLACE INTO vol_picks (vol_id, day_key, time_key, kind, rank) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(vol_id, p.get("day_key"), p.get("time_key"), p.get("kind", "pref"),
                      p.get("rank")) for p in (v.get("picks") or [])],
                )
            written += 1
        result = _record_op(conn, op_id, {"ok": True, "written": written, "renamed": renamed,
                                         "rev": db.current_rev(conn)})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


# The host, then nothing or a path, query or fragment. The host can't contain the character
# that starts the rest, so the match can't backtrack (CodeQL #5). Same links as before.
_PUBLIC_LINK = re.compile(r"^https?://[^\s/?#]+(?:[/?#]\S*)?$")


def _public_link(raw: Any, what: str) -> Optional[str]:
    """Check a pushed link before the volunteer pages show it.

    The scheduler checks the same rule. This is the second net: an ``href`` is the one place a
    pushed string could run script on a public page, so only ``http(s)`` with a host passes.

    Args:
        raw: The pushed value.
        what: The field's name, for the error.

    Returns:
        The link, or ``None`` when absent or blank.

    Raises:
        HTTPException 400: Anything else.
    """
    if raw in (None, ""):
        return None
    if (not isinstance(raw, str) or len(raw) > MAX_BRANDING_TEXT
            or not _PUBLIC_LINK.match(raw)):
        raise HTTPException(status_code=400, detail=f"{what} must be an http(s) link")
    return raw


def _public_contact(payload: dict) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """The branding's contact address and links, checked.

    Absent from a scheduler older than portal schema v6, which reads as "none of them".

    Args:
        payload: The branding push.

    Returns:
        ``(contact_email, conference_url, code_of_conduct_url)``, each ``None`` when unset.

    Raises:
        HTTPException 400: An address or link that fails its check.
    """
    email = payload.get("contact_email") or None
    if email is not None and (not isinstance(email, str) or len(email) > MAX_BRANDING_TEXT
                              or not _CONTACT_ADDR.match(email)):
        raise HTTPException(status_code=400, detail="contact_email must be one plain address")
    return (email, _public_link(payload.get("conference_url"), "conference_url"),
            _public_link(payload.get("code_of_conduct_url"), "code_of_conduct_url"))


@router.post("/api/sync/branding")
async def push_branding(body: bytes = Depends(verified_body),
                        conn: sqlite3.Connection = Depends(db.get_db)):
    """Conference title, the theme logo, its favicon, the header logo, and the public contact.

    The scheduler sends data URLs because that is how it stores them; the portal decodes once
    and keeps bytes, so it can serve them with an ETag instead of inlining megabytes of base64
    into every page load.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    import base64

    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored

    incoming_hash = payload.get("content_hash") or ""
    current = conn.execute("SELECT content_hash FROM branding WHERE id = 1").fetchone()
    if incoming_hash and current and current["content_hash"] == incoming_hash:
        return _record_op(conn, op_id, {"ok": True, "unchanged": True})

    def _decode(data_url: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
        if not data_url:
            return None, None
        if not data_url.startswith("data:"):
            raise HTTPException(status_code=400, detail="logo/favicon must be a data URL")
        header, _, b64 = data_url.partition(",")
        mime = header[5:].split(";")[0] or "application/octet-stream"
        try:
            return base64.b64decode(b64), mime
        except Exception:
            raise HTTPException(status_code=400,
                                detail="logo/favicon base64 is malformed") from None

    contact_email, conference_url, coc_url = _public_contact(payload)
    logo, logo_mime = _decode(payload.get("logo"))
    favicon, _ = _decode(payload.get("favicon"))
    # Absent from a scheduler that predates it: that reads as "no header logo".
    header_logo, header_logo_mime = _decode(payload.get("header_logo"))
    conn.execute(
        "INSERT INTO branding (id, app_title, portal_title, logo, logo_mime, favicon, "
        "header_logo, header_logo_mime, contact_email, conference_url, code_of_conduct_url, "
        "content_hash, pushed_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET app_title = excluded.app_title, "
        "portal_title = excluded.portal_title, logo = excluded.logo, "
        "logo_mime = excluded.logo_mime, favicon = excluded.favicon, "
        "header_logo = excluded.header_logo, header_logo_mime = excluded.header_logo_mime, "
        "contact_email = excluded.contact_email, conference_url = excluded.conference_url, "
        "code_of_conduct_url = excluded.code_of_conduct_url, "
        "content_hash = excluded.content_hash, pushed_at = excluded.pushed_at",
        (payload.get("app_title"), payload.get("portal_title"), logo, logo_mime, favicon,
         header_logo, header_logo_mime, contact_email, conference_url, coc_url,
         incoming_hash, clock.now_iso()),
    )
    return _record_op(conn, op_id, {"ok": True, "logo_bytes": len(logo or b""),
                                    "favicon_bytes": len(favicon or b""),
                                    "header_logo_bytes": len(header_logo or b"")})


def _iso_or_none(raw, what: str) -> Optional[str]:
    """An ISO-8601 timestamp, normalised to the portal's own format so text comparison works."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=f"{what} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{what} must be a timestamp") from None
    if parsed.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"{what} needs a timezone")
    return clock.iso(parsed)


@router.post("/api/sync/signup_invites")
async def push_signup_invites(body: bytes = Depends(verified_body),
                              conn: sqlite3.Connection = Depends(db.get_db)):
    """The scheduler's early-access invites, replacing the portal's list wholesale.

    Each is {invite_id, token_hash, expires_at, revoked_at}: a hash, never the link. An invite
    the scheduler no longer lists is dropped. `used_at` is the portal's (set at signup submit)
    and survives every push; the scheduler learns of a use from the volunteers pull, which
    carries the volunteer's `invite_id`.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    invites = payload.get("invites")
    if not isinstance(invites, list):
        raise HTTPException(status_code=400, detail="invites must be a list")
    rows = []
    for inv in invites:
        if not isinstance(inv, dict):
            raise HTTPException(status_code=400, detail="each invite must be an object")
        invite_id, token_hash = inv.get("invite_id"), inv.get("token_hash")
        if not (isinstance(invite_id, str) and invite_id and isinstance(token_hash, str)
                and len(token_hash) == 64):
            raise HTTPException(status_code=400, detail="invite_id and a SHA-256 token_hash "
                                "are required")
        expires = _iso_or_none(inv.get("expires_at"), "expires_at")
        if expires is None:
            raise HTTPException(status_code=400, detail="expires_at is required")
        rows.append((invite_id, token_hash, expires, _iso_or_none(inv.get("revoked_at"),
                                                                    "revoked_at")))
    keep = [r[0] for r in rows]
    conn.execute("DELETE FROM signup_invites WHERE invite_id NOT IN "
                 "(SELECT value FROM json_each(?))", (json.dumps(keep),))
    conn.executemany(
        "INSERT INTO signup_invites (invite_id, token_hash, expires_at, revoked_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(invite_id) DO UPDATE SET "
        "token_hash = excluded.token_hash, expires_at = excluded.expires_at, "
        "revoked_at = excluded.revoked_at", rows)
    db.kv_set(conn, "signup_invites_hash", payload.get("content_hash") or "")
    return _record_op(conn, op_id, {"ok": True, "invites": len(rows)})


# Purge: everything the portal holds about a volunteer, keyed by their ids or their address
# hash. One literal statement each; json_each takes the lists. Rate-limit buckets are keyed
# "{scope}:addr:{window}:{email_hmac}" (ratelimit.check), and a hex hash holds no LIKE
# wildcard. snapshot_slots is the scheduler's copy and is re-pushed after its own delete; the
# slot is cleared here too so My info can't show a shift for a row that no longer exists.
_PURGE_HMACS_SQL = ("SELECT email_hmac FROM volunteers WHERE email_hmac IS NOT NULL "
                    "AND vol_id IN (SELECT value FROM json_each(?))")
_PURGE_SQL = (
    "DELETE FROM sessions WHERE vol_id IN (SELECT value FROM json_each(?)) "
    "OR email_hmac IN (SELECT value FROM json_each(?))",
    "DELETE FROM login_tokens WHERE vol_id IN (SELECT value FROM json_each(?)) "
    "OR email_hmac IN (SELECT value FROM json_each(?))",
)
_PURGE_BY_ID_SQL = (
    "DELETE FROM vol_picks WHERE vol_id IN (SELECT value FROM json_each(?))",
    "UPDATE snapshot_slots SET vol_id = NULL WHERE vol_id IN (SELECT value FROM json_each(?))",
)
_PURGE_RATE_SQL = ("DELETE FROM rate_limits WHERE EXISTS (SELECT 1 FROM json_each(?) AS h "
                   "WHERE rate_limits.bucket_key LIKE '%:addr:%:' || h.value)")
_PURGE_VOL_SQL = "DELETE FROM volunteers WHERE vol_id = ?"
PURGE_MAX = 500


@router.post("/api/sync/purge_volunteers")
async def purge_volunteers(body: bytes = Depends(verified_body),
                           conn: sqlite3.Connection = Depends(db.get_db)):
    """Delete volunteers outright, so the same address can sign up again from scratch.

    For clearing test volunteers. Takes {op_id, vol_ids}; removes each row, its picks, every
    session and sign-in link for its id or its address hash, and that address's rate-limit
    counters, in one transaction. An id the portal doesn't have is skipped. The audit line
    records the id. The scheduler calls this before deleting its own copy, so a pull can't
    bring the volunteer back.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    vol_ids = payload.get("vol_ids")
    if (not isinstance(vol_ids, list) or len(vol_ids) > PURGE_MAX
            or not all(isinstance(v, str) and v for v in vol_ids)):
        raise HTTPException(status_code=400,
                            detail=f"vol_ids must be a list of up to {PURGE_MAX} ids")
    ids = json.dumps(sorted(set(vol_ids)))
    conn.execute("BEGIN IMMEDIATE")
    try:
        hmacs = json.dumps([r["email_hmac"] for r in conn.execute(_PURGE_HMACS_SQL, (ids,))])
        for stmt in _PURGE_SQL:
            conn.execute(stmt, (ids, hmacs))
        for stmt in _PURGE_BY_ID_SQL:
            conn.execute(stmt, (ids,))
        conn.execute(_PURGE_RATE_SQL, (hmacs,))
        purged = []
        for vol_id in sorted(set(vol_ids)):
            if conn.execute(_PURGE_VOL_SQL, (vol_id,)).rowcount:
                purged.append(vol_id)
                db.audit(conn, "volunteer.purged", vol_id)
        result = _record_op(conn, op_id, {"ok": True, "purged": purged})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


@router.post("/api/sync/announcements")
async def push_announcements(body: bytes = Depends(verified_body),
                             conn: sqlite3.Connection = Depends(db.get_db)):
    """Replace the announcement set.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    items = payload.get("announcements") or []
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM announcements")
        for a in items:
            conn.execute(
                "INSERT INTO announcements (ann_id, title, body, starts_at, ends_at, rev) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (a.get("ann_id"), a.get("title", ""), a.get("body", ""),
                 a.get("starts_at"), a.get("ends_at"), db.next_rev(conn)),
            )
        result = _record_op(conn, op_id, {"ok": True, "count": len(items)})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


@router.post("/api/sync/settings")
async def push_settings(body: bytes = Depends(verified_body),
                        conn: sqlite3.Connection = Depends(db.get_db)):
    """Set portal_kv entries — signup open/closed, the email public key, login copy.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    allowed = {"portal_signup_open", "portal_login_disclose", "email_pubkey", "portal_title"}
    incoming = payload.get("settings") or {}
    rejected = sorted(set(incoming) - allowed)
    if rejected:
        raise HTTPException(status_code=400, detail=f"Unknown settings: {rejected}")
    for k, v in incoming.items():
        if v is None:
            conn.execute("DELETE FROM portal_kv WHERE k = ?", (k,))
        else:
            db.kv_set(conn, k, str(v))
    return _record_op(conn, op_id, {"ok": True, "set": sorted(incoming)})


ADMIN_MAIL_PER_HOUR = 20
# Early-access signup invites, sent in batches from the admin page (one every 10 s there).
# Their own allowance, so a batch can't starve admin mail, and below the relay's 300 an hour
# (shared with every sign-in link) so volunteers can still get theirs mid-batch.
SIGNUP_INVITE_MAIL_PER_HOUR = 200
# The domain is dot-separated labels, each without a dot, so the match can't backtrack (a
# domain part of [^@\s]+\.[^@\s]+ could, quadratically: CodeQL #3, #4, #6). It also refuses an
# empty label, as in a@.b or a@b..c, which were never deliverable anyway.
_ADDR = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
# The branding's contact address: one plain address, as the scheduler checks it too.
_CONTACT_ADDR = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAX_BRANDING_TEXT = 300
_ADMIN_PATHS = {"invite": "/admin/invite?t=", "reset": "/admin/reset?t=",
                "signup_invite": "/apply/signup?invite="}
# Sent in the last hour, per allowance. One complete statement each.
_ADMIN_MAIL_COUNT_SQL = (
    "SELECT COUNT(*) FROM portal_audit WHERE action = 'admin_mail.sent' "
    "AND detail IN ('invite', 'reset') AND ts > ?")
_INVITE_MAIL_COUNT_SQL = (
    "SELECT COUNT(*) FROM portal_audit WHERE action = 'admin_mail.sent' "
    "AND detail = 'signup_invite' AND ts > ?")


def _invite_template(conn: sqlite3.Connection) -> dict:
    """The early-access invite template in force: the pushed one, else the default.

    Args:
        conn: The database connection.

    Returns:
        ``{subject, body}``.
    """
    row = conn.execute("SELECT invite FROM portal_content WHERE id = 1").fetchone()
    return json.loads(row["invite"]) if row and row["invite"] else content.DEFAULT_INVITE


def _invite_values(conn: sqlite3.Connection, link: str, payload: dict) -> dict:
    """What an invite's template may use: the scheduler's fields and the branding's contact.

    Args:
        conn: The database connection.
        link: The checked signup link.
        payload: The send request, for ``first_name``, ``app_title`` and ``expires``.

    Returns:
        A string for each of ``content.INVITE_VARS``.
    """
    brand = conn.execute("SELECT app_title, contact_email, conference_url, code_of_conduct_url "
                         "FROM branding WHERE id = 1").fetchone()
    brand = dict(brand) if brand else {}
    return {"first_name": str(payload.get("first_name") or ""),
            "conference": str(payload.get("app_title") or brand.get("app_title") or ""),
            "link": link, "expires": str(payload.get("expires") or ""),
            "contact_email": brand.get("contact_email") or "",
            "conference_url": brand.get("conference_url") or "",
            "code_of_conduct_url": brand.get("code_of_conduct_url") or ""}


@router.post("/api/sync/content")
async def push_content(body: bytes = Depends(verified_body),
                       conn: sqlite3.Connection = Depends(db.get_db)):
    """The organizers' landing page, questionnaire and invite email, replacing what's here.

    Each part is a definition or ``null`` for the default. All three are checked here by
    ``content.validate_content`` before anything is stored, whatever the scheduler checked:
    they become a public page, the signup form's rules, and an email.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    try:
        clean = content.validate_content(payload)
    except content.ContentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    as_text = {k: None if v is None else json.dumps(v) for k, v in clean.items()}
    conn.execute(
        "INSERT INTO portal_content (id, landing, questionnaire, invite, content_hash, "
        "pushed_at) VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "landing = excluded.landing, questionnaire = excluded.questionnaire, "
        "invite = excluded.invite, content_hash = excluded.content_hash, "
        "pushed_at = excluded.pushed_at",
        (as_text["landing"], as_text["questionnaire"], as_text["invite"],
         str(payload.get("content_hash") or ""), clock.now_iso()))
    db.audit(conn, "content.pushed",
             detail=", ".join(k for k, v in clean.items() if v is not None) or "defaults")
    return _record_op(conn, op_id, {"ok": True})


@router.post("/api/sync/send_admin_mail")
async def send_admin_mail(body: bytes = Depends(verified_body),
                          conn: sqlite3.Connection = Depends(db.get_db)):
    """Mail an admin invite, password reset or volunteer signup invite for the scheduler.

    The one exception to "the portal only mails an address someone has just proved they
    know" (CLAUDE.md), and so kept narrow: a signed request, one of three fixed templates, and
    a link that must be this deployment's own — the organizer site's (ADMIN_PUBLIC_URL)
    /admin/invite or /admin/reset, or the portal's own (PORTAL_PUBLIC_URL) /apply/signup?invite=.
    Admin mail is at most ADMIN_MAIL_PER_HOUR, signup invites SIGNUP_INVITE_MAIL_PER_HOUR.
    Neither the address nor the link is stored; the audit line records the kind. op_id makes a
    retried request a replay, not a second email.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    kind, to, link = payload.get("kind"), payload.get("to"), payload.get("link")
    if kind not in _ADMIN_PATHS:
        raise HTTPException(status_code=400, detail="Unknown kind")
    if not isinstance(to, str) or len(to) > 254 or not _ADDR.match(to):
        raise HTTPException(status_code=400, detail="Bad address")
    base = settings.base_url if kind == "signup_invite" else settings.admin_url
    if not isinstance(link, str) or not link.startswith(base + _ADMIN_PATHS[kind]) \
            or any(c.isspace() for c in link):
        raise HTTPException(status_code=400, detail="That link isn't this site's")
    since = clock.iso(clock.now() - timedelta(hours=1))
    if kind == "signup_invite":
        sql, cap, what = _INVITE_MAIL_COUNT_SQL, SIGNUP_INVITE_MAIL_PER_HOUR, "invite"
    else:
        sql, cap, what = _ADMIN_MAIL_COUNT_SQL, ADMIN_MAIL_PER_HOUR, "admin"
    if conn.execute(sql, (since,)).fetchone()[0] >= cap:
        raise HTTPException(status_code=429, detail=f"Too much {what} mail this hour")
    try:
        if kind == "signup_invite":
            # The organizers' own template (Volunteer Portal → Invite email), else the default.
            subject, text = content.render_invite(_invite_template(conn),
                                                  _invite_values(conn, link, payload))
            Mailer(settings).send(to, subject, text)
        else:
            Mailer(settings).send_admin(kind, to, link, payload)
    except Exception as exc:                                    # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Mail failed: {exc}") from exc
    db.audit(conn, "admin_mail.sent", detail=kind)
    return _record_op(conn, op_id, {"ok": True, "kind": kind})


@router.post("/api/sync/email_index")
async def push_email_index(body: bytes = Depends(verified_body),
                           conn: sqlite3.Connection = Depends(db.get_db)):
    """Backfill {vol_id, email_hmac} pairs produced offline by vol_mail.py index.

    Existing volunteers have ciphertext but no HMAC, and nothing on a server can read the
    ciphertext — so this is the only way they can ever sign in, and it necessarily comes from
    the machine holding the private key.

    Args:
        body: The raw body, after the signature check (`verified_body`).
        conn: The database connection.
    """
    payload = _json_body(body)
    op_id, stored = _idempotent(conn, payload)
    if stored is not None:
        return stored
    pairs = payload.get("pairs") or []
    updated, conflicts = 0, []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for pair in pairs:
            vol_id, addr_hash = pair.get("vol_id"), pair.get("email_hmac")
            if not vol_id or not addr_hash:
                continue
            try:
                cur = conn.execute(
                    "UPDATE volunteers SET email_hmac = ?, rev = ? WHERE vol_id = ?",
                    (addr_hash, db.next_rev(conn), vol_id),
                )
                updated += cur.rowcount
            except sqlite3.IntegrityError:
                # Two vol_ids resolving to one address is a real duplicate for an organizer
                # to merge, not something to paper over here.
                conflicts.append(vol_id)
        result = _record_op(conn, op_id, {"ok": True, "updated": updated,
                                         "conflicts": conflicts})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result
