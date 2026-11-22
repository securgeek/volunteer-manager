"""Export API keys, the export endpoints, shift check-in, and the schedule email template,
against a REAL Postgres.

Needs a throwaway Postgres; skipped unless TEST_PG_HOST is set. Everything in the target
database's public schema is DROPPED.

    TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 TEST_PG_DB=scheduler_test \\
    TEST_PG_USER=scheduler TEST_PG_PASSWORD=test  pytest backend/test_export.py -v
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST (see module docstring)",
                allow_module_level=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import db as dbm  # noqa: E402
import checkin_codes  # noqa: E402

SITE = "https://scheduler.test"
TZ = "America/New_York"


def _raw_conn():
    import psycopg2
    dsn = {k: v for k, v in dbm._dsn().items() if k != "cursor_factory"}
    return psycopg2.connect(**dsn)


def _reset_schema():
    conn = _raw_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()


def _sql(stmt, params=()):
    conn = _raw_conn()
    with conn.cursor() as cur:
        cur.execute(stmt, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    conn.close()
    return rows


def _migrate():
    cwd = os.getcwd()
    os.chdir(_HERE)  # migrate() opens scheduler.sql and migrations/
    try:
        dbm.open_db()
    finally:
        os.chdir(cwd)


@pytest.fixture(scope="module")
def app():
    dbm.DB_HOST = os.environ["TEST_PG_HOST"]
    dbm.DB_PORT = int(os.getenv("TEST_PG_PORT", "5432"))
    dbm.DB_NAME = os.getenv("TEST_PG_DB", "scheduler_test")
    dbm.DB_USER = os.getenv("TEST_PG_USER", "scheduler")
    dbm.DB_PASSWORD = os.getenv("TEST_PG_PASSWORD", "")
    dbm.close_pool()

    _reset_schema()
    _migrate()

    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from routers import (auth, api_tokens, export, checkin, volunteers, email_template, setup,
                         portal_content)
    application = FastAPI()
    for r in (auth, api_tokens, export, checkin, volunteers, email_template, setup,
              portal_content):
        application.include_router(r.router)
    application.add_middleware(SessionMiddleware, secret_key="test-secret")

    from argon2 import PasswordHasher
    ph = PasswordHasher()
    for uid, name, role in (("u-alice", "alice", "organizer"), ("u-bob", "bob", "checkin")):
        _sql("INSERT INTO users (user_id, username, password_hash, role) VALUES (%s, %s, %s, %s)",
             (uid, name, ph.hash(f"{name}-password"), role))
    _sql("INSERT INTO app_kv (app_key, val) VALUES ('app_title', 'BSidesTest'), ('event_tz', %s)",
         (TZ,))

    # Saturday has a date far in the future (any check-in is early); Sunday has none.
    _sql("INSERT INTO days_table (day_key, day_str, event_date) VALUES "
         "('sat', 'Saturday', '2099-04-25'), ('sun', 'Sunday', NULL)")
    _sql("INSERT INTO times_table (time_key, time_str) VALUES ('0800', '0800-1200'), "
         "('1300', '1300-1700')")
    _sql("INSERT INTO categories (cat_name, sort_order) VALUES ('Security', 1)")
    _sql("INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES "
         "('sec1', 'Security', 'Security 1'), ('sec2', 'Security', 'Security 2')")
    _sql("INSERT INTO volunteers (vol_id, name, status, email_enc) VALUES "
         "('1042', 'Jim E. (Zero Cool)', 'active', 'CIPHERTEXT'), "
         "('1043', 'Mary Ann S.', 'active', NULL), "
         "('p-pend', 'Pat P.', 'pending', NULL)")
    _sql("INSERT INTO volunteers (vol_id, name, status, inactive_at) VALUES "
         "('1044', 'Gone G.', 'active', NOW())")
    for d, t, p, v in (("sat", "0800", "sec1", "1042"), ("sat", "0800", "sec2", None),
                       ("sat", "1300", "sec1", "1043"), ("sun", "0800", "sec1", "1042")):
        _sql("INSERT INTO day_times (day_key, time_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
             (d, t))
        _sql("INSERT INTO schedule (day_key, time_key, pos_key, vol_id) VALUES (%s, %s, %s, %s)",
             (d, t, p, v))
    yield application
    dbm.close_pool()


@pytest.fixture(autouse=True)
def clear_fail_limit():
    import api_auth
    api_auth._fails.clear()


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url=SITE)


def _signed_in(app, user="alice"):
    c = _client(app)
    r = c.post("/api/login", data={"username": user, "password": f"{user}-password"})
    assert r.status_code == 200, r.text
    return c


def _new_key(app, scopes="schedule:read volunteers:read", **extra):
    r = _signed_in(app).post("/api/api_tokens", data={"label": "test key", "scopes": scopes, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _get(app, path, key, **headers):
    return _client(app).get(path, headers={"Authorization": f"Bearer {key}", **headers})


# ── migration ─────────────────────────────────────────────────────────────────

# One volunteer per state, and the (is_live, is_counted, is_assignable) each must get.
_STATES = {
    "s-active":    ("active",  None,  None,  (True,  True,  True)),
    "s-pending":   ("pending", None,  None,  (True,  True,  False)),
    "s-cancelled": ("active",  "now", None,  (False, False, False)),
    "s-inactive":  ("pending", None,  "now", (False, False, False)),
    "s-declined":  ("declined", None, None,  (True,  False, False)),  # pre-v14 leftover
}


# ── keys ──────────────────────────────────────────────────────────────────────

def test_key_is_shown_once_and_only_its_hash_is_stored(app):
    made = _new_key(app)
    assert made["key"].startswith("bsc_") and len(made["key"]) > 40
    stored = _sql("SELECT token_hash FROM api_tokens WHERE token_id = %s", (made["token_id"],))
    assert stored[0][0] != made["key"]
    listing = _signed_in(app).get("/api/api_tokens").json()
    assert made["key"] not in str(listing) and stored[0][0] not in str(listing)
    row = next(t for t in listing["tokens"] if t["token_id"] == made["token_id"])
    assert row["active"] and row["scopes"] == ["schedule:read", "volunteers:read"]
    assert "checkin:codes" in listing["scopes"]


def test_only_organizers_manage_keys(app):
    c = _signed_in(app, "bob")
    assert c.get("/api/api_tokens").status_code == 403
    assert c.post("/api/api_tokens", data={"label": "x", "scopes": "schedule:read"}).status_code == 403
    assert _client(app).get("/api/api_tokens").status_code == 401


@pytest.mark.parametrize("data", [
    {"label": "", "scopes": "schedule:read"},
    {"label": "x", "scopes": ""},
    {"label": "x", "scopes": "schedule:write"},
    {"label": "x", "scopes": "schedule:read", "expires_days": "0"},
    {"label": "x", "scopes": "schedule:read", "expires_days": "400"},
    {"label": "x", "scopes": "schedule:read", "allowed_ips": "10.0.0.0/8 not-an-ip"},
])
def test_bad_key_requests_are_400(app, data):
    assert _signed_in(app).post("/api/api_tokens", data=data).status_code == 400


def test_every_failure_is_the_same_bare_401(app):
    good = _new_key(app, scopes="volunteers:read")
    revoked = _new_key(app)
    _signed_in(app).post("/api/api_tokens/revoke", data={"token_id": revoked["token_id"]})
    expired = _new_key(app)
    _sql("UPDATE api_tokens SET expires_at = NOW() - INTERVAL '1 minute' WHERE token_id = %s",
         (expired["token_id"],))
    cases = [
        _client(app).get("/api/export/schedule"),                         # no header
        _get(app, "/api/export/schedule", "bsc_nonsense"),                # unknown key
        _get(app, "/api/export/schedule", revoked["key"]),
        _get(app, "/api/export/schedule", expired["key"]),
        _get(app, "/api/export/schedule", good["key"]),                   # missing scope
        _client(app).get("/api/export/schedule", headers={"Authorization": f"Basic {good['key']}"}),
    ]
    for r in cases:
        assert r.status_code == 401
        assert r.json() == {"detail": "Unauthorized"}
    assert _get(app, "/api/export/volunteers", good["key"]).status_code == 200


def test_a_session_cookie_is_not_a_key(app):
    assert _signed_in(app).get("/api/export/schedule").status_code == 401


def test_allowed_ips(app):
    k = _new_key(app, allowed_ips="203.0.113.0/24, 2001:db8::1")
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "203.0.113.9"}).status_code == 200
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "198.51.100.1"}).status_code == 401
    row = _sql("SELECT last_used_ip FROM api_tokens WHERE token_id = %s", (k["token_id"],))
    assert row[0][0] == "203.0.113.9"


def test_repeated_failures_are_rate_limited_per_ip(app):
    k = _new_key(app)
    for _ in range(10):
        assert _get(app, "/api/export/schedule", "bsc_wrong",
                    **{"X-Forwarded-For": "198.51.100.7"}).status_code == 401
    # Over the limit, even the right key is turned away from that address...
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    # ...but not from another.
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "198.51.100.8"}).status_code == 200


def test_failures_from_one_ipv6_64_share_a_limit(app):
    k = _new_key(app)
    for n in range(10):                       # a fresh address each time, all in one /64
        assert _get(app, "/api/export/schedule", "bsc_wrong",
                    **{"X-Forwarded-For": f"2001:db8:7:7::{n + 1:x}"}).status_code == 401
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "2001:db8:7:7::ff"}).status_code == 429
    assert _get(app, "/api/export/schedule", k["key"],
                **{"X-Forwarded-For": "2001:db8:7:8::1"}).status_code == 200


@pytest.mark.parametrize("ip, key", [
    ("2001:db8:24e8:1b02::638", "2001:db8:24e8:1b02::/64"),
    ("fe80::1%eth0", "fe80::/64"),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("203.0.113.9", "203.0.113.9"),
    ("", ""),
])
def test_rate_key(ip, key):
    import api_auth
    assert api_auth.rate_key(ip) == key


# ── schedule export ───────────────────────────────────────────────────────────

def test_schedule_export(app):
    k = _new_key(app)
    body = _get(app, "/api/export/schedule", k["key"]).json()
    assert body["event_title"] == "BSidesTest" and body["event_tz"] == TZ
    slots = {s["slot_key"]: s for s in body["slots"]}
    assert len(slots) == 4
    s = slots["sat_0800_sec1"]
    assert s["vol_id"] == "1042" and s["name"] == "Zero Cool [Jim E.]"
    assert s["start"] == "2099-04-25T08:00:00-04:00" and s["end"] == "2099-04-25T12:00:00-04:00"
    assert s["position"] == "Security 1" and s["category"] == "Security" and s["day"] == "Saturday"
    assert slots["sat_1300_sec1"]["name"] == "Mary Ann S."
    assert slots["sat_0800_sec2"]["vol_id"] == "" and slots["sat_0800_sec2"]["name"] == ""
    assert slots["sun_0800_sec1"]["start"] is None and slots["sun_0800_sec1"]["date"] is None
    assert "checkin_code" not in s and "email_enc" not in s


def test_schedule_export_filters(app):
    k = _new_key(app)
    assigned = _get(app, "/api/export/schedule?assigned=1", k["key"]).json()["slots"]
    assert {s["slot_key"] for s in assigned} == {"sat_0800_sec1", "sat_1300_sec1", "sun_0800_sec1"}
    sun = _get(app, "/api/export/schedule?day=sun", k["key"]).json()["slots"]
    assert [s["slot_key"] for s in sun] == ["sun_0800_sec1"]


# ── volunteer export ──────────────────────────────────────────────────────────

def test_volunteer_export_defaults_to_active(app):
    k = _new_key(app)
    vols = {v["vol_id"]: v for v in _get(app, "/api/export/volunteers", k["key"]).json()["volunteers"]}
    assert set(vols) == {"1042", "1043"}        # not pending, not inactive
    jim = vols["1042"]
    assert (jim["name"], jim["first_name"], jim["last_initial"], jim["handle"]) == \
           ("Zero Cool [Jim E.]", "Jim", "E", "Zero Cool")
    assert [s["slot_key"] for s in jim["shifts"]] == ["sat_0800_sec1", "sun_0800_sec1"]
    assert "email_enc" not in jim and "checkin_code" not in jim["shifts"][0]
    every = _get(app, "/api/export/volunteers?status=all", k["key"]).json()["volunteers"]
    assert {v["vol_id"] for v in every} == {"1042", "1043", "1044", "p-pend"}
    assert next(v for v in every if v["vol_id"] == "1044")["inactive"] is True
    assert _get(app, "/api/export/volunteers?status=bogus", k["key"]).status_code == 400


def test_volunteer_export_extra_scopes(app):
    k = _new_key(app, scopes="volunteers:read volunteers:email checkin:codes")
    vols = {v["vol_id"]: v for v in _get(app, "/api/export/volunteers?assigned=1", k["key"]).json()["volunteers"]}
    assert vols["1042"]["email_enc"] == "CIPHERTEXT" and vols["1043"]["email_enc"] is None
    code = vols["1042"]["shifts"][0]["checkin_code"]
    key = bytes.fromhex(_sql("SELECT val FROM app_kv WHERE app_key = 'checkin_key'")[0][0])
    assert checkin_codes.verify_code(key, code) == ("1042", "sat_0800_sec1")


# ── check-in ──────────────────────────────────────────────────────────────────

def _code(app, slot_key, vol_id="1042"):
    k = _new_key(app, scopes="volunteers:read checkin:codes")
    for v in _get(app, "/api/export/volunteers?status=all", k["key"]).json()["volunteers"]:
        for s in v["shifts"]:
            if v["vol_id"] == vol_id and s["slot_key"] == slot_key:
                return s["checkin_code"]
    raise AssertionError("no such shift")


def _notes(vol_id, slot_key):
    return [r[0] for r in _sql("SELECT note FROM vol_notes WHERE vol_id = %s AND note LIKE %s "
                               "ORDER BY note", (vol_id, f"%| {slot_key}"))]


def test_early_scan_asks_first_then_checks_in_anyway(app):
    code = _code(app, "sat_0800_sec1")
    c = _signed_in(app, "bob")          # the checkin role may scan
    r = c.post("/api/checkin/scan", data={"code": code}).json()
    assert r["needs_confirm"] and r["timing"] == "early" and not r["checked_in"]
    assert r["name"] == "Jim E. (Zero Cool)" and r["position"] == "Security 1"
    assert _notes("1042", "sat_0800_sec1") == []                   # nothing written yet
    r = c.post("/api/checkin/scan", data={"code": code, "force": "true"}).json()
    assert r["checked_in"] and not r["already"]
    notes = _notes("1042", "sat_0800_sec1")
    assert len(notes) == 2 and "[CheckIn] | sat_0800_sec1" in notes
    timing = next(n for n in notes if n.startswith("[CheckInTiming]"))
    assert timing.startswith("[CheckInTiming] Early ") and timing.endswith(" min | sat_0800_sec1")
    again = c.post("/api/checkin/scan", data={"code": code}).json()
    assert again["already"] and again["checked_in"]
    # Reset clears the timing note along with the check-in.
    assert c.post("/api/reset_vol_status",
                  data={"vol_id": "1042", "pos_key": "sat_0800_sec1"}).status_code == 204
    assert _notes("1042", "sat_0800_sec1") == []


def test_a_shift_with_no_date_checks_in_as_unknown(app):
    r = _signed_in(app, "bob").post("/api/checkin/scan",
                                    data={"code": _code(app, "sun_0800_sec1")}).json()
    assert r["checked_in"] and r["timing"] == "unknown"
    assert _notes("1042", "sun_0800_sec1") == ["[CheckIn] | sun_0800_sec1"]


def test_on_time_checks_in_without_asking(app):
    now = datetime.now(ZoneInfo(TZ))
    hhmm = now.strftime("%H%M")
    _sql("INSERT INTO days_table (day_key, day_str, event_date) VALUES ('tod', 'Today', %s)",
         (now.date().isoformat(),))
    _sql("INSERT INTO times_table (time_key, time_str) VALUES ('now', %s)", (f"{hhmm}-{hhmm}",))
    _sql("INSERT INTO day_times VALUES ('tod', 'now')")
    _sql("INSERT INTO schedule (day_key, time_key, pos_key, vol_id) VALUES ('tod', 'now', 'sec2', '1043')")
    r = _signed_in(app, "bob").post("/api/checkin/scan",
                                    data={"code": _code(app, "tod_now_sec2", "1043")}).json()
    assert r["checked_in"] and r["timing"] == "on_time"
    assert _notes("1043", "tod_now_sec2") == ["[CheckIn] | tod_now_sec2"]


def test_reassigned_slot_voids_the_old_code(app):
    code = _code(app, "sat_1300_sec1", "1043")
    _sql("UPDATE schedule SET vol_id = '1042' WHERE day_key = 'sat' AND time_key = '1300' "
         "AND pos_key = 'sec1'")
    try:
        r = _signed_in(app, "bob").post("/api/checkin/scan", data={"code": code, "force": "true"})
        assert r.status_code == 409
    finally:
        _sql("UPDATE schedule SET vol_id = '1043' WHERE day_key = 'sat' AND time_key = '1300' "
             "AND pos_key = 'sec1'")


def test_bad_codes_and_roles(app):
    code = _code(app, "sat_1300_sec1", "1043")
    c = _signed_in(app, "bob")
    assert c.post("/api/checkin/scan", data={"code": code[:-2] + "xx"}).status_code == 400
    assert c.post("/api/checkin/scan", data={"code": "RADIO-17"}).status_code == 400
    assert _client(app).post("/api/checkin/scan", data={"code": code}).status_code == 401
    assert c.post("/api/checkin/rotate_key").status_code == 403


def test_rotating_the_key_voids_codes(app):
    code = _code(app, "sat_1300_sec1", "1043")
    assert _signed_in(app).post("/api/checkin/rotate_key").status_code == 200
    assert _signed_in(app, "bob").post("/api/checkin/scan", data={"code": code}).status_code == 400
    assert _code(app, "sat_1300_sec1", "1043") != code


def test_slot_dialog_button_uses_the_same_rule(app):
    c = _signed_in(app, "bob")
    r = c.post("/api/checkin_vol", data={"vol_id": "1043", "pos_key": "sat_1300_sec1"}).json()
    assert r["needs_confirm"] and r["timing"] == "early"
    r = c.post("/api/checkin_vol", data={"vol_id": "1043", "pos_key": "sat_1300_sec1",
                                         "force": "true"}).json()
    assert r["checked_in"]
    assert c.post("/api/checkin_vol", data={"vol_id": "1042",
                                            "pos_key": "sat_1300_sec1"}).status_code == 409


# ── email_hmac for volunteers added by hand ───────────────────────────────────

HMAC_KEY = b"k" * 32


def _portal_hmac(email):
    """The portal's own function, so a mismatch in normalisation shows up here."""
    sys.path.insert(0, os.path.join(_HERE, "..", "portal"))
    try:
        import security
    finally:
        sys.path.pop(0)
    return security.email_hmac(HMAC_KEY, email)


