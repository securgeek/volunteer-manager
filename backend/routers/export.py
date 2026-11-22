"""The read-only export API, for other servers and tools/vol_mail. Keys: api_auth.py.

Both endpoints are built from one slot query, so a volunteer's shifts and the schedule can't
disagree. Shift times are given in the event timezone (App Settings) with its offset;
``start``/``end`` are null for a day with no date or a time that isn't a range.

Names come from names.export_name: ``First L.``, or ``Handle [First L.]`` with the parts
alongside.

The response models below are the contract. docs/api/export.md (the caller's guide) is built
from them by docs/gen_export_api.py, so a field's ``description`` and ``examples`` are what
callers read. Field order is the JSON key order. Optional fields carry ``x-scope``, and both
routes use ``response_model_exclude_unset``, so a field the handler didn't set (``email_enc``
without volunteers:email) is left out, while one set to None is sent as null.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import checkin_codes
from api_auth import TokenChecker
from db import PgClient, get_app_kv, get_db
from names import export_name, split_name
from routers.checkin import checkin_key, event_tz

router = APIRouter()


# ── Response models ───────────────────────────────────────────────────────────────────────

class ExportHeader(BaseModel):
    """Fields at the top of every export response."""

    generated_at: str = Field(description="Server time of the response, ISO-8601 UTC.",
                              examples=["2027-04-20T14:03:11.402Z"])
    event_title: str = Field(description="The event's title from App Settings.",
                             examples=["BSides Charm 2027"])
    event_tz: str = Field(description="IANA timezone that `start` and `end` are given in.",
                          examples=["America/New_York"])


class SlotFields(BaseModel):
    """Where and when a slot is: shared by schedule slots and volunteer shifts."""

    slot_key: str = Field(description="`day_time_position`. Stable while the schedule's "
                          "structure is unchanged.", examples=["sat_0800_sec3"])
    day_key: str = Field(description="The day's key.", examples=["sat"])
    day: str = Field(description="The day's label.", examples=["Saturday"])
    date: str | None = Field(description="`YYYY-MM-DD`, or null when the day has no date.",
                             examples=["2027-04-24"])
    time_key: str = Field(description="The time's key.", examples=["0800"])
    time: str = Field(description="The shift label as the organizers wrote it.",
                      examples=["0800-1200"])
    start: str | None = Field(description="ISO-8601 in the event timezone, with its offset. "
                              "Null when the day has no date or `time` isn't a range.",
                              examples=["2027-04-24T08:00:00-04:00"])
    end: str | None = Field(description="Like `start`. A range past midnight ends the next "
                            "day.", examples=["2027-04-24T12:00:00-04:00"])
    pos_key: str = Field(description="The position's key.", examples=["sec3"])
    position: str = Field(description="The position's label.", examples=["Security 3"])
    category: str = Field(description="The position's category.", examples=["Security"])


class Slot(SlotFields):
    """One schedule slot, filled or not."""

    vol_id: str = Field(description="The assigned volunteer, or `\"\"` when the slot is "
                        "empty. Opaque.", examples=["1042"])
    checked_in: bool = Field(description="Whether the volunteer has checked in. Always false "
                             "for an empty slot.", examples=[False])
    name: str = Field(description="The volunteer's display name, or `\"\"` when the slot is "
                      "empty.", examples=["Zero Cool [Jim E.]"])


class Shift(SlotFields):
    """One of a volunteer's shifts."""

    checked_in: bool = Field(description="Whether the volunteer has checked in.",
                             examples=[False])
    checkin_code: str | None = Field(
        default=None, description="The check-in code mailed to the volunteer as a QR code.",
        examples=["BSC1.sat_0800_sec3.MTA0Mg.3q2-7wAAABBB_ccDDdeEeg"],
        json_schema_extra={"x-scope": "checkin:codes"})


