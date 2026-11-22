"""Passkey registration and sign-in against a REAL Postgres, with a software passkey.

The software passkey (passkey_test_device.py) makes genuinely signed responses, so this
exercises py_webauthn's verification, the challenge bookkeeping, and the session the sign-in
creates, end to end. The browser half is not covered.

Needs a throwaway Postgres; skipped unless TEST_PG_HOST is set. Everything in the target
database's public schema is DROPPED.

    TEST_PG_HOST=127.0.0.1 TEST_PG_PORT=55432 TEST_PG_DB=scheduler_test \\
    TEST_PG_USER=scheduler TEST_PG_PASSWORD=test  pytest backend/test_passkeys.py -v
"""
import os
import sys

import pytest

if not os.getenv("TEST_PG_HOST"):
    pytest.skip("needs a throwaway Postgres: set TEST_PG_HOST (see module docstring)",
                allow_module_level=True)
pytest.importorskip("webauthn")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import db as dbm  # noqa: E402
from passkey_test_device import SoftPasskey, b64url  # noqa: E402

SITE = "https://scheduler.test"


@pytest.fixture(scope="module")
def app():
    # db captured its DSN at import, possibly before this module ran; point it here.
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
    from routers import auth, passkeys, users
    application = FastAPI()
    for r in (auth, passkeys, users):
        application.include_router(r.router)
    application.add_middleware(SessionMiddleware, secret_key="test-secret")

    from argon2 import PasswordHasher
    ph = PasswordHasher()
    conn = dbm.open_db()
    for uid, name, pw, role in (("u-alice", "alice", "alice-password", "organizer"),
                                ("u-bob", "bob", "bob-password", "checkin")):
        conn.execute("INSERT INTO users (user_id, username, password_hash, role) VALUES (?, ?, ?, ?);",
                     (uid, name, ph.hash(pw), role))
    conn.commit()
    yield application
    dbm.close_pool()


def _raw_conn():
    """A plain-tuple connection, unlike the app's RealDictCursor ones."""
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


def _signed_in(app, user="alice"):
    c = _client(app)
    r = c.post("/api/login", data={"username": user, "password": f"{user}-password"})
    assert r.status_code == 200, r.text
    return c


def _register(app, user="alice", alg=-7, synced=False, name="Laptop", aaguid=None):
    c = _signed_in(app, user)
    dev = SoftPasskey(alg=alg, synced=synced, aaguid=aaguid)
    opts = c.post("/api/passkeys/register/options", data={"password": f"{user}-password"})
    assert opts.status_code == 200, opts.text
    r = c.post("/api/passkeys/register/verify",
               data={"credential": dev.register(opts.json(), SITE), "name": name})
    assert r.status_code == 200, r.text
    return c, dev, r.json()


def _login(app, dev, origin=SITE, **kw):
    c = _client(app)
    opts = c.post("/api/passkeys/login/options")
    assert opts.status_code == 200, opts.text
    return c, c.post("/api/passkeys/login/verify",
                     data={"credential": dev.assert_(opts.json(), origin, **kw)})


# ── configuration ─────────────────────────────────────────────────────────────

def test_config_reports_enabled(app):
    assert _client(app).get("/api/passkeys/config").json() == {"enabled": True}


def test_unconfigured_or_plain_http_is_disabled(app, monkeypatch):
    c = _client(app)
    for url in ("", "http://scheduler.test"):
        monkeypatch.setenv("PORTAL_PUBLIC_URL", url)
        assert c.get("/api/passkeys/config").json() == {"enabled": False}
        assert c.post("/api/passkeys/login/options").status_code == 503
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "http://localhost:8080")  # localhost is exempt
    assert c.get("/api/passkeys/config").json() == {"enabled": True}


def test_a_separate_admin_host_is_the_relying_party(app, monkeypatch):
    from routers.passkeys import _site
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://vols.example.org")
    monkeypatch.setenv("ADMIN_PUBLIC_URL", "https://admin.example.org")
    assert _site() == ("admin.example.org", "https://admin.example.org")
    # The portal host's plain http doesn't matter once the admin host is set.
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "")
    assert _client(app).get("/api/passkeys/config").json() == {"enabled": True}


