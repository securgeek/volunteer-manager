"""Request signals: malformed input, attack patterns and probing, noticed for alerting.

`SecuritySignals` is an ASGI middleware that watches every request and response and reports
what looks hostile. It **never refuses a client**: the one request it answers itself is one
carrying a NUL character, which no browser sends and the database can't take (a 400).
Everything else goes through untouched; this only watches.

This file is identical in `backend/` and `portal/` (the apps share no code, so each has a
copy; `backend/test_security_signals.py` fails if they differ). Each app passes in what
differs: how to read the client's address, where findings go, and which paths to skip.

What it looks at, before the route runs:

* **Signatures** (`SIGNATURES`, `SCANNER_AGENT`, `PROBE_PATH`): SQL injection, path
  traversal, script injection, template and command injection, scanner user agents and
  the paths scanners try. Checked in the path, the query string, the User-Agent and form
  fields (urlencoded or multipart, up to BODY_LIMIT; file parts and any field named like a
  password are skipped). Deliberately narrow, so O'Brien, "Rock & Roll" or a note saying
  "drop off at the desk" never match.
* **A NUL** anywhere in those: refused with a 400.
* **Cross-site writes**: a POST (PUT, PATCH, DELETE) the browser marks `Sec-Fetch-Site:
  cross-site`, or whose `Origin` isn't one of the app's own.
* **A session cookie whose signature fails** (when the app passes `cookie_check`).

and after it, the response status: 404 under the API prefix (probing), 422 and 400
(malformed input), 401, 403 (reaching above a role), 405, 413, 415.

Each signal has a weight (`WEIGHTS`). A client's weights add up over WINDOW_SEC; crossing
SCORE_ALERT is a `suspicious_client` finding. Any signature is an `attack_signature` finding
at once. A 401 under `critical_prefix` (the portal's signed sync API, which no browser
calls) is a `sync_auth_fail`. Each finding is reported at most once per client per
NOTE_COOLDOWN_SEC; the ones in between are counted and the next report carries the count.
So a scanner's thousand requests are a handful of rows, not a thousand, and single 404s or
422s are never rows at all, only counts in memory.

Clients are counted by address, an IPv6 one by its /64. State is in memory, capped at
MAX_CLIENTS (oldest dropped), for the one worker each app runs.
"""
import asyncio
import ipaddress
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("security_signals")

# The clock. A name of its own so tests can move it without touching time.monotonic itself,
# which asyncio's loop runs on too.
_monotonic = time.monotonic

WINDOW_SEC = 600
SCORE_ALERT = 15
NOTE_COOLDOWN_SEC = 3600
MAX_CLIENTS = 10_000
BODY_LIMIT = 1_000_000
SNIPPET = 80
MAX_FIELDS = 200

# Signal -> weight toward SCORE_ALERT.
WEIGHTS = {
    "signature": 5, "scanner": 5, "probe_path": 5, "nul": 5,
    "bad_cookie": 4, "cross_site": 4,
    "status_403": 3, "status_404": 2, "status_400": 2, "status_413": 2, "status_415": 2,
    "status_401": 1, "status_405": 1, "status_422": 1,
    "malformed_body": 2,
}
SEVERITY = {"attack_signature": "high", "suspicious_client": "high", "sync_auth_fail": "critical"}

SIGNATURES: tuple = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in (
    ("sql_union", r"\bunion\b[\s/*+]+(?:all[\s/*+]+)?select\b"),
    ("sql_tautology", r"'\s*(?:or|and)\s+'?(\w+)'?\s*=\s*'?\1\b"),
    ("sql_stacked", r";\s*(?:drop|truncate|alter)\s+(?:table|database|schema)\b"),
    ("sql_sleep", r"\b(?:pg_sleep|sleep|benchmark)\s*\(\s*\d|\bwaitfor\s+delay\b"),
    ("sql_catalog", r"\binformation_schema\b|\bpg_catalog\b|\bsqlite_master\b"),
    ("path_traversal", r"\.\.[/\\]|%2e%2e|%252e|/etc/passwd\b|\\windows\\win\.ini"),
    ("script", r"<\s*script\b|<\s*iframe\b|\bon(?:error|load)\s*=|javascript\s*:"),
    ("template", r"\{\{\s*\d+\s*\*\s*\d+\s*\}\}|\$\{\s*\d+\s*\*\s*\d+\s*\}|\$\{\s*jndi\s*:"),
    ("command", r"[;|&]\s*(?:cat|wget|curl|nc|bash|sh)\s+(?:-|/|https?:)"
                r"|\$\(\s*(?:curl|wget|id|whoami|uname)\b"),
))
SCANNER_AGENT = re.compile(
    r"sqlmap|nikto|nuclei|zgrab|masscan|wpscan|dirbuster|gobuster|ffuf|feroxbuster|nmap"
    r"|acunetix|netsparker|interactsh|openvas|nessus", re.IGNORECASE)