class Volunteer(BaseModel):
    """One volunteer with their shifts."""

    vol_id: str = Field(description="The volunteer's id. Opaque.", examples=["1042"])
    name: str = Field(description="`First L.`, or `Handle [First L.]` with a handle.",
                      examples=["Zero Cool [Jim E.]"])
    first_name: str = Field(description="May be `\"\"`.", examples=["Jim"])
    last_initial: str = Field(description="May be `\"\"`.", examples=["E"])
    handle: str = Field(description="May be `\"\"`.", examples=["Zero Cool"])
    status: str = Field(description="`pending` (signed up, not yet approved) or `active`.",
                        examples=["active"])
    cancelled: bool = Field(description="The volunteer cancelled.", examples=[False])
    inactive: bool = Field(description="An organizer set the volunteer inactive.",
                           examples=[False])
    shifts: list[Shift] = Field(description="Shifts in time order; `[]` when none.")
    email_enc: str | None = Field(
        default=None, description="The email address, RSA-OAEP/SHA-256 encrypted to the "
        "organizers' key, base64. Null when none is on file.", examples=["k3Jv…base64…=="],
        json_schema_extra={"x-scope": "volunteers:email"})


class ScheduleExport(ExportHeader):
    """Response of ``GET /api/export/schedule``."""

    slots: list[Slot] = Field(description="Every slot in time order.")


class VolunteersExport(ExportHeader):
    """Response of ``GET /api/export/volunteers``."""

    volunteers: list[Volunteer] = Field(description="Volunteers sorted by name.")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _name_fields(name: Optional[str]) -> dict:
    first, initial, handle = split_name(name or "")
    return {"name": export_name(first, initial, handle), "first_name": first,
            "last_initial": initial, "handle": handle}


def _slots(db: PgClient, tz: str) -> list:
    """Every slot in display order, with times resolved and check-in state."""
    rows = db.execute("""
        SELECT s.day_key, d.day_str, d.event_date, s.time_key, t.time_str,
               s.pos_key, p.pos_str, p.pos_cat, s.vol_id, v.name AS vol_name,
               EXISTS (
                   SELECT 1 FROM vol_notes vn
                   WHERE vn.vol_id = s.vol_id
                   AND vn.note = '[CheckIn] | ' || s.day_key || '_' || s.time_key || '_' || s.pos_key
               ) AS checked_in
        FROM schedule s
        JOIN days_table d  ON d.day_key  = s.day_key
        JOIN times_table t ON t.time_key = s.time_key
        JOIN positions p   ON p.pos_key  = s.pos_key
        LEFT JOIN categories c ON c.cat_name = p.pos_cat
        LEFT JOIN volunteers v ON v.vol_id   = s.vol_id
        ORDER BY d.event_date NULLS LAST, s.day_key, t.time_str, s.time_key,
                 COALESCE(c.sort_order, 999), LENGTH(s.pos_key), s.pos_key;
    """).fetchall()
    out = []
    for r in rows:
        start = checkin_codes.shift_start(r["event_date"], r["time_str"], tz)
        end = checkin_codes.shift_end(start, r["time_str"])
        out.append({
            "slot_key": f"{r['day_key']}_{r['time_key']}_{r['pos_key']}",
            "day_key": r["day_key"], "day": r["day_str"], "date": r["event_date"] or None,
            "time_key": r["time_key"], "time": r["time_str"],
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "pos_key": r["pos_key"], "position": r["pos_str"], "category": r["pos_cat"],
            "vol_id": r["vol_id"], "vol_name": r["vol_name"],
            "checked_in": bool(r["checked_in"]) if r["vol_id"] else False,
        })
    return out


def _header(db: PgClient, tz: str) -> dict:
    return {"generated_at": _now_iso(), "event_title": get_app_kv(db, "app_title") or "",
            "event_tz": tz}


@router.get("/api/export/schedule", response_model=ScheduleExport,
            response_model_exclude_unset=True)
