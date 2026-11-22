"""Volunteer signup, email first (vol_signup.md §7-§8).

Step order: email -> verify -> name and questionnaire -> exactly two timeslots -> submit.

Properties this file exists to keep:

* **No volunteer row for an unverified address.** check_email mails a signup link instead of
  creating anything; the click yields a *signup session* (vol_id IS NULL) that authorises the
  name check and the submit, and nothing else.
* **The address is never stored readably.** It's HMACed for lookup and RSA-encrypted to the
  organizers' public key, both at check_email, while the portal briefly has it in hand.
  Signup is closed while the portal has no public key, so there is never a row an organizer
  couldn't contact.
* **The unique index on email_hmac is the duplicate guarantee.** check_email is UX; a race on
  one address loses to the index and gets the same "already signed up" answer.
* **Nobody else is ever described.** check_name says only whether *some* volunteer has a
  similar name, never who or how many, and the duplicate list it implies is recomputed on the
  server at submit rather than taken from the browser.
* **Picks are re-checked inside the write.** A timeslot that filled while someone was filling
  in the form is refused at submit with its key, so the form can reopen the shifts step.
* **The two picks can't overlap.** Same day, ranges intersecting (from each time_str, e.g.
  1130-1530 against 1500-1900): nobody can work both, and counting them as demand on both is
  how the old bands overstated the 1130 slot.
"""
import json
import re
import secrets
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request

import clock
import db
import ratelimit
import security
import signup_fields
from config import settings
from mailer import Mailer
from routers.auth import (EDITABLE_STATUSES, is_live, _app_title, _client_ip, _floor, _issue_token, _ua,
                          get_session, me_payload, require_volunteer)

router = APIRouter()

NAME_MAX = 60
HANDLE_MAX = 40
EXTRAS_MAX = 20
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ── state ─────────────────────────────────────────────────────────────────────

_RANGE = re.compile(r"^\s*(\d{3,4})\s*-\s*(\d{3,4})")


def time_range(time_str: Optional[str]) -> Optional[tuple]:
    """Minutes since midnight for a time_str like '1130-1530'; None if it isn't one.

    The scheduler's routers/schedule.py has its own copy for its picker; the two apps share
    no code. Keep them in step.

    Args:
        time_str: A range like `'1130-1530'`. One that ends before it starts runs past midnight.
    """
    m = _RANGE.match(time_str or "")
    if not m:
        return None
    start, end = (int(x.zfill(4)[:2]) * 60 + int(x.zfill(4)[2:]) for x in m.groups())
    if end <= start:
        end += 24 * 60
    return start, end


def overlap(a, b) -> bool:
    """Whether two timeslot rows (day_key, time_key, time_str) clash.

    Args:
        a: One timeslot row.
        b: The other.
    """
    if a["day_key"] != b["day_key"]:
        return False
    if a["time_key"] == b["time_key"]:
        return True
    ra, rb = time_range(a["time_str"]), time_range(b["time_str"])
    return bool(ra and rb and ra[0] < rb[1] and rb[0] < ra[1])


def slot_key(row) -> str:
    """`day_time`: neither key may contain an underscore (backend routers/schedule.py).

    Args:
        row: A row with `day_key` and `time_key`.
    """
    return f"{row['day_key']}_{row['time_key']}"


def _timeslots(conn: sqlite3.Connection) -> list:
    return conn.execute(
        "SELECT dt.day_key, dt.time_key, dt.remaining, d.day_str, d.event_date, t.time_str "
        "FROM snapshot_day_times dt "
        "LEFT JOIN snapshot_days d ON d.day_key = dt.day_key "
        "LEFT JOIN snapshot_times t ON t.time_key = dt.time_key "
        "ORDER BY COALESCE(d.event_date, ''), COALESCE(d.sort_order, 0), dt.day_key, "
        "COALESCE(t.sort_order, 0), dt.time_key"
    ).fetchall()


def _label(row) -> str:
    return f"{row['day_str'] or row['day_key']} {row['time_str'] or row['time_key']}"


def signup_state(conn: sqlite3.Connection) -> str:
    """'open', 'closed', or 'full'.

    Closed: an organizer hasn't opened signup, or there's no public key to encrypt addresses
    to. Full: open, but no two timeslots with space can be picked together (they'd overlap,
    or there aren't two), so the form couldn't be completed — a terminal screen beats a form
    that can't submit.

    Args:
        conn: The database connection.
    """
    if db.kv_get(conn, "portal_signup_open") != "1" or not db.kv_get(conn, "email_pubkey"):
        return "closed"
    return "full" if _shifts_full(conn) else "open"


