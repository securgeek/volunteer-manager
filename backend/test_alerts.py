"""Security alert rules, cooldowns, the hourly cap and mail delivery (`alerts.py`).

No database: records go straight to `alerts.observe`, and mail goes to a fake portal. The
parts that read the database (recipients, "new network") are in test_invites.py.

    cd backend && pytest test_alerts.py -v
"""
import asyncio
import json

import pytest

import alerts
import audit
import portal_sync
from ratelimit import SlidingWindow


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    """Empty alert state, a fake clock, and the audit log on (file only) for each test."""
    alerts.reset()

    class Clock:
        """The current fake time, in seconds."""

        now = 5000.0
    clock = Clock()
    monkeypatch.setattr(alerts, "_monotonic", lambda: clock.now)
    path = tmp_path / "audit.jsonl"
    audit.configure(str(path))
    yield clock
    audit.configure(None)
    alerts.reset()


def _fired(tmp_path):
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [r for r in (json.loads(x) for x in path.read_text(encoding="utf-8").splitlines())
            if r["event"] == "alert.fired"]


def _rec(event, ip="203.0.113.9", severity="low", actor=None, user_id=None, **data):
    return {"ts": "2026-11-23T10:00:00.000Z", "event": event, "severity": severity,
            "actor": actor, "user_id": user_id, "ip": ip, "method": "POST",
            "path": "/api/login", "message": "m", "data": data}


def _fail(username="bob", ip="203.0.113.9", reason="bad_password"):
    alerts.observe(_rec("auth.login.fail", ip=ip, username=username, reason=reason))


def _rules():
    return [a.rule_id for a in alerts._state.unsent]


# ── thresholds ────────────────────────────────────────────────────────────────

def test_ten_failures_from_one_address_fire_once(tmp_path):
    for i in range(9):
        _fail(username=f"u{i % 3}", reason="unknown_user")
    assert "login.bruteforce.ip" not in _rules()
    _fail(username="u0", reason="unknown_user")
    assert _rules().count("login.bruteforce.ip") == 1
    (fired,) = [f for f in _fired(tmp_path) if f["data"]["rule"] == "login.bruteforce.ip"]
    assert fired["severity"] == "high" and fired["data"]["count"] == 10
    assert fired["data"]["mailed"] is True and fired["data"]["trigger"] == "auth.login.fail"


def test_an_ipv6_64_counts_as_one_address():
    for i in range(10):
        _fail(username="x", ip=f"2001:db8:1:1::{i + 1:x}", reason="unknown_user")
    assert _rules().count("login.bruteforce.ip") == 1


def test_wrong_passwords_for_one_account_from_anywhere():
    for i in range(4):
        _fail(username="alice", ip=f"198.51.100.{i}")
    _fail(username="alice", ip="198.51.100.9", reason="unknown_user")    # not a wrong password
    assert "login.bruteforce.user" not in _rules()
    _fail(username="alice", ip="198.51.100.10")
    assert _rules() == ["login.bruteforce.user"]


def test_a_spray_counts_distinct_usernames():
    for _ in range(6):
        _fail(username="same", reason="unknown_user")
    assert "login.spray" not in _rules()
    for name in ("a", "b", "c", "d"):
        _fail(username=name, reason="unknown_user")
    assert "login.spray" in _rules()


def test_old_events_leave_the_window(fresh):
    for _ in range(9):
        _fail(reason="unknown_user")
    fresh.now += 15 * 60 + 1
    _fail(reason="unknown_user")
    assert "login.bruteforce.ip" not in _rules()


# ── cooldown and the cap ──────────────────────────────────────────────────────

def test_a_flood_is_one_alert_an_hour_with_a_count_of_the_rest(fresh, tmp_path):
    for _ in range(10):
        _fail(reason="unknown_user")
    for _ in range(25):
        _fail(reason="unknown_user")                    # during the cooldown: counted only
    assert _rules().count("login.bruteforce.ip") == 1
    fresh.now += alerts.COOLDOWN_SEC + 1
    for _ in range(10):
        _fail(reason="unknown_user")
    ip_alerts = [a for a in alerts._state.unsent if a.rule_id == "login.bruteforce.ip"]
    assert len(ip_alerts) == 2 and ip_alerts[1].held_back == 25


def test_the_cooldown_is_per_key(fresh):
    for ip in ("203.0.113.1", "203.0.113.2"):
        for _ in range(10):
            _fail(ip=ip, reason="unknown_user")
    assert _rules().count("login.bruteforce.ip") == 2


