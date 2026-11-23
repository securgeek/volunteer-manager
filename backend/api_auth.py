"""Bearer keys for the read-only export API (routers/export.py; managed in routers/api_tokens.py).

The admin hostname is behind an access gate at the edge, but ``/api/export/*`` bypasses it so servers
can call it, so the export API is reachable from anywhere. Hence:

- A key is ``bsc_`` + 32 random bytes, shown once; only its SHA-256 is stored (api_tokens).
- Every failure is the same bare 401 (no key, unknown, expired, revoked, wrong IP, missing
  scope), so a caller can't learn which check rejected it.
- Failed attempts are limited per client IP in memory (one worker); over the limit is a 429
  before the key is even looked at.
- Every accepted call is audited under the key's label and id, never the key itself.

Scopes are additive. The other team's app needs schedule:read and volunteers:read; the mailer
(tools/vol_mail) also needs volunteers:email and checkin:codes.
"""
import hashlib
import ipaddress
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import Depends, HTTPException, Request

from audit import audit, client_ip  # client_ip re-exported for callers
from db import PgClient, get_db

PREFIX = "bsc_"

SCOPES = {
    "schedule:read":    "Every slot, with the assigned volunteer's id and name",
    "volunteers:read":  "Every volunteer's id, name and shifts",
    "volunteers:email": "Adds each volunteer's encrypted email (ciphertext only)",
    "checkin:codes":    "Adds each shift's check-in code",
}

FAIL_LIMIT, FAIL_WINDOW_SEC = 10, 15 * 60

_fails: dict = defaultdict(deque)
_fails_lock = threading.Lock()


def new_key() -> str:
    """Generate a new export API key.

    The key is shown to the organizer once. Only its `key_hash` is stored.

    Returns:
        `PREFIX` followed by 32 random bytes, URL-safe base64 encoded.
    """
    return PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    """Hash an API key for storage and lookup.

    Args:
        key: The full bearer key, prefix included.

    Returns:
        The SHA-256 hex digest. A plain hash is enough because the key is random, not a password.
    """
    return hashlib.sha256(key.encode()).hexdigest()


def rate_key(ip: str) -> str:
    """The identity a client is rate limited under: an IPv6 address's /64, otherwise the address.

    One IPv6 connection is normally handed a whole /64, so counting single addresses would let
    a caller take a fresh one per request. An IPv4-mapped address counts as its IPv4 address; a
    value that isn't an address passes through. Only for counting: allow-lists, audit and
    ``last_used_ip`` keep the exact address. ``routers/password_reset.py`` uses it too, and
    the portal has its own copy (``portal/ratelimit.py``), as the apps share no code.

    Args:
        ip: The client address, as ``client_ip`` resolved it.

    Returns:
        The rate-limit key.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        # From the integer, so a zone suffix (fe80::1%eth0) can't upset the parse.
        return str(ipaddress.IPv6Network((int(addr) >> 64 << 64, 64)))
    return str(addr)


def parse_allowed_ips(text: str) -> list:
    """Space- or comma-separated addresses / CIDRs, normalised; raises ValueError on a bad one.

    Args:
        text: What the organizer typed, e.g. `"203.0.113.7, 2001:db8::/48"`. Empty is allowed.

    Returns:
        The networks as normalised CIDR strings. A bare address becomes a /32 or /128.

    Raises:
        ValueError: When any part isn't an address or network.
    """
    nets = []
    for part in (text or "").replace(",", " ").split():
        nets.append(str(ipaddress.ip_network(part, strict=False)))
    return nets


def ip_allowed(ip: str, allowed: str) -> bool:
    """True when allowed is empty (any address) or ip falls in one of its networks.

    Args:
        ip: The caller's address from `client_ip`.
        allowed: The key's stored allow-list, space-separated CIDRs.

    Returns:
        Whether the call may go ahead. An address that doesn't parse is refused.
    """
    nets = (allowed or "").split()
    if not nets:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(n) for n in nets)


def _over_limit(ip: str) -> bool:
    now = time.monotonic()
    with _fails_lock:
        q = _fails[rate_key(ip)]
        while q and q[0] <= now - FAIL_WINDOW_SEC:
            q.popleft()
        return len(q) >= FAIL_LIMIT


def _record_fail(ip: str) -> None:
    with _fails_lock:
        _fails[rate_key(ip)].append(time.monotonic())


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Unauthorized",
                         headers={"WWW-Authenticate": "Bearer"})


class TokenChecker:
    """Depends: the calling key's row (with ``scopes`` as a set), or 401/429.

    Example: ``key: dict = Depends(TokenChecker("schedule:read"))``.
    """

    def __init__(self, scope: str):
        assert scope in SCOPES, scope
        self.scope = scope

    def __call__(self, request: Request, db: PgClient = Depends(get_db)) -> dict:
        ip = client_ip(request)
        if _over_limit(ip):
            raise HTTPException(status_code=429, detail="Too many failed attempts. Try later.")
        header = request.headers.get("authorization", "")
        scheme, _, key = header.partition(" ")
        row: Optional[dict] = None
        if scheme.lower() == "bearer" and key.strip().startswith(PREFIX):
            row = db.execute(
                "SELECT token_id, label, scopes, allowed_ips FROM api_tokens "
                "WHERE token_hash = ? AND revoked_at IS NULL "
                "AND expires_at > NOW() AT TIME ZONE 'UTC';",
                (key_hash(key.strip()),)
            ).fetchone()
        scopes = set((row or {}).get("scopes", "").split())
        if not row or self.scope not in scopes or not ip_allowed(ip, row["allowed_ips"]):
            _record_fail(ip)
            audit("api_key.refused", request, None,
                  f"Export API refused: {request.method} {request.url.path}",
                  token_id=row["token_id"] if row else None)
            raise _unauthorized()

        db.execute("UPDATE api_tokens SET last_used_at = NOW() AT TIME ZONE 'UTC', "
                   "last_used_ip = ? WHERE token_id = ?;", (ip, row["token_id"]))
        db.commit()
        audit("api_key.use", request, None, f"{request.method} {request.url.path}",
              who=f"API key {row['label']!r} ({row['token_id']})", token_id=row["token_id"])
        return {**row, "scopes": scopes}
