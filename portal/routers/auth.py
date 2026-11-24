"""Passwordless auth: request a link, confirm it, get a session.

Two properties this file exists to preserve, both easy to lose by accident:

* **Only hashes are stored.** Login tokens and session ids are generated, handed out once,
  and kept as SHA-256. A read of portal.db must not be enough to mint a session.
* **A GET never consumes a token.** Mail scanners and link prefetchers fetch URLs, so if the
  GET burned the token the real click would always arrive second and fail. The GET renders a
  confirmation; the POST behind it is what consumes.
"""
import asyncio
import html
import re
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

import clock
import db
import ratelimit
import security
import validation
from config import settings
from mailer import Mailer

# Every route here is held to validation.py's strict rules.
router = APIRouter(dependencies=[Depends(validation.strict_request)])

COOKIE_NAME = "portal_sid"
COOKIE_PATH = "/api/portal"


# ── helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """The client address, as attributed by the one proxy we trust.

    The frontend nginx is the only thing in front of the portal. It resolves the client
    from the edge's client-IP header and sends X-Forwarded-For as that single value, so
    first and last are the same. Reading the last entry stays right for any proxy that
    *appends*: the leftmost entry is whatever the client chose to send — and since this
    value keys the per-IP rate-limit buckets in `ratelimit.check`, trusting it would let a
    caller rotate the header and walk straight through the limit.

    Put a second proxy in front of the portal and this assumption needs revisiting.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# The same, under a public name, for main.py's SecuritySignals middleware.
client_ip = _client_ip


def _ua(request: Request) -> str:
    return request.headers.get("user-agent", "")[:400]


async def _floor(started: float) -> None:
    """Hold every auth response to a minimum duration.

    §10 accepts that the portal discloses whether an address is registered. What it must not
    also do is leak that through timing when the copy has been configured not to say it — so
    the fast path is padded to match the slow one.
    """
    elapsed_ms = (time.monotonic() - started) * 1000
    remaining = settings.response_floor_ms - elapsed_ms
    if remaining > 0:
        await asyncio.sleep(remaining / 1000)


def _app_title(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT portal_title, app_title FROM branding WHERE id = 1").fetchone()
    if row:
        return row["portal_title"] or row["app_title"] or "Volunteer Portal"
    return "Volunteer Portal"


def _issue_token(conn: sqlite3.Connection, purpose: str, vol_id: Optional[str],
                 addr_hash: str, email_enc: Optional[str], email_key_fp: Optional[str],
                 ip: str, ua: str, invite_id: Optional[str] = None) -> str:
    """Create a single-use login token, invalidating any live one for the same address.

    One live token per address: a resend must not leave the previous link working, or a
    forwarded older mail stays a valid credential.
    """
    conn.execute(
        "UPDATE login_tokens SET used_at = ? WHERE email_hmac = ? AND used_at IS NULL",
        (clock.now_iso(), addr_hash),
    )
    token = security.new_secret()
    conn.execute(
        "INSERT INTO login_tokens (token_hash, purpose, vol_id, email_hmac, email_enc, "
        "email_key_fp, created_at, expires_at, ip, ua, invite_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (security.hash_secret(token), purpose, vol_id, addr_hash, email_enc, email_key_fp,
         clock.now_iso(), clock.in_future(minutes=settings.token_ttl_min), ip, ua, invite_id),
    )
    return token


def _create_session(conn: sqlite3.Connection, response: Response, token_row: sqlite3.Row,
                    remember: bool, ip: str, ua: str) -> str:
    """Mint a session for a consumed token and set the cookie."""
    sid = security.new_secret()
    if remember:
        expires = clock.in_future(days=settings.remember_ttl_days)
        max_age = settings.remember_ttl_days * 86400
    else:
        expires = clock.in_future(hours=settings.session_ttl_hours)
        max_age = None  # browser-session cookie; the server-side row still expires
    conn.execute(
        "INSERT INTO sessions (sid_hash, vol_id, email_hmac, email_enc, email_key_fp, "
        "remember, created_at, expires_at, last_seen_at, ip, ua, invite_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (security.hash_secret(sid), token_row["vol_id"], token_row["email_hmac"],
         token_row["email_enc"], token_row["email_key_fp"], 1 if remember else 0,
         clock.now_iso(), expires, clock.now_iso(), ip, ua, token_row["invite_id"]),
    )
    response.set_cookie(
        COOKIE_NAME, sid, max_age=max_age, path=COOKIE_PATH,
        httponly=True, secure=True, samesite="lax",
    )
    return sid


