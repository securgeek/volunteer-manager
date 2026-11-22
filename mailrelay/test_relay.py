"""Relay tests. No network: Graph and the identity platform are replaced by a fake urlopen,
and the HTTP tests bind to loopback only.

    pytest test_relay.py -v

What is NOT covered here: a real sign-in or send, and the compose network isolation.
"""
import base64
import http.client
import io
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.parse

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import relay  # noqa: E402

TOKEN = "t" * 40
MAILBOX = "jim@bsides.test"
SENDER = "volunteers@conference.test"
GOOD = {"to": "vol@example.com", "subject": "Sign in", "body": "Your link:\nhttps://x.test/v?t=1"}


class _FakeResp:
    def __init__(self, body=b""):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _id_token(user):
    claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": user}).encode())
    return f"e30.{claims.decode().rstrip('=')}.sig"


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-1")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-1")
    monkeypatch.setenv("MAIL_RELAY_TOKEN", TOKEN)
    monkeypatch.setenv("RELAY_MAILBOX", MAILBOX)
    monkeypatch.setenv("MAIL_SENDER", SENDER)
    monkeypatch.setenv("RELAY_STATE_DIR", str(tmp_path))
    return relay.Config()


@pytest.fixture
def signed_in(cfg):
    relay.TokenStore(cfg).write("rt-0")
    return cfg