def _add(app, vol_id, email, enc="ENC"):
    return _signed_in(app).post("/api/add_vol", data={
        "vol_id": vol_id, "first_name": "Hand", "last_name": "Added",
        "email_enc": enc, "email": email})


def test_add_vol_computes_the_portals_hmac(app, monkeypatch):
    import portal_sync
    monkeypatch.setattr(portal_sync, "EMAIL_HMAC_KEY", HMAC_KEY)
    r = _add(app, "h-1", "  Hand@Example.COM ")
    assert r.status_code == 201 and r.json()["email_indexed"] is True
    stored = _sql("SELECT email_hmac FROM volunteers WHERE vol_id = 'h-1'")[0][0]
    assert stored == _portal_hmac("hand@example.com")
    # The plain address is in no column of the row.
    row = _sql("SELECT row_to_json(v)::text FROM volunteers v WHERE vol_id = 'h-1'")[0][0]
    assert "example.com" not in row.lower()
    # Same address again, any case: refused, naming who has it.
    r = _add(app, "h-2", "hand@example.com")
    assert r.status_code == 409 and "h-1" in r.json()["detail"]
    assert not _sql("SELECT 1 FROM volunteers WHERE vol_id = 'h-2'")


def test_add_vol_without_a_key_still_adds_unindexed(app, monkeypatch):
    import portal_sync
    monkeypatch.setattr(portal_sync, "EMAIL_HMAC_KEY", b"")
    r = _add(app, "h-3", "three@example.com")
    assert r.status_code == 201 and r.json()["email_indexed"] is False
    # And a plain address with no ciphertext is ignored rather than hashed.
    monkeypatch.setattr(portal_sync, "EMAIL_HMAC_KEY", HMAC_KEY)
    r = _add(app, "h-4", "four@example.com", enc="")
    assert r.json()["email_indexed"] is False


