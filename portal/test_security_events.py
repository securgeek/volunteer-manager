"""The portal's security events: noticed by SecuritySignals (main.py), pulled by the scheduler.

    pytest portal/test_security_events.py -v
"""
import json

from test_portal_local import _conn, _signed, app_env, client  # noqa: F401  (fixtures)


def _rows(app_env):
    conn = _conn(app_env)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM portal_security_events ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_a_hostile_request_is_stored_for_the_scheduler(app_env, client):
    r = client.post("/api/portal/request_link", data={"email": "x' union select 1--"},
                    headers={"X-Forwarded-For": "203.0.113.9", "User-Agent": "sqlmap/1.8"})
    assert r.status_code != 500
    (row,) = _rows(app_env)
    assert row["event"] == "security.attack_signature" and row["severity"] == "high"
    assert row["ip"] == "203.0.113.9" and row["path"] == "/api/portal/request_link"
    data = json.loads(row["data"])
    assert set(data["rules"]) == {"sql_union", "scanner_agent"}
    assert row["ts"].endswith("Z")


def test_a_nul_is_a_400_and_a_row(app_env, client):
    r = client.post("/api/portal/request_link", data={"email": "a@b.c\x00"})
    assert r.status_code == 400 and r.json() == {"detail": "Malformed request"}
    assert [r["event"] for r in _rows(app_env)] == ["security.attack_signature"]


def test_a_bad_sync_signature_is_a_critical_row(app_env, client):
    r = client.get("/api/sync/heartbeat", headers={"x-sync-key-id": "nope"})
    assert r.status_code == 401
    (row,) = _rows(app_env)
    assert row["event"] == "security.sync_auth_fail" and row["severity"] == "critical"


def test_ordinary_volunteer_traffic_leaves_no_row(app_env, client):
    client.get("/api/portal/branding")
    client.get("/api/portal/signup/status")
    client.post("/api/portal/request_link", data={"email": "maría.o'brien@example.com"})
    client.get("/api/portal/me")                          # 401: signed out, one signal
    assert _rows(app_env) == []


def test_the_heartbeat_reports_the_newest_and_the_pull_pages(app_env, client):
    assert _signed(client, "GET", "/api/sync/heartbeat").json()["security_events_rev"] == 0
    for i in range(3):
        client.get("/api/portal/branding", params={"q": "{{7*7}}"},
                   headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert _signed(client, "GET", "/api/sync/heartbeat").json()["security_events_rev"] == 3
    page = _signed(client, "GET", "/api/sync/security_events?since=0&limit=2").json()
    assert [e["id"] for e in page["events"]] == [1, 2] and page["next"] == 2
    assert page["events"][0]["data"]["rules"] == ["template"]
    rest = _signed(client, "GET", "/api/sync/security_events?since=2&limit=500").json()
    assert [e["id"] for e in rest["events"]] == [3] and rest["next"] == 3
    empty = _signed(client, "GET", "/api/sync/security_events?since=3").json()
    assert empty == {"events": [], "next": 3}


def test_the_pull_needs_a_signature(app_env, client):
    assert client.get("/api/sync/security_events?since=0").status_code == 401


def test_week_old_events_are_pruned_on_pull(app_env, client):
    conn = _conn(app_env)
    conn.execute("INSERT INTO portal_security_events (ts, event, severity, message) VALUES "
                 "('2020-01-01T00:00:00.000Z', 'security.attack_signature', 'high', 'old')")
    conn.close()
    _signed(client, "GET", "/api/sync/security_events?since=0")
    assert [r["message"] for r in _rows(app_env)] == []
