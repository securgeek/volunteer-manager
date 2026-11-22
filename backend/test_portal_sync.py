"""Scheduler-side sync tests that run with no Postgres and no portal.

The seam worth testing hardest is the signing contract: `portal_sync._sign` and the portal's
`security.verify_request` are two independent implementations of one canonical string, in two
services that share no code by design. If they disagree by a newline, every sync request 401s
and the symptom ("Unauthorized") says nothing about the cause. So these tests sign with the
scheduler's code and verify with the portal's.

    pytest backend/test_portal_sync.py -v

What is NOT covered: anything needing Postgres (the pull's upsert, portal_sync_state
bookkeeping) or a live portal. Those need the stack up.
"""
import hashlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PORTAL = os.path.join(os.path.dirname(HERE), "portal")
sys.path.insert(0, HERE)
sys.path.insert(0, PORTAL)

os.environ.setdefault("PORTAL_SYNC_URL", "https://portal.test")
os.environ.setdefault("SYNC_KEY_ID", "test-key")
os.environ.setdefault("SYNC_SECRET", "s3cret-for-tests")
os.environ.setdefault("EMAIL_HMAC_KEY", "0" * 64)

import portal_sync                                    # noqa: E402


@pytest.fixture(scope="module")
def portal_security():
    """The portal's own security module — the other half of the contract."""
    os.environ["PORTAL_SKIP_CONFIG"] = "1"
    sys.modules.pop("security", None)
    import security
    return security


# ── a stand-in for PgClient, so these tests need no database ───────────────────

class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """Answers queries by substring match. Crude on purpose: it keeps the tests readable."""

    def __init__(self, responses: dict, tables=()):
        self.responses = responses
        self.tables = set(tables)
        self.seen = []

    def execute(self, sql, params=()):
        self.seen.append(" ".join(sql.split()))
        if "information_schema.tables" in sql:
            return FakeCursor([{"exists": 1}] if params and params[0] in self.tables else [])
        for needle, rows in self.responses.items():
            if needle in " ".join(sql.split()):
                return FakeCursor(rows)
        return FakeCursor([])

    def commit(self):
        pass


# ── the signing contract ──────────────────────────────────────────────────────

def test_scheduler_signature_verifies_with_the_portals_verifier(portal_security):
    """The whole point: two implementations, one canonical string."""
    body = json.dumps({"op_id": "x", "days": []}).encode()
    headers = portal_sync._sign("POST", "/api/sync/schedule", body)
    assert portal_security.verify_request(
        [os.environ["SYNC_SECRET"].encode()],
        headers["x-sync-signature"], "POST", "/api/sync/schedule",
        headers["x-sync-timestamp"], headers["x-sync-nonce"], body,
    )


def test_signature_covers_the_query_string(portal_security):
    """A pull signs its cursor. If only the path were signed, ?since= would be unauthenticated."""
    target = "/api/sync/volunteers?since=42&limit=500"
    headers = portal_sync._sign("GET", target, b"")
    secret = [os.environ["SYNC_SECRET"].encode()]
    assert portal_security.verify_request(
        secret, headers["x-sync-signature"], "GET", target,
        headers["x-sync-timestamp"], headers["x-sync-nonce"], b"")
    # Same signature against a different cursor must fail.
    assert not portal_security.verify_request(
        secret, headers["x-sync-signature"], "GET", "/api/sync/volunteers?since=99&limit=500",
        headers["x-sync-timestamp"], headers["x-sync-nonce"], b"")


def test_empty_body_hashes_identically_on_both_sides(portal_security):
    """A GET has no body; both sides must hash b'' the same way, not skip the field."""
    headers = portal_sync._sign("GET", "/api/sync/heartbeat", b"")
    assert portal_security.verify_request(
        [os.environ["SYNC_SECRET"].encode()], headers["x-sync-signature"],
        "GET", "/api/sync/heartbeat", headers["x-sync-timestamp"],
        headers["x-sync-nonce"], b"")


def test_nonces_do_not_repeat():
    seen = {portal_sync._sign("GET", "/x", b"")["x-sync-nonce"] for _ in range(200)}
    assert len(seen) == 200


def test_hmac_key_fp_matches_the_portals(portal_security):
    """Both services derive this from the same env var; a mismatch is a misconfiguration."""
    assert portal_sync.hmac_key_fp() == portal_security.hmac_key_fp(
        os.environ["EMAIL_HMAC_KEY"].encode())


