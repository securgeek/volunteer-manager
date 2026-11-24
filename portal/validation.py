"""Strict input rules for the public portal, and the anomalies that break them.

The volunteer pages (frontend/src/lib/portal_rules.js) check every field before sending, by
the same rules, and the two are held together by one list of cases,
`test_data/validation_cases.json`, which both test suites run. So anything that reaches this
app breaking a rule here didn't come through the pages: it's an **anomaly**. Two kinds of
refusal follow from that:

* an **ordinary mistake** (a required field left empty, two shifts that overlap, a shift that
  filled up meanwhile) gets the helpful message it always did;
* an **anomaly** (a wrong type, an unknown field, a control character, a name with digits, an
  over-long value, a malformed email or token or shift key) is recorded as a `portal.anomaly`
  security event, which the scheduler pulls and alerts on, and then — in `enforce` mode —
  refused with a bare `400 Malformed request` that never says which check failed.

`PORTAL_STRICT` picks the mode: `report` (the default) records the anomaly and carries on
with the old forgiving behaviour, so a rule that turns out too strict costs a log line, not a
volunteer; `enforce` refuses. Run `report` until the audit log shows no anomalies from real
volunteers, then switch.

Anomalies are written at most once a minute per client, rule and path; the rest are counted
and the next row carries the count, so a flood can't fill the table.

Normalising is never an anomaly: NFC, trimming, collapsing runs of spaces (including
no-break spaces) and turning \\r\\n into \\n all happen here and in the pages alike.
"""
import contextvars
import json
import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict

from fastapi import HTTPException, Request

import clock
import db
import ratelimit

log = logging.getLogger("validation")
# The clock. A name of its own so tests can move it without touching time.monotonic itself,
# which asyncio's loop runs on too.
_monotonic = time.monotonic

NAME_MAX = 60
HANDLE_MAX = 40
EXTRAS_MAX = 20
EMAIL_MAX = 254
EMAIL_LOCAL_MAX = 64
JSON_FIELD_MAX = 32_000
JSON_DEPTH_MAX = 4

_NAME_PUNCT = frozenset(" '\N{RIGHT SINGLE QUOTATION MARK}-.")
_HANDLE_BANNED = frozenset("<>{}`")
_ZWJ = "\N{ZERO WIDTH JOINER}"                  # joins emoji sequences; the one format character allowed
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")
PICK_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*_[a-z0-9][a-z0-9-]*$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,128}$")


class Anomaly(HTTPException):
    """Input the volunteer pages can't produce. A bare 400; the rule is only in the log."""

    def __init__(self, rule: str):
        super().__init__(status_code=400, detail="Malformed request")
        self.rule = rule


# ── recording ─────────────────────────────────────────────────────────────────

_request: contextvars.ContextVar = contextvars.ContextVar("validation_request", default=None)
NOTE_EVERY_SEC = 60
MAX_NOTED = 5000


