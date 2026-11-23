"""Admin invites, password reset by email, and users.email, against a REAL Postgres.

The portal's side of the mail is faked (portal_sync.send_admin_mail): these check what the
scheduler stores, what it asks the portal to send, and what a link can and can't do. The
portal's half is portal/test_admin_mail.py.

Needs a throwaway Postgres; skipped unless TEST_PG_HOST is set. Everything in the target
database's public schema is DROPPED.

    TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 TEST_PG_DB=scheduler_test \\
    TEST_PG_USER=scheduler TEST_PG_PASSWORD=test  pytest backend/test_invites.py -v
"""
import json
import os
import sys
from urllib.parse import parse_qs, urlparse

import pytest

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST (see module docstring)",
                allow_module_level=True)

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import audit  # noqa: E402
import db as dbm  # noqa: E402
import portal_sync  # noqa: E402

SITE = "https://scheduler.test"


@pytest.fixture(scope="module")
def app():
    dbm.DB_HOST = os.environ["TEST_PG_HOST"]
    dbm.DB_PORT = int(os.getenv("TEST_PG_PORT", "5432"))
    dbm.DB_NAME = os.getenv("TEST_PG_DB", "scheduler_test")
    dbm.DB_USER = os.getenv("TEST_PG_USER", "scheduler")
    dbm.DB_PASSWORD = os.getenv("TEST_PG_PASSWORD", "")
    dbm.close_pool()
    os.environ["PORTAL_PUBLIC_URL"] = SITE
    os.environ.pop("ADMIN_PUBLIC_URL", None)  # one origin; the split has its own tests

    _reset_schema()
    cwd = os.getcwd()
    os.chdir(_HERE)  # migrate() opens ./scheduler.sql
    try:
        dbm.open_db()
    finally:
        os.chdir(cwd)

    from fastapi import FastAPI
    from starlette.middleware.sessions import SessionMiddleware
    from routers import auth, invites, password_reset, users
    application = FastAPI()
    for r in (auth, invites, password_reset, users):
        application.include_router(r.router)
    application.add_middleware(SessionMiddleware, secret_key="test-secret")

    from argon2 import PasswordHasher
    ph = PasswordHasher()
    conn = dbm.open_db()
    for uid, name, pw, role, email in (
            ("u-alice", "alice", "alice-password", "organizer", "alice@example.com"),
            ("u-bob", "bob", "bob-password", "checkin", None)):
        conn.execute("INSERT INTO users (user_id, username, password_hash, role, email) "
                     "VALUES (?, ?, ?, ?, ?);", (uid, name, ph.hash(pw), role, email))
    conn.commit()
    yield application
    dbm.close_pool()


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Every send_admin_mail call, instead of the portal. Set .fail to make it raise."""
    class Calls(list):
        pass
    calls = Calls()

    async def fake(kind, to, link, **fields):
        if getattr(fake, "fail", None):
            raise RuntimeError(fake.fail)
        calls.append({"kind": kind, "to": to, "link": link, **fields})
        return {"ok": True}

    monkeypatch.setattr(portal_sync, "send_admin_mail", fake)
    calls.fake = fake
    from routers import password_reset
    password_reset._requests.clear()
    yield calls


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


def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app, base_url=SITE)


def _signed_in(app, user="alice", password=None):
    c = _client(app)
    r = c.post("/api/login", data={"username": user, "password": password or f"{user}-password"})
    assert r.status_code == 200, r.text
    return c


def _token(link):
    return parse_qs(urlparse(link).query)["t"][0]


def _invite(app, role="checkin", email=""):
    r = _signed_in(app).post("/api/invites", data={"role": role, "email": email})
    assert r.status_code == 200, r.text
    return r.json()


# ── invites ───────────────────────────────────────────────────────────────────

def test_an_invite_is_mailed_and_accepted(app, sent):
    out = _invite(app, email="New.Person@Example.com")
    assert out["emailed"] is True and out["link"].startswith(f"{SITE}/admin/invite?t=")
    (mail,) = sent
    assert mail == {"kind": "invite", "to": "new.person@example.com", "link": out["link"],
                    "role": "checkin", "inviter": "alice", "app_title": "the conference",
                    "hours": 72}
    t = _token(out["link"])
    # Only the hash is stored.
    assert _sql("SELECT count(*) FROM user_invites WHERE token_hash = %s", (t,))[0][0] == 0

    c = _client(app)
    chk = c.get("/api/invites/check", params={"t": t}).json()
    assert chk["valid"] and chk["role"] == "checkin" and chk["email"] == "new.person@example.com"
    r = c.post("/api/invites/accept", data={"t": t, "username": "Carol", "password": "carol-pass",
                                            "email": "new.person@example.com"})
    assert r.status_code == 200 and r.json() == {"username": "carol", "role": "checkin"}
    assert c.get("/api/users/me").json() == {"username": "carol", "role": "checkin",
                                            "email": "new.person@example.com"}
    # Spent, and the invite row no longer holds the address.
    assert c.get("/api/invites/check", params={"t": t}).json() == {"valid": False}
    assert _sql("SELECT email, used_by FROM user_invites WHERE invite_id = %s",
                (out["invite_id"],)) == [(None, "carol")]
    r = _client(app).post("/api/invites/accept",
                          data={"t": t, "username": "carol2", "password": "carol-pass"})
    assert r.status_code == 400 and "expired, was already used" in r.json()["detail"]


def test_without_an_address_nothing_is_mailed(app, sent):
    out = _invite(app)
    assert out["emailed"] is False and "email_error" not in out and sent == []


def test_a_failed_mail_still_leaves_a_usable_invite(app, sent):
    sent.fake.fail = "portal unreachable"
    out = _invite(app, email="x@example.com")
    assert out["emailed"] is False and "couldn't be sent" in out["email_error"]
    assert "portal unreachable" not in out["email_error"]       # the detail is logged, not shown
    assert _client(app).get("/api/invites/check",
                            params={"t": _token(out["link"])}).json()["valid"]


def test_only_organizers_invite(app):
    c = _signed_in(app, "bob")
    assert c.post("/api/invites", data={"role": "view"}).status_code == 403
    assert c.get("/api/invites").status_code == 403
    assert _client(app).post("/api/invites", data={"role": "view"}).status_code == 401
    assert _signed_in(app).post("/api/invites", data={"role": "root"}).status_code == 400


def test_check_is_inert(app):
    t = _token(_invite(app)["link"])
    c = _client(app)
    for _ in range(3):
        assert c.get("/api/invites/check", params={"t": t}).json()["valid"]
    assert c.get("/api/invites/check", params={"t": "nope"}).json() == {"valid": False}
    assert c.get("/api/invites/check").json() == {"valid": False}


@pytest.mark.parametrize("form, code, why", [
    ({"username": "alice"}, 409, "is taken"),
    ({"username": "x"}, 400, "Username"),
    ({"username": "bad name"}, 400, "Username"),
    ({"password": "short"}, 400, "at least 8"),
    ({"email": "nope"}, 400, "email address"),
])
def test_a_refused_accept_leaves_the_link_usable(app, form, code, why):
    t = _token(_invite(app)["link"])
    data = {"t": t, "username": "dave", "password": "dave-password", **form}
    r = _client(app).post("/api/invites/accept", data=data)
    assert r.status_code == code and why in r.json()["detail"]
    assert _client(app).get("/api/invites/check", params={"t": t}).json()["valid"]


def test_expired_and_revoked_invites_are_refused(app):
    org = _signed_in(app)
    exp, rev = _invite(app), _invite(app)
    _sql("UPDATE user_invites SET expires_at = NOW() AT TIME ZONE 'UTC' - INTERVAL '1 minute' "
         "WHERE invite_id = %s", (exp["invite_id"],))
    assert org.post("/api/invites/revoke", data={"invite_id": rev["invite_id"]}).json() == {"ok": True}
    assert org.post("/api/invites/revoke", data={"invite_id": rev["invite_id"]}).status_code == 404
    listed = [i["invite_id"] for i in org.get("/api/invites").json()]
    for inv in (exp, rev):
        assert inv["invite_id"] not in listed
        r = _client(app).post("/api/invites/accept", data={
            "t": _token(inv["link"]), "username": "erin", "password": "erin-password"})
        assert r.status_code == 400


def test_the_list_never_carries_a_link(app):
    _invite(app, email="list@example.com")
    rows = _signed_in(app).get("/api/invites").json()
    assert rows and all(set(r) == {"invite_id", "role", "email", "created_by", "created_at",
                                   "expires_at"} for r in rows)


def test_no_public_url_no_invite(app, monkeypatch):
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "")
    assert _signed_in(app).post("/api/invites", data={"role": "view"}).status_code == 503


def test_links_use_the_admin_host_when_it_is_separate(app, monkeypatch):
    monkeypatch.setenv("ADMIN_PUBLIC_URL", "https://admin.example.org/")
    link = _signed_in(app).post("/api/invites", data={"role": "view"}).json()["link"]
    assert link.startswith("https://admin.example.org/admin/invite?t=")


# ── password reset ────────────────────────────────────────────────────────────

def _request_reset(app, username, ip="10.0.0.1"):
    return _client(app).post("/api/password_reset/request", data={"username": username},
                             headers={"x-forwarded-for": ip})


def test_the_reply_is_the_same_whoever_asks(app, sent):
    from routers.password_reset import GENERIC
    replies = [_request_reset(app, u).json() for u in ("alice", "bob", "nobody")]
    assert replies == [{"ok": True, "detail": GENERIC}] * 3
    (mail,) = sent                                  # only alice has an address
    assert mail["kind"] == "reset" and mail["to"] == "alice@example.com"
    assert mail["link"].startswith(f"{SITE}/admin/reset?t=") and mail["minutes"] == 60
    sent.fake.fail = "relay down"
    assert _request_reset(app, "alice").json() == {"ok": True, "detail": GENERIC}


def test_reset_requests_are_rate_limited(app):
    for _ in range(5):
        assert _request_reset(app, "nobody", ip="10.0.0.2").status_code == 200
    assert _request_reset(app, "nobody", ip="10.0.0.3").status_code == 429   # per username
    for i in range(5):                                 # the refused one above didn't count
        assert _request_reset(app, f"other{i}", ip="10.0.0.3").status_code == 200
    assert _request_reset(app, "other9", ip="10.0.0.3").status_code == 429   # per address


def test_reset_requests_from_one_ipv6_64_share_a_limit(app):
    for i in range(5):
        assert _request_reset(app, f"six{i}", ip=f"2001:db8:5:5::{i + 1:x}").status_code == 200
    assert _request_reset(app, "six9", ip="2001:db8:5:5::ff").status_code == 429
    assert _request_reset(app, "six9", ip="2001:db8:5:6::1").status_code == 200


def test_a_reset_signs_out_everywhere_and_spends_every_link(app, sent):
    a, b = _signed_in(app), _signed_in(app)
    assert a.get("/api/users/me").status_code == 200
    _request_reset(app, "alice")
    _request_reset(app, "alice")
    first, second = (_token(m["link"]) for m in sent)

    c = _client(app)
    assert c.get("/api/password_reset/check", params={"t": first}).json() == \
        {"valid": True, "username": "alice"}
    r = c.post("/api/password_reset/complete", data={"t": first, "password": "short"})
    assert r.status_code == 400                                    # link not spent
    r = c.post("/api/password_reset/complete", data={"t": first, "password": "new-alice-pw"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/api/users/me").status_code == 401                # doesn't sign in

    for s in (a, b):                                               # sessions and cache gone
        assert s.get("/api/users/me").status_code == 401
    for t in (first, second):
        assert c.get("/api/password_reset/check", params={"t": t}).json() == {"valid": False}
        assert c.post("/api/password_reset/complete",
                      data={"t": t, "password": "another-pw"}).status_code == 400
    assert _client(app).post("/api/login", data={"username": "alice",
                                                 "password": "alice-password"}).status_code == 401
    _signed_in(app, password="new-alice-pw")
    # Put it back for the other tests.
    from argon2 import PasswordHasher
    _sql("UPDATE users SET password_hash = %s WHERE username = 'alice'",
         (PasswordHasher().hash("alice-password"),))


def test_an_expired_reset_link_is_refused(app, sent):
    _request_reset(app, "alice")
    t = _token(sent[0]["link"])
    _sql("UPDATE password_resets SET expires_at = NOW() AT TIME ZONE 'UTC' - INTERVAL '1 second'")
    c = _client(app)
    assert c.get("/api/password_reset/check", params={"t": t}).json() == {"valid": False}
    assert c.post("/api/password_reset/complete",
                  data={"t": t, "password": "new-password"}).status_code == 400


# ── users.email ───────────────────────────────────────────────────────────────

def test_changing_ones_own_email_needs_the_password(app):
    c = _signed_in(app, "bob")
    r = c.post("/api/users/change_email", data={"current_password": "wrong", "email": "b@x.org"})
    assert r.status_code == 403
    assert c.post("/api/users/change_email", data={"current_password": "bob-password",
                                                   "email": "nope"}).status_code == 400
    r = c.post("/api/users/change_email", data={"current_password": "bob-password",
                                                "email": " Bob@X.org "})
    assert r.status_code == 200 and c.get("/api/users/me").json()["email"] == "bob@x.org"
    c.post("/api/users/change_email", data={"current_password": "bob-password", "email": ""})
    assert c.get("/api/users/me").json()["email"] is None


def test_organizers_set_anyones_email(app):
    org = _signed_in(app)
    r = org.post("/api/users/set_email", data={"user_id": "u-bob", "email": "bob@example.com"})
    assert r.json() == {"ok": True, "email": "bob@example.com"}
    assert [u["email"] for u in org.get("/api/users").json() if u["username"] == "bob"] == \
        ["bob@example.com"]
    assert org.post("/api/users/set_email", data={"user_id": "u-none"}).status_code == 404
    assert _signed_in(app, "bob").post("/api/users/set_email",
                                       data={"user_id": "u-alice"}).status_code == 403
    org.post("/api/users/set_email", data={"user_id": "u-bob", "email": ""})



# ── failed sign-ins are rate limited ──────────────────────────────────────────

def _login(app, user, password, ip="10.9.0.1"):
    return _client(app).post("/api/login", data={"username": user, "password": password},
                             headers={"X-Forwarded-For": ip})


def test_five_failures_for_one_user_from_one_address_lock_that_pair(app):
    for _ in range(5):
        assert _login(app, "bob", "wrong").status_code == 401
    r = _login(app, "bob", "bob-password")                     # even the right password
    assert r.status_code == 429 and 0 < int(r.headers["Retry-After"]) <= 15 * 60
    assert _login(app, "bob", "bob-password", ip="10.9.0.2").status_code == 200   # elsewhere
    assert _login(app, "alice", "alice-password").status_code == 200              # others


def test_twenty_failures_from_one_address_lock_the_address(app):
    for i in range(20):
        assert _login(app, f"nobody{i}", "x", ip="10.9.1.1").status_code == 401
    assert _login(app, "alice", "alice-password", ip="10.9.1.1").status_code == 429
    assert _login(app, "alice", "alice-password", ip="10.9.1.2").status_code == 200


def test_an_ipv6_64_counts_as_one_address(app):
    for i in range(20):
        assert _login(app, f"v6-{i}", "x", ip=f"2001:db8:7:7::{i + 1:x}").status_code == 401
    assert _login(app, "alice", "alice-password", ip="2001:db8:7:7::ff").status_code == 429


def test_a_spread_out_guess_at_one_username_hits_the_ceiling(app):
    for i in range(30):
        assert _login(app, "bob", "wrong", ip=f"10.9.2.{i + 1}").status_code == 401
    assert _login(app, "bob", "bob-password", ip="10.9.3.1").status_code == 429


def test_success_clears_the_pair_and_refusals_do_not_count(app):
    for _ in range(4):
        _login(app, "bob", "wrong")
    assert _login(app, "bob", "bob-password").status_code == 200     # clears the 4
    for _ in range(4):
        assert _login(app, "bob", "wrong").status_code == 401         # 4 again, not 8
    assert _login(app, "bob", "wrong").status_code == 401             # the 5th
    for _ in range(10):
        assert _login(app, "bob", "wrong").status_code == 429         # refused, not counted
    assert _login(app, "bob", "bob-password", ip="10.9.0.9").status_code == 200


def test_successes_from_one_address_never_lock_it(app):
    """The venue: every check-in laptop signs in from one public address."""
    for _ in range(30):
        assert _login(app, "bob", "bob-password", ip="10.9.4.1").status_code == 200


# ── the audit log, end to end through the real routes ─────────────────────────

@pytest.fixture
def audit_file(tmp_path):
    """Turn the audit log on for one test; return a function that reads its records."""
    path = tmp_path / "audit.jsonl"
    audit.configure(str(path))
    yield lambda: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    audit.configure(None)


def test_a_hostile_username_cannot_forge_an_audit_line(app, audit_file):
    forged = "eve\n2026-01-01 00:00:00 - alice(u-alice) from 10.0.0.1: Deleted user 'bob'"
    r = _client(app).post("/api/login", data={"username": forged, "password": "x"},
                          headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401
    (rec,) = audit_file()
    assert rec["event"] == "auth.login.fail" and rec["ip"] == "203.0.113.9"
    assert rec["data"] == {"username": forged.lower(), "reason": "unknown_user"}


def test_sign_in_failure_success_and_sign_out_are_audited(app, audit_file):
    c = _client(app)
    assert c.post("/api/login", data={"username": "bob", "password": "wrong"}).status_code == 401
    ok = c.post("/api/login", data={"username": "bob", "password": "bob-password"})
    assert ok.status_code == 200
    assert c.get("/api/logout").status_code == 200
    recs = audit_file()
    assert [r["event"] for r in recs] == ["auth.login.fail", "auth.login.ok", "auth.logout"]
    assert recs[0]["data"]["reason"] == "bad_password" and recs[0]["actor"] is None
    assert recs[1]["actor"] == "bob" and recs[1]["user_id"] == "u-bob"
    assert recs[2]["actor"] == "bob"


def test_throttling_is_audited_once_however_hard_a_client_hammers(app, audit_file):
    for _ in range(5):
        _login(app, "bob", "wrong")
    for _ in range(10):
        assert _login(app, "bob", "wrong").status_code == 429
    events = [r["event"] for r in audit_file()]
    assert events == ["auth.login.fail"] * 5 + ["auth.login.throttled"]
    assert audit_file()[-1]["data"] == {"username": "bob", "limits": ["user_ip"]}


def test_a_password_reset_request_is_audited_without_the_address(app, audit_file, sent):
    _request_reset(app, "alice")
    (rec,) = audit_file()
    assert rec["event"] == "password.reset.request" and rec["data"]["username"] == "alice"
    assert "@" not in json.dumps(rec)
