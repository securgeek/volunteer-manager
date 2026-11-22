"""Mail relay — the only process that holds a Microsoft Graph credential.

It sends as RELAY_MAILBOX's account using a *delegated* Mail.Send sign-in: one device-code login
(`python relay.py login`) stores a refresh token, and every send exchanges it for a
short-lived access token. That refresh token is a credential for a real person's account,
so it's kept out of the internet-facing portal and lives only here. The relay does one
thing: send a plain-text message as MAIL_SENDER, which the operator sets at startup. A caller
supplies exactly `to`, `subject` and `body`; it can't choose the sender, add headers, send
HTML or attach anything.

Messages go to Graph as structured JSON with contentType "Text", not as MIME, so Microsoft
builds every header and there is nothing for a caller to inject into.

Standard library only, on purpose: the smaller this process, the less there is to attack in
the one container that holds the credential.

    python relay.py             # serve; needs MAIL_RELAY_TOKEN, GRAPH_*, RELAY_MAILBOX
    python relay.py login       # one-time device-code sign-in as RELAY_MAILBOX
    pytest test_relay.py -v     # no network needed
"""
import base64
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The From address is MAIL_SENDER (Config.sender): set by the operator when the relay starts,
# never by a request, so a caller can no more choose it than before. It's usually a Microsoft
# 365 group, which Graph can't send from directly, so the message goes through the signed-in
# account (RELAY_MAILBOX), which has Send As on it. Exchange enforces that permission on
# every send.

GRAPH_LOGIN = "https://login.microsoftonline.com"
GRAPH_API = "https://graph.microsoft.com/v1.0"
# Delegated. offline_access gets a refresh token; openid + profile get an id_token naming who
# signed in, which `login` checks against RELAY_MAILBOX before saving anything.
SCOPES = "https://graph.microsoft.com/Mail.Send offline_access openid profile"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

MAX_REQUEST_BYTES = 64 * 1024
MAX_SUBJECT = 200
MAX_BODY = 20_000
# One plain address, ASCII only. No display name, no comma or semicolon lists.
_ADDR = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_BODY_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # allows \t \n \r

log = logging.getLogger("mailrelay")

# Tests swap this out; the relay never needs any other network call.
_urlopen = urllib.request.urlopen


class ConfigError(RuntimeError):
    """A required setting is missing or unusable."""


class MailError(RuntimeError):
    """A send failed. The message carries Graph's error, never the recipient."""


class SignInRequired(MailError):
    """No refresh token, or Microsoft no longer accepts it. Fixed by `relay.py login`."""


class Config:
    """The relay's settings, read from the environment once at startup.

    Needs `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `MAIL_RELAY_TOKEN` (32 characters or more),
    `RELAY_MAILBOX` and `MAIL_SENDER` (one plain address). Optional: `RELAY_STATE_DIR` (`/state`), `RELAY_PORT` (5002),
    `RELAY_RATE_PER_MIN` (20) and `RELAY_RATE_PER_HOUR` (300).

    Raises:
        ConfigError: When a required setting is missing, the token is too short, or
            `MAIL_SENDER` isn't one plain address.
    """
    def __init__(self):
        self.tenant_id     = os.getenv("GRAPH_TENANT_ID", "").strip()
        self.client_id     = os.getenv("GRAPH_CLIENT_ID", "").strip()
        self.token         = os.getenv("MAIL_RELAY_TOKEN", "").strip()
        # The account that signs in and the mailbox mail goes through, not the From address.
        self.mailbox       = os.getenv("RELAY_MAILBOX", "").strip()
        # The From address of every message, the conference's own (e.g. volunteers@...).
        self.sender        = os.getenv("MAIL_SENDER", "").strip()
        # Holds the refresh token. The only writable path in the container.
        self.state_dir     = os.getenv("RELAY_STATE_DIR", "/state")
        self.port          = int(os.getenv("RELAY_PORT", "5002"))
        # Graph allows 30 messages a minute per mailbox; stay under it.
        self.rate_per_min  = int(os.getenv("RELAY_RATE_PER_MIN", "20"))
        self.rate_per_hour = int(os.getenv("RELAY_RATE_PER_HOUR", "300"))

        missing = [n for n, v in (("GRAPH_TENANT_ID", self.tenant_id),
                                  ("GRAPH_CLIENT_ID", self.client_id),
                                  ("MAIL_RELAY_TOKEN", self.token),
                                  ("RELAY_MAILBOX", self.mailbox),
                                  ("MAIL_SENDER", self.sender)) if not v]
        if missing:
            raise ConfigError(f"missing {', '.join(missing)}")
        if len(self.token) < 32:
            raise ConfigError("MAIL_RELAY_TOKEN must be at least 32 characters "
                              "(generate one with `openssl rand -hex 32`)")
        if len(self.sender) > 254 or not _ADDR.match(self.sender):
            raise ConfigError("MAIL_SENDER must be one plain address, e.g. volunteers@example.org")