def test_backfill_fills_only_a_gap(app, monkeypatch):
    import portal_sync
    monkeypatch.setattr(portal_sync, "EMAIL_HMAC_KEY", HMAC_KEY)
    org = _signed_in(app)
    listed = {v["vol_id"]: v for v in org.get("/api/admin/volunteers").json()["volunteers"]}
    assert listed["h-3"]["email_indexed"] is False and listed["h-1"]["email_indexed"] is True
    r = org.post("/api/admin/volunteers/email_hmac", data={"vol_id": "h-3", "email": "Three@example.com"})
    assert r.status_code == 200
    assert _sql("SELECT email_hmac FROM volunteers WHERE vol_id = 'h-3'")[0][0] == \
        _portal_hmac("three@example.com")
    again = org.post("/api/admin/volunteers/email_hmac", data={"vol_id": "h-3", "email": "x@example.com"})
    assert again.status_code == 409                                  # already indexed
    taken = org.post("/api/admin/volunteers/email_hmac", data={"vol_id": "1042", "email": "hand@example.com"})
    assert taken.status_code == 409                                  # h-1's address
    assert org.post("/api/admin/volunteers/email_hmac",
                    data={"vol_id": "1043", "email": "a@b.c"}).status_code == 404   # no email_enc
    assert _signed_in(app, "bob").post("/api/admin/volunteers/email_hmac",
                                       data={"vol_id": "1042", "email": "a@b.c"}).status_code == 403


