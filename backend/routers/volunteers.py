"""Volunteers: the roster, notes, ratings, check-in and status resets.

`get_vols` and the assignment pickers list assignable volunteers only (approved, not
cancelled or inactive). `admin/volunteers` lists everyone for Admin → Volunteers.

Notes double as event records, using fixed prefixes: `[CheckIn] | {slot_key}`,
`[CheckInTiming] ...` and `[Rating] {Good|Bad|No-Show} | {slot_key}`. The notes report
filters those out.
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import JSONResponse, Response

from db import get_db, get_session_data, RoleChecker, audit, PgClient
from portal_sync import email_hmac
from routers.events import broadcast
from routers.checkin import record_checkin

router = APIRouter()

VALID_RATINGS = {"Good", "Bad", "No-Show"}


def _query_vols(db: PgClient, schedule_filter=None, request_filter=None):
    """Aggregate volunteers with their assigned slots.

    Args:
        db: The database connection.
        schedule_filter: List of ``"day_time"`` keys; volunteers already assigned to any
                         of these slots are excluded from the result.
        request_filter: Time-of-day prefix string (e.g. ``"Saturday AM "``); when provided,
                        only volunteers whose ``time_pref`` contains this string are returned.

    Returns:
        list of volunteer dicts, each with keys ``key``, ``name``, ``requested``,
        ``additional``, ``schedule``, ``sched_list``, and ``slot_count``.

    Only assignable volunteers (volunteers.is_assignable): approved, i.e. through orientation,
    and neither cancelled nor inactive. Pending signups wait in Pending Volunteers.
    """
    # ORDER BY names the UNION's output columns. Postgres rejects table-qualified names there
    # ("missing FROM-clause entry for table v"), which broke this query in the SQLite -> Postgres
    # move; test_signup_sync.py runs it against Postgres now.
    vol_cur = db.execute("""
        SELECT v.vol_id, v.name, v.time_pref, v.add_time,
               d.day_str, t.time_str, p.pos_str, p.pos_key, s.day_key, s.time_key
        FROM schedule s
        RIGHT JOIN volunteers v ON v.vol_id = s.vol_id
        LEFT JOIN days_table d  ON s.day_key  = d.day_key
        LEFT JOIN times_table t ON t.time_key = s.time_key
        LEFT JOIN positions p   ON s.pos_key  = p.pos_key
        WHERE s.vol_id IS NOT NULL AND v.is_assignable
        UNION
        SELECT v.vol_id, v.name, v.time_pref, v.add_time,
               NULL, NULL, NULL, NULL, NULL, NULL
        FROM volunteers v
        WHERE v.is_assignable
        ORDER BY vol_id, day_str, time_str;
    """)

    vol_data     = []
    cur_vol      = ""
    cur_vol_data = None
    filter_vol   = []

    for vol in vol_cur.fetchall():
        sched       = ""
        sched_entry = None
        count_add   = 0

        if schedule_filter:
            sched_key = f"{vol['day_key']}_{vol['time_key']}"
            if sched_key in schedule_filter:
                filter_vol.append(vol['vol_id'])

        if vol["day_str"]:
            sched = f"{vol['day_str']} - {vol['time_str']} : {vol['pos_str']}\n"
            sched_entry = {
                "key":   f"{vol['day_key']}_{vol['time_key']}_{vol['pos_key']}",
                "label": sched[:-1],
            }
            count_add = 1

        if cur_vol_data is None:
            cur_vol      = vol["vol_id"]
            cur_vol_data = {"key": vol["vol_id"], "name": vol["name"],
                            "requested": vol["time_pref"], "additional": vol["add_time"],
                            "schedule": "", "sched_list": [], "slot_count": 0}

        if cur_vol == vol["vol_id"]:
            cur_vol_data["schedule"] += sched
            if sched_entry:
                cur_vol_data["sched_list"].append(sched_entry)
            cur_vol_data["slot_count"] += count_add
        else:
            vol_data.append(cur_vol_data)
            cur_vol      = vol["vol_id"]
            cur_vol_data = {"key": vol["vol_id"], "name": vol["name"],
                            "requested": vol["time_pref"], "additional": vol["add_time"],
                            "schedule": sched, "sched_list": [], "slot_count": count_add}
            if sched_entry:
                cur_vol_data["sched_list"].append(sched_entry)

    if cur_vol_data is not None:
        vol_data.append(cur_vol_data)

    filtered = []
    for vol in vol_data:
        if vol["key"] in filter_vol:
            continue
        if request_filter and request_filter not in vol["requested"]:
            continue
        # Any number of entries: a timeslot removed after signup, or a legacy row, leaves fewer
        # than two, and indexing [0] and [1] used to raise for the whole list.
        req_split = sorted(p.strip() for p in (vol["requested"] or "").split(";") if p.strip())
        vol["requested"] = ("Requested:" + "".join(f"\n - {r}" for r in req_split)
                            if req_split else "Requested: (none)")
        filtered.append(vol)

    return filtered


@router.get("/api/get_vols")
def get_vols(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return all volunteers with their schedule and slot counts.

    Args:
        s_data: Injected session dict; endpoint requires any authenticated user.
        db: Injected database connection.

    Returns:
        list of volunteer dicts from _query_vols() with no filters applied.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return _query_vols(db)


@router.post("/api/add_vol")
def add_vol(
    request: Request,
    vol_id: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    nickname: str = Form(default=""),
    time_pref: str = Form(default=""),
    add_time: str = Form(default=""),
    email_enc: str = Form(default=""),
    email: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Add a new volunteer record.

    The address arrives twice: encrypted in the browser (``email_enc``, the only form stored)
    and plain (``email``), solely so ``email_hmac`` can be computed here, as the portal does at
    signup. The HMAC key is in this process's environment, so the browser can't do it. The plain
    address is never stored or logged. Without EMAIL_HMAC_KEY the volunteer is still added,
    unindexed, and ``email_indexed`` says so.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Unique volunteer identifier (e.g. a badge number).
        first_name: Volunteer's first name.
        last_name: Volunteer's last name; only the initial is stored in the display name.
        nickname: Optional handle appended to display name as ``(nickname)``.
        time_pref: Semicolon-delimited preferred shift strings.
        add_time: Availability notes.
        email_enc: RSA-OAEP ciphertext of the volunteer's email (base64); empty → NULL.
        email: The same address in plain text, for email_hmac only; ignored without email_enc.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 201 with a volunteer dict: ``key``, ``name``, ``requested``, ``additional``,
        ``sched_list``, ``slot_count``, ``email_indexed``.

    Raises:
        HTTPException 400: vol_id, first_name, or last_name is blank.
        HTTPException 409: vol_id already exists, or the address already belongs to someone.
        HTTPException 401/403: Insufficient role.
    """
    vol_id     = vol_id.strip()
    first_name = first_name.strip()
    last_name  = last_name.strip()
    nickname   = nickname.strip()
    email_enc  = email_enc.strip() or None

    if not vol_id or not first_name or not last_name:
        raise HTTPException(status_code=400, detail="Volunteer ID, first name, and last name are required")

    if db.execute("SELECT 1 FROM volunteers WHERE vol_id = ?;", (vol_id,)).fetchone():
        raise HTTPException(status_code=409, detail=f"Volunteer ID '{vol_id}' already exists")

    addr_hash = email_hmac(email) if email_enc and email.strip() else None
    _check_email_free(db, addr_hash)

    vol_name = f"{first_name} {last_name[0]}."
    if nickname:
        vol_name += f" ({nickname})"

    db.execute(
        "INSERT INTO volunteers (vol_id, name, time_pref, add_time, email_enc, email_hmac) "
        "VALUES (?, ?, ?, ?, ?, ?);",
        (vol_id, vol_name, time_pref, add_time, email_enc, addr_hash)
    )
    db.commit()
    audit(request, request.session.get("session_id"), f"Added volunteer {vol_id}: {vol_name}")

    return JSONResponse(status_code=201, content={
        "key": vol_id, "name": vol_name, "requested": time_pref,
        "additional": add_time, "sched_list": [], "slot_count": 0,
        "email_indexed": addr_hash is not None,
    })