class _Noted:
    """When each (client, rule, path) was last written, and how many came since."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last: OrderedDict = OrderedDict()      # key -> (monotonic time, held back)

    def take(self, key: tuple) -> int | None:
        """The count to report with a new row, or None to hold this one back."""
        now = _monotonic()
        with self.lock:
            when, held = self.last.get(key, (None, 0))
            if when is not None and now - when < NOTE_EVERY_SEC:
                self.last[key] = (when, held + 1)
                return None
            self.last[key] = (now, 0)
            self.last.move_to_end(key)
            while len(self.last) > MAX_NOTED:
                self.last.popitem(last=False)
            return held

    def clear(self) -> None:
        """Forget everything (tests)."""
        with self.lock:
            self.last.clear()


_noted = _Noted()
_INSERT_SQL = (
    "INSERT INTO portal_security_events (ts, event, severity, ip, method, path, message, data) "
    "VALUES (?, 'portal.anomaly', 'medium', ?, ?, ?, ?, ?)")


def _settings():
    """The portal's settings, read when needed: signup_fields imports this module, and some
    tests import that before the environment config.py requires is in place."""
    from config import settings  # pylint: disable=import-outside-toplevel
    return settings


def _client(request: Request) -> str:
    from routers.auth import client_ip     # here: routers import this module
    return client_ip(request)


def anomaly(rule: str, where: str) -> None:
    """Record an anomaly and, in enforce mode, refuse the request.

    Args:
        rule: Which rule, e.g. `name_chars`.
        where: Which field, e.g. `first_name` or `signup.prefs`.

    Raises:
        Anomaly: In enforce mode, always.
    """
    request = _request.get()
    if request is not None:
        ip = _client(request)
        held = _noted.take((ratelimit.ip_bucket(ip), rule, request.url.path))
        if held is not None:
            try:
                conn = db.connect(_settings().db_path)
                try:
                    conn.execute(_INSERT_SQL, (
                        clock.now_iso(), ip, request.method, request.url.path[:2000],
                        "Input the volunteer pages can't produce",
                        json.dumps({"rule": rule, "where": where[:100],
                                    "mode": _settings().strict, "held_back": held})))
                finally:
                    conn.close()
            except Exception:  # pylint: disable=broad-exception-caught
                # Deliberately broad: recording must never be why a request fails.
                log.exception("validation: could not record %s", rule)
    if _settings().strict == "enforce":
        raise Anomaly(rule)


async def strict_request(request: Request) -> None:
    """Router dependency for the public routers: remember the request for `anomaly()`, and
    flag what no page sends — a query parameter or form field the route doesn't take, or a
    POST body that isn't a form.

    Async on purpose: the context variable it sets is copied into the threadpool a sync
    endpoint runs in.

    Args:
        request: The incoming request.
    """
    _request.set(request)
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return
    taken = _declared(dependant)
    for name in request.query_params:
        if name not in taken["query"]:
            anomaly("unknown_field", f"query:{name[:40]}")
            break
    if request.method != "POST":
        return
    ctype = request.headers.get("content-type", "").lower()
    if request.headers.get("content-length", "0") not in ("", "0") and not ctype.startswith(
            ("multipart/form-data", "application/x-www-form-urlencoded")):
        anomaly("content_type", "body")
        return
    if ctype.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        form = await request.form()
        for name in form:
            if name not in taken["form"]:
                anomaly("unknown_field", f"form:{name[:40]}")
                break


def _declared(dependant) -> dict:
    """The query and form parameter names a route (and its dependencies) take."""
    out = {"query": set(), "form": set()}
    stack = [dependant]
    while stack:
        d = stack.pop()
        out["query"].update(p.alias for p in d.query_params)
        out["form"].update(p.alias for p in d.body_params)
        stack.extend(d.dependencies)
    return out


def reset() -> None:
    """Forget the per-client note times (tests)."""
    _noted.clear()


# ── the field rules (mirrored in frontend/src/lib/portal_rules.js) ────────────

def _normal(text: str, newlines: bool = False) -> str:
    """NFC, \\r\\n -> \\n, no-break spaces -> spaces, runs of spaces collapsed, trimmed.
    Newlines are kept (per line) only when `newlines`."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\N{NO-BREAK SPACE}", " ")
    if newlines:
        return "\n".join(re.sub(r" {2,}", " ", line).strip()
                         for line in text.split("\n")).strip()
    return re.sub(r" {2,}", " ", text).strip()


def _bad_char(text: str, newlines: bool = False) -> bool:
    """Control, format, private-use or line/paragraph-separator characters, other than a
    newline when `newlines` and the emoji zero-width joiner."""
    for c in text:
        if c == "\n" and newlines or c == _ZWJ:
            continue
        cat = unicodedata.category(c)
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"):
            return True
    return False