# ── schedule email template ───────────────────────────────────────────────────

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def test_template_starts_as_the_default_and_saves(app):
    import email_render
    org = _signed_in(app)
    t = org.get("/api/email_template").json()
    assert t["is_default"] and t["body_md"] == email_render.DEFAULT_BODY and t["problems"] == []
    body = "![Logo](asset:logo.png)\n\nHi {{first_name}}\n\n{{shifts}}"
    r = org.post("/api/email_template", data={"subject": "Shifts for {{name}}", "body_md": body,
                                              "shift_md": "{{day}} {{time}} {{qr}}"})
    assert r.status_code == 200 and r.json()["missing_assets"] == ["logo.png"]
    t = org.get("/api/email_template").json()
    assert not t["is_default"] and t["updated_by"] == "alice" and t["missing_assets"] == ["logo.png"]


@pytest.mark.parametrize("data,needle", [
    ({"subject": "x", "body_md": "{{shifts}} {{nope}}", "shift_md": "{{qr}}"}, "{{nope}}"),
    ({"subject": "x", "body_md": "no list", "shift_md": "{{qr}}"}, "{{shifts}}"),
    ({"subject": "x", "body_md": "{{shifts}}", "shift_md": "no code"}, "{{qr}}"),
    ({"subject": "", "body_md": "{{shifts}}", "shift_md": "{{qr}}"}, "subject"),
    ({"subject": "{{shifts}}", "body_md": "{{shifts}}", "shift_md": "{{qr}}"}, "Subject"),
])
def test_template_problems_are_refused(app, data, needle):
    r = _signed_in(app).post("/api/email_template", data=data)
    assert r.status_code == 400 and needle in r.json()["detail"]