class RateLimiter:
    """Sliding windows over send attempts. Bounds what a compromised caller can do.

    Args:
        per_min: Sends allowed in any 60 seconds.
        per_hour: Sends allowed in any hour.
    """

    def __init__(self, per_min: int, per_hour: int):
        self.per_min, self.per_hour = per_min, per_hour
        self.sent: deque = deque()
        self.lock = threading.Lock()

    def allow(self, now: float) -> bool:
        """Count one send, if the limits allow it.

        Args:
            now: The current time, in seconds (`time.time()`).

        Returns:
            True when the send may go ahead and has been counted; False when either window is full.
        """
        with self.lock:
            while self.sent and self.sent[0] <= now - 3600:
                self.sent.popleft()
            last_min = sum(1 for t in self.sent if t > now - 60)
            if last_min >= self.per_min or len(self.sent) >= self.per_hour:
                return False
            self.sent.append(now)
            return True


def _error_body(exc: urllib.error.HTTPError) -> dict:
    try:
        return json.loads(exc.read() or b"{}")
    except ValueError:
        return {}


def _describe(code: int, body: dict) -> str:
    # Token endpoint: {"error", "error_description"}. Graph: {"error": {"code", "message"}}.
    err = body.get("error")
    if isinstance(err, dict):
        return f"HTTP {code} {err.get('code')}: {err.get('message')}"
    if not err:
        return f"HTTP {code}"
    # AADSTS descriptions run on with trace and correlation ids; the first line is the reason.
    desc = (body.get("error_description") or "").splitlines()
    return f"HTTP {code} {err}: {desc[0] if desc else ''}"


def _post_form(url: str, fields: dict) -> dict:
    """POST to the identity platform. HTTPError propagates for the caller to interpret."""
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with _urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _id_token_user(id_token: str) -> str:
    """preferred_username from an id_token. Not a signature check: the token came straight
    from Microsoft over TLS, and this is only a guard against signing in as the wrong
    account."""
    try:
        part = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return claims.get("preferred_username") or ""
    except (AttributeError, IndexError, ValueError):
        return ""


class TokenStore:
    """The refresh token, one file, owner-only. Microsoft hands out a new refresh token on
    every use, so this is rewritten often: atomically, so a crash can't leave half a token.

    Args:
        cfg: The relay's settings, for `state_dir`.
    """

    def __init__(self, cfg: Config):
        self.path = os.path.join(cfg.state_dir, "refresh_token")

    def read(self):
        """The stored refresh token, read fresh each time.

        Returns:
            The token, or `None` before the first `login`.
        """
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().strip() or None
        except FileNotFoundError:
            return None

    def write(self, token: str) -> None:
        """Replace the refresh token atomically, with mode 0600.

        Args:
            token: The new refresh token.
        """
        tmp = f"{self.path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class Graph:
    """Delegated Graph client. One access token serves every send until shortly before it
    expires; then the stored refresh token buys the next one.

    Args:
        cfg: The relay's settings.
        store: Where the refresh token lives. Defaults to a `TokenStore` on `cfg`.
    """

    def __init__(self, cfg: Config, store: TokenStore = None):
        self.cfg = cfg
        self.store = store or TokenStore(cfg)
        self.lock = threading.Lock()
        self.token, self.expires = None, 0.0

    @property
    def token_url(self) -> str:
        """The tenant's OAuth token endpoint."""
        return f"{GRAPH_LOGIN}/{urllib.parse.quote(self.cfg.tenant_id)}/oauth2/v2.0/token"

    def signed_in(self) -> bool:
        """Whether `login` has stored a refresh token.

        Returns:
            True when a token is on file. It may still have expired or been revoked.
        """
        return self.store.read() is not None

    def _token(self) -> str:
        with self.lock:
            if self.token and time.time() < self.expires:
                return self.token
            # Read every time rather than once at start, so a `login` run in another process
            # takes effect without a restart.
            refresh = self.store.read()
            if not refresh:
                raise SignInRequired("not signed in; run `python relay.py login`")
            try:
                body = _post_form(self.token_url, {
                    "client_id": self.cfg.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "scope": SCOPES,
                })
            except urllib.error.HTTPError as exc:
                self.token = None
                err = _error_body(exc)
                reason = _describe(exc.code, err)
                if err.get("error") in ("invalid_grant", "interaction_required"):
                    # Password change, MFA re-prompt, 90 days unused, or revoked.
                    raise SignInRequired(f"sign-in expired; run `python relay.py login` "
                                         f"({reason})") from None
                raise MailError(f"Graph token refresh failed: {reason}") from None
            if body.get("refresh_token"):
                self.store.write(body["refresh_token"])
            # Refresh a minute early so a token never expires between here and the send.
            self.token = body["access_token"]
            self.expires = time.time() + int(body.get("expires_in", 3600)) - 60
            return self.token

    def send(self, to: str, subject: str, body: str) -> None:
        """Send one plain-text message from `MAIL_SENDER` through `/me/sendMail`.

        Args:
            to: The one recipient.
            subject: The subject line.
            body: The plain-text body.

        Raises:
            SignInRequired: When there is no usable sign-in.
            MailError: When Graph refuses the token refresh or the send.
        """
        payload = {
            "message": {
                "from": {"emailAddress": {"address": self.cfg.sender}},
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        }
        req = urllib.request.Request(
            f"{GRAPH_API}/me/sendMail",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {self._token()}",
                     "Content-Type": "application/json"})
        try:
            with _urlopen(req, timeout=20):
                pass  # 202 Accepted, empty body
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self.token = None  # revoked mid-hour; the next send refreshes
            raise MailError(f"Graph sendMail failed: {_describe(exc.code, _error_body(exc))}") from None


