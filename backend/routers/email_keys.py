"""The RSA public key that volunteer addresses are encrypted to, and the ciphertexts.

Addresses are never stored readable. The portal encrypts each signup's address to this key;
only `private.pem`, kept off the server, can decrypt them. The organizer's browser does that
locally, so the server never sees a plaintext address. Setting a new key also pushes it to the
portal (`portal_sync`).
"""
from typing import Optional
from fastapi import APIRouter, Request, Depends, Form, HTTPException

import portal_sync
from audit import audit
from db import get_db, get_session_data, RoleChecker, PgClient

router = APIRouter()


@router.get("/api/email_pubkey")
def get_email_pubkey(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Return the stored RSA public key used to encrypt volunteer email addresses.

    Args:
        s_data: Injected session dict; endpoint requires any authenticated user.
        db: Injected database connection.

    Returns:
        dict with key ``pubkey`` containing the SPKI PEM string, or ``None`` if not yet set.

    Raises:
        HTTPException 401: Not authenticated.
    """
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = db.execute("SELECT val FROM app_kv WHERE app_key = 'email_pubkey';").fetchone()
    return {"pubkey": row["val"] if row else None}


@router.post("/api/email_pubkey")
def set_email_pubkey(
    request: Request,
    pubkey: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Store or replace the RSA public key used to encrypt volunteer email addresses.

    Args:
        request: Starlette Request; used for audit logging.
        pubkey: SPKI PEM string for an RSA-OAEP key; validated client-side before submission.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict ``{"ok": True}``.

    Raises:
        HTTPException 400: pubkey is empty after stripping whitespace.
        HTTPException 401/403: Insufficient role.
    """
    pubkey = pubkey.strip()
    if not pubkey:
        raise HTTPException(status_code=400, detail="Public key is required")
    db.execute(
        "INSERT INTO app_kv (app_key, val) VALUES ('email_pubkey', ?) "
        "ON CONFLICT(app_key) DO UPDATE SET val = excluded.val;",
        (pubkey,)
    )
    db.commit()
    audit("email_key.update", request, request.session.get("session_id"),
          "Updated email public key")
    # The portal encrypts signups to this key and keeps signup shut without one.
    portal_sync.request_push()
    return {"ok": True}


@router.post("/api/get_vol_email")
def get_vol_email(
    vol_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Return the encrypted email ciphertext for a volunteer; decryption happens in the browser.

    Args:
        vol_id: Volunteer ID to retrieve the encrypted email for.
        s_data: Injected session dict; requires organizer role.
        db: Injected database connection.

    Returns:
        dict with key ``email_enc`` containing the RSA-OAEP ciphertext (base64), or ``None``.

    Raises:
        HTTPException 404: Volunteer not found.
        HTTPException 401/403: Insufficient role.
    """
    row = db.execute("SELECT email_enc FROM volunteers WHERE vol_id = ?;", (vol_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Volunteer not found")
    return {"email_enc": row["email_enc"]}