def test_assets_are_checked_by_content(app):
    org = _signed_in(app)
    r = org.post("/api/email_template/assets", files={"file": ("My Logo.PNG", PNG, "image/png")},
                 data={"name": "logo"})
    assert r.status_code == 200 and r.json()["name"] == "logo.png"
    assert org.get("/api/email_template/assets/logo.png").content == PNG
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    assert org.post("/api/email_template/assets",
                    files={"file": ("x.png", svg, "image/png")}).status_code == 400
    big = PNG + b"\0" * 1_000_001
    assert org.post("/api/email_template/assets",
                    files={"file": ("big.png", big, "image/png")}).status_code == 413
    # A misleading extension is replaced by the real one.
    r = org.post("/api/email_template/assets", files={"file": ("photo.gif", PNG, "image/gif")})
    assert r.json()["name"] == "photo.png"
    org.post("/api/email_template/assets/delete", data={"name": "photo.png"})
    assert org.get("/api/email_template/assets/photo.png").status_code == 404
    assert _signed_in(app, "bob").post("/api/email_template/assets",
                                       files={"file": ("a.png", PNG, "image/png")}).status_code == 403


def test_site_logo_copies_a_raster_logo_and_refuses_svg(app):
    import base64 as b64
    org = _signed_in(app)
    _sql("INSERT INTO app_kv (app_key, val) VALUES ('app_logo', %s) ON CONFLICT (app_key) "
         "DO UPDATE SET val = excluded.val", ("data:image/svg+xml;base64," + b64.b64encode(b"<svg/>").decode(),))
    assert org.post("/api/email_template/assets/site_logo").status_code == 400
    _sql("UPDATE app_kv SET val = %s WHERE app_key = 'app_logo'",
         ("data:image/png;base64," + b64.b64encode(PNG).decode(),))
    r = org.post("/api/email_template/assets/site_logo")
    assert r.status_code == 200 and r.json()["name"] == "site-logo.png"


