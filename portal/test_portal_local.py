"""Portal tests that run with no Postgres, no SMTP, no browser, and no network.

This is the half of §16 that is verifiable in the dev container, and it is the reason SQLite
is worth something here beyond convenience: a temp-file database plus MAIL_BACKEND=file makes
the whole auth and sync surface exercisable in-process.

    pytest test_portal_local.py -v

What is NOT covered here, and cannot be: the scheduler<->portal integration against a real
scheduler, real SMTP delivery, and every browser flow.
"""
import base64
import importlib
import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HMAC_KEY = "0" * 64
SYNC_KEY_ID = "test-key"
SYNC_SECRET = "s3cret-for-tests"


# ── fixtures ──────────────────────────────────────────────────────────────────

# Popping "routers.auth" from sys.modules does NOT clear the `auth` attribute on the already
# imported `routers` package, so `from routers import auth` in main.py would re-bind the stale
# module — and the app would serve a previous test's settings. The package has to go too.
_PORTAL_MODULES = ("main", "routers.auth", "routers.sync", "routers.branding",
                   "routers.signup", "routers", "config", "db", "clock", "security", "mailer",
                   "ratelimit", "signup_fields")


def _fresh_portal():
    """Drop every portal module so the next import rebuilds them against current env vars."""
    for mod in _PORTAL_MODULES:
        sys.modules.pop(mod, None)
    return importlib.import_module("main")



@pytest.fixture(scope="session")
def rsa_keys():
    """A throwaway 2048-bit pair. Smaller than the 4096 we ship, purely for test speed."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv, pub


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A fully configured portal in a temp directory, imported fresh."""
    monkeypatch.setenv("EMAIL_HMAC_KEY", HMAC_KEY)
    monkeypatch.setenv("SYNC_KEY_ID", SYNC_KEY_ID)
    monkeypatch.setenv("SYNC_SECRET", SYNC_SECRET)
    monkeypatch.setenv("PORTAL_DB", str(tmp_path / "portal.db"))
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("MAIL_DIR", str(tmp_path / "mail"))
    monkeypatch.setenv("PORTAL_PUBLIC_URL", "https://portal.test")
    monkeypatch.setenv("PORTAL_RESPONSE_FLOOR_MS", "0")  # keep the suite fast
    monkeypatch.delenv("PORTAL_SKIP_CONFIG", raising=False)

    main = _fresh_portal()
    import clock
    clock.reset()
    from fastapi.testclient import TestClient

    with TestClient(main.app, base_url="https://portal.test") as client:
        yield client, tmp_path


@pytest.fixture
def client(app_env):
    return app_env[0]


def _conn(app_env) -> sqlite3.Connection:
    import db as dbm
    return dbm.connect(str(app_env[1] / "portal.db"))


def _add_volunteer(app_env, vol_id="1042", email="jim@example.com", status="active",
                   pubkey=None):
    import security
    from config import settings
    conn = _conn(app_env)
    enc = security.encrypt_email(pubkey, email) if pubkey else None
    conn.execute(
        "INSERT INTO volunteers (vol_id, name, status, email_hmac, email_enc, rev) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (vol_id, "Jim E.", status,
         security.email_hmac(settings.email_hmac_key, email), enc),
    )
    conn.commit()
    conn.close()


def _link_from_mail(app_env) -> str:
    """Pull the magic link out of the .eml the file backend wrote.

    Parsed with the email module rather than scanned as text: quoted-printable encodes the
    '=' in '?t=' as '=3D', so naive string matching recovers a token that starts with "3D".
    """
    import email
    import email.policy
    import re

    mail_dir = app_env[1] / "mail"
    files = sorted(mail_dir.glob("*.eml"), key=lambda p: p.stat().st_mtime)
    assert files, "no mail was written"
    msg = email.message_from_bytes(files[-1].read_bytes(), policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain",)) or msg.get_body()
    text = part.get_content()
    match = re.search(r"verify\?t=([A-Za-z0-9_\-]+)", text)
    assert match, f"no verify link found in:\n{text}"
    return match.group(1)


def _signed(client, method: str, path: str, payload=None, secret=SYNC_SECRET,
            key_id=SYNC_KEY_ID, ts=None, nonce=None):
    """Issue a correctly signed sync request."""
    import clock
    import security
    body = json.dumps(payload).encode() if payload is not None else b""
    ts = str(ts if ts is not None else int(clock.now().timestamp()))
    nonce = nonce or security.new_nonce()
    sig = security.sign_request(secret.encode(), method, path, ts, nonce, body)
    headers = {
        "x-sync-key-id": key_id,
        "x-sync-timestamp": ts,
        "x-sync-nonce": nonce,
        "x-sync-signature": sig,
    }
    if payload is not None:
        headers["content-type"] = "application/json"
    return client.request(method, path, content=body, headers=headers)


# ── config ────────────────────────────────────────────────────────────────────

def test_missing_hmac_key_is_a_hard_failure(monkeypatch):
    """A generated-on-the-fly key would orphan every stored hash. Refuse to start instead."""
    monkeypatch.delenv("EMAIL_HMAC_KEY", raising=False)
    monkeypatch.delenv("PORTAL_SKIP_CONFIG", raising=False)
    sys.modules.pop("config", None)
    # Importing the module is what enforces it, so the import itself must raise — the portal
    # cannot reach a state where it is running without the key.
    with pytest.raises(Exception) as exc:
        importlib.import_module("config")
    assert "EMAIL_HMAC_KEY" in str(exc.value)
    sys.modules.pop("config", None)


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True, "service": "portal"}


