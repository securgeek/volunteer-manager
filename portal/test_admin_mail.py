"""POST /api/sync/send_admin_mail: admin invites and password resets, mailed for the scheduler.

The portal's one exception to mailing only addresses someone just proved they know, so these
pin how narrow it is: signed, two fixed templates, links to this site's own /admin pages only,
rate limited, and nothing about the address or link kept.

    pytest portal/test_admin_mail.py -v
"""
import email
import email.policy

import pytest

from test_portal_local import _conn, _signed, app_env, client  # noqa: F401  (fixtures)

SITE = "https://portal.test"     # PORTAL_PUBLIC_URL in the app_env fixture


def _mail(app_env):
    files = sorted((app_env[1] / "mail").glob("*.eml"), key=lambda p: p.stat().st_mtime)
    return [email.message_from_bytes(f.read_bytes(), policy=email.policy.default) for f in files]


def _send(client, op="op-1", **over):
    payload = {"op_id": op, "kind": "invite", "to": "new.admin@example.com",
               "link": f"{SITE}/admin/invite?t=TOKEN123", "inviter": "jim", "role": "checkin",
               "app_title": "BSidesCharm", "hours": 72}
    payload.update(over)
    return _signed(client, "POST", "/api/sync/send_admin_mail", payload)


def test_an_invite_is_mailed_from_the_fixed_template(app_env, client):
    r = _send(client)
    assert r.status_code == 200 and r.json() == {"ok": True, "kind": "invite"}
    (msg,) = _mail(app_env)
    assert msg["To"] == "new.admin@example.com"
    assert msg["Subject"] == "You're invited to help run BSidesCharm"
    body = msg.get_body(("plain",)).get_content()
    assert "jim has invited you to help run BSidesCharm as checkin" in body
    assert f"{SITE}/admin/invite?t=TOKEN123" in body and "72 hours" in body


def test_a_reset_is_mailed_from_its_template(app_env, client):
    r = _send(client, kind="reset", link=f"{SITE}/admin/reset?t=RST", username="dave", minutes=60)
    assert r.status_code == 200
    (msg,) = _mail(app_env)
    body = msg.get_body(("plain",)).get_content()
    assert msg["Subject"] == "Reset your BSidesCharm password"
    assert "account dave" in body and "/admin/reset?t=RST" in body and "60 minutes" in body


@pytest.mark.parametrize("over, why", [
    ({"kind": "volunteer"}, "Unknown kind"),
    ({"to": "not an address"}, "Bad address"),
    ({"to": "a@b.c\r\nBcc: x@y.z"}, "Bad address"),
    ({"link": "https://evil.example/admin/invite?t=X"}, "isn't this site's"),
    ({"link": f"{SITE}/apply/verify?t=X"}, "isn't this site's"),
    ({"link": f"{SITE}/admin/reset?t=X"}, "isn't this site's"),         # wrong page for kind
    ({"link": f"{SITE}/admin/invite?t=X\nclick evil.example"}, "isn't this site's"),
])
def test_anything_else_is_refused_and_nothing_is_sent(app_env, client, over, why):
    r = _send(client, **over)
    assert r.status_code == 400 and why in r.json()["detail"]
    assert _mail(app_env) == []


def test_it_needs_a_signature(client):
    r = client.post("/api/sync/send_admin_mail", json={"op_id": "x", "kind": "invite"})
    assert r.status_code == 401


def test_a_retried_request_does_not_send_twice(app_env, client):
    assert _send(client, op="same").status_code == 200
    assert _send(client, op="same").status_code == 200
    assert len(_mail(app_env)) == 1


def test_at_most_twenty_an_hour(app_env, client):
    for i in range(20):
        assert _send(client, op=f"n{i}").status_code == 200
    r = _send(client, op="n20")
    assert r.status_code == 429 and len(_mail(app_env)) == 20


def test_neither_the_address_nor_the_link_is_kept(app_env, client):
    _send(client)
    conn = _conn(app_env)
    audit = " ".join(f"{r['action']} {r['detail']}" for r in
                     conn.execute("SELECT action, detail FROM portal_audit").fetchall())
    ops = " ".join(r[0] for r in conn.execute("SELECT result FROM sync_ops").fetchall())
    conn.close()
    assert "admin_mail.sent invite" in audit
    for blob in (audit, ops):
        assert "@" not in blob and "TOKEN123" not in blob