# A real 1x1 PNG, padded past the old 1 MB form-field cap: PNG readers ignore bytes after IEND.
_BIG_PNG = PNG + b"\0" * 1_500_000


def test_header_logo_is_an_upload_set_shown_and_removed_by_organizers_only(app):
    import base64 as b64
    org = _signed_in(app)
    assert _signed_in(app, "bob").post(
        "/api/app_header_logo", files={"logo": ("h.png", PNG, "image/png")}).status_code == 403
    # Over the old ~750 KB ceiling: the point of sending a file instead of a data URL.
    assert org.post("/api/app_header_logo",
                    files={"logo": ("h.png", _BIG_PNG, "image/png")}).status_code == 200
    stored = _client(app).get("/api/app_config").json()["header_logo"]
    assert stored == "data:image/png;base64," + b64.b64encode(_BIG_PNG).decode()
    assert org.post("/api/app_header_logo", data={"remove": "true"}).status_code == 200
    assert _client(app).get("/api/app_config").json()["header_logo"] is None


def test_logo_type_comes_from_the_bytes_and_size_is_capped(app):
    org = _signed_in(app)
    webp = b"RIFF\x10\x00\x00\x00WEBPVP8 "
    assert org.post("/api/app_header_logo",          # the browser's claim is ignored
                    files={"logo": ("h.png", webp, "image/png")}).status_code == 200
    assert _client(app).get("/api/app_config").json()["header_logo"].startswith("data:image/webp;")
    for name, body in (("x.txt", b"hello"), ("x.html", b"<html><script>")):
        r = org.post("/api/app_header_logo", files={"logo": (name, body, "image/png")})
        assert r.status_code == 400
    r = org.post("/api/app_header_logo",
                 files={"logo": ("h.png", PNG + b"\0" * 2_000_000, "image/png")})
    assert r.status_code == 413
    # Saving with no file is an error, never a silent removal.
    assert org.post("/api/app_header_logo", data={}).status_code == 400
    assert _client(app).get("/api/app_config").json()["header_logo"] is not None


def test_theme_logo_upload_stores_the_logo_and_its_favicon(app):
    import base64 as b64
    org = _signed_in(app)
    fav = "data:image/png;base64," + b64.b64encode(PNG).decode()
    r = org.post("/api/app_logo", files={"logo": ("t.png", _BIG_PNG, "image/png")},
                 data={"favicon": fav})
    assert r.status_code == 200
    cfg = _client(app).get("/api/app_config").json()
    assert cfg["logo"].startswith("data:image/png;base64,") and cfg["favicon"] == fav
    assert org.post("/api/app_logo", files={"logo": ("t.png", PNG, "image/png")},
                    data={"favicon": "javascript:alert(1)"}).status_code == 400
    assert org.post("/api/app_logo", data={"remove": "true"}).status_code == 200
    cfg = _client(app).get("/api/app_config").json()
    assert cfg["logo"] is None and cfg["favicon"] is None


def test_conference_contact_is_set_with_the_title_and_checked(app):
    org = _signed_in(app)
    assert _client(app).get("/api/app_config").json()["contact_email"] == ""
    r = org.post("/api/setup/set_title", data={
        "title": "ExampleCon", "with_contact": "true", "contact_email": " volunteers@example.org ",
        "conference_url": "https://example.org/", "code_of_conduct_url": "https://example.org/coc"})
    assert r.status_code == 200, r.text
    assert r.json()["contact_email"] == "volunteers@example.org"
    cfg = _client(app).get("/api/app_config").json()
    assert (cfg["title"], cfg["contact_email"], cfg["conference_url"],
            cfg["code_of_conduct_url"]) == ("ExampleCon", "volunteers@example.org",
                                             "https://example.org/", "https://example.org/coc")
    # The title alone (the wizard's Skip) leaves them; a blank field removes just that one.
    assert org.post("/api/setup/set_title", data={"title": "ExampleCon"}).status_code == 200
    assert _client(app).get("/api/app_config").json()["contact_email"] == "volunteers@example.org"
    org.post("/api/setup/set_title", data={
        "title": "ExampleCon", "with_contact": "true", "contact_email": "volunteers@example.org",
        "conference_url": "", "code_of_conduct_url": "https://example.org/coc"})
    cfg = _client(app).get("/api/app_config").json()
    assert cfg["conference_url"] == "" and cfg["code_of_conduct_url"] == "https://example.org/coc"
    _restore_title(org)