def login(cfg: Config, out=print, sleep=time.sleep, clock=time.time) -> str:
    """Device-code sign-in as RELAY_MAILBOX. Saves the refresh token; returns who signed in.

    Args:
        cfg: The relay's settings.
        out: Where to print the device-code instructions.
        sleep: Waits between polls. Tests pass a fake.
        clock: The time source. Tests pass a fake.

    Raises:
        MailError: When sign-in fails, times out, or signs in as an account other than
            `RELAY_MAILBOX`.
    """
    base = f"{GRAPH_LOGIN}/{urllib.parse.quote(cfg.tenant_id)}/oauth2/v2.0"
    try:
        dc = _post_form(f"{base}/devicecode", {"client_id": cfg.client_id, "scope": SCOPES})
    except urllib.error.HTTPError as exc:
        raise MailError(f"device code request failed: {_describe(exc.code, _error_body(exc))}") from None
    out(dc["message"])
    interval = int(dc.get("interval", 5))
    deadline = clock() + int(dc.get("expires_in", 900))
    while clock() < deadline:
        sleep(interval)
        try:
            tok = _post_form(f"{base}/token", {"client_id": cfg.client_id,
                                               "grant_type": DEVICE_CODE_GRANT,
                                               "device_code": dc["device_code"]})
        except urllib.error.HTTPError as exc:
            err = _error_body(exc)
            if err.get("error") == "authorization_pending":
                continue
            if err.get("error") == "slow_down":
                interval += 5
                continue
            raise MailError(f"sign-in failed: {_describe(exc.code, err)}") from None
        user = _id_token_user(tok.get("id_token"))
        if user.lower() != cfg.mailbox.lower():
            raise MailError(f"signed in as {user or 'an unknown account'}, but RELAY_MAILBOX "
                            f"is {cfg.mailbox}; nothing was saved")
        if not tok.get("refresh_token"):
            raise MailError("no refresh token returned; is offline_access on the app's "
                            "API permissions?")
        TokenStore(cfg).write(tok["refresh_token"])
        return user
    raise MailError("the device code expired before sign-in finished; run login again")


def validate(payload) -> tuple[str, str, str]:
    """Return (to, subject, body) or raise ValueError with a reason safe to send back.

    Args:
        payload: The decoded JSON body.
    """
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    extra = set(payload) - {"to", "subject", "body"}
    if extra:
        raise ValueError(f"unexpected field(s): {', '.join(sorted(extra))}")
    to, subject, body = payload.get("to"), payload.get("subject"), payload.get("body")
    if not all(isinstance(v, str) for v in (to, subject, body)):
        raise ValueError("to, subject and body are required strings")
    to = to.strip()
    if len(to) > 254 or not _ADDR.match(to):
        raise ValueError("to must be a single plain email address")
    if not subject or len(subject) > MAX_SUBJECT or _CTRL.search(subject):
        raise ValueError(f"subject must be 1-{MAX_SUBJECT} characters on one line")
    if not body or len(body) > MAX_BODY or _BODY_CTRL.search(body):
        raise ValueError(f"body must be 1-{MAX_BODY} characters of plain text")
    return to, subject, body