def _shifts_full(conn: sqlite3.Connection) -> bool:
    """No two timeslots with space that can be picked together. Separate from signup_state so
    an invited signup, which gets past "closed", is still stopped by "full"."""
    avail = [r for r in _timeslots(conn) if r["remaining"] > 0]
    return not any(not overlap(a, b) for i, a in enumerate(avail) for b in avail[i + 1:])


# ── early-access invites ──────────────────────────────────────────────────────
# Made in the scheduler, pushed here as hashes (routers/sync.py push_signup_invites). A live
# invite gets a signup past "closed", never past "full". It's checked when the email is, rides
# on the login token and the session, and is consumed at submit.

INVITE_GONE = "That invite link has expired or was already used. Please contact the organizers."


def _invite_status(row: Optional[sqlite3.Row]) -> str:
    """'valid', 'used', 'expired', 'revoked' or 'unknown'."""
    if row is None:
        return "unknown"
    if row["revoked_at"]:
        return "revoked"
    if row["used_at"]:
        return "used"
    if row["expires_at"] <= clock.now_iso():
        return "expired"
    return "valid"


def _invite_by_token(conn: sqlite3.Connection, token: str) -> Optional[sqlite3.Row]:
    if not token:
        return None
    return conn.execute("SELECT * FROM signup_invites WHERE token_hash = ?",
                        (security.hash_secret(token),)).fetchone()


def _consume_invite(conn: sqlite3.Connection, invite_id: Optional[str]) -> bool:
    """Use up a live invite; one conditional UPDATE, so two submits can't both have it."""
    if not invite_id:
        return False
    now = clock.now_iso()
    return conn.execute(
        "UPDATE signup_invites SET used_at = ? WHERE invite_id = ? AND used_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ?", (now, invite_id, now)).rowcount == 1


@router.get("/api/portal/signup/invite")
def invite_status(t: str = "", conn: sqlite3.Connection = Depends(db.get_db)):
    """Whether an early-access link still works, so the page can say so before anyone types.

    Read-only: it never consumes the invite, and says nothing about who it was for.

    Args:
        t: The token from the early-access link.
        conn: The database connection.
    """
    return {"status": _invite_status(_invite_by_token(conn, t))}


def _require_open(conn: sqlite3.Connection, invited: bool = False) -> None:
    """For starting a signup: closed and full both stop it; a live invite gets past closed."""
    if not invited:
        _require_not_closed(conn)
    if _shifts_full(conn):
        raise HTTPException(status_code=409, detail="All volunteer shifts are currently full.")


def _require_not_closed(conn: sqlite3.Connection) -> None:
    """For finishing one. "Full" is left to the per-pick checks, which can say *which* pick
    filled, so the form reopens that step instead of dead-ending someone mid-form."""
    if signup_state(conn) == "closed":
        raise HTTPException(status_code=409, detail="Signups aren't open right now.")


def _require_signup_session(session: Optional[sqlite3.Row]) -> sqlite3.Row:
    if session is None:
        raise HTTPException(status_code=401, detail="Confirm your email address first.")
    if session["vol_id"] is not None:
        raise HTTPException(status_code=409, detail="You're already signed up.")
    return session


# ── names ─────────────────────────────────────────────────────────────────────

def _clean_name(raw, limit: int, label: str, required: bool = True) -> str:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=f"{label} must be text")
    clean = " ".join("".join(ch for ch in raw if ch >= " ").split())
    if required and not clean:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(clean) > limit:
        raise HTTPException(status_code=400, detail=f"{label}: at most {limit} characters")
    return clean


def display_name(first: str, last: str, handle: str) -> str:
    """'First L.' plus an optional '(handle)', the roster's existing convention.

    Args:
        first: The first name.
        last: The last name. Only its initial is kept.
        handle: The volunteer's handle, or blank.
    """
    name = f"{first} {last[0].upper()}." if last else first
    return f"{name} ({handle})" if handle else name


def _name_key(first: str, last: str) -> tuple:
    return (first.casefold(), last[:1].casefold())


