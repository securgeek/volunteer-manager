"""Organizer accounts: listing, adding, roles and passwords, and each user's own profile.

Organizers manage every account (`/api/users/*`). Anyone signed in can change their own
password and email, which needs their current password, so a borrowed session can't take the
account over.
"""
import uuid
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from audit import audit
from db import get_db, get_session_data, RoleChecker, PgClient

router = APIRouter()

# passkey_count backs the User Management column and its "remove passkeys" action
# (routers/passkeys.py). The two statements share their column list; keep them in step.
_LIST_USERS_SQL = (
    "SELECT user_id, username, role, email, created_at, last_login_at, failed_login_cnt, "
    "(SELECT COUNT(*) FROM user_passkeys p WHERE p.user_id = users.user_id) AS passkey_count "
    "FROM users ORDER BY username;"
)
_GET_USER_SQL = (
    "SELECT user_id, username, role, email, created_at, last_login_at, failed_login_cnt, "
    "(SELECT COUNT(*) FROM user_passkeys p WHERE p.user_id = users.user_id) AS passkey_count "
    "FROM users WHERE user_id = ?;"
)


@router.get("/api/users")
def list_users(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Return all user accounts ordered by username.

    Args:
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        list of user dicts with keys ``user_id``, ``username``, ``role``,
        ``created_at``, ``last_login_at``, ``failed_login_cnt``, ``passkey_count``.

    Raises:
        HTTPException 401/403: Insufficient role.
    """
    return db.execute(_LIST_USERS_SQL).fetchall()


@router.post("/api/users/add")
def add_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role:     str = Form(default="view"),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Create a new user account.

    Args:
        request: Starlette Request; used for audit logging.
        username: Desired username; lowercased and stripped before storage.
        password: Plaintext password; must be at least 8 characters.
        role: One of ``"organizer"``, ``"checkin"``, ``"view"``; defaults to ``"view"``.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 201 with the new user dict: ``user_id``, ``username``, ``role``,
        ``created_at``, ``last_login_at``, ``failed_login_cnt``, ``passkey_count``.

    Raises:
        HTTPException 400: Password too short, username blank, or invalid role.
        HTTPException 409: Username already exists.
        HTTPException 401/403: Insufficient role.
    """
    username = username.lower().strip()
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if role not in ("organizer", "checkin", "view"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if db.execute("SELECT 1 FROM users WHERE username = ?;", (username,)).fetchone():
        raise HTTPException(status_code=409, detail=f"Username '{username}' already exists")
    user_id = str(uuid.uuid4())
    ph = PasswordHasher()
    db.execute(
        "INSERT INTO users (user_id, username, password_hash, role) VALUES (?, ?, ?, ?);",
        (user_id, username, ph.hash(password), role)
    )
    db.commit()
    audit("user.create", request, request.session.get("session_id"),
          f"Added user {username!r} with role {role!r}", target=username, role=role)
    return JSONResponse(status_code=201, content=jsonable_encoder(db.execute(
        _GET_USER_SQL, (user_id,)
    ).fetchone()))


@router.post("/api/users/set_role")
def set_user_role(
    request: Request,
    user_id: str = Form(...),
    role:    str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Change a user's role; self-modification is blocked to prevent accidental lockout.

    Args:
        request: Starlette Request; used for audit logging.
        user_id: UUID of the user to update.
        role: New role; must be ``"organizer"``, ``"checkin"``, or ``"view"``.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: Attempting to change own role, or invalid role value.
        HTTPException 404: User not found.
        HTTPException 401/403: Insufficient role.
    """
    if user_id == s_data["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if role not in ("organizer", "checkin", "view"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if not db.execute("SELECT 1 FROM users WHERE user_id = ?;", (user_id,)).fetchone():
        raise HTTPException(status_code=404, detail="User not found")
    db.execute(
        "UPDATE users SET role = ?, updated_at = NOW() WHERE user_id = ?;",
        (role, user_id)
    )
    db.commit()
    audit("user.role_change", request, request.session.get("session_id"),
          f"Set role of {user_id!r} to {role!r}", target_user_id=user_id, role=role)
    return {"ok": True}


@router.post("/api/users/set_password")
def set_user_password(
    request: Request,
    user_id:  str = Form(...),
    password: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Reset a user's password; increments password_version so any cached sessions re-auth.

    Args:
        request: Starlette Request; used for audit logging.
        user_id: UUID of the user whose password should be reset.
        password: New plaintext password; must be at least 8 characters.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: Password is shorter than 8 characters.
        HTTPException 404: User not found.
        HTTPException 401/403: Insufficient role.
    """
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not db.execute("SELECT 1 FROM users WHERE user_id = ?;", (user_id,)).fetchone():
        raise HTTPException(status_code=404, detail="User not found")
    ph = PasswordHasher()
    db.execute(
        "UPDATE users SET password_hash = ?, password_version = password_version + 1, "
        "updated_at = NOW(), failed_login_cnt = 0 WHERE user_id = ?;",
        (ph.hash(password), user_id)
    )
    db.commit()
    audit("password.reset.by_admin", request, request.session.get("session_id"),
          f"Reset password for user {user_id!r}", target_user_id=user_id)
    return {"ok": True}


@router.post("/api/users/delete")
def delete_user(
    request: Request,
    user_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Delete a user account; self-deletion is blocked to prevent lockout.

    Args:
        request: Starlette Request; used for audit logging.
        user_id: UUID of the user to delete.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        HTTP 204 with no body.

    Raises:
        HTTPException 400: Attempting to delete own account.
        HTTPException 404: User not found.
        HTTPException 401/403: Insufficient role.
    """
    if user_id == s_data["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not db.execute("SELECT 1 FROM users WHERE user_id = ?;", (user_id,)).fetchone():
        raise HTTPException(status_code=404, detail="User not found")
    db.execute("DELETE FROM users WHERE user_id = ?;", (user_id,))
    db.commit()
    audit("user.delete", request, request.session.get("session_id"), f"Deleted user {user_id!r}",
          target_user_id=user_id)
    return Response(status_code=204)


@router.post("/api/users/change_password")
def change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Change the signed-in user's own password. Any role. Backs the Profile dialog.

    Requires the current password, so a borrowed session can't lock the owner out. Bumps
    password_version, as set_password does.

    Args:
        request: The incoming request, for the session id and the audit log.
        current_password: Their current password.
        new_password: The new one, at least 8 characters.
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.

    Raises:
        HTTPException 400: New password shorter than 8 characters.
        HTTPException 401: Not signed in.
        HTTPException 403: Current password incorrect.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = db.execute("SELECT password_hash FROM users WHERE user_id = ?;",
                     (s_data["user_id"],)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Unauthorized")
    ph = PasswordHasher()
    try:
        ph.verify(row["password_hash"], current_password)
    except (VerificationError, InvalidHashError):
        audit("password.change.fail", request, request.session.get("session_id"),
              "Password change refused: wrong current password")
        raise HTTPException(status_code=403, detail="Current password is incorrect") from None
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    db.execute(
        "UPDATE users SET password_hash = ?, password_version = password_version + 1, "
        "updated_at = NOW(), failed_login_cnt = 0 WHERE user_id = ?;",
        (ph.hash(new_password), s_data["user_id"]))
    db.commit()
    audit("password.change", request, request.session.get("session_id"), "Changed own password")
    return {"ok": True}



@router.post("/api/users/set_email")
def set_user_email(
    request: Request,
    user_id: str = Form(...),
    email: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Set or clear another user's email (where password reset links go). Organizers can already
    reset passwords outright, so this grants nothing new; blank clears it.

    Args:
        request: The incoming request, for the session id and the audit log.
        user_id: The account to change.
        email: The new address. Blank clears it.
        s_data: The organizer's session.
        db: The request's database connection.
    """
    from routers.invites import clean_email
    address = clean_email(email)
    cur = db.execute("UPDATE users SET email = ?, updated_at = NOW() WHERE user_id = ?;",
                     (address, user_id))
    db.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="User not found")
    audit("user.email_set", request, request.session.get("session_id"),
          f"{'Set' if address else 'Cleared'} email for user {user_id!r}",
          target_user_id=user_id)
    return {"ok": True, "email": address}


@router.get("/api/users/me")
def my_account(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """The signed-in user's own account, for Profile.

    Args:
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = db.execute("SELECT username, role, email FROM users WHERE user_id = ?;",
                     (s_data["user_id"],)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return dict(row)


@router.post("/api/users/change_email")
def change_own_email(
    request: Request,
    current_password: str = Form(...),
    email: str = Form(default=""),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Set or clear one's own email. Needs the current password: otherwise someone holding a
    stolen session could point resets at their own address and take the account.

    Args:
        request: The incoming request, for the session id and the audit log.
        current_password: Their current password.
        email: The new address. Blank clears it.
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.

    Raises:
        HTTPException 400: Not an address. 401: Not signed in. 403: Wrong password.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    from routers.invites import clean_email
    address = clean_email(email)
    row = db.execute("SELECT password_hash FROM users WHERE user_id = ?;",
                     (s_data["user_id"],)).fetchone()
    try:
        PasswordHasher().verify(row["password_hash"], current_password)
    except (VerificationError, InvalidHashError, TypeError):
        audit("user.email_change.fail", request, request.session.get("session_id"),
              "Email change refused: wrong current password")
        raise HTTPException(status_code=403, detail="Current password is incorrect") from None
    db.execute("UPDATE users SET email = ?, updated_at = NOW() WHERE user_id = ?;",
               (address, s_data["user_id"]))
    db.commit()
    audit("user.email_change", request, request.session.get("session_id"),
          "Set own email" if address else "Cleared own email")
    return {"ok": True, "email": address}
