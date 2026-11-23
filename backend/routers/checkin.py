"""Shift check-in: scanning a volunteer's QR code, and the key behind the codes.

The codes themselves are made and checked in `checkin_codes.py`. A check-in is a
`[CheckIn] | {slot_key}` note in `vol_notes`. Arriving well before or after the shift's start
asks the operator to confirm first (`record_checkin`).
"""
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request, Depends, Form, HTTPException

import checkin_codes
from audit import audit
from db import get_db, RoleChecker, get_app_kv, PgClient
from routers.events import broadcast

router = APIRouter()


def checkin_key(db: PgClient) -> bytes:
    """The HMAC key behind every check-in code; made on first use.

    Args:
        db: The database connection.
    """
    return bytes.fromhex(get_app_kv(db, "checkin_key", lambda: secrets.token_hex(32)))


def event_tz(db: PgClient) -> str:
    """The timezone shift times are in (App Settings); shared with the export API.

    Args:
        db: The database connection.

    Returns:
        An IANA timezone name, `checkin_codes.DEFAULT_TZ` until one is set.
    """
    return get_app_kv(db, "event_tz") or checkin_codes.DEFAULT_TZ


def _slot(db: PgClient, slot_key: str) -> Optional[dict]:
    parts = slot_key.split("_")
    if len(parts) != 3:
        return None
    return db.execute(
        "SELECT s.day_key, s.time_key, s.pos_key, s.vol_id, d.day_str, d.event_date, "
        "t.time_str, p.pos_str, p.pos_cat, v.name AS vol_name "
        "FROM schedule s "
        "JOIN days_table d  ON d.day_key  = s.day_key "
        "JOIN times_table t ON t.time_key = s.time_key "
        "JOIN positions p   ON p.pos_key  = s.pos_key "
        "LEFT JOIN volunteers v ON v.vol_id = s.vol_id "
        "WHERE s.day_key = ? AND s.time_key = ? AND s.pos_key = ?;",
        tuple(parts),
    ).fetchone()


def record_checkin(request: Request, db: PgClient, s_data: dict,
                   vol_id: str, slot_key: str, force: bool) -> dict:
    """Check a volunteer in to a slot, unless they're early or late and force isn't set.

    Shared by the scanner (POST /api/checkin/scan) and the slot dialog's button
    (POST /api/checkin_vol), so both apply the same rule.

    More than EARLY_MINUTES before the start, or more than LATE_MINUTES after it, returns
    ``needs_confirm`` and writes nothing; the caller asks the operator and resends with force.
    A forced early or late check-in also records a ``[CheckInTiming]`` note, which the notes
    report shows and reset_vol_status clears. A shift with no event_date, or a time_str that
    isn't a range, can't be judged: it checks in with timing ``unknown``.

    Args:
        request: The request, for the audit log.
        db: The database connection. Committed on a check-in.
        s_data: The operator's session. Their username goes on the note.
        vol_id: The volunteer being checked in.
        slot_key: The shift, `{day_key}_{time_key}_{pos_key}`.
        force: Check in even when early or late, after the operator confirmed.

    Returns:
        The volunteer and shift, `timing` and `minutes`, and three flags: `checked_in`,
        `already` (checked in before) and `needs_confirm`.

    Raises:
        HTTPException 409: The volunteer isn't (or is no longer) assigned to that slot.
    """
    slot = _slot(db, slot_key)
    if not slot or slot["vol_id"] != vol_id:
        raise HTTPException(status_code=409,
                            detail="That volunteer isn't assigned to this shift any more.")

    start = checkin_codes.shift_start(slot["event_date"], slot["time_str"], event_tz(db))
    timing, minutes = checkin_codes.classify(datetime.now(timezone.utc), start)
    result = {
        "vol_id": vol_id, "name": slot["vol_name"], "slot_key": slot_key,
        "day": slot["day_str"], "time": slot["time_str"],
        "position": slot["pos_str"], "category": slot["pos_cat"],
        "start": start.isoformat() if start else None,
        "timing": timing, "minutes": minutes,
        "checked_in": False, "already": False, "needs_confirm": False,
    }

    note = f"[CheckIn] | {slot_key}"
    if db.execute("SELECT 1 FROM vol_notes WHERE vol_id = ? AND note = ?;",
                  (vol_id, note)).fetchone():
        return {**result, "checked_in": True, "already": True}

    if timing in ("early", "late") and not force:
        return {**result, "needs_confirm": True}

    db.execute(
        "INSERT INTO vol_notes (vol_id, note, username, created_at) VALUES (?, ?, ?, NOW());",
        (vol_id, note, s_data["username"])
    )
    if timing in ("early", "late"):
        db.execute(
            "INSERT INTO vol_notes (vol_id, note, username, created_at) VALUES (?, ?, ?, NOW());",
            (vol_id, f"[CheckInTiming] {timing.capitalize()} {minutes} min | {slot_key}",
             s_data["username"])
        )
    db.commit()
    broadcast("checkin")
    extra = f" ({timing} {minutes} min, confirmed)" if timing in ("early", "late") else ""
    audit("checkin.ok", request, request.session.get("session_id"),
          f"Checked in vol_id={vol_id} for {slot_key}{extra}.")
    return {**result, "checked_in": True}


@router.post("/api/checkin/scan")
def scan_checkin(
    request: Request,
    code: str = Form(...),
    force: bool = Form(default=False),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Check a volunteer in from the code on their schedule email.

    Args:
        request: Starlette Request; used for audit logging.
        code: The scanned or typed check-in code.
        force: Check in even when early or late (the operator's "Check in anyway").
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected PgClient.

    Returns:
        The record_checkin result: who, which shift, timing, and whether it was written.

    Raises:
        HTTPException 400: Not a genuine check-in code (or one from before a key rotation).
        HTTPException 409: The volunteer is no longer assigned to that shift.
        HTTPException 401/403: Insufficient role.
    """
    found = checkin_codes.verify_code(checkin_key(db), code)
    if not found:
        audit("checkin.code_rejected", request, request.session.get("session_id"),
              "Rejected a check-in code.")
        raise HTTPException(status_code=400, detail="That isn't a valid check-in code.")
    vol_id, slot_key = found
    return record_checkin(request, db, s_data, vol_id, slot_key, force)


@router.post("/api/checkin/rotate_key")
def rotate_checkin_key(
    request: Request,
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Replace the check-in key, voiding every code already sent.

    Args:
        request: The incoming request, for the session id and the audit log.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict ``{"ok": True}``.
    """
    db.execute(
        "INSERT INTO app_kv (app_key, val) VALUES ('checkin_key', ?) "
        "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
        (secrets.token_hex(32),)
    )
    db.commit()
    audit("checkin.key_rotate", request, request.session.get("session_id"),
          "Rotated the check-in key.")
    return {"ok": True}
