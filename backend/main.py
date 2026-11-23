"""The scheduler API's FastAPI app: routers, session middleware and startup.

`app` is what uvicorn serves. Each area of the API is a router in `routers/`. Startup
(`lifespan`) quiets the healthcheck log lines, hands the event loop to the SSE broadcaster in
`routers/events.py`, and starts the portal sync loop when the portal is configured.

Run as a script, it starts uvicorn on port 5000. It takes `-d` for debug logging and `--audit
<path>` for where the audit log goes.
"""
import asyncio
import logging
import secrets
import argparse
from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

import audit
import db as _db
import portal_sync
from routers import (auth, schedule, volunteers, users, email_keys, setup, events, radios,
                    portal_admin, passkeys, invites, password_reset, checkin, api_tokens,
                    export, email_template, portal_content)


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
    yield


app = FastAPI(title="Conference Scheduler", lifespan=lifespan)

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

if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(prog="Conference Scheduler")
    parser.add_argument("-d", help="Debug logging", action="store_true")
    parser.add_argument("--audit", help="Audit log path (JSON lines)",
                        default="/app/logs/audit.jsonl")

    args = parser.parse_args()
    audit.configure(args.audit)

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

    app.add_middleware(SessionMiddleware, secret_key=app_sec)

    # timeout_graceful_shutdown: open /api/events SSE streams never finish, so without it a
    # stop waits for them until podman's 10s SIGKILL. Clients reconnect on their own.
    # host: all interfaces inside the container; compose.yml decides what reaches it.
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="debug" if args.d else "info",  # nosec B104
                timeout_graceful_shutdown=3)
