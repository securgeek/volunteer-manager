"""Inviting someone to create their own admin account, instead of an organizer making it.

An organizer creates an invite for a role, optionally with an email address; the link is
mailed through the portal (portal_sync.send_admin_mail) and shown once to copy as well. The
invitee opens /admin/invite?t=…, picks a username and password, and is signed in.

* **Only the token's hash is stored** (32 random bytes, SHA-256): a copy of user_invites can't
  accept anything, and the link can't be shown again after creation.
* **Opening the link does nothing.** GET check is read-only, because mail and chat previews
  fetch links; accept is a POST that consumes the token in one conditional UPDATE, inside the
  same transaction as the new user, so two submissions resolve to one account.
* **72 hours, single use, revocable.**
"""
import hashlib
import logging
import re
import secrets
import uuid
from typing import Optional

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, Form, HTTPException, Request

import portal_sync
from audit import audit
from db import PgClient, RoleChecker, get_app_kv, get_db
from routers.auth import start_session

router = APIRouter()

INVITE_HOURS = 72
ROLES = ("organizer", "checkin", "view")
# The domain is dot-separated labels, each without a dot, so the match can't backtrack (a
# domain part of [^@\s]+\.[^@\s]+ could, quadratically: CodeQL #3, #4, #6). It also refuses an
# empty label, as in a@.b or a@b..c, which were never deliverable anyway.
EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


def token_hash(token: str) -> str:
    """Hash an invite token for storage. Only the hash is kept.

    Args:
        token: The random token from the link.

    Returns:
        The SHA-256 hex digest.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def clean_email(raw: Optional[str]) -> Optional[str]:
    """A plausible address, lowercased, or None for blank. 400 for anything else.

    Args:
        raw: The address as typed, or `None`.

    Raises:
        HTTPException 400: Too long, or not shaped like an address.
    """
    email = (raw or "").strip().lower()
    if not email:
        return None
    if len(email) > 254 or not EMAIL.match(email):
        raise HTTPException(status_code=400, detail="That doesn't look like an email address")
    return email


def _iso(ts):
    return ts.isoformat() + "Z" if ts else None


# An invite is live while unused, unrevoked and unexpired. Each query below spells that out,
#   used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC'
# so every statement is a complete literal. Keep the copies identical.


@router.post("/api/invites")
async def create_invite(
    request: Request,
    role: str = Form(...),
    email: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Create an invite, and mail it if an address is given.

    Args:
        request: The incoming request, for the session id and the audit log.
        role: The new account's role: `organizer`, `checkin` or `view`.
        email: Where to mail the link. Blank means the organizer sends it by hand.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict with ``invite_id``, ``link`` (shown once; never retrievable later),
        ``expires_at``, ``emailed`` and, when mailing failed, ``email_error`` — the invite
        stands either way, so the link can be sent by hand.
    """
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")
    email = clean_email(email)
    site = portal_sync.site_origin()
    if not site:
        raise HTTPException(status_code=503, detail="ADMIN_PUBLIC_URL is unset, so there's "
                            "no address to put in the link")
    token = secrets.token_urlsafe(32)
    invite_id = str(uuid.uuid4())
    row = db.execute(
        "INSERT INTO user_invites (invite_id, token_hash, role, email, created_by, expires_at) "
        "VALUES (?, ?, ?, ?, ?, NOW() AT TIME ZONE 'UTC' + ? * INTERVAL '1 hour') "
        "RETURNING expires_at;",
        (invite_id, token_hash(token), role, email, s_data["username"], INVITE_HOURS),
    ).fetchone()
    db.commit()
    link = f"{site}/admin/invite?t={token}"
    audit("invite.create", request, request.session.get("session_id"),
          f"Created a {role} invite {invite_id}")

    out = {"invite_id": invite_id, "link": link, "expires_at": _iso(row["expires_at"]),
           "emailed": False}
    if email:
        try:
            await portal_sync.send_admin_mail(
                "invite", email, link, role=role, inviter=s_data["username"],
                app_title=get_app_kv(db, "app_title") or "the conference",
                hours=INVITE_HOURS)
            out["emailed"] = True
        # Deliberately broad: a failed mail mustn't undo the invite. The link is still
        # usable. The organizer is told it didn't send; the reason (which can carry the
        # portal's reply) goes to the server log, not the page (CodeQL #7).
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            logging.getLogger(__name__).warning("invite %s: mail not sent: %s", invite_id, exc)
            out["email_error"] = ("The invite was created, but the email couldn't be sent. "
                                  "Copy the link and send it yourself; Admin → Volunteer "
                                  "Portal shows any mail problems.")
    return out