# ── registration ──────────────────────────────────────────────────────────────

def test_registration_needs_a_session_and_the_password(app):
    assert _client(app).post("/api/passkeys/register/options",
                             data={"password": "alice-password"}).status_code == 401
    c = _signed_in(app)
    r = c.post("/api/passkeys/register/options", data={"password": "wrong"})
    assert r.status_code == 403 and r.json()["detail"] == "Password is incorrect"


def test_registration_options_prefer_ml_dsa_and_require_a_verified_discoverable_key(app):
    c = _signed_in(app)
    opts = c.post("/api/passkeys/register/options", data={"password": "alice-password"}).json()
    assert [p["alg"] for p in opts["pubKeyCredParams"]] == [-49, -48, -50, -7, -8, -257]
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["rp"]["id"] == "scheduler.test"
    handle = opts["user"]["id"]
    again = c.post("/api/passkeys/register/options", data={"password": "alice-password"}).json()
    assert again["user"]["id"] == handle  # one stable handle per account
    assert "alice" not in handle and "u-alice" not in handle


def test_any_role_can_register_es256_and_sign_in(app):
    _, dev, out = _register(app, user="bob", name="Phone")
    assert out["alg"] == "ES256" and out["post_quantum"] is False and out["name"] == "Phone"
    c, r = _login(app, dev)
    assert r.status_code == 200 and r.json() == {"username": "bob", "role": "checkin"}
    assert [p["name"] for p in c.get("/api/passkeys").json()] == ["Phone"]  # session works


@pytest.mark.parametrize("alg, label", [(-49, "ML-DSA-65"), (-48, "ML-DSA-44"),
                                        (-50, "ML-DSA-87")])
def test_ml_dsa_passkeys_register_and_sign_in(app, alg, label):
    _, dev, out = _register(app, alg=alg, name=label)
    assert out["alg"] == label and out["post_quantum"] is True
    _, r = _login(app, dev)
    assert r.status_code == 200 and r.json()["username"] == "alice"


def test_the_same_passkey_cannot_register_twice(app):
    c, dev, _ = _register(app)
    opts = c.post("/api/passkeys/register/options", data={"password": "alice-password"}).json()
    assert b64url(dev.credential_id) in [e["id"] for e in opts["excludeCredentials"]]
    r = c.post("/api/passkeys/register/verify", data={"credential": dev.register(opts, SITE)})
    assert r.status_code == 409


def test_a_registration_challenge_belongs_to_the_account_that_asked(app):
    alice = _signed_in(app, "alice")
    bob = _signed_in(app, "bob")
    opts = alice.post("/api/passkeys/register/options", data={"password": "alice-password"}).json()
    r = bob.post("/api/passkeys/register/verify",
                 data={"credential": SoftPasskey().register(opts, SITE)})
    assert r.status_code == 400 and "expired or was already used" in r.json()["detail"]


# ── sign-in ───────────────────────────────────────────────────────────────────

def test_a_sign_in_response_works_once(app):
    _, dev, _ = _register(app)
    c = _client(app)
    opts = c.post("/api/passkeys/login/options").json()
    resp = dev.assert_(opts, SITE)
    assert c.post("/api/passkeys/login/verify", data={"credential": resp}).status_code == 200
    replay = _client(app).post("/api/passkeys/login/verify", data={"credential": resp})
    assert replay.status_code == 400


def test_an_expired_challenge_is_refused(app):
    _, dev, _ = _register(app)
    c = _client(app)
    opts = c.post("/api/passkeys/login/options").json()
    _sql("UPDATE webauthn_challenges SET expires_at = NOW() AT TIME ZONE 'UTC' - INTERVAL '1 second' "
         "WHERE used_at IS NULL")
    r = c.post("/api/passkeys/login/verify", data={"credential": dev.assert_(opts, SITE)})
    assert r.status_code == 400