def check(kind: str, value, *, maximum: int = 0, required: bool = False):
    """Check one value against a rule. The fixture's `kind`s, one function.

    Args:
        kind: `name`, `handle`, `email`, `text`, `pick_key` or `token`.
        value: The value as sent.
        maximum: For `text`, its length limit.
        required: Whether empty is a mistake.

    Returns:
        `(clean, problem)`: problem is None, `("error", reason)` for an ordinary mistake, or
        `("anomaly", rule)`.
    """
    if not isinstance(value, str):
        return "", ("anomaly", "wrong_type")
    if kind in ("pick_key", "token"):
        pattern = PICK_KEY if kind == "pick_key" else TOKEN
        return value, (None if pattern.match(value) else ("anomaly", kind))
    multiline = kind == "text"
    if _bad_char(value.replace("\r\n", "\n"), newlines=multiline):
        return "", ("anomaly", "control_char")
    clean = _normal(value, newlines=multiline)
    if not clean:
        return "", ("error", "required") if required or kind in ("name", "email") else None
    if kind == "name":
        if len(clean) > NAME_MAX:
            return clean, ("anomaly", "too_long")
        ok = all(unicodedata.category(c)[0] in "LM" or c in _NAME_PUNCT for c in clean)
        return clean, (None if ok else ("anomaly", "name_chars"))
    if kind == "handle":
        if len(clean) > HANDLE_MAX:
            return clean, ("anomaly", "too_long")
        return clean, (("anomaly", "handle_chars") if set(clean) & _HANDLE_BANNED else None)
    if kind == "email":
        local = clean.rsplit("@", 1)[0]
        if len(clean) > EMAIL_MAX or len(local) > EMAIL_LOCAL_MAX or not _EMAIL.match(clean):
            return clean, ("anomaly", "email_syntax")
        return clean, None
    if kind == "text":
        return clean, (("anomaly", "too_long") if len(clean) > maximum else None)
    raise ValueError(f"unknown kind {kind!r}")


def field(kind: str, value, where: str, *, maximum: int = 0, required: bool = False,
          label: str = "") -> str:
    """`check`, acted on: an anomaly is recorded (and refused in enforce mode); an ordinary
    mistake is a 400 with a message for `label`.

    In report mode an anomaly gets what the portal did before this module existed: control
    characters dropped and spaces collapsed, and a 400 for an over-long value.

    Args:
        kind: As for `check`.
        value: The value as sent.
        where: The field, for the record.
        maximum: As for `check`.
        required: As for `check`.
        label: The field's name in an ordinary error, e.g. "First name".

    Returns:
        The clean value.

    Raises:
        HTTPException 400: An ordinary mistake (`label is required`).
        Anomaly: An anomaly, in enforce mode.
    """
    clean, problem = check(kind, value, maximum=maximum, required=required)
    if problem is None:
        return clean
    if problem[0] == "error":
        raise HTTPException(status_code=400, detail=f"{label or where} is required")
    anomaly(problem[1], where)
    # Report mode: what the portal did before. A wrong type was always a 400.
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{label or where} must be text")
    keep = " ".join("".join(c for c in value if c >= " ").split())
    limit = {"name": NAME_MAX, "handle": HANDLE_MAX}.get(kind)
    if limit and len(keep) > limit:
        raise HTTPException(status_code=400, detail=f"{label or where}: at most {limit} characters")
    return keep


# ── JSON in a form field ──────────────────────────────────────────────────────

def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constants(name):
    raise ValueError(f"{name} is not JSON")


def _depth(value, level: int = 1) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level + 1) for v in value])
    return level - 1


def json_object(raw: str, where: str, keys: frozenset) -> dict:
    """Parse a form field holding a JSON object, strictly: no duplicate keys, no NaN or
    Infinity, at most JSON_DEPTH_MAX deep, at most JSON_FIELD_MAX characters, and only `keys`.

    Args:
        raw: The field's text.
        where: The field's name, for the record.
        keys: The keys the object may have.

    Returns:
        The object, without unknown keys (in report mode they're dropped, as they always
        were ignored).

    Raises:
        HTTPException 400: Not JSON or not an object at all (always refused).
        Anomaly: Any other breach, in enforce mode.
    """
    if len(raw) > JSON_FIELD_MAX:
        anomaly("too_long", where)
    try:
        data = json.loads(raw, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
    except ValueError as exc:
        anomaly("json", where)
        try:
            data = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="Malformed request") from exc
    if not isinstance(data, dict):
        anomaly("json", where)
        raise HTTPException(status_code=400, detail="Malformed request")
    if _depth(data) > JSON_DEPTH_MAX:
        anomaly("json_depth", where)
    unknown = set(data) - keys
    if unknown:
        anomaly("unknown_key", f"{where}.{sorted(unknown)[0][:40]}")
    return {k: v for k, v in data.items() if k in keys}


def typed(value, kind: type, where: str, default=None):
    """`value` if it's a `kind` (bool is not an int here), else an anomaly.

    Args:
        value: The value.
        kind: The type it must be.
        where: The field, for the record.
        default: Returned when `value` is None.

    Returns:
        The value, or `default` for None, or (report mode) `default` for a wrong type.
    """
    if value is None:
        return default
    if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
        return value
    anomaly("wrong_type", where)
    return default
