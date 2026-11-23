"""Radio tracking: who has which radio, and the history of every handover.

`radios` holds each barcode and when it was first seen; `radio_checkouts` holds one row per
issue, closed by `checked_in_at`. A radio registers itself the first time it's checked out,
so there is no inventory step. A partial unique index allows one open checkout per radio.
Checking out a radio someone else holds hands it over in the same request
(`transferred_from`). Scanning it to its current holder again changes nothing
(`already_held`).
"""
from datetime import datetime
from typing import Optional

import psycopg2
from fastapi import APIRouter, Request, Depends, Form, HTTPException

from audit import audit
from db import get_db, get_session_data, RoleChecker, PgClient
from routers.events import broadcast

router = APIRouter()

MAX_BARCODE_LEN = 64


def _clean_barcode(raw: str) -> str:
    """Normalise a scanned or typed barcode.

    Args:
        raw: Barcode as it arrived from the form.

    Returns:
        The trimmed barcode.

    Raises:
        HTTPException 400: Empty, over-long, or containing control characters.
    """
    code = (raw or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="A barcode is required")
    if len(code) > MAX_BARCODE_LEN:
        raise HTTPException(status_code=400, detail="That barcode is too long to be a radio")
    # Code 128 is case-sensitive, so no folding — but a stray newline from a wedge
    # scanner should not become part of the identity.
    if any(ch < ' ' or ch == '\x7f' for ch in code):
        raise HTTPException(status_code=400, detail="That barcode contains unreadable characters")
    return code


def _iso(ts: Optional[datetime]) -> Optional[str]:
    """Render a naive UTC timestamp as an ISO string the browser reads as UTC.

    The radio columns default to ``NOW() AT TIME ZONE 'UTC'``, so the values are UTC
    with no tzinfo. Without the Z a browser parses them as local time and every
    "out for how long" is wrong by the viewer's offset.
    """
    return ts.isoformat() + "Z" if ts else None


def _same_person(existing: dict, vol_id: str, name: str) -> bool:
    """True when an open checkout is already held by the person being issued to.

    Compares volunteer ids when both sides have one — two volunteers can share a
    name — and falls back to a case-insensitive name match, which is what a
    free-form entry has to be matched on.

    Args:
        existing: The open checkout row.
        vol_id: Volunteer id the radio is being issued to, or "".
        name: Resolved display name the radio is being issued to.

    Returns:
        True if this is the same person re-scanning, not a handoff.
    """
    if existing["vol_id"] and vol_id:
        return existing["vol_id"] == vol_id
    return (existing["person_name"] or "").strip().lower() == name.strip().lower()


def _open_checkout(db: PgClient, barcode: str) -> Optional[dict]:
    """Return the open checkout row for a barcode, or None if the radio is in."""
    return db.execute(
        "SELECT checkout_id, barcode, vol_id, person_name, checked_out_at, checked_out_by "
        "FROM radio_checkouts WHERE barcode = ? AND checked_in_at IS NULL;",
        (barcode,)
    ).fetchone()


