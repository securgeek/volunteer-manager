"""The Audit Log (Admin → Audit Log): reading audit_events, where alerts go, a test alert.

Every organizer may read it. Who gets the alert emails is separate: `users.audit_alerts`,
which any organizer sets in Admin → Users (`POST /api/users/set_audit_alerts`, in
routers/users.py). It may be nobody; alerts are then only recorded, and the status here says
so for the page to show in red.

| Method | Path | |
|---|---|---|
| GET  | `/api/admin/audit` | events, newest first, filtered, paged by `before_id` |
| GET  | `/api/admin/audit/status` | who alerts go to, the last alert and failure |
| POST | `/api/admin/audit/test_alert` | mail a test alert to every recipient now |
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import alerts
import audit as audit_mod
import portal_sync
from audit import audit
from db import PgClient, RoleChecker, get_db
from ratelimit import SlidingWindow

router = APIRouter()

SEVERITY_ORDER = ("info", "low", "medium", "high", "critical")
PAGE_MAX = 200
TEST_ALERT_EVERY_SEC = 60
_test_alerts = SlidingWindow(1, TEST_ALERT_EVERY_SEC)

# Every filter is a bound flag: '' means "not filtering on this". LIKE patterns come from
# _like(), which escapes the user's % _ and \.
_EVENTS_SQL = (
    "SELECT id, to_char(ts, 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS ts, source, event, "
    "severity, actor, user_id, ip, method, path, message, data "
    "FROM audit_events "
    "WHERE (? = '' OR event LIKE ?) "
    "AND severity = ANY(?::text[]) "
    "AND (? = '' OR actor ILIKE ?) "
    "AND (? = '' OR ip LIKE ?) "
    "AND (? = '' OR source = ?) "
    "AND (? = '' OR message ILIKE ?) "
    "AND (?::bigint IS NULL OR id < ?) "
    "ORDER BY id DESC LIMIT ?;"
)
_ALERTED_SQL = (
    "SELECT username, (email IS NOT NULL AND email <> '') AS has_email FROM users "
    "WHERE audit_alerts AND role = 'organizer' ORDER BY username;"
)
_LAST_EVENT_SQL = (
    "SELECT to_char(ts, 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') AS ts, message, data "
    "FROM audit_events WHERE event = ? ORDER BY id DESC LIMIT 1;"
)


def _like(text: str, prefix_only: bool = False) -> str:
    """A LIKE pattern matching `text` literally: as a prefix, or anywhere."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return esc + "%" if prefix_only else "%" + esc + "%"


@router.get("/api/admin/audit")
def list_events(
    request: Request,
    event: str = Query(default="", max_length=64, description="Event code prefix, e.g. auth."),
    min_severity: str = Query(default="info", description="info, low, medium, high, critical"),
    actor: str = Query(default="", max_length=100),
    ip: str = Query(default="", max_length=64, description="Address prefix"),
    source: str = Query(default="", description="backend or portal; blank for both"),
    q: str = Query(default="", max_length=100, description="Text in the message"),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=PAGE_MAX),
    s_data: dict = Depends(RoleChecker("organizer")),
    db: PgClient = Depends(get_db),
):
    """Audit events, newest first.

    Args:
        request: The request, for auditing the read itself.
        event: Only codes starting with this.
        min_severity: Only this severity and above.
        actor: Only actors containing this (case-insensitive).
        ip: Only addresses starting with this.
        source: Only this source.
        q: Only messages containing this (case-insensitive).
        before_id: Only events older than this id (the previous page's `next_before_id`).
        limit: At most this many.
        s_data: The caller's session; organizers only.
        db: The request's database connection.

    Returns:
        `{"events": [...], "next_before_id": id or None}`.

    Raises:
        HTTPException 400: An unknown severity or source.
    """
    if min_severity not in SEVERITY_ORDER:
        raise HTTPException(status_code=400, detail="Unknown severity")
    if source not in ("", "backend", "portal"):
        raise HTTPException(status_code=400, detail="Unknown source")
    levels = list(SEVERITY_ORDER[SEVERITY_ORDER.index(min_severity):])
    rows = db.execute(_EVENTS_SQL, (
        event, _like(event, prefix_only=True), levels, actor, _like(actor),
        ip, _like(ip, prefix_only=True), source, source, q, _like(q),
        before_id, before_id, limit)).fetchall()
    if before_id is None:
        audit("audit.view", request, request.session.get("session_id"), "Opened the audit log",
              filters={k: v for k, v in (("event", event), ("min_severity", min_severity),
                                         ("actor", actor), ("ip", ip), ("source", source),
                                         ("q", q)) if v and v != "info"})
    return {"events": rows,
            "next_before_id": rows[-1]["id"] if len(rows) == limit else None,
            "viewer": s_data["username"]}


@router.get("/api/admin/audit/status")
def alert_status(s_data: dict = Depends(RoleChecker("organizer")), db: PgClient = Depends(get_db)):
    """Where alerts go and whether they're getting there.

    Args:
        s_data: The caller's session; organizers only.
        db: The request's database connection.

    Returns:
        `alerted` ({username, has_email}: the organizers set to get alerts), `recipients`
        (how many of them alerts actually reach: those with an address), `portal_configured`,
        `retention_days`, `last_alert` and `last_failure` ({ts, message, data} or None).
    """
    alerted = db.execute(_ALERTED_SQL).fetchall()
    return {
        "alerted": alerted,
        "recipients": sum(1 for h in alerted if h["has_email"]),
        "portal_configured": portal_sync.configured(),
        "retention_days": audit_mod.retention_days(),
        "last_alert": db.execute(_LAST_EVENT_SQL, ("alert.fired",)).fetchone(),
        "last_failure": db.execute(_LAST_EVENT_SQL, ("alert.mail_fail",)).fetchone(),
        "you": s_data["username"],
    }


@router.post("/api/admin/audit/test_alert")
async def test_alert(request: Request, s_data: dict = Depends(RoleChecker("organizer"))):
    """Mail a test alert to everyone alerts go to, now, and say how many it reached.

    Goes the whole way a real alert does (portal, relay, inbox), without the retry, and at
    most once a minute.

    Args:
        request: The request, for the audit log.
        s_data: The caller's session; organizers only.

    Returns:
        `{"reached": n, "recipients": n}`.

    Raises:
        HTTPException 429: Another test in the last minute.
    """
    if not _test_alerts.allow("test"):
        raise HTTPException(status_code=429, detail="One test alert a minute. Try again shortly.",
                            headers={"Retry-After": str(_test_alerts.retry_after("test"))})
    audit("alert.test", request, request.session.get("session_id"), "Sent a test alert")
    alert = alerts.Alert("test", "Test alert from the audit log page", "low", [
        ("Sent by", s_data["username"]),
        ("Why", "Checking that security alerts reach you. Nothing has happened."),
    ])
    people = len(await asyncio.to_thread(alerts.recipients))
    reached = await alerts.deliver(alert, retry=False)
    return {"reached": reached, "recipients": people}