# ── schema and db ─────────────────────────────────────────────────────────────

def test_schema_version_and_pragmas(app_env):
    import db as dbm
    conn = _conn(app_env)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == dbm.PORTAL_SCHEMA_VERSION
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_migrate_is_idempotent(app_env):
    import db as dbm
    conn = _conn(app_env)
    before = dbm.migrate(conn)
    after = dbm.migrate(conn)
    assert before == after == dbm.PORTAL_SCHEMA_VERSION
    conn.close()


def test_email_hmac_unique_index_is_the_real_guarantee(app_env):
    """check_email is UX. This index is what holds under a race."""
    _add_volunteer(app_env, vol_id="1", email="dup@example.com")
    conn = _conn(app_env)
    import security
    from config import settings
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO volunteers (vol_id, name, email_hmac) VALUES (?, ?, ?)",
            ("2", "Other P.", security.email_hmac(settings.email_hmac_key, "DUP@example.com")),
        )
    # But many rows may have no address at all.
    conn.execute("INSERT INTO volunteers (vol_id, name) VALUES ('3', 'No Email')")
    conn.execute("INSERT INTO volunteers (vol_id, name) VALUES ('4', 'Also None')")
    conn.commit()
    conn.close()


def test_rev_counter_is_monotonic(app_env):
    import db as dbm
    conn = _conn(app_env)
    revs = [dbm.next_rev(conn) for _ in range(5)]
    assert revs == sorted(revs) and len(set(revs)) == 5
    assert dbm.current_rev(conn) == revs[-1]
    conn.close()


# ── crypto ────────────────────────────────────────────────────────────────────

def test_email_hmac_normalizes_case_and_whitespace_only(app_env):
    import security
    k = b"k" * 32
    assert security.email_hmac(k, "  Jim@Example.COM ") == security.email_hmac(k, "jim@example.com")
    # Plus-tags and dots stay distinct: merging them would hand one person another's signup.
    assert security.email_hmac(k, "a+x@b.com") != security.email_hmac(k, "a@b.com")
    assert security.email_hmac(k, "a.b@c.com") != security.email_hmac(k, "ab@c.com")


def test_hmac_key_fp_proves_possession_without_revealing_the_key(app_env):
    import hashlib
    import security
    k = b"k" * 32
    fp = security.hmac_key_fp(k)
    assert fp != hashlib.sha256(k).hexdigest()   # not a plain hash of the key
    assert security.hmac_key_fp(b"j" * 32) != fp


def test_rsa_roundtrip_and_fingerprint_format(rsa_keys, app_env):
    import security
    priv, pub = rsa_keys
    ct = security.encrypt_email(pub, "  Jim@Example.COM ")
    assert security.decrypt_email(priv, ct) == "jim@example.com"
    # OAEP is randomized, so equal addresses do not produce equal ciphertext.
    assert security.encrypt_email(pub, "a@b.c") != security.encrypt_email(pub, "a@b.c")
    fp = security.pubkey_fingerprint(pub)
    assert fp.startswith("SHA256:") and "=" not in fp


def test_magic_link_lands_on_frontend_verify(app_env, client):
    """The link opens the frontend's /apply/verify, on the public URL, not the portal's API."""
    import email
    import email.policy
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    eml = sorted((app_env[1] / "mail").glob("*.eml"))[-1]
    msg = email.message_from_bytes(eml.read_bytes(), policy=email.policy.default)
    text = msg.get_body(preferencelist=("plain",)).get_content()
    assert "https://portal.test/apply/verify?t=" in text
    assert "/api/portal/verify" not in text


def test_tokens_are_stored_hashed_only(app_env, client):
    """A read of portal.db must not be enough to mint a login."""
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    token = _link_from_mail(app_env)
    conn = _conn(app_env)
    rows = conn.execute("SELECT token_hash FROM login_tokens").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["token_hash"] != token
    import security
    assert rows[0]["token_hash"] == security.hash_secret(token)


# ── auth ──────────────────────────────────────────────────────────────────────

def test_request_link_unknown_address_discloses_by_default(client):
    r = client.post("/api/portal/request_link", data={"email": "nobody@example.com"})
    assert r.status_code == 200
    assert r.json()["sent"] is False and r.json()["known"] is False


def test_request_link_generic_copy_when_disclosure_is_off(app_env, client):
    conn = _conn(app_env)
    conn.execute("INSERT INTO portal_kv (k, v) VALUES ('portal_login_disclose', '0')")
    conn.commit()
    conn.close()
    r = client.post("/api/portal/request_link", data={"email": "nobody@example.com"})
    assert r.json()["sent"] is True and "known" not in r.json()