def test_past_the_hourly_cap_alerts_are_recorded_but_not_mailed(tmp_path, monkeypatch):
    monkeypatch.setattr(alerts._state, "mailed", SlidingWindow(2, 3600))
    for name in ("a", "b", "c"):
        alerts.observe(_rec("user.create", actor="alice", target=name, role="view"))
    assert len(alerts._state.unsent) == 2
    fired = _fired(tmp_path)
    assert [f["data"]["mailed"] for f in fired] == [True, True, False]


# ── one-event rules and the fallback ──────────────────────────────────────────

@pytest.mark.parametrize("event, data, rule", [
    ("user.create", {"target": "eve", "role": "organizer"}, "account.privilege"),
    ("user.role_change", {"target_user_id": "u-2", "role": "organizer"}, "account.privilege"),
    ("invite.accept", {"invite_id": "i-1", "role": "view"}, "account.privilege"),
    ("setup.create_admin", {"user_id": "u-1"}, "setup.create_admin"),
    ("setup.create_admin.refused", {"username": "x"}, "setup.after_complete"),
    ("password.reset.by_admin", {"target_user_id": "u-2"}, "account.credentials"),
    ("passkey.clear_user", {"target": "bob", "removed": 2}, "account.credentials"),
    ("user.email_set", {"target_user_id": "u-2"}, "account.credentials"),
    ("password.reset.complete", {"user_id_reset": "u-2"}, "account.reset"),
    ("email_key.update", {}, "email_key.change"),
    ("api_key.create", {}, "api_key.create"),
    ("checkin.key_rotate", {}, "checkin.key_rotate"),
])
def test_each_watched_event_alerts_on_its_own(event, data, rule):
    alerts.observe(_rec(event, actor="alice", **data))
    assert _rules() == [rule]


def test_two_different_accounts_created_are_two_alerts():
    alerts.observe(_rec("user.create", actor="alice", target="eve", role="view"))
    alerts.observe(_rec("user.create", actor="alice", target="mallory", role="view"))
    assert _rules() == ["account.privilege", "account.privilege"]


def test_an_unwatched_high_severity_event_still_alerts():
    alerts.observe(_rec("security.something_new", severity="high"))
    alerts.observe(_rec("security.something_mild", severity="medium"))
    assert _rules() == ["severity.high"]


def test_ordinary_events_do_not_alert():
    for event in ("auth.login.ok", "schedule.assign", "radio.checkout", "volunteer.note.add"):
        alerts.observe(_rec(event, severity="info", actor="alice"))
    assert _rules() == []


def test_alert_events_never_feed_back_into_the_rules():
    alerts.observe(_rec("alert.fired", severity="critical", rule="x"))
    alerts.observe(_rec("alert.mail_fail", severity="high", rule="x"))
    assert _rules() == []


def test_purges_alert_only_in_bulk():
    for i in range(4):
        alerts.observe(_rec("volunteer.purge", actor="alice", vol_id=f"v{i}"))
    assert _rules() == []
    alerts.observe(_rec("volunteer.purge", actor="alice", vol_id="v9"))
    assert _rules() == ["volunteer.bulk_purge"]


def test_refused_api_calls_from_one_address():
    for _ in range(5):
        alerts.observe(_rec("api_key.refused"))
    assert _rules() == ["api_key.refused"]


# ── what the email says ───────────────────────────────────────────────────────

def test_facts_carry_the_details_but_never_an_address():
    alerts.observe(_rec("user.create", actor="alice", target="eve", role="organizer",
                        email="eve@example.com", invited_email="x@y.z"))
    (alert,) = alerts._state.unsent
    facts = dict(alert.facts)
    assert facts["Event"] == "user.create" and facts["Signed-in user"] == "alice"
    assert facts["Client address"] == "203.0.113.9" and facts["Target"] == "eve"
    assert facts["Role"] == "organizer"
    assert not any("@" in v for v in facts.values())
    assert len(alert.facts) <= alerts.MAX_FACTS


def test_counting_rules_say_how_many_and_over_what():
    for i in range(5):
        _fail(username=f"n{i}", reason="unknown_user")
    spray = next(a for a in alerts._state.unsent if a.rule_id == "login.spray")
    assert dict(spray.facts)["Seen"] == "5 different usernames in 15 minutes"


def test_long_values_are_cut():
    alerts.observe(_rec("user.create", actor="alice", target="x" * 1000))
    (alert,) = alerts._state.unsent
    assert len(dict(alert.facts)["Target"]) == alerts.FACT_MAX


