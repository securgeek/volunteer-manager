"""Fixed-window rate limiting, per IP and per address hash.

Both buckets matter and for different reasons: the per-IP bucket stops one host walking a
list of candidate addresses, and the per-hash bucket stops a distributed attempt at one
address (and stops a volunteer mailbombing themselves with resends).

Windows are fixed rather than sliding. A sliding window is more accurate and needs a row per
event; at this volume the extra accuracy buys nothing worth the write amplification.

An IPv6 client is counted by its /64, not its address: one connection is normally handed a
whole /64, so per-address buckets would let it pick a fresh address for every request.
"""
import ipaddress
import sqlite3

from clock import now


def _bump(conn: sqlite3.Connection, key: str, window_sec: int, limit: int) -> bool:
    """Increment one bucket. False if it is already at its limit."""
    now_epoch = int(now().timestamp())
    window_start = now_epoch - (now_epoch % window_sec)
    row = conn.execute(
        "SELECT window_start, count FROM rate_limits WHERE bucket_key = ?", (key,)
    ).fetchone()
    if row is None or row["window_start"] != window_start:
        conn.execute(
            "INSERT INTO rate_limits (bucket_key, window_start, count) VALUES (?, ?, 1) "
            "ON CONFLICT(bucket_key) DO UPDATE SET window_start = excluded.window_start, count = 1",
            (key, window_start),
        )
        return True
    if row["count"] >= limit:
        return False
    conn.execute(
        "UPDATE rate_limits SET count = count + 1 WHERE bucket_key = ?", (key,)
    )
    return True


def ip_bucket(ip: str) -> str:
    """The rate-limit identity of a client address: an IPv6 address's /64, otherwise the address.

    An IPv4-mapped IPv6 address counts as its IPv4 address, so one client can't straddle two
    buckets. A value that isn't an address (``"unknown"``) passes through unchanged.

    Args:
        ip: The client address, as ``auth._client_ip`` resolved it.

    Returns:
        The key the per-IP buckets are counted under.
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


def check(conn: sqlite3.Connection, scope: str, ip: str, addr_hash: str,
          per_min: int, per_hour: int) -> bool:
    """Consume one unit from all four buckets. False if any is exhausted.

    Every bucket is bumped even when an earlier one fails, so a caller cannot probe cheaply
    by watching which limit trips first.

    Args:
        conn: The database connection.
        scope: What is being limited, e.g. `request_link`. Each scope has its own buckets.
        ip: The client address. IPv6 is grouped by /64.
        addr_hash: The email address's HMAC.
        per_min: Allowed per minute, per bucket.
        per_hour: Allowed per hour, per bucket.
    """
    ip = ip_bucket(ip)
    results = [
        _bump(conn, f"{scope}:ip:min:{ip}", 60, per_min),
        _bump(conn, f"{scope}:ip:hour:{ip}", 3600, per_hour),
        _bump(conn, f"{scope}:addr:min:{addr_hash}", 60, per_min),
        _bump(conn, f"{scope}:addr:hour:{addr_hash}", 3600, per_hour),
    ]
    return all(results)
