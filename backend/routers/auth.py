"""Organizer sign-in with a password, and sign-out.

Passwords are Argon2 hashes. A successful sign-in creates a `sessions` row and puts its id in
the signed session cookie. `start_session` is shared with the passkey and invite routers, so
every way in produces the same session.

**Failed sign-ins are rate limited** (`LOGIN_LIMITS`), checked before the password is: a
refused request is a 429 with `Retry-After` and never tests a password. Only failures count,
because at the event every check-in laptop shares the venue's one address. The limits are per
username *from one address*, per address, and a much higher per-username ceiling across all
addresses, so a stranger who knows a username can't lock its owner out from elsewhere while a
spread-out guessing run still hits a wall (and the alerts fire long before it). A successful
sign-in clears its username-and-address count. An unknown username is checked against a
dummy hash, so its reply takes as long as a wrong password's.
"""
import uuid
from contextlib import suppress
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import Response
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from audit import audit, client_ip
from api_auth import rate_key
from db import get_db, _session_cache, get_session_data, PgClient
from ratelimit import SlidingWindow

router = APIRouter()

LOGIN_WINDOW_SEC = 15 * 60
# Failed sign-ins allowed per window, per key kind.
LOGIN_LIMITS = {"user_ip": 5, "ip": 20, "user": 30}
_login_fails = {kind: SlidingWindow(n, LOGIN_WINDOW_SEC) for kind, n in LOGIN_LIMITS.items()}
# One `auth.login.throttled` audit line per key per window, however hard a client hammers.
_throttle_noted = SlidingWindow(1, LOGIN_WINDOW_SEC)

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so that reply costs the same Argon2 time
# as a wrong password and response time doesn't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash(uuid.uuid4().hex)


def _login_keys(username: str, request: Request) -> dict:
    """The rate-limit key for each of LOGIN_LIMITS' kinds, for this username and client."""
    ip = rate_key(client_ip(request))
    return {"user_ip": f"{username}|{ip}", "ip": ip, "user": username}


def reset_login_limits() -> None:
    """Forget every failed-sign-in count (tests)."""
    for w in (*_login_fails.values(), _throttle_noted):
        w.clear()


def _throttled(request: Request, username: str, keys: dict) -> HTTPException | None:
    """A 429 to raise if any failed-sign-in limit is reached for these keys, else None."""
    hit = [kind for kind, key in keys.items() if _login_fails[kind].over(key)]
    if not hit:
        return None
    if any(_throttle_noted.allow(f"{kind}:{keys[kind]}") for kind in hit):
        audit("auth.login.throttled", request, None, "Sign-in refused: too many failures",
              username=username, limits=hit)
    wait = max(_login_fails[kind].retry_after(keys[kind]) for kind in hit)
    return HTTPException(status_code=429, detail="Too many failed sign-ins. Try again later.",
                         headers={"Retry-After": str(wait)})


def _failed(request: Request, username: str, keys: dict, reason: str) -> HTTPException:
    """Count a failed sign-in against every key, audit it, and return the 401 to raise."""
    for kind, key in keys.items():
        _login_fails[kind].hit(key)
    audit("auth.login.fail", request, None, "Invalid login attempt",
          username=username, reason=reason)
    return HTTPException(status_code=401, detail="Unauthorized")


@router.post("/api/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: PgClient = Depends(get_db),
):
    """Verify credentials, create a session record, and set the session cookie.

    Passwords are hashed with Argon2
    
    Session IDs are generated with UUID4

    Args:
        request: Starlette Request; client IP and user-agent stored in the sessions table.
        username: Login username; lowercased before lookup.
        password: Plaintext password verified against the stored Argon2 hash.
        db: Injected database connection.

    Returns:
        dict with keys ``username`` and ``role``.

    Raises:
        HTTPException 401: User not found or password mismatch.
        HTTPException 429: Too many failed sign-ins for this username or from this address
            (`LOGIN_LIMITS`); `Retry-After` says when to try again.
    """
    username = username.lower()
    keys = _login_keys(username, request)
    refused = _throttled(request, username, keys)
    if refused:
        raise refused

    # Postgres can't take a NUL in a query parameter (psycopg2 raises, which was a 500). No
    # username has one, so it's simply an unknown user.
    row = None if "\x00" in username else db.execute(
        "SELECT user_id, username, password_hash, failed_login_cnt, role FROM users "
        "WHERE username = ?;",
        (username,)
    ).fetchone()
    if not row:
        with suppress(VerifyMismatchError):          # for the time it takes; never matches
            _hasher.verify(_DUMMY_HASH, password)
        raise _failed(request, username, keys, "unknown_user")

    try:
        _hasher.verify(row["password_hash"], password)
    except VerifyMismatchError:
        new_failed = row["failed_login_cnt"] + 1
        db.execute("UPDATE users SET failed_login_cnt = ? WHERE username = ?;",
                   (new_failed, username))
        db.commit()
        raise _failed(request, username, keys, "bad_password") from None

    _login_fails["user_ip"].clear(keys["user_ip"])
    result = start_session(request, db, row)
    audit("auth.login.ok", request, request.session.get("session_id"), "Signed in with a password")
    return result


def start_session(request: Request, db: PgClient, row: dict) -> dict:
    """Create a session for an authenticated user and set the cookie.

    Shared by the password login and the passkey login (routers/passkeys.py), so both
    produce exactly the same session.

    Args:
        row: A ``users`` row with at least ``user_id``, ``username`` and ``role``.

    Returns:
        dict with keys ``username`` and ``role``.
    """
    session_id = str(uuid.uuid4())
    user_id = row["user_id"]
    role = row["role"] or "organizer"

    db.execute(
        "UPDATE users SET last_login_at = NOW(), failed_login_cnt = 0 WHERE user_id = ?;",
        (user_id,)
    )
    db.execute(
        "INSERT INTO sessions (session_id, user_id, ip_addr, user_agent) VALUES (?, ?, ?, ?);",
        (session_id, user_id,
         request.client.host if request.client else "",
         request.headers.get("user-agent", ""))
    )
    db.commit()

    request.session["session_id"] = session_id
    _session_cache[session_id] = {"user_id": user_id, "username": row["username"], "role": role}

    return {"username": row["username"], "role": role}


@router.get("/api/logout")
def logout(request: Request, db: PgClient = Depends(get_db)):
    """Invalidate the session cookie, in-memory cache entry, and DB session row.

    Args:
        request: Starlette Request; session cookie cleared via request.session.
        db: Injected database connection.

    Returns:
        HTTP 200 with an empty body.
    """
    s_id = request.session.pop("session_id", None)
    if s_id:
        audit("auth.logout", request, s_id, "Signed out")
        _session_cache.pop(s_id, None)
        db.execute("DELETE FROM sessions WHERE session_id = ?;", (s_id,))
        db.commit()
    return Response(status_code=200)
