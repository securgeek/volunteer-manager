"""vol_mail end to end with no network: a real package (built by the backend's email_render),
a box with addresses encrypted to a throwaway key, and Graph faked at the HTTP layer.

    cd tools/vol_mail && pytest test_vol_mail.py -v

Needs cryptography and segno, plus markdown-it-py for building the package.
"""
import argparse
import base64
import hashlib
import io
import json
import os
import sys
import urllib.error
from email import message_from_bytes

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "backend"))

import vol_mail  # noqa: E402
import email_render  # noqa: E402
import checkin_codes  # noqa: E402

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
CODE_KEY = bytes(32)
SUBJECT = "Your {{event_title}} shifts, {{first_name}}"
BODY = "![Logo](asset:logo.png)\n\nHi {{first_name}} & co,\n\n{{shifts}}\n\nThanks"
SHIFT = "### {{day}} {{time}}\n**{{position}}** {{date}}\n\n{{qr}}"


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _enc(key, addr):
    return base64.b64encode(key.public_key().encrypt(
        addr.encode(), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                    algorithm=hashes.SHA256(), label=None))).decode()


def _shift(vol_id, slot, day="Saturday", time="0800-1200", pos="Security 1", date="2027-04-24"):
    return {"slot_key": slot, "day_key": slot.split("_")[0], "day": day, "date": date,
            "time_key": slot.split("_")[1], "time": time, "start": None, "end": None,
            "pos_key": slot.split("_")[2], "position": pos, "category": "Security",
            "checked_in": False, "checkin_code": checkin_codes.make_code(CODE_KEY, vol_id, slot)}


def _vol(vol_id, first, shifts, email_enc, handle=""):
    name = f"{handle} [{first} E.]" if handle else f"{first} E."
    return {"vol_id": vol_id, "name": name, "first_name": first, "last_initial": "E",
            "handle": handle, "status": "active", "cancelled": False, "inactive": False,
            "shifts": shifts, "email_enc": email_enc}