PROBE_PATH = re.compile(
    r"(?:^|/)(?:\.env|\.git|\.svn|\.hg|\.aws|\.ssh|\.ds_store|wp-admin|wp-login|wp-content"
    r"|xmlrpc\.php|phpmyadmin|actuator|server-status|cgi-bin|vendor/phpunit|boaform|hnap1)"
    r"(?:/|$|\.)|\.(?:php\d?|aspx?|jsp|cgi)$", re.IGNORECASE)
# Never inspected: what's in them is no one's business, even an attacker's.
_SECRET_FIELD = re.compile(r"pass(?:word)?|secret|credential", re.IGNORECASE)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def client_key(ip: str) -> str:
    """What a client is counted by: an IPv6 address's /64, otherwise the address.

    Args:
        ip: The client's address, or anything else.

    Returns:
        The key.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def _snippet(text: str, start: int, end: int) -> str:
    """Up to SNIPPET characters of `text` around `start`..`end`."""
    pad = max(0, (SNIPPET - (end - start)) // 2)
    return text[max(0, start - pad):max(0, start - pad) + SNIPPET]


@dataclass
class Hit:
    """One thing noticed in a request.

    Attributes:
        signal: A key of WEIGHTS.
        rule: Which pattern, for a signature.
        where: The field, `path`, `query:<name>` or `user-agent`.
        snippet: The text around the match.
    """

    signal: str
    rule: str = ""
    where: str = ""
    snippet: str = ""


def inspect_text(where: str, text: str) -> list:
    """The signature hits (and a NUL) in one piece of text.

    Args:
        where: What the text is, for the report.
        text: The text.

    Returns:
        A list of Hit.
    """
    hits = []
    if "\x00" in text:
        i = text.index("\x00")
        hits.append(Hit("nul", "nul", where, _snippet(text, i, i + 1)))
    for name, pattern in SIGNATURES:
        m = pattern.search(text)
        if m:
            hits.append(Hit("signature", name, where, _snippet(text, m.start(), m.end())))
    return hits


def inspect_head(request: Request) -> list:
    """Hits in what every request has: path, query string, User-Agent.

    Args:
        request: The request.

    Returns:
        A list of Hit.
    """
    path = request.url.path
    hits = inspect_text("path", path)
    raw = request.scope.get("raw_path") or b""
    if b"%00" in raw.lower() or b"%2500" in raw.lower():
        hits.append(Hit("nul", "nul", "path", path[:SNIPPET]))
    if PROBE_PATH.search(path):
        hits.append(Hit("probe_path", "probe_path", "path", path[:SNIPPET]))
    for name, value in request.query_params.multi_items():
        if not _SECRET_FIELD.search(name):
            hits += inspect_text(f"query:{name[:40]}", value)
            hits += inspect_text("query", name)
    agent = request.headers.get("user-agent", "")
    if SCANNER_AGENT.search(agent):
        hits.append(Hit("scanner", "scanner_agent", "user-agent", agent[:SNIPPET]))
    hits += inspect_text("user-agent", agent)
    return hits


@dataclass
class _Client:
    """One client's recent signals and when each finding kind may be reported again."""

    signals: deque = field(default_factory=deque)       # (time, signal)
    quiet_until: dict = field(default_factory=dict)      # kind -> time
    held_back: dict = field(default_factory=dict)        # kind -> count