def _check_email_free(db: PgClient, addr_hash, vol_id: str = "") -> None:
    """409 when another volunteer already has this address (the volunteers_email_hmac index
    would refuse it anyway; this says who, which only an organizer ever sees)."""
    if not addr_hash:
        return
    other = db.execute("SELECT vol_id, name FROM volunteers WHERE email_hmac = ? AND vol_id <> ?;",
                       (addr_hash, vol_id)).fetchone()
    if other:
        raise HTTPException(status_code=409, detail=f"That email address already belongs to "
                            f"{other['name']} ({other['vol_id']})")


@router.post("/api/admin/volunteers/email_hmac")
def fill_email_hmac(
    request: Request,
    vol_id: str = Form(...),
    email: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Fill in a missing email_hmac for a volunteer added by hand before add_vol computed it.

    The Volunteers tab decrypts the address in the browser with the private key and sends it
    here; it's hashed and dropped, never stored or logged. Only fills a gap: a volunteer who
    already has an email_hmac (every portal signup) is left alone.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_id: The volunteer.
        email: Their address, decrypted in the browser. Hashed, never stored or logged.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 404: No such volunteer, or it has no encrypted email.
        HTTPException 409: Already indexed, or the address belongs to someone else.
        HTTPException 503: EMAIL_HMAC_KEY isn't set.
    """
    row = db.execute("SELECT email_enc, email_hmac FROM volunteers WHERE vol_id = ?;",
                     (vol_id,)).fetchone()
    if not row or not row["email_enc"]:
        raise HTTPException(status_code=404, detail="No such volunteer with an email")
    if row["email_hmac"]:
        raise HTTPException(status_code=409, detail="That volunteer's email is already indexed")
    addr_hash = email_hmac(email)
    if not addr_hash:
        raise HTTPException(status_code=503, detail="EMAIL_HMAC_KEY isn't set on the scheduler")
    _check_email_free(db, addr_hash, vol_id)
    db.execute("UPDATE volunteers SET email_hmac = ? WHERE vol_id = ? AND email_hmac IS NULL;",
               (addr_hash, vol_id))
    db.commit()
    audit(request, request.session.get("session_id"), f"Indexed the email of volunteer {vol_id}")
    return {"ok": True}


def _parse_ratings(rows):
    """Extract rating value and metadata from '[Rating] X | slot_key' vol_notes rows.

    Args:
        rows: List of vol_notes dicts containing ``note``, ``username``, ``created_at``.

    Returns:
        list of dicts with keys ``rating``, ``username``, ``created_at``.
    """
    return [
        {"rating":     r["note"].split(" | ")[0].replace("[Rating] ", ""),
         "username":   r["username"],
         "created_at": r["created_at"]}
        for r in rows
    ]


@router.post("/api/get_vol_rating")
def get_vol_rating(
    vol_id: str = Form(...),
    slot_key: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return existing ratings for a volunteer at a specific slot.

    Args:
        vol_id: Volunteer ID to look up.
        slot_key: Composite slot key ``day_time_poskey``.
        s_data: Injected session dict; endpoint requires any authenticated user.
        db: Injected database connection.

    Returns:
        list of rating dicts with keys ``rating``, ``username``, ``created_at``;
        newest first.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = db.execute(
        "SELECT note, username, created_at FROM vol_notes "
        "WHERE vol_id = ? AND note LIKE ? ORDER BY created_at DESC;",
        (vol_id, f"[Rating] % | {slot_key}")
    ).fetchall()
    return _parse_ratings(rows)


@router.post("/api/rate_volunteer")
def rate_volunteer(
    request: Request,
    vol_id: str = Form(...),
    slot_key: str = Form(...),
    rating: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Record a rating note; checkin role is restricted to No-Show only.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Volunteer being rated.
        slot_key: Composite slot key ``day_time_poskey``.
        rating: One of ``"Good"``, ``"Bad"``, ``"No-Show"``.
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected database connection.

    Returns:
        HTTP 201 with a dict: ``rating``, ``username``, ``created_at``.

    Raises:
        HTTPException 400: rating is not a valid value.
        HTTPException 403: checkin role attempted a rating other than No-Show.
        HTTPException 401/403: Insufficient role.
    """
    if rating not in VALID_RATINGS:
        raise HTTPException(status_code=400, detail=f"Invalid rating. Must be one of: {', '.join(VALID_RATINGS)}")
    if s_data["role"] == "checkin" and rating != "No-Show":
        raise HTTPException(status_code=403, detail="Forbidden")

    note_id  = str(uuid.uuid4())
    note     = f"[Rating] {rating} | {slot_key}"
    db.execute("INSERT INTO vol_notes (note_id, vol_id, note, username) VALUES (?, ?, ?, ?);",
               (note_id, vol_id, note, s_data["username"]))
    db.commit()
    broadcast("rating")
    audit(request, request.session.get("session_id"), f"Rated volunteer {vol_id} at {slot_key}: {rating}")

    row = db.execute(
        "SELECT note, username, created_at FROM vol_notes WHERE note_id = ?;", (note_id,)
    ).fetchone()
    return JSONResponse(status_code=201, content={
        "rating": rating, "username": row["username"], "created_at": row["created_at"]
    })


@router.post("/api/get_vol_notes")
def get_vol_notes(
    vol_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Return all notes for a volunteer, including system [Rating] and [CheckIn] notes.

    Args:
        vol_id: Volunteer ID to fetch notes for.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        list of note dicts with keys ``note_id``, ``note``, ``username``, ``created_at``;
        newest first.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    rows = db.execute(
        "SELECT note_id, note, username, created_at FROM vol_notes "
        "WHERE vol_id = ? ORDER BY created_at DESC;",
        (vol_id,)
    ).fetchall()
    return rows


@router.post("/api/add_vol_note")
def add_vol_note(
    request: Request,
    vol_id: str = Form(...),
    note: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Add a free-text organizer note to a volunteer record.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Volunteer ID to attach the note to.
        note: Note text; must be non-empty.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 201 with the created note dict: ``note_id``, ``note``, ``username``, ``created_at``.

    Raises:
        HTTPException 400: Note text is empty.
        HTTPException 401/403: Insufficient role.
    """
    note = note.strip()
    if not note:
        raise HTTPException(status_code=400, detail="Note cannot be empty")

    note_id = str(uuid.uuid4())
    db.execute("INSERT INTO vol_notes (note_id, vol_id, note, username) VALUES (?, ?, ?, ?);",
               (note_id, vol_id, note, s_data["username"]))
    db.commit()
    audit(request, request.session.get("session_id"), f"Added note to volunteer {vol_id}: {note!r}")

    row = db.execute(
        "SELECT note_id, note, username, created_at FROM vol_notes WHERE note_id = ?;", (note_id,)
    ).fetchone()
    return JSONResponse(status_code=201, content=row)


@router.post("/api/delete_vol_note")
def delete_vol_note(
    request: Request,
    note_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a single volunteer note by its ID.

    Args:
        request: Starlette Request; used for audit logging.
        note_id: UUID of the note to delete.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 204 with no body.

    Raises:
        HTTPException 404: Note not found.
        HTTPException 401/403: Insufficient role.
    """
    row = db.execute("SELECT vol_id FROM vol_notes WHERE note_id = ?;", (note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")
    db.execute("DELETE FROM vol_notes WHERE note_id = ?;", (note_id,))
    db.commit()
    audit(request, request.session.get("session_id"), f"Deleted note {note_id} for volunteer {row['vol_id']}")
    return Response(status_code=204)


@router.post("/api/drop_vol")
def drop_vol(
    request: Request,
    vol_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Unassign a volunteer from all slots and delete their record.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Volunteer ID to remove.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 204 with no body.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    db.execute("UPDATE schedule SET vol_id = NULL WHERE vol_id = ?;", (vol_id,))
    db.execute("DELETE FROM volunteers WHERE vol_id = ?;", (vol_id,))
    db.commit()
    broadcast("assignment")
    audit(request, request.session.get("session_id"), f"Dropped volunteer {vol_id}.")
    return Response(status_code=204)


@router.post("/api/checkin_vol")
def checkin_vol(
    request: Request,
    vol_id: str = Form(...),
    pos_key: str = Form(...),
    force: bool = Form(default=False),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Check a volunteer in at a specific slot, from the slot dialog's button.

    Same rule as scanning a check-in code (routers/checkin.record_checkin): more than 45
    minutes early or 10 minutes late comes back as ``needs_confirm`` with nothing written, and
    the dialog resends with ``force`` once the operator confirms.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Volunteer being checked in.
        pos_key: Composite slot key ``day_time_poskey`` stored in the note.
        force: Check in even when early or late.
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected PgClient.

    Returns:
        The record_checkin result: timing, and whether it was written.

    Raises:
        HTTPException 409: The volunteer isn't assigned to that slot.
        HTTPException 401/403: Insufficient role.
    """
    return record_checkin(request, db, s_data, vol_id, pos_key, force)


@router.post("/api/reset_vol_status")
def reset_vol_status(
    request: Request,
    vol_id: str = Form(...),
    pos_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer", "checkin")),
    db: PgClient = Depends(get_db),
):
    """Remove any check-in, check-in timing and No-Show notes for a volunteer at a slot.

    Args:
        request: Starlette Request; used for audit logging.
        vol_id: Volunteer whose status notes should be cleared.
        pos_key: Composite slot key ``day_time_poskey`` to target.
        s_data: Injected session dict; requires organizer or checkin role.
        db: Injected database connection.

    Returns:
        HTTP 204 with no body.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    db.execute(
        "DELETE FROM vol_notes WHERE vol_id = ? AND ("
        "  note = '[CheckIn] | ' || ? OR"
        "  note = '[Rating] No-Show | ' || ? OR"
        "  (note LIKE ? AND RIGHT(note, LENGTH(?) + 3) = ' | ' || ?)"
        ");",
        # The LIKE pattern is a parameter: a literal % in a query with parameters is read by
        # psycopg2 as a placeholder.
        (vol_id, pos_key, pos_key, "[CheckInTiming]%", pos_key, pos_key)
    )
    db.commit()
    broadcast("checkin")
    audit(request, request.session.get("session_id"), f"Reset status for vol_id={vol_id} at {pos_key}.")
    return Response(status_code=204)


@router.post("/api/vol_notes_report")
def vol_notes_report(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Return all organizer notes per volunteer, excluding [Rating] and [CheckIn] system notes.

    Args:
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        list of volunteer dicts, each with keys ``vol_id``, ``name``, ``pos_keys``,
        and ``notes`` (list of ``{note, username, created_at}``).

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    rows = db.execute("""
        SELECT v.vol_id, v.name,
               (SELECT STRING_AGG(s.day_key || '_' || s.time_key || '_' || s.pos_key, ', ')
                FROM schedule s WHERE s.vol_id = v.vol_id) as pos_keys,
               vn.note, vn.username, vn.created_at
        FROM volunteers v
        LEFT JOIN vol_notes vn ON v.vol_id = vn.vol_id
            AND vn.note NOT LIKE '[Rating]%'
            AND vn.note NOT LIKE '[CheckIn]%'
        ORDER BY v.name, vn.created_at DESC;
    """).fetchall()

    vols      = {}
    vol_order = []
    for row in rows:
        vid = row["vol_id"]
        if vid not in vols:
            vols[vid] = {"vol_id": vid, "name": row["name"],
                         "pos_keys": row["pos_keys"] or "", "notes": []}
            vol_order.append(vid)
        if row["note"] is not None:
            vols[vid]["notes"].append({
                "note": row["note"], "username": row["username"], "created_at": row["created_at"],
            })

    return [vols[vid] for vid in vol_order]


@router.get("/api/admin/volunteers")
def list_all_volunteers(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Every volunteer, whatever their status, with their picks, answers, and schedule.

    Backs Admin → Volunteers. Unlike get_vols, which feeds the assignment pickers and so lists
    assignable volunteers only, this includes pending, cancelled and inactive ones.

    Three queries for the whole roster, grouped here, rather than one per volunteer.
    Check-ins are the `[CheckIn] | {day}_{time}_{pos}` notes, matched exactly as
    _build_schedule matches them.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict ``{"volunteers": [...]}``, each with ``vol_id``, ``name``, ``status``,
        ``created_at``, ``time_pref``, ``add_time``, ``add_any``, ``answers``,
        ``possible_duplicate_of``, ``email_enc`` (ciphertext), ``picks`` (with labels),
        ``shifts`` (in schedule order, with ``checked_in``), and ``shift_count``.
    """
    vols = db.execute(
        "SELECT vol_id, name, status, created_at, time_pref, add_time, add_any, answers, "
        "possible_duplicate_of, cancelled_at, inactive_at, email_enc, "
        "(email_hmac IS NOT NULL) AS email_indexed FROM volunteers "
        "ORDER BY LOWER(name), vol_id;"
    ).fetchall()

    picks: dict = {}
    for p in db.execute(
        "SELECT vp.vol_id, vp.day_key, vp.time_key, vp.kind, vp.rank, "
        "d.day_str || ' ' || t.time_str AS label FROM vol_picks vp "
        "LEFT JOIN days_table d ON d.day_key = vp.day_key "
        "LEFT JOIN times_table t ON t.time_key = vp.time_key "
        "ORDER BY vp.vol_id, vp.kind DESC, vp.rank, vp.day_key, vp.time_key;"
    ).fetchall():
        picks.setdefault(p["vol_id"], []).append(
            {k: p[k] for k in ("day_key", "time_key", "kind", "rank", "label")})

    shifts: dict = {}
    for s in db.execute("""
        SELECT s.vol_id, s.day_key, d.day_str, s.time_key, t.time_str,
               s.pos_key, p.pos_str, p.pos_cat,
               EXISTS (
                   SELECT 1 FROM vol_notes vn
                   WHERE vn.vol_id = s.vol_id
                   AND vn.note = '[CheckIn] | ' || s.day_key || '_' || s.time_key || '_' || s.pos_key
               ) AS checked_in
        FROM schedule s
        LEFT JOIN days_table d  ON d.day_key  = s.day_key
        LEFT JOIN times_table t ON t.time_key = s.time_key
        LEFT JOIN positions p   ON p.pos_key  = s.pos_key
        WHERE s.vol_id IS NOT NULL
        ORDER BY s.vol_id, s.day_key, s.time_key, s.pos_key;
    """).fetchall():
        shifts.setdefault(s["vol_id"], []).append(
            {k: s[k] for k in ("day_key", "day_str", "time_key", "time_str",
                               "pos_key", "pos_str", "pos_cat", "checked_in")})

    out = []
    for v in vols:
        mine = shifts.get(v["vol_id"], [])
        out.append({
            "vol_id": v["vol_id"], "name": v["name"], "status": v["status"],
            "cancelled": v["cancelled_at"] is not None, "inactive": v["inactive_at"] is not None,
            # Ciphertext only; the Volunteers tab decrypts it in the browser with a private key
            # the organizer supplies (frontend lib/email_crypto.js). Never readable here.
            "email_enc": v["email_enc"],
            # False for a volunteer added by hand before add_vol computed the HMAC; the
            # Volunteers tab offers to fill these in once emails are decrypted.
            "email_indexed": bool(v["email_indexed"]),
            "created_at": v["created_at"].isoformat() + "Z" if v["created_at"] else None,
            "time_pref": v["time_pref"] or "", "add_time": v["add_time"] or "",
            "add_any": bool(v["add_any"]), "answers": v["answers"] or {},
            "possible_duplicate_of": v["possible_duplicate_of"] or [],
            "picks": picks.get(v["vol_id"], []),
            "shifts": mine, "shift_count": len(mine),
        })
    return {"volunteers": out}
