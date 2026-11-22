"""Hashing, tokens, email encryption, and sync request signing.

Three separate jobs live here and it is worth keeping them straight:

* ``email_hmac`` is a deterministic index. It exists so the portal can find a volunteer by
  an address it cannot read, and its key is env-only (never in the database).
* ``email_enc`` is RSA-OAEP ciphertext. The private key is offline; nothing in this process
  can decrypt it, by design.
* The sync signature authenticates the scheduler to the portal. Unrelated to the two above,
  and keyed separately.
"""
import base64
import hashlib
import hmac
import secrets
from typing import Optional

# ── email_hmac — deterministic lookup ─────────────────────────────────────────

HMAC_KEY_CHECK = b"bsc-hmac-key-check"


def normalize_email(email: str) -> str:
    """Strip and lowercase, and deliberately nothing else.

    No Gmail dot-stripping and no plus-tag removal: both would silently merge addresses
    that are genuinely distinct at some providers, and a wrong merge here means one person
    holding another's signup.

    Args:
        email: The address as typed.
    """
    return email.strip().lower()


def email_hmac(key: bytes, email: str) -> str:
    """The lookup index for an address: HMAC-SHA256 of its normalized form.

    Args:
        key: The `EMAIL_HMAC_KEY` the scheduler shares.
        email: The address, in any case or spacing.

    Returns:
        The hex digest.
    """
    return hmac.new(key, normalize_email(email).encode(), hashlib.sha256).hexdigest()


def hmac_key_fp(key: bytes) -> str:
    """A proof-of-possession digest of the HMAC key, safe to publish.

    The scheduler compares this against its own to catch the two services holding different
    keys — which would otherwise show up as organizer-created volunteers mysteriously unable
    to sign in. It is an HMAC of a fixed string rather than a hash of the key, so publishing
    it reveals nothing that helps an attacker who steals the digest.

    Args:
        key: The HMAC key.
    """
    return hmac.new(key, HMAC_KEY_CHECK, hashlib.sha256).hexdigest()


# ── tokens and session ids — stored hashed, always ────────────────────────────

def new_secret(nbytes: int = 32) -> str:
    """A URL-safe random secret. The plaintext is handed out once and never stored.

    Args:
        nbytes: Random bytes before encoding.
    """
    return secrets.token_urlsafe(nbytes)


def hash_secret(secret: str) -> str:
    """SHA-256 of a token or session id. A read of the database must not mint a login.

    Args:
        secret: The token or session id.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def compare(a: str, b: str) -> bool:
    """Compare two strings in constant time.

    Args:
        a: One value.
        b: The other.

    Returns:
        Whether they are equal.
    """
    return hmac.compare_digest(a, b)


# ── email_enc — RSA-OAEP under the offline key ────────────────────────────────

def pem_to_der(pem: str) -> bytes:
    """Strip PEM armour and base64-decode, matching EmailKeyManager.vue's pem_to_der.

    Args:
        pem: A PEM block.
    """
    body = []
    for line in pem.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("-----"):
            continue
        body.append(line)
    return base64.b64decode("".join(body))


def pubkey_fingerprint(pem: str) -> str:
    """``SHA256:<b64 of sha256(SPKI DER)>`` with padding stripped.

    Identical to the format EmailKeyManager.vue already displays, so an organizer can
    eyeball the portal's key against the scheduler's without converting anything.

    Args:
        pem: A PEM public key.
    """
    digest = hashlib.sha256(pem_to_der(pem)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def encrypt_email(pubkey_pem: str, email: str) -> str:
    """RSA-OAEP(SHA-256) the normalized address; returns base64, as the scheduler stores it.

    Args:
        pubkey_pem: The scheduler's RSA public key, PEM.
        email: The address, normalized before encrypting.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_public_key(pubkey_pem.encode())
    ct = key.encrypt(
        normalize_email(email).encode(),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(ct).decode()


def decrypt_email(privkey_pem: str, email_enc: str) -> str:
    """Only ever used by the offline tool and by tests. No server path calls this.

    Args:
        privkey_pem: The RSA private key, PEM.
        email_enc: The base64 ciphertext.

    Returns:
        The address.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key = serialization.load_pem_private_key(privkey_pem.encode(), password=None)
    pt = key.decrypt(
        base64.b64decode(email_enc),
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                     algorithm=hashes.SHA256(), label=None),
    )
    return pt.decode()


# ── sync request signing (§2) ─────────────────────────────────────────────────

def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    """The bytes actually signed.

    Path and body hash are in here so a captured request cannot be replayed against a
    different endpoint or with altered contents; the timestamp bounds how long a capture is
    worth anything at all.

    Args:
        method: The HTTP method.
        path: The path, with any query string.
        timestamp: The request's timestamp header.
        nonce: The request's nonce header.
        body: The raw body bytes.
    """
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return "\n".join([method.upper(), path, str(timestamp), nonce, body_hash])


def sign_request(secret: bytes, method: str, path: str, timestamp: str,
                 nonce: str, body: bytes) -> str:
    """Sign one sync API request.

    Args:
        secret: The shared sync secret.
        method: The HTTP method.
        path: The path, with any query string.
        timestamp: The timestamp sent in the header.
        nonce: The nonce sent in the header.
        body: The raw body bytes.

    Returns:
        The hex HMAC-SHA256 of `canonical_string`.
    """
    msg = canonical_string(method, path, timestamp, nonce, body)
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()


def verify_request(candidates: list[bytes], signature: str, method: str, path: str,
                   timestamp: str, nonce: str, body: bytes) -> bool:
    """True if the signature matches under any candidate secret.

    A list rather than one secret so a key rotation can accept both the new and previous
    secret for as long as it takes to restart the other service.

    Args:
        candidates: Secrets to try, from `Settings.secrets_for`.
        signature: The signature header.
        method: The HTTP method.
        path: The path, with any query string.
        timestamp: The timestamp header.
        nonce: The nonce header.
        body: The raw body bytes.
    """
    for secret in candidates:
        expected = sign_request(secret, method, path, timestamp, nonce, body)
        if hmac.compare_digest(expected, signature or ""):
            return True
    return False


def new_nonce() -> str:
    """A fresh request nonce.

    Returns:
        32 random hex characters.
    """
    return secrets.token_hex(16)


def content_hash(*parts: Optional[str | bytes]) -> str:
    """Stable hash over a push payload, so an unchanged push is a cheap no-op.

    Args:
        *parts: Strings or bytes. `None` hashes differently from empty.
    """
    h = hashlib.sha256()
    for part in parts:
        if part is None:
            h.update(b"\x00")
        elif isinstance(part, bytes):
            h.update(b"b"); h.update(part)
        else:
            h.update(b"s"); h.update(part.encode())
        h.update(b"\x1e")
    return h.hexdigest()