def _restore_title(org):
    """Put back the title the later tests expect (BSidesTest), with no conference contact."""
    assert org.post("/api/setup/set_title",
                    data={"title": "BSidesTest", "with_contact": "true"}).status_code == 200


@pytest.mark.parametrize("field, value", [
    ("contact_email", "Volunteers <v@example.org>"), ("contact_email", "a@x.org, b@x.org"),
    ("contact_email", "not-an-address"),
    ("conference_url", "javascript:alert(1)"), ("conference_url", "example.org"),
    ("code_of_conduct_url", "data:text/html,<script>x</script>"),
    ("code_of_conduct_url", "https://example.org/a b"), ("conference_url", "https://"),
])
def test_conference_contact_refuses_anything_unsafe_to_link(app, field, value):
    org = _signed_in(app)
    org.post("/api/setup/set_title", data={"title": "ExampleCon", "with_contact": "true",
                                           "contact_email": "keep@example.org"})
    r = org.post("/api/setup/set_title", data={"title": "Changed", "with_contact": "true",
                                               field: value})
    assert r.status_code == 400
    # Nothing was stored, the title included.
    cfg = _client(app).get("/api/app_config").json()
    assert cfg["title"] == "ExampleCon" and cfg["contact_email"] == "keep@example.org"
    assert _signed_in(app, "bob").post("/api/setup/set_title",
                                       data={"title": "x"}).status_code == 403
    _restore_title(org)


def test_recipients_and_preview(app):
    org = _signed_in(app)
    recips = {v["vol_id"]: v for v in org.get("/api/email_template/recipients").json()}
    assert recips["1042"]["has_email"] and recips["1042"]["shift_count"] == 2
    assert not recips["1043"]["has_email"]
    assert "p-pend" not in recips and "1044" not in recips
    r = org.post("/api/email_template/preview", data={
        "vol_id": "1042", "subject": "Shifts for {{name}}",
        "body_md": "![Logo](asset:logo.png)\n\nHi {{first_name}} <b>x</b>\n\n{{shifts}}",
        "shift_md": "### {{day}} {{time}}\n{{position}}\n\n{{qr}}"}).json()
    assert r["subject"] == "Shifts for Zero Cool [Jim E.]"
    assert "Hi Jim &lt;b&gt;x&lt;/b&gt;" in r["html"]                       # raw HTML escaped
    assert r["html"].count('src="data:image/png;base64,') == 3             # logo + 2 QR codes
    assert "asset:" not in r["html"] and "{{" not in r["html"]
    assert "Saturday 0800-1200" in r["html"] and "Security 1" in r["html"]
    assert "Check-in code: BSC1.sat_0800_sec1." in r["text"]
    assert org.post("/api/email_template/preview", data={"vol_id": "p-pend", "body_md": "{{shifts}}",
                                                         "shift_md": "{{qr}}"}).status_code == 404


def test_package(app):
    import io, json, zipfile, hashlib
    org = _signed_in(app)
    org.post("/api/email_template", data={
        "subject": "Shifts", "body_md": "![L](asset:missing.png)\n{{shifts}}", "shift_md": "{{qr}}"})
    r = org.get("/api/email_template/package")
    assert r.status_code == 409 and "missing.png" in r.json()["detail"]
    org.post("/api/email_template", data={
        "subject": "Shifts for {{first_name}}",
        "body_md": "![Logo](asset:logo.png)\n\nHi {{first_name}}\n\n{{shifts}}",
        "shift_md": "**{{position}}** {{date}}\n\n{{qr}}"})
    r = org.get("/api/email_template/package")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        files = {name: z.read(name) for name in z.namelist()}
    assert sorted(files) == ["assets/logo.png", "body.html", "body.txt", "manifest.json",
                                    "shift.html", "shift.txt", "template.md"]
    m = json.loads(files["manifest.json"])
    assert m["format"] == 1 and m["subject"] == "Shifts for {{first_name}}"
    assert m["created_by"] == "alice" and m["event_title"] == "BSidesTest"
    # Only the image the template uses; site-logo.png was uploaded but isn't referenced.
    assert [a["name"] for a in m["assets"]] == ["logo.png"]
    assert m["assets"][0]["sha256"] == hashlib.sha256(files["assets/logo.png"]).hexdigest()
    body_html = files["body.html"].decode()
    assert "{{shifts}}" in body_html and 'src="asset:logo.png"' in body_html
    assert "{{qr}}" in files["shift.html"].decode()
    assert files["body.txt"].decode().startswith("Logo")


# ── the portal's editable content (routers/portal_content.py) ─────────────────────────────

