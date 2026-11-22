"""compute_remaining against a REAL database (in-memory SQLite), not a stub.

test_portal_sync.py's FakeDB matches SQL by substring and never runs it, which is how a
missing predicate once survived review. This runs the actual query.

    pytest backend/test_remaining_sql.py -v
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("SYNC_KEY_ID", "k")
os.environ.setdefault("SYNC_SECRET", "s")

import portal_sync                                             # noqa: E402

SCHEMA = """
CREATE TABLE app_kv     (app_key TEXT PRIMARY KEY, val TEXT NOT NULL);
CREATE TABLE volunteers (vol_id TEXT PRIMARY KEY, name TEXT, status TEXT NOT NULL DEFAULT 'active',
                         cancelled_at TEXT, inactive_at TEXT,
    -- The who-counts columns, as in scheduler.sql (SQLite supports generated columns too).
    is_live BOOLEAN GENERATED ALWAYS AS (cancelled_at IS NULL AND inactive_at IS NULL),
    is_counted BOOLEAN GENERATED ALWAYS AS (status IN ('pending', 'active')
                                           AND cancelled_at IS NULL AND inactive_at IS NULL),
    is_assignable BOOLEAN GENERATED ALWAYS AS (status = 'active'
                                              AND cancelled_at IS NULL AND inactive_at IS NULL));
CREATE TABLE schedule (
    day_key TEXT, time_key TEXT, pos_key TEXT, vol_id TEXT,
    PRIMARY KEY (day_key, time_key, pos_key)
);
CREATE TABLE vol_picks (
    vol_id TEXT, day_key TEXT, time_key TEXT, kind TEXT NOT NULL, rank INTEGER,
    PRIMARY KEY (vol_id, day_key, time_key, kind)
);
"""

# The 2027 shape: three overlapping starts a day.
SLOTS = {("sat", "0800"): 10, ("sat", "1130"): 8, ("sat", "1500"): 6,
         ("sun", "0800"): 4, ("sun", "1130"): 4, ("sun", "1400"): 3}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for (day, time), n in SLOTS.items():
        for i in range(n):
            conn.execute("INSERT INTO schedule VALUES (?, ?, ?, NULL)", (day, time, f"p{i}"))
    conn.commit()
    return conn


def _pick(conn, vol_id, day, time, kind="pref", rank=1, status="active"):
    conn.execute("INSERT OR IGNORE INTO volunteers (vol_id, name, status) VALUES (?, ?, ?)",
                 (vol_id, vol_id, status))
    conn.execute("INSERT INTO vol_picks VALUES (?, ?, ?, ?, ?)", (vol_id, day, time, kind, rank))
    conn.commit()


def _assign(conn, vol_id, day, time):
    conn.execute(
        "UPDATE schedule SET vol_id = ? WHERE day_key = ? AND time_key = ? AND pos_key = "
        "(SELECT pos_key FROM schedule WHERE day_key = ? AND time_key = ? AND vol_id IS NULL "
        " LIMIT 1)", (vol_id, day, time, day, time))
    conn.commit()


def _overage(conn, pct):
    conn.execute("INSERT OR REPLACE INTO app_kv VALUES ('portal_overage_pct', ?)", (str(pct),))
    conn.commit()


def test_has_table_falls_back_to_sqlite_master(db):
    assert portal_sync._has_table(db, "vol_picks") is True
    assert portal_sync._has_table(db, "no_such_table") is False


def test_with_no_overage_remaining_is_the_slots(db):
    assert portal_sync.compute_remaining(db) == SLOTS


def test_each_timeslot_counts_only_its_own_day(db):
    """sat 0800 and sun 0800 share a time_key; a pick on one must not touch the other."""
    _pick(db, "v1", "sat", "0800")
    r = portal_sync.compute_remaining(db)
    assert r[("sat", "0800")] == 9 and r[("sun", "0800")] == 4


def test_only_live_pending_and_approved_prefs_count(db):
    _pick(db, "a", "sat", "1130", status="active")
    _pick(db, "p", "sat", "1130", status="pending")
    _pick(db, "c", "sat", "1130", status="active")
    _pick(db, "i", "sat", "1130", status="active")
    db.execute("UPDATE volunteers SET cancelled_at = 'x' WHERE vol_id = 'c'")
    db.execute("UPDATE volunteers SET inactive_at = 'x' WHERE vol_id = 'i'")
    db.commit()
    _pick(db, "x", "sat", "1130", kind="add", rank=None)     # extras don't consume
    assert portal_sync.compute_remaining(db)[("sat", "1130")] == 8 - 2


def test_assigning_someone_where_they_picked_leaves_remaining_unchanged(db):
    _pick(db, "v1", "sat", "1500")
    before = portal_sync.compute_remaining(db)[("sat", "1500")]
    _assign(db, "v1", "sat", "1500")
    assert portal_sync.compute_remaining(db)[("sat", "1500")] == before == 5


def test_a_pick_stays_demand_until_assigned_on_that_day_and_time(db):
    _pick(db, "v1", "sat", "0800")
    _assign(db, "v1", "sun", "0800")            # same time, other day: still owed sat 0800
    r = portal_sync.compute_remaining(db)
    assert r[("sat", "0800")] == 9
    assert r[("sun", "0800")] == 3              # and sun lost a slot to the assignment


def test_assigning_a_non_picker_uses_up_a_slot(db):
    _assign(db, "walk-in", "sun", "1400")
    assert portal_sync.compute_remaining(db)[("sun", "1400")] == 2


@pytest.mark.parametrize("pct, want", [(0, 10), (25, 12), (50, 15), (100, 20), (200, 30)])
def test_overage_scales_capacity_and_rounds_down(db, pct, want):
    _overage(db, pct)
    assert portal_sync.compute_remaining(db)[("sat", "0800")] == want
    assert portal_sync.compute_remaining(db)[("sun", "1400")] == (3 * (100 + pct)) // 100


def test_overage_is_clamped_and_garbage_reads_as_zero(db):
    _overage(db, 999)
    assert portal_sync.overage_pct(db) == portal_sync.OVERAGE_MAX
    _overage(db, "lots")
    assert portal_sync.overage_pct(db) == 0
    _overage(db, -10)
    assert portal_sync.overage_pct(db) == 0


def test_remaining_never_goes_negative(db):
    for i in range(7):
        _pick(db, f"v{i}", "sun", "1400")
    assert portal_sync.compute_remaining(db)[("sun", "1400")] == 0
