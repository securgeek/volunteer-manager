"""Volunteer portal — FastAPI application entry point.

A separate application from the scheduler in every sense that matters: its own SQLite store,
its own schema version, its own session handling, and no import from backend/. It reaches the
scheduler's data only through what the scheduler pushes to it over the signed sync API.

Run with a single worker. SQLite has one writer, so a second worker would only manufacture
SQLITE_BUSY for no gain.
"""
import argparse
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

import db
from config import settings
from routers import auth, branding, signup, sync

log = logging.getLogger("portal")


class QuietPolls(logging.Filter):
    """Drop successful polls from the access log: the container healthcheck and the
    scheduler's sync heartbeat (heartbeat + config, every tick). They arrive every few seconds
    and say nothing. A failing one (status >= 400) still logs, as does every other request."""
    PATHS = {"/api/health", "/api/sync/heartbeat", "/api/sync/config"}

    def filter(self, record: logging.LogRecord) -> bool:
        """Decide whether an access-log record is written.

        Args:
            record: One uvicorn access-log record. Its `args` are `(client, method, path,
                version, status)`.

        Returns:
            False for a successful GET of a quiet path, True for everything else.
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
async def lifespan(_app: FastAPI):
    """Open and migrate the database once, before the first request.

    Args:
        _app: The FastAPI app (unused).
    """
    quiet_polls()
    db.configure(settings.db_path)
    log.info("portal ready: db=%s mail=%s", settings.db_path, settings.mail_backend)
    yield


app = FastAPI(title="BSides Volunteer Portal", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.include_router(auth.router)
app.include_router(sync.router)
app.include_router(branding.router)
app.include_router(signup.router)


@app.get("/api/health")
def health():
    """Liveness check for the container healthcheck. Needs no auth.

    Returns:
        `{"ok": True, "service": "portal"}`.
    """
    return {"ok": True, "service": "portal"}


def main():
    """Run the portal under uvicorn. Takes `-d`, `--host` and `--port` (default 5001)."""
    import uvicorn

    parser = argparse.ArgumentParser(description="BSides volunteer portal")
    parser.add_argument("-d", "--debug", action="store_true", help="debug logging")
    # All interfaces inside the container; compose.yml decides what reaches it.
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    # workers is deliberately absent: see the module docstring.
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
