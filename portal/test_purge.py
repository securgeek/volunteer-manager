"""POST /api/sync/purge_volunteers: removing a volunteer outright, so the address can sign up
again from scratch (clearing test volunteers).

In-process like test_signup.py, whose helpers this reuses.

    pytest portal/test_purge.py -v
"""
# The fixtures are used by name, and the helpers are the other test modules' own.
# pylint: disable=unused-import,import-private-name
from test_portal_local import _conn, _signed, app_env, client, rsa_keys  # noqa: F401
from test_signup import _submit, _verified, open_signup  # noqa: F401

EMAIL = "test.vol@example.com"


def _purge(client, op, *vol_ids):
    return _signed(client, "POST", "/api/sync/purge_volunteers",
                   {"op_id": op, "vol_ids": list(vol_ids)})


def _count(app_env, stmt, params=()):
    conn = _conn(app_env)
    n = conn.execute(stmt, params).fetchone()[0]
    conn.close()
    return n


def test_a_purge_removes_everything_and_the_address_can_sign_up_again(app_env, open_signup):
    c = _verified(open_signup, app_env, EMAIL)
    r = _submit(c)
    assert r.status_code == 200, r.text
    vol_id = r.json()["vol_id"]
    conn = _conn(app_env)
    hmac = conn.execute("SELECT email_hmac FROM volunteers WHERE vol_id = ?",
                        (vol_id,)).fetchone()[0]
    conn.close()
    # A second sign-in link, still unused, and the address's rate-limit counters.
    open_signup.post("/api/portal/request_link", data={"email": EMAIL})
    assert _count(app_env, "SELECT COUNT(*) FROM rate_limits WHERE bucket_key LIKE ?",
                  (f"%:addr:%:{hmac}",)) > 0

    r = _purge(open_signup, "purge-1", vol_id)
    assert r.status_code == 200 and r.json()["purged"] == [vol_id], r.text

    for stmt in ("SELECT COUNT(*) FROM volunteers WHERE vol_id = ?",
                 "SELECT COUNT(*) FROM vol_picks WHERE vol_id = ?",
                 "SELECT COUNT(*) FROM sessions WHERE vol_id = ?",
                 "SELECT COUNT(*) FROM login_tokens WHERE vol_id = ?"):
        assert _count(app_env, stmt, (vol_id,)) == 0, stmt
    for stmt in ("SELECT COUNT(*) FROM sessions WHERE email_hmac = ?",
                 "SELECT COUNT(*) FROM login_tokens WHERE email_hmac = ?"):
        assert _count(app_env, stmt, (hmac,)) == 0, stmt
    assert _count(app_env, "SELECT COUNT(*) FROM rate_limits WHERE bucket_key LIKE ?",
                  (f"%:addr:%:{hmac}",)) == 0
    assert _count(app_env, "SELECT COUNT(*) FROM portal_audit WHERE action = "
                  "'volunteer.purged' AND actor_vol_id = ?", (vol_id,)) == 1

    # The browser that was signed in is signed out, and the address starts over.
    assert c.get("/api/portal/me").status_code == 401
    r = open_signup.post("/api/portal/signup/check_email", data={"email": EMAIL})
    assert r.status_code == 200 and r.json()["sent"] is True and not r.json().get("registered")
    again = _verified(open_signup, app_env, EMAIL)
    # Their Saturday 1500 place comes back with the scheduler's next snapshot, not here.
    r = _submit(again, prefs=["sat_0800", "sun_1400"])
    assert r.status_code == 200 and r.json()["vol_id"] != vol_id, r.text


def test_other_volunteers_and_their_counters_are_untouched(app_env, open_signup):
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app, base_url="https://portal.test") as a, \
         TestClient(main.app, base_url="https://portal.test") as b:
        gone = _submit(_verified(a, app_env, "gone@example.com")).json()["vol_id"]
        kept = _submit(_verified(b, app_env, "kept@example.com"),
                       prefs=["sat_0800", "sun_1400"]).json()["vol_id"]
        assert _purge(open_signup, "purge-2", gone).json()["purged"] == [gone]
        assert b.get("/api/portal/me").status_code == 200
    assert _count(app_env, "SELECT COUNT(*) FROM volunteers WHERE vol_id = ?", (kept,)) == 1
    assert _count(app_env, "SELECT COUNT(*) FROM vol_picks WHERE vol_id = ?", (kept,)) > 0


def test_unknown_ids_are_skipped_and_a_replay_does_nothing_more(app_env, open_signup):
    r = _purge(open_signup, "purge-3", "p-nobody", "imported-7")
    assert r.status_code == 200 and r.json()["purged"] == []
    assert _purge(open_signup, "purge-3", "p-nobody").json() == r.json()


def test_a_purge_must_be_signed_and_well_formed(client):
    assert client.post("/api/sync/purge_volunteers",
                       json={"op_id": "x", "vol_ids": ["p-1"]}).status_code == 401
    for bad in ({"op_id": "b1"}, {"op_id": "b2", "vol_ids": "p-1"},
                {"op_id": "b3", "vol_ids": [""]}, {"op_id": "b4", "vol_ids": [7]}):
        assert _signed(client, "POST", "/api/sync/purge_volunteers", bad).status_code == 400