class Relay:
    """Request handling with no HTTP in it, so tests can drive it directly.

    Args:
        cfg: The relay's settings.
        graph: The Graph client. Defaults to a real one; tests pass a fake.
        limiter: The rate limiter. Defaults to one built from `cfg`.
        clock: The time source for the limiter.
    """

    def __init__(self, cfg: Config, graph=None, limiter=None, clock=time.time):
        self.cfg = cfg
        self.graph = graph or Graph(cfg)
        self.limiter = limiter or RateLimiter(cfg.rate_per_min, cfg.rate_per_hour)
        self.clock = clock

    def authorized(self, header: str) -> bool:
        """Check the caller's bearer token, in constant time.

        Args:
            header: The `Authorization` header, or blank.

        Returns:
            Whether it is `Bearer` with the relay's token.
        """
        scheme, _, presented = (header or "").partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(presented.encode(),
                                                          self.cfg.token.encode())

    def send(self, auth_header: str, raw: bytes) -> tuple[int, dict]:
        """Handle one `POST /send`: check auth, validate, rate limit, send.

        Args:
            auth_header: The `Authorization` header.
            raw: The request body.

        Returns:
            `(status, body)`: 202 sent, 400 invalid, 401 unauthorized, 429 rate limited, 502
            Graph or network failure, 503 not signed in.
        """
        if not self.authorized(auth_header):
            return 401, {"error": "unauthorized"}
        try:
            to, subject, body = validate(json.loads(raw))
        except ValueError as exc:  # json.JSONDecodeError is a ValueError too
            return 400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError)
                         else "body must be JSON"}
        if not self.limiter.allow(self.clock()):
            log.warning("send refused: rate limit")
            return 429, {"error": "rate limit exceeded"}
        mid = uuid.uuid4().hex
        try:
            self.graph.send(to, subject, body)
        except SignInRequired as exc:
            log.error("send %s failed: %s", mid, exc)
            return 503, {"error": str(exc), "id": mid}
        except MailError as exc:
            log.error("send %s failed: %s", mid, exc)
            return 502, {"error": str(exc), "id": mid}
        except OSError as exc:  # DNS, connection refused, timeout
            log.error("send %s failed: %s", mid, type(exc).__name__)
            return 502, {"error": f"could not reach Microsoft: {type(exc).__name__}", "id": mid}
        # Logs carry an id, never the address — the same rule as the portal's audit log.
        log.info("send %s ok", mid)
        return 202, {"sent": True, "id": mid}


class Handler(BaseHTTPRequestHandler):
    """The HTTP layer: `GET /health` and `POST /send`. The work is in `Relay`."""
    server_version = "mailrelay"
    sys_version = ""

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        """Answer `GET /health` with whether the relay is signed in; 404 for anything else."""
        if self.path == "/health":
            return self._reply(200, {"ok": True, "signed_in": self.server.relay.graph.signed_in()})
        self._reply(404, {"error": "not found"})

    def do_POST(self):
        """Check the transport (path, JSON content type, length) and pass `/send` to `Relay`."""
        if self.path != "/send":
            return self._reply(404, {"error": "not found"})
        if self.headers.get_content_type() != "application/json":
            return self._reply(415, {"error": "Content-Type must be application/json"})
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, {"error": "Content-Length required"})
        if length < 0 or length > MAX_REQUEST_BYTES:
            return self._reply(413, {"error": "request too large"})
        raw = self.rfile.read(length)
        self._reply(*self.server.relay.send(self.headers.get("Authorization", ""), raw))

    def log_message(self, fmt, *args):
        """Send the request log to debug logging rather than stderr.

        Args:
            fmt: The format string from `BaseHTTPRequestHandler`.
            *args: Its arguments.
        """
        log.debug("%s %s", self.address_string(), fmt % args)


# All interfaces inside the container, which has no published ports: only mail_internal (the
# portal) can reach it. See compose.yml.
def make_server(relay: Relay, host: str = "0.0.0.0",  # nosec B104
                port: int = 5002) -> ThreadingHTTPServer:
    """Build the threaded HTTP server for a relay.

    Args:
        relay: The relay to serve.
        host: Address to listen on. All interfaces inside the container.
        port: Port to listen on.

    Returns:
        The server, not yet started.
    """
    server = ThreadingHTTPServer((host, port), Handler)
    server.relay = relay
    return server


def main() -> None:
    """Run the relay, or `relay.py login` to sign in. Exits 2 on bad configuration."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        cfg = Config()
    except ConfigError as exc:
        print(f"mailrelay: refusing to start — {exc}", file=sys.stderr)
        sys.exit(2)

    if sys.argv[1:] == ["login"]:
        print(f"Sign in as {cfg.mailbox}.")
        try:
            user = login(cfg)
        except MailError as exc:
            sys.exit(f"login failed: {exc}")
        print(f"Signed in as {user}. The relay will use this sign-in from its next send.")
        return
    if sys.argv[1:]:
        sys.exit("usage: relay.py [login]")

    relay = Relay(cfg)
    server = make_server(relay, port=cfg.port)
    log.info("mailrelay ready on :%d, sending as %s through %s", cfg.port, cfg.sender,
             cfg.mailbox)
    if not relay.graph.signed_in():
        # Starts anyway, so `login` can be run inside the running container.
        log.warning("not signed in: sends will fail until `python relay.py login` is run")
    server.serve_forever()


if __name__ == "__main__":
    main()