@pytest.mark.parametrize("kw", [
    {"origin": "https://scheduler.test:8443"},   # the portal's direct port
    {"origin": "https://evil.example"},
    {"rp_id": "evil.example"},
    {"flags": 0x01},                              # user present, not verified
])
def test_wrong_origin_rp_or_missing_verification_is_refused(app, kw):
    _, dev, _ = _register(app)
    c, r = _login(app, dev, **kw)
    assert r.status_code == 401
    assert c.get("/api/passkeys").status_code == 401  # no session was created


def test_a_sign_count_going_backwards_is_refused(app):
    _, dev, _ = _register(app)
    assert _login(app, dev)[1].status_code == 200
    dev.sign_count -= 2   # a cloned authenticator replaying an older count
    assert _login(app, dev)[1].status_code == 401


def test_synced_passkeys_that_always_report_zero_keep_working(app):
    _, dev, out = _register(app, synced=True)
    assert out["backed_up"] is True
    for _ in range(3):
        assert _login(app, dev)[1].status_code == 200


def test_an_unknown_passkey_says_to_use_the_password(app):
    dev = SoftPasskey()
    dev.user_handle = b"x" * 32
    _, r = _login(app, dev)
    assert r.status_code == 401 and "Sign in with your password" in r.json()["detail"]


def test_a_mismatched_user_handle_is_refused(app):
    _, dev, _ = _register(app)
    _, r = _login(app, dev, user_handle=b"y" * 32)
    assert r.status_code == 401


# ── management ────────────────────────────────────────────────────────────────

def test_delete_only_your_own_passkey_and_it_stops_working(app):
    alice, dev, out = _register(app)
    bob = _signed_in(app, "bob")
    assert bob.post("/api/passkeys/delete", data={"credential_id": out["id"]}).status_code == 404
    assert alice.post("/api/passkeys/delete", data={"credential_id": out["id"]}).status_code == 204
    _, r = _login(app, dev)
    assert r.status_code == 401 and "not recognized" in r.json()["detail"]


def test_organizers_see_counts_and_can_clear_another_users_passkeys(app):
    _register(app, user="bob")
    bob = _signed_in(app, "bob")
    alice = _signed_in(app, "alice")
    assert bob.post("/api/passkeys/clear_user", data={"user_id": "u-alice"}).status_code == 403
    counts = {u["username"]: u["passkey_count"] for u in alice.get("/api/users").json()}
    assert counts["bob"] >= 1
    r = alice.post("/api/passkeys/clear_user", data={"user_id": "u-bob"})
    assert r.status_code == 200 and r.json()["removed"] == counts["bob"]
    assert bob.get("/api/passkeys").json() == []


BITWARDEN = "d548826e-79b4-db40-a3d8-11116f7e8349"
UNLISTED = "12345678-1234-4321-8765-123456789abc"


def test_the_passkey_manager_is_recorded_and_named(app):
    _, _, out = _register(app, aaguid=BITWARDEN.upper(), name="")
    assert out["provider"] == "Bitwarden"
    assert out["name"] == "Bitwarden"  # blank name defaults to the manager
    stored = _sql("SELECT aaguid FROM user_passkeys WHERE name = 'Bitwarden'")
    assert stored == [(BITWARDEN,)]  # canonical lowercase


def test_an_all_zero_model_id_is_stored_as_unknown(app):
    _, dev, out = _register(app, name="   ")
    assert out["provider"] is None and out["name"] == "Passkey"
    assert _sql("SELECT aaguid FROM user_passkeys WHERE credential_id = %s",
                (dev.credential_id,)) == [(None,)]


def test_an_unlisted_model_id_is_kept_for_a_later_list_refresh(app):
    _, dev, out = _register(app, aaguid=UNLISTED, name="Mystery key")
    assert out["provider"] is None and out["name"] == "Mystery key"
    assert _sql("SELECT aaguid FROM user_passkeys WHERE credential_id = %s",
                (dev.credential_id,)) == [(UNLISTED,)]