class SecuritySignals:
    """The middleware. See the module docstring.

    Args:
        app: The ASGI app it wraps.
        report: Called as `report(kind, request, data)` for each finding, in a worker
            thread. `kind` is a key of SEVERITY; `data` is plain JSON values. It must not
            raise (a failure is logged and dropped).
        client_ip: The app's own way of reading the client's address from a request.
        api_prefix: Under it, a 404 counts as probing.
        skip_paths: Paths not watched at all (health checks).
        no_body_prefixes: Paths whose bodies aren't read (a signed JSON API).
        critical_prefix: Under it, a 401 is a `sync_auth_fail`. None for none.
        allowed_origins: The app's own origins; a write from any other Origin is a signal.
            Empty to skip the check.
        cookie_check: `cookie_check(request)` -> False when the request carries a session
            cookie whose signature fails. None to skip.
    """

    def __init__(self, app, *, report: Callable, client_ip: Callable,
                 api_prefix: str = "/api/", skip_paths: tuple = (),
                 no_body_prefixes: tuple = (), critical_prefix: str | None = None,
                 allowed_origins: tuple = (), cookie_check: Callable | None = None):
        self.app = app
        self.report = report
        self.client_ip = client_ip
        self.api_prefix = api_prefix
        self.skip_paths = frozenset(skip_paths)
        self.no_body_prefixes = tuple(no_body_prefixes)
        self.critical_prefix = critical_prefix
        self.allowed_origins = frozenset(o.rstrip("/") for o in allowed_origins if o)
        self.cookie_check = cookie_check
        self._clients: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    # ── the request ──────────────────────────────────────────────────────────

    def _wants_body(self, request: Request) -> bool:
        if request.method not in _WRITE_METHODS:
            return False
        if self.no_body_prefixes and request.url.path.startswith(self.no_body_prefixes):
            return False
        ctype = request.headers.get("content-type", "").lower()
        length = request.headers.get("content-length", "")
        return (ctype.startswith(_FORM_TYPES) and length.isdigit()
                and int(length) <= BODY_LIMIT)

    @staticmethod
    async def _read(receive) -> tuple:
        """Every body message, and whether the client went away while reading."""
        messages, more = [], True
        while more:
            msg = await receive()
            messages.append(msg)
            if msg["type"] != "http.request":
                return messages, True
            more = msg.get("more_body", False)
        return messages, False

    @staticmethod
    def _replay(messages: list, receive):
        """A `receive` that gives back `messages`, then defers to the real one."""
        queue = list(messages)

        async def replay():
            if queue:
                return queue.pop(0)
            return await receive()
        return replay

    async def _inspect_body(self, scope, messages: list, receive) -> list:
        """Hits in the form fields of a buffered body."""
        hits = []
        try:
            form = await Request(scope, self._replay(messages, receive)).form(
                max_fields=MAX_FIELDS, max_files=MAX_FIELDS)
        except Exception:  # pylint: disable=broad-exception-caught
            # Deliberately broad: a body the form parser chokes on is itself the finding,
            # and the route will refuse it on its own.
            return [Hit("malformed_body", "form_parse", "body")]
        try:
            for name, value in form.multi_items():
                if isinstance(value, UploadFile) or _SECRET_FIELD.search(name):
                    continue
                hits += inspect_text(f"form:{name[:40]}", value)
        finally:
            await form.close()
        return hits

    def _head_signals(self, request: Request) -> list:
        """Cross-site writes and a bad session cookie."""
        hits = []
        if request.method in _WRITE_METHODS:
            site = request.headers.get("sec-fetch-site", "")
            origin = request.headers.get("origin", "").rstrip("/")
            if site == "cross-site" or (origin and self.allowed_origins
                                        and origin not in self.allowed_origins):
                hits.append(Hit("cross_site", "cross_site", "origin", (origin or site)[:SNIPPET]))
        if self.cookie_check is not None:
            try:
                if self.cookie_check(request) is False:
                    hits.append(Hit("bad_cookie", "bad_cookie", "cookie"))
            except Exception:  # pylint: disable=broad-exception-caught
                # Deliberately broad: the check is the app's; it must not break a request.
                log.exception("security_signals: cookie_check failed")
        return hits

    # ── the middleware ───────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.skip_paths:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        hits = inspect_head(request) + self._head_signals(request)
        if self._wants_body(request):
            messages, gone = await self._read(receive)
            receive = self._replay(messages, receive)
            if not gone:
                hits += await self._inspect_body(scope, messages, receive)
        status = {}

        async def watch(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            if any(h.signal == "nul" for h in hits):
                await JSONResponse({"detail": "Malformed request"}, status_code=400)(
                    scope, receive, watch)
            else:
                await self.app(scope, receive, watch)
        finally:
            await self._settle(request, hits, status.get("code"))

    # ── scoring and reporting ────────────────────────────────────────────────

    def _client(self, key: str) -> _Client:
        client = self._clients.get(key)
        if client is None:
            client = self._clients[key] = _Client()
            while len(self._clients) > MAX_CLIENTS:
                self._clients.popitem(last=False)
        else:
            self._clients.move_to_end(key)
        return client

    def _note(self, client: _Client, kind: str, now: float) -> int | None:
        """Whether a `kind` finding may be reported now: the count held back since the last
        one, or None while it's still quiet (and then this one is held back too)."""
        if client.quiet_until.get(kind, 0) > now:
            client.held_back[kind] = client.held_back.get(kind, 0) + 1
            return None
        client.quiet_until[kind] = now + NOTE_COOLDOWN_SEC
        return client.held_back.pop(kind, 0)

    def _attack(self, client: _Client, hits: list, now: float) -> list:
        """An `attack_signature` finding for any signature among `hits`, if not quiet."""
        attack = [h for h in hits if WEIGHTS[h.signal] >= WEIGHTS["signature"]]
        held = self._note(client, "attack_signature", now) if attack else None
        if held is None:
            return []
        return [("attack_signature", {"rules": sorted({h.rule for h in attack}),
                                      "where": attack[0].where, "snippet": attack[0].snippet,
                                      "held_back": held})]

    def _suspicious(self, client: _Client, now: float) -> list:
        """A `suspicious_client` finding when the client's score is over, if not quiet."""
        score = sum(WEIGHTS[s] for _, s in client.signals)
        held = self._note(client, "suspicious_client", now) if score >= SCORE_ALERT else None
        if held is None:
            return []
        counts: dict = {}
        for _, s in client.signals:
            counts[s] = counts.get(s, 0) + 1
        return [("suspicious_client", {"score": score, "window_min": WINDOW_SEC // 60,
                                       "signals": counts, "held_back": held})]

    def _findings(self, request: Request, hits: list, status: int | None) -> list:
        """The findings this request produces, as (kind, data) pairs."""
        path = request.url.path
        signals = [h.signal for h in hits]
        if status in (400, 401, 403, 405, 413, 415, 422) or (
                status == 404 and path.startswith(self.api_prefix)):
            signals.append(f"status_{status}")
        key = client_key(self.client_ip(request) or "unknown")
        now = _monotonic()
        with self._lock:
            client = self._client(key)
            client.signals.extend((now, s) for s in signals)
            while client.signals and client.signals[0][0] <= now - WINDOW_SEC:
                client.signals.popleft()
            found = self._attack(client, hits, now) + self._suspicious(client, now)
            if status == 401 and self.critical_prefix and path.startswith(self.critical_prefix):
                held = self._note(client, "sync_auth_fail", now)
                if held is not None:
                    found.append(("sync_auth_fail", {"held_back": held}))
        for _, data in found:
            data.update({"status": status, "client_key": key,
                         "user_agent": request.headers.get("user-agent", "")[:SNIPPET]})
        return found

    async def _settle(self, request: Request, hits: list, status: int | None) -> None:
        try:
            found = self._findings(request, hits, status)
        except Exception:  # pylint: disable=broad-exception-caught
            # Deliberately broad: watching must never be why a request fails.
            log.exception("security_signals: scoring failed")
            return
        for kind, data in found:
            try:
                await asyncio.to_thread(self.report, kind, request, data)
            except Exception:  # pylint: disable=broad-exception-caught
                # Deliberately broad, for the same reason.
                log.exception("security_signals: report of %s failed", kind)

    def reset(self) -> None:
        """Forget every client (tests)."""
        with self._lock:
            self._clients.clear()
