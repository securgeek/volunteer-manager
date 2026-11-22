"""Passkeys (WebAuthn) for organizer accounts — an alternative to the password.

Passwords keep working; a passkey is a second way in. Three properties this file exists to
keep:

* **Nothing stored here can sign anyone in.** The server holds public keys, which only check
  signatures, and challenges, stored as SHA-256.
* **Each challenge verifies once.** It's consumed by one conditional UPDATE before the
  response is checked, so two submissions of the same response resolve by rowcount and a
  captured response can't be replayed.
* **Adding a passkey needs the password again.** A stolen session cookie must not be enough
  to plant a permanent way back in.

The relying party is the organizer site's origin: ADMIN_PUBLIC_URL, or PORTAL_PUBLIC_URL when
the two are served from one origin (local dev). Passkeys are bound to that hostname for good:
change it and every passkey stops working, though passwords still do.
"""
import hashlib
import json
import os
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from webauthn import (
    generate_authentication_options, generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers import (
    base64url_to_bytes, bytes_to_base64url, decode_credential_public_key,
    parse_authentication_credential_json, parse_client_data_json,
    parse_registration_credential_json,
)
from webauthn.helpers.cose import COSEAlgorithmIdentifier as COSE
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, AuthenticatorTransport, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from db import get_db, get_session_data, get_app_kv, RoleChecker, audit, PgClient
from passkey_providers import UNKNOWN_AAGUID, provider_name
from routers.auth import start_session

router = APIRouter()

# Preference order: an authenticator takes the first it supports. Post-quantum ML-DSA first
# (1Password creates these as of late 2026), then the classical algorithms nearly every
# device uses: ES256 (Apple, Google, security keys), EdDSA, and RS256 (Windows Hello).
SUPPORTED_ALGS = [
    COSE.ML_DSA_65, COSE.ML_DSA_44, COSE.ML_DSA_87,
    COSE.ECDSA_SHA_256, COSE.EDDSA, COSE.RSASSA_PKCS1_v1_5_SHA_256,
]
ALG_NAMES = {
    -49: "ML-DSA-65", -48: "ML-DSA-44", -50: "ML-DSA-87",
    -7: "ES256", -8: "EdDSA", -257: "RS256",
}
POST_QUANTUM = {-48, -49, -50}

CHALLENGE_TTL_MIN = 5
NAME_MAX = 60


# ── helpers ───────────────────────────────────────────────────────────────────

def _site() -> tuple[str, str]:
    """(rp_id, origin) from ADMIN_PUBLIC_URL (else PORTAL_PUBLIC_URL), or 503 if passkeys
    can't work here."""
    url = (os.getenv("ADMIN_PUBLIC_URL", "").strip()
           or os.getenv("PORTAL_PUBLIC_URL", "").strip())
    parts = urlsplit(url)
    if not url or parts.scheme not in ("http", "https") or not parts.hostname:
        raise HTTPException(status_code=503, detail="Passkeys are not configured "
                            "(ADMIN_PUBLIC_URL and PORTAL_PUBLIC_URL are unset)")
    # Browsers only allow WebAuthn in a secure context; http is secure only on localhost.
    if parts.scheme == "http" and parts.hostname != "localhost":
        raise HTTPException(status_code=503, detail="Passkeys need the site to be served over https")
    port = f":{parts.port}" if parts.port else ""
    return parts.hostname, f"{parts.scheme}://{parts.hostname}{port}"


def _iso(ts: Optional[datetime]) -> Optional[str]:
    """A naive UTC timestamp as ISO with a Z, so the browser doesn't read it as local time."""
    return ts.isoformat() + "Z" if ts else None


def _require_user(s_data: Optional[dict]) -> dict:
    if not s_data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return s_data


def _challenge_hash(challenge: bytes) -> str:
    return hashlib.sha256(challenge).hexdigest()


def _issue_challenge(db: PgClient, challenge: bytes, purpose: str, user_id: Optional[str]) -> None:
    # Housekeeping rides along: sign-in options need no auth, so expired rows are swept here
    # rather than left to accumulate.
    db.execute("DELETE FROM webauthn_challenges WHERE expires_at < NOW() AT TIME ZONE 'UTC';")
    db.execute(
        "INSERT INTO webauthn_challenges (challenge_hash, purpose, user_id, expires_at) "
        "VALUES (?, ?, ?, NOW() AT TIME ZONE 'UTC' + ? * INTERVAL '1 minute');",
        (_challenge_hash(challenge), purpose, user_id, CHALLENGE_TTL_MIN),
    )
    db.commit()


def _consume_challenge(db: PgClient, client_data_json: bytes, purpose: str,
                       user_id: Optional[str]) -> bytes:
    """Burn the challenge the response claims to answer, and return it for verification.

    Consumed before the signature is checked, and committed either way: a response gets one
    attempt. verify_*_response still compares the challenge, so reading it out of the
    client data first only tells us which row to burn.
    """
    try:
        challenge = parse_client_data_json(client_data_json).challenge
    except (WebAuthnException, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed passkey response") from None
    row = db.execute(
        "UPDATE webauthn_challenges SET used_at = NOW() AT TIME ZONE 'UTC' "
        "WHERE challenge_hash = ? AND purpose = ? AND used_at IS NULL "
        "AND expires_at > NOW() AT TIME ZONE 'UTC' AND user_id IS NOT DISTINCT FROM ? "
        "RETURNING challenge_hash;",
        (_challenge_hash(challenge), purpose, user_id),
    ).fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=400,
                            detail="This passkey request expired or was already used. Try again.")
    return challenge


def _parse(credential: str, parser):
    try:
        return parser(credential)
    except (WebAuthnException, ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="Malformed passkey response") from None


def _transports(stored: str) -> list:
    out = []
    for t in stored.split(","):
        try:
            out.append(AuthenticatorTransport(t))
        except ValueError:
            pass
    return out


def _name(raw: str) -> str:
    """A passkey label: trimmed, single line, at most NAME_MAX characters."""
    return " ".join(raw.split())[:NAME_MAX]


def _out(row: dict) -> dict:
    alg = row["cose_alg"]
    return {
        "id":           bytes_to_base64url(bytes(row["credential_id"])),
        "name":         row["name"],
        "provider":     provider_name(row["aaguid"]),
        "alg":          ALG_NAMES.get(alg, f"COSE {alg}"),
        "post_quantum": alg in POST_QUANTUM,
        "backed_up":    row["backed_up"],
        "created_at":   _iso(row["created_at"]),
        "last_used_at": _iso(row["last_used_at"]),
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/passkeys/config")
def passkey_config():
    """Whether passkeys can work on this deployment. No auth; the login page asks."""
    try:
        _site()
    except HTTPException:
        return {"enabled": False}
    return {"enabled": True}


@router.post("/api/passkeys/register/options")
def register_options(
    request: Request,
    password: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Start adding a passkey to the signed-in account. Requires the current password.

    Any role may add a passkey to its own account. Returns the PublicKeyCredentialCreationOptions
    JSON for navigator.credentials.create().

    Args:
        request: The incoming request, for the session id and the audit log.
        password: The account's current password.
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.

    Raises:
        HTTPException 401: Not signed in.
        HTTPException 403: Password incorrect.
        HTTPException 503: Passkeys not configured.
    """
    user = _require_user(s_data)
    rp_id, _ = _site()
    row = db.execute(
        "SELECT user_id, username, password_hash, webauthn_user_id FROM users WHERE user_id = ?;",
        (user["user_id"],),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        PasswordHasher().verify(row["password_hash"], password)
    except (VerificationError, InvalidHashError):
        audit(request, request.session.get("session_id"),
              "Passkey registration refused: wrong password")
        raise HTTPException(status_code=403, detail="Password is incorrect") from None

    handle = row["webauthn_user_id"]
    if handle is None:
        handle = secrets.token_bytes(32)
        db.execute("UPDATE users SET webauthn_user_id = ? WHERE user_id = ?;",
                   (handle, row["user_id"]))
    existing = db.execute(
        "SELECT credential_id, transports FROM user_passkeys WHERE user_id = ?;",
        (row["user_id"],),
    ).fetchall()

    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=get_app_kv(db, "app_title") or "Conference Scheduler",
        user_name=row["username"],
        user_id=bytes(handle),
        user_display_name=row["username"],
        # Discoverable, so sign-in needs no username; verified, so a passkey alone is
        # possession plus PIN or biometric.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(c["credential_id"]),
                                          transports=_transports(c["transports"]))
            for c in existing
        ],
        supported_pub_key_algs=SUPPORTED_ALGS,
    )
    _issue_challenge(db, opts.challenge, "register", row["user_id"])
    return json.loads(options_to_json(opts))


@router.post("/api/passkeys/register/verify")
def register_verify(
    request: Request,
    credential: str = Form(...),
    name: str = Form(default=""),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Finish adding a passkey: verify the authenticator's response and store the public key.

    Args:
        credential: The navigator.credentials.create() result, as JSON.
        name: A label for the passkey list. Blank means the passkey manager's name, or
            "Passkey" when the authenticator doesn't say which it is.

    Returns:
        The stored passkey as listed by GET /api/passkeys.

    Raises:
        HTTPException 400: Malformed, expired, replayed, or rejected response.
        HTTPException 409: That passkey is already registered.
    """
    user = _require_user(s_data)
    rp_id, origin = _site()
    cred = _parse(credential, parse_registration_credential_json)
    challenge = _consume_challenge(db, cred.response.client_data_json, "register", user["user_id"])
    try:
        v = verify_registration_response(
            credential=cred, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True,
            supported_pub_key_algs=SUPPORTED_ALGS,
        )
    except WebAuthnException as exc:
        audit(request, request.session.get("session_id"), f"Passkey registration rejected: {exc}")
        raise HTTPException(status_code=400, detail=f"Passkey not accepted: {exc}") from exc

    alg = int(decode_credential_public_key(v.credential_public_key).alg)
    # Stored even when unlisted, so a refreshed passkey_providers.py can name it later.
    aaguid = (v.aaguid or "").lower()
    aaguid = None if not aaguid or aaguid == UNKNOWN_AAGUID else aaguid
    provider = provider_name(aaguid)
    label = _name(name) or provider or "Passkey"
    transports = ",".join(t.value for t in (cred.response.transports or []))
    inserted = db.execute(
        "INSERT INTO user_passkeys (credential_id, user_id, public_key, cose_alg, sign_count, "
        "transports, backed_up, name, aaguid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (credential_id) DO NOTHING;",
        (v.credential_id, user["user_id"], v.credential_public_key, alg, v.sign_count,
         transports, v.credential_backed_up, label, aaguid),
    ).rowcount
    db.commit()
    if not inserted:
        raise HTTPException(status_code=409, detail="That passkey is already registered")
    audit(request, request.session.get("session_id"),
          f"Registered passkey {label!r} ({provider or aaguid or 'unknown provider'}, "
          f"{ALG_NAMES.get(alg, alg)})")
    return _out(db.execute("SELECT * FROM user_passkeys WHERE credential_id = ?;",
                           (v.credential_id,)).fetchone())


@router.post("/api/passkeys/login/options")
def login_options(db: PgClient = Depends(get_db)):
    """Start a passkey sign-in. No username: the authenticator offers this site's passkeys.

    Args:
        db: The request's database connection.
    """
    rp_id, _ = _site()
    opts = generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
    )
    _issue_challenge(db, opts.challenge, "login", None)
    return json.loads(options_to_json(opts))


@router.post("/api/passkeys/login/verify")
def login_verify(
    request: Request,
    credential: str = Form(...),
    db: PgClient = Depends(get_db),
):
    """Finish a passkey sign-in and create the same session the password login does.

    Args:
        request: The incoming request, for the new session and the audit log.
        credential: The JSON from `navigator.credentials.get()`.
        db: The request's database connection.

    Returns:
        dict with keys ``username`` and ``role``, as POST /api/login.

    Raises:
        HTTPException 400: Malformed, expired or replayed response.
        HTTPException 401: Unknown passkey, or the signature didn't verify.
    """
    rp_id, origin = _site()
    cred = _parse(credential, parse_authentication_credential_json)
    challenge = _consume_challenge(db, cred.response.client_data_json, "login", None)

    row = db.execute(
        "SELECT p.credential_id, p.public_key, p.cose_alg, p.sign_count, "
        "u.user_id, u.username, u.role, u.webauthn_user_id "
        "FROM user_passkeys p JOIN users u ON u.user_id = p.user_id "
        "WHERE p.credential_id = ?;",
        (cred.raw_id,),
    ).fetchone()
    # A removed passkey still lives on the device, which keeps offering it.
    unknown = HTTPException(status_code=401, detail="Passkey not recognized. Sign in with your "
                            "password, then add the passkey again.")
    if not row:
        audit(request, None, "Passkey sign-in with an unknown passkey")
        raise unknown
    handle = cred.response.user_handle
    if handle is not None and bytes(handle) != bytes(row["webauthn_user_id"]):
        audit(request, None, f"Passkey sign-in for {row['username']!r} with a mismatched user handle")
        raise unknown

    try:
        v = verify_authentication_response(
            credential=cred, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"], require_user_verification=True,
        )
    except WebAuthnException as exc:
        audit(request, None, f"Invalid passkey sign-in for user {row['username']!r}: {exc}")
        raise HTTPException(status_code=401, detail="Passkey sign-in failed") from exc

    db.execute(
        "UPDATE user_passkeys SET sign_count = ?, backed_up = ?, "
        "last_used_at = NOW() AT TIME ZONE 'UTC' WHERE credential_id = ?;",
        (v.new_sign_count, v.credential_backed_up, cred.raw_id),
    )
    result = start_session(request, db, row)  # commits
    audit(request, request.session.get("session_id"),
          f"Signed in with a passkey ({ALG_NAMES.get(row['cose_alg'], row['cose_alg'])})")
    return result


@router.get("/api/passkeys")
def list_passkeys(
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """The signed-in user's own passkeys, newest first.

    Args:
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.
    """
    user = _require_user(s_data)
    rows = db.execute(
        "SELECT * FROM user_passkeys WHERE user_id = ? ORDER BY created_at DESC;",
        (user["user_id"],),
    ).fetchall()
    return [_out(r) for r in rows]


@router.post("/api/passkeys/delete")
def delete_passkey(
    request: Request,
    credential_id: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Remove one of the signed-in user's own passkeys. HTTP 204, or 404 if it isn't theirs.

    Args:
        request: The incoming request, for the session id and the audit log.
        credential_id: The passkey's credential id, base64url.
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.
    """
    user = _require_user(s_data)
    try:
        cid = base64url_to_bytes(credential_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid passkey id") from None
    row = db.execute(
        "DELETE FROM user_passkeys WHERE credential_id = ? AND user_id = ? RETURNING name;",
        (cid, user["user_id"]),
    ).fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Passkey not found")
    audit(request, request.session.get("session_id"), f"Removed passkey {row['name']!r}")
    return Response(status_code=204)


@router.post("/api/passkeys/rename")
def rename_passkey(
    request: Request,
    credential_id: str = Form(...),
    name: str = Form(...),
    s_data: Optional[dict] = Depends(get_session_data),
    db: PgClient = Depends(get_db),
):
    """Rename one of the signed-in user's own passkeys.

    Args:
        request: The incoming request, for the session id and the audit log.
        credential_id: The passkey's credential id, base64url.
        name: The new name.
        s_data: The caller's session, or `None` when signed out.
        db: The request's database connection.

    Returns:
        The passkey as listed by GET /api/passkeys.

    Raises:
        HTTPException 400: Blank name or bad id.
        HTTPException 404: Not one of theirs.
    """
    user = _require_user(s_data)
    label = _name(name)
    if not label:
        raise HTTPException(status_code=400, detail="Name can't be blank")
    try:
        cid = base64url_to_bytes(credential_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid passkey id") from None
    old = db.execute(
        "SELECT name FROM user_passkeys WHERE credential_id = ? AND user_id = ?;",
        (cid, user["user_id"]),
    ).fetchone()
    if not old:
        raise HTTPException(status_code=404, detail="Passkey not found")
    row = db.execute(
        "UPDATE user_passkeys SET name = ? WHERE credential_id = ? AND user_id = ? RETURNING *;",
        (label, cid, user["user_id"]),
    ).fetchone()
    db.commit()
    audit(request, request.session.get("session_id"),
          f"Renamed passkey {old['name']!r} to {label!r}")
    return _out(row)


@router.post("/api/passkeys/clear_user")
def clear_user_passkeys(
    request: Request,
    user_id: str = Form(...),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Remove every passkey on an account — for someone who lost a device. Organizer only.

    Args:
        request: The incoming request, for the session id and the audit log.
        user_id: The account to clear.
        s_data: The organizer's session.
        db: The request's database connection.

    Returns:
        dict ``{"removed": n}``.
    """
    target = db.execute("SELECT username FROM users WHERE user_id = ?;", (user_id,)).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    removed = db.execute("DELETE FROM user_passkeys WHERE user_id = ?;", (user_id,)).rowcount
    db.commit()
    audit(request, request.session.get("session_id"),
          f"Removed {removed} passkey(s) from user {target['username']!r}")
    return {"removed": removed}