@router.get("/api/radios")
def list_radios(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return every known radio with its current holder, if it is out.

    Args:
        s_data: Injected session dict; endpoint requires any authenticated user.
        db: Injected PgClient from get_db().

    Returns:
        list of dicts with ``barcode``, ``first_seen_at``, ``out``, and — when out —
        ``person_name``, ``vol_id``, ``checked_out_at``, ``checked_out_by``.
        Radios that are out sort first, longest-out first.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = db.execute("""
        SELECT r.barcode, r.first_seen_at,
               c.checkout_id, c.vol_id, c.person_name, c.checked_out_at, c.checked_out_by
        FROM radios r
        LEFT JOIN radio_checkouts c
               ON c.barcode = r.barcode AND c.checked_in_at IS NULL
        ORDER BY (c.checkout_id IS NULL), c.checked_out_at, r.barcode;
    """).fetchall()
    return [{
        "barcode":        row["barcode"],
        "first_seen_at":  _iso(row["first_seen_at"]),
        "out":            row["checkout_id"] is not None,
        "vol_id":         row["vol_id"],
        "person_name":    row["person_name"],
        "checked_out_at": _iso(row["checked_out_at"]),
        "checked_out_by": row["checked_out_by"],
    } for row in rows]


@router.get("/api/radios/history")
def radio_history(
    limit: int = 50,
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return recently returned radios, most recently returned first.

    Args:
        limit: Maximum rows to return; clamped to 1–500.
        s_data: Injected session dict; endpoint requires any authenticated user.
        db: Injected PgClient from get_db().

    Returns:
        list of completed checkout dicts.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    limit = max(1, min(int(limit), 500))
    rows = db.execute("""
        SELECT barcode, vol_id, person_name, checked_out_at, checked_out_by,
               checked_in_at, checked_in_by
        FROM radio_checkouts
        WHERE checked_in_at IS NOT NULL
        ORDER BY checked_in_at DESC
        LIMIT ?;
    """, (limit,)).fetchall()
    return [{
        "barcode":        row["barcode"],
        "vol_id":         row["vol_id"],
        "person_name":    row["person_name"],
        "checked_out_at": _iso(row["checked_out_at"]),
        "checked_out_by": row["checked_out_by"],
        "checked_in_at":  _iso(row["checked_in_at"]),
        "checked_in_by":  row["checked_in_by"],
    } for row in rows]


@router.post("/api/radios/checkout")
def checkout_radio(
    request: Request,
    barcode: str = Form(...),
    vol_id: str = Form(default=""),
    person_name: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Issue a radio to a volunteer or a named person.

    An unknown barcode registers itself here — there is no inventory step — so the
    first scan of a new radio is also its first checkout.

    A radio that is already out is handed over: its open checkout is closed and a new
    one opened, so passing a radio to someone else is a single scan. Re-scanning to
    the person who already holds it changes nothing, rather than resetting their
    checkout time and leaving a phantom return in the history.

    Args:
        request: Starlette Request; used for audit logging.
        barcode: Scanned or typed radio barcode.
        vol_id: Volunteer receiving the radio; takes precedence over person_name.
        person_name: Free-form name, for anyone not on the volunteer roster.
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected PgClient from get_db().

    Returns:
        dict with ``ok``, ``barcode``, ``person_name``, ``checked_out_at``,
        ``transferred_from`` (the previous holder's name, or None) and
        ``already_held`` (True when the same person re-scanned and nothing changed).

    Raises:
        HTTPException 400: Bad barcode, unknown vol_id, or no recipient given.
        HTTPException 409: Another operator issued this radio mid-request.
        HTTPException 401/403: Insufficient role.
    """
    barcode = _clean_barcode(barcode)
    vol_id  = vol_id.strip()
    name    = person_name.strip()

    if vol_id:
        vol = db.execute("SELECT name FROM volunteers WHERE vol_id = ?;", (vol_id,)).fetchone()
        if not vol:
            raise HTTPException(status_code=400, detail="That volunteer no longer exists")
        name = vol["name"]
    elif not name:
        raise HTTPException(status_code=400, detail="Choose a volunteer or type a name")

    transferred_from = None
    existing = _open_checkout(db, barcode)
    if existing:
        if _same_person(existing, vol_id, name):
            # Nothing to do — and deliberately no write, so a double scan does not
            # restart the clock on how long they have had it.
            return {
                "ok":               True,
                "barcode":          barcode,
                "person_name":      existing["person_name"],
                "checked_out_at":   _iso(existing["checked_out_at"]),
                "transferred_from": None,
                "already_held":     True,
            }
        db.execute(
            "UPDATE radio_checkouts SET checked_in_at = (NOW() AT TIME ZONE 'UTC'), "
            "checked_in_by = ? WHERE checkout_id = ?;",
            (s_data["username"], existing["checkout_id"]),
        )
        transferred_from = existing["person_name"]

    db.execute(
        "INSERT INTO radios (barcode) VALUES (?) ON CONFLICT DO NOTHING;", (barcode,)
    )
    try:
        row = db.execute(
            "INSERT INTO radio_checkouts (barcode, vol_id, person_name, checked_out_by) "
            "VALUES (?, ?, ?, ?) RETURNING checkout_id, checked_out_at;",
            (barcode, vol_id or None, name, s_data["username"]),
        ).fetchone()
    except psycopg2.errors.UniqueViolation:
        # radio_one_open_checkout — someone else issued this radio between the read above
        # and this insert. get_db() rolls the connection back as the request unwinds.
        raise HTTPException(
            status_code=409,
            detail=f"Radio {barcode} was just checked out by someone else.",
        ) from None
    db.commit()

    broadcast("radio")
    audit("radio.checkout", request, request.session.get("session_id"),
          f"Checked out radio {barcode} to {name}"
          + (f" (handed over from {transferred_from})" if transferred_from else ""))
    return {
        "ok":               True,
        "barcode":          barcode,
        "person_name":      name,
        "checked_out_at":   _iso(row["checked_out_at"]),
        "transferred_from": transferred_from,
        "already_held":     False,
    }


@router.post("/api/radios/checkin")
def checkin_radio(
    request: Request,
    barcode: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Return a radio. The barcode alone is enough — no need to say who had it.

    Args:
        request: Starlette Request; used for audit logging.
        barcode: Scanned or typed radio barcode.
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected PgClient from get_db().

    Returns:
        dict with ``ok``, ``barcode``, ``person_name``, ``checked_out_at``, ``checked_in_at``.

    Raises:
        HTTPException 400: Bad barcode, unknown radio, or the radio is not out.
        HTTPException 401/403: Insufficient role.
    """
    barcode = _clean_barcode(barcode)
    row = db.execute(
        "UPDATE radio_checkouts SET checked_in_at = (NOW() AT TIME ZONE 'UTC'), checked_in_by = ? "
        "WHERE barcode = ? AND checked_in_at IS NULL "
        "RETURNING person_name, checked_out_at, checked_in_at;",
        (s_data["username"], barcode),
    ).fetchone()

    if not row:
        known = db.execute("SELECT 1 FROM radios WHERE barcode = ?;", (barcode,)).fetchone()
        raise HTTPException(
            status_code=400,
            detail=(f"Radio {barcode} is already checked in." if known
                    else f"Radio {barcode} has never been checked out."),
        )

    db.commit()
    broadcast("radio")
    audit("radio.checkin", request, request.session.get("session_id"),
          f"Checked in radio {barcode} from {row['person_name']}")
    return {
        "ok":             True,
        "barcode":        barcode,
        "person_name":    row["person_name"],
        "checked_out_at": _iso(row["checked_out_at"]),
        "checked_in_at":  _iso(row["checked_in_at"]),
    }