def get_session(request: Request,
                conn: sqlite3.Connection = Depends(db.get_db)) -> Optional[sqlite3.Row]:
    """Resolve the session cookie, or None. Rolls the expiry for remember-me sessions.

    Args:
        request: The incoming request, whose cookie holds the session id.
        conn: The database connection.
    """
    sid = request.cookies.get(COOKIE_NAME)
    if not sid:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE sid_hash = ? AND revoked_at IS NULL AND expires_at > ?",
        (security.hash_secret(sid), clock.now_iso()),
    ).fetchone()
    if row is None:
        return None
    if row["remember"]:
        conn.execute(
            "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE sid_hash = ?",
            (clock.now_iso(), clock.in_future(days=settings.remember_ttl_days), row["sid_hash"]),
        )
    else:
        conn.execute("UPDATE sessions SET last_seen_at = ? WHERE sid_hash = ?",
                     (clock.now_iso(), row["sid_hash"]))
    return row


def require_volunteer(session: Optional[sqlite3.Row] = Depends(get_session)) -> sqlite3.Row:
    """A full session. A signup session (vol_id IS NULL) is explicitly not enough.

    Args:
        session: The session from `get_session`, or `None`.

    Returns:
        The session row.

    Raises:
        HTTPException 401: No session, or only a signup session.
    """
    if session is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    if session["vol_id"] is None:
        raise HTTPException(status_code=403, detail="Finish signing up first")
    return session


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/portal/request_link")
async def request_link(request: Request, email: str = Form(...),
                       conn: sqlite3.Connection = Depends(db.get_db)):
    """Mail a sign-in link to a registered address.

    Whether the response admits that an address is unknown is a setting, not a code path:
    §10 recommends matching `check_email`'s disclosure so someone who mistyped their address
    is told, rather than left watching an inbox that will never fill. Flip
    `portal_login_disclose` to '0' to make the copy generic.

    Args:
        request: The incoming request, for the rate limit's client address.
        email: The address as typed.
        conn: The database connection.
    """
    started = time.monotonic()
    ip, ua = _client_ip(request), _ua(request)
    _clean, problem = validation.check("email", email, required=True)
    if problem and problem[0] == "anomaly":
        # Report mode carries on exactly as before: the address is simply not found.
        validation.anomaly(problem[1], "email")
    addr_hash = security.email_hmac(settings.email_hmac_key, email)

    if not ratelimit.check(conn, "request_link", ip, addr_hash,
                           settings.rate_per_min, settings.rate_per_hour):
        await _floor(started)
        raise HTTPException(status_code=429, detail="Too many attempts. Try again shortly.")

    vol = conn.execute(
        "SELECT vol_id, status, inactive_at, email_enc, email_key_fp FROM volunteers "
        "WHERE email_hmac = ?",
        (addr_hash,),
    ).fetchone()
    disclose = db.kv_get(conn, "portal_login_disclose", "1") == "1"

    if vol is None:
        db.audit(conn, "request_link.unknown_address")
        await _floor(started)
        if disclose:
            return {"sent": False, "known": False,
                    "detail": "We don't have that address on file. Want to sign up?"}
        return {"sent": True, "detail": "If that address is on file, a link is on its way."}

    if vol["inactive_at"]:
        db.audit(conn, "request_link.inactive", vol["vol_id"])
        await _floor(started)
        return {"sent": False, "known": True,
                "detail": "That account isn't active. Please contact an organizer."}

    token = _issue_token(conn, "login", vol["vol_id"], addr_hash,
                         vol["email_enc"], vol["email_key_fp"], ip, ua)
    Mailer(settings).send_login_link(email.strip(), token, "login", _app_title(conn))
    db.audit(conn, "request_link.sent", vol["vol_id"])
    await _floor(started)
    return {"sent": True, "known": True,
            "detail": "Check your inbox for a sign-in link."}


# What security.new_secret makes: URL-safe base64. Anything else can't be a token, and is never
# put into the page (CodeQL #10: this is the one HTML page either app builds by hand).
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