@pytest.fixture
def graph_calls(monkeypatch):
    """Record every outbound request and answer it the way Microsoft would.

    Set .send_status or .refresh_error to make a step fail, and .device to script the
    device-code polling: each entry is an error code string ("authorization_pending",
    "slow_down", ...) or a token response dict."""
    class Calls(list):
        send_status = 202
        refresh_error = None
        refreshes = 0

    calls = Calls()
    calls.device = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        url = req.full_url
        if url.endswith("/devicecode"):
            return _FakeResp(json.dumps({
                "device_code": "dc-1", "user_code": "ABCD-EFGH", "interval": 5,
                "expires_in": 900,
                "message": "To sign in, open https://microsoft.com/devicelogin and enter ABCD-EFGH",
            }).encode())
        if url.endswith("/token"):
            form = urllib.parse.parse_qs(req.data.decode())
            if form["grant_type"] == ["refresh_token"]:
                if calls.refresh_error:
                    raise _http_error(url, 400, {
                        "error": calls.refresh_error,
                        "error_description": "AADSTS700082: The refresh token has expired.\r\nTrace ID: x"})
                calls.refreshes += 1
                return _FakeResp(json.dumps({
                    "access_token": f"tok-{calls.refreshes}",
                    "refresh_token": f"rt-{calls.refreshes}",
                    "expires_in": 3599}).encode())
            step = calls.device.pop(0)
            if isinstance(step, str):
                raise _http_error(url, 400, {"error": step})
            return _FakeResp(json.dumps(step).encode())
        if calls.send_status != 202:
            raise _http_error(url, calls.send_status,
                              {"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}})
        return _FakeResp()

    monkeypatch.setattr(relay, "_urlopen", fake_urlopen)
    return calls


def _send(r, payload=None, token=TOKEN):
    payload = GOOD if payload is None else payload
    return r.send(f"Bearer {token}", json.dumps(payload).encode())


# ── configuration ─────────────────────────────────────────────────────────────

def test_refuses_to_start_without_its_settings(monkeypatch):
    for name in ("GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "MAIL_RELAY_TOKEN", "RELAY_MAILBOX",
                 "MAIL_SENDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRAPH_TENANT_ID", "tenant-1")
    with pytest.raises(relay.ConfigError,
                       match="GRAPH_CLIENT_ID, MAIL_RELAY_TOKEN, RELAY_MAILBOX, MAIL_SENDER"):
        relay.Config()


@pytest.mark.parametrize("sender", ["Volunteers <v@example.org>", "a@example.org, b@example.org",
                                    "v@example.org\nBcc: x@example.org", "not-an-address"])
def test_refuses_a_sender_that_isnt_one_plain_address(cfg, monkeypatch, sender):
    monkeypatch.setenv("MAIL_SENDER", sender)
    with pytest.raises(relay.ConfigError, match="MAIL_SENDER"):
        relay.Config()


def test_refuses_a_short_token(cfg, monkeypatch):
    monkeypatch.setenv("MAIL_RELAY_TOKEN", "short")
    with pytest.raises(relay.ConfigError, match="at least 32"):
        relay.Config()


# ── authentication ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("header", ["", "Bearer", f"Basic {TOKEN}", "Bearer wrong", f"bearer {TOKEN}"])
def test_rejects_a_missing_or_wrong_token(signed_in, graph_calls, header):
    status, _ = relay.Relay(signed_in).send(header, json.dumps(GOOD).encode())
    assert status == 401 and graph_calls == []


# ── what a caller may send ────────────────────────────────────────────────────

@pytest.mark.parametrize("payload, reason", [
    ({**GOOD, "from": "ceo@example.org"}, "unexpected field(s): from"),
    ({**GOOD, "html": "<b>x</b>"}, "unexpected field(s): html"),
    ({**GOOD, "headers": {"Bcc": "x@y.z"}}, "unexpected field(s): headers"),
    ({"to": "vol@example.com", "subject": "x"}, "required strings"),
    ({**GOOD, "to": ["a@example.com"]}, "required strings"),
    ({**GOOD, "to": "a@example.com, b@example.com"}, "single plain email"),
    ({**GOOD, "to": "a@example.com;b@example.com"}, "single plain email"),
    ({**GOOD, "to": "Jim <a@example.com>"}, "single plain email"),
    ({**GOOD, "to": "a@example.com\r\nBcc: b@example.com"}, "single plain email"),
    ({**GOOD, "to": "not-an-address"}, "single plain email"),
    ({**GOOD, "subject": "Hi\r\nBcc: b@example.com"}, "one line"),
    ({**GOOD, "subject": ""}, "one line"),
    ({**GOOD, "subject": "x" * 201}, "one line"),
    ({**GOOD, "body": ""}, "plain text"),
    ({**GOOD, "body": "x" * 20_001}, "plain text"),
    ({**GOOD, "body": "nul\x00byte"}, "plain text"),
    (["not", "an", "object"], "JSON object"),
])
def test_rejects_anything_but_one_plain_text_message(signed_in, graph_calls, payload, reason):
    status, body = _send(relay.Relay(signed_in), payload)
    assert status == 400 and reason in body["error"]
    assert graph_calls == []


def test_rejects_malformed_json(signed_in, graph_calls):
    status, body = relay.Relay(signed_in).send(f"Bearer {TOKEN}", b"{not json")
    assert status == 400 and body["error"] == "body must be JSON"


# ── the Graph request ─────────────────────────────────────────────────────────

def test_sends_plain_text_as_the_fixed_sender_from_the_signed_in_mailbox(signed_in, graph_calls):
    status, body = _send(relay.Relay(signed_in))
    assert status == 202 and body["sent"] is True and body["id"]

    token_req, send_req = graph_calls
    assert token_req.full_url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    form = urllib.parse.parse_qs(token_req.data.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["rt-0"]
    assert form["client_id"] == ["client-1"] and "client_secret" not in form
    assert "https://graph.microsoft.com/Mail.Send" in form["scope"][0]

    assert send_req.full_url == "https://graph.microsoft.com/v1.0/me/sendMail"
    assert send_req.get_header("Authorization") == "Bearer tok-1"
    assert json.loads(send_req.data) == {
        "message": {
            "from": {"emailAddress": {"address": SENDER}},
            "subject": GOOD["subject"],
            "body": {"contentType": "Text", "content": GOOD["body"]},
            "toRecipients": [{"emailAddress": {"address": GOOD["to"]}}],
        },
        "saveToSentItems": True,
    }


def test_access_token_is_reused_until_it_nears_expiry(signed_in, graph_calls):
    r = relay.Relay(signed_in)
    _send(r)
    _send(r)
    assert graph_calls.refreshes == 1
    r.graph.expires = 0  # as if the hour had passed
    _send(r)
    assert graph_calls.refreshes == 2


def test_each_new_refresh_token_is_saved_owner_only(signed_in, graph_calls):
    r = relay.Relay(signed_in)
    _send(r)
    store = relay.TokenStore(signed_in)
    assert store.read() == "rt-1"
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    r.graph.expires = 0
    _send(r)
    form = urllib.parse.parse_qs(graph_calls[-2].data.decode())
    assert form["refresh_token"] == ["rt-1"]  # the rotated token was the one used
    assert store.read() == "rt-2"


def test_a_401_from_sendmail_drops_the_cached_access_token(signed_in, graph_calls):
    r = relay.Relay(signed_in)
    graph_calls.send_status = 401
    assert _send(r)[0] == 502
    graph_calls.send_status = 202
    assert _send(r)[0] == 202
    assert graph_calls.refreshes == 2


def test_graph_errors_name_the_cause_but_not_the_recipient(signed_in, graph_calls, caplog):
    graph_calls.send_status = 403
    status, body = _send(relay.Relay(signed_in), {**GOOD, "to": "secret.person@example.com"})
    assert status == 502 and "ErrorAccessDenied" in body["error"]
    assert "secret.person" not in body["error"]
    assert "secret.person" not in caplog.text


def test_success_log_carries_an_id_not_the_address(signed_in, graph_calls, caplog):
    caplog.set_level("INFO", logger="mailrelay")
    _send(relay.Relay(signed_in), {**GOOD, "to": "secret.person@example.com"})
    assert "ok" in caplog.text and "secret.person" not in caplog.text


# ── sign-in state ─────────────────────────────────────────────────────────────

def test_not_signed_in_is_a_503_that_says_how_to_fix_it(cfg, graph_calls):
    status, body = _send(relay.Relay(cfg))
    assert status == 503 and "relay.py login" in body["error"]
    assert graph_calls == []


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required"])
def test_an_expired_sign_in_is_a_503_that_says_how_to_fix_it(signed_in, graph_calls, error):
    graph_calls.refresh_error = error
    status, body = _send(relay.Relay(signed_in))
    assert status == 503 and "sign-in expired" in body["error"] and "relay.py login" in body["error"]
    assert "AADSTS700082" in body["error"] and "Trace ID" not in body["error"]


def test_other_token_errors_are_a_502(signed_in, graph_calls):
    graph_calls.refresh_error = "invalid_client"
    status, body = _send(relay.Relay(signed_in))
    assert status == 502 and "token refresh failed" in body["error"]


def test_a_login_in_another_process_takes_effect_without_a_restart(cfg, graph_calls):
    r = relay.Relay(cfg)
    assert _send(r)[0] == 503
    relay.TokenStore(cfg).write("rt-0")  # what `relay.py login` does
    assert _send(r)[0] == 202


# ── login ─────────────────────────────────────────────────────────────────────

def _login(cfg):
    printed = []
    user = relay.login(cfg, out=printed.append, sleep=lambda s: None)
    return user, printed


def test_login_polls_until_approved_then_saves_the_refresh_token(cfg, graph_calls):
    graph_calls.device = ["authorization_pending", "slow_down",
                          {"access_token": "a", "refresh_token": "rt-login",
                           "id_token": _id_token("Jim@Bsides.test")}]
    user, printed = _login(cfg)
    assert user == "Jim@Bsides.test"  # matched case-insensitively
    assert "ABCD-EFGH" in printed[0]
    assert relay.TokenStore(cfg).read() == "rt-login"

    dc_req = graph_calls[0]
    assert dc_req.full_url == "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/devicecode"
    scope = urllib.parse.parse_qs(dc_req.data.decode())["scope"][0]
    assert set(scope.split()) == {"https://graph.microsoft.com/Mail.Send", "offline_access",
                                  "openid", "profile"}
    poll = urllib.parse.parse_qs(graph_calls[1].data.decode())
    assert poll["grant_type"] == [relay.DEVICE_CODE_GRANT] and poll["device_code"] == ["dc-1"]


def test_login_as_the_wrong_account_saves_nothing(cfg, graph_calls):
    graph_calls.device = [{"access_token": "a", "refresh_token": "rt-x",
                           "id_token": _id_token("someone.else@outlook.com")}]
    with pytest.raises(relay.MailError, match="signed in as someone.else@outlook.com"):
        _login(cfg)
    assert relay.TokenStore(cfg).read() is None


def test_login_without_a_refresh_token_says_why(cfg, graph_calls):
    graph_calls.device = [{"access_token": "a", "id_token": _id_token(MAILBOX)}]
    with pytest.raises(relay.MailError, match="offline_access"):
        _login(cfg)


def test_login_reports_a_declined_sign_in(cfg, graph_calls):
    graph_calls.device = ["authorization_declined"]
    with pytest.raises(relay.MailError, match="authorization_declined"):
        _login(cfg)


def test_login_gives_up_when_the_code_expires(cfg, graph_calls):
    now = [0.0]

    def sleep(s):
        now[0] += s

    graph_calls.device = ["authorization_pending"] * 1000
    with pytest.raises(relay.MailError, match="expired"):
        relay.login(cfg, out=lambda m: None, sleep=sleep, clock=lambda: now[0])


# ── rate limit ────────────────────────────────────────────────────────────────

def test_rate_limit_per_minute_and_hour(signed_in, graph_calls):
    now = [1000.0]
    r = relay.Relay(signed_in, limiter=relay.RateLimiter(per_min=2, per_hour=3),
                    clock=lambda: now[0])
    assert _send(r)[0] == 202
    assert _send(r)[0] == 202
    assert _send(r)[0] == 429          # third in the same minute
    now[0] += 61
    assert _send(r)[0] == 202          # new minute, third of the hour
    now[0] += 61
    assert _send(r)[0] == 429          # hourly cap
    now[0] += 3600
    assert _send(r)[0] == 202


def test_rejected_requests_do_not_spend_the_rate_limit(signed_in, graph_calls):
    r = relay.Relay(signed_in, limiter=relay.RateLimiter(per_min=1, per_hour=10))
    assert _send(r, token="wrong")[0] == 401
    assert _send(r, {**GOOD, "to": "bad"})[0] == 400
    assert _send(r)[0] == 202


# ── over HTTP ─────────────────────────────────────────────────────────────────

@pytest.fixture
def server(signed_in, graph_calls):
    srv = relay.make_server(relay.Relay(signed_in), host="127.0.0.1", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _http(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, json.loads(data) if data else None


def test_http_health_reports_sign_in_state(server, signed_in):
    assert _http(server, "GET", "/health") == (200, {"ok": True, "signed_in": True})
    os.remove(relay.TokenStore(signed_in).path)
    assert _http(server, "GET", "/health") == (200, {"ok": True, "signed_in": False})


def test_http_send(server, graph_calls):
    status, body = _http(server, "POST", "/send", json.dumps(GOOD),
                         {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    assert status == 202 and body["sent"] is True
    assert len(graph_calls) == 2


def test_http_rejects_other_content_types_and_paths(server, graph_calls):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert _http(server, "POST", "/send", json.dumps(GOOD),
                 {**auth, "Content-Type": "text/plain"})[0] == 415
    assert _http(server, "POST", "/other", json.dumps(GOOD),
                 {**auth, "Content-Type": "application/json"})[0] == 404
    assert _http(server, "GET", "/send")[0] == 404
    assert graph_calls == []


def test_http_rejects_an_oversized_request(server, graph_calls):
    big = json.dumps({**GOOD, "body": "x" * (relay.MAX_REQUEST_BYTES + 1)})
    status, _ = _http(server, "POST", "/send", big,
                      {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
    assert status == 413 and graph_calls == []