@pytest.mark.parametrize("t", [
    '"><script>alert(1)</script>', "abc' onmouseover='x", "</form><img src=x onerror=alert(1)>",
    "a" * 300, "short",
])
def test_the_verify_page_never_reflects_anything_but_a_token(client, t):
    """CodeQL #10: ?t= used to go into the page's HTML unescaped."""
    import urllib.parse
    page = client.get("/api/portal/verify?t=" + urllib.parse.quote(t))
    assert page.status_code == 200
    assert "<script>" not in page.text and "onerror" not in page.text
    assert "onmouseover" not in page.text and t not in page.text
    assert "missing its token" in page.text and "<form" not in page.text


def test_full_login_flow_get_then_post(app_env, client):
    _add_volunteer(app_env)
    assert client.post("/api/portal/request_link",
                       data={"email": "jim@example.com"}).json()["sent"] is True
    token = _link_from_mail(app_env)

    # The GET must render a confirmation and consume nothing: scanners fetch URLs.
    page = client.get(f"/api/portal/verify?t={token}")
    assert page.status_code == 200 and "Confirm sign-in" in page.text
    assert f'name="t" value="{token}"' in page.text            # a real token still works
    conn = _conn(app_env)
    assert conn.execute("SELECT used_at FROM login_tokens").fetchone()["used_at"] is None
    conn.close()

    r = client.post("/api/portal/verify", data={"t": token})
    assert r.status_code == 200 and r.json()["volunteer"]["vol_id"] == "1042"
    assert client.get("/api/portal/me").json()["volunteer"]["vol_id"] == "1042"


def test_token_is_single_use(app_env, client):
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    token = _link_from_mail(app_env)
    assert client.post("/api/portal/verify", data={"t": token}).status_code == 200
    again = client.post("/api/portal/verify", data={"t": token})
    assert again.status_code == 400 and "already used" in again.json()["detail"]


def test_token_expires(app_env, client):
    import clock
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    token = _link_from_mail(app_env)
    clock.shift(minutes=16)      # default TTL is 15
    try:
        r = client.post("/api/portal/verify", data={"t": token})
        assert r.status_code == 400
    finally:
        clock.reset()


def test_a_resend_invalidates_the_previous_link(app_env, client):
    """A forwarded older mail must not remain a working credential."""
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    first = _link_from_mail(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    second = _link_from_mail(app_env)
    assert first != second
    assert client.post("/api/portal/verify", data={"t": first}).status_code == 400
    assert client.post("/api/portal/verify", data={"t": second}).status_code == 200


def test_session_id_is_stored_hashed(app_env, client):
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    client.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})
    cookie = client.cookies.get("portal_sid")
    conn = _conn(app_env)
    stored = conn.execute("SELECT sid_hash FROM sessions").fetchone()["sid_hash"]
    conn.close()
    import security
    assert stored != cookie and stored == security.hash_secret(cookie)


def test_remember_me_survives_beyond_the_short_ttl(app_env, client):
    import clock
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    client.post("/api/portal/verify", data={"t": _link_from_mail(app_env), "remember": "1"})
    clock.shift(hours=24)        # past the 12h non-remember window
    try:
        assert client.get("/api/portal/me").status_code == 200
    finally:
        clock.reset()


def test_plain_session_expires(app_env, client):
    import clock
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    client.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})
    clock.shift(hours=13)
    try:
        assert client.get("/api/portal/me").status_code == 401
    finally:
        clock.reset()


def test_logout_all_revokes_every_session(app_env, client):
    from fastapi.testclient import TestClient
    import main
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    client.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})

    with TestClient(main.app, base_url="https://portal.test") as second:
        second.post("/api/portal/request_link", data={"email": "jim@example.com"})
        second.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})
        assert second.get("/api/portal/me").status_code == 200
        assert client.post("/api/portal/logout_all").json()["revoked"] == 2
        assert second.get("/api/portal/me").status_code == 401


def test_me_requires_a_full_session_not_a_signup_session(app_env, client):
    """vol_id IS NULL authorises the signup submit and nothing else."""
    import security        # the fixture's freshly imported copy
    conn = _conn(app_env)
    conn.execute(
        "INSERT INTO login_tokens (token_hash, purpose, vol_id, email_hmac, created_at, expires_at) "
        "VALUES (?, 'signup', NULL, 'abc', '2020-01-01T00:00:00Z', '2099-01-01T00:00:00Z')",
        (security.hash_secret("signup-token"),),
    )
    conn.commit()
    conn.close()
    r = client.post("/api/portal/verify", data={"t": "signup-token"})
    assert r.status_code == 200 and r.json()["volunteer"] is None
    assert client.get("/api/portal/me").status_code == 403


def test_rate_limit_trips(app_env, client, monkeypatch):
    _add_volunteer(app_env)
    for _ in range(5):
        client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    r = client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    assert r.status_code == 429



@pytest.mark.parametrize("ip, bucket", [
    ("2001:db8:24e8:1b02::638", "2001:db8:24e8:1b02::/64"),
    ("2001:db8:24e8:1b02:ffff:1:2:3", "2001:db8:24e8:1b02::/64"),
    ("fe80::1%eth0", "fe80::/64"),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("203.0.113.9", "203.0.113.9"),
    ("unknown", "unknown"),
])
def test_ipv6_clients_are_counted_by_their_64(app_env, ip, bucket):
    import ratelimit
    assert ratelimit.ip_bucket(ip) == bucket


