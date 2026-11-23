"""`ratelimit.SlidingWindow`, with a fake clock. No database.

    cd backend && pytest test_ratelimit.py -v
"""
import pytest

import ratelimit
from ratelimit import SlidingWindow


@pytest.fixture
def clock(monkeypatch):
    """A settable stand-in for time.monotonic."""
    class Clock:
        """The current fake time, in seconds."""

        now = 1000.0
    c = Clock()
    monkeypatch.setattr(ratelimit.time, "monotonic", lambda: c.now)
    return c


def test_allow_counts_up_to_the_limit_then_refuses(clock):
    w = SlidingWindow(3, 60)
    assert [w.allow("a") for _ in range(4)] == [True, True, True, False]
    assert w.allow("b")                                  # keys are independent


def test_a_refused_allow_records_nothing_against_any_key(clock):
    w = SlidingWindow(1, 60)
    assert w.allow("a")
    assert not w.allow("a", "b")                         # "a" is full, so "b" isn't counted
    assert w.allow("b")


def test_events_age_out_of_the_window(clock):
    w = SlidingWindow(2, 60)
    w.hit("a")
    clock.now += 30
    w.hit("a")
    assert w.over("a")
    clock.now += 30.5                                    # the first one is now 60.5 s old
    assert not w.over("a")


def test_over_and_hit_are_separate(clock):
    w = SlidingWindow(2, 60)
    assert not w.over("a") and not w.over("a")           # checking records nothing
    w.hit("a", "b")
    w.hit("a")
    assert w.over("a") and not w.over("b")
    assert w.over("b", "a")                              # any key over is enough


def test_retry_after_is_when_the_count_drops_under_the_limit(clock):
    w = SlidingWindow(2, 60)
    assert w.retry_after("a") == 0
    w.hit("a")
    clock.now += 10
    w.hit("a")
    clock.now += 5
    assert w.retry_after("a") == 45                      # the first hit expires at +60
    w.hit("a")                                           # three now; two must go
    assert w.retry_after("a") == 55


def test_clear_forgets_one_key_or_all(clock):
    w = SlidingWindow(1, 60)
    w.hit("a", "b")
    w.clear("a")
    assert not w.over("a") and w.over("b")
    w.clear()
    assert not w.over("b")


def test_expired_keys_are_dropped_so_memory_does_not_grow(clock):
    w = SlidingWindow(5, 60)
    for i in range(1000):
        w.hit(f"one-off-{i}")
    clock.now += 61
    for i in range(1000):
        w.over(f"one-off-{i}")
    assert not w._hits