def _row_name_keys(row: sqlite3.Row) -> tuple:
    """(first, last initial) and handle for an existing volunteer.

    Rows the portal created carry the parts as columns; rows pushed from the scheduler may
    have only `name` ('First L. (handle)'), so that is parsed as a fallback.
    """
    first, last, handle = row["first_name"] or "", row["last_name"] or "", row["nickname"] or ""
    name = row["name"] or ""
    if not first:
        m = re.match(r"\s*(\S+)(?:\s+(\S))?", name)
        if m:
            first, last = m.group(1), m.group(2) or ""
    if not handle:
        m = re.search(r"\(([^)]+)\)\s*$", name)
        if m:
            handle = m.group(1).strip()
    return _name_key(first, last), handle.casefold()


def possible_duplicates(conn: sqlite3.Connection, first: str, last: str, handle: str) -> list:
    """vol_ids whose name looks like this one: same first name and last initial, or the same
    handle. Inactive volunteers are skipped; they aren't records to merge into.

    Args:
        conn: The database connection.
        first: The first name.
        last: The last name.
        handle: The handle, or blank.
    """
    want, want_handle = _name_key(first, last), handle.casefold()
    out = []
    for row in conn.execute(
        "SELECT vol_id, first_name, last_name, nickname, name FROM volunteers "
        "WHERE inactive_at IS NULL"
    ).fetchall():
        key, row_handle = _row_name_keys(row)
        if key == want or (want_handle and row_handle == want_handle):
            out.append(row["vol_id"])
    return out


# ── picks ─────────────────────────────────────────────────────────────────────

def _parse_picks(data: dict) -> tuple:
    """(prefs, extras, add_any) from a signup or update body, shape-checked only."""
    prefs, extras = data.get("prefs"), data.get("extras") or []
    if (not isinstance(prefs, list) or len(prefs) != 2 or len(set(prefs)) != 2
            or not all(isinstance(p, str) for p in prefs)):
        raise HTTPException(status_code=400, detail="Pick exactly two different shifts.")
    if (not isinstance(extras, list) or len(extras) > EXTRAS_MAX
            or not all(isinstance(e, str) for e in extras)):
        raise HTTPException(status_code=400, detail="Malformed extra availability")
    extras = [e for e in dict.fromkeys(extras) if e not in prefs]
    return prefs, extras, 1 if data.get("add_any") is True else 0


def _check_picks(conn: sqlite3.Connection, prefs: list, extras: list, held) -> dict:
    """Check picks against what's on offer, inside the caller's transaction; return the
    timeslots by key.

    `held` is the volunteer's current prefs. Keeping one needs no space, even if it now reads
    as full — their place is already counted in it. Anything new needs remaining > 0.
    """
    known = {slot_key(r): r for r in _timeslots(conn)}
    if all(p in known for p in prefs) and overlap(known[prefs[0]], known[prefs[1]]):
        raise HTTPException(status_code=400, detail="Those two shifts overlap, so nobody "
                            "could work both. Pick two that don't.")
    full = [p for p in prefs
            if p not in known or (p not in held and known[p]["remaining"] <= 0)]
    if full:
        raise HTTPException(status_code=409, detail={
            "message": "One of your picks just filled up or is no longer offered. "
                       "Please choose again.", "full": full})
    unknown = [e for e in extras if e not in known]
    if unknown:
        raise HTTPException(status_code=409, detail={
            "message": "Some of your extra availability is no longer offered. "
                       "Please check it again.", "full": unknown})
    return known


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/portal/signup/status")
def status(conn: sqlite3.Connection = Depends(db.get_db)):
    """Whether the signup form can be used: {state: open | closed | full}.

    Args:
        conn: The database connection.
    """
    return {"state": signup_state(conn)}


@router.get("/api/portal/signup/fields")
def fields(conn: sqlite3.Connection = Depends(db.get_db)):
    """The questionnaire in force, rendered by the form in order. See signup_fields.py.

    The organizer app reads it too, for the labels of stored answers.

    Args:
        conn: The database connection.
    """
    return {"fields": signup_fields.current(conn)}