def test_rotating_addresses_within_a_64_shares_one_limit(app_env, client):
    def ask(n, ip):
        return client.post("/api/portal/request_link", data={"email": f"v{n}@example.com"},
                           headers={"x-forwarded-for": ip}).status_code
    # A fresh address and a fresh email every time, so only the per-IP bucket can trip.
    for n in range(5):
        assert ask(n, f"2001:db8:1:2::{n + 1:x}") != 429
    assert ask(5, "2001:db8:1:2::ff") == 429
    assert ask(6, "2001:db8:1:3::1") != 429          # the next /64 is someone else


# ── sync: authentication ──────────────────────────────────────────────────────

def test_sync_requires_a_signature(client):
    assert client.get("/api/sync/heartbeat").status_code == 401


def test_sync_accepts_a_valid_signature(client):
    r = _signed(client, "GET", "/api/sync/heartbeat")
    assert r.status_code == 200 and "hmac_key_fp" in r.json()


def test_sync_rejects_a_wrong_secret(client):
    assert _signed(client, "GET", "/api/sync/heartbeat", secret="wrong").status_code == 401


def test_sync_rejects_an_unknown_key_id(client):
    assert _signed(client, "GET", "/api/sync/heartbeat", key_id="nope").status_code == 401


def test_sync_rejects_a_stale_timestamp(client):
    import clock
    old = int(clock.now().timestamp()) - 400      # window is 300s
    assert _signed(client, "GET", "/api/sync/heartbeat", ts=old).status_code == 401


def test_sync_rejects_a_replayed_nonce(client):
    import security
    nonce = security.new_nonce()
    assert _signed(client, "GET", "/api/sync/heartbeat", nonce=nonce).status_code == 200
    assert _signed(client, "GET", "/api/sync/heartbeat", nonce=nonce).status_code == 401


def test_signature_is_bound_to_the_path(client):
    """A capture must not be replayable against a different endpoint."""
    import clock
    import security
    ts = str(int(clock.now().timestamp()))
    nonce = security.new_nonce()
    sig = security.sign_request(SYNC_SECRET.encode(), "GET", "/api/sync/heartbeat", ts, nonce, b"")
    r = client.get("/api/sync/config", headers={
        "x-sync-key-id": SYNC_KEY_ID, "x-sync-timestamp": ts,
        "x-sync-nonce": nonce, "x-sync-signature": sig,
    })
    assert r.status_code == 401


def test_signature_is_bound_to_the_body(client):
    import clock
    import security
    ts = str(int(clock.now().timestamp()))
    nonce = security.new_nonce()
    real = json.dumps({"op_id": "x", "announcements": []}).encode()
    sig = security.sign_request(SYNC_SECRET.encode(), "POST", "/api/sync/announcements",
                                ts, nonce, real)
    tampered = json.dumps({"op_id": "x", "announcements": [{"ann_id": "evil"}]}).encode()
    r = client.post("/api/sync/announcements", content=tampered, headers={
        "x-sync-key-id": SYNC_KEY_ID, "x-sync-timestamp": ts, "x-sync-nonce": nonce,
        "x-sync-signature": sig, "content-type": "application/json",
    })
    assert r.status_code == 401


def test_previous_secret_is_accepted_during_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("EMAIL_HMAC_KEY", HMAC_KEY)
    monkeypatch.setenv("SYNC_KEY_ID", SYNC_KEY_ID)
    monkeypatch.setenv("SYNC_SECRET", "new-secret")
    monkeypatch.setenv("SYNC_SECRET_PREV", "old-secret")
    monkeypatch.setenv("PORTAL_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MAIL_BACKEND", "file")
    monkeypatch.setenv("MAIL_DIR", str(tmp_path / "mail"))
    monkeypatch.setenv("PORTAL_RESPONSE_FLOOR_MS", "0")
    main = _fresh_portal()
    from fastapi.testclient import TestClient
    with TestClient(main.app, base_url="https://portal.test") as c:
        assert _signed(c, "GET", "/api/sync/heartbeat", secret="new-secret").status_code == 200
        assert _signed(c, "GET", "/api/sync/heartbeat", secret="old-secret").status_code == 200
        assert _signed(c, "GET", "/api/sync/heartbeat", secret="other").status_code == 401


# ── sync: idempotency and pushes ──────────────────────────────────────────────

def test_push_requires_an_op_id(client):
    r = _signed(client, "POST", "/api/sync/announcements", {"announcements": []})
    assert r.status_code == 400 and "op_id" in r.json()["detail"]


def test_op_id_replay_returns_the_stored_result_without_reapplying(client):
    payload = {"op_id": "op-1", "announcements": [{"ann_id": "a1", "title": "Hi"}]}
    first = _signed(client, "POST", "/api/sync/announcements", payload)
    assert first.status_code == 200 and first.json()["count"] == 1
    # Same op_id, different contents: the stored result wins and nothing is applied.
    payload2 = {"op_id": "op-1", "announcements": [{"ann_id": "a2"}, {"ann_id": "a3"}]}
    second = _signed(client, "POST", "/api/sync/announcements", payload2)
    assert second.json() == first.json()


