"""The scheduler API's FastAPI app: routers, session middleware and startup.

`app` is what uvicorn serves. Each area of the API is a router in `routers/`. Startup
(`lifespan`) quiets the healthcheck log lines, hands the event loop to the SSE broadcaster in
`routers/events.py`, and starts the portal sync loop when the portal is configured.

Run as a script, it starts uvicorn on port 5000. It takes `-d` for debug logging and `--audit
<path>` for where the audit log goes.
"""
import asyncio
import logging
import os
import secrets
import argparse
from dataclasses import dataclass
from contextlib import asynccontextmanager
import itsdangerous
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware

import alerts
import audit
import db as _db
import security_signals
import portal_sync
from routers import (auth, schedule, volunteers, users, email_keys, setup, events, radios,
                    portal_admin, passkeys, invites, password_reset, checkin, api_tokens,
                    export, email_template, portal_content, audit_log)


class QuietPolls(logging.Filter):
    """Drop successful container healthchecks from the access log. They arrive every few
    seconds and say nothing. A failing one (status >= 400) still logs, as does every other
    request. (The portal has its own copy, which also drops the sync heartbeat.)"""
    PATHS = {"/api/health"}

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether an access-log record is written.

        Args:
            record: One uvicorn access-log record. Its `args` are `(client, method, path,
                version, status)`.

        Returns:
            False for a successful GET of a healthcheck path, True for everything else.
        """
        try:
            _client, method, path, _ver, status = record.args
        except (TypeError, ValueError):
            return True
        return not (method == "GET" and path.split("?")[0] in self.PATHS and int(status) < 400)


def quiet_polls() -> None:
    """Attach QuietPolls to uvicorn's access log. Called from lifespan, which runs after
    uvicorn has configured its loggers, so the filter isn't replaced; idempotent, since a
    TestClient runs lifespan once per client."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietPolls) for f in access.filters):
        access.addFilter(QuietPolls())


log = logging.getLogger("main")

PRUNE_EVERY_SEC = 24 * 3600


def _prune_audit() -> None:
    """Prune `audit_events` once (audit.prune). Logs and swallows a failure."""
    conn = _db.get_conn()
    try:
        gone = audit.prune(conn)
        if gone:
            log.info("audit: pruned %d event(s) older than %d days", gone, audit.retention_days())
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Deliberately broad: a failed prune must not stop the loop; it tries again tomorrow.
        log.error("audit: prune failed: %s", exc)
    finally:
        _db.put_conn(conn)


async def _prune_audit_daily() -> None:
    """Prune `audit_events` now and then every PRUNE_EVERY_SEC, until cancelled."""
    while True:
        await asyncio.to_thread(_prune_audit)
        await asyncio.sleep(PRUNE_EVERY_SEC)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the app's background parts, and stop them on shutdown.

    Args:
        app: The FastAPI app (unused; FastAPI passes it).

    Yields:
        Control to FastAPI while the app runs.
    """
    quiet_polls()
    loop = asyncio.get_running_loop()
    events.set_loop(loop)
    # No-op unless PORTAL_SYNC_URL and the sync keys are set, so a deployment without a
    # portal behaves exactly as it did before.
    portal_sync.start(loop)
    # Only when the audit table is in use (main below); tests start the app without it.
    pruner = None
    if audit.configured_to_db():
        pruner = loop.create_task(_prune_audit_daily())
        alerts.start(loop)
    yield
    if pruner:
        pruner.cancel()
        alerts.stop()


app = FastAPI(title="Conference Scheduler", lifespan=lifespan)


@dataclass
class _MainState:
    """Module state set in __main__: the session cookie's signing key, for the signals'
    cookie check (None until then, which skips the check)."""

    session_secret: str | None = None


_main = _MainState()


def _report_signal(kind: str, request: Request, data: dict) -> None:
    """Record a SecuritySignals finding as an audit event (and so, through alerts, an alert).

    Args:
        kind: A key of security_signals.SEVERITY.
        request: The request that produced it.
        data: The finding's details.
    """
    sid = (request.scope.get("session") or {}).get("session_id")
    if kind == "attack_signature":
        audit.audit("security.attack_signature", request, sid,
                    "Attack patterns in a request", **data)
    elif kind == "suspicious_client":
        audit.audit("security.suspicious_client", request, sid,
                    "Unusual run of refused or malformed requests from one client", **data)
    elif kind == "sync_auth_fail":
        audit.audit("security.sync_auth_fail", request, sid,
                    "Bad signature on the sync API", **data)


def _bad_session_cookie(request: Request) -> bool | None:
    """False when the request carries a session cookie whose signature fails (not merely an
    expired one, which an old tab sends); None when there's nothing to say."""
    cookie = request.cookies.get("session")
    if not cookie or not _main.session_secret:
        return None
    try:
        itsdangerous.TimestampSigner(_main.session_secret).unsign(cookie.encode("utf-8"))
    except itsdangerous.BadSignature:
        return False
    return None