# ── snapshot construction ─────────────────────────────────────────────────────

def test_snapshot_hash_is_stable_and_content_sensitive():
    responses = {
        "FROM days_table": [{"day_key": "sat", "day_str": "Saturday",
                             "event_date": "2026-05-09", "sort_order": 0}],
        "FROM times_table": [{"time_key": "0800", "time_str": "0800-1200", "sort_order": 0}],
        "FROM day_times": [{"day_key": "sat", "time_key": "0800"}],
        "FROM categories": [],
        "FROM positions": [{"pos_key": "sec1", "pos_cat": "Security", "pos_str": "Security 1"}],
        "FROM schedule ORDER BY": [{"day_key": "sat", "time_key": "0800",
                                    "pos_key": "sec1", "vol_id": None}],
    }
    a = portal_sync.build_snapshot(FakeDB(responses))
    b = portal_sync.build_snapshot(FakeDB(responses))
    assert a["content_hash"] == b["content_hash"]

    changed = dict(responses)
    changed["FROM schedule ORDER BY"] = [{"day_key": "sat", "time_key": "0800",
                                          "pos_key": "sec1", "vol_id": "1042"}]
    c = portal_sync.build_snapshot(FakeDB(changed))
    assert c["content_hash"] != a["content_hash"], "assigning a slot must change the hash"


def _snapshot_responses(overage=None):
    r = {
        "FROM days_table": [{"day_key": "sat", "day_str": "Saturday",
                             "event_date": "2026-05-09", "sort_order": 0}],
        "FROM times_table": [{"time_key": "0800", "time_str": "0800-1200", "sort_order": 0}],
        "FROM day_times": [{"day_key": "sat", "time_key": "0800"}],
        "FROM schedule GROUP BY": [{"day_key": "sat", "time_key": "0800",
                                    "total": 4, "assigned": 1}],
    }
    if overage is not None:
        r["portal_overage_pct"] = [{"val": str(overage)}]
    return r


def test_each_timeslot_carries_its_remaining_with_overage():
    """The arithmetic itself is tested against real SQL in test_remaining_sql.py."""
    snap = portal_sync.build_snapshot(FakeDB(_snapshot_responses(50)))
    assert snap["day_times"] == [{"day_key": "sat", "time_key": "0800", "remaining": 6 - 1}]
    assert "bands" not in snap and "band_times" not in snap


def test_changing_the_overage_changes_the_hash():
    """So a new percentage is pushed on the next tick like any other schedule edit."""
    a = portal_sync.build_snapshot(FakeDB(_snapshot_responses(0)))
    b = portal_sync.build_snapshot(FakeDB(_snapshot_responses(25)))
    assert a["content_hash"] != b["content_hash"]


# ── branding ──────────────────────────────────────────────────────────────────

def test_branding_hash_changes_with_the_logo():
    base = {"SELECT app_key, val FROM app_kv": [
        {"app_key": "app_title", "val": "BSidesCharm"},
        {"app_key": "app_logo", "val": "data:image/png;base64,AAA"},
    ]}
    a = portal_sync.build_branding(FakeDB(base))
    assert a["app_title"] == "BSidesCharm" and a["logo"].startswith("data:image/png")
    changed = {"SELECT app_key, val FROM app_kv": [
        {"app_key": "app_title", "val": "BSidesCharm"},
        {"app_key": "app_logo", "val": "data:image/png;base64,BBB"},
    ]}
    assert portal_sync.build_branding(FakeDB(changed))["content_hash"] != a["content_hash"]


def test_branding_carries_the_conference_contact_and_hashes_it():
    rows = [{"app_key": "app_title", "val": "ExampleCon"}]
    a = portal_sync.build_branding(FakeDB({"SELECT app_key, val FROM app_kv": rows}))
    assert a["contact_email"] is None and a["code_of_conduct_url"] is None
    b = portal_sync.build_branding(FakeDB({"SELECT app_key, val FROM app_kv": rows + [
        {"app_key": "contact_email", "val": "volunteers@example.org"},
        {"app_key": "conference_url", "val": "https://example.org/"},
        {"app_key": "code_of_conduct_url", "val": "https://example.org/coc"}]}))
    assert (b["contact_email"], b["conference_url"], b["code_of_conduct_url"]) == (
        "volunteers@example.org", "https://example.org/", "https://example.org/coc")
    assert b["content_hash"] != a["content_hash"]