@pytest.fixture
def files(tmp_path, rsa_key):
    pkg = email_render.build_package(
        SUBJECT, BODY, SHIFT,
        [{"name": "logo.png", "content_type": "image/png", "data": PNG,
          "sha256": hashlib.sha256(PNG).hexdigest()}],
        {"created_at": "2027-04-01T00:00:00Z", "created_by": "alice", "event_title": "BSidesTest"})
    (tmp_path / "pkg.zip").write_bytes(pkg)
    box = {"generated_at": "2027-04-01T00:00:00Z", "event_title": "BSidesTest",
           "event_tz": "America/New_York", "volunteers": [
               _vol("1042", "Jim", [_shift("1042", "sat_0800_sec1"),
                                    _shift("1042", "sun_1300_sec2", "Sunday", "1300-1700", "Security 2",
                                           "2027-04-25")],
                    _enc(rsa_key, "jim@example.com"), handle="Zero Cool"),
               _vol("1043", "Mary", [_shift("1043", "sat_0800_sec2", pos="Security 2")],
                    _enc(rsa_key, "mary@example.com")),
               _vol("1044", "Noemail", [_shift("1044", "sat_1300_sec1")], None),
           ]}
    (tmp_path / "box.json").write_text(json.dumps(box))
    (tmp_path / "private.pem").write_bytes(rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp_path


SENDER = "volunteers@conference.test"


class FakeGraph:
    sender = SENDER

    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def sign_in(self):
        return "organizer@example.org"

    def send(self, msg, to):
        if to in self.fail_for:
            raise vol_mail.Fail("Graph sendMail failed: HTTP 500")
        self.sent.append((to, msg))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(vol_mail, "_sleep", lambda s: None)


def _args(files, *extra):
    return ["send", "--package", str(files / "pkg.zip"), "--box", str(files / "box.json"),
            "--key", str(files / "private.pem"), "--state", str(files / "state"), *extra]


def _send(files, graph, *extra, confirm="send"):
    out = []
    rc = vol_mail.cmd_send(_parse(_args(files, *extra)), graph_factory=lambda a, o: graph,
                           out=out.append, confirm=lambda prompt: confirm)
    return rc, "\n".join(out)


def _parse(argv):
    # Reuse main's parser by capturing the namespace it builds.
    captured = {}

    def fake_cmd(args, **kw):
        captured["args"] = args
        return 0
    real = (vol_mail.cmd_fetch, vol_mail.cmd_preview, vol_mail.cmd_send)
    vol_mail.cmd_fetch = vol_mail.cmd_preview = vol_mail.cmd_send = fake_cmd
    try:
        vol_mail.main(argv)
    finally:
        vol_mail.cmd_fetch, vol_mail.cmd_preview, vol_mail.cmd_send = real
    return captured["args"]


# ── the tool renders exactly what the site previews ──────────────────────────

def test_rendering_matches_the_backend(files):
    pkg = vol_mail.load_package(str(files / "pkg.zip"))
    box = vol_mail.load_box(str(files / "box.json"))
    parts = email_render.build_parts(SUBJECT, BODY, SHIFT)
    assert pkg["parts"] == parts
    for v in box["volunteers"]:
        src = lambda i, s: f"cid:qr-{i}"   # noqa: E731
        assert vol_mail.render_message(pkg["parts"], v, "BSidesTest", src) == \
            email_render.render_message(parts, v, "BSidesTest", src)
    for s in box["volunteers"][0]["shifts"]:
        assert vol_mail.qr_png(s["checkin_code"]) == email_render.qr_png(s["checkin_code"])


def test_message_content(files):
    pkg = vol_mail.load_package(str(files / "pkg.zip"))
    box = vol_mail.load_box(str(files / "box.json"))
    msg = vol_mail.build_message(pkg, box, box["volunteers"][0])
    assert msg["subject"] == "Your BSidesTest shifts, Jim"
    assert "Hi Jim &amp; co," in msg["html"]
    assert 'src="cid:qr-0"' in msg["html"] and 'src="cid:qr-1"' in msg["html"]
    assert 'src="cid:asset-logo.png"' in msg["html"] and "asset:" not in msg["html"].replace("cid:asset-", "")
    assert "April 25, 2027" in msg["html"] and "{{" not in msg["html"]
    assert [a["cid"] for a in msg["attachments"]] == ["qr-0", "qr-1", "asset-logo.png"]
    assert "Check-in code: BSC1.sat_0800_sec1." in msg["text"]


def test_package_checks(files, tmp_path):
    import zipfile
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(files / "pkg.zip") as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            dst.writestr(n, b"tampered" if n == "assets/logo.png" else data)
    with pytest.raises(vol_mail.Fail, match="checksum"):
        vol_mail.load_package(str(bad))
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    with pytest.raises(vol_mail.Fail, match="isn't a schedule email package"):
        vol_mail.load_package(str(tmp_path / "junk.zip"))


def test_box_without_scopes_is_refused(files, tmp_path):
    box = json.loads((files / "box.json").read_text())
    for v in box["volunteers"]:
        del v["email_enc"]
    (tmp_path / "noemail.json").write_text(json.dumps(box))
    with pytest.raises(vol_mail.Fail, match="volunteers:email"):
        vol_mail.load_box(str(tmp_path / "noemail.json"))


# ── preview ───────────────────────────────────────────────────────────────────

def test_preview_writes_eml_html_and_text(files):
    out = files / "preview"
    rc = vol_mail.main(["preview", "--package", str(files / "pkg.zip"), "--box", str(files / "box.json"),
                        "--vol", "1042", "--out", str(out)])
    assert rc == 0
    m = message_from_bytes((out / "1042.eml").read_bytes())
    assert m["To"] == "preview@example.invalid"           # never a real address
    assert m.get_content_type() == "multipart/alternative"
    types = [p.get_content_type() for p in m.walk()]
    assert "text/plain" in types and "text/html" in types and types.count("image/png") == 3
    page = (out / "1042.html").read_text()
    assert "cid:" not in page and page.count("data:image/png;base64,") == 3
    assert "jim@example.com" not in (out / "1042.eml").read_bytes().decode(errors="ignore")


# ── send ──────────────────────────────────────────────────────────────────────

def test_send_needs_a_dry_run_first(files):
    with pytest.raises(vol_mail.Fail, match="--dry-run first"):
        _send(files, FakeGraph(), "--round", "draft")


def test_draft_then_resume(files):
    rc, out = _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    assert rc == 0 and "2 to send" in out and "skip 1044: no email on file" in out
    plan = json.loads((files / "state" / "draft.plan.json").read_text())
    assert [r["vol_id"] for r in plan["recipients"]] == ["1042", "1043"]
    state_text = (files / "state" / "draft.plan.json").read_text()
    assert "example.com" not in state_text
    assert oct(os.stat(files / "state" / "draft.plan.json").st_mode & 0o777) == "0o600"

    # The first send fails for Mary; Jim goes out.
    g = FakeGraph(fail_for={"mary@example.com"})
    rc, out = _send(files, g, "--round", "draft")
    assert rc == 1 and [t for t, _ in g.sent] == ["jim@example.com"]
    assert "FAILED 1043" in out and "example.com" not in out
    # Running it again sends only what's left.
    g2 = FakeGraph()
    rc, out = _send(files, g2, "--round", "draft")
    assert rc == 0 and [t for t, _ in g2.sent] == ["mary@example.com"]
    rc, out = _send(files, FakeGraph(), "--round", "draft")
    assert "Nothing to do" in out
    assert "example.com" not in (files / "state" / "draft.sent.jsonl").read_text()


def test_cancelling_at_the_prompt_sends_nothing(files):
    _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    g = FakeGraph()
    rc, out = _send(files, g, "--round", "draft", confirm="no")
    assert rc == 1 and not g.sent and "nothing sent" in out


def test_changed_box_or_package_needs_a_new_dry_run(files):
    _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    box = json.loads((files / "box.json").read_text())
    box["volunteers"][0]["shifts"][0]["position"] = "Security 9"
    (files / "box.json").write_text(json.dumps(box))
    with pytest.raises(vol_mail.Fail, match="box changed"):
        _send(files, FakeGraph(), "--round", "draft")


def test_final_only_to_changed(files):
    _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    _send(files, FakeGraph(), "--round", "draft")
    # Adjustments: Mary moves to another position; Jim is unchanged.
    box = json.loads((files / "box.json").read_text())
    box["volunteers"][1]["shifts"][0]["position"] = "Registration 4"
    (files / "box.json").write_text(json.dumps(box))
    rc, out = _send(files, FakeGraph(), "--round", "final", "--only-changed-since", "draft", "--dry-run")
    assert "1 to send" in out and "skip 1042: unchanged since draft" in out
    g = FakeGraph()
    _send(files, g, "--round", "final")
    assert [t for t, _ in g.sent] == ["mary@example.com"]
    assert "Registration 4" in g.sent[0][1]["html"]


def test_only_list(files):
    (files / "ids.txt").write_text("# just me\n1043\n9999\n")
    rc, out = _send(files, FakeGraph(), "--round", "test", "--only", str(files / "ids.txt"), "--dry-run")
    assert "1 to send" in out and "skip 9999: not in the box" in out


def test_wrong_private_key_is_caught_in_the_dry_run(files, tmp_path):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (files / "private.pem").write_bytes(other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    rc, out = _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    assert rc == 1 and "0 to send" in out and "doesn't decrypt" in out


def test_rate_limit_spacing(files, monkeypatch):
    waits = []
    monkeypatch.setattr(vol_mail, "_sleep", waits.append)
    _send(files, FakeGraph(), "--round", "draft", "--dry-run")
    _send(files, FakeGraph(), "--round", "draft", "--per-minute", "20")
    assert waits == [3.0]          # between the two sends, none after the last


# ── the Graph client, against a fake HTTP layer ───────────────────────────────

class FakeHTTP:
    """Scripted responses for urlopen; records every request."""

    def __init__(self, script):
        self.script, self.requests = list(script), []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        status, body, headers = self.script.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err", headers or {},
                                         io.BytesIO(json.dumps(body).encode()))
        resp = io.BytesIO(json.dumps(body).encode() if body is not None else b"")
        resp.__enter__ = lambda: resp
        resp.__exit__ = lambda *a: False
        return _Ctx(resp)


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self.resp

    def __exit__(self, *a):
        return False


def _id_token(user):
    claims = base64.urlsafe_b64encode(json.dumps({"preferred_username": user}).encode()).decode().rstrip("=")
    return f"h.{claims}.s"


def test_graph_sign_in_and_send(files, monkeypatch):
    http = FakeHTTP([
        (200, {"device_code": "dc", "message": "Go to https://microsoft.com/devicelogin", "interval": 1}, None),
        (400, {"error": "authorization_pending"}, None),
        (200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600,
               "id_token": _id_token("organizer@example.org")}, None),
        (429, {"error": {"code": "TooManyRequests"}}, {"Retry-After": "7"}),
        (202, None, None),
    ])
    monkeypatch.setattr(vol_mail, "_urlopen", http)
    waits = []
    monkeypatch.setattr(vol_mail, "_sleep", waits.append)
    out = []
    g = vol_mail.Graph("tenant", "client", "organizer@example.org", SENDER, out.append)
    assert g.sign_in() == "organizer@example.org"
    pkg = vol_mail.load_package(str(files / "pkg.zip"))
    box = vol_mail.load_box(str(files / "box.json"))
    g.send(vol_mail.build_message(pkg, box, box["volunteers"][0]), "jim@example.com")
    assert 7 in waits
    req = http.requests[-1]
    assert req.full_url.endswith("/me/sendMail") and req.headers["Authorization"] == "Bearer AT"
    payload = json.loads(req.data)
    m = payload["message"]
    assert m["from"]["emailAddress"]["address"] == SENDER
    assert m["body"]["contentType"] == "HTML"
    assert m["toRecipients"] == [{"emailAddress": {"address": "jim@example.com"}}]
    att = m["attachments"]
    assert [a["contentId"] for a in att] == ["qr-0", "qr-1", "asset-logo.png"]
    assert all(a["isInline"] and a["@odata.type"] == "#microsoft.graph.fileAttachment" for a in att)
    assert base64.b64decode(att[2]["contentBytes"]) == PNG
    # Nothing about the sign-in was written anywhere.
    assert not any(p.name not in ("pkg.zip", "box.json", "private.pem") for p in files.iterdir())


