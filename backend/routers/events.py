"""Server-sent events: tell open organizer browsers that something changed.

`GET /api/events` is an SSE stream. Endpoints call `broadcast("schedule")`, `"volunteers"`,
`"checkin"` and so on after a change, and each browser refetches what it shows. Events that
change what the portal holds also trigger a portal push.

Endpoints are mostly synchronous and run in a thread pool, so `broadcast` hands the event to
the event loop with `call_soon_threadsafe`. `set_loop` gives it that loop at startup.
"""
import asyncio
import threading
from dataclasses import dataclass

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

router = APIRouter()

_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()


@dataclass
class _EventsState:
    """Module state: the running event loop, once set_loop has been called."""

    loop: asyncio.AbstractEventLoop | None = None


_state = _EventsState()


def set_loop(loop: asyncio.AbstractEventLoop):
    """Store the running event loop so sync endpoints can schedule broadcasts.

    Args:
        loop: The app's running event loop.
    """
    _state.loop = loop


# Events that change what the portal holds. "assignment" moves a slot's vol_id, which moves
# band availability; "schedule" changes the structure itself. The rest — radio checkouts,
# ratings, check-ins, slot notes — are scheduler-only data the portal never receives.
_PORTAL_EVENTS = frozenset({"schedule", "assignment"})


def broadcast(event: str = "schedule"):
    """Push a named event to all connected SSE clients. Safe to call from sync endpoints.

    This is also where the portal push is triggered. Hooking it here rather than at each of
    the twenty-odd mutation sites means a new mutation cannot forget to push: if it tells open
    browsers that something changed, it tells the portal too.

    Args:
        event: The event name the browsers listen for.
    """
    if event in _PORTAL_EVENTS:
        # Imported lazily so this module stays importable without the sync client, and
        # debounced on the other side, so applying a grid of forty slot edits pushes once.
        import portal_sync
        portal_sync.request_push()
    if _state.loop is None:
        return
    with _lock:
        for q in list(_subscribers):
            _state.loop.call_soon_threadsafe(q.put_nowait, event)


@router.get("/api/events")
async def sse_events():
    """SSE endpoint; each connected client receives a push whenever schedule state changes."""
    queue: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.add(queue)

    async def generator():
        try:
            while True:
                event = await queue.get()
                yield {"event": event, "data": ""}
        finally:
            # Runs on disconnect too: the CancelledError that ends the stream passes through.
            with _lock:
                _subscribers.discard(queue)

    return EventSourceResponse(generator())
