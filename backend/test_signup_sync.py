"""The scheduler's half of volunteer signup, against a REAL Postgres.

Covers: mirroring a signup's timeslot picks on pull, approve / decline / badge-number re-key, the
portal_dirty retry, the settings push (signup open, email public key), the Pending Volunteers
endpoint, and pending signups staying out of assignment pickers.

The portal is a separate app and can't be imported here (both define `db` and `main`), so its
side of the wire is a fake `_request` that records what the scheduler sends. The portal's own
handling of those requests is tested in portal/test_signup.py.

Skipped unless TEST_PG_HOST is set; DROPS the target database's public schema. Same variables
as test_passkeys.py.
"""
import asyncio
import json
import os
import sys

import pytest

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST (see test_passkeys.py)",
                allow_module_level=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import db as dbm  # noqa: E402
import portal_sync  # noqa: E402

PUBKEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEexample0000000000000000000000000000000000000000000000
0000000000000000000000000000000000==
-----END PUBLIC KEY-----"""


def _plain_conn():
    import psycopg2
    return psycopg2.connect(**{k: v for k, v in dbm._dsn().items() if k != "cursor_factory"})


def _sql(stmt, params=()):
    conn = _plain_conn()
    with conn.cursor() as cur:
        cur.execute(stmt, params)
        rows = cur.fetchall() if cur.description else None
    conn.commit()
    conn.close()
    return rows


@pytest.fixture(scope="module")
def app():
    dbm.DB_HOST = os.environ["TEST_PG_HOST"]
    dbm.DB_PORT = int(os.getenv("TEST_PG_PORT", "5432"))
    dbm.DB_NAME = os.getenv("TEST_PG_DB", "scheduler_test")
    dbm.DB_USER = os.getenv("TEST_PG_USER", "scheduler")
    dbm.DB_PASSWORD = os.getenv("TEST_PG_PASSWORD", "")
    dbm.close_pool()
    conn = _plain_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.close()
    cwd = os.getcwd()
    os.chdir(_HERE)
    try:
        dbm.open_db()
    finally:
        os.chdir(cwd)

    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from routers import auth, portal_admin, schedule, volunteers
    application = FastAPI()
    for r in (auth, portal_admin, schedule, volunteers):
        application.include_router(r.router)
    application.add_middleware(SessionMiddleware, secret_key="test-secret")

    from argon2 import PasswordHasher
    _sql("INSERT INTO users (user_id, username, password_hash, role) VALUES (%s, %s, %s, %s)",
         ("u-org", "org", PasswordHasher().hash("org-password"), "organizer"))
    # The 2027 shape: three overlapping starts a day, Sunday's last at 1400.
    _sql("""INSERT INTO days_table (day_key, day_str) VALUES ('sat', 'Saturday'), ('sun', 'Sunday');
            INSERT INTO times_table (time_key, time_str) VALUES
                ('0800', '0800-1200'), ('1130', '1130-1530'), ('1500', '1500-1900'),
                ('1400', '1400-1800');
            INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES
                ('sec1', 'Security', 'Security 1'), ('sec2', 'Security', 'Security 2');
            INSERT INTO day_times (day_key, time_key) VALUES
                ('sat', '0800'), ('sat', '1130'), ('sat', '1500'),
                ('sun', '0800'), ('sun', '1130'), ('sun', '1400');
            INSERT INTO schedule (day_key, time_key, pos_key)
                SELECT day_key, time_key, p.pos_key FROM day_times, positions p;""")
    yield application
    dbm.close_pool()


@pytest.fixture
def org(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="https://scheduler.test")
    assert c.post("/api/login", data={"username": "org", "password": "org-password"}).status_code == 200
    return c


class FakePortal:
    """Stands in for portal_sync._request; records calls, answers from canned data."""

    def __init__(self):
        self.calls = []
        self.pull = []          # volunteers the next GET /api/sync/volunteers returns
        self.config = {"portal_signup_open": "0"}
        self.security_events = []   # what GET /api/sync/security_events serves, by id

    async def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/api/sync/volunteers"):
            rows, self.pull = self.pull, []
            return {"volunteers": rows, "cursor": 100, "more": False}
        if method == "GET" and path.startswith("/api/sync/security_events"):
            since = int(path.split("since=")[1].split("&")[0])
            rows = [e for e in self.security_events if e["id"] > since][:500]
            return {"events": rows, "next": rows[-1]["id"] if rows else since}
        if method == "GET" and path == "/api/sync/config":
            return {"settings": dict(self.config)}
        if method == "POST" and path == "/api/sync/settings":
            self.config.update({k: v for k, v in payload["settings"].items()
                                if k == "portal_signup_open"})
        return {"ok": True}

    def posted(self, path):
        return [p for m, pth, p in self.calls if m == "POST" and pth == path]


@pytest.fixture
def portal(monkeypatch):
    fake = FakePortal()
    monkeypatch.setattr(portal_sync, "_request", fake)
    monkeypatch.setattr(portal_sync, "request_push", lambda: None)
    return fake


_opened = []


def _conn():
    """A fresh PgClient, closed after the test. PgClient.close() is a no-op, so without this a
    read left open (say, by a push that raised) holds locks that block the migration test."""
    c = dbm.open_db()
    _opened.append(c)
    return c


@pytest.fixture(autouse=True)
def _close_connections():
    yield
    while _opened:
        _opened.pop()._conn.close()


def _signup(vol_id, email_hmac, prefs=(("sat", "0800"), ("sat", "1500")), name="Ada L.",
            **extra):
    rec = {"vol_id": vol_id, "name": name, "status": "pending",
           "time_pref": "Saturday 0800-1200;Saturday 1500-1900", "add_time": "", "add_any": 0,
           "email_hmac": email_hmac, "email_enc": "ct", "email_key_fp": "SHA256:x",
           "answers": json.dumps({"tshirt": "L"}), "possible_duplicate_of": None,
           "picks": [{"day_key": d, "time_key": t, "kind": "pref", "rank": i}
                     for i, (d, t) in enumerate(prefs, start=1)]}
    rec.update(extra)
    return rec


def _pull(portal, *recs):
    portal.pull = list(recs)
    return asyncio.run(portal_sync.pull_volunteers(_conn()))


# ── pull ──────────────────────────────────────────────────────────────────────

def _remaining(day, time):
    snap = portal_sync.build_snapshot(_conn())
    return next(dt["remaining"] for dt in snap["day_times"]
                if (dt["day_key"], dt["time_key"]) == (day, time))


def test_a_pulled_signup_brings_its_picks_and_counts_against_space(app, portal):
    before = _remaining("sat", "1130")
    _pull(portal, _signup("p-aaaa0001", "h1", picks=[
        {"day_key": "sat", "time_key": "1130", "kind": "pref", "rank": 1},
        {"day_key": "sun", "time_key": "1400", "kind": "pref", "rank": 2},
        {"day_key": "sat", "time_key": "2200", "kind": "add", "rank": None}]))
    picks = _sql("SELECT day_key, time_key, kind, rank FROM vol_picks "
                 "WHERE vol_id = 'p-aaaa0001' ORDER BY rank")
    assert picks == [("sat", "1130", "pref", 1), ("sun", "1400", "pref", 2)]  # unknown dropped
    assert _remaining("sat", "1130") == before - 1


def test_pending_signups_stay_out_of_the_pickers(app, org, portal):
    _pull(portal, _signup("p-aaaa0002", "h2", name="Pending P."))
    names = [v["name"] for v in org.get("/api/get_vols").json()]
    assert "Pending P." not in names


def test_the_volunteer_list_survives_odd_time_prefs(app, org):
    _sql("INSERT INTO volunteers (vol_id, name, time_pref) VALUES "
         "('v-none', 'No Pref', ''), ('v-one', 'One Pref', 'Saturday AM'), "
         "('v-three', 'Three Pref', 'b;a;c')")
    vols = {v["key"]: v["requested"] for v in org.get("/api/get_vols").json()}
    assert vols["v-none"] == "Requested: (none)"
    assert vols["v-one"] == "Requested:\n - Saturday AM"
    assert vols["v-three"] == "Requested:\n - a\n - b\n - c"


# ── pending list ──────────────────────────────────────────────────────────────

def test_pending_lists_picks_and_resembling_volunteers(app, org, portal):
    _sql("INSERT INTO volunteers (vol_id, name, time_pref) VALUES ('1042', 'Jim E.', 'x;y')")
    _pull(portal, _signup("p-aaaa0003", "h3", name="Jim E.",
                          possible_duplicate_of=json.dumps(["1042"])))
    rec = next(p for p in org.get("/api/admin/portal/pending").json()["pending"]
               if p["vol_id"] == "p-aaaa0003")
    assert [(p["day_key"], p["time_key"], p["label"]) for p in rec["picks"]] == [
        ("sat", "0800", "Saturday 0800-1200"), ("sat", "1500", "Saturday 1500-1900")]
    assert [d["vol_id"] for d in rec["possible_duplicates"]] == ["1042"]
    assert rec["answers"] == {"tshirt": "L"}


# ── approve / decline ─────────────────────────────────────────────────────────

def test_approve_marks_active_and_owes_the_portal(app, org, portal):
    _pull(portal, _signup("p-aaaa0004", "h4"))
    r = org.post("/api/admin/portal/approve", data={"vol_id": "p-aaaa0004"})
    assert r.status_code == 200 and r.json()["vol_id"] == "p-aaaa0004"
    assert _sql("SELECT status, portal_dirty FROM volunteers WHERE vol_id = 'p-aaaa0004'") \
        == [("active", True)]
    assert org.post("/api/admin/portal/approve", data={"vol_id": "p-aaaa0004"}).status_code == 409

    # A stale copy from the portal must not undo the approval before it lands there.
    _pull(portal, _signup("p-aaaa0004", "h4", status="pending"))
    assert _sql("SELECT status FROM volunteers WHERE vol_id = 'p-aaaa0004'") == [("active",)]

    pushed = asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    assert pushed >= 1
    sent = next(v for v in portal.posted("/api/sync/volunteers")[-1]["volunteers"]
                if v["vol_id"] == "p-aaaa0004")
    assert sent["status"] == "active"
    # Scheduler-owned columns only: picks, answers and name parts are the volunteer's.
    assert set(sent) == {"vol_id", "name", "status", "inactive_at"}
    assert _sql("SELECT portal_dirty FROM volunteers WHERE vol_id = 'p-aaaa0004'") == [(False,)]


def test_a_failed_push_stays_owed(app, org, portal, monkeypatch):
    _pull(portal, _signup("p-aaaa0005", "h5"))
    org.post("/api/admin/portal/decline", data={"vol_id": "p-aaaa0005"})

    async def down(*a, **k):
        raise RuntimeError("portal unreachable")
    monkeypatch.setattr(portal_sync, "_request", down)
    with pytest.raises(RuntimeError):
        asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    assert _sql("SELECT status, inactive_at IS NOT NULL, portal_dirty FROM volunteers "
                "WHERE vol_id = 'p-aaaa0005'") == [("pending", True, True)]


def test_approve_with_a_badge_number_rekeys(app, org, portal):
    _pull(portal, _signup("p-aaaa0006", "h6"))
    r = org.post("/api/admin/portal/approve", data={"vol_id": "p-aaaa0006", "new_vol_id": "2207"})
    assert r.status_code == 200 and r.json()["vol_id"] == "2207"
    assert _sql("SELECT 1 FROM volunteers WHERE vol_id = 'p-aaaa0006'") == []
    assert _sql("SELECT status, email_hmac, portal_rekey_from FROM volunteers "
                "WHERE vol_id = '2207'") == [("active", "h6", "p-aaaa0006")]
    assert _sql("SELECT time_key FROM vol_picks WHERE vol_id = '2207' ORDER BY rank") \
        == [("0800",), ("1500",)]

    # The portal still has the old id; a pull of it must not resurrect the pending row.
    _pull(portal, _signup("p-aaaa0006", "h6"))
    assert _sql("SELECT 1 FROM volunteers WHERE vol_id = 'p-aaaa0006'") == []

    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    payload = portal.posted("/api/sync/volunteers")[-1]
    assert {"vol_id": "p-aaaa0006", "new_vol_id": "2207"} in payload["rekeys"]
    assert _sql("SELECT portal_dirty, portal_rekey_from FROM volunteers WHERE vol_id = '2207'") \
        == [(False, None)]


@pytest.mark.parametrize("badge, status", [
    ("p-1234", 400), ("has space", 400), ("x" * 33, 400), ("1042", 409),
])
def test_bad_or_taken_badge_numbers_are_refused(app, org, portal, badge, status):
    _sql("INSERT INTO volunteers (vol_id, name) VALUES ('1042', 'Jim E.') ON CONFLICT DO NOTHING")
    _pull(portal, _signup("p-aaaa0007", "h7"))
    r = org.post("/api/admin/portal/approve", data={"vol_id": "p-aaaa0007", "new_vol_id": badge})
    assert r.status_code == status
    assert _sql("SELECT status FROM volunteers WHERE vol_id = 'p-aaaa0007'") == [("pending",)]


def test_rekey_refuses_a_volunteer_already_referenced(app, org, portal):
    _pull(portal, _signup("p-aaaa0008", "h8"))
    _sql("INSERT INTO vol_notes (vol_id, note, username) VALUES ('p-aaaa0008', 'called them', 'org')")
    r = org.post("/api/admin/portal/approve", data={"vol_id": "p-aaaa0008", "new_vol_id": "3001"})
    assert r.status_code == 409 and "vol_notes" in r.json()["detail"]


def test_decline_makes_a_signup_inactive_without_touching_approval(app, org, portal):
    _pull(portal, _signup("p-aaaa0009", "h9"))
    assert org.post("/api/admin/portal/decline", data={"vol_id": "p-aaaa0009"}).status_code == 200
    assert _sql("SELECT status, inactive_at IS NOT NULL, portal_dirty FROM volunteers "
                "WHERE vol_id = 'p-aaaa0009'") == [("pending", True, True)]
    assert "p-aaaa0009" not in [p["vol_id"] for p in
                                org.get("/api/admin/portal/pending").json()["pending"]]
    assert org.post("/api/admin/portal/decline", data={"vol_id": "nope"}).status_code == 404


# ── settings ──────────────────────────────────────────────────────────────────

def test_signup_cannot_open_without_a_public_key_and_settings_reach_the_portal(app, org, portal):
    _sql("DELETE FROM app_kv WHERE app_key = 'email_pubkey'")
    r = org.post("/api/admin/portal/settings", data={"signup_open": "1"})
    assert r.status_code == 409 and "public key" in r.json()["detail"]

    _sql("INSERT INTO app_kv (app_key, val) VALUES ('email_pubkey', %s)", (PUBKEY,))
    assert org.post("/api/admin/portal/settings", data={"signup_open": "1"}).json()["signup_open"]
    status = org.get("/api/admin/portal/status").json()
    assert status["signup_open"] is True and status["has_pubkey"] is True
    assert status["overage_pct"] == 0
    assert status["open_timeslots"] == sum(1 for n in portal_sync.compute_remaining(_conn()).values()
                                           if n > 0)

    pushed = asyncio.run(portal_sync.sync_settings(_conn(), {"pubkey_fp": None}))
    assert pushed == ["email_pubkey", "portal_signup_open"]
    sent = portal.posted("/api/sync/settings")[-1]["settings"]
    assert sent == {"portal_signup_open": "1", "email_pubkey": PUBKEY}

    # In agreement: nothing to send.
    fp = portal_sync.pubkey_fingerprint(PUBKEY)
    assert asyncio.run(portal_sync.sync_settings(_conn(), {"pubkey_fp": fp})) is None


def test_pubkey_fingerprint_matches_the_portals_format():
    # SHA256:<base64 of sha256(DER)>, padding stripped — the portal's security.pubkey_fingerprint.
    import base64
    import hashlib
    der = b"\x30\x03\x02\x01\x00"
    pem = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(der).decode() + "\n-----END PUBLIC KEY-----"
    want = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode().rstrip("=")
    assert portal_sync.pubkey_fingerprint(pem) == want


# ── Admin → Volunteers ────────────────────────────────────────────────────────

def test_the_volunteer_list_shows_every_status_with_schedule_and_checkins(app, org, portal):
    _sql("""INSERT INTO times_table (time_key, time_str) VALUES ('1500', '1500-1900')
              ON CONFLICT DO NOTHING;
            INSERT INTO day_times (day_key, time_key) VALUES ('sat', '1500') ON CONFLICT DO NOTHING;
            INSERT INTO positions (pos_key, pos_cat, pos_str) VALUES ('reg1', 'Registration', 'Reg 1')
              ON CONFLICT DO NOTHING;
            INSERT INTO schedule (day_key, time_key, pos_key) VALUES ('sat', '1500', 'reg1')
              ON CONFLICT DO NOTHING;""")
    _pull(portal, _signup("p-list0001", "hl1", name="Pending Person"))
    _pull(portal, _signup("p-list0002", "hl2", name="Declined Person"))
    org.post("/api/admin/portal/decline", data={"vol_id": "p-list0002"})
    _sql("""INSERT INTO volunteers (vol_id, name, time_pref, add_time)
              VALUES ('5001', 'Active Person', 'Saturday AM;Saturday PM', 'Any');
            UPDATE schedule SET vol_id = '5001'
              WHERE (day_key, time_key, pos_key) IN (('sat', '1500', 'reg1'), ('sat', '0800', 'sec1'));
            INSERT INTO vol_notes (vol_id, note, username)
              VALUES ('5001', '[CheckIn] | sat_0800_sec1', 'org');""")

    r = org.get("/api/admin/volunteers")
    assert r.status_code == 200
    by_id = {v["vol_id"]: v for v in r.json()["volunteers"]}
    assert by_id["p-list0001"]["status"] == "pending"
    assert by_id["p-list0002"]["status"] == "pending" and by_id["p-list0002"]["inactive"] is True
    assert [p["label"] for p in by_id["p-list0001"]["picks"]] == ["Saturday 0800-1200",
                                                                  "Saturday 1500-1900"]
    assert by_id["p-list0001"]["answers"] == {"tshirt": "L"}
    assert by_id["p-list0001"]["email_enc"] == "ct"          # ciphertext, for the browser
    assert by_id["p-list0001"]["shift_count"] == 0

    active = by_id["5001"]
    assert active["status"] == "active" and active["picks"] == []       # an organizer-added one
    assert active["time_pref"] == "Saturday AM;Saturday PM"             # so the text still shows
    assert active["shift_count"] == 2
    assert [(s["time_key"], s["pos_str"], s["checked_in"]) for s in active["shifts"]] == [
        ("0800", "Security 1", True), ("1500", "Reg 1", False)]         # schedule order
    assert active["shifts"][0]["day_str"] == "Saturday"
    _sql("UPDATE schedule SET vol_id = NULL WHERE vol_id = '5001'")


def test_the_volunteer_list_is_for_organizers_only(app):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient
    _sql("INSERT INTO users (user_id, username, password_hash, role) VALUES (%s, %s, %s, %s) "
         "ON CONFLICT DO NOTHING", ("u-ci", "ci", PasswordHasher().hash("ci-password"), "checkin"))
    c = TestClient(app, base_url="https://scheduler.test")
    assert c.get("/api/admin/volunteers").status_code == 401
    c.post("/api/login", data={"username": "ci", "password": "ci-password"})
    assert c.get("/api/admin/volunteers").status_code == 403


# ── volunteer edits: column ownership and shift flags ─────────────────────────

def test_a_volunteer_edit_lands_even_while_an_approval_is_waiting(app, org, portal):
    """The crossing case: approved here, edited in the portal before the approval went out."""
    _pull(portal, _signup("p-edit0001", "he1"))
    org.post("/api/admin/portal/approve", data={"vol_id": "p-edit0001"})       # now dirty
    _pull(portal, _signup("p-edit0001", "he1", status="pending",
                          time_pref="Saturday 0800-1200;Sunday 1400-1800",
                          answers=json.dumps({"tshirt": "XL"}),
                          picks=[{"day_key": "sat", "time_key": "0800", "kind": "pref", "rank": 1},
                                 {"day_key": "sun", "time_key": "1400", "kind": "pref", "rank": 2}]))
    row = _sql("SELECT status, time_pref, answers->>'tshirt', portal_dirty FROM volunteers "
               "WHERE vol_id = 'p-edit0001'")
    assert row == [("active", "Saturday 0800-1200;Sunday 1400-1800", "XL", True)]
    assert _sql("SELECT day_key, time_key FROM vol_picks WHERE vol_id = 'p-edit0001' "
                "ORDER BY rank") == [("sat", "0800"), ("sun", "1400")]
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    sent = next(v for v in portal.posted("/api/sync/volunteers")[-1]["volunteers"]
                if v["vol_id"] == "p-edit0001")
    assert sent == {"vol_id": "p-edit0001", "name": "Ada L.", "status": "active",
                    "inactive_at": None}


def _flags(org):
    r = org.get("/api/admin/shift_flags")
    assert r.status_code == 200, r.text
    return {(f["vol_id"], f["day_key"], f["time_key"]): f for f in r.json()["flags"]}


def _picks(*cells):
    return [{"day_key": d, "time_key": t, "kind": "pref", "rank": i}
            for i, (d, t) in enumerate(cells, start=1)]


def test_dropping_an_assigned_shift_raises_a_flag_that_clears_itself(app, org, portal):
    _pull(portal, _signup("p-flag0001", "hf1", name="Flag F."))
    org.post("/api/admin/portal/approve", data={"vol_id": "p-flag0001"})
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    _sql("UPDATE schedule SET vol_id = 'p-flag0001' WHERE day_key = 'sat' AND time_key = '1500' "
         "AND pos_key = 'sec1'")

    # Dropping sat 0800 (not assigned) raises nothing; dropping sat 1500 (assigned) does.
    _pull(portal, _signup("p-flag0001", "hf1", name="Flag F.", status="active",
                          picks=_picks(("sat", "1500"), ("sun", "1400"))))
    assert ("p-flag0001", "sat", "0800") not in _flags(org)
    _pull(portal, _signup("p-flag0001", "hf1", name="Flag F.", status="active",
                          picks=_picks(("sun", "0800"), ("sun", "1400"))))
    flag = _flags(org)[("p-flag0001", "sat", "1500")]
    assert flag["name"] == "Flag F." and flag["time_str"] == "1500-1900"
    assert flag["slots"] == [{"key": "sat_1500_sec1", "pos_str": "Security 1"}]

    # Picking it again clears it; dropping it again brings it back.
    _pull(portal, _signup("p-flag0001", "hf1", name="Flag F.", status="active",
                          picks=_picks(("sat", "1500"), ("sun", "1400"))))
    assert ("p-flag0001", "sat", "1500") not in _flags(org)
    _pull(portal, _signup("p-flag0001", "hf1", name="Flag F.", status="active",
                          picks=_picks(("sun", "0800"), ("sun", "1400"))))
    assert ("p-flag0001", "sat", "1500") in _flags(org)

    # Unassigning them clears it with no other step.
    org.post("/api/clear_timeslot", data={"pos_key": "sat_1500_sec1"})
    assert ("p-flag0001", "sat", "1500") not in _flags(org)


def test_keep_dismisses_a_flag_until_the_shift_is_dropped_again(app, org, portal):
    _pull(portal, _signup("p-flag0002", "hf2", name="Keep K."))
    org.post("/api/admin/portal/approve", data={"vol_id": "p-flag0002"})
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    _sql("UPDATE schedule SET vol_id = 'p-flag0002' WHERE day_key = 'sat' AND time_key = '0800' "
         "AND pos_key = 'sec2'")
    drop = _signup("p-flag0002", "hf2", name="Keep K.", status="active",
                   picks=_picks(("sat", "1500"), ("sun", "1400")))
    _pull(portal, drop)
    assert ("p-flag0002", "sat", "0800") in _flags(org)
    r = org.post("/api/admin/shift_flags/keep",
                 data={"vol_id": "p-flag0002", "day_key": "sat", "time_key": "0800"})
    assert r.status_code == 200
    assert ("p-flag0002", "sat", "0800") not in _flags(org)
    assert org.post("/api/admin/shift_flags/keep", data={
        "vol_id": "p-flag0002", "day_key": "sat", "time_key": "0800"}).status_code == 404
    _pull(portal, _signup("p-flag0002", "hf2", name="Keep K.", status="active",
                          picks=_picks(("sat", "0800"), ("sun", "1400"))))      # re-picked
    _pull(portal, drop)                                                          # dropped again
    assert ("p-flag0002", "sat", "0800") in _flags(org)
    _sql("UPDATE schedule SET vol_id = NULL WHERE vol_id = 'p-flag0002'")


def test_shift_flags_are_for_organizers_only(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="https://scheduler.test")
    assert c.get("/api/admin/shift_flags").status_code == 401


# ── cancelled and inactive: separate from approval ────────────────────────────

def _approved(org, portal, vol_id, h, **kw):
    _pull(portal, _signup(vol_id, h, **kw))
    org.post("/api/admin/portal/approve", data={"vol_id": vol_id})
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))


def test_a_cancel_is_taken_even_while_an_approval_waits_and_keeps_it(app, org, portal):
    _pull(portal, _signup("p-can00001", "hc1"))
    org.post("/api/admin/portal/approve", data={"vol_id": "p-can00001"})      # dirty
    _pull(portal, _signup("p-can00001", "hc1", status="pending",
                          cancelled_at="2026-10-25T10:00:00Z"))
    assert _sql("SELECT status, cancelled_at IS NOT NULL FROM volunteers "
                "WHERE vol_id = 'p-can00001'") == [("active", True)]
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    sent = next(v for v in portal.posted("/api/sync/volunteers")[-1]["volunteers"]
                if v["vol_id"] == "p-can00001")
    assert sent["status"] == "active" and "cancelled_at" not in sent     # never pushed
    # Rejoining clears it on the next pull; approval was never touched.
    _pull(portal, _signup("p-can00001", "hc1", status="active", cancelled_at=None))
    assert _sql("SELECT status, cancelled_at FROM volunteers WHERE vol_id = 'p-can00001'") \
        == [("active", None)]


def test_cancelled_and_inactive_neither_count_nor_appear_in_pickers(app, org, portal):
    base = _remaining("sun", "1130")
    for vid, h in (("p-gone0001", "hg1"), ("p-gone0002", "hg2")):
        _approved(org, portal, vid, h, name=f"Gone {vid[-1]}",
                  picks=_picks(("sun", "1130"), ("sat", "1500")))
    assert _remaining("sun", "1130") == max(0, base - 2)
    assert {"p-gone0001", "p-gone0002"} <= set(_avail(org, "sun_1130_sec1"))

    _pull(portal, _signup("p-gone0001", "hg1", name="Gone 1", status="active",
                          cancelled_at="2026-10-25T10:00:00Z",
                          picks=_picks(("sun", "1130"), ("sat", "1500"))))
    org.post("/api/admin/volunteers/set_inactive", data={"vol_id": "p-gone0002", "inactive": "1"})
    assert _remaining("sun", "1130") == base
    avail = _avail(org, "sun_1130_sec1")
    assert "p-gone0001" not in avail and "p-gone0002" not in avail
    names = [v["name"] for v in org.get("/api/get_vols").json()]
    assert "Gone 1" not in names and "Gone 2" not in names
    assert _sql("SELECT status FROM volunteers WHERE vol_id IN ('p-gone0001', 'p-gone0002')") \
        == [("active",), ("active",)]                                   # approval kept


def test_set_inactive_and_reinstate_reach_the_portal(app, org, portal):
    _approved(org, portal, "p-ina00001", "hi1")
    assert org.post("/api/admin/volunteers/set_inactive",
                    data={"vol_id": "p-ina00001", "inactive": "1"}).json()["inactive"] is True
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    sent = next(v for v in portal.posted("/api/sync/volunteers")[-1]["volunteers"]
                if v["vol_id"] == "p-ina00001")
    assert sent["status"] == "active" and sent["inactive_at"].endswith("Z")
    org.post("/api/admin/volunteers/set_inactive", data={"vol_id": "p-ina00001", "inactive": "0"})
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))
    sent = next(v for v in portal.posted("/api/sync/volunteers")[-1]["volunteers"]
                if v["vol_id"] == "p-ina00001")
    assert sent["status"] == "active" and sent["inactive_at"] is None
    assert org.post("/api/admin/volunteers/set_inactive",
                    data={"vol_id": "p-ina00001", "inactive": "x"}).status_code == 400
    assert org.post("/api/admin/volunteers/set_inactive",
                    data={"vol_id": "nope", "inactive": "1"}).status_code == 404


def test_slots_held_by_cancelled_or_inactive_volunteers_need_replacing(app, org, portal):
    _approved(org, portal, "p-rep00001", "hr1", name="Rep R.",
              picks=_picks(("sat", "0800"), ("sun", "1400")))
    org.post("/api/assign_vol", data={"pos_key": "sun_1400_sec2", "vol_id": "p-rep00001"})
    assert all(r["vol_id"] != "p-rep00001" for r in org.get("/api/admin/shift_flags").json()["replace"])

    org.post("/api/admin/volunteers/set_inactive", data={"vol_id": "p-rep00001", "inactive": "1"})
    rep = [r for r in org.get("/api/admin/shift_flags").json()["replace"]
           if r["vol_id"] == "p-rep00001"]
    assert rep == [{"key": "sun_1400_sec2", "day_str": "Sunday", "time_str": "1400-1800",
                    "pos_str": "Security 2", "vol_id": "p-rep00001", "name": "Rep R.",
                    "reason": "inactive"}]
    chip = next(p for d in org.get("/api/get_schedule").json()["schedule"] for t in d["times"]
                for c in t["categories"] for p in c["positions"] if p["key"] == "sun_1400_sec2")
    assert chip["needs_replacement"] is True and chip["vol_id"] == "p-rep00001"   # still assigned

    # Assigning someone else is the replacement; the entry goes.
    _sql("INSERT INTO volunteers (vol_id, name) VALUES ('sub1', 'Sub S.') ON CONFLICT DO NOTHING")
    org.post("/api/assign_vol", data={"pos_key": "sun_1400_sec2", "vol_id": "sub1"})
    assert all(r["vol_id"] != "p-rep00001" for r in org.get("/api/admin/shift_flags").json()["replace"])
    _sql("UPDATE schedule SET vol_id = NULL WHERE vol_id = 'sub1'")


# ── overage and demand ────────────────────────────────────────────────────────

def test_overage_is_set_validated_and_offered(app, org, portal):
    base = _remaining("sun", "0800")
    for bad in ("-1", "201", "lots"):
        assert org.post("/api/admin/portal/settings", data={"overage_pct": bad}).status_code == 400
    r = org.post("/api/admin/portal/settings", data={"overage_pct": "50"})
    assert r.status_code == 200 and r.json()["overage_pct"] == 50
    assert org.get("/api/admin/portal/status").json()["overage_pct"] == 50
    assert _remaining("sun", "0800") == base + 1          # 2 slots x 1.5 = 3
    demand = {(t["day_key"], t["time_key"]): t
              for t in org.get("/api/admin/portal/demand").json()["timeslots"]}
    assert demand[("sun", "0800")]["slots"] == 2 and demand[("sun", "0800")]["offered"] == 3
    org.post("/api/admin/portal/settings", data={"overage_pct": "0"})


# ── the slot dialog's picker ──────────────────────────────────────────────────

def _avail(org, slot):
    r = org.post("/api/get_avail_vols", data={"pos_key": slot})
    assert r.status_code == 200, r.text
    return {v["key"]: v["group"] for v in r.json()}


def test_the_picker_groups_by_why_and_drops_overlaps(app, org, portal):
    _sql("""INSERT INTO volunteers (vol_id, name, status, answers, add_any) VALUES
              ('k-req',  'Req',   'active', '{}', FALSE),
              ('k-flex', 'Flex',  'active', '{"flexible": true}', FALSE),
              ('k-add',  'Extra', 'active', '{}', FALSE),
              ('k-any',  'Any',   'active', '{}', TRUE),
              ('k-none', 'Nopicks', 'active', NULL, FALSE),
              ('k-else', 'Elsewhere', 'active', '{}', FALSE),
              ('k-busy', 'Busy',  'active', '{}', FALSE);
            INSERT INTO vol_picks (vol_id, day_key, time_key, kind, rank) VALUES
              ('k-req',  'sun', '1400', 'pref', 1), ('k-req', 'sat', '0800', 'pref', 2),
              ('k-flex', 'sun', '0800', 'pref', 1), ('k-flex', 'sat', '1500', 'pref', 2),
              ('k-add',  'sat', '0800', 'pref', 1), ('k-add', 'sat', '1500', 'pref', 2),
              ('k-add',  'sun', '1400', 'add', NULL),
              ('k-any',  'sat', '0800', 'pref', 1), ('k-any', 'sat', '1500', 'pref', 2),
              ('k-else', 'sat', '0800', 'pref', 1), ('k-else', 'sat', '1500', 'pref', 2),
              ('k-busy', 'sun', '1400', 'pref', 1), ('k-busy', 'sat', '0800', 'pref', 2);
            UPDATE schedule SET vol_id = 'k-busy'
              WHERE day_key = 'sun' AND time_key = '1130' AND pos_key = 'sec2';""")
    got = _avail(org, "sun_1400_sec1")
    assert got["k-req"] == "requested"
    assert got["k-flex"] == "flexible"          # flexible, with a pick on Sunday
    assert got["k-add"] == "available"          # listed sun 1400 as an extra
    assert got["k-any"] == "available"          # "any time"
    assert got["k-none"] == "no_picks"
    assert "k-else" not in got                  # picks, but nothing that fits
    assert "k-busy" not in got                  # at sun 1130, which overlaps 1400-1800 by 90 min
    order = [v["group"] for v in org.post("/api/get_avail_vols",
                                          data={"pos_key": "sun_1400_sec1"}).json()]
    assert order == sorted(order, key=["requested", "flexible", "available", "no_picks"].index)
    _sql("UPDATE schedule SET vol_id = NULL WHERE vol_id = 'k-busy'; "
         "DELETE FROM volunteers WHERE vol_id LIKE 'k-%%'")


def test_overlap_follows_the_time_ranges():
    from routers.schedule import times_overlap
    assert times_overlap("0800", "0800-1200", "1130", "1130-1530")
    assert times_overlap("1130", "1130-1530", "1400", "1400-1800")
    assert not times_overlap("0800", "0800-1200", "1500", "1500-1900")
    assert not times_overlap("0800", "0800-1200", "1200", "1200-1600")     # touching is fine
    assert times_overlap("x", "TBD", "x", "TBD")                           # same key
    assert not times_overlap("x", "TBD", "y", "0800-1200")                 # unparseable
    from routers.schedule import time_range
    assert time_range("2200-0200") == (22 * 60, 26 * 60)                  # runs past midnight


def test_unassigned_timeslots_skip_overlaps_unless_back_to_back(app, org):
    _sql("""INSERT INTO volunteers (vol_id, name) VALUES ('b2b', 'B2B');
            UPDATE schedule SET vol_id = 'b2b'
              WHERE day_key = 'sun' AND time_key = '1130' AND pos_key = 'sec1';""")
    def times(b2b):
        rows = org.post("/api/get_unassigned_timeslots",
                        data={"vol_id": "b2b", "allow_back_to_back": b2b}).json()
        return {k.rsplit("_", 1)[0] for k in (r["key"] for r in rows) if k.startswith("sun_")}
    assert times("false") == set()                           # 0800 and 1400 both overlap 1130
    assert times("true") == {"sun_0800", "sun_1400"}         # same start time still excluded
    _sql("DELETE FROM volunteers WHERE vol_id = 'b2b'")


# ── migration (last: rebuilds the schema) ─────────────────────────────────────

# ── early-access invites ──────────────────────────────────────────────────────

def test_an_invite_link_is_shown_once_and_only_its_hash_is_kept(app, org, portal, monkeypatch):
    import hashlib
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    r = org.post("/api/admin/portal/invites", data={"label": "Jane, room lead", "days": "10"})
    assert r.status_code == 200, r.text
    made = r.json()
    prefix = "https://vols.example.org/apply/signup?invite="
    assert made["link"].startswith(prefix)
    token = made["link"][len(prefix):]
    stored = _sql("SELECT token_hash, label FROM signup_invites WHERE invite_id = %s",
                  (made["invite_id"],))[0]
    assert stored == (hashlib.sha256(token.encode()).hexdigest(), "Jane, room lead")
    listed = {i["invite_id"]: i for i in org.get("/api/admin/portal/invites").json()}
    assert listed[made["invite_id"]]["state"] == "unused"
    assert "link" not in listed[made["invite_id"]] and token not in json.dumps(listed)


def test_invites_are_checked_and_for_organizers_only(app, org, portal, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    for data in ({"label": "", "days": "10"}, {"label": "x" * 81, "days": "10"},
                 {"label": "ok", "days": "0"}, {"label": "ok", "days": "91"}):
        assert org.post("/api/admin/portal/invites", data=data).status_code == 400
    anon = TestClient(app, base_url="https://scheduler.test")
    assert anon.post("/api/admin/portal/invites", data={"label": "x"}).status_code == 401
    assert anon.get("/api/admin/portal/invites").status_code == 401
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "")
    assert org.post("/api/admin/portal/invites", data={"label": "ok"}).status_code == 503


def test_revoke_and_the_push_carry_hashes_and_state(app, org, portal, monkeypatch):
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    inv = org.post("/api/admin/portal/invites", data={"label": "Revoke me"}).json()
    before = portal_sync.build_invites(_conn())
    assert org.post("/api/admin/portal/invites/revoke",
                    data={"invite_id": inv["invite_id"]}).status_code == 200
    assert org.post("/api/admin/portal/invites/revoke",           # twice: nothing to revoke
                    data={"invite_id": inv["invite_id"]}).status_code == 404
    after = portal_sync.build_invites(_conn())
    assert after["content_hash"] != before["content_hash"]
    sent = next(i for i in after["invites"] if i["invite_id"] == inv["invite_id"])
    assert sent["revoked_at"].endswith("Z") and sent["expires_at"].endswith("Z")
    assert set(sent) == {"invite_id", "token_hash", "expires_at", "revoked_at"}
    listed = {i["invite_id"]: i for i in org.get("/api/admin/portal/invites").json()}
    assert listed[inv["invite_id"]]["state"] == "revoked"
    # Pushed when the portal's hash differs, and only to a portal that reports one.
    asyncio.run(portal_sync.push_signup_invites(_conn()))
    assert portal.posted("/api/sync/signup_invites")[-1]["invites"] == after["invites"]


def test_a_pulled_signup_marks_its_invite_used(app, org, portal, monkeypatch):
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    inv = org.post("/api/admin/portal/invites", data={"label": "Early bird"}).json()
    _pull(portal, _signup("p-inv00001", "hmac-inv-1", name="Early Bird",
                          invite_id=inv["invite_id"]))
    listed = {i["invite_id"]: i for i in org.get("/api/admin/portal/invites").json()}
    assert listed[inv["invite_id"]]["state"] == "used"
    assert listed[inv["invite_id"]]["used_by"] == "Early Bird"
    # A used invite can't be revoked.
    assert org.post("/api/admin/portal/invites/revoke",
                    data={"invite_id": inv["invite_id"]}).status_code == 404


# ── Send invites (email from the admin page) ──────────────────────────────────

@pytest.fixture
def paced(monkeypatch):
    """Each test starts with the 10-second gap already elapsed."""
    from routers import portal_admin
    monkeypatch.setattr(portal_admin._invite_send, "last", -1e9)
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    return portal_admin


def test_send_makes_an_invite_pushes_it_then_mails_it(app, org, portal, paced, monkeypatch):
    lines = []
    monkeypatch.setattr(paced, "audit",
                        lambda event, req, sid, action, who=None, **data:
                        lines.append(f"{action} {who} {data}"))
    r = org.post("/api/admin/portal/invites/send",
                 data={"name": "Grace Hopper", "email": "Grace@Example.org", "days": "7"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {"invite_id", "label", "expires_at"}         # never the link
    paths = [p for m, p, _ in portal.calls if m == "POST"]
    assert paths.index("/api/sync/signup_invites") < paths.index("/api/sync/send_admin_mail")
    mail = portal.posted("/api/sync/send_admin_mail")[-1]
    assert mail["kind"] == "signup_invite" and mail["to"] == "grace@example.org"
    assert mail["link"].startswith("https://vols.example.org/apply/signup?invite=")
    assert mail["first_name"] == "Grace" and mail["expires"]
    listed = {i["invite_id"]: i for i in org.get("/api/admin/portal/invites").json()}
    assert listed[out["invite_id"]]["emailed_at"] and listed[out["invite_id"]]["label"] == "Grace Hopper"
    # The address is nowhere in the database or the audit log.
    assert not _sql("SELECT 1 FROM signup_invites WHERE label ILIKE %s OR used_by ILIKE %s",
                    ("%@%", "%@%"))
    assert lines and not any("@" in line for line in lines)


def test_sends_are_paced_ten_seconds_apart(app, org, portal, paced):
    ok = org.post("/api/admin/portal/invites/send", data={"name": "One", "email": "one@example.org"})
    assert ok.status_code == 200
    r = org.post("/api/admin/portal/invites/send", data={"name": "Two", "email": "two@example.org"})
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 10
    assert not _sql("SELECT 1 FROM signup_invites WHERE label = 'Two'")   # nothing made


def test_a_failed_mail_leaves_no_invite_behind(app, org, portal, paced, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("POST /api/sync/send_admin_mail -> 502: Mail failed")
    monkeypatch.setattr(portal_sync, "send_admin_mail", boom)
    r = org.post("/api/admin/portal/invites/send", data={"name": "Lost", "email": "lost@example.org"})
    assert r.status_code == 502 and "lost@" not in r.text
    assert not _sql("SELECT 1 FROM signup_invites WHERE label = 'Lost'")


def test_the_portals_hourly_limit_comes_back_as_a_429(app, org, portal, paced, monkeypatch):
    async def limited(*a, **k):
        raise RuntimeError("POST /api/sync/send_admin_mail -> 429: Too much invite mail")
    monkeypatch.setattr(portal_sync, "send_admin_mail", limited)
    r = org.post("/api/admin/portal/invites/send", data={"name": "Later", "email": "l@example.org"})
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    assert not _sql("SELECT 1 FROM signup_invites WHERE label = 'Later'")


def test_send_checks_its_input_and_is_for_organizers_only(app, org, portal, paced):
    from fastapi.testclient import TestClient
    for data in ({"name": "No Mail", "email": ""}, {"name": "Bad", "email": "not-an-address"}):
        assert org.post("/api/admin/portal/invites/send", data=data).status_code == 400
    anon = TestClient(app, base_url="https://scheduler.test")
    assert anon.post("/api/admin/portal/invites/send",
                     data={"name": "x", "email": "x@example.org"}).status_code == 401


# ── purge (test volunteers, gone for good) ────────────────────────────────────

@pytest.fixture
def linked(portal, monkeypatch):
    """The portal fake, with sync configured so a purge calls it."""
    monkeypatch.setattr(portal_sync, "configured", lambda: True)
    return portal


def _inactive(org, portal, vol_id, h, **kw):
    _approved(org, portal, vol_id, h, **kw)
    org.post("/api/admin/volunteers/set_inactive", data={"vol_id": vol_id, "inactive": "1"})
    asyncio.run(portal_sync.push_dirty_volunteers(_conn()))


def _purge(org, *vol_ids, confirm=None):
    if confirm is None:
        confirm = vol_ids[0] if len(vol_ids) == 1 else str(len(vol_ids))
    return org.post("/api/admin/volunteers/purge",
                    data={"vol_ids": list(vol_ids), "confirm": confirm})


def test_a_purge_removes_the_volunteer_here_and_on_the_portal(app, org, linked):
    _inactive(org, linked, "p-pur00001", "hp1", name="Test T.")
    org.post("/api/assign_vol", data={"pos_key": "sat_0800_sec1", "vol_id": "p-pur00001"})
    _sql("INSERT INTO vol_notes (vol_id, note, username) VALUES "
         "('p-pur00001', 'called them', 'org'), ('p-pur00001', '[CheckIn] | sat_0800_sec1', 'org')")
    _sql("INSERT INTO shift_flags (vol_id, day_key, time_key) VALUES ('p-pur00001', 'sat', '1500')")
    _sql("INSERT INTO radios (barcode) VALUES ('R-PUR1')")
    _sql("INSERT INTO radio_checkouts (barcode, vol_id, person_name, checked_out_by) "
         "VALUES ('R-PUR1', 'p-pur00001', 'Test T.', 'org')")

    r = _purge(org, "p-pur00001")
    assert r.status_code == 200 and r.json()["purged"] == ["p-pur00001"], r.text
    assert linked.posted("/api/sync/purge_volunteers")[-1]["vol_ids"] == ["p-pur00001"]
    for stmt in ("SELECT 1 FROM volunteers WHERE vol_id = 'p-pur00001'",
                 "SELECT 1 FROM vol_picks WHERE vol_id = 'p-pur00001'",
                 "SELECT 1 FROM vol_notes WHERE vol_id = 'p-pur00001'",
                 "SELECT 1 FROM shift_flags WHERE vol_id = 'p-pur00001'",
                 "SELECT 1 FROM schedule WHERE vol_id = 'p-pur00001'"):
        assert _sql(stmt) == [], stmt
    assert _sql("SELECT vol_id FROM schedule WHERE day_key = 'sat' AND time_key = '0800' "
                "AND pos_key = 'sec1'") == [(None,)]
    assert _sql("SELECT vol_id, person_name FROM radio_checkouts WHERE barcode = 'R-PUR1'") \
        == [(None, "Test T.")]

    # The next sync doesn't bring them back: the portal no longer has them.
    assert _pull(linked) == 0
    assert _sql("SELECT 1 FROM volunteers WHERE vol_id = 'p-pur00001'") == []


def test_a_purge_sends_a_pending_rekeys_old_id_too(app, org, linked):
    _pull(linked, _signup("p-pur00002", "hp2"))
    org.post("/api/admin/portal/approve", data={"vol_id": "p-pur00002", "new_vol_id": "4401"})
    org.post("/api/admin/volunteers/set_inactive", data={"vol_id": "4401", "inactive": "1"})
    assert _purge(org, "4401").status_code == 200
    assert linked.posted("/api/sync/purge_volunteers")[-1]["vol_ids"] == ["4401", "p-pur00002"]


def test_several_at_once_confirm_with_the_count(app, org, linked):
    _inactive(org, linked, "p-pur00003", "hp3")
    _inactive(org, linked, "p-pur00004", "hp4")
    assert _purge(org, "p-pur00003", "p-pur00004", confirm="p-pur00003").status_code == 400
    r = _purge(org, "p-pur00003", "p-pur00004")
    assert r.status_code == 200 and r.json()["purged"] == ["p-pur00003", "p-pur00004"]


def test_if_the_portal_fails_nothing_is_deleted(app, org, linked, monkeypatch):
    _inactive(org, linked, "p-pur00005", "hp5")

    async def down(*a, **k):
        raise RuntimeError("POST /api/sync/purge_volunteers -> 503: down")
    monkeypatch.setattr(portal_sync, "_request", down)
    r = _purge(org, "p-pur00005")
    assert r.status_code == 502 and "nothing was deleted" in r.json()["detail"]
    assert _sql("SELECT 1 FROM volunteers WHERE vol_id = 'p-pur00005'") == [(1,)]


def test_purge_refusals(app, org, linked):
    _approved(org, linked, "p-pur00006", "hp6")
    r = _purge(org, "p-pur00006")
    assert r.status_code == 409 and "inactive first" in r.json()["detail"]
    _sql("UPDATE volunteers SET inactive_at = NOW() WHERE vol_id = 'p-pur00006'")
    assert _purge(org, "p-pur00006", confirm="wrong").status_code == 400
    assert _purge(org, "nope-1").status_code == 404
    assert linked.posted("/api/sync/purge_volunteers") == []
    assert _sql("SELECT 1 FROM volunteers WHERE vol_id = 'p-pur00006'") == [(1,)]


def test_purge_without_a_portal_deletes_here_only(app, org, portal, monkeypatch):
    monkeypatch.setattr(portal_sync, "configured", lambda: False)
    _sql("INSERT INTO volunteers (vol_id, name, inactive_at) VALUES ('5501', 'Hand A.', NOW())")
    assert _purge(org, "5501").json()["purged"] == ["5501"]
    assert portal.posted("/api/sync/purge_volunteers") == []


def test_purge_is_for_organizers_only(app):
    from fastapi.testclient import TestClient
    c = TestClient(app, base_url="https://scheduler.test")
    assert c.post("/api/admin/volunteers/purge",
                  data={"vol_ids": ["x"], "confirm": "x"}).status_code == 401


# ── the portal's security events ──────────────────────────────────────────────

def _sec_event(i, **over):
    e = {"id": i, "ts": "2026-11-23T10:00:00.000Z", "event": "security.attack_signature",
         "severity": "high", "ip": "203.0.113.9", "method": "POST",
         "path": "/api/portal/signup/check_email", "message": "Attack patterns in a request",
         "data": {"rules": ["sql_union"], "where": "form:email", "held_back": 0}}
    e.update(over)
    return e


@pytest.fixture
def audit_on(tmp_path):
    """The audit log on (file and table) with the alert rules watching."""
    import alerts
    import audit
    _sql("DELETE FROM audit_events")
    audit.configure(str(tmp_path / "audit.jsonl"), to_db=True)
    audit.add_observer(alerts.observe)
    yield alerts
    audit.remove_observer(alerts.observe)
    audit.configure(None)


def test_portal_security_events_become_audit_events_and_alerts(app, portal, audit_on):
    portal.security_events = [_sec_event(1), _sec_event(2, ip="198.51.100.4"),
                              _sec_event(3, event="security.sync_auth_fail",
                                         severity="critical", path="/api/sync/heartbeat")]
    assert asyncio.run(portal_sync.pull_security_events(_conn())) == 3
    rows = _sql("SELECT source, event, severity, ip FROM audit_events "
                "WHERE event LIKE %s ORDER BY id", ("security.%",))
    assert rows == [("portal", "security.attack_signature", "high", "203.0.113.9"),
                    ("portal", "security.attack_signature", "high", "198.51.100.4"),
                    ("portal", "security.sync_auth_fail", "critical", "203.0.113.9")]
    assert sorted(a.rule_id for a in audit_on._state.unsent) == [
        "security.attack", "security.attack", "security.sync_auth"]
    # The cursor moved: the next pull asks for what's after 3, and takes nothing again.
    portal.calls.clear()
    assert asyncio.run(portal_sync.pull_security_events(_conn())) == 0
    assert any("since=3" in p for _, p, _ in portal.calls)


def test_a_malformed_portal_event_is_dropped(app, portal, audit_on):
    portal.security_events = [_sec_event(1, ts="yesterday"), _sec_event(2, data="x"),
                              _sec_event(3, event=None), _sec_event(4, severity="apocalyptic")]
    assert asyncio.run(portal_sync.pull_security_events(_conn())) == 1
    (row,) = _sql("SELECT severity FROM audit_events WHERE source = 'portal'")
    assert row == ("high",)          # an unknown severity falls back to the registry's


def test_a_portal_without_security_events_is_never_asked(app, portal):
    status = asyncio.run(portal_sync._sync_once(_conn()))     # heartbeat has no rev
    assert status["ok"], status
    assert not any("security_events" in p for _, p, _ in portal.calls)