def test_the_content_rules_match_the_portals_copy_exactly():
    """backend/portal_content.py and portal/content.py share no code but must agree."""
    here = os.path.dirname(os.path.abspath(__file__))

    def body(path):
        with open(path, encoding="utf-8") as f:
            src = f.read()
        return src[src.index('"""', 3) + 3:]
    assert body(os.path.join(here, "portal_content.py")) == \
        body(os.path.join(here, "..", "portal", "content.py"))


def test_content_starts_at_the_defaults_and_counts_answers(app):
    import json
    import portal_content as pc
    _sql("UPDATE volunteers SET answers = %s WHERE vol_id = '1043'",
         (json.dumps({"tshirt": "M", "gone_question": "x"}),))
    r = _signed_in(app).get("/api/admin/portal/content")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["saved"] == {"landing": None, "questionnaire": None, "invite": None}
    assert body["defaults"]["questionnaire"] == pc.DEFAULT_FIELDS
    assert body["answer_counts"]["tshirt"] >= 1 and body["answer_counts"]["gone_question"] == 1
    assert body["flexible_key"] == "flexible" and "microphone" in body["icons"]
    assert _signed_in(app, "bob").get("/api/admin/portal/content").status_code == 403


def test_saving_checks_stores_and_resets_each_part(app):
    import json
    import portal_content as pc
    org = _signed_in(app)
    fields = pc.DEFAULT_FIELDS + [{"key": "pronouns", "section": "additional", "type": "text",
                                   "required": False, "editable": True, "label": "Pronouns",
                                   "max": 40}]
    r = org.post("/api/admin/portal/content",
                 data={"part": "questionnaire", "value": json.dumps(fields)})
    assert r.status_code == 200, r.text
    assert org.get("/api/admin/portal/content").json()["saved"]["questionnaire"][-1]["key"] \
        == "pronouns"
    # A part that fails its check is refused with the reason, and nothing changes.
    bad = dict(pc.DEFAULT_INVITE, body="Hi {{ first_name }}, no link here.")
    r = org.post("/api/admin/portal/content", data={"part": "invite", "value": json.dumps(bad)})
    assert r.status_code == 400 and "link" in r.json()["detail"]
    assert org.get("/api/admin/portal/content").json()["saved"]["invite"] is None
    # Reset: no value.
    assert org.post("/api/admin/portal/content", data={"part": "questionnaire"}).json() == \
        {"ok": True, "saved": None}
    assert org.get("/api/admin/portal/content").json()["saved"]["questionnaire"] is None
    assert org.post("/api/admin/portal/content",
                    data={"part": "nope", "value": "{}"}).status_code == 400
    assert _signed_in(app, "bob").post("/api/admin/portal/content",
                                       data={"part": "invite"}).status_code == 403


@pytest.mark.parametrize("template", [
    "{% for x in range(99999999) %}{% endfor %}{{ link }}",
    "{{ link.__class__ }}", "{{ link }}{{ secret }}", "{% set x = 1 %}{{ link }}",
    "{{ link }}{% include 'other' %}", "{{ link|attr('x') }}", "{{ link() }}",
])
def test_an_invite_template_outside_the_allowed_jinja_is_refused(app, template):
    import json
    r = _signed_in(app).post("/api/admin/portal/content", data={
        "part": "invite", "value": json.dumps({"subject": "Hi", "body": template})})
    assert r.status_code == 400, template


def test_previews_render_as_the_portal_will(app):
    import json
    import portal_content as pc
    org = _signed_in(app)
    landing = dict(pc.DEFAULT_LANDING,
                   intro="Hello from **{{ conference }}** <script>alert(1)</script> "
                         "[bad](javascript:alert(1)) [ok](https://example.org/)")
    r = org.post("/api/admin/portal/content/preview",
                 data={"part": "landing", "value": json.dumps(landing)})
    assert r.status_code == 200, r.text
    intro = r.json()["intro"]
    assert "<strong>BSidesTest</strong>" in intro and "<script>" not in intro
    assert "&lt;script&gt;" in intro and 'href="javascript' not in intro
    assert '<a href="https://example.org/">ok</a>' in intro
    assert r.json()["roles"] == pc.DEFAULT_LANDING["roles"]
    r = org.post("/api/admin/portal/content/preview",
                 data={"part": "invite", "value": json.dumps(pc.DEFAULT_INVITE)})
    assert r.json()["subject"] == "You're invited to volunteer at BSidesTest"
    assert "Hi Alex," in r.json()["body"] and "/apply/signup?invite=" in r.json()["body"]


def test_content_is_pushed_as_saved_and_hashed():
    import portal_sync

    class DB:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, *_):
            rows = self.rows

            class Cur:
                def fetchall(self):
                    return rows
            return Cur()
    a = portal_sync.build_content(DB([]))
    assert {k: a[k] for k in ("landing", "questionnaire", "invite")} == \
        {"landing": None, "questionnaire": None, "invite": None}
    b = portal_sync.build_content(DB([{"app_key": "portal_invite",
                                       "val": '{"subject": "S", "body": "{{ link }}"}'}]))
    assert b["invite"] == {"subject": "S", "body": "{{ link }}"}
    assert b["content_hash"] != a["content_hash"]