def test_schedule_push_replaces_and_reports_counts(client):
    payload = {
        "op_id": "sched-1",
        "content_hash": "hash-a",
        "days":  [{"day_key": "sat", "day_str": "Saturday", "sort_order": 0}],
        "times": [{"time_key": "0800", "time_str": "0800-1200", "sort_order": 0},
                  {"time_key": "1130", "time_str": "1130-1530", "sort_order": 1}],
        "day_times": [{"day_key": "sat", "time_key": "0800", "remaining": 12},
                      {"day_key": "sat", "time_key": "1130", "remaining": 3}],
        "positions": [{"pos_key": "sec1", "pos_cat": "Security", "pos_str": "Security 1"}],
        "slots": [{"day_key": "sat", "time_key": "0800", "pos_key": "sec1", "vol_id": None}],
    }
    r = _signed(client, "POST", "/api/sync/schedule", payload)
    assert r.status_code == 200, r.text
    assert r.json()["counts"]["slots"] == 1
    assert r.json()["counts"]["day_times"] == 2

    hb = _signed(client, "GET", "/api/sync/heartbeat").json()
    assert hb["snapshot_hash"] == "hash-a" and hb["snapshot_rev"] > 0


def test_unchanged_schedule_push_is_a_no_op(client):
    payload = {"op_id": "s1", "content_hash": "same", "days": [], "slots": []}
    assert _signed(client, "POST", "/api/sync/schedule", payload).status_code == 200
    payload2 = dict(payload, op_id="s2")
    r = _signed(client, "POST", "/api/sync/schedule", payload2)
    assert r.json() == {"ok": True, "unchanged": True}


def test_volunteer_push_upserts_and_carries_picks(client):
    payload = {"op_id": "v1", "volunteers": [{
        "vol_id": "1042", "name": "Jim E.", "status": "active",
        "time_pref": "Saturday 0800-1200;Sunday 0800-1200",
        "picks": [{"day_key": "sat", "time_key": "0800", "kind": "pref", "rank": 1},
                  {"day_key": "sun", "time_key": "0800", "kind": "pref", "rank": 2},
                  {"day_key": "sat", "time_key": "1500", "kind": "add", "rank": None}],
    }]}
    r = _signed(client, "POST", "/api/sync/volunteers", payload)
    assert r.status_code == 200 and r.json()["written"] == 1

    pulled = _signed(client, "GET", "/api/sync/volunteers?since=0").json()
    assert len(pulled["volunteers"]) == 1
    vol = pulled["volunteers"][0]
    assert vol["vol_id"] == "1042"
    assert {(p["day_key"], p["time_key"]) for p in vol["picks"]} == {
        ("sat", "0800"), ("sun", "0800"), ("sat", "1500")}


def test_volunteer_push_updates_only_the_columns_it_carries(app_env, client):
    """The scheduler doesn't hold first_name / last_name / nickname; omitting them keeps them."""
    _signed(client, "POST", "/api/sync/volunteers", {"op_id": "full", "volunteers": [{
        "vol_id": "1042", "first_name": "Jim", "last_name": "Elliott", "nickname": "Zero Cool",
        "name": "Jim E. (Zero Cool)", "status": "pending", "time_pref": "Saturday 0800-1200"}]})
    r = _signed(client, "POST", "/api/sync/volunteers", {"op_id": "status-only", "volunteers": [
        {"vol_id": "1042", "status": "active"}]})
    assert r.status_code == 200 and r.json()["written"] == 1
    conn = _conn(app_env)
    row = conn.execute("SELECT first_name, last_name, nickname, name, status, time_pref "
                       "FROM volunteers WHERE vol_id = '1042'").fetchone()
    conn.close()
    assert tuple(row) == ("Jim", "Elliott", "Zero Cool", "Jim E. (Zero Cool)", "active",
                          "Saturday 0800-1200")


def test_volunteer_push_with_nothing_to_update_leaves_the_row_alone(app_env, client):
    _signed(client, "POST", "/api/sync/volunteers", {"op_id": "one", "volunteers": [
        {"vol_id": "1042", "name": "Jim E.", "status": "pending"}]})
    conn = _conn(app_env)
    before = tuple(conn.execute("SELECT * FROM volunteers WHERE vol_id = '1042'").fetchone())
    conn.close()
    r = _signed(client, "POST", "/api/sync/volunteers", {"op_id": "two", "volunteers": [
        {"vol_id": "1042"}]})
    assert r.status_code == 200
    conn = _conn(app_env)
    after = tuple(conn.execute("SELECT * FROM volunteers WHERE vol_id = '1042'").fetchone())
    conn.close()
    assert after == before          # not even rev or updated_at moves


def _insert_columns(sql: str) -> tuple:
    """The column list of an ``INSERT INTO t (a, b, ...) VALUES`` statement."""
    import re
    cols = re.match(r"INSERT INTO \w+ \(([^)]*)\) VALUES", sql).group(1)
    return tuple(c.strip() for c in cols.split(","))


