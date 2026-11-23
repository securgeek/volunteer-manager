"""Managing export API keys (Admin → API Keys). Organizers only.

The keys themselves are checked in api_auth.py. A new key is returned once and only its hash
is kept, like invites. Keys aren't deleted, only revoked, so the list and the audit log keep
saying which key did what.
"""
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request

import api_auth
from audit import audit
from db import PgClient, RoleChecker, get_db

router = APIRouter()

DEFAULT_DAYS, MAX_DAYS = 30, 365
MAX_LABEL = 80


def _iso(ts):
    return ts.isoformat() + "Z" if ts else None


@router.get("/api/api_tokens")
def list_tokens(
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Every key, newest first, with whether it still works. Never the key or its hash.

    Args:
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict with ``tokens`` (list) and ``scopes`` (name → description) for the create form.
    """
    rows = db.execute(
        "SELECT token_id, label, scopes, allowed_ips, created_by, created_at, expires_at, "
        "revoked_at, last_used_at, last_used_ip, "
        "(revoked_at IS NULL AND expires_at > NOW() AT TIME ZONE 'UTC') AS active "
        "FROM api_tokens ORDER BY created_at DESC;"
    ).fetchall()
    tokens = [{
        **dict(r),
        "scopes": r["scopes"].split(),
        "allowed_ips": r["allowed_ips"].split(),
        **{k: _iso(r[k]) for k in ("created_at", "expires_at", "revoked_at", "last_used_at")},
    } for r in rows]
    return {"tokens": tokens, "scopes": api_auth.SCOPES}


@router.post("/api/api_tokens")
def create_token(
    request: Request,
    label: str = Form(default=""),
    scopes: str = Form(default=""),
    expires_days: int = Form(default=DEFAULT_DAYS),
    allowed_ips: str = Form(default=""),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Create a key.

    Args:
        label: Who or what it's for, e.g. "Badge app – Sam"; shown in the list and audit log.
        scopes: Space- or comma-separated names from api_auth.SCOPES; at least one.
        expires_days: 1 to MAX_DAYS.
        allowed_ips: Optional space- or comma-separated addresses / CIDRs; blank for any.

    Returns:
        dict with ``token_id``, ``key`` (shown once; never retrievable later) and ``expires_at``.

    Raises:
        HTTPException 400: Blank or over-long label, unknown or no scopes, bad expiry or IP.
    """
    label = " ".join(label.split())
    if not label or len(label) > MAX_LABEL:
        raise HTTPException(status_code=400, detail=f"A label of 1-{MAX_LABEL} characters is required")
    wanted = sorted(set(scopes.replace(",", " ").split()))
    unknown = [s for s in wanted if s not in api_auth.SCOPES]
    if not wanted or unknown:
        raise HTTPException(status_code=400, detail="Pick at least one scope" if not wanted
                            else f"Unknown scope: {', '.join(unknown)}")
    if not 1 <= expires_days <= MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"Expiry must be 1-{MAX_DAYS} days")
    try:
        nets = api_auth.parse_allowed_ips(allowed_ips)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Not an IP address or network: {exc}") \
            from exc

    key = api_auth.new_key()
    token_id = str(uuid.uuid4())
    row = db.execute(
        "INSERT INTO api_tokens (token_id, token_hash, label, scopes, allowed_ips, created_by, "
        "expires_at) VALUES (?, ?, ?, ?, ?, ?, "
        "NOW() AT TIME ZONE 'UTC' + make_interval(days => ?)) RETURNING expires_at;",
        (token_id, api_auth.key_hash(key), label, " ".join(wanted), " ".join(nets),
         s_data["username"], expires_days),
    ).fetchone()
    db.commit()
    audit("api_key.create", request, request.session.get("session_id"),
          f"Created API key {label!r} ({token_id}) with {' '.join(wanted)}")
    return {"token_id": token_id, "key": key, "expires_at": _iso(row["expires_at"])}


@router.post("/api/api_tokens/revoke")
def revoke_token(
    request: Request,
    token_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Revoke a key at once. Revoking one already revoked or expired changes nothing.

    Args:
        request: The incoming request, for the session id and the audit log.
        token_id: The key to revoke.
        s_data: The organizer's session.
        db: The request's database connection.

    Raises:
        HTTPException 404: No such key.
    """
    row = db.execute("SELECT label FROM api_tokens WHERE token_id = ?;", (token_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such key")
    db.execute("UPDATE api_tokens SET revoked_at = NOW() AT TIME ZONE 'UTC' "
               "WHERE token_id = ? AND revoked_at IS NULL;", (token_id,))
    db.commit()
    audit("api_key.revoke", request, request.session.get("session_id"),
          f"Revoked API key {row['label']!r} ({token_id})")
    return {"ok": True}
