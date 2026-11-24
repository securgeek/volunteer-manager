"""Strict input validation (validation.py): the shared rules, and anomalies in both modes.

The rules are also the volunteer pages' (frontend/src/lib/portal_rules.js); both run every
case in test_data/validation_cases.json.

    pytest portal/test_validation.py -v
"""
import json
import os

import pytest

from test_portal_local import _conn, _link_from_mail, app_env, client, rsa_keys  # noqa: F401
from test_signup import GOOD_ANSWERS, _push_schedule, _settings

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "test_data", "validation_cases.json"), encoding="utf-8") as _f:
    CASES = json.load(_f)["cases"]


# ── the shared rules ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c['kind']}:{ascii(c['value'])[:30]}")
def test_every_shared_case(app_env, case):
    import validation
    clean, problem = validation.check(case["kind"], case["value"], maximum=case.get("max", 0))
    got = "ok" if problem is None else problem[0]
    assert got == case["expect"], (case, problem)
    if "clean" in case:
        assert clean == case["clean"]


def test_the_shared_cases_cover_every_kind():
    assert {c["kind"] for c in CASES} == {"name", "handle", "email", "text", "pick_key", "token"}
    assert {c["expect"] for c in CASES} == {"ok", "error", "anomaly"}


# ── end to end ────────────────────────────────────────────────────────────────

@pytest.fixture
def enforce_env(monkeypatch, request):
    monkeypatch.setenv("PORTAL_STRICT", "enforce")
    return request.getfixturevalue("app_env")


def _opened(client, keys):
    _settings(client, "open", portal_signup_open="1", email_pubkey=keys[1])
    _push_schedule(client)


def _session(client, env, email="new.person@example.com"):
    r = client.post("/api/portal/signup/check_email", data={"email": email})
    assert r.status_code == 200, r.text
    r = client.post("/api/portal/verify", data={"t": _link_from_mail(env)})
    assert r.status_code == 200, r.text


def _submit(client, **over):
    data = {"first_name": "María-José", "last_name": "O'Brien", "handle": "crab🦀",
            "answers": GOOD_ANSWERS, "prefs": ["sat_0800", "sat_1500"],
            "extras": ["sun_1130"], "add_any": False}
    data.update(over)
    return client.post("/api/portal/signup/submit", data={"signup": json.dumps(data)})