ADMIN = "https://admin.portal.test"


@pytest.fixture
def split_env(monkeypatch, request):
    """The deployment's shape: the organizer site on its own hostname (ADMIN_PUBLIC_URL)."""
    monkeypatch.setenv("ADMIN_PUBLIC_URL", ADMIN)
    return request.getfixturevalue("app_env")


def test_with_a_separate_admin_host_links_must_point_there(split_env):
    client = split_env[0]
    assert _send(client, op="a", link=f"{ADMIN}/admin/invite?t=TOK").status_code == 200
    # The public portal host doesn't serve /admin, so a link there is refused.
    r = _send(client, op="b", link=f"{SITE}/admin/invite?t=TOK")
    assert r.status_code == 400 and "isn't this site's" in r.json()["detail"]
    assert len(_mail(split_env)) == 1


# ── volunteer signup invites ──────────────────────────────────────────────────

def _invite(client, op, **over):
    payload = {"op_id": op, "kind": "signup_invite", "to": "early.bird@example.com",
               "link": f"{SITE}/apply/signup?invite=TOK{op}", "first_name": "Early",
               "app_title": "BSidesCharm", "expires": "December 5, 2026"}
    payload.update(over)
    return _signed(client, "POST", "/api/sync/send_admin_mail", payload)


def test_a_signup_invite_is_mailed_from_its_template(app_env, client):
    r = _invite(client, "s1")
    assert r.status_code == 200 and r.json()["kind"] == "signup_invite"
    (msg,) = _mail(app_env)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert msg["To"] == "early.bird@example.com"
    assert "volunteer at BSidesCharm" in msg["Subject"]
    assert "Hi Early," in body and f"{SITE}/apply/signup?invite=TOKs1" in body
    assert "December 5, 2026" in body
    # Nothing in it names one particular conference: the title comes from the push, and
    # questions go back to whoever sent it.
    assert "BSidesCharm is completely volunteer-run" in body
    r = _invite(client, "s1b", app_title="ExampleCon", to="other.bird@example.com")
    other = next(m for m in _mail(app_env) if m["To"] == "other.bird@example.com")
    body = other.get_body(preferencelist=("plain",)).get_content()
    assert "ExampleCon is completely volunteer-run" in body and "bsidescharm" not in body.lower()
    assert "Just reply to this email" in body


@pytest.mark.parametrize("link", [
    f"{SITE}/admin/invite?t=X",                   # an admin link under the wrong kind
    "https://evil.example/apply/signup?invite=X",
    f"{SITE}/apply/signup?other=X",
])
def test_a_signup_invite_link_must_be_this_portals(app_env, client, link):
    r = _invite(client, f"bad-{len(link)}", link=link)
    assert r.status_code == 400 and "isn't this site's" in r.json()["detail"]
    assert _mail(app_env) == []


def test_signup_invites_have_their_own_hourly_allowance(app_env, client, monkeypatch):
    from routers import sync
    monkeypatch.setattr(sync, "SIGNUP_INVITE_MAIL_PER_HOUR", 3)
    for i in range(3):
        assert _invite(client, f"cap{i}").status_code == 200
    r = _invite(client, "cap3")
    assert r.status_code == 429 and "invite mail" in r.json()["detail"]
    # A batch of invites doesn't use up admin mail: an admin invite still goes.
    assert _send(client, op="admin-after").status_code == 200


def test_a_signup_invite_keeps_neither_address_nor_link(app_env, client):
    _invite(client, "priv")
    conn = _conn(app_env)
    audit = " ".join(f"{r['action']} {r['detail']}" for r in
                     conn.execute("SELECT action, detail FROM portal_audit").fetchall())
    ops = " ".join(r[0] for r in conn.execute("SELECT result FROM sync_ops").fetchall())
    conn.close()
    assert "admin_mail.sent signup_invite" in audit
    for blob in (audit, ops):
        assert "@" not in blob and "TOKpriv" not in blob


# ── security alerts (backend/alerts.py) ───────────────────────────────────────

