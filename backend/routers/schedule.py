"""The schedule: reading it, assigning volunteers, and editing its structure.

A schedule is days, times and positions, plus one `schedule` row per slot (a position at a
timeslot) holding its `vol_id`. `day_times` says which times each day runs and is kept a
superset of the pairs in `schedule`. All slot creation goes through `_day_time_pairs` and
`_fill_slots`, so "all times" means every time that day actually runs.

Slot keys are `{day_key}_{time_key}_{pos_key}`, taken apart with `split("_")`, so no key may
contain an underscore (`POS_KEY_RE`). `assign_vol` and `clear_timeslot` return the whole
updated schedule, so the browser needs no second fetch.

`schedule/build` writes a whole new schedule in one commit, and `schedule/apply_grid` diffs
the editor's counts grid against the live one. Neither removes a slot with someone assigned.
"""
import re
import json
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import Response

from db import get_db, get_session_data, RoleChecker, audit, PgClient
from routers.events import broadcast

router = APIRouter()

# pos_key is parsed as pos_key.split("_")[0:3] by assign_vol and clear_timeslot, so a key
# containing an underscore would silently break assignment.
POS_KEY_RE = re.compile(r'^[a-z0-9][a-z0-9-]*$')


def _validate_key(key: str, field: str) -> str:
    """Return key unchanged, or raise 400 if it would not survive slot-key parsing.

    Slot keys are ``{day_key}_{time_key}_{pos_key}`` and are taken apart with
    ``split("_")[0:3]``, so no component may contain an underscore.

    Args:
        key: Candidate key.
        field: Field name to quote in the error message.

    Returns:
        The key as given.

    Raises:
        HTTPException 400: Key is empty or contains anything but lowercase
            alphanumerics and hyphens.
    """
    if not POS_KEY_RE.match(key):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be lowercase letters, numbers, or hyphens "
                   f"(no spaces or underscores): {key!r}",
        )
    return key


def _validate_pos_key(pos_key: str, field: str = "pos_key") -> str:
    """Return pos_key unchanged, or raise 400 if it is not a safe position key."""
    return _validate_key(pos_key, field)


# Chip colors handed out to new categories in order, so each one is visibly distinct
# without the organizer having to pick anything. Cycles once exhausted.
_DEFAULT_CAT_COLORS = [
    "#ff4500", "#ffa500", "#3cb371", "#87ceeb",
    "#a78bfa", "#f472b6", "#facc15", "#2dd4bf",
]

COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def _norm_color(raw: str, field: str = "cat_color") -> Optional[str]:
    """Return a lowercased ``#rrggbb`` string, or None if raw is blank.

    The value ends up in a style attribute on the schedule chips, so anything that is not
    a plain six-digit hex color is rejected here rather than passed through.

    Args:
        raw: Candidate color, with or without surrounding whitespace.
        field: Field name to quote in the error message.

    Returns:
        The normalized color, or None when raw is empty.

    Raises:
        HTTPException 400: Non-empty value that is not ``#rrggbb``.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith("#"):
        raw = "#" + raw
    if not COLOR_RE.match(raw):
        raise HTTPException(status_code=400, detail=f"{field} must be a #rrggbb hex color: {raw!r}")
    return raw.lower()


def _next_cat_color(db: PgClient) -> str:
    """Return the default color for the next category, cycling _DEFAULT_CAT_COLORS."""
    row = db.execute("SELECT COUNT(*) AS n FROM categories;").fetchone()
    return _DEFAULT_CAT_COLORS[(row["n"] if row else 0) % len(_DEFAULT_CAT_COLORS)]


def _day_time_pairs(db: PgClient, day_keys=None, time_keys=None) -> list:
    """Return the (day_key, time_key) pairs a day actually runs, from day_times.

    Args:
        db: Active PgClient connection.
        day_keys: Optional list restricting the result to these days; None means all.
        time_keys: Optional list restricting the result to these times; None means all.

    Returns:
        list of (day_key, time_key) tuples. Empty if the filters match nothing.
    """
    if (day_keys is not None and not day_keys) or (time_keys is not None and not time_keys):
        return []
    # One static statement: a NULL array means "no filter". psycopg2 sends a list as an ARRAY
    # (a tuple would be a row), hence list().
    days = list(day_keys) if day_keys is not None else None
    times = list(time_keys) if time_keys is not None else None
    rows = db.execute(
        "SELECT day_key, time_key FROM day_times "
        "WHERE (?::text[] IS NULL OR day_key = ANY(?::text[])) "
        "AND (?::text[] IS NULL OR time_key = ANY(?::text[]));",
        (days, days, times, times),
    ).fetchall()
    return [(r["day_key"], r["time_key"]) for r in rows]


def _fill_slots(db: PgClient, pairs, pos_keys) -> int:
    """Insert every (day, time) pair × position into schedule, skipping existing rows.

    Args:
        db: Active PgClient connection; the caller commits.
        pairs: Iterable of (day_key, time_key) tuples, typically from _day_time_pairs().
        pos_keys: Iterable of position keys.

    Returns:
        Number of schedule rows actually created.
    """
    created = 0
    for day_key, time_key in pairs:
        for pos_key in pos_keys:
            cur = db.execute(
                "INSERT INTO schedule (day_key, time_key, pos_key) VALUES (?, ?, ?) "
                "ON CONFLICT (day_key, time_key, pos_key) DO NOTHING;",
                (day_key, time_key, pos_key),
            )
            created += cur.rowcount
    return created


# One complete statement per lookup table: table and column names can't be parameters.
_ALL_KEYS_SQL = {
    ("days_table", "day_key"): "SELECT day_key FROM days_table;",
    ("times_table", "time_key"): "SELECT time_key FROM times_table;",
    ("positions", "pos_key"): "SELECT pos_key FROM positions;",
}


def _all_keys(db: PgClient, table: str, col: str) -> list:
    """Return every key in one of the lookup tables, e.g. every day_key in days_table.

    Args:
        db: Active PgClient connection.
        table: ``days_table``, ``times_table`` or ``positions``.
        col: That table's key column.

    Returns:
        Every key in the table.

    Raises:
        KeyError: Not one of the three lookup tables.
    """
    return [r[col] for r in db.execute(_ALL_KEYS_SQL[(table, col)]).fetchall()]


def _split_keys(raw: str):
    """Parse a comma-separated key list from a form field; empty/blank means 'all' (None)."""
    keys = [k.strip() for k in (raw or "").split(",") if k.strip()]
    return keys or None


def _build_schedule(db: PgClient) -> list:
    """Return the full schedule as a nested list: days → times → categories → positions.

    Days are pre-loaded from days_table so days with no slots still appear in the output.
    Categories are ordered by their sort_order in the categories table; positions with no
    matching category row fall to the end (COALESCE sort_order 999).

    Args:
        db: Active PgClient connection.

    Returns:
        list of day dicts, each with keys ``key``, ``title``, ``event_date``, and ``times``.
        Each time has ``key``, ``title``, and ``categories``.
        Each category has ``key``, ``color`` (``#rrggbb`` or None) and ``positions``.
        Each position has ``key``, ``title``, ``day``, ``time``, ``category``,
        ``time_key``, ``day_key``, ``vol_id``, ``vol_name``, ``vol_nick``,
        ``has_note``, ``checked_in``.
    """
    day_rows = db.execute(
        "SELECT day_key, day_str, event_date FROM days_table ORDER BY day_key;"
    ).fetchall()
    if not day_rows:
        return []

    day_map   = {r["day_key"]: {"key": r["day_key"], "title": r["day_str"],
                                 "event_date": r["event_date"] or "", "times": []}
                 for r in day_rows}
    day_order = [r["day_key"] for r in day_rows]

    rows = db.execute("""
        SELECT s.day_key, s.time_key, times_table.time_str,
               s.pos_key, positions.pos_cat, positions.pos_str, categories.cat_color,
               s.vol_id, volunteers.name as vol_name,
               COALESCE(NOT volunteers.is_live, FALSE) AS vol_gone,
               CASE WHEN sn.pos_key IS NOT NULL THEN 1 ELSE 0 END as has_note,
               CASE WHEN EXISTS (
                   SELECT 1 FROM vol_notes vn
                   WHERE vn.vol_id = s.vol_id
                   AND vn.note = '[CheckIn] | ' || s.day_key || '_' || s.time_key || '_' || s.pos_key
               ) THEN 1 ELSE 0 END as checked_in
        FROM schedule s
        LEFT JOIN times_table   ON s.time_key = times_table.time_key
        LEFT JOIN positions     ON s.pos_key  = positions.pos_key
        LEFT JOIN categories    ON positions.pos_cat = categories.cat_name
        LEFT JOIN volunteers    ON s.vol_id   = volunteers.vol_id
        LEFT JOIN schedule_notes sn ON s.day_key  = sn.day_key
                                   AND s.time_key = sn.time_key
                                   AND s.pos_key  = sn.pos_key
        ORDER BY s.day_key, s.time_key, COALESCE(categories.sort_order, 999),
                 LENGTH(s.pos_key), s.pos_key, s.vol_id;
    """).fetchall()

    cur_dk   = None
    cur_time = {"key": None}
    cur_cat  = {"key": None}

    def flush_cat():
        if cur_cat["key"] is not None:
            cur_time["categories"].append(cur_cat)

    def flush_time(dk):
        if cur_time["key"] is not None:
            flush_cat()
            day_map[dk]["times"].append(cur_time)

    for row in rows:
        dk = row["day_key"]

        if dk != cur_dk:
            flush_time(cur_dk) if cur_dk else None
            cur_dk   = dk
            cur_time = {"key": None}
            cur_cat  = {"key": None}

        if row["time_key"] != cur_time["key"]:
            flush_time(dk)
            cur_time = {"key": row["time_key"], "title": row["time_str"], "categories": []}
            cur_cat  = {"key": None}

        if row["pos_cat"] != cur_cat["key"]:
            if cur_cat["key"] is not None:
                cur_time["categories"].append(cur_cat)
            cur_cat = {"key": row["pos_cat"], "color": row["cat_color"], "positions": []}

        ts_key = f"{dk}_{row['time_key']}_{row['pos_key']}"
        vol_id = vol_name = vol_nick = ""
        if row["vol_id"] is not None:
            vol_id   = row["vol_id"]
            vol_name = row["vol_name"]
            idx = vol_name.find("(")
            if idx > 0:
                vol_nick = vol_name[idx:]
                vol_name = vol_name[:idx - 1]

        cur_cat["positions"].append({
            "key": ts_key, "title": row["pos_str"],
            "day": day_map[dk]["title"], "time": row["time_str"], "category": row["pos_cat"],
            "time_key": row["time_key"], "day_key": dk,
            "vol_id": vol_id, "vol_name": vol_name, "vol_nick": vol_nick,
            "has_note": row["has_note"], "checked_in": bool(row["checked_in"]),
            # Assigned, but the volunteer has cancelled or been made inactive: the slot needs
            # someone else (Timeslot.vue marks it; GET /api/admin/shift_flags lists it).
            "needs_replacement": bool(row["vol_id"] is not None and row["vol_gone"]),
        })

    if cur_dk:
        flush_time(cur_dk)

    return [day_map[dk] for dk in day_order]


@router.get("/api/get_schedule")
def get_schedule(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return the full schedule tree for all configured days.

    Args:
        s_data: Injected session dict; any authenticated user is permitted.
        db: Injected PgClient from get_db().

    Returns:
        dict with key ``schedule`` containing the nested list from _build_schedule().

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"schedule": _build_schedule(db)}


_RANGE = re.compile(r"^\s*(\d{3,4})\s*-\s*(\d{3,4})")


def time_range(time_str: Optional[str]) -> Optional[tuple]:
    """Minutes since midnight for a time_str like '1130-1530'; None if it isn't one.

    The portal's routers/signup.py has its own copy for the same rule at signup; the two apps
    share no code. Keep them in step.

    Args:
        time_str: A range like `'1130-1530'`. One that ends before it starts runs past midnight.

    Returns:
        `(start, end)` in minutes since midnight, or `None`.
    """
    m = _RANGE.match(time_str or "")
    if not m:
        return None
    start, end = (int(x.zfill(4)[:2]) * 60 + int(x.zfill(4)[2:]) for x in m.groups())
    if end <= start:
        end += 24 * 60   # runs past midnight
    return start, end


def times_overlap(a_key: str, a_str: Optional[str], b_key: str, b_str: Optional[str]) -> bool:
    """Whether two timeslots on the same day overlap. Same key always does; an unparseable
    time_str overlaps nothing else, rather than blocking assignments on a guess.

    Args:
        a_key: The first timeslot's `time_key`.
        a_str: The first timeslot's `time_str`.
        b_key: The second timeslot's `time_key`.
        b_str: The second timeslot's `time_str`.
    """
    if a_key == b_key:
        return True
    a, b = time_range(a_str), time_range(b_str)
    return bool(a and b and a[0] < b[1] and b[0] < a[1])


# Picker groups, in display order. The labels are the frontend's (TimeslotDialog.vue).
AVAIL_GROUPS = ("requested", "flexible", "available", "no_picks")


@router.post("/api/get_avail_vols")
def get_avail_vols(
    pos_key: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Active volunteers who could fill a slot, grouped by why, minus anyone already busy then.

    Groups, in order (each volunteer appears once, in the first that fits):
        requested — one of their two picks is this day and start time
        flexible  — answered "I'm flexible" and picked something on this day
        available — listed this timeslot as "also available", or ticked "any time"
        no_picks  — no picks on file (added by an organizer, or from before signup existed)
    Anyone with picks that fit none of these is left out.

    Excluded: volunteers already assigned on the same day at a start time whose range
    overlaps this one (times_overlap), which includes this very timeslot.

    Args:
        pos_key: Composite slot key in the form ``day_time_poskey``.

    Returns:
        list of volunteer dicts from _query_vols(), each with a ``group``, ordered by group
        then name.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    day, time = pos_key.split("_")[0:2]
    times = {r["time_key"]: r["time_str"]
             for r in db.execute("SELECT time_key, time_str FROM times_table;").fetchall()}
    clash = [f"{day}_{r['time_key']}" for r in db.execute(
        "SELECT time_key FROM day_times WHERE day_key = ?;", (day,)).fetchall()
        if times_overlap(time, times.get(time), r["time_key"], times.get(r["time_key"]))]

    picks: dict = {}
    for p in db.execute("SELECT vol_id, day_key, time_key, kind FROM vol_picks;").fetchall():
        picks.setdefault(p["vol_id"], []).append((p["day_key"], p["time_key"], p["kind"]))
    flags = {r["vol_id"]: r for r in db.execute(
        "SELECT vol_id, add_any, COALESCE(answers->>'flexible', '') = 'true' AS flexible "
        "FROM volunteers WHERE is_assignable;").fetchall()}

    def group(vol_id: str) -> Optional[str]:
        mine = picks.get(vol_id, [])
        f = flags.get(vol_id)
        if any(k == "pref" and d == day and t == time for d, t, k in mine):
            return "requested"
        if f and f["flexible"] and any(k == "pref" and d == day for d, t, k in mine):
            return "flexible"
        if (f and f["add_any"]) or any(k == "add" and d == day and t == time for d, t, k in mine):
            return "available"
        if not mine:
            return "no_picks"
        return None

    from routers.volunteers import _query_vols
    out = []
    for v in _query_vols(db, schedule_filter=clash):
        g = group(v["key"])
        if g:
            v["group"] = g
            out.append(v)
    out.sort(key=lambda v: (AVAIL_GROUPS.index(v["group"]), v["name"].lower()))
    return out


@router.post("/api/get_unassigned_timeslots")
def get_unassigned_timeslots(
    vol_id: str = Form(...),
    allow_back_to_back: str = Form(default="false"),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return empty schedule slots compatible with a volunteer's existing assignments.

    Args:
        vol_id: ID of the volunteer to check conflicts against.
        allow_back_to_back: ``"true"`` to include back-to-back slots; defaults to ``"false"``.
        s_data: Injected session dict; any authenticated user is permitted.
        db: Injected PgClient from get_db().

    Returns:
        list of dicts with keys ``key``, ``day``, ``time``, ``pos`` for each open slot.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Back-to-back means an overlapping shift on the same day; allowing it still excludes the
    # same start time. Overlap is from the time ranges (times_overlap), as in get_avail_vols.
    back_to_back = allow_back_to_back.lower() == "true"
    times = {r["time_key"]: r["time_str"]
             for r in db.execute("SELECT time_key, time_str FROM times_table;").fetchall()}
    day_times = db.execute("SELECT day_key, time_key FROM day_times;").fetchall()
    filter_times = []

    for time in db.execute("SELECT day_key, time_key FROM schedule WHERE vol_id = ?;", (vol_id,)).fetchall():
        filter_times.append(f"{time['day_key']}_{time['time_key']}")
        if not back_to_back:
            filter_times += [
                f"{dt['day_key']}_{dt['time_key']}" for dt in day_times
                if dt["day_key"] == time["day_key"] and times_overlap(
                    time["time_key"], times.get(time["time_key"]),
                    dt["time_key"], times.get(dt["time_key"]))]

    rows = db.execute(
        "SELECT s.day_key, days_table.day_str, s.time_key, times_table.time_str, "
        "s.pos_key, positions.pos_cat, positions.pos_str "
        "FROM schedule s "
        "LEFT JOIN days_table  ON s.day_key  = days_table.day_key "
        "LEFT JOIN times_table ON s.time_key = times_table.time_key "
        "LEFT JOIN positions   ON s.pos_key  = positions.pos_key "
        "WHERE s.vol_id IS NULL ORDER BY s.day_key, s.time_key, positions.pos_cat, s.pos_key;"
    ).fetchall()

    return [
        {"key": f"{r['day_key']}_{r['time_key']}_{r['pos_key']}",
         "day": r["day_str"], "time": r["time_str"], "pos": r["pos_str"]}
        for r in rows
        if f"{r['day_key']}_{r['time_key']}" not in filter_times
    ]


@router.post("/api/assign_vol")
def assign_vol(
    request: Request,
    pos_key: str = Form(...),
    vol_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Assign a volunteer to a schedule slot and return the updated schedule.

    Args:
        request: Starlette Request; used for audit logging.
        pos_key: Composite slot key ``day_time_poskey``.
        vol_id: ID of the volunteer to assign.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict with key ``schedule`` containing the full updated schedule tree.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    parts = pos_key.split("_")
    db.execute(
        "UPDATE schedule SET vol_id = ? WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (vol_id, parts[0], parts[1], parts[2])
    )
    db.commit()
    broadcast("assignment")
    audit(request, request.session.get("session_id"), f"Assigned {vol_id} to {pos_key}.")
    return {"schedule": _build_schedule(db)}


@router.post("/api/clear_timeslot")
def clear_timeslot(
    request: Request,
    pos_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Remove the assigned volunteer from a slot and return the updated schedule.

    Args:
        request: Starlette Request; used for audit logging.
        pos_key: Composite slot key ``day_time_poskey``.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict with key ``schedule`` containing the full updated schedule tree.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    parts = pos_key.split("_")
    db.execute(
        "UPDATE schedule SET vol_id = NULL WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (parts[0], parts[1], parts[2])
    )
    db.commit()
    broadcast("assignment")
    audit(request, request.session.get("session_id"), f"Cleared timeslot {pos_key}.")
    return {"schedule": _build_schedule(db)}


@router.post("/api/get_schedule_note")
def get_schedule_note(
    pos_key: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return the organizer note for a specific slot.

    Args:
        pos_key: Composite slot key ``day_time_poskey``.
        s_data: Injected session dict; any authenticated user is permitted.
        db: Injected PgClient from get_db().

    Returns:
        dict with keys ``note``, ``username``, ``updated_at`` if a note exists;
        empty dict otherwise.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    parts = pos_key.split("_", 2)
    day_key, time_key, p_key = parts[0], parts[1], parts[2]
    row = db.execute(
        "SELECT note, username, updated_at FROM schedule_notes "
        "WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (day_key, time_key, p_key)
    ).fetchone()
    return row if row else {}


@router.post("/api/set_schedule_note")
def set_schedule_note(
    request: Request,
    pos_key: str = Form(...),
    note: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Upsert a slot note; passing an empty note deletes the existing note.

    Args:
        request: Starlette Request; used for audit logging.
        pos_key: Composite slot key ``day_time_poskey``.
        note: Note text; empty string triggers deletion of any existing note.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        HTTP 204 with no body.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    parts = pos_key.split("_", 2)
    day_key, time_key, p_key = parts[0], parts[1], parts[2]
    note = note.strip()
    if note:
        db.execute(
            "INSERT INTO schedule_notes (day_key, time_key, pos_key, note, username, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NOW()) "
            "ON CONFLICT(day_key, time_key, pos_key) DO UPDATE SET "
            "note = excluded.note, username = excluded.username, updated_at = excluded.updated_at;",
            (day_key, time_key, p_key, note, s_data["username"])
        )
        audit(request, request.session.get("session_id"), f"Set note on slot {day_key}/{time_key}/{p_key}: {note!r}")
    else:
        db.execute(
            "DELETE FROM schedule_notes WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
            (day_key, time_key, p_key)
        )
        audit(request, request.session.get("session_id"), f"Cleared note on slot {day_key}/{time_key}/{p_key}.")
    db.commit()
    broadcast("note")
    return Response(status_code=204)


# ── schedule structure management ────────────────────────────────────────────

@router.get("/api/schedule/structure")
def schedule_structure(
    s_data: dict = Depends(RoleChecker("organizer", "checkin", "view")),
    db: PgClient = Depends(get_db),
):
    """Return raw lookup tables used by the Schedule Editor dialog.

    Args:
        s_data: Injected session dict; requires organizer, checkin, or view role.
        db: Injected PgClient from get_db().

    Returns:
        dict with keys ``days``, ``times``, ``day_times``, ``positions``, ``slots``, and
        ``categories`` (each a list of row dicts).

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    days       = db.execute("SELECT day_key, day_str, event_date FROM days_table ORDER BY day_key;").fetchall()
    times      = db.execute("SELECT time_key, time_str FROM times_table ORDER BY time_key;").fetchall()
    day_times  = db.execute("SELECT day_key, time_key FROM day_times ORDER BY day_key, time_key;").fetchall()
    positions  = db.execute(
        "SELECT pos_key, pos_cat, pos_str FROM positions "
        "ORDER BY pos_cat, LENGTH(pos_key), pos_key;"
    ).fetchall()
    slots      = db.execute(
        "SELECT day_key, time_key, pos_key, "
        "CASE WHEN vol_id IS NOT NULL THEN 1 ELSE 0 END as assigned "
        "FROM schedule ORDER BY day_key, time_key, LENGTH(pos_key), pos_key;"
    ).fetchall()
    categories = db.execute(
        "SELECT cat_name, sort_order, cat_color FROM categories ORDER BY sort_order;"
    ).fetchall()
    return {"days": days, "times": times, "day_times": day_times,
            "positions": positions, "slots": slots, "categories": categories}


@router.post("/api/schedule/upsert_day")
def upsert_day(
    request: Request,
    day_key:    str = Form(...),
    day_str:    str = Form(...),
    event_date: str = Form(""),
    time_keys:  str = Form(""),
    fill_all:   bool = Form(False),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Insert or update a day entry, optionally declaring its times and filling its slots.

    Args:
        request: Starlette Request; used for audit logging.
        day_key: Short identifier, e.g. ``"sat"`` (lowercased on insert).
        day_str: Display label, e.g. ``"Saturday"``.
        event_date: ISO date ``YYYY-MM-DD`` for precise past-detection; empty string → NULL.
        time_keys: Comma-separated times this day runs, written to day_times; blank means
            every time in times_table. Existing day_times rows are never removed here —
            use set_day_time to take a time off a day.
        fill_all: When true, create a slot for every position at each of this day's times.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True, "slots_created": int}``.

    Raises:
        HTTPException 400: day_key or day_str is blank, or a named time does not exist.
        HTTPException 401/403: Insufficient role.
    """
    day_key    = day_key.strip().lower()
    day_str    = day_str.strip()
    event_date = event_date.strip() or None
    if not day_key or not day_str:
        raise HTTPException(status_code=400, detail="day_key and day_str are required")
    db.execute(
        "INSERT INTO days_table (day_key, day_str, event_date) VALUES (?, ?, ?) "
        "ON CONFLICT(day_key) DO UPDATE SET day_str = excluded.day_str, event_date = excluded.event_date;",
        (day_key, day_str, event_date),
    )

    times = _split_keys(time_keys) or _all_keys(db, "times_table", "time_key")
    for tk in times:
        if not db.execute("SELECT 1 FROM times_table WHERE time_key = ?;", (tk,)).fetchone():
            raise HTTPException(status_code=400, detail=f"Time '{tk}' does not exist")
        db.execute(
            "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
            (day_key, tk),
        )

    slots_created = 0
    if fill_all:
        slots_created = _fill_slots(
            db, _day_time_pairs(db, [day_key]), _all_keys(db, "positions", "pos_key")
        )

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Upserted day {day_key!r} ({day_str}), {len(times)} time(s), {slots_created} slot(s)")
    return {"ok": True, "slots_created": slots_created}


@router.post("/api/schedule/delete_day")
def delete_day(
    request: Request,
    day_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a day and all its schedule slots and notes.

    Args:
        request: Starlette Request; used for audit logging.
        day_key: Short identifier of the day to remove.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: The day has volunteers assigned to its slots.
        HTTPException 401/403: Insufficient role.
    """
    if db.execute(
        "SELECT 1 FROM schedule WHERE day_key = ? AND vol_id IS NOT NULL;", (day_key,)
    ).fetchone():
        raise HTTPException(status_code=400, detail="Cannot delete: day has assigned volunteers")
    db.execute("DELETE FROM schedule_notes WHERE day_key = ?;", (day_key,))
    db.execute("DELETE FROM schedule WHERE day_key = ?;", (day_key,))
    db.execute("DELETE FROM day_times WHERE day_key = ?;", (day_key,))
    db.execute("DELETE FROM days_table WHERE day_key = ?;", (day_key,))
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Deleted day {day_key!r}")
    return {"ok": True}


@router.post("/api/schedule/upsert_time")
def upsert_time(
    request: Request,
    time_key: str = Form(...),
    time_str: str = Form(...),
    day_keys: str = Form(""),
    fill_all: bool = Form(False),
    create:   bool = Form(False),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Insert or update a time, optionally declaring its days and filling its slots.

    Args:
        request: Starlette Request; used for audit logging.
        time_key: Short identifier, e.g. ``"0800"``.
        time_str: Display label, e.g. ``"0800-1130"``.
        day_keys: Comma-separated days that run this time, written to day_times; blank
            means every day in days_table. Existing day_times rows are never removed here —
            use set_day_time to take this time off a day.
        fill_all: When true, create a slot for every position on each of those days.
        create: Set by the add path. Rejects a time_key that already exists with a
            different range rather than silently relabelling it — two days running
            different-length shifts from the same start need two distinct keys.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True, "slots_created": int}``.

    Raises:
        HTTPException 400: time_key or time_str is blank, a named day does not exist, or
            create was set and the key is already taken by a different range.
        HTTPException 401/403: Insufficient role.
    """
    time_key = time_key.strip()
    time_str = time_str.strip()
    if not time_key or not time_str:
        raise HTTPException(status_code=400, detail="time_key and time_str are required")
    if create:
        row = db.execute("SELECT time_str FROM times_table WHERE time_key = ?;", (time_key,)).fetchone()
        if row and row["time_str"] != time_str:
            raise HTTPException(
                status_code=400,
                detail=f"Time key '{time_key}' is already used by {row['time_str']}. "
                       f"Give this range a different key.",
            )
    db.execute(
        "INSERT INTO times_table (time_key, time_str) VALUES (?, ?) "
        "ON CONFLICT(time_key) DO UPDATE SET time_str = excluded.time_str;",
        (time_key, time_str),
    )

    days = _split_keys(day_keys) or _all_keys(db, "days_table", "day_key")
    for dk in days:
        if not db.execute("SELECT 1 FROM days_table WHERE day_key = ?;", (dk,)).fetchone():
            raise HTTPException(status_code=400, detail=f"Day '{dk}' does not exist")
        db.execute(
            "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
            (dk, time_key),
        )

    slots_created = 0
    if fill_all:
        slots_created = _fill_slots(
            db, _day_time_pairs(db, None, [time_key]), _all_keys(db, "positions", "pos_key")
        )

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Upserted time {time_key!r} ({time_str}), {len(days)} day(s), {slots_created} slot(s)")
    return {"ok": True, "slots_created": slots_created}


@router.post("/api/schedule/set_day_time")
def set_day_time(
    request: Request,
    day_key:  str  = Form(...),
    time_key: str  = Form(...),
    enabled:  bool = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Declare, or withdraw, that a day runs a given time.

    Turning a time off deletes its empty slots on that day, so it disappears from the
    schedule view. Assigned slots block the removal rather than being discarded.

    Args:
        request: Starlette Request; used for audit logging.
        day_key: Must already exist in days_table.
        time_key: Must already exist in times_table.
        enabled: True to add the pair to day_times, False to remove it.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True, "slots_removed": int}``.

    Raises:
        HTTPException 400: Unknown day or time, or the pair has assigned volunteers.
        HTTPException 401/403: Insufficient role.
    """
    if not db.execute("SELECT 1 FROM days_table WHERE day_key = ?;", (day_key,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Day '{day_key}' does not exist")
    if not db.execute("SELECT 1 FROM times_table WHERE time_key = ?;", (time_key,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Time '{time_key}' does not exist")

    slots_removed = 0
    if enabled:
        db.execute(
            "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
            (day_key, time_key),
        )
    else:
        if db.execute(
            "SELECT 1 FROM schedule WHERE day_key = ? AND time_key = ? AND vol_id IS NOT NULL;",
            (day_key, time_key),
        ).fetchone():
            raise HTTPException(
                status_code=400,
                detail="Cannot remove: this day and time has assigned volunteers",
            )
        db.execute("DELETE FROM schedule_notes WHERE day_key = ? AND time_key = ?;", (day_key, time_key))
        slots_removed = db.execute(
            "DELETE FROM schedule WHERE day_key = ? AND time_key = ?;", (day_key, time_key)
        ).rowcount
        db.execute("DELETE FROM day_times WHERE day_key = ? AND time_key = ?;", (day_key, time_key))

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"{'Added' if enabled else 'Removed'} time {time_key} "
          f"{'to' if enabled else 'from'} day {day_key} ({slots_removed} slot(s) removed)")
    return {"ok": True, "slots_removed": slots_removed}


@router.post("/api/schedule/delete_time")
def delete_time(
    request: Request,
    time_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a time slot and all schedule entries and notes that reference it.

    Args:
        request: Starlette Request; used for audit logging.
        time_key: Short identifier of the time to remove.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: The time has volunteers assigned to its slots.
        HTTPException 401/403: Insufficient role.
    """
    if db.execute(
        "SELECT 1 FROM schedule WHERE time_key = ? AND vol_id IS NOT NULL;", (time_key,)
    ).fetchone():
        raise HTTPException(status_code=400, detail="Cannot delete: time has assigned volunteers")
    db.execute("DELETE FROM schedule_notes WHERE time_key = ?;", (time_key,))
    db.execute("DELETE FROM schedule WHERE time_key = ?;", (time_key,))
    db.execute("DELETE FROM day_times WHERE time_key = ?;", (time_key,))
    db.execute("DELETE FROM times_table WHERE time_key = ?;", (time_key,))
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Deleted time {time_key!r}")
    return {"ok": True}


@router.post("/api/schedule/upsert_position")
def upsert_position(
    request: Request,
    pos_key:  str = Form(...),
    pos_cat:  str = Form(...),
    pos_str:  str = Form(...),
    fill_all: bool = Form(False),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Insert or update a position definition, optionally scheduling it everywhere.

    Args:
        request: Starlette Request; used for audit logging.
        pos_key: Short identifier, e.g. ``"sec1"``; lowercase alphanumerics and hyphens only.
        pos_cat: Category name, e.g. ``"Security"``; should match a row in categories.
        pos_str: Display label, e.g. ``"Security 1"``.
        fill_all: When true, create a slot for this position at every declared day/time pair.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True, "slots_created": int}``.

    Raises:
        HTTPException 400: Any required field is blank, or pos_key has invalid characters.
        HTTPException 401/403: Insufficient role.
    """
    pos_key = pos_key.strip()
    pos_cat = pos_cat.strip()
    pos_str = pos_str.strip()
    if not pos_key or not pos_cat or not pos_str:
        raise HTTPException(status_code=400, detail="pos_key, pos_cat, and pos_str are required")
    _validate_pos_key(pos_key)
    db.execute(
        "INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES (?, ?, ?) "
        "ON CONFLICT(pos_key) DO UPDATE SET pos_cat = excluded.pos_cat, pos_str = excluded.pos_str;",
        (pos_key, pos_cat, pos_str),
    )

    slots_created = 0
    if fill_all:
        slots_created = _fill_slots(db, _day_time_pairs(db), [pos_key])

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Upserted position {pos_key!r} ({pos_cat}/{pos_str}), {slots_created} slot(s)")
    return {"ok": True, "slots_created": slots_created}


@router.post("/api/schedule/delete_position")
def delete_position(
    request: Request,
    pos_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a position definition and all schedule slots and notes that reference it.

    Args:
        request: Starlette Request; used for audit logging.
        pos_key: Short identifier of the position to remove.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: The position has a volunteer assigned.
        HTTPException 401/403: Insufficient role.
    """
    if db.execute(
        "SELECT 1 FROM schedule WHERE pos_key = ? AND vol_id IS NOT NULL;", (pos_key,)
    ).fetchone():
        raise HTTPException(status_code=400, detail="Cannot delete: position has assigned volunteers")
    db.execute("DELETE FROM schedule_notes WHERE pos_key = ?;", (pos_key,))
    db.execute("DELETE FROM schedule WHERE pos_key = ?;", (pos_key,))
    db.execute("DELETE FROM positions WHERE pos_key = ?;", (pos_key,))
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Deleted position {pos_key!r}")
    return {"ok": True}


@router.post("/api/schedule/add_slot")
def add_schedule_slot(
    request: Request,
    day_key:  str = Form(...),
    time_key: str = Form(...),
    pos_key:  str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Add a single (day, time, position) slot to the schedule; silently ignores duplicates.

    Args:
        request: Starlette Request; used for audit logging.
        day_key: Must already exist in days_table.
        time_key: Must already exist in times_table.
        pos_key: Must already exist in positions.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: day_key, time_key, or pos_key not found.
        HTTPException 401/403: Insufficient role.
    """
    if not db.execute("SELECT 1 FROM days_table WHERE day_key = ?;", (day_key,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Day '{day_key}' does not exist")
    if not db.execute("SELECT 1 FROM times_table WHERE time_key = ?;", (time_key,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Time '{time_key}' does not exist")
    if not db.execute("SELECT 1 FROM positions WHERE pos_key = ?;", (pos_key,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Position '{pos_key}' does not exist")
    # Keep day_times a superset of the pairs actually in use.
    db.execute(
        "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
        (day_key, time_key),
    )
    db.execute(
        "INSERT INTO schedule (day_key, time_key, pos_key) VALUES (?, ?, ?) ON CONFLICT (day_key, time_key, pos_key) DO NOTHING;",
        (day_key, time_key, pos_key),
    )
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Added slot {day_key}_{time_key}_{pos_key}")
    return {"ok": True}


@router.post("/api/schedule/bulk_add_slots")
def bulk_add_slots(
    request: Request,
    cat:       str = Form(...),
    count:     int = Form(...),
    prefix:    str = Form(""),
    label:     str = Form(""),
    start:     int = Form(1),
    day_keys:  str = Form(""),
    time_keys: str = Form(""),
    time_key:  str = Form(""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Create N numbered positions and schedule them across days and times.

    Keys are ``{prefix}{n}`` and labels ``{label} {n}`` for n in ``start … start+count-1``.
    Slots are only created for day/time pairs declared in day_times, so a day that does not
    run a given time is skipped rather than being given phantom slots. Existing positions
    and slots are silently kept via ON CONFLICT DO NOTHING, so re-running is safe.

    Args:
        request: Starlette Request; used for audit logging.
        cat: Category name; must exist in the categories table.
        count: Number of positions to create/schedule (1–50).
        prefix: Position key prefix; defaults to the category name stripped to alphanumerics.
        label: Display-name prefix; defaults to the category name.
        start: First numeric suffix; defaults to 1.
        day_keys: Comma-separated day keys; blank means every day.
        time_keys: Comma-separated time keys; blank means every time.
        time_key: Deprecated single-time alias for time_keys, kept so an old client still works.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict with keys ``positions_created`` and ``slots_created`` (counts of new rows).

    Raises:
        HTTPException 400: Invalid category, prefix, count or start, or unknown day/time.
        HTTPException 401/403: Insufficient role.
    """
    if not 1 <= count <= 50:
        raise HTTPException(status_code=400, detail="Count must be between 1 and 50")
    if start < 1 or start + count - 1 > 999:
        raise HTTPException(status_code=400, detail="Start must be at least 1, and start + count cannot exceed 999")
    if not db.execute("SELECT 1 FROM categories WHERE cat_name = ?;", (cat,)).fetchone():
        raise HTTPException(status_code=400, detail=f"Category '{cat}' does not exist")

    prefix = prefix.strip().lower() or re.sub(r'[^a-z0-9]', '', cat.lower()) or 'pos'
    _validate_pos_key(prefix, "prefix")
    label = label.strip() or cat

    days  = _split_keys(day_keys)
    times = _split_keys(time_keys) or _split_keys(time_key)
    for dk in days or []:
        if not db.execute("SELECT 1 FROM days_table WHERE day_key = ?;", (dk,)).fetchone():
            raise HTTPException(status_code=400, detail=f"Day '{dk}' does not exist")
    for tk in times or []:
        if not db.execute("SELECT 1 FROM times_table WHERE time_key = ?;", (tk,)).fetchone():
            raise HTTPException(status_code=400, detail=f"Time '{tk}' does not exist")

    numbers  = range(start, start + count)
    pos_keys = [f"{prefix}{n}" for n in numbers]

    positions_created = 0
    for n, pos_key in zip(numbers, pos_keys):
        cur = db.execute(
            "INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES (?, ?, ?) ON CONFLICT (pos_key) DO NOTHING;",
            (pos_key, cat, f"{label} {n}"),
        )
        positions_created += cur.rowcount

    pairs         = _day_time_pairs(db, days, times)
    slots_created = _fill_slots(db, pairs, pos_keys)

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Bulk added {positions_created} positions and {slots_created} slots "
          f"({cat}, prefix={prefix}, {start}–{start + count - 1}, {len(pairs)} day/time pair(s))")
    return {"positions_created": positions_created, "slots_created": slots_created}


@router.post("/api/schedule/remove_slot")
def remove_schedule_slot(
    request: Request,
    day_key:  str = Form(...),
    time_key: str = Form(...),
    pos_key:  str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Remove a single schedule slot and its note, if no volunteer is assigned.

    Args:
        request: Starlette Request; used for audit logging.
        day_key: Day component of the slot.
        time_key: Time component of the slot.
        pos_key: Position component of the slot.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: A volunteer is currently assigned to the slot.
        HTTPException 404: Slot not found.
        HTTPException 401/403: Insufficient role.
    """
    slot = db.execute(
        "SELECT vol_id FROM schedule WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (day_key, time_key, pos_key),
    ).fetchone()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    if slot["vol_id"]:
        raise HTTPException(status_code=400, detail="Cannot remove: slot has an assigned volunteer")
    db.execute(
        "DELETE FROM schedule WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (day_key, time_key, pos_key),
    )
    db.execute(
        "DELETE FROM schedule_notes WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
        (day_key, time_key, pos_key),
    )
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Removed slot {day_key}_{time_key}_{pos_key}")
    return {"ok": True}


# ── category management ───────────────────────────────────────────────────────

@router.post("/api/schedule/upsert_category")
def upsert_category(
    request: Request,
    cat_name:  str = Form(...),
    cat_color: str = Form(""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Add a category at the end of the sort order, or recolor one that already exists.

    A new category with no color given is assigned the next default from
    _DEFAULT_CAT_COLORS, so every category is visibly distinct without the organizer
    picking anything. Adding a name that already exists keeps its sort_order: with a
    color it is recolored, without one it is left alone.

    Args:
        request: Starlette Request; used for audit logging.
        cat_name: Display name of the category to add.
        cat_color: Optional ``#rrggbb`` chip color; blank means "leave as is".
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: cat_name is blank, or cat_color is not a hex color.
        HTTPException 401/403: Insufficient role.
    """
    cat_name = cat_name.strip()
    if not cat_name:
        raise HTTPException(status_code=400, detail="cat_name is required")
    color = _norm_color(cat_color)
    row = db.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM categories;").fetchone()
    next_order = row["next"] if row else 0
    if color:
        db.execute(
            "INSERT INTO categories (cat_name, sort_order, cat_color) VALUES (?, ?, ?) "
            "ON CONFLICT (cat_name) DO UPDATE SET cat_color = excluded.cat_color;",
            (cat_name, next_order, color),
        )
    else:
        db.execute(
            "INSERT INTO categories (cat_name, sort_order, cat_color) VALUES (?, ?, ?) "
            "ON CONFLICT (cat_name) DO NOTHING;",
            (cat_name, next_order, _next_cat_color(db)),
        )
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Upserted category {cat_name!r}" + (f" with color {color}" if color else ""))
    return {"ok": True}


@router.post("/api/schedule/delete_category")
def delete_category(
    request: Request,
    cat_name: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a category if no positions currently reference it.

    Args:
        request: Starlette Request; used for audit logging.
        cat_name: Name of the category to remove.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: One or more positions reference this category.
        HTTPException 401/403: Insufficient role.
    """
    if db.execute("SELECT 1 FROM positions WHERE pos_cat = ?;", (cat_name,)).fetchone():
        raise HTTPException(status_code=400, detail="Cannot delete: category has positions referencing it")
    db.execute("DELETE FROM categories WHERE cat_name = ?;", (cat_name,))
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Deleted category {cat_name!r}")
    return {"ok": True}


@router.post("/api/schedule/reorder_categories")
def reorder_categories(
    request: Request,
    cat_names: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Update sort_order for all categories based on the supplied ordering.

    Args:
        request: Starlette Request; used for audit logging.
        cat_names: Comma-separated category names in the desired display order;
                   each name's position in the list becomes its new sort_order.
        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    names = [n.strip() for n in cat_names.split(",") if n.strip()]
    for i, name in enumerate(names):
        db.execute("UPDATE categories SET sort_order = ? WHERE cat_name = ?;", (i, name))
    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"), f"Reordered {len(names)} categories")
    return {"ok": True}


# ── schedule builder ──────────────────────────────────────────────────────────

# Guards against a runaway payload; generous enough for any real conference.
BUILD_MAX_DAYS       = 30
BUILD_MAX_TIMES      = 30
BUILD_MAX_CATEGORIES = 30
BUILD_MAX_POSITIONS  = 200


def _build_labels(cat: dict, idx: int) -> tuple:
    """Return (prefix, labels) for one builder category.

    A category is either *numbered* — labels ``{label} 1 … {label} N``, N taken from the
    largest count in its grid — or *named*, in which case the supplied names are the labels
    in list order and a cell count of N means "the first N names". Either way position keys
    are ``{prefix}{n}``, which keeps them sorting correctly under the
    ``LENGTH(pos_key), pos_key`` ordering used everywhere positions are displayed.

    Args:
        cat: One entry from the config's ``categories`` list.
        idx: Its index, quoted in error messages.

    Returns:
        tuple of (prefix, list of display labels, 1-indexed).

    Raises:
        HTTPException 400: Blank name, bad prefix, empty name list, or a position count
            outside 1…BUILD_MAX_POSITIONS.
    """
    name = str(cat.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail=f"Category {idx + 1} has no name")

    prefix = str(cat.get("prefix") or "").strip().lower() or re.sub(r'[^a-z0-9]', '', name.lower())
    _validate_key(prefix or "pos", f"category {name!r} prefix")

    counts   = cat.get("counts") or {}
    max_used = max((int(v) for v in counts.values()), default=0)

    names = cat.get("names")
    if names:
        labels = [str(n).strip() for n in names]
        if not all(labels):
            raise HTTPException(status_code=400, detail=f"Category {name!r} has a blank name in its list")
    else:
        label  = str(cat.get("label") or "").strip() or name
        labels = [f"{label} {n}" for n in range(1, max_used + 1)]

    if len(labels) > BUILD_MAX_POSITIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Category {name!r} defines {len(labels)} positions (max {BUILD_MAX_POSITIONS})",
        )
    return prefix, labels


@router.post("/api/schedule/build")
def build_schedule_structure(
    request: Request,
    config: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Create a whole schedule — days, times, categories, positions and slots — in one write.

    Backs the setup wizard's schedule builder. The counts grid is what the flat
    ``bulk_add_slots`` cross-product cannot express: a category may need ten registration
    desks at one timeslot and four at the next, so each (day, time) cell carries its own
    count and gets slots for the first N positions of that category.

    Everything is inserted with ON CONFLICT DO NOTHING under a single commit, so a re-run
    adds what is missing and leaves existing rows — including any volunteer assignments —
    untouched.

    Args:
        request: Starlette Request; used for audit logging.
        config: JSON object as a form field (the API takes no JSON request bodies) with keys:

            ``days``: list of ``{"key", "title", "date"}`` — date is ISO ``YYYY-MM-DD`` or "".
            ``times``: list of ``{"key", "title", "day_keys": [...]}`` — day_keys declares
                which days run that time. Two days running different-length shifts from the
                same start are two entries with different keys.
            ``categories``: list of ``{"name", "prefix", "label", "names", "counts", "color"}``
                in display order. ``names`` (optional) makes the category named rather than
                numbered. ``counts`` maps ``"{day_key}|{time_key}"`` to how many of that
                category's positions are scheduled in that cell; 0 or absent means none.
                ``color`` (optional) is the ``#rrggbb`` chip color; omitted, a new category
                takes the next default and an existing one keeps the color it has.

        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict with keys ``days``, ``times``, ``categories``, ``positions_created``,
        ``slots_created``.

    Raises:
        HTTPException 400: Malformed JSON, a missing or duplicate key, a key with characters
            that would break slot parsing, a count referencing an undeclared day/time pair,
            or any list exceeding its size cap.
        HTTPException 401/403: Insufficient role.
    """
    try:
        cfg = json.loads(config)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="config must be a JSON object") from None
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config must be a JSON object")

    days  = cfg.get("days")       or []
    times = cfg.get("times")      or []
    cats  = cfg.get("categories") or []
    if not days:
        raise HTTPException(status_code=400, detail="At least one day is required")
    if not times:
        raise HTTPException(status_code=400, detail="At least one time is required")
    for label, seq, cap in (("days", days, BUILD_MAX_DAYS), ("times", times, BUILD_MAX_TIMES),
                            ("categories", cats, BUILD_MAX_CATEGORIES)):
        if len(seq) > cap:
            raise HTTPException(status_code=400, detail=f"Too many {label} (max {cap})")

    # ── days ──────────────────────────────────────────────────────────────────
    day_keys = []
    for i, d in enumerate(days):
        key   = str(d.get("key") or "").strip().lower()
        title = str(d.get("title") or "").strip()
        date  = str(d.get("date") or "").strip() or None
        if not key or not title:
            raise HTTPException(status_code=400, detail=f"Day {i + 1} needs a key and a title")
        _validate_key(key, "day_key")
        if key in day_keys:
            raise HTTPException(status_code=400, detail=f"Duplicate day key {key!r}")
        day_keys.append(key)
        db.execute(
            "INSERT INTO days_table (day_key, day_str, event_date) VALUES (?, ?, ?) "
            "ON CONFLICT (day_key) DO NOTHING;",
            (key, title, date),
        )

    # ── times, and the day/time pairs they run ────────────────────────────────
    pairs = set()
    time_keys = []
    for i, t in enumerate(times):
        key   = str(t.get("key") or "").strip()
        title = str(t.get("title") or "").strip()
        if not key or not title:
            raise HTTPException(status_code=400, detail=f"Time {i + 1} needs a key and a title")
        _validate_key(key, "time_key")
        if key in time_keys:
            raise HTTPException(status_code=400, detail=f"Duplicate time key {key!r}")
        time_keys.append(key)
        db.execute(
            "INSERT INTO times_table (time_key, time_str) VALUES (?, ?) "
            "ON CONFLICT (time_key) DO NOTHING;",
            (key, title),
        )
        for dk in (t.get("day_keys") or []):
            dk = str(dk).strip().lower()
            if dk not in day_keys:
                raise HTTPException(status_code=400, detail=f"Time {key!r} names unknown day {dk!r}")
            pairs.add((dk, key))
            db.execute(
                "INSERT INTO day_times (day_key, time_key) VALUES (?, ?) ON CONFLICT DO NOTHING;",
                (dk, key),
            )

    # ── categories, positions, slots ──────────────────────────────────────────
    positions_created = 0
    slots_created     = 0

    for idx, cat in enumerate(cats):
        name  = str(cat.get("name") or "").strip()
        color = _norm_color(cat.get("color") or "", f"category {name!r} color")
        db.execute(
            "INSERT INTO categories (cat_name, sort_order, cat_color) VALUES (?, ?, ?) "
            "ON CONFLICT (cat_name) DO UPDATE SET sort_order = excluded.sort_order, "
            "cat_color = COALESCE(?, categories.cat_color);",
            (name, idx, color or _DEFAULT_CAT_COLORS[idx % len(_DEFAULT_CAT_COLORS)], color),
        )

        prefix, labels = _build_labels(cat, idx)
        pos_keys = [f"{prefix}{n}" for n in range(1, len(labels) + 1)]
        for pos_key, pos_str in zip(pos_keys, labels):
            cur = db.execute(
                "INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES (?, ?, ?) "
                "ON CONFLICT (pos_key) DO NOTHING;",
                (pos_key, name, pos_str),
            )
            positions_created += cur.rowcount

        for cell, raw in (cat.get("counts") or {}).items():
            try:
                count = int(raw)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"Category {name!r} has a non-numeric count at {cell!r}") from None
            if count <= 0:
                continue
            dk, _, tk = str(cell).partition("|")
            if (dk, tk) not in pairs:
                raise HTTPException(
                    status_code=400,
                    detail=f"Category {name!r} schedules {cell!r}, which is not a declared day/time",
                )
            # A cell may ask for more than the category defines (a named list is finite);
            # take what exists rather than failing the whole build.
            slots_created += _fill_slots(db, [(dk, tk)], pos_keys[:count])

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Built schedule: {len(day_keys)} day(s), {len(time_keys)} time(s), "
          f"{len(cats)} categor(ies), {positions_created} position(s), {slots_created} slot(s)")
    return {
        "days":              len(day_keys),
        "times":             len(time_keys),
        "categories":        len(cats),
        "positions_created": positions_created,
        "slots_created":     slots_created,
    }


# ── live counts grid ──────────────────────────────────────────────────────────

GRID_MAX_PER_CELL = 200


def _infer_prefix(pos_keys: list, cat_name: str) -> str:
    """Return the key prefix new positions in a category should use.

    Inferred from the keys already in the category rather than derived from its name:
    a category called "Security" is conventionally keyed ``sec1``…``sec9``, and deriving
    ``security`` from the name would scatter ``security10`` through an existing run.
    Falls back to the name when the existing keys disagree or there are none.

    Args:
        pos_keys: Existing position keys in the category, any order.
        cat_name: Category display name, used for the fallback.

    Returns:
        A key prefix matching POS_KEY_RE.
    """
    stems = {re.sub(r'\d+$', '', k) for k in pos_keys}
    stems.discard('')
    if len(stems) == 1:
        stem = stems.pop()
        if POS_KEY_RE.match(stem):
            return stem
    return re.sub(r'[^a-z0-9]', '', cat_name.lower()) or "pos"


def _cat_positions(db: PgClient, cat_name: str) -> list:
    """Return a category's positions in display order (the house LENGTH, key ordering)."""
    return db.execute(
        "SELECT pos_key, pos_str FROM positions WHERE pos_cat = ? "
        "ORDER BY LENGTH(pos_key), pos_key;",
        (cat_name,),
    ).fetchall()


def _reconcile_cell(ordered: list, here: dict, want: int) -> tuple:
    """Decide what one grid cell needs: which slots to add, which to remove.

    Positions are matched by *how many*, not by which. Filling a cell takes the
    lowest-numbered positions not already in it; emptying one drops from the highest-numbered
    end. Both leave the slots already in the middle alone, so raising and then lowering a
    count does not churn through volunteers' assignments.

    A slot with a volunteer is never removed. If skipping those means the cell cannot shrink
    all the way, the shortfall comes back as ``short`` for the caller to report.

    Args:
        ordered: Every position key in the category, in display order.
        here: The category's slots already in this cell, ``{pos_key: vol_id or None}``.
        want: How many the grid asks for.

    Returns:
        tuple of (keys to add, keys to remove, shortfall that could not be removed).
    """
    have = len(here)
    if want >= have:
        return [k for k in ordered if k not in here][:want - have], [], 0

    surplus   = have - want
    to_remove = []
    for key in reversed([k for k in ordered if k in here]):
        if surplus == 0:
            break
        if here[key]:          # assigned — leave it, and keep looking further down
            continue
        to_remove.append(key)
        surplus -= 1
    return [], to_remove, surplus


def _new_pos_key(prefix: str, n: int, taken: set) -> tuple:
    """Return the first free ``{prefix}{n}`` key at or after n, with the n it landed on."""
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}", n


@router.post("/api/schedule/apply_grid")
def apply_grid(
    request: Request,
    config: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Reconcile the live schedule against a counts grid: add what is missing, remove the surplus.

    This is the editor's counterpart to ``/api/schedule/build``. Build is additive and runs
    against an empty database; this one diffs against a schedule that already has volunteers
    in it, so it can also take slots away.

    **Assignments are never discarded.** Lowering a count removes only unassigned slots,
    highest-numbered position first; a slot with a volunteer is left alone and reported in
    ``blocked``. This matches ``set_day_time`` and ``remove_slot``, which refuse rather than
    drop an assignment.

    Positions are matched to counts by *how many*, not by which: raising a cell from 2 to 4
    schedules the two lowest-numbered positions of that category not already there, rather
    than forcing the cell to hold exactly positions 1-4. That leaves existing assignments
    where they are instead of churning them.

    Display names are only written for positions this call creates, except in a *named*
    category, where the supplied list is the point and is applied to positions 1…len(names).
    A numbered category never renames a position that already exists, so a hand-edited
    ``pos_str`` survives.

    Args:
        request: Starlette Request; used for audit logging.
        config: JSON object as a form field (the API takes no JSON request bodies) with keys:

            ``cells``: list of ``"{day_key}|{time_key}"`` — every cell the grid displayed.
                Reconciliation is confined to these, so a stale grid cannot clear a cell it
                never showed. Each must be a pair declared in ``day_times``.
            ``categories``: list of ``{"name", "mode", "names", "counts"}``. ``mode`` is
                ``"numbered"`` (default) or ``"named"``; ``names`` is the display-name list
                for a named category. ``counts`` maps a cell key to how many of that
                category's positions belong there; a listed cell missing from ``counts``
                means zero.

        s_data: Injected session dict; requires organizer role.
        db: Injected PgClient from get_db().

    Returns:
        dict with ``ok``, ``positions_created``, ``positions_deleted``, ``slots_created``,
        ``slots_removed``, and ``blocked`` — a list of
        ``{"category", "cell", "kept"}`` for cells that could not shrink all the way.

    Raises:
        HTTPException 400: Malformed JSON, an unknown category, a cell that is not a declared
            day/time pair, a non-numeric or out-of-range count.
        HTTPException 401/403: Insufficient role.
    """
    try:
        cfg = json.loads(config)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=400, detail="config must be a JSON object") from None
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=400, detail="config must be a JSON object")

    cells = cfg.get("cells") or []
    cats  = cfg.get("categories") or []
    if len(cats) > BUILD_MAX_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Too many categories (max {BUILD_MAX_CATEGORIES})")

    # Confine every write to cells the grid actually displayed, and only real day/time pairs.
    pairs = set(_day_time_pairs(db))
    grid_cells = []
    for cell in cells:
        dk, _, tk = str(cell).partition("|")
        if (dk, tk) not in pairs:
            raise HTTPException(status_code=400, detail=f"{cell!r} is not a declared day/time")
        grid_cells.append((dk, tk))
    if not grid_cells:
        raise HTTPException(status_code=400, detail="No cells to apply")

    known_cats = {r["cat_name"] for r in db.execute("SELECT cat_name FROM categories;").fetchall()}
    all_keys   = set(_all_keys(db, "positions", "pos_key"))

    positions_created = positions_deleted = 0
    slots_created     = slots_removed     = 0
    blocked           = []

    for cat in cats:
        name = str(cat.get("name") or "").strip()
        if name not in known_cats:
            raise HTTPException(status_code=400, detail=f"Unknown category {name!r}")

        raw_counts = cat.get("counts") or {}
        counts     = {}
        for (dk, tk) in grid_cells:
            try:
                n = int(raw_counts.get(f"{dk}|{tk}", 0) or 0)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400,
                    detail=f"Category {name!r} has a non-numeric count at {dk}|{tk}") from None
            if n < 0 or n > GRID_MAX_PER_CELL:
                raise HTTPException(
                    status_code=400,
                    detail=f"Category {name!r} count at {dk}|{tk} must be 0…{GRID_MAX_PER_CELL}")
            counts[(dk, tk)] = n

        rows    = _cat_positions(db, name)
        ordered = [r["pos_key"] for r in rows]
        labels  = {r["pos_key"]: r["pos_str"] for r in rows}
        prefix  = _infer_prefix(ordered, name)

        named = str(cat.get("mode") or "numbered") == "named"
        names = [str(n).strip() for n in (cat.get("names") or []) if str(n).strip()] if named else []
        if named and not names:
            raise HTTPException(status_code=400, detail=f"Category {name!r} is named but has no names")

        # How many positions this category must be able to offer.
        needed = len(names) if named else max(counts.values(), default=0)
        if needed > BUILD_MAX_POSITIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Category {name!r} needs {needed} positions (max {BUILD_MAX_POSITIONS})")

        # ── grow the position list to fit ────────────────────────────────────
        next_n = len(ordered) + 1
        while len(ordered) < needed:
            key, next_n = _new_pos_key(prefix, next_n, all_keys)
            label = names[len(ordered)] if named else f"{name} {len(ordered) + 1}"
            db.execute(
                "INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES (?, ?, ?) "
                "ON CONFLICT (pos_key) DO NOTHING;",
                (key, name, label),
            )
            all_keys.add(key)
            ordered.append(key)
            labels[key] = label
            positions_created += 1
            next_n += 1

        # A named list is the point of naming — apply it. A numbered category leaves
        # existing display names alone, so a hand-edited one is not overwritten.
        if named:
            for i, label in enumerate(names):
                key = ordered[i]
                if labels.get(key) != label:
                    db.execute("UPDATE positions SET pos_str = ? WHERE pos_key = ?;", (label, key))

        # ── reconcile each cell ──────────────────────────────────────────────
        placed = {}
        for r in db.execute(
            "SELECT s.day_key, s.time_key, s.pos_key, s.vol_id FROM schedule s "
            "JOIN positions p ON s.pos_key = p.pos_key WHERE p.pos_cat = ?;",
            (name,),
        ).fetchall():
            placed.setdefault((r["day_key"], r["time_key"]), {})[r["pos_key"]] = r["vol_id"]

        for (dk, tk) in grid_cells:
            here = placed.get((dk, tk), {})
            to_add, to_remove, short = _reconcile_cell(ordered, here, counts[(dk, tk)])

            for key in to_add:
                db.execute(
                    "INSERT INTO schedule (day_key, time_key, pos_key) VALUES (?, ?, ?) "
                    "ON CONFLICT (day_key, time_key, pos_key) DO NOTHING;",
                    (dk, tk, key),
                )
                slots_created += 1

            for key in to_remove:
                db.execute(
                    "DELETE FROM schedule WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
                    (dk, tk, key),
                )
                db.execute(
                    "DELETE FROM schedule_notes WHERE day_key = ? AND time_key = ? AND pos_key = ?;",
                    (dk, tk, key),
                )
                slots_removed += 1

            if short:
                blocked.append({"category": name, "cell": f"{dk}|{tk}", "kept": short})

        # ── tidy positions the grid no longer needs ──────────────────────────
        # Only ones left with no slots anywhere: invisible either way, and recreating one
        # is a keystroke. Anything still scheduled, or assigned, is left alone.
        for key in ordered[needed:]:
            if db.execute("SELECT 1 FROM schedule WHERE pos_key = ?;", (key,)).fetchone():
                continue
            db.execute("DELETE FROM schedule_notes WHERE pos_key = ?;", (key,))
            db.execute("DELETE FROM positions WHERE pos_key = ?;", (key,))
            positions_deleted += 1

    db.commit()
    broadcast("schedule")
    audit(request, request.session.get("session_id"),
          f"Applied grid: +{slots_created}/-{slots_removed} slot(s), "
          f"+{positions_created}/-{positions_deleted} position(s), {len(blocked)} blocked")
    return {
        "ok":                True,
        "positions_created": positions_created,
        "positions_deleted": positions_deleted,
        "slots_created":     slots_created,
        "slots_removed":     slots_removed,
        "blocked":           blocked,
    }