@router.get("/api/portal/verify", response_class=HTMLResponse)
def verify_page(t: str = ""):
    """Render a confirm-click page. Deliberately has no side effect.

    A fallback: magic links now point at the frontend's /apply/verify. This stays for links
    mailed before that change and for reaching the portal directly, without the frontend.

    Args:
        t: The token from the link. Not consumed here.
    """
    token_ok = bool(_TOKEN_SHAPE.match(t or ""))
    if t and not token_ok:
        validation.anomaly("token", "t")
    # Escaped as well, though the shape check already rules out every special character.
    safe_t = html.escape(t, quote=True) if token_ok else ""
    body = f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Confirm sign-in</title>
<style>
 body {{ font-family: system-ui, -apple-system, "Segoe UI", sans-serif; margin: 0;
        display: grid; place-items: center; min-height: 100vh; background: #fafafa; color: #18181b; }}
 .card {{ background: #fff; padding: 2rem; border-radius: 10px; max-width: 24rem;
         box-shadow: 0 1px 3px rgba(0,0,0,.12); }}
 button {{ background: #18181b; color: #fff; border: 0; padding: .7rem 1.4rem;
          border-radius: 6px; font-size: 1rem; cursor: pointer; }}
 label {{ display: block; margin: 1rem 0; font-size: .9rem; }}
 .err {{ color: #b91c1c; }}
</style>
<div class="card">
  <h2>Confirm sign-in</h2>
  {"<p>Click below to finish signing in.</p>" if token_ok
   else '<p class="err">This link is missing its token, or it is damaged. Request a new one.</p>'}
  {'''<form method="post" action="/api/portal/verify">
    <input type="hidden" name="t" value="''' + safe_t + '''">
    <label><input type="checkbox" name="remember" value="1"> Remember this browser</label>
    <button type="submit">Sign in</button>
  </form>''' if token_ok else ''}
</div>"""
    return HTMLResponse(body)


@router.post("/api/portal/verify")
def verify(request: Request, response: Response, t: str = Form(...),
           remember: str = Form(default=""),
           conn: sqlite3.Connection = Depends(db.get_db)):
    """Consume a token and mint a session.

    The consume is one conditional UPDATE, so two simultaneous clicks on the same link
    resolve to one winner by rowcount rather than both being handed a session.

    Args:
        request: The incoming request, for the client address.
        response: The response, to set the session cookie on.
        t: The token from the link.
        remember: `1` for a long-lived session whose expiry rolls forward with use.
        conn: The database connection.
    """
    ip, ua = _client_ip(request), _ua(request)
    if not validation.TOKEN.match(t):
        validation.anomaly("token", "t")
    token_hash = security.hash_secret(t)

    cur = conn.execute(
        "UPDATE login_tokens SET used_at = ? "
        "WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (clock.now_iso(), token_hash, clock.now_iso()),
    )
    if cur.rowcount != 1:
        raise HTTPException(status_code=400,
                            detail="That link has expired or was already used. Request a new one.")

    row = conn.execute("SELECT * FROM login_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
    _create_session(conn, response, row, remember == "1", ip, ua)
    db.audit(conn, "verify.ok", row["vol_id"], detail=row["purpose"])

    if row["vol_id"] is None:
        return {"ok": True, "purpose": row["purpose"], "volunteer": None}
    vol = conn.execute(
        "SELECT vol_id, name, status, time_pref, add_time FROM volunteers WHERE vol_id = ?",
        (row["vol_id"],),
    ).fetchone()
    return {"ok": True, "purpose": row["purpose"], "volunteer": dict(vol) if vol else None}


@router.post("/api/portal/logout")
def logout(request: Request, response: Response,
           conn: sqlite3.Connection = Depends(db.get_db)):
    """Revoke this session only.

    Args:
        request: The incoming request, for the session cookie and client address.
        response: The response, to clear the cookie on.
        conn: The database connection.
    """
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        conn.execute("UPDATE sessions SET revoked_at = ? WHERE sid_hash = ? AND revoked_at IS NULL",
                     (clock.now_iso(), security.hash_secret(sid)))
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return {"ok": True}


@router.post("/api/portal/logout_all")
def logout_all(request: Request, response: Response,
               session: sqlite3.Row = Depends(require_volunteer),
               conn: sqlite3.Connection = Depends(db.get_db)):
    """Revoke every session this volunteer holds, on every device.

    Args:
        request: The incoming request, for the session cookie and client address.
        response: The response, to clear the cookie on.
        session: The volunteer's session, from `require_volunteer`.
        conn: The database connection.
    """
    cur = conn.execute(
        "UPDATE sessions SET revoked_at = ? WHERE vol_id = ? AND revoked_at IS NULL",
        (clock.now_iso(), session["vol_id"]),
    )
    db.audit(conn, "logout_all", session["vol_id"], detail=f"{cur.rowcount} sessions")
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return {"ok": True, "revoked": cur.rowcount}


EDITABLE_STATUSES = ("pending", "active")


def is_live(vol) -> bool:
    """Pending or approved, and neither cancelled nor inactive: what can be edited or cancelled.

    Args:
        vol: A `volunteers` row.
    """
    return (vol["status"] in EDITABLE_STATUSES and not vol["cancelled_at"]
            and not vol["inactive_at"])


def me_payload(conn: sqlite3.Connection, vol_id: str) -> dict:
    """The viewer's own record, picks, answers and assigned shifts. Mentions nobody else.

    `scheduled` comes from the pushed snapshot's slots, filtered to this volunteer: the edit
    form warns before they drop a timeslot they're assigned to (which raises a flag for the
    organizers rather than being refused).

    Args:
        conn: The database connection.
        vol_id: The signed-in volunteer.
    """
    import json
    vol = conn.execute(
        "SELECT vol_id, name, first_name, last_name, nickname, status, time_pref, add_time, "
        "add_any, answers, cancelled_at, inactive_at FROM volunteers WHERE vol_id = ?",
        (vol_id,),
    ).fetchone()
    if vol is None:
        raise HTTPException(status_code=404, detail="Volunteer record not found")
    picks = conn.execute(
        "SELECT vp.day_key, vp.time_key, vp.kind, vp.rank, d.day_str, t.time_str "
        "FROM vol_picks vp LEFT JOIN snapshot_days d ON d.day_key = vp.day_key "
        "LEFT JOIN snapshot_times t ON t.time_key = vp.time_key "
        "WHERE vp.vol_id = ? ORDER BY vp.kind DESC, vp.rank, vp.day_key, vp.time_key",
        (vol_id,),
    ).fetchall()
    scheduled = conn.execute(
        "SELECT s.day_key, s.time_key, s.pos_key, p.pos_str, d.day_str, t.time_str "
        "FROM snapshot_slots s LEFT JOIN snapshot_positions p ON p.pos_key = s.pos_key "
        "LEFT JOIN snapshot_days d ON d.day_key = s.day_key "
        "LEFT JOIN snapshot_times t ON t.time_key = s.time_key "
        "WHERE s.vol_id = ? ORDER BY s.day_key, s.time_key", (vol_id,),
    ).fetchall()
    volunteer = {k: vol[k] for k in vol.keys()
                 if k not in ("answers", "cancelled_at", "inactive_at")}

    def label(r):
        return f"{r['day_str'] or r['day_key']} {r['time_str'] or r['time_key']}"
    return {
        "volunteer": volunteer,
        "answers": json.loads(vol["answers"]) if vol["answers"] else {},
        # Approval (status) is untouched by cancelling or going inactive; these say whether
        # the registration is live. See portal.sql (volunteers: cancelled_at, inactive_at).
        "cancelled": bool(vol["cancelled_at"]),
        "inactive": bool(vol["inactive_at"]),
        "editable": is_live(vol),
        "can_rejoin": bool(vol["cancelled_at"]) and not vol["inactive_at"],
        "picks": [{**dict(p), "label": label(p)} for p in picks],
        "scheduled": [{"key": f"{r['day_key']}_{r['time_key']}", "day_key": r["day_key"],
                       "time_key": r["time_key"], "label": label(r),
                       "position": r["pos_str"] or r["pos_key"]} for r in scheduled],
    }


@router.get("/api/portal/me")
def me(session: sqlite3.Row = Depends(require_volunteer),
       conn: sqlite3.Connection = Depends(db.get_db)):
    """The viewer's own record. See me_payload.

    Args:
        session: The volunteer's session, from `require_volunteer`.
        conn: The database connection.
    """
    return me_payload(conn, session["vol_id"])