# ── delivery ──────────────────────────────────────────────────────────────────

@pytest.fixture
def portal(monkeypatch):
    """A fake portal: records each send_admin_mail; `fail_for` makes chosen addresses fail."""
    class Portal:
        """What was sent, and which addresses to fail."""

        sent: list = []
        fail_for: dict = {}

    p = Portal()
    p.sent, p.fail_for = [], {}

    async def send(kind, to, link, **fields):
        left = p.fail_for.get(to, 0)
        if left:
            p.fail_for[to] = left - 1
            raise RuntimeError("portal said 502")
        p.sent.append({"kind": kind, "to": to, "link": link, **fields})
        return {"ok": True}

    monkeypatch.setattr(portal_sync, "send_admin_mail", send)
    monkeypatch.setattr(portal_sync, "site_origin", lambda: "https://admin.example")
    monkeypatch.setattr(alerts, "_app_title", lambda: "ExampleCon")
    monkeypatch.setattr(alerts, "MAIL_RETRY_SEC", 0)
    monkeypatch.setattr(alerts, "recipients", lambda: [
        {"username": "alice", "email": "alice@example.com"},
        {"username": "carol", "email": "carol@example.com"}])
    return p


def _one_alert():
    alerts.observe(_rec("email_key.update", actor="alice"))
    (alert,) = alerts._state.unsent
    return alert


def test_an_alert_is_mailed_to_every_recipient(portal, tmp_path):
    assert asyncio.run(alerts.deliver(_one_alert())) == 2
    assert [m["to"] for m in portal.sent] == ["alice@example.com", "carol@example.com"]
    m = portal.sent[0]
    assert m["kind"] == "alert" and m["link"] == "https://admin.example/admin/audit"
    assert m["rule"] == "email_key.change" and m["severity"] == "high"
    assert m["app_title"] == "ExampleCon" and m["held_back"] == 0
    assert all(isinstance(f, list) and len(f) == 2 for f in m["facts"])
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    mailed = [r for r in log if r["event"] == "alert.mailed"]
    assert mailed and mailed[0]["data"]["recipients"] == 2
    assert not any("@" in json.dumps(r) for r in log)


def test_one_failure_is_retried_and_two_are_recorded(portal, tmp_path):
    portal.fail_for = {"alice@example.com": 1, "carol@example.com": 2}
    assert asyncio.run(alerts.deliver(_one_alert())) == 1
    assert [m["to"] for m in portal.sent] == ["alice@example.com"]
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    (fail,) = [r for r in log if r["event"] == "alert.mail_fail"]
    assert fail["data"]["recipient"] == "carol" and "502" in fail["data"]["error"]


def test_no_recipients_is_recorded(portal, monkeypatch, tmp_path):
    monkeypatch.setattr(alerts, "recipients", lambda: [])
    assert asyncio.run(alerts.deliver(_one_alert())) == 0
    assert portal.sent == []
    log = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [r["data"]["reason"] for r in log if r["event"] == "alert.mail_fail"] == [
        "no_recipients"]


def test_start_drains_alerts_queued_before_it_and_mails_new_ones(portal):
    async def run():
        alerts.observe(_rec("email_key.update", actor="alice"))       # before start
        alerts.start(asyncio.get_running_loop())
        try:
            alerts.observe(_rec("checkin.key_rotate", actor="alice"))  # after
            for _ in range(200):                     # up to 2 s; delivery uses to_thread
                if len(portal.sent) == 4:
                    break
                await asyncio.sleep(0.01)
        finally:
            alerts.stop()
    asyncio.run(run())
    assert sorted({m["rule"] for m in portal.sent}) == ["checkin.key_rotate", "email_key.change"]
    assert alerts.observe not in audit._config.observers


def test_a_failing_rule_never_fails_the_audit_call(monkeypatch, tmp_path, caplog):
    def broken(_rec):
        raise ValueError("rule bug")
    audit.add_observer(broken)
    try:
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "query_string": b"", "client": ("1.2.3.4", 1)})
        audit.audit("auth.logout", req, None, "Signed out")
    finally:
        audit.remove_observer(broken)
    assert "rule bug" in caplog.text
    assert (tmp_path / "audit.jsonl").read_text().count("auth.logout") == 1


# ── networks ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip, net", [
    ("203.0.113.9", "203.0.113.0/24"),
    ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
    ("::ffff:198.51.100.7", "198.51.100.0/24"),
    ("unknown", "unknown"),
    (None, None),
])
def test_network(ip, net):
    assert alerts.network(ip) == net