def _alert(client, op, **over):
    payload = {"op_id": op, "kind": "alert", "to": "organizer@example.com",
               "link": f"{SITE}/admin/audit", "app_title": "BSidesCharm",
               "rule": "login.bruteforce.ip", "title": "Many failed sign-ins from one address",
               "severity": "high", "held_back": 0,
               "facts": [["Event", "auth.login.fail"], ["Client address", "203.0.113.9"],
                         ["Seen", "10 events in 15 minutes"]]}
    payload.update(over)
    return _signed(client, "POST", "/api/sync/send_admin_mail", payload)


def _body(msg):
    return msg.get_body(preferencelist=("plain",)).get_content()


def test_an_alert_is_mailed_from_its_template(app_env, client):
    r = _alert(client, "a1", held_back=7)
    assert r.status_code == 200 and r.json() == {"ok": True, "kind": "alert"}
    (msg,) = _mail(app_env)
    assert msg["Subject"] == "[BSidesCharm] Security alert: Many failed sign-ins from one address"
    body = _body(msg)
    assert "BSidesCharm: security alert (high)" in body
    assert "  Client address: 203.0.113.9" in body and "  Event:          auth.login.fail" in body
    assert "7 more matching events" in body and f"{SITE}/admin/audit" in body


@pytest.mark.parametrize("hostile, shown", [
    ("http://evil.example/login", "http[:]//evil.example/login"),
    ("see www.evil.example now", "see www[.]evil.example now"),
    ("javascript:alert(1)", "javascript[:]alert(1)"),
    ("bob\nClick here: hxxp\r\nBcc: x@y.z", "bob Click here: hxxp Bcc: x@y.z"),
    ("a" + chr(0x202E) + "evil" + chr(0x2028) + "b\x00c", "a evil b c"),
    ("{{ link }}", "{ { link } }"),
])
def test_alert_text_is_flattened_and_defanged(app_env, client, hostile, shown):
    r = _alert(client, f"h{abs(hash(hostile))}", facts=[["Username", hostile]], title=hostile)
    assert r.status_code == 200, r.text
    (msg,) = _mail(app_env)
    assert f"  Username: {shown}" in _body(msg)
    assert msg["Subject"].endswith(shown)
    assert "\n" not in msg["Subject"]


@pytest.mark.parametrize("over, why", [
    ({"rule": "Bad Rule!"}, "Bad alert rule"),
    ({"rule": None}, "Bad alert rule"),
    ({"severity": "urgent"}, "Bad alert severity"),
    ({"title": ""}, "Bad alert title"),
    ({"title": "\n\x00 "}, "Bad alert title"),
    ({"title": 5}, "Bad alert title"),
    ({"held_back": -1}, "Bad alert count"),
    ({"held_back": True}, "Bad alert count"),
    ({"facts": "x"}, "Bad alert facts"),
    ({"facts": [["only one"]]}, "Bad alert facts"),
    ({"facts": [["k", "v"]] * 11}, "Bad alert facts"),
    ({"link": f"{SITE}/admin/audit?next=https://evil.example"}, "isn't this site's"),
    ({"link": f"{SITE}/admin/auditx"}, "isn't this site's"),
    ({"link": f"{SITE}/admin/invite?t=X"}, "isn't this site's"),
])
def test_a_malformed_alert_is_refused_and_nothing_is_sent(app_env, client, over, why):
    r = _alert(client, "bad", **over)
    assert r.status_code == 400 and why in r.json()["detail"]
    assert _mail(app_env) == []


def test_alerts_have_their_own_hourly_allowance(app_env, client, monkeypatch):
    from routers import sync
    monkeypatch.setattr(sync, "ALERT_MAIL_PER_HOUR", 3)
    for i in range(3):
        assert _alert(client, f"q{i}").status_code == 200
    assert _alert(client, "q3").status_code == 429
    # Invites and resets aren't touched by the alerts' allowance.
    assert _send(client, op="still-ok").status_code == 200


def test_an_alert_keeps_neither_address_nor_details(app_env, client):
    _alert(client, "k1", facts=[["Username", "secret-name"]])
    conn = _conn(app_env)
    audit = " ".join(f"{r['action']} {r['detail']}" for r in
                     conn.execute("SELECT action, detail FROM portal_audit").fetchall())
    conn.close()
    assert "admin_mail.sent alert" in audit
    assert "@" not in audit and "secret-name" not in audit
