"""The audit log (`audit.py`): record shape, injection safety, and the event registry.

No database: `audit()` only needs a request and a file.

    cd backend && pytest test_audit.py -v
"""
import ast
import json
import os
from datetime import datetime

import pytest
from starlette.requests import Request

import audit
from db import _session_cache

_HERE = os.path.dirname(os.path.abspath(__file__))


def _request(path="/api/login", method="POST", headers=None, client=("10.0.0.5", 1234)):
    """A bare Starlette request, as a route would see it."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": path, "headers": raw,
                    "query_string": b"", "client": client})


@pytest.fixture
def log_file(tmp_path):
    """Point the audit log at a temporary file for one test; turn it off again after."""
    path = tmp_path / "audit.jsonl"
    audit.configure(str(path))
    yield path
    audit.configure(None)


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


# ── what a line holds ─────────────────────────────────────────────────────────

def test_a_line_is_one_json_object_with_the_expected_fields(log_file):
    audit.audit("auth.login.fail", _request(), None, "Invalid login attempt", username="bob")
    (line,) = _lines(log_file)
    rec = json.loads(line)
    assert set(rec) == {"ts", "event", "severity", "source", "actor", "user_id", "ip", "method", "path",
                        "message", "data"}
    assert rec["event"] == "auth.login.fail" and rec["severity"] == "low"
    assert rec["ts"].endswith("Z") and "T" in rec["ts"]
    assert rec["method"] == "POST" and rec["path"] == "/api/login"
    assert rec["actor"] is None and rec["data"] == {"username": "bob"}


def test_the_session_names_the_actor():
    _session_cache["sid-1"] = {"user_id": "u-1", "username": "alice", "role": "organizer"}
    try:
        rec = audit.build_record("user.create", _request(), "sid-1", "Added user")
    finally:
        _session_cache.pop("sid-1")
    assert rec["actor"] == "alice" and rec["user_id"] == "u-1"


def test_who_wins_over_the_session():
    _session_cache["sid-2"] = {"user_id": "u-2", "username": "alice", "role": "organizer"}
    try:
        rec = audit.build_record("api_key.use", _request(), "sid-2", "GET", who="API key 'x' (t1)")
    finally:
        _session_cache.pop("sid-2")
    assert rec["actor"] == "API key 'x' (t1)" and rec["user_id"] is None


def test_the_ip_is_nginx_forwarded_address_else_the_socket_peer():
    fwd = _request(headers={"X-Forwarded-For": "203.0.113.9"})
    assert audit.build_record("auth.logout", fwd, None, "x")["ip"] == "203.0.113.9"
    assert audit.build_record("auth.logout", _request(), None, "x")["ip"] == "10.0.0.5"
    assert audit.build_record("auth.logout", _request(client=None), None, "x")["ip"] == "unknown"


# ── injection: text a caller controls can't forge a line ──────────────────────

@pytest.mark.parametrize("hostile", [
    "bob\n2026-01-01 00:00:00 - admin(1) from 127.0.0.1: Deleted user 'alice'",
    'bob", "event": "auth.login.ok',
    "bob\r\nX: y",
    "bob\x00\x1b[2J\u2028\u2029\x85\u202e",
])
def test_hostile_text_stays_inside_its_field(log_file, hostile):
    audit.audit("auth.login.fail", _request(headers={"User-Agent": hostile}), None,
                f"Invalid login attempt for {hostile}", username=hostile)
    lines = _lines(log_file)
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "auth.login.fail"
    assert rec["data"]["username"] == hostile
    assert rec["message"] == f"Invalid login attempt for {hostile}"
    assert lines[0].isascii()       # nothing a terminal or a line splitter would act on


def test_non_ascii_names_survive_the_round_trip(log_file):
    audit.audit("volunteer.add", _request(), None, "Added volunteer", name="Nguyễn María-José")
    assert json.loads(_lines(log_file)[0])["data"]["name"] == "Nguyễn María-José"


def test_long_text_is_truncated_and_marked():
    rec = audit.build_record("volunteer.note.add", _request(), None, "x" * 5000, note="y" * 5000)
    assert len(rec["message"]) < 2100 and rec["message"].endswith("[truncated]")
    assert rec["data"]["note"].endswith("[truncated]")


def test_values_that_are_not_json_are_written_as_text(log_file):
    audit.audit("api_key.create", _request(), None, "Created", expires_at=datetime(2026, 12, 1))
    assert json.loads(_lines(log_file)[0])["data"]["expires_at"] == "2026-12-01 00:00:00"


# ── never the reason a request fails ──────────────────────────────────────────

def test_off_when_no_path_is_configured(tmp_path):
    audit.configure(None)
    audit.audit("auth.logout", _request(), None, "Signed out")   # nothing to assert: no raise
    assert not list(tmp_path.iterdir())


def test_an_unwritable_log_is_logged_not_raised(tmp_path, caplog):
    audit.configure(str(tmp_path / "no-such-dir" / "audit.jsonl"))
    try:
        audit.audit("auth.logout", _request(), None, "Signed out")
    finally:
        audit.configure(None)
    assert "could not write auth.logout" in caplog.text


def test_an_unregistered_code_is_still_recorded_and_flagged(log_file, caplog):
    audit.audit("no.such.event", _request(), None, "Something")
    rec = json.loads(_lines(log_file)[0])
    assert rec["unregistered"] is True and rec["event"] == "no.such.event"
    assert "unregistered event code" in caplog.text


# ── the registry ──────────────────────────────────────────────────────────────

def test_every_registered_event_has_a_known_severity_and_a_description():
    for code, (severity, description) in audit.EVENTS.items():  # Event unpacks like a tuple
        assert severity in audit.SEVERITIES, code
        assert description, code
        assert code == code.lower() and " " not in code, code


def _audit_calls():
    """Every `audit(...)` call in the backend's own code (tests excluded), as
    (file, line, first argument node)."""
    for root, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "migrations")]
        for name in files:
            if not name.endswith(".py") or name.startswith("test_") or name == "conftest.py":
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if called == "audit":
                    yield os.path.relpath(path, _HERE), node.lineno, (node.args or [None])[0]


def test_every_audit_call_names_a_registered_code_as_a_literal():
    calls = list(_audit_calls())
    assert len(calls) > 80          # the scan found the routers, not nothing
    bad = []
    for path, line, first in calls:
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            bad.append(f"{path}:{line}: event code is not a string literal")
        elif first.value not in audit.EVENTS:
            bad.append(f"{path}:{line}: unregistered event code {first.value!r}")
    assert not bad, "\n".join(bad)


# ── the audit_events copy, without a database ─────────────────────────────────

def test_no_database_is_logged_and_the_file_still_written(log_file, monkeypatch, caplog):
    def no_pool():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(audit.db, "get_conn", no_pool)
    audit.configure(str(log_file), to_db=True)
    audit.audit("auth.logout", _request(), None, "Signed out")      # no raise
    assert len(_lines(log_file)) == 1
    assert "no database connection for auth.logout" in caplog.text


def test_nothing_is_built_when_both_outputs_are_off(monkeypatch):
    audit.configure(None)
    monkeypatch.setattr(audit, "build_record", lambda *a, **k: pytest.fail("built a record"))
    audit.audit("auth.logout", _request(), None, "Signed out")


@pytest.mark.parametrize("raw, days", [
    (None, 365), ("30", 30), ("0", 0), (" 90 ", 90), ("-1", 365), ("forever", 365), ("", 365),
])
def test_retention_days_reads_the_environment(monkeypatch, raw, days):
    if raw is None:
        monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("AUDIT_RETENTION_DAYS", raw)
    assert audit.retention_days() == days


def test_no_nul_reaches_through_nested_values():
    assert audit._no_nul({"a\x00": ["x\x00", {"b": "\x00"}], "n": 3}) == {
        "a\ufffd": ["x\ufffd", {"b": "\ufffd"}], "n": 3}
