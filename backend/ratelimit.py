"""In-memory sliding-window limits, for the backend's single worker.

`SlidingWindow` counts events per key over a window and says whether a key is over its
limit. Password sign-in (`routers/auth.py`) and password reset (`routers/password_reset.py`)
use it. Counts live in process memory: a restart forgets them, which is fine for limits
measured in minutes, and correct only because the backend runs one uvicorn worker.

Keys are whatever the caller builds (`"ip:…"`, `"user:…"`, `"user-ip:…"`). Pass an IP
through `api_auth.rate_key` first, so an IPv6 client counts by its /64.
"""
import threading
import time
from collections import defaultdict, deque

# The clock. A name of its own so tests can move it without touching time.monotonic itself.
_monotonic = time.monotonic


class SlidingWindow:
    """At most `limit` events per key in any `window_sec` seconds.

    Args:
        limit: Events allowed per key per window.
        window_sec: The window's length in seconds.
    """

    def __init__(self, limit: int, window_sec: float):
        self.limit = limit
        self.window_sec = window_sec
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._hits[key]
        while q and q[0] <= now - self.window_sec:
            q.popleft()
        if not q:
            del self._hits[key]          # keep the dict from growing with one-off keys
            return deque()
        return q

    def over(self, *keys: str) -> bool:
        """Whether any of `keys` is already at its limit. Records nothing.

        Args:
            *keys: The keys to check.

        Returns:
            True if at least one key has `limit` events in the current window.
        """
        now = _monotonic()
        with self._lock:
            return any(len(self._trim(k, now)) >= self.limit for k in keys)

    def hit(self, *keys: str) -> None:
        """Record one event against each of `keys`.

        Args:
            *keys: The keys to count against.
        """
        now = _monotonic()
        with self._lock:
            for k in keys:
                self._hits[k].append(now)

    def allow(self, *keys: str) -> bool:
        """Check and record in one step: count against every key only if none is over.

        Args:
            *keys: The keys to check and count against.

        Returns:
            False if any key is at its limit (nothing recorded), else True.
        """
        now = _monotonic()
        with self._lock:
            if any(len(self._trim(k, now)) >= self.limit for k in keys):
                return False
            for k in keys:
                self._hits[k].append(now)
        return True

    def retry_after(self, *keys: str) -> int:
        """Seconds until every one of `keys` is under its limit again (0 if already).

        Args:
            *keys: The keys to check.

        Returns:
            Whole seconds, rounded up.
        """
        now = _monotonic()
        wait = 0.0
        with self._lock:
            for k in keys:
                q = self._trim(k, now)
                if len(q) >= self.limit:
                    # The oldest event that must expire to bring the count under the limit.
                    wait = max(wait, q[len(q) - self.limit] + self.window_sec - now)
        return int(wait) + (1 if wait % 1 else 0)

    def clear(self, *keys: str) -> None:
        """Forget `keys`, or every key when none is given.

        Args:
            *keys: The keys to forget.
        """
        with self._lock:
            if not keys:
                self._hits.clear()
            for k in keys:
                self._hits.pop(k, None)