def test_sync_statements_list_the_columns_their_parameters_fill(app_env):
    """Every statement is a literal, so its column list and the tuple filling it can drift."""
    import sys
    sync = sys.modules["routers.sync"]
    for key, (delete_sql, insert_sql, cols) in sync.SNAPSHOT_TABLES.items():
        table = insert_sql.split()[2]
        assert delete_sql.split() == ["DELETE", "FROM", table], key
        assert _insert_columns(insert_sql) == cols, key
        assert insert_sql.count("?") == len(cols), key
    upsert = sync._UPSERT_VOLUNTEER_SQL
    assert _insert_columns(upsert) == ("vol_id", *sync.PUSH_COLUMNS, "updated_at", "rev")
    updated = [c for c in sync.PUSH_COLUMNS if f"{c} = CASE WHEN ? THEN excluded.{c} " in upsert]
    assert updated == list(sync.PUSH_COLUMNS)
    # values, then one flag per column, then "anything sent"
    assert upsert.count("?") == (len(sync.PUSH_COLUMNS) + 3) + len(sync.PUSH_COLUMNS) + 1


def test_volunteer_pull_cursor_only_returns_changes(client):
    _signed(client, "POST", "/api/sync/volunteers",
            {"op_id": "a", "volunteers": [{"vol_id": "1", "name": "A"}]})
    first = _signed(client, "GET", "/api/sync/volunteers?since=0").json()
    cursor = first["cursor"]
    assert _signed(client, "GET", f"/api/sync/volunteers?since={cursor}").json()["volunteers"] == []
    _signed(client, "POST", "/api/sync/volunteers",
            {"op_id": "b", "volunteers": [{"vol_id": "2", "name": "B"}]})
    second = _signed(client, "GET", f"/api/sync/volunteers?since={cursor}").json()
    assert [v["vol_id"] for v in second["volunteers"]] == ["2"]


def test_rekey_cascades_to_sessions_and_picks(app_env, client):
    _add_volunteer(app_env, vol_id="p-abc12345")
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    client.post("/api/portal/verify", data={"t": _link_from_mail(app_env)})
    assert client.get("/api/portal/me").json()["volunteer"]["vol_id"] == "p-abc12345"

    conn = _conn(app_env)
    conn.execute("INSERT INTO vol_picks (vol_id, day_key, time_key, kind, rank) "
                 "VALUES ('p-abc12345', 'sat', '0800', 'pref', 1)")
    conn.commit()
    conn.close()

    r = _signed(client, "POST", "/api/sync/volunteers",
                {"op_id": "rk", "rekeys": [{"vol_id": "p-abc12345", "new_vol_id": "1042"}]})
    assert r.status_code == 200 and r.json()["renamed"] == 1
    # The open session survives the rename, which is the whole point.
    me = client.get("/api/portal/me")
    assert me.status_code == 200 and me.json()["volunteer"]["vol_id"] == "1042"
    assert me.json()["picks"][0]["time_key"] == "0800"


def test_settings_push_rejects_unknown_keys(client):
    r = _signed(client, "POST", "/api/sync/settings",
                {"op_id": "s", "settings": {"portal_signup_open": "1", "haxx": "1"}})
    assert r.status_code == 400 and "haxx" in r.json()["detail"]


def test_settings_push_sets_pubkey_and_heartbeat_reports_its_fingerprint(client, rsa_keys):
    import security
    _, pub = rsa_keys
    r = _signed(client, "POST", "/api/sync/settings",
                {"op_id": "s1", "settings": {"email_pubkey": pub}})
    assert r.status_code == 200
    hb = _signed(client, "GET", "/api/sync/heartbeat").json()
    assert hb["pubkey_fp"] == security.pubkey_fingerprint(pub)


def test_email_index_backfill_and_conflict_reporting(client):
    _signed(client, "POST", "/api/sync/volunteers", {"op_id": "v", "volunteers": [
        {"vol_id": "1", "name": "A"}, {"vol_id": "2", "name": "B"}]})
    r = _signed(client, "POST", "/api/sync/email_index", {"op_id": "e1", "pairs": [
        {"vol_id": "1", "email_hmac": "aaa"}, {"vol_id": "2", "email_hmac": "bbb"}]})
    assert r.json()["updated"] == 2 and r.json()["conflicts"] == []
    # Two vol_ids resolving to one address is a duplicate for an organizer, not a silent fix.
    r2 = _signed(client, "POST", "/api/sync/email_index",
                 {"op_id": "e2", "pairs": [{"vol_id": "2", "email_hmac": "aaa"}]})
    assert r2.json()["conflicts"] == ["2"]


# ── branding ──────────────────────────────────────────────────────────────────

def _png() -> str:
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+0f8AAAAASUVORK5CYII="
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def test_branding_defaults_before_any_push(client):
    b = client.get("/api/portal/branding").json()
    assert b["title"] == "Volunteer Portal" and b["has_logo"] is False
    assert b["has_header_logo"] is False


def test_branding_push_decodes_and_serves_with_an_etag(client):
    r = _signed(client, "POST", "/api/sync/branding", {
        "op_id": "b1", "content_hash": "bh1", "app_title": "BSidesCharm",
        "portal_title": "BSidesCharm 2026 Volunteers",
        "logo": _png(), "favicon": _png(),
    })
    assert r.status_code == 200 and r.json()["logo_bytes"] > 0

    meta = client.get("/api/portal/branding").json()
    assert meta["title"] == "BSidesCharm 2026 Volunteers" and meta["has_logo"] is True

    img = client.get("/api/portal/branding/logo")
    assert img.status_code == 200 and img.headers["content-type"].startswith("image/png")
    etag = img.headers["etag"]
    assert "max-age=3600" in img.headers["cache-control"]
    # The whole reason for storing bytes rather than inlining a data URL.
    again = client.get("/api/portal/branding/logo", headers={"if-none-match": etag})
    assert again.status_code == 304