def test_branding_carries_the_header_logo_and_hashes_it():
    rows = [{"app_key": "app_logo", "val": "data:image/png;base64,AAA"}]
    a = portal_sync.build_branding(FakeDB({"SELECT app_key, val FROM app_kv": rows}))
    assert a["header_logo"] is None
    b = portal_sync.build_branding(FakeDB({"SELECT app_key, val FROM app_kv": rows + [
        {"app_key": "app_header_logo", "val": "data:image/webp;base64,CCC"}]}))
    assert b["header_logo"] == "data:image/webp;base64,CCC"
    assert b["content_hash"] != a["content_hash"]


def test_sync_is_not_configured_without_a_base_url(monkeypatch):
    """An existing deployment with no portal must behave exactly as it did before."""
    monkeypatch.setattr(portal_sync, "PORTAL_SYNC_URL", "")
    assert portal_sync.configured() is False


def test_request_push_is_safe_before_the_loop_starts():
    """broadcast() calls this on every schedule mutation, including during CLI/one-shot runs."""
    monkey = portal_sync._state.push_event
    portal_sync._state.push_event = None
    try:
        portal_sync.request_push()       # must not raise
    finally:
        portal_sync._state.push_event = monkey


# ── cursor handling and health logging ────────────────────────────────────────

def test_cursor_of_treats_a_null_cursor_as_zero():
    """last_rev is NULL for push streams. It must never reach a comparison as None.

    `hb["volunteers_rev"] > state["last_rev"]` would raise TypeError on None, and it sits in
    the sync loop's happy path where it would break every cycle.
    """
    assert portal_sync.cursor_of({"last_rev": None}) == 0
    assert portal_sync.cursor_of({}) == 0
    assert portal_sync.cursor_of({"last_rev": 0}) == 0
    assert portal_sync.cursor_of({"last_rev": 42}) == 42
    # The comparison the loop actually performs.
    assert (7 > portal_sync.cursor_of({"last_rev": None})) is True


@pytest.fixture(autouse=True)
def _reset_health():
    portal_sync._state.healthy = None
    portal_sync._state.key_match = None
    yield
    portal_sync._state.healthy = None
    portal_sync._state.key_match = None


def test_first_success_logs_once_and_repeats_stay_quiet(caplog):
    """A line every interval saying 'nothing changed' would bury the one that matters."""
    with caplog.at_level("INFO", logger="portal_sync"):
        portal_sync._note_outcome({"hmac_key_match": True})
        first = [r.getMessage() for r in caplog.records]
        caplog.clear()
        portal_sync._note_outcome({"hmac_key_match": True})
        second = [r.getMessage() for r in caplog.records]
    assert any("healthy" in m for m in first)
    assert second == []


def test_a_cycle_that_did_work_says_so_even_when_already_healthy(caplog):
    with caplog.at_level("INFO", logger="portal_sync"):
        portal_sync._note_outcome({"hmac_key_match": True})
        caplog.clear()
        portal_sync._note_outcome({"hmac_key_match": True, "schedule_push": {"ok": True}})
        messages = [r.getMessage() for r in caplog.records]
    assert any("schedule_push" in m for m in messages)


def test_failure_warns_once_then_drops_to_debug(caplog):
    """A persistent outage must not emit a warning every interval forever."""
    with caplog.at_level("DEBUG", logger="portal_sync"):
        portal_sync._note_failure(RuntimeError("connection refused"))
        levels_first = [r.levelname for r in caplog.records]
        caplog.clear()
        portal_sync._note_failure(RuntimeError("connection refused"))
        levels_second = [r.levelname for r in caplog.records]
    assert "WARNING" in levels_first
    assert levels_second == ["DEBUG"]


def test_recovery_after_failure_logs_healthy_again(caplog):
    with caplog.at_level("INFO", logger="portal_sync"):
        portal_sync._note_failure(RuntimeError("down"))
        caplog.clear()
        portal_sync._note_outcome({"hmac_key_match": True})
        messages = [r.getMessage() for r in caplog.records]
    assert any("healthy" in m for m in messages)


def test_hmac_key_mismatch_is_logged_as_an_error_once(caplog):
    """The failure it predicts is silent: volunteers simply never manage to sign in."""
    with caplog.at_level("DEBUG", logger="portal_sync"):
        portal_sync._note_outcome({"hmac_key_match": False})
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        caplog.clear()
        portal_sync._note_outcome({"hmac_key_match": False})
        repeat_errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "EMAIL_HMAC_KEY" in errors[0].getMessage()
    assert repeat_errors == []