def export_schedule(
    assigned: bool = Query(default=False, description="Only slots with a volunteer."),
    day: Optional[str] = Query(default=None, description="Only one day, by `day_key`, for "
                               "example `sat`. An unknown `day_key` returns an empty list, "
                               "not an error."),
    key: dict = Depends(TokenChecker("schedule:read")),
    db: PgClient = Depends(get_db),
):
    """Every slot, with the assigned volunteer's id and name (blank when unassigned).

    Args:
        assigned: Only slots with a volunteer.
        day: Only this `day_key`.
        key: The calling key's record, after the scope check.
        db: The request's database connection.

    Returns:
        dict with ``generated_at``, ``event_title``, ``event_tz`` and ``slots``.
    """
    tz = event_tz(db)
    slots = []
    for s in _slots(db, tz):
        if (assigned and not s["vol_id"]) or (day and s["day_key"] != day):
            continue
        vol_name = s.pop("vol_name")
        s["name"] = _name_fields(vol_name)["name"] if s["vol_id"] else ""
        s["vol_id"] = s["vol_id"] or ""
        slots.append(s)
    return {**_header(db, tz), "slots": slots}


@router.get("/api/export/volunteers", response_model=VolunteersExport,
            response_model_exclude_unset=True)
def export_volunteers(
    status: str = Query(default="active", description="`active`: approved, and neither "
                        "cancelled nor inactive. `all`: everyone, including pending signups; "
                        "check `status`, `cancelled` and `inactive`. Anything else is a 400."),
    assigned: bool = Query(default=False, description="Only volunteers with at least one "
                           "shift."),
    key: dict = Depends(TokenChecker("volunteers:read")),
    db: PgClient = Depends(get_db),
):
    """Every volunteer with their shifts in time order.

    ``email_enc`` is included only for a key with volunteers:email, and each shift's
    ``checkin_code`` only for one with checkin:codes.

    Args:
        status: `active` or `all`.
        assigned: Only volunteers with at least one shift.
        key: The calling key's record, after the scope check. Its scopes decide whether
            addresses and codes are included.
        db: The request's database connection.

    Returns:
        dict with ``generated_at``, ``event_title``, ``event_tz`` and ``volunteers``.

    Raises:
        HTTPException 400: Unknown status.
    """
    if status not in ("active", "all"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'all'")
    return volunteer_records(db, status, assigned, with_email="volunteers:email" in key["scopes"],
                             with_codes="checkin:codes" in key["scopes"])


def volunteer_records(db: PgClient, status: str = "active", assigned: bool = False,
                      with_email: bool = False, with_codes: bool = False) -> dict:
    """The volunteers export, shared with the email template preview (routers/email_template),
    so the preview shows exactly what tools/vol_mail will be sent.

    Args:
        db: The database connection.
        status: `active` for assignable volunteers only, anything else for everyone.
        assigned: Only volunteers with at least one shift.
        with_email: Include each volunteer's `email_enc`.
        with_codes: Include each shift's `checkin_code`.

    Returns:
        The export header plus `volunteers`.
    """
    tz = event_tz(db)
    code_key = checkin_key(db) if with_codes else None

    shifts: dict = {}
    for s in _slots(db, tz):
        if not s["vol_id"]:
            continue
        s.pop("vol_name")
        vol_id = s.pop("vol_id")
        if with_codes:
            s["checkin_code"] = checkin_codes.make_code(code_key, vol_id, s["slot_key"])
        shifts.setdefault(vol_id, []).append(s)

    rows = db.execute(
        "SELECT v.vol_id, v.name, v.status, v.email_enc, "
        "(v.cancelled_at IS NOT NULL) AS cancelled, (v.inactive_at IS NOT NULL) AS inactive "
        "FROM volunteers v WHERE (? OR v.is_assignable) ORDER BY v.name, v.vol_id;",
        (status != "active",),
    ).fetchall()
    vols = []
    for r in rows:
        mine = shifts.get(r["vol_id"], [])
        if assigned and not mine:
            continue
        v = {"vol_id": r["vol_id"], **_name_fields(r["name"]), "status": r["status"],
             "cancelled": r["cancelled"], "inactive": r["inactive"], "shifts": mine}
        if with_email:
            v["email_enc"] = r["email_enc"]
        vols.append(v)
    return {**_header(db, tz), "volunteers": vols}
