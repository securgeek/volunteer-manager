"""Password reset by email, for admin accounts. Sign-in stays by username.

"Forgot password?" on the login page posts a username. If that account has an email address
(users.email, set in Profile or by an invite), a single-use link good for RESET_MINUTES is
mailed through the portal (portal_sync.send_admin_mail); it opens /admin/reset?t=….

* **The reply never varies.** Unknown username, no email on file, or sent: the same words, so
  the page can't be used to find accounts. A mail failure is logged, not shown.
* **Only the token's hash is stored**, and GET check is read-only (link previews fetch it);
  complete consumes it in one conditional UPDATE.
* **A successful reset signs the account out everywhere** — sessions deleted and dropped from
  _session_cache — and spends any other outstanding reset links for it.
* Requests are rate limited per username and per IP, in memory: the backend runs one worker.
"""
import logging
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, Form, HTTPException, Request

import portal_sync
from api_auth import rate_key
from db import PgClient, _session_cache, audit, get_app_kv, get_db
from routers.invites import token_hash

router = APIRouter()
log = logging.getLogger("password_reset")

RESET_MINUTES = 60
RATE_LIMIT, RATE_WINDOW_SEC = 5, 15 * 60
GENERIC = ("If that account has an email address on file, a reset link is on its way. "
           "It works once, for an hour.")

_hits: dict = defaultdict(deque)
_hits_lock = threading.Lock()


def _allowed(*keys: str) -> bool:
    """A sliding window per key; False if any key is over the limit. Counts only if allowed."""
    now = time.monotonic()
    with _hits_lock:
        for k in keys:
            q = _hits[k]
            while q and q[0] <= now - RATE_WINDOW_SEC:
                q.popleft()
            if len(q) >= RATE_LIMIT:
                return False
        for k in keys:
            _hits[k].append(now)
    return True


def _client_ip(request: Request) -> str:
    # nginx sends a single resolved X-Forwarded-For (frontend/CLAUDE.md → Reverse proxy).
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


# A reset link is live while unused and unexpired. Each query below spells that out,
#   used_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC'
# so every statement is a complete literal. Keep the copies identical.


@router.post("/api/password_reset/request")
async def request_reset(request: Request, username: str = Form(...),
                        db: PgClient = Depends(get_db)):
    """Mail a reset link if the account exists and has an email. The reply is always GENERIC.

    Args:
        request: The incoming request, for the rate limit and the audit log.
        username: The account to reset.
        db: The request's database connection.

    Raises:
        HTTPException 429: Too many requests for this username or from this address.
    """
    username = username.lower().strip()
    ip = _client_ip(request)
    if not _allowed(f"u:{username}", f"ip:{rate_key(ip)}"):
        raise HTTPException(status_code=429, detail="Too many reset requests. Try again later.")
    row = db.execute("SELECT user_id, email FROM users WHERE username = ?;",
                     (username,)).fetchone()
    if not row or not row["email"]:
        audit(request, None, f"Password reset requested for {username!r}: "
                             f"{'no email on file' if row else 'no such user'}")
        return {"ok": True, "detail": GENERIC}

    site = portal_sync.site_origin()
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO password_resets (token_hash, user_id, expires_at, ip) "
        "VALUES (?, ?, NOW() AT TIME ZONE 'UTC' + ? * INTERVAL '1 minute', ?);",
        (token_hash(token), row["user_id"], RESET_MINUTES, ip))
    db.commit()
    try:
        await portal_sync.send_admin_mail(
            "reset", row["email"], f"{site}/admin/reset?t={token}", username=username,
            app_title=get_app_kv(db, "app_title") or "the conference", minutes=RESET_MINUTES)
        audit(request, None, f"Password reset link sent for {username!r}")
    # Deliberately broad: the reply never varies (GENERIC), so no failure may reach the
    # caller. It goes to the log and the audit trail instead.
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        log.error("password reset mail for %r failed: %s", username, exc)
        audit(request, None, f"Password reset for {username!r}: mail failed")
    return {"ok": True, "detail": GENERIC}


@router.get("/api/password_reset/check")
def check_reset(t: str = "", db: PgClient = Depends(get_db)):
    """Whether a reset link is still good. Read-only: link previews fetch this.

    Args:
        t: The token from the link.
        db: The request's database connection.
    """
    row = db.execute(
        "SELECT u.username FROM password_resets r JOIN users u ON u.user_id = r.user_id "
        "WHERE r.token_hash = ? AND r.used_at IS NULL "
        "AND r.expires_at > NOW() AT TIME ZONE 'UTC';",
        (token_hash(t),)).fetchone() if t else None
    return {"valid": True, "username": row["username"]} if row else {"valid": False}


@router.post("/api/password_reset/complete")
def complete_reset(request: Request, t: str = Form(...), password: str = Form(...),
                   db: PgClient = Depends(get_db)):
    """Set the new password and sign the account out everywhere. Doesn't sign in: the user
    signs in fresh with the new password.

    Args:
        request: The incoming request, for the session id and the audit log.
        t: The token from the link.
        password: The new password, at least 8 characters.
        db: The request's database connection.

    Raises:
        HTTPException 400: Short password (the link stays usable), or a link that's expired
            or already used.
    """
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    row = db.execute(
        "UPDATE password_resets SET used_at = NOW() AT TIME ZONE 'UTC' "
        "WHERE token_hash = ? AND used_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC' "
        "RETURNING user_id;", (token_hash(t),)).fetchone()
    if not row:
        db.commit()
        raise HTTPException(status_code=400, detail="This reset link has expired or was "
                            "already used. Request a new one from the sign-in page.")
    user_id = row["user_id"]
    db.execute(
        "UPDATE users SET password_hash = ?, password_version = password_version + 1, "
        "failed_login_cnt = 0, updated_at = NOW() WHERE user_id = ?;",
        (PasswordHasher().hash(password), user_id))
    # Any other link for this account is spent too, and every session ends.
    db.execute("UPDATE password_resets SET used_at = NOW() AT TIME ZONE 'UTC' "
               "WHERE user_id = ? AND used_at IS NULL "
               "AND expires_at > NOW() AT TIME ZONE 'UTC';", (user_id,))
    sids = [r["session_id"] for r in db.execute(
        "DELETE FROM sessions WHERE user_id = ? RETURNING session_id;", (user_id,)).fetchall()]
    db.commit()
    for sid in sids:
        _session_cache.pop(sid, None)
    name = db.execute("SELECT username FROM users WHERE user_id = ?;", (user_id,)).fetchone()
    audit(request, None, f"Password reset completed for {name['username'] if name else user_id!r}; "
                         f"{len(sids)} session(s) ended")
    return {"ok": True}