@router.get("/api/portal/timeslots")
def timeslots(conn: sqlite3.Connection = Depends(db.get_db)):
    """The timeslots on offer, by day, with remaining space. Nothing about who holds them.

    `remaining` is the scheduler's pushed figure (overage included), decremented here for
    signups taken since the last push; the next push replaces it. `overlaps` lists the keys
    that can't be picked alongside this one, so the form can grey them out.

    Args:
        conn: The database connection.
    """
    rows = _timeslots(conn)
    return {"timeslots": [{
        "key": slot_key(r), "day_key": r["day_key"], "time_key": r["time_key"],
        "day_str": r["day_str"] or r["day_key"], "time_str": r["time_str"] or r["time_key"],
        "event_date": r["event_date"], "label": _label(r),
        "remaining": max(0, r["remaining"]), "full": r["remaining"] <= 0,
        "overlaps": [slot_key(o) for o in rows if o is not r and overlap(r, o)],
    } for r in rows]}


@router.post("/api/portal/signup/check_email")
async def check_email(request: Request, email: str = Form(...), invite: str = Form(default=""),
                      conn: sqlite3.Connection = Depends(db.get_db)):
    """Step 1. A registered address stops here; any other gets a signup link by mail.

    `invite` is an early-access token from the signup link. A live one lets this through while
    signup is closed and rides along on the mailed link; it isn't consumed until submit.

    Discloses whether an address is registered — deliberately (vol_signup.md §10) — and is
    rate limited and response-floored like request_link.

    Args:
        request: The incoming request, for the rate limit's client address.
        email: The address as typed.
        invite: An early-access token, or blank.
        conn: The database connection.
    """
    started = time.monotonic()
    ip, ua = _client_ip(request), _ua(request)
    email = email.strip()
    if len(email) > 254 or not _EMAIL.match(email):
        await _floor(started)
        raise HTTPException(status_code=400, detail="That doesn't look like an email address.")
    addr_hash = security.email_hmac(settings.email_hmac_key, email)

    if not ratelimit.check(conn, "signup_email", ip, addr_hash,
                           settings.rate_per_min, settings.rate_per_hour):
        await _floor(started)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again shortly.")
    # Registered first: someone already signed up should be told to sign in, even when
    # signup is closed or full.
    if conn.execute("SELECT 1 FROM volunteers WHERE email_hmac = ?", (addr_hash,)).fetchone():
        db.audit(conn, "signup.check_email.registered")
        await _floor(started)
        return {"registered": True, "sent": False,
                "detail": "That address is already signed up. Sign in with it instead."}
    inv = _invite_by_token(conn, invite)
    invite_id = inv["invite_id"] if _invite_status(inv) == "valid" else None
    try:
        if invite and not invite_id and signup_state(conn) == "closed":
            raise HTTPException(status_code=409, detail=INVITE_GONE)
        _require_open(conn, invited=bool(invite_id))
    except HTTPException:
        await _floor(started)
        raise

    pubkey = db.kv_get(conn, "email_pubkey")
    if not pubkey:   # closed for want of a key: an invite can't help, addresses can't be stored
        await _floor(started)
        raise HTTPException(status_code=409, detail="Signups aren't open right now.")
    token = _issue_token(conn, "signup", None, addr_hash,
                         security.encrypt_email(pubkey, email),
                         security.pubkey_fingerprint(pubkey), ip, ua, invite_id=invite_id)
    Mailer(settings).send_login_link(email, token, "signup", _app_title(conn))
    db.audit(conn, "signup.check_email.sent", detail="invite" if invite_id else "")
    await _floor(started)
    return {"registered": False, "sent": True,
            "detail": "Check your inbox for a link to continue signing up."}


@router.get("/api/portal/signup/session")
def signup_session(session: Optional[sqlite3.Row] = Depends(get_session)):
    """Which kind of session this browser holds, so the form knows which step to show.

    The inbox step polls this: the tab that requested the link unlocks once the link is
    clicked, even if it was clicked in another tab.

    Args:
        session: The signup session, from `get_session`, or `None`.
    """
    if session is None:
        return {"signup_session": False, "volunteer": False}
    return {"signup_session": session["vol_id"] is None,
            "volunteer": session["vol_id"] is not None}