def test_names_are_one_line_and_capped(app):
    _, _, out = _register(app, name="  My\n  phone\t" + "x" * 100)
    assert out["name"].startswith("My phone ") and len(out["name"]) == 60


def test_rename_your_own_passkey_only(app):
    alice, dev, out = _register(app, aaguid=BITWARDEN)
    bob = _signed_in(app, "bob")
    r = bob.post("/api/passkeys/rename", data={"credential_id": out["id"], "name": "Mine now"})
    assert r.status_code == 404
    r = alice.post("/api/passkeys/rename", data={"credential_id": out["id"], "name": "  "})
    assert r.status_code == 400
    # base64url decoding drops invalid characters, so garbage simply matches nothing.
    r = alice.post("/api/passkeys/rename", data={"credential_id": "!!", "name": "x"})
    assert r.status_code == 404
    r = alice.post("/api/passkeys/rename",
                   data={"credential_id": out["id"], "name": "  Android   phone "})
    assert r.status_code == 200
    assert r.json()["name"] == "Android phone" and r.json()["provider"] == "Bitwarden"
    names = {p["id"]: p["name"] for p in alice.get("/api/passkeys").json()}
    assert names[out["id"]] == "Android phone"
    assert _login(app, dev)[1].status_code == 200  # renaming changes nothing else


def test_add_user_returns_the_new_user(app):
    """POST /api/users/add used to hand a raw datetime to JSONResponse."""
    alice = _signed_in(app)
    r = alice.post("/api/users/add", data={"username": "carol", "password": "carol-password",
                                           "role": "view"})
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "carol" and r.json()["passkey_count"] == 0


def test_deleting_a_user_removes_their_passkeys(app):
    _register(app, user="bob")
    _sql("DELETE FROM users WHERE user_id = 'u-bob'")
    assert _sql("SELECT COUNT(*) FROM user_passkeys WHERE user_id = 'u-bob'")[0][0] == 0


# ── changing your own password (Profile) ─────────────────────────────────────

def test_change_own_password(app):
    from argon2 import PasswordHasher
    _sql("INSERT INTO users (user_id, username, password_hash, role) VALUES (%s, %s, %s, %s)",
         ("u-dave", "dave", PasswordHasher().hash("dave-password"), "view"))
    assert _client(app).post("/api/users/change_password", data={
        "current_password": "dave-password", "new_password": "longer-one"}).status_code == 401

    c = _signed_in(app, "dave")                     # any role, view included
    r = c.post("/api/users/change_password",
               data={"current_password": "wrong", "new_password": "new-password-1"})
    assert r.status_code == 403 and r.json()["detail"] == "Current password is incorrect"
    r = c.post("/api/users/change_password",
               data={"current_password": "dave-password", "new_password": "short"})
    assert r.status_code == 400
    version = _sql("SELECT password_version FROM users WHERE user_id = 'u-dave'")[0][0]

    r = c.post("/api/users/change_password",
               data={"current_password": "dave-password", "new_password": "new-password-1"})
    assert r.status_code == 200
    assert _sql("SELECT password_version FROM users WHERE user_id = 'u-dave'")[0][0] == version + 1
    fresh = _client(app)
    assert fresh.post("/api/login", data={"username": "dave",
                                          "password": "dave-password"}).status_code == 401
    assert fresh.post("/api/login", data={"username": "dave",
                                          "password": "new-password-1"}).status_code == 200


# ── migrations (last: they rebuild the schema) ─────────────────────────────────────

def _migrate_twice():
    cwd = os.getcwd()
    os.chdir(_HERE)
    try:
        dbm.open_db()
        dbm.open_db()  # and again: the migrations are idempotent and recorded
    finally:
        os.chdir(cwd)


def _has_column(table, column):
    return bool(_sql("SELECT 1 FROM information_schema.columns "
                     "WHERE table_name = %s AND column_name = %s", (table, column)))

