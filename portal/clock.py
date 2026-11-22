"""One place that decides what 'now' means, so tests can move it.

Every timestamp the portal writes is UTC ISO-8601 with a trailing Z. The scheduler already
serves its timestamps that way, and doing the same here means elapsed times are right in
any timezone rather than right only where the container happens to run.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class _Clock:
    """Module state: how far tests have moved the clock from real time."""

    offset: timedelta = field(default_factory=timedelta)


_clock = _Clock()


def now() -> datetime:
    """The current time, UTC, plus any offset a test applied with `shift`.

    Returns:
        An aware datetime.
    """
    return datetime.now(timezone.utc) + _clock.offset


def now_iso() -> str:
    """The current time as the portal stores it.

    Returns:
        `YYYY-MM-DDTHH:MM:SSZ`, UTC.
    """
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(dt: datetime) -> str:
    """Format a datetime the way the portal stores times.

    Args:
        dt: An aware datetime, in any timezone.

    Returns:
        `YYYY-MM-DDTHH:MM:SSZ`, converted to UTC.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    """Read a time the portal stored.

    Args:
        s: `YYYY-MM-DDTHH:MM:SSZ`.

    Returns:
        An aware UTC datetime.

    Raises:
        ValueError: When `s` isn't in that format.
    """
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def in_future(minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    """A stored-format time this far from now, for expiry columns.

    Args:
        minutes: Minutes to add.
        hours: Hours to add.
        days: Days to add.

    Returns:
        `YYYY-MM-DDTHH:MM:SSZ`, UTC.
    """
    return iso(now() + timedelta(minutes=minutes, hours=hours, days=days))


def shift(**kwargs) -> None:
    """Move the clock, for tests. shift(minutes=20) then shift(minutes=-20) to undo.

    Args:
        **kwargs: Any `timedelta` arguments, e.g. `minutes=20`.
    """
    _clock.offset += timedelta(**kwargs)


def reset() -> None:
    """Put the clock back to real time."""
    _clock.offset = timedelta(0)
