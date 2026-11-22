"""Organizer sign-in with a password, and sign-out.

Passwords are Argon2 hashes. A successful sign-in creates a `sessions` row and puts its id in
the signed session cookie. `start_session` is shared with the passkey and invite routers, so
every way in produces the same session.
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import Response
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from db import get_db, audit, _session_cache, get_session_data, PgClient

router = APIRouter()


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
    """
    username = username.lower()
    row = db.execute(
        "SELECT user_id, username, password_hash, failed_login_cnt, role FROM users WHERE username = ?;",
        (username,)
    ).fetchone()
    if not row:
        audit(request, None, f"Invalid login attempt for unknown user {username}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    ph = PasswordHasher()
    try:
        ph.verify(row["password_hash"], password)
    except VerifyMismatchError:
        new_failed = row["failed_login_cnt"] + 1
        db.execute("UPDATE users SET failed_login_cnt = ? WHERE username = ?;", (new_failed, username))
        db.commit()
        audit(request, None, f"Invalid login attempt for user {username}")
        raise HTTPException(status_code=401, detail="Unauthorized") from None

    return start_session(request, db, row)


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
        _session_cache.pop(s_id, None)
        db.execute("DELETE FROM sessions WHERE session_id = ?;", (s_id,))
        db.commit()
    return Response(status_code=200)