# Inside SessionMiddleware (added in __main__, so outermost): the session is decoded by the
# time a finding is reported, which names the signed-in user.
app.add_middleware(
    security_signals.SecuritySignals,
    report=_report_signal, client_ip=audit.client_ip, api_prefix="/api/",
    skip_paths=("/api/health",), cookie_check=_bad_session_cookie,
    allowed_origins=tuple(o.strip() for o in (os.getenv("ADMIN_PUBLIC_URL", ""),
                                               os.getenv("PORTAL_PUBLIC_URL", ""))),
)

@app.get("/api/health")
def health():
    """Liveness check for the container healthcheck. Needs no auth.

    Returns:
        `"OK"`.
    """
    return "OK"

app.include_router(auth.router)
app.include_router(schedule.router)
app.include_router(volunteers.router)
app.include_router(users.router)
app.include_router(email_keys.router)
app.include_router(setup.router)
app.include_router(events.router)
app.include_router(radios.router)
app.include_router(portal_admin.router)
app.include_router(passkeys.router)
app.include_router(invites.router)
app.include_router(password_reset.router)
app.include_router(checkin.router)
app.include_router(api_tokens.router)
app.include_router(export.router)
app.include_router(email_template.router)
app.include_router(portal_content.router)
app.include_router(audit_log.router)

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(prog="Conference Scheduler")
    parser.add_argument("-d", help="Debug logging", action="store_true")
    parser.add_argument("--audit", help="Audit log path (JSON lines)",
                        default="/app/logs/audit.jsonl")

    args = parser.parse_args()
    audit.configure(args.audit, to_db=True)

    # Without this the root logger has no handler, so Python's last-resort handler emits
    # WARNING and above only — every log.info() in this application is silently discarded,
    # including the portal sync loop's startup and per-cycle lines. uvicorn's log_level below
    # configures uvicorn's own loggers and not the root, so this is not redundant with it.
    logging.basicConfig(
        level=logging.DEBUG if args.d else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # httpx logs every request at INFO, which is the portal sync's heartbeat twice a tick.
    # portal_sync reports what matters itself: a warning when the portal stops answering,
    # and what each cycle pushed or pulled. -d brings the per-request lines back.
    if not args.d:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    tmp = _db.open_db()
    app_sec   = _db.get_app_kv(tmp, 'app_secret', secrets.token_hex)
    app_title = _db.get_app_kv(tmp, 'app_title') or 'Conference Scheduler'
    tmp.close()
    app.title = app_title

    _main.session_secret = app_sec
    app.add_middleware(SessionMiddleware, secret_key=app_sec)

    # timeout_graceful_shutdown: open /api/events SSE streams never finish, so without it a
    # stop waits for them until podman's 10s SIGKILL. Clients reconnect on their own.
    # host: all interfaces inside the container; compose.yml decides what reaches it.
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="debug" if args.d else "info",  # nosec B104
                timeout_graceful_shutdown=3)
