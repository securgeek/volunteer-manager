"""Shift check-in codes and the early/late rule. No database, so it can be unit-tested alone.

A code is ``BSC1.{slot_key}.{b64url(vol_id)}.{mac}``, where mac is HMAC-SHA256 under
``app_kv.checkin_key``, truncated to 128 bits. It is deterministic: the same volunteer and slot
always give the same code, so a re-sent schedule email (draft, then final) doesn't void the
first one. A valid code is not enough on its own to check in; routers/checkin.py also requires
the volunteer to still be assigned to that slot, which is what voids a code on reassignment.
Rotating the key voids every code at once.
"""
import base64
import hashlib
import hmac
import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

PREFIX = "BSC1"
MAC_BYTES = 16
MAX_CODE_LEN = 300

EARLY_MINUTES = 45
LATE_MINUTES = 10

DEFAULT_TZ = "America/New_York"

_SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
_RANGE = re.compile(r"^\s*(\d{3,4})\s*-\s*(\d{3,4})")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, vol_id: str, slot_key: str) -> str:
    # Newline-separated so no vol_id / slot_key pair can collide with another.
    msg = f"{PREFIX}\n{vol_id}\n{slot_key}".encode()
    return _b64(hmac.new(key, msg, hashlib.sha256).digest()[:MAC_BYTES])


def make_code(key: bytes, vol_id: str, slot_key: str) -> str:
    """The check-in code for one volunteer's shift.

    Args:
        key: The check-in HMAC key from `app_kv`.
        vol_id: The volunteer's id.
        slot_key: The shift's `{day_key}_{time_key}_{pos_key}`.

    Returns:
        `PREFIX.slot_key.base64(vol_id).mac`, which goes into the QR code.
    """
    return f"{PREFIX}.{slot_key}.{_b64(vol_id.encode())}.{_mac(key, vol_id, slot_key)}"


def verify_code(key: bytes, code: str) -> Optional[tuple]:
    """Return (vol_id, slot_key) for a genuine code, or None.

    Every way of failing (malformed, wrong key, tampered) returns the same None, so a caller
    can't be told which check rejected it.

    Args:
        key: The current check-in HMAC key.
        code: What the scanner read or the organizer typed. Surrounding whitespace is ignored.
    """
    code = (code or "").strip()
    if not code or len(code) > MAX_CODE_LEN:
        return None
    parts = code.split(".")
    if len(parts) != 4 or parts[0] != PREFIX or not _SLOT_RE.match(parts[1]):
        return None
    _, slot_key, vol_b64, mac = parts
    try:
        vol_id = _unb64(vol_b64).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if not vol_id or not hmac.compare_digest(mac, _mac(key, vol_id, slot_key)):
        return None
    return vol_id, slot_key


def shift_start(event_date: Optional[str], time_str: Optional[str], tz: str) -> Optional[datetime]:
    """When a shift starts, as an aware datetime, or None when it can't be told.

    event_date is ISO ``YYYY-MM-DD`` (days_table, optional); time_str is the
    ``'0800-1200'`` range from times_table, read the same way as routers/schedule.time_range.

    Args:
        event_date: The day's ISO date, or `None` when the day has none set.
        time_str: The shift's time range.
        tz: The event's IANA timezone name, from App Settings.
    """
    if not event_date:
        return None
    m = _RANGE.match(time_str or "")
    if not m:
        return None
    try:
        day = date.fromisoformat(event_date.strip())
        hhmm = m.group(1).zfill(4)
        hour, minute = int(hhmm[:2]), int(hhmm[2:])
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(tz))
    except (ValueError, KeyError):
        return None


def shift_end(start: Optional[datetime], time_str: Optional[str]) -> Optional[datetime]:
    """When a shift ends, given its start; a range like 2200-0200 runs past midnight.

    Args:
        start: The shift's start, from `shift_start`.
        time_str: The shift's time range, like `'0800-1200'`.

    Returns:
        The end as an aware datetime, or `None` without a start or a readable range.
    """
    m = _RANGE.match(time_str or "")
    if not start or not m:
        return None
    a, b = (int(x.zfill(4)[:2]) * 60 + int(x.zfill(4)[2:]) for x in m.groups())
    length = b - a if b > a else b - a + 24 * 60
    return start + timedelta(minutes=length)


def classify(now: datetime, start: Optional[datetime],
             early: int = EARLY_MINUTES, late: int = LATE_MINUTES) -> tuple:
    """('early' | 'late' | 'on_time' | 'unknown', minutes).

    minutes is how far before (early) or after (late) the start, rounded down; 0 otherwise.
    Exactly 45 minutes early or 10 minutes late still counts as on time.

    Args:
        now: The current time, aware.
        start: The shift's start from `shift_start`. `None` gives `'unknown'`.
        early: Minutes before the start that still count as on time.
        late: Minutes after the start that still count as on time.
    """
    if start is None:
        return "unknown", 0
    delta = (now - start).total_seconds() / 60
    if delta < -early:
        return "early", int(-delta)
    if delta > late:
        return "late", int(delta)
    return "on_time", 0