def test_header_logo_is_pushed_and_served_with_its_own_type(client):
    webp = "data:image/webp;base64," + base64.b64encode(b"RIFFfakeWEBP").decode()
    r = _signed(client, "POST", "/api/sync/branding", {
        "op_id": "h1", "content_hash": "hh1", "app_title": "BSidesCharm",
        "logo": _png(), "favicon": _png(), "header_logo": webp,
    })
    assert r.status_code == 200 and r.json()["header_logo_bytes"] == len(b"RIFFfakeWEBP")
    assert client.get("/api/portal/branding").json()["has_header_logo"] is True
    head = client.get("/api/portal/branding/header_logo")
    assert head.status_code == 200 and head.content == b"RIFFfakeWEBP"
    assert head.headers["content-type"].startswith("image/webp")
    # The theme logo keeps its own type, and the etags differ per image.
    logo = client.get("/api/portal/branding/logo")
    assert logo.headers["content-type"].startswith("image/png")
    assert logo.headers["etag"] != head.headers["etag"]


def test_a_push_without_a_header_logo_clears_it(client):
    """An older scheduler sends no header_logo; the header falls back to the theme logo."""
    webp = "data:image/webp;base64," + base64.b64encode(b"RIFFfakeWEBP").decode()
    _signed(client, "POST", "/api/sync/branding",
            {"op_id": "h2", "content_hash": "hh2", "logo": _png(), "header_logo": webp})
    _signed(client, "POST", "/api/sync/branding",
            {"op_id": "h3", "content_hash": "hh3", "logo": _png()})
    assert client.get("/api/portal/branding").json()["has_header_logo"] is False
    assert client.get("/api/portal/branding/header_logo").status_code == 404


def test_branding_carries_the_conference_contact(client):
    b = client.get("/api/portal/branding").json()
    assert (b["contact_email"], b["conference_url"], b["code_of_conduct_url"]) == ("", "", "")
    r = _signed(client, "POST", "/api/sync/branding", {
        "op_id": "c1", "content_hash": "ch1", "app_title": "ExampleCon",
        "contact_email": "volunteers@example.org", "conference_url": "https://example.org/",
        "code_of_conduct_url": "https://example.org/coc"})
    assert r.status_code == 200, r.text
    b = client.get("/api/portal/branding").json()
    assert (b["contact_email"], b["conference_url"], b["code_of_conduct_url"]) == (
        "volunteers@example.org", "https://example.org/", "https://example.org/coc")
    # A push without them (an older scheduler, or the organizers cleared them) clears them.
    _signed(client, "POST", "/api/sync/branding",
            {"op_id": "c2", "content_hash": "ch2", "app_title": "ExampleCon"})
    assert client.get("/api/portal/branding").json()["contact_email"] == ""


@pytest.mark.parametrize("field, value", [
    ("conference_url", "javascript:alert(1)"), ("code_of_conduct_url", "//evil.example/"),
    ("conference_url", "https://example.org/a b"), ("contact_email", "x@y.org\nBcc: z@y.org"),
    ("contact_email", 7),
])
def test_branding_push_refuses_a_contact_unsafe_to_link(client, field, value):
    r = _signed(client, "POST", "/api/sync/branding",
                {"op_id": f"u-{field}-{value!s:.10}", "app_title": "X", field: value})
    assert r.status_code == 400


def test_branding_push_rejects_a_non_data_url(client):
    r = _signed(client, "POST", "/api/sync/branding",
                {"op_id": "b2", "logo": "https://evil.example/logo.png"})
    assert r.status_code == 400


def test_unchanged_branding_push_is_a_no_op(client):
    p = {"op_id": "b3", "content_hash": "same", "app_title": "X", "logo": _png()}
    assert _signed(client, "POST", "/api/sync/branding", p).status_code == 200
    r = _signed(client, "POST", "/api/sync/branding", dict(p, op_id="b4"))
    assert r.json() == {"ok": True, "unchanged": True}


# ── relay mail backend ────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body=b""):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://mailrelay/", "mailrelay:5002"])
def test_relay_url_must_be_http(monkeypatch, url):
    """urlopen would follow file:// and friends; the config refuses them before it gets there."""
    monkeypatch.setenv("PORTAL_SKIP_CONFIG", "1")
    monkeypatch.setenv("MAIL_BACKEND", "relay")
    monkeypatch.setenv("MAIL_RELAY_URL", "http://mailrelay:5002")
    monkeypatch.setenv("MAIL_RELAY_TOKEN", "relay-token")
    sys.modules.pop("config", None)
    import config          # importing loads once; that one must succeed
    monkeypatch.setenv("MAIL_RELAY_URL", url)
    with pytest.raises(config.ConfigError, match="MAIL_RELAY_URL"):
        config.load(require_secrets=False)


