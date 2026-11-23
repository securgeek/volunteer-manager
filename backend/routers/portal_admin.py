"""Organizer-facing portal endpoints: sync health, and a manual sync trigger.

Everything here reads mirrored data from our own database rather than calling the portal, so
the organizer tool keeps working when the portal is unreachable. The one exception is
sync_now, which is by definition a request to go and talk to it.

Paths are under /api/admin/portal/, never /api/portal/. nginx (and the Vite dev proxy) send
every path beginning with the text "/api/portal" to the portal service, so a backend route
there is unreachable from the browser — it 404s at the portal instead. That is how this
dialog came to say "Not configured" while sync was healthy.
"""
import json
import math
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request

import portal_sync
from audit import audit
from db import PgClient, RoleChecker, get_app_kv, get_db
from routers.invites import clean_email, token_hash

# A badge number given at approval. Not `p-…`: that prefix is the portal's namespace for
# volunteers who signed up themselves, so the two kinds of id can never collide.
_BADGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,31}$")

router = APIRouter()


@router.get("/api/admin/portal/status")
def portal_status(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Sync health, cursors, and whether our keys agree with the portal's.

    A silently dead sync is the failure mode that would hurt most — a volunteer signs up, an
    organizer never sees them, and nobody finds out until the event. So this is built to be
    surfaced in the UI, not just logged.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    rows = db.execute(
        "SELECT stream, last_rev, last_ok_at, last_error, last_error_at "
        "FROM portal_sync_state ORDER BY stream;"
    ).fetchall()
    counts = db.execute(
        "SELECT status, COUNT(*) AS n FROM volunteers GROUP BY status;"
    ).fetchall()
    return {
        "configured": portal_sync.configured(),
        "missing": portal_sync.missing_settings(),
        "base_url": portal_sync.PORTAL_SYNC_URL or None,
        "interval_sec": portal_sync.SYNC_INTERVAL,
        "streams": [dict(r) for r in rows],
        "volunteer_counts": {r["status"]: r["n"] for r in counts},
        "last_run": portal_sync.last_status(),
        "our_hmac_key_fp": portal_sync.hmac_key_fp(),
        # What the Signup open switch needs to know, and the Pending Volunteers badge.
        "signup_open": get_app_kv(db, "portal_signup_open") == "1",
        "has_pubkey": bool(get_app_kv(db, "email_pubkey")),
        "open_timeslots": sum(1 for n in portal_sync.compute_remaining(db).values() if n > 0),
        "overage_pct": portal_sync.overage_pct(db),
        "pending_count": db.execute(
            "SELECT COUNT(*) AS n FROM volunteers v "
            "WHERE v.status = 'pending' AND v.is_live;").fetchone()["n"],
    }


@router.post("/api/admin/portal/sync_now")
async def sync_now(
    request: Request,
    s_data: dict = Depends(RoleChecker("organizer")),
):
    """Run one reconcile immediately rather than waiting for the next tick.

    Args:
        request: The incoming request, for the session id and the audit log.
        s_data: The organizer's session.
    """
    result = await portal_sync.sync_now()
    audit("portal.sync_now", request, request.session.get("session_id"), "Triggered portal sync")
    return result


@router.get("/api/admin/portal/pending")
def pending_signups(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Volunteers the portal created who are waiting on a decision, with their timeslot picks
    and any existing volunteers their name resembles.

    Read from the mirror, so this works whether or not the portal is currently reachable.
    Answers come back raw; their labels are the portal's (GET /api/portal/signup/fields).

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    rows = db.execute(
        "SELECT vol_id, name, status, time_pref, add_time, add_any, answers, "
        "possible_duplicate_of, created_at FROM volunteers v "
        "WHERE v.status = 'pending' AND v.is_live ORDER BY created_at, vol_id;"
    ).fetchall()
    out = []
    for r in rows:
        rec = dict(r)
        rec["created_at"] = r["created_at"].isoformat() + "Z" if r["created_at"] else None
        rec["picks"] = picks_for(db, r["vol_id"])
        dupes = r["possible_duplicate_of"] or []
        if isinstance(dupes, str):
            dupes = json.loads(dupes)
        rec["possible_duplicates"] = [dict(d) for d in db.execute(
            "SELECT vol_id, name, status, time_pref FROM volunteers WHERE vol_id = ANY(?) "
            "ORDER BY vol_id;", (list(dupes),)).fetchall()] if dupes else []
        out.append(rec)
    return {"pending": out}


def _pending(db: PgClient, vol_id: str) -> dict:
    row = db.execute("SELECT * FROM volunteers WHERE vol_id = ?;", (vol_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Volunteer not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Already {row['status']}")
    if row["inactive_at"] or row["cancelled_at"]:
        raise HTTPException(status_code=409, detail="Cancelled or inactive; nothing to decide")
    return row


@router.post("/api/admin/portal/approve")
def approve_signup(
    request: Request,
    vol_id: str = Form(...),
    new_vol_id: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Approve a pending signup, optionally giving them a badge number as their vol_id.

    Approval doesn't schedule anyone; the picks stay preferences and assignment is the
    usual organizer act. The change reaches the portal on the next sync (portal_dirty), and a
    re-key renames the portal's row and the volunteer's sessions rather than replacing them.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_id: The pending signup's portal id.
        new_vol_id: A badge number to become their vol_id. Blank keeps the portal id.
        s_data: The organizer's session.
        db: The request's database connection.

    Raises:
        HTTPException 400: Badge number malformed.
        HTTPException 404/409: Not found, not pending, badge taken, or the volunteer is
            already referenced somewhere a re-key can't follow.
    """
    row = _pending(db, vol_id)
    new_vol_id = new_vol_id.strip()
    final = vol_id
    if new_vol_id and new_vol_id != vol_id:
        if not _BADGE.match(new_vol_id) or new_vol_id.lower().startswith("p-"):
            raise HTTPException(status_code=400, detail="Badge number: letters, digits and "
                                "dashes, up to 32, not starting with p-")
        if db.execute("SELECT 1 FROM volunteers WHERE vol_id = ?;", (new_vol_id,)).fetchone():
            raise HTTPException(status_code=409, detail=f"{new_vol_id} is already in use")
        # Pending volunteers can't be assigned or noted (pickers show active only), so their
        # picks are all a re-key has to carry. Anything else means someone got there anyway.
        held = db.execute(
            "SELECT EXISTS (SELECT 1 FROM schedule WHERE vol_id = ?) AS schedule, "
            "EXISTS (SELECT 1 FROM vol_notes WHERE vol_id = ?) AS vol_notes, "
            "EXISTS (SELECT 1 FROM radio_checkouts WHERE vol_id = ?) AS radio_checkouts;",
            (vol_id, vol_id, vol_id)).fetchone()
        for table in ("schedule", "vol_notes", "radio_checkouts"):
            if held[table]:
                raise HTTPException(status_code=409, detail=f"Can't re-key: {vol_id} already "
                                    f"has entries in {table}. Approve without a badge number.")
        # Free the unique email_hmac before the copy takes it.
        db.execute("UPDATE volunteers SET email_hmac = NULL WHERE vol_id = ?;", (vol_id,))
        db.execute(
            "INSERT INTO volunteers (vol_id, name, time_pref, add_time, archived, email_enc, "
            "status, email_hmac, email_key_fp, add_any, answers, possible_duplicate_of, "
            "created_at, portal_dirty, portal_rekey_from) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, TRUE, ?);",
            (new_vol_id, row["name"], row["time_pref"], row["add_time"], row["archived"],
             row["email_enc"], row["email_hmac"], row["email_key_fp"], row["add_any"],
             portal_sync._json_text(row["answers"]),
             portal_sync._json_text(row["possible_duplicate_of"]), row["created_at"], vol_id),
        )
        db.execute("UPDATE vol_picks SET vol_id = ? WHERE vol_id = ?;", (new_vol_id, vol_id))
        db.execute("DELETE FROM volunteers WHERE vol_id = ?;", (vol_id,))
        final = new_vol_id
    else:
        db.execute("UPDATE volunteers SET status = 'active', portal_dirty = TRUE "
                   "WHERE vol_id = ?;", (vol_id,))
    db.commit()
    audit("portal.signup.approve", request, request.session.get("session_id"),
          f"Approved signup {vol_id}" + (f" as {final}" if final != vol_id else ""))
    portal_sync.request_push()
    return {"ok": True, "vol_id": final}


@router.post("/api/admin/portal/decline")
def decline_signup(
    request: Request,
    vol_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Make a pending signup inactive ("Make inactive" in Pending Volunteers). Their picks stop
    counting against capacity; the row stays, so the address stays registered and can't simply
    sign up again. Reversible with set_inactive.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_id: The pending signup.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    _pending(db, vol_id)
    db.execute("UPDATE volunteers SET inactive_at = NOW() AT TIME ZONE 'UTC', "
               "portal_dirty = TRUE WHERE vol_id = ?;", (vol_id,))
    db.commit()
    audit("portal.signup.decline", request, request.session.get("session_id"),
          f"Made signup {vol_id} inactive")
    portal_sync.request_push()
    return {"ok": True}


@router.post("/api/admin/portal/settings")
def set_portal_settings(
    request: Request,
    signup_open: Optional[str] = Form(default=None),
    overage_pct: Optional[str] = Form(default=None),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Open or close volunteer signup, and set the overbooking allowance. Either or both.

    Both reach the portal on the next sync: signup_open as a setting, the overage through the
    `remaining` counts in the schedule snapshot (portal_sync.compute_remaining).

    Args:
        request: The incoming request, for the session id and the audit log.
        signup_open: `1` to open signup, `0` to close it. Leave it out to keep it as it is.
        overage_pct: The overbooking allowance, a whole percentage. Leave it out to keep it.
        s_data: The organizer's session.
        db: The request's database connection.

    Raises:
        HTTPException 400: overage_pct not a whole number from 0 to OVERAGE_MAX.
        HTTPException 409: Opening with no email public key — the portal couldn't store an
            address anyone could read, and would stay closed regardless.
    """
    def put(key, val):
        db.execute("INSERT INTO app_kv (app_key, val) VALUES (?, ?) "
                   "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;", (key, val))

    sid = request.session.get("session_id")
    if overage_pct is not None:
        try:
            pct = int(overage_pct.strip())
        except ValueError:
            pct = -1
        if not 0 <= pct <= portal_sync.OVERAGE_MAX:
            raise HTTPException(status_code=400, detail="Overage must be a whole number from 0 "
                                f"to {portal_sync.OVERAGE_MAX}")
        put("portal_overage_pct", str(pct))
        audit("portal.settings", request, sid, f"Set signup overage to {pct}%")
    if signup_open is not None:
        want = "1" if signup_open == "1" else "0"
        if want == "1" and not get_app_kv(db, "email_pubkey"):
            raise HTTPException(status_code=409, detail="Upload an email public key (Admin → "
                                "Email Key) before opening signup.")
        put("portal_signup_open", want)
        audit("portal.settings", request, sid,
              "Opened volunteer signup" if want == "1" else "Closed volunteer signup")
    db.commit()
    portal_sync.request_push()
    return {"ok": True, "signup_open": get_app_kv(db, "portal_signup_open") == "1",
            "overage_pct": portal_sync.overage_pct(db)}


@router.get("/api/admin/portal/demand")
def shift_demand(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """The organizer's honest view, per timeslot: what exists, who wants it, what's offered.

    `offered` and `remaining` include the overage, which is what volunteers see; `slots` and
    `open` don't. `ratio` is volunteers who picked the timeslot against its slots: above 1.0
    the overage is being used, and not every pick can be honoured unless people drop out.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    pct = portal_sync.overage_pct(db)
    remaining = portal_sync.compute_remaining(db, pct)
    picks = {(r["day_key"], r["time_key"]): r["n"] for r in db.execute(
        "SELECT vp.day_key, vp.time_key, COUNT(*) AS n FROM vol_picks vp "
        "JOIN volunteers v ON v.vol_id = vp.vol_id "
        "WHERE vp.kind = 'pref' AND v.is_counted "
        "GROUP BY vp.day_key, vp.time_key;").fetchall()}
    out = []
    for r in db.execute(
        "SELECT dt.day_key, d.day_str, dt.time_key, t.time_str, COUNT(s.pos_key) AS slots, "
        "SUM(CASE WHEN s.pos_key IS NOT NULL AND s.vol_id IS NULL THEN 1 ELSE 0 END) AS open "
        "FROM day_times dt JOIN days_table d ON d.day_key = dt.day_key "
        "JOIN times_table t ON t.time_key = dt.time_key "
        "LEFT JOIN schedule s ON s.day_key = dt.day_key AND s.time_key = dt.time_key "
        "GROUP BY dt.day_key, d.day_str, dt.time_key, t.time_str "
        "ORDER BY dt.day_key, dt.time_key;"
    ).fetchall():
        cell = (r["day_key"], r["time_key"])
        slots, n = r["slots"], picks.get(cell, 0)
        out.append({
            "day_key": r["day_key"], "day_str": r["day_str"],
            "time_key": r["time_key"], "time_str": r["time_str"],
            "slots": slots, "open": r["open"] or 0,
            "offered": (slots * (100 + pct)) // 100, "picks": n,
            "remaining": remaining.get(cell, 0),
            "ratio": round(n / slots, 2) if slots else None,
        })
    return {"overage_pct": pct, "timeslots": out}


def picks_for(db: PgClient, vol_id: str) -> list:
    """A volunteer's picks with display labels, prefs by rank then extras.

    Args:
        db: The database connection.
        vol_id: The volunteer.
    """
    return [dict(p) for p in db.execute(
        "SELECT vp.day_key, vp.time_key, vp.kind, vp.rank, "
        "d.day_str || ' ' || t.time_str AS label FROM vol_picks vp "
        "LEFT JOIN days_table d ON d.day_key = vp.day_key "
        "LEFT JOIN times_table t ON t.time_key = vp.time_key "
        "WHERE vp.vol_id = ? ORDER BY vp.kind DESC, vp.rank, vp.day_key, vp.time_key;",
        (vol_id,)).fetchall()]


@router.get("/api/admin/shift_flags")
def shift_flags(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Assignments whose volunteer has since dropped that timeslot in the portal.

    Only flags that still hold: not kept, the volunteer is still assigned there, and the
    timeslot isn't back among their picks. So unassigning them (clear_timeslot) or the
    volunteer re-picking it clears a flag with nothing to update. Backs the banner in Admin.vue.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict with ``flags`` — each with ``vol_id``, ``name``, ``day_key``, ``day_str``,
        ``time_key``, ``time_str``, ``flagged_at``, and ``slots`` (``key``, ``pos_str``) for the
        assignments concerned — and ``replace``: slots held by a volunteer who has cancelled
        or been made inactive, each with ``key``, ``day_str``, ``time_str``, ``pos_str``,
        ``vol_id``, ``name`` and ``reason``.
    """
    rows = db.execute(
        "SELECT f.vol_id, v.name, f.day_key, d.day_str, f.time_key, t.time_str, f.flagged_at, "
        "s.pos_key, p.pos_str FROM shift_flags f "
        "JOIN volunteers v ON v.vol_id = f.vol_id "
        "JOIN schedule s ON s.vol_id = f.vol_id AND s.day_key = f.day_key "
        "                AND s.time_key = f.time_key "
        "LEFT JOIN days_table d ON d.day_key = f.day_key "
        "LEFT JOIN times_table t ON t.time_key = f.time_key "
        "LEFT JOIN positions p ON p.pos_key = s.pos_key "
        "WHERE f.resolved_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM vol_picks vp WHERE vp.vol_id = f.vol_id "
        "  AND vp.day_key = f.day_key AND vp.time_key = f.time_key) "
        "ORDER BY f.flagged_at, f.vol_id, f.day_key, f.time_key, s.pos_key;"
    ).fetchall()
    flags: dict = {}
    for r in rows:
        key = (r["vol_id"], r["day_key"], r["time_key"])
        if key not in flags:
            flags[key] = {k: r[k] for k in ("vol_id", "name", "day_key", "day_str",
                                            "time_key", "time_str")}
            flags[key]["flagged_at"] = r["flagged_at"].isoformat() + "Z"
            flags[key]["slots"] = []
        flags[key]["slots"].append({"key": f"{r['day_key']}_{r['time_key']}_{r['pos_key']}",
                                    "pos_str": r["pos_str"] or r["pos_key"]})
    # Slots held by someone who has since cancelled or been made inactive. Their assignment
    # stays — an organizer replaces them by assigning someone else to the slot — and the entry
    # goes as soon as that happens, or they rejoin or are reinstated.
    replace = [{
        "key": f"{r['day_key']}_{r['time_key']}_{r['pos_key']}", "day_str": r["day_str"],
        "time_str": r["time_str"], "pos_str": r["pos_str"] or r["pos_key"],
        "vol_id": r["vol_id"], "name": r["name"],
        "reason": "cancelled" if r["cancelled_at"] else "inactive",
    } for r in db.execute(
        "SELECT s.day_key, s.time_key, s.pos_key, d.day_str, t.time_str, p.pos_str, "
        "v.vol_id, v.name, v.cancelled_at FROM schedule s "
        "JOIN volunteers v ON v.vol_id = s.vol_id "
        "LEFT JOIN days_table d ON d.day_key = s.day_key "
        "LEFT JOIN times_table t ON t.time_key = s.time_key "
        "LEFT JOIN positions p ON p.pos_key = s.pos_key "
        "WHERE NOT v.is_live ORDER BY s.day_key, s.time_key, s.pos_key;"
    ).fetchall()]
    return {"flags": list(flags.values()), "replace": replace}


@router.post("/api/admin/shift_flags/keep")
def keep_flagged_shift(
    request: Request,
    vol_id: str = Form(...),
    day_key: str = Form(...),
    time_key: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Dismiss a flag and leave the assignment as it is — say, after confirming with the
    volunteer. Dropping the timeslot again later raises it afresh.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_id: The flagged volunteer.
        day_key: The flagged timeslot's day.
        time_key: The flagged timeslot's time.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    cur = db.execute(
        "UPDATE shift_flags SET resolved_at = NOW() AT TIME ZONE 'UTC', resolved_by = ? "
        "WHERE vol_id = ? AND day_key = ? AND time_key = ? AND resolved_at IS NULL;",
        (s_data["username"], vol_id, day_key, time_key))
    db.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="No open flag for that shift")
    audit("portal.shift_flag.keep", request, request.session.get("session_id"),
          f"Kept {vol_id} on {day_key} {time_key} despite their dropping it")
    return {"ok": True}



@router.post("/api/admin/volunteers/set_inactive")
def set_inactive(
    request: Request,
    vol_id: str = Form(...),
    inactive: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Make any volunteer inactive (`inactive=1`), or reinstate them (`inactive=0`).

    Approval is untouched either way: reinstating an approved volunteer leaves them approved.
    Their assignments stay; while inactive they're listed as slots needing a replacement.
    A cancelled volunteer's cancellation is theirs to undo (rejoin), not changed here.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_id: The volunteer.
        inactive: `1` to make them inactive, `0` to reinstate them.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    if inactive not in ("0", "1"):
        raise HTTPException(status_code=400, detail="inactive must be 0 or 1")
    row = db.execute("SELECT name FROM volunteers WHERE vol_id = ?;", (vol_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Volunteer not found")
    db.execute(
        "UPDATE volunteers SET inactive_at = CASE WHEN ? "
        "THEN COALESCE(inactive_at, NOW() AT TIME ZONE 'UTC') ELSE NULL END, "
        "portal_dirty = TRUE WHERE vol_id = ?;", (inactive == "1", vol_id))
    db.commit()
    audit("volunteer.set_inactive", request, request.session.get("session_id"),
          f"{'Made inactive' if inactive == '1' else 'Reinstated'} {row['name']!r} ({vol_id})")
    portal_sync.request_push()
    # Open browsers: the schedule's replacement markers and the banner change.
    from routers.events import broadcast
    broadcast("volunteers")
    return {"ok": True, "inactive": inactive == "1"}


PURGE_MAX = 500


@router.post("/api/admin/volunteers/purge")
async def purge_volunteers(
    request: Request,
    vol_ids: list[str] = Form(...),
    confirm: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete inactive volunteers outright, here and on the portal (`portal_sync.purge_volunteers`).

    For clearing test volunteers so their addresses can sign up again from scratch. Only
    volunteers already made inactive can go, as a guard for real ones. `confirm` must be the
    vol_id when there's one, or how many there are: the dialog has the organizer type it.

    Gone with them: picks, notes (check-ins and ratings too) and shift flags; their schedule
    slots become empty; radio history keeps the name. If the portal can't be reached, nothing
    is deleted (502). Can't be undone.

    Args:
        request: The incoming request, for the session id and the audit log.
        vol_ids: The volunteers to delete, up to `PURGE_MAX`. Repeat the field for each.
        confirm: What the organizer typed: the vol_id, or the count.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    ids = sorted({v.strip() for v in vol_ids if v.strip()})
    if not ids or len(ids) > PURGE_MAX:
        raise HTTPException(status_code=400, detail=f"Give 1 to {PURGE_MAX} volunteers")
    if confirm.strip() != (ids[0] if len(ids) == 1 else str(len(ids))):
        raise HTTPException(status_code=400, detail="The confirmation doesn't match")
    rows = db.execute("SELECT vol_id, name, inactive_at FROM volunteers "
                      "WHERE vol_id = ANY(?::text[]);", (ids,)).fetchall()
    found = {r["vol_id"]: r for r in rows}
    if missing := [v for v in ids if v not in found]:
        raise HTTPException(status_code=404, detail=f"Not found: {', '.join(missing)}")
    if active := [v for v in ids if not found[v]["inactive_at"]]:
        raise HTTPException(status_code=409, detail="Make them inactive first: "
                            f"{', '.join(found[v]['name'] for v in active)}")
    db.commit()
    try:
        deleted = await portal_sync.purge_volunteers(db, ids)
    except RuntimeError as exc:
        audit("volunteer.purge.fail", request, request.session.get("session_id"),
              f"Purge of {len(ids)} volunteer(s) failed at the portal")
        raise HTTPException(status_code=502, detail="The portal couldn't be reached, so "
                            "nothing was deleted. Try again shortly.") from exc
    sid = request.session.get("session_id")
    for v in deleted:
        audit("volunteer.purge", request, sid, f"Purged volunteer {found[v]['name']!r} ({v})")
    from routers.events import broadcast
    broadcast("volunteers")
    broadcast("schedule")
    return {"ok": True, "purged": deleted}


# ── early-access invites ──────────────────────────────────────────────────────
# A single-use link that lets one person sign up while the call for volunteers is closed.
# Only the token's hash is stored; the link is shown once. portal_sync pushes the list to the
# portal and marks an invite used when the volunteers pull brings back its invite_id.

INVITE_LABEL_MAX = 80
INVITE_DAYS_MAX = 90


def _invite_state(row: dict) -> str:
    if row["revoked_at"]:
        return "revoked"
    if row["used_at"]:
        return "used"
    if row["expired"]:
        return "expired"
    return "unused"


def _ts(ts) -> Optional[str]:
    return ts.isoformat() + "Z" if ts else None


@router.get("/api/admin/portal/invites")
def list_signup_invites(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Every early-access invite, newest first, with its state. Never the link: it isn't kept.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    rows = db.execute(
        "SELECT invite_id, label, created_by, created_at, expires_at, revoked_at, used_at, "
        "used_by, emailed_at, expires_at <= NOW() AT TIME ZONE 'UTC' AS expired "
        "FROM signup_invites ORDER BY created_at DESC;").fetchall()
    return [{"invite_id": r["invite_id"], "label": r["label"], "created_by": r["created_by"],
             "created_at": _ts(r["created_at"]), "expires_at": _ts(r["expires_at"]),
             "used_at": _ts(r["used_at"]), "used_by": r["used_by"],
             "emailed_at": _ts(r["emailed_at"]), "state": _invite_state(r)}
            for r in rows]


@router.post("/api/admin/portal/invites")
def create_signup_invite(
    request: Request,
    label: str = Form(default=""),
    days: int = Form(default=14),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Make an early-access invite and return its link, once.

    Args:
        request: Starlette Request; used for audit logging.
        label: Who it's for, for the list (e.g. "Jane, room lead"). Not shown to them.
        days: How long it works, 1 to INVITE_DAYS_MAX.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with ``invite_id``, ``link`` (shown this once; only its hash is kept) and
        ``expires_at``.

    Raises:
        HTTPException 400: Empty or overlong label, or days out of range.
        HTTPException 503: PORTAL_PUBLIC_URL is unset, so there's no address for the link.
    """
    invite_id, link, expires_at, label = _new_signup_invite(db, label, days, s_data["username"])
    db.commit()
    audit("portal.invite.create", request, request.session.get("session_id"),
          f"Created early-access invite {invite_id} ({label!r}, {days} days)")
    portal_sync.request_push()
    return {"invite_id": invite_id, "link": link, "expires_at": _ts(expires_at)}


def _new_signup_invite(db: PgClient, label: str, days: int, created_by: str) -> tuple:
    """Check the inputs and insert an invite (uncommitted). Returns (id, link, expires, label).

    Raises:
        HTTPException 400: Empty or overlong label, or days out of range.
        HTTPException 503: PORTAL_PUBLIC_URL is unset, so there's no address for the link.
    """
    label = " ".join(label.split())
    if not label or len(label) > INVITE_LABEL_MAX:
        raise HTTPException(status_code=400, detail=f"Give it a label of up to "
                            f"{INVITE_LABEL_MAX} characters, e.g. who it's for.")
    if not 1 <= days <= INVITE_DAYS_MAX:
        raise HTTPException(status_code=400, detail=f"Days must be 1 to {INVITE_DAYS_MAX}.")
    site = os.getenv("PORTAL_PUBLIC_URL", "").strip().rstrip("/")
    if not site:
        raise HTTPException(status_code=503, detail="PORTAL_PUBLIC_URL is unset, so there's "
                            "no address to put in the link")
    token = secrets.token_urlsafe(32)
    invite_id = str(uuid.uuid4())
    row = db.execute(
        "INSERT INTO signup_invites (invite_id, token_hash, label, created_by, expires_at) "
        "VALUES (?, ?, ?, ?, NOW() AT TIME ZONE 'UTC' + ? * INTERVAL '1 day') "
        "RETURNING expires_at;",
        (invite_id, token_hash(token), label, created_by, days)).fetchone()
    return invite_id, f"{site}/apply/signup?invite={token}", row["expires_at"], label


# Send invites (the admin page) mails one invite at a time, paced. The page waits between
# sends; this enforces it for everyone at once, so two organizers can't double the rate.
INVITE_SEND_GAP_SEC = 10


@dataclass
class _InviteSendClock:
    last: float = -INVITE_SEND_GAP_SEC


_invite_send = _InviteSendClock()


@router.post("/api/admin/portal/invites/send")
async def send_signup_invite(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    days: int = Form(default=14),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Make one early-access invite for ``name`` and email it to ``email``, through the portal.

    The address passes through to the portal's mail relay and is never stored or logged here
    or there (the portal's admin-mail path, kind ``signup_invite``). The invite is pushed to
    the portal before the mail goes, so the link works as soon as it arrives. If the mail
    fails, the invite is deleted: nobody is left with an invite nobody received.

    Args:
        request: Starlette Request; used for audit logging.
        name: Who it's for: the invite's label, and their first name in the greeting.
        email: Where to send it. Not stored.
        days: How long the invite works, 1 to INVITE_DAYS_MAX.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with ``invite_id``, ``label`` and ``expires_at``. Never the link.

    Raises:
        HTTPException 400: Bad name, address or days.
        HTTPException 429: Within INVITE_SEND_GAP_SEC of the last send (``Retry-After``), or
            the portal's hourly allowance for invite mail is used up.
        HTTPException 502: The portal or the mail relay couldn't send it.
        HTTPException 503: PORTAL_PUBLIC_URL is unset.
    """
    address = clean_email(email)
    if not address:
        raise HTTPException(status_code=400, detail="An email address is required")
    wait = INVITE_SEND_GAP_SEC - (time.monotonic() - _invite_send.last)
    if wait > 0:
        raise HTTPException(status_code=429, detail="One invite every "
                            f"{INVITE_SEND_GAP_SEC} seconds; try again shortly.",
                            headers={"Retry-After": str(math.ceil(wait))})
    invite_id, link, expires_at, label = _new_signup_invite(db, name, days, s_data["username"])
    db.commit()
    _invite_send.last = time.monotonic()   # no await since the check: nothing else ran between
    sid = request.session.get("session_id")
    try:
        await portal_sync.push_signup_invites(db)
        await portal_sync.send_admin_mail(
            "signup_invite", address, link, first_name=label.split()[0],
            app_title=get_app_kv(db, "app_title") or "the conference",
            expires=f"{expires_at:%B} {expires_at.day}, {expires_at.year}")
    # Deliberately broad: any failure to deliver undoes the invite, and is reported to the
    # page as a per-person result. The address never goes into the error or the log.
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        limited = " -> 429" in str(exc)
        db.execute("DELETE FROM signup_invites WHERE invite_id = ?;", (invite_id,))
        db.commit()
        portal_sync.request_push()
        audit("portal.invite.send_fail", request, sid,
              f"Early-access invite {invite_id} ({label!r}) not sent: "
              f"{'hourly limit' if limited else 'send failed'}")
        if limited:
            raise HTTPException(status_code=429, detail="The hourly limit for invite emails is "
                                "used up. Try again later.",
                                headers={"Retry-After": "60"}) from exc
        raise HTTPException(status_code=502, detail="The email couldn't be sent. Check Admin → "
                            "Volunteer Portal for sync or mail problems.") from exc
    db.execute("UPDATE signup_invites SET emailed_at = NOW() AT TIME ZONE 'UTC' "
               "WHERE invite_id = ?;", (invite_id,))
    db.commit()
    audit("portal.invite.send", request, sid,
          f"Emailed early-access invite {invite_id} ({label!r}, {days} days)")
    return {"invite_id": invite_id, "label": label, "expires_at": _ts(expires_at)}


@router.post("/api/admin/portal/invites/revoke")
def revoke_signup_invite(
    request: Request,
    invite_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Stop an unused invite working. The portal hears on the next sync (seconds, normally).

    Args:
        request: The incoming request, for the session id and the audit log.
        invite_id: The invite to revoke.
        s_data: The organizer's session.
        db: The request's database connection.

    Raises:
        HTTPException 404: No such invite, or it's already used or revoked.
    """
    cur = db.execute("UPDATE signup_invites SET revoked_at = NOW() AT TIME ZONE 'UTC' "
                     "WHERE invite_id = ? AND revoked_at IS NULL AND used_at IS NULL;",
                     (invite_id,))
    if cur.rowcount != 1:
        raise HTTPException(status_code=404, detail="No unused invite with that id")
    db.commit()
    audit("portal.invite.revoke", request, request.session.get("session_id"),
          f"Revoked early-access invite {invite_id}")
    portal_sync.request_push()
    return {"ok": True}