def test_graph_refuses_the_wrong_account(monkeypatch):
    http = FakeHTTP([
        (200, {"device_code": "dc", "message": "m", "interval": 1}, None),
        (200, {"access_token": "AT", "id_token": _id_token("someone@else.com")}, None),
    ])
    monkeypatch.setattr(vol_mail, "_urlopen", http)
    g = vol_mail.Graph("t", "c", "organizer@example.org", SENDER, lambda *_: None)
    with pytest.raises(vol_mail.Fail, match="someone@else.com"):
        g.sign_in()


# ── fetch ─────────────────────────────────────────────────────────────────────

def test_fetch(files, monkeypatch, tmp_path):
    box = json.loads((files / "box.json").read_text())
    http = FakeHTTP([(200, box, None)])
    monkeypatch.setattr(vol_mail, "_urlopen", http)
    (tmp_path / "key.txt").write_text("bsc_secret\n")
    out = tmp_path / "fetched.json"
    rc = vol_mail.main(["fetch", "--url", "https://sched.example", "--key-file",
                        str(tmp_path / "key.txt"), "--out", str(out)])
    assert rc == 0
    req = http.requests[0]
    assert req.full_url == "https://sched.example/api/export/volunteers?status=active&assigned=1"
    assert req.headers["Authorization"] == "Bearer bsc_secret"
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert vol_mail.main(["fetch", "--url", "http://sched.example", "--key-file",
                          str(tmp_path / "key.txt"), "--out", str(out)]) == 2


def test_fetch_explains_a_401(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(vol_mail, "_urlopen", FakeHTTP([(401, {"detail": "Unauthorized"}, None)]))
    (tmp_path / "key.txt").write_text("bsc_x")
    rc = vol_mail.main(["fetch", "--url", "https://s.example", "--key-file", str(tmp_path / "key.txt"),
                        "--out", str(tmp_path / "b.json")])
    assert rc == 2 and "revoked key" in capsys.readouterr().err


def test_send_needs_a_from_address(monkeypatch):
    # No built-in sender: without MAIL_SENDER or --from, send stops before signing in.
    monkeypatch.setenv("GRAPH_TENANT_ID", "t")
    monkeypatch.setenv("GRAPH_CLIENT_ID", "c")
    monkeypatch.setenv("VOL_MAIL_MAILBOX", "organizer@example.org")
    with pytest.raises(vol_mail.Fail, match="MAIL_SENDER"):
        vol_mail._graph_from_env(argparse.Namespace(sender=""), print)
    graph = vol_mail._graph_from_env(argparse.Namespace(sender=SENDER), print)
    assert graph.sender == SENDER