@router.post("/api/portal/signup/check_name")
async def check_name(request: Request,
                     first_name: str = Form(default=""), last_name: str = Form(default=""),
                     handle: str = Form(default=""),
                     session: Optional[sqlite3.Row] = Depends(get_session),
                     conn: sqlite3.Connection = Depends(db.get_db)):
    """{possible_match: bool} and nothing else — no names, no count.

    Needs a signup session, so an anonymous caller can't walk the roster one guess at a time,
    and is rate limited on the verified address as well as the IP.

    Args:
        request: The incoming request, for the rate limit's client address.
        first_name: The first name as typed.
        last_name: The last name as typed.
        handle: The handle, or blank.
        session: The signup session, from `get_session`, or `None`.
        conn: The database connection.
    """
    started = time.monotonic()
    session = _require_signup_session(session)
    if not ratelimit.check(conn, "signup_name", _client_ip(request), session["email_hmac"],
                           settings.rate_per_min, settings.rate_per_hour):
        await _floor(started)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again shortly.")
    first = _clean_name(first_name, NAME_MAX, "First name")
    last = _clean_name(last_name, NAME_MAX, "Last name")
    handle = _clean_name(handle, HANDLE_MAX, "Handle", required=False)
    match = bool(possible_duplicates(conn, first, last, handle))
    await _floor(started)
    return {"possible_match": match}


@router.post("/api/portal/signup/submit")
def submit(request: Request, signup: str = Form(...),
           session: Optional[sqlite3.Row] = Depends(get_session),
           conn: sqlite3.Connection = Depends(db.get_db)):
    """Create the pending volunteer and their picks, and upgrade the session in place.

    `signup` is JSON: {first_name, last_name, handle, answers, prefs: [two `day_time` keys in
    rank order], extras: [`day_time` keys], add_any}.

    Args:
        request: The incoming request, for the client address.
        signup: The form, as JSON (above).
        session: The signup session, from `get_session`, or `None`.
        conn: The database connection.

    Raises:
        HTTPException 400: Missing or invalid fields, or two picks that overlap.
        HTTPException 401/409: No signup session, or already signed up.
        HTTPException 409: Signups closed, or a picked timeslot filled up (or went) meanwhile
            — then the detail is {message, full: [keys]}.
    """
    session = _require_signup_session(session)
    try:
        data = json.loads(signup)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed signup") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Malformed signup")

    first = _clean_name(data.get("first_name"), NAME_MAX, "First name")
    last = _clean_name(data.get("last_name"), NAME_MAX, "Last name")
    handle = _clean_name(data.get("handle"), HANDLE_MAX, "Handle", required=False)
    try:
        answers = signup_fields.validate(data.get("answers"), signup_fields.current(conn))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    prefs, extras, add_any = _parse_picks(data)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT 1 FROM volunteers WHERE email_hmac = ?",
                        (session["email_hmac"],)).fetchone():
            raise HTTPException(status_code=409, detail="That address is already signed up.")
        # An invite is used up here, inside the transaction: a failure below rolls it back.
        # Taken whether or not signup is open, so the scheduler sees who it was for.
        invite_id = session["invite_id"] if _consume_invite(conn, session["invite_id"]) else None
        if signup_state(conn) == "closed" and not invite_id:
            raise HTTPException(status_code=409, detail=INVITE_GONE if session["invite_id"]
                                else "Signups aren't open right now.")
        known = _check_picks(conn, prefs, extras, held=())

        dupes = possible_duplicates(conn, first, last, handle)
        while True:
            vol_id = f"p-{secrets.token_hex(4)}"
            if not conn.execute("SELECT 1 FROM volunteers WHERE vol_id = ?", (vol_id,)).fetchone():
                break
        try:
            conn.execute(
                "INSERT INTO volunteers (vol_id, first_name, last_name, nickname, name, status, "
                "time_pref, add_time, add_any, email_hmac, email_enc, email_key_fp, answers, "
                "possible_duplicate_of, invite_id, created_at, updated_at, rev) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vol_id, first, last, handle, display_name(first, last, handle),
                 ";".join(_label(known[p]) for p in prefs),
                 ";".join(_label(known[e]) for e in extras), add_any,
                 session["email_hmac"], session["email_enc"], session["email_key_fp"],
                 json.dumps(answers), json.dumps(dupes) if dupes else None, invite_id,
                 clock.now_iso(), clock.now_iso(), db.next_rev(conn)),
            )
        except sqlite3.IntegrityError:
            # The partial-unique index on email_hmac: two submits for one address raced.
            raise HTTPException(status_code=409,
                                detail="That address is already signed up.") from None
        cell = {k: (known[k]["day_key"], known[k]["time_key"]) for k in (*prefs, *extras)}
        conn.executemany(
            "INSERT INTO vol_picks (vol_id, day_key, time_key, kind, rank) VALUES (?, ?, ?, ?, ?)",
            [(vol_id, *cell[p], "pref", rank) for rank, p in enumerate(prefs, start=1)]
            + [(vol_id, *cell[e], "add", None) for e in extras],
        )
        # The local estimate until the scheduler's next push replaces it.
        conn.executemany("UPDATE snapshot_day_times SET remaining = remaining - 1 "
                         "WHERE day_key = ? AND time_key = ?", [cell[p] for p in prefs])
        conn.execute("UPDATE sessions SET vol_id = ? WHERE sid_hash = ?",
                     (vol_id, session["sid_hash"]))
        db.audit(conn, "signup.submit", vol_id,
                 detail="possible duplicate" if dupes else "")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "vol_id": vol_id, "status": "pending"}