def _relay_mailer(monkeypatch, calls, status=202, error=None):
    """A mailer on the relay backend whose urlopen records requests instead of sending."""
    import io
    import urllib.error
    monkeypatch.setenv("PORTAL_SKIP_CONFIG", "1")
    monkeypatch.setenv("MAIL_BACKEND", "relay")
    monkeypatch.setenv("MAIL_RELAY_URL", "http://mailrelay:5002/")
    monkeypatch.setenv("MAIL_RELAY_TOKEN", "relay-token")
    for mod in ("config", "mailer"):
        sys.modules.pop(mod, None)
    import config
    import mailer

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if error:
            raise error
        if status != 202:
            raise urllib.error.HTTPError(req.full_url, status, "", {}, io.BytesIO(
                json.dumps({"error": "Graph sendMail failed: HTTP 403 ErrorAccessDenied"}).encode()))
        return _FakeResp(json.dumps({"sent": True, "id": "abc123"}).encode())

    monkeypatch.setattr(mailer.urllib.request, "urlopen", fake_urlopen)
    return mailer.Mailer(config.load(require_secrets=False))


def test_relay_backend_requires_its_token(monkeypatch):
    monkeypatch.setenv("PORTAL_SKIP_CONFIG", "1")
    monkeypatch.setenv("MAIL_BACKEND", "file")  # the import itself must succeed
    sys.modules.pop("config", None)
    import config
    monkeypatch.setenv("MAIL_BACKEND", "relay")
    monkeypatch.delenv("MAIL_RELAY_TOKEN", raising=False)
    with pytest.raises(config.ConfigError, match="MAIL_RELAY_TOKEN"):
        config.load(require_secrets=False)


def test_relay_send_posts_only_to_subject_and_plain_text_body(monkeypatch):
    calls = []
    m = _relay_mailer(monkeypatch, calls)
    assert m.send_login_link("vol@example.com", "TOKEN123", "login") == "abc123"

    (req,) = calls
    assert req.full_url == "http://mailrelay:5002/send"
    assert req.get_header("Authorization") == "Bearer relay-token"
    assert req.get_header("Content-type") == "application/json"
    sent = json.loads(req.data)
    assert set(sent) == {"to", "subject", "body"}  # no html, no from
    assert sent["to"] == "vol@example.com"
    assert "/apply/verify?t=TOKEN123" in sent["body"] and "<" not in sent["body"]


def test_relay_errors_name_the_cause_but_not_the_recipient(monkeypatch):
    import urllib.error
    m = _relay_mailer(monkeypatch, [], status=502)
    import mailer  # after _relay_mailer, which re-imports it
    with pytest.raises(mailer.MailError) as exc:
        m.send("secret.person@example.com", "s", "t")
    assert "502" in str(exc.value) and "ErrorAccessDenied" in str(exc.value)
    assert "secret.person" not in str(exc.value)

    m = _relay_mailer(monkeypatch, [], error=urllib.error.URLError("Name or service not known"))
    import mailer
    with pytest.raises(mailer.MailError, match="unreachable") as exc:
        m.send("secret.person@example.com", "s", "t")
    assert "secret.person" not in str(exc.value)


# ── the audit log must never contain an address ────────────────────────────────

def test_audit_log_records_actions_not_addresses(app_env, client):
    _add_volunteer(app_env)
    client.post("/api/portal/request_link", data={"email": "jim@example.com"})
    conn = _conn(app_env)
    rows = conn.execute("SELECT action, detail, actor_vol_id FROM portal_audit").fetchall()
    conn.close()
    assert rows, "expected an audit entry"
    blob = " ".join(f"{r['action']} {r['detail']} {r['actor_vol_id']}" for r in rows)
    assert "jim@example.com" not in blob and "@" not in blob


# ── regular expressions on outside input (CodeQL #4, #5, #6) ──────────────────

_ADDRESSES = {
    "jim@example.com": True, "first.last+tag@sub.example.co.uk": True, "a@b.c": True,
    "no-at.example.com": False, "two@@example.com": False, "a@nodot": False,
    "a@.example.com": False, "a@example..com": False, "a@example.com.": False,
    "sp ace@example.com": False, "a@exa mple.com": False,
}


def test_the_address_patterns_accept_what_they_should(app_env):  # app_env: the settings routers import
    from routers import signup, sync
    for pattern in (signup._EMAIL, sync._ADDR):
        for addr, ok in _ADDRESSES.items():
            assert bool(pattern.match(addr)) is ok, (pattern.pattern, addr)


def test_the_branding_link_pattern_accepts_what_it_should(app_env):  # app_env: the settings routers import
    from routers import sync
    for link, ok in {"https://example.org": True, "https://example.org/coc?x=1#y": True,
                     "http://example.org?q": True, "https://": False, "https:///x": False,
                     "https://a b.org": False, "javascript:alert(1)": False,
                     "//evil.example": False}.items():
        assert bool(sync._PUBLIC_LINK.match(link)) is ok, link


def test_the_patterns_stay_fast_on_their_worst_inputs(app_env):  # app_env: the settings routers import
    import time
    from routers import signup, sync
    worst = ["a@" + "." * 5000 + "@", "a@" + "a." * 5000 + "!", "a@" + "a" * 10000 + "!"]
    links = ["https://" + "a" * 10000 + " ", "https://" + "a/" * 5000 + " "]
    started = time.perf_counter()
    for text in worst:
        signup._EMAIL.match(text)
        sync._ADDR.match(text)
    for text in links:
        sync._PUBLIC_LINK.match(text)
    assert time.perf_counter() - started < 0.5