@router.get("/api/invites")
def list_invites(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Outstanding invites: not used, revoked or expired. Never the link.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.
    """
    rows = db.execute(
        "SELECT invite_id, role, email, created_by, created_at, expires_at FROM user_invites "
        "WHERE used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC' "
        "ORDER BY created_at DESC;").fetchall()
    return [{**dict(r), "created_at": _iso(r["created_at"]), "expires_at": _iso(r["expires_at"])}
            for r in rows]


@router.post("/api/invites/revoke")
def revoke_invite(
    request: Request,
    invite_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Revoke an outstanding invite, so its link stops working.

    Args:
        request: The incoming request, for the session id and the audit log.
        invite_id: The invite to revoke.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict `{"ok": True}`.

    Raises:
        HTTPException 404: No outstanding invite with that id (used, revoked or expired).
    """
    cur = db.execute(
        "UPDATE user_invites SET revoked_at = NOW() AT TIME ZONE 'UTC' "
        "WHERE invite_id = ? AND used_at IS NULL AND revoked_at IS NULL "
        "AND expires_at > NOW() AT TIME ZONE 'UTC';",
        (invite_id,))
    db.commit()
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="No outstanding invite with that id")
    audit("invite.revoke", request, request.session.get("session_id"),
          f"Revoked invite {invite_id}")
    return {"ok": True}


@router.get("/api/invites/check")
def check_invite(t: str = "", db: PgClient = Depends(get_db)):
    """Whether a link is still good, and for what role. Read-only: link previews fetch this.

    Args:
        t: The token from the link.
        db: The request's database connection.
    """
    row = db.execute(
        "SELECT role, email, expires_at FROM user_invites WHERE token_hash = ? "
        "AND used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC';",
        (token_hash(t),)).fetchone() if t else None
    if not row:
        return {"valid": False}
    return {"valid": True, "role": row["role"], "email": row["email"],
            "expires_at": _iso(row["expires_at"])}


@router.post("/api/invites/accept")
def accept_invite(
    request: Request,
    t: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(default=""),
    db: PgClient = Depends(get_db),
):
    """Create the invited account and sign it in.

    Everything is checked before the token is spent, so a taken username or short password
    leaves the link usable; the consume and the insert commit together.

    Args:
        request: The incoming request, for the new session and the audit log.
        t: The token from the link.
        username: The new account's username.
        password: The new account's password, at least 8 characters.
        email: An optional address for password resets.
        db: The request's database connection.

    Returns:
        dict with ``username`` and ``role``, as POST /api/login.

    Raises:
        HTTPException 400: Expired, used or revoked link; bad username, password or email.
        HTTPException 409: Username taken.
    """
    username = username.lower().strip()
    if not re.match(r"^[a-z0-9][a-z0-9._-]{1,31}$", username):
        raise HTTPException(status_code=400, detail="Username: 2-32 letters, digits, dots, "
                            "dashes or underscores")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    email = clean_email(email)
    if db.execute("SELECT 1 FROM users WHERE username = ?;", (username,)).fetchone():
        raise HTTPException(status_code=409, detail=f"Username '{username}' is taken")

    user_id = str(uuid.uuid4())
    invite = db.execute(
        "UPDATE user_invites SET used_at = NOW() AT TIME ZONE 'UTC', used_by = ?, email = NULL "
        "WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL "
        "AND expires_at > NOW() AT TIME ZONE 'UTC' RETURNING invite_id, role;",
        (username, token_hash(t))).fetchone()
    if not invite:
        db.commit()
        raise HTTPException(status_code=400, detail="This invite link has expired, was "
                            "already used, or was revoked. Ask an organizer for a new one.")
    try:
        db.execute("INSERT INTO users (user_id, username, password_hash, role, email) "
                   "VALUES (?, ?, ?, ?, ?);",
                   (user_id, username, PasswordHasher().hash(password), invite["role"], email))
    except Exception:
        # Someone took the username in the moment since the check; the invite isn't spent.
        db._conn.rollback()
        raise HTTPException(status_code=409, detail=f"Username '{username}' is taken") from None
    # start_session commits, so the consume, the new user and the session land together.
    result = start_session(request, db, {"user_id": user_id, "username": username,
                                         "role": invite["role"]})
    audit("invite.accept", request, request.session.get("session_id"),
          f"Accepted invite {invite['invite_id']} as {invite['role']}",
          invite_id=invite["invite_id"], role=invite["role"])
    return result