def _anomalies(env):
    conn = _conn(env)
    try:
        return [json.loads(r["data"]) for r in conn.execute(
            "SELECT data FROM portal_security_events WHERE event = 'portal.anomaly' "
            "ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_what_the_pages_send_passes_in_enforce_mode(enforce_env, rsa_keys):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    r = client.post("/api/portal/signup/check_name",
                    data={"first_name": "María-José", "last_name": "O'Brien", "handle": ""})
    assert r.status_code == 200, r.text
    assert _submit(client).status_code == 200
    assert _anomalies(enforce_env) == []


@pytest.mark.parametrize("over, rule", [
    ({"first_name": "R2D2"}, "name_chars"),
    ({"last_name": "x" * 61}, "too_long"),
    ({"handle": "<script>"}, "handle_chars"),
    ({"first_name": "Ad" + chr(0x202E) + "a"}, "control_char"),
    ({"first_name": 5}, "wrong_type"),
    ({"add_any": "yes"}, "wrong_type"),
    ({"answers": "all good"}, "wrong_type"),
    ({"prefs": ["sat_0800", "SAT 1500"]}, "pick_key"),
    ({"prefs": ["sat_0800"]}, "pick_count"),
    ({"is_admin": True}, "unknown_key"),
])
def test_an_anomaly_is_refused_and_recorded_in_enforce_mode(enforce_env, rsa_keys, over, rule):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    r = _submit(client, **over)
    assert r.status_code == 400 and r.json() == {"detail": "Malformed request"}
    (rec,) = _anomalies(enforce_env)
    assert rec["rule"] == rule and rec["mode"] == "enforce"


def test_in_report_mode_the_anomaly_is_recorded_and_the_request_goes_on(app_env, rsa_keys):
    client = app_env[0]
    _opened(client, rsa_keys)
    _session(client, app_env)
    assert _submit(client, is_admin=True, handle="h4x0r").status_code == 200
    (rec,) = _anomalies(app_env)
    assert rec == {"rule": "unknown_key", "where": "signup.is_admin", "mode": "report",
                   "held_back": 0}


def test_an_ordinary_mistake_is_not_an_anomaly(enforce_env, rsa_keys):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    r = _submit(client, first_name="   ")
    assert r.status_code == 400 and r.json()["detail"] == "First name is required"
    r = _submit(client, prefs=["sat_0800", "sat_1130"])                # overlap
    assert r.status_code == 400 and "overlap" in r.json()["detail"]
    assert _anomalies(enforce_env) == []


@pytest.mark.parametrize("raw, rule", [
    ('{"first_name": "A", "first_name": "B"}', "json"),
    ('{"add_any": NaN}', "json"),
    ('{"answers": {"a": {"b": {"c": {"d": 1}}}}}', "json_depth"),
])
def test_json_is_parsed_strictly(enforce_env, rsa_keys, raw, rule):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    r = client.post("/api/portal/signup/submit", data={"signup": raw})
    assert r.status_code == 400
    assert _anomalies(enforce_env)[0]["rule"] == rule


def test_unknown_fields_and_query_parameters(enforce_env):
    client = enforce_env[0]
    r = client.post("/api/portal/request_link", data={"email": "a@b.co", "admin": "1"})
    assert r.status_code == 400
    r = client.get("/api/portal/branding", params={"debug": "1"})
    assert r.status_code == 400
    assert [(a["rule"], a["where"]) for a in _anomalies(enforce_env)] == [
        ("unknown_field", "form:admin"), ("unknown_field", "query:debug")]


def test_a_json_body_where_a_form_belongs(enforce_env):
    r = enforce_env[0].post("/api/portal/request_link", json={"email": "a@b.co"})
    assert r.status_code == 400
    assert _anomalies(enforce_env)[0]["rule"] == "content_type"


@pytest.mark.parametrize("path, data, rule", [
    ("/api/portal/request_link", {"email": "not-an-address"}, "email_syntax"),
    ("/api/portal/signup/check_email", {"email": "a@b..c"}, "email_syntax"),
    ("/api/portal/verify", {"t": "short"}, "token"),
])
def test_malformed_emails_and_tokens(enforce_env, path, data, rule):
    r = enforce_env[0].post(path, data=data)
    assert r.status_code == 400
    assert _anomalies(enforce_env)[0]["rule"] == rule


def test_a_bad_verify_link_is_noted_but_still_gets_its_page(enforce_env):
    r = enforce_env[0].get("/api/portal/verify", params={"t": "<x>"})
    assert r.status_code == 400
    assert _anomalies(enforce_env)[0]["rule"] == "token"


def test_repeats_within_a_minute_are_one_row_then_a_count(app_env, monkeypatch):
    import validation
    client = app_env[0]
    clock = [1000.0]
    monkeypatch.setattr(validation, "_monotonic", lambda: clock[0])
    for _ in range(5):
        client.post("/api/portal/request_link", data={"email": "nope"})
    clock[0] += validation.NOTE_EVERY_SEC + 1
    client.post("/api/portal/request_link", data={"email": "nope"})
    assert [a["held_back"] for a in _anomalies(app_env)] == [0, 4]


def test_portal_strict_must_be_a_known_mode(monkeypatch):
    import config
    monkeypatch.setenv("PORTAL_STRICT", "maybe")
    with pytest.raises(config.ConfigError, match="PORTAL_STRICT"):
        config.Settings(require_secrets=False)


def test_a_control_character_in_a_free_text_answer(enforce_env, rsa_keys):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    answers = {**GOOD_ANSWERS, "dietary": "vegetarian" + chr(0x202E) + "x"}
    assert _submit(client, answers=answers).status_code == 400
    (rec,) = _anomalies(enforce_env)
    assert rec["rule"] == "control_char" and rec["where"] == "answers.dietary"


def test_an_answer_to_a_removed_question_is_dropped_not_an_anomaly(enforce_env, rsa_keys):
    client = enforce_env[0]
    _opened(client, rsa_keys)
    _session(client, enforce_env)
    assert _submit(client, answers={**GOOD_ANSWERS, "old_question": "x"}).status_code == 200
    assert _anomalies(enforce_env) == []