def _json_field(raw: str, what: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Malformed {what}") from None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"Malformed {what}")
    return data


def _save_picks(conn: sqlite3.Connection, vol_id: str, vol: sqlite3.Row, data: dict,
                held: set, rejoin: bool = False) -> None:
    """Write a volunteer's picks and editable answers, inside the caller's transaction.

    Shared by update (held = their current prefs, which stay keepable when full) and rejoin
    (held = nothing: cancelling gave their places back, so every pick needs space again).
    `remaining` is adjusted locally: +1 for each held pick dropped, -1 for each new one.
    """
    prefs, extras, add_any = _parse_picks(data)
    try:
        answers = signup_fields.validate_edit(
            data.get("answers") or {}, json.loads(vol["answers"]) if vol["answers"] else {},
            signup_fields.current(conn))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    known = _check_picks(conn, prefs, extras, held)
    cell = {k: (known[k]["day_key"], known[k]["time_key"]) for k in (*prefs, *extras)}

    conn.execute("DELETE FROM vol_picks WHERE vol_id = ?", (vol_id,))
    conn.executemany(
        "INSERT INTO vol_picks (vol_id, day_key, time_key, kind, rank) VALUES (?, ?, ?, ?, ?)",
        [(vol_id, *cell[p], "pref", rank) for rank, p in enumerate(prefs, start=1)]
        + [(vol_id, *cell[e], "add", None) for e in extras],
    )
    # A pick that's no longer on offer has no row to give back to.
    conn.executemany("UPDATE snapshot_day_times SET remaining = remaining + 1 "
                     "WHERE day_key = ? AND time_key = ?",
                     [tuple(k.split("_", 1)) for k in held - set(prefs)])
    conn.executemany("UPDATE snapshot_day_times SET remaining = remaining - 1 "
                     "WHERE day_key = ? AND time_key = ?",
                     [cell[p] for p in prefs if p not in held])
    conn.execute(
        "UPDATE volunteers SET time_pref = ?, add_time = ?, add_any = ?, answers = ?, "
        "updated_at = ?, rev = ?, cancelled_at = CASE WHEN ? THEN NULL ELSE cancelled_at END "
        "WHERE vol_id = ?",
        (";".join(_label(known[p]) for p in prefs),
         ";".join(_label(known[e]) for e in extras), add_any, json.dumps(answers),
         clock.now_iso(), db.next_rev(conn), rejoin, vol_id))


def _held_prefs(conn: sqlite3.Connection, vol_id: str) -> set:
    return {f"{r['day_key']}_{r['time_key']}" for r in conn.execute(
        "SELECT day_key, time_key FROM vol_picks WHERE vol_id = ? AND kind = 'pref'",
        (vol_id,)).fetchall()}


@router.post("/api/portal/me/update")
def update_me(update: str = Form(...),
              session: sqlite3.Row = Depends(require_volunteer),
              conn: sqlite3.Connection = Depends(db.get_db)):
    """Change one's own shifts, and the answers marked `editable` (signup_fields.py).

    `update` is JSON: {prefs: [two `day_time` keys], extras, add_any, answers: {...}}. The same
    rules as signup — exactly two, no overlap, new picks need space — but a shift already held
    can be kept even when full. Allowed while pending or active, and whether or not signup is
    open.

    Dropping a timeslot the volunteer is assigned to is allowed. The scheduler notices on its
    next pull and flags the assignment for an organizer (backend portal_sync
    _flag_dropped_assignments); the form warns before they save.

    Args:
        update: The changes, as JSON (above).
        session: The volunteer's session, from `require_volunteer`.
        conn: The database connection.

    Returns:
        The same payload as GET /api/portal/me.

    Raises:
        HTTPException 400: Malformed, two picks that overlap, or a bad answer.
        HTTPException 403: Inactive, cancelled (rejoin instead), or otherwise not editable.
        HTTPException 409: A new pick filled up or isn't offered: detail {message, full}.
    """
    vol_id = session["vol_id"]
    data = _json_field(update, "update")
    conn.execute("BEGIN IMMEDIATE")
    try:
        vol = conn.execute("SELECT status, cancelled_at, inactive_at, answers FROM volunteers "
                           "WHERE vol_id = ?", (vol_id,)).fetchone()
        if vol is None or not is_live(vol):
            raise HTTPException(status_code=403, detail="This registration can't be changed "
                                "here. Please contact an organizer.")
        _save_picks(conn, vol_id, vol, data, _held_prefs(conn, vol_id))
        outside = conn.execute(
            "SELECT COUNT(*) FROM snapshot_slots s WHERE s.vol_id = ? AND NOT EXISTS ("
            "SELECT 1 FROM vol_picks vp WHERE vp.vol_id = s.vol_id AND vp.day_key = s.day_key "
            "AND vp.time_key = s.time_key)", (vol_id,)).fetchone()[0]
        db.audit(conn, "me.update", vol_id,
                 detail="scheduled outside their picks" if outside else "")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return me_payload(conn, vol_id)


@router.post("/api/portal/me/cancel")
def cancel_me(session: sqlite3.Row = Depends(require_volunteer),
              conn: sqlite3.Connection = Depends(db.get_db)):
    """Cancel one's own registration. Undoable with POST /api/portal/me/rejoin.

    Sets cancelled_at and nothing else: approval (status) is untouched, so an approved
    volunteer who rejoins is still approved. Their two places go back into `remaining` at once;
    picks are kept so rejoin can offer them again, but no longer count against anything.
    Assignments stay; the organizer app lists them as slots needing a replacement.

    Args:
        session: The volunteer's session, from `require_volunteer`.
        conn: The database connection.

    Raises:
        HTTPException 409: Already cancelled, or inactive.
    """
    vol_id = session["vol_id"]
    conn.execute("BEGIN IMMEDIATE")
    try:
        vol = conn.execute("SELECT status, cancelled_at, inactive_at FROM volunteers "
                           "WHERE vol_id = ?", (vol_id,)).fetchone()
        if vol is None or not is_live(vol):
            raise HTTPException(status_code=409, detail="This registration can't be cancelled "
                                "here. Please contact an organizer.")
        conn.executemany("UPDATE snapshot_day_times SET remaining = remaining + 1 "
                         "WHERE day_key = ? AND time_key = ?",
                         [tuple(k.split("_", 1)) for k in _held_prefs(conn, vol_id)])
        conn.execute("UPDATE volunteers SET cancelled_at = ?, updated_at = ?, rev = ? "
                     "WHERE vol_id = ?", (clock.now_iso(), clock.now_iso(), db.next_rev(conn),
                                          vol_id))
        db.audit(conn, "me.cancel", vol_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return me_payload(conn, vol_id)


@router.post("/api/portal/me/rejoin")
def rejoin_me(update: str = Form(...),
              session: sqlite3.Row = Depends(require_volunteer),
              conn: sqlite3.Connection = Depends(db.get_db)):
    """Undo a cancellation, with shifts chosen again (`update` as for me/update).

    Clears cancelled_at; approval was never touched, so it's as it was. Their old places were
    given back when they cancelled, so every pick needs space, including ones they had before.
    Allowed whether or not signup is open.

    Args:
        update: The shifts to rejoin with, as JSON.
        session: The volunteer's session, from `require_volunteer`.
        conn: The database connection.

    Raises:
        HTTPException 409: Not cancelled, or inactive; or a pick is full or gone: detail
            {message, full}.
    """
    vol_id = session["vol_id"]
    data = _json_field(update, "update")
    conn.execute("BEGIN IMMEDIATE")
    try:
        vol = conn.execute("SELECT status, cancelled_at, inactive_at, answers FROM volunteers "
                           "WHERE vol_id = ?", (vol_id,)).fetchone()
        if vol is None or not vol["cancelled_at"] or vol["inactive_at"]:
            raise HTTPException(status_code=409, detail="Only a cancelled registration can "
                                "be rejoined.")
        _save_picks(conn, vol_id, vol, data, held=set(), rejoin=True)
        db.audit(conn, "me.rejoin", vol_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return me_payload(conn, vol_id)
