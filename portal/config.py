"""Portal configuration, read from the environment at import time.

The one rule worth stating: ``EMAIL_HMAC_KEY`` is required and has no default. If the portal
generated a fresh key when the variable was missing, every stored ``email_hmac`` would be
orphaned — nobody could sign in, and the partial-unique index would stop deduplicating
against existing rows. That failure is silent at boot and disastrous a week later, so a
missing key is a hard startup failure instead.
"""
import os
import sys


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


def _req(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError(
            f"{name} is required and has no default. Generate one with "
            f"`openssl rand -hex 32` and supply it to both the portal and the scheduler."
        )
    return val


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


class Settings:
    """Resolved settings. Instantiated once, at the bottom of this module."""

    def __init__(self, require_secrets: bool = True):
        self.db_path      = os.getenv("PORTAL_DB", "/data/portal.db")
        # The public URL volunteers see in a magic link. Deliberately NOT the same variable
        # the scheduler uses to reach the sync API: that one is often an internal address
        # (http://portal:5001) while this one must be the address a browser can open.
        self.base_url     = (os.getenv("PORTAL_PUBLIC_URL")
                             or os.getenv("PORTAL_BASE_URL")
                             or "http://localhost:5001").rstrip("/")
        # The organizer site's origin, which admin invite and reset links must start with
        # (routers/sync.py, send_admin_mail). A separate hostname in the deployment, so the
        # public portal host never serves /admin; unset, it is the same origin as base_url.
        self.admin_url    = (os.getenv("ADMIN_PUBLIC_URL", "").strip()
                             or self.base_url).rstrip("/")
        self.mail_backend = os.getenv("MAIL_BACKEND", "file")
        self.mail_dir     = os.getenv("MAIL_DIR", "/app/logs/portal_mail")
        self.smtp_host    = os.getenv("SMTP_HOST", "")
        self.smtp_port    = _int("SMTP_PORT", 587)
        self.smtp_user    = os.getenv("SMTP_USER", "")
        self.smtp_pass    = os.getenv("SMTP_PASS", "")
        self.smtp_from    = os.getenv("SMTP_FROM", "volunteers@example.org")
        # MAIL_BACKEND=relay sends through the mailrelay container. The Graph credentials live
        # there, never here; this process only holds the token that lets it ask.
        self.relay_url    = os.getenv("MAIL_RELAY_URL", "http://mailrelay:5002").rstrip("/")
        self.relay_token  = os.getenv("MAIL_RELAY_TOKEN", "").strip()
        if self.mail_backend == "relay" and not self.relay_token:
            raise ConfigError("MAIL_BACKEND=relay needs MAIL_RELAY_TOKEN")
        # mailer.py hands this to urlopen, which also opens file:// and other schemes.
        if self.mail_backend == "relay" and not self.relay_url.startswith(("http://",
                                                                            "https://")):
            raise ConfigError(f"MAIL_RELAY_URL must be http:// or https://, got "
                              f"{self.relay_url!r}")

        # Login link and session lifetimes.
        self.token_ttl_min      = _int("PORTAL_TOKEN_TTL_MIN", 15)
        self.session_ttl_hours  = _int("PORTAL_SESSION_TTL_HOURS", 12)
        self.remember_ttl_days  = _int("PORTAL_REMEMBER_TTL_DAYS", 90)

        # Rate limits, per IP and per address hash (§10).
        self.rate_per_min  = _int("PORTAL_RATE_PER_MIN", 5)
        self.rate_per_hour = _int("PORTAL_RATE_PER_HOUR", 30)
        # A response floor, so timing doesn't leak what the copy doesn't.
        self.response_floor_ms = _int("PORTAL_RESPONSE_FLOOR_MS", 250)

        # Strict input validation (validation.py): "report" records anomalies and lets the
        # request through as before; "enforce" refuses them too.
        self.strict = os.getenv("PORTAL_STRICT", "report").strip().lower() or "report"
        if self.strict not in ("report", "enforce"):
            raise ConfigError(f"PORTAL_STRICT must be report or enforce, got {self.strict!r}")

        # Sync request signing (§2). SYNC_SECRET_PREV lets a rotation land without
        # restarting both services at the same moment.
        self.sync_skew_sec = _int("SYNC_SKEW_SEC", 300)
        if require_secrets:
            self.email_hmac_key = _req("EMAIL_HMAC_KEY").encode()
            self.sync_key_id    = _req("SYNC_KEY_ID")
            self.sync_secret    = _req("SYNC_SECRET").encode()
        else:
            self.email_hmac_key = b""
            self.sync_key_id    = ""
            self.sync_secret    = b""
        prev = os.getenv("SYNC_SECRET_PREV", "").strip()
        self.sync_secret_prev = prev.encode() if prev else None

    def secrets_for(self, key_id: str) -> list[bytes]:
        """Candidate signing secrets for a key id — current, plus the previous during rotation.

        Args:
            key_id: The key id from the request's signature header.

        Returns:
            The secrets to try, or `[]` for an unknown key id.
        """
        if key_id != self.sync_key_id:
            return []
        return [s for s in (self.sync_secret, self.sync_secret_prev) if s]


def load(require_secrets: bool = True) -> Settings:
    """Build a Settings instance. Tests call this with require_secrets=False.

    Args:
        require_secrets: Refuse to start without the sync and email keys. Tests pass False.
    """
    return Settings(require_secrets=require_secrets)


# Importing this module is what enforces the hard failure described above. Tests import
# `load` directly and never touch this, so they don't need the real environment.
settings: Settings
if os.getenv("PORTAL_SKIP_CONFIG") == "1":
    settings = load(require_secrets=False)
else:
    try:
        settings = load()
    except ConfigError as exc:
        print(f"portal: refusing to start — {exc}", file=sys.stderr)
        raise
