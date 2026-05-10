"""ASGI middleware that logs per-request route + duration (Batch 6.1).

Lightweight: wraps the next ASGI app, stamps a perf_counter on each
HTTP request entry, computes elapsed at response end, and pushes the
record into a bounded in-memory ring. The ring is exposed via
`recent_requests()` so `/api/system/perf-stats` can serve the recent
window without a Prometheus scrape dependency.

Why an in-memory ring and not stdout logging only:
  * sweep #2 baseline needs queryable history, not a haystack to grep
  * the device has no persistent log shipper -- stdout goes to
    journald and rotates aggressively
  * 256 entries x ~80 bytes each = ~20 KB; cheap on a Pi Zero 2 W

The middleware also emits a single INFO log line per request so
operators can correlate slow responses with the device's other logs
when the in-memory ring has rolled over.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

# Bounded ring -- the most-recent N requests survive. 256 ≈ a few
# minutes of typical fleet UI poll traffic; sweep baseline capture
# uses ~30s windows so the ring never overflows in practice.
_REQUEST_LOG_MAX = 256
_request_log: deque[dict[str, Any]] = deque(maxlen=_REQUEST_LOG_MAX)


def recent_requests() -> list[dict[str, Any]]:
    """Snapshot of the request ring, oldest-first."""
    return list(_request_log)


def clear_request_log() -> None:
    """Drop the ring. Test hook only."""
    _request_log.clear()


class PerfMiddleware:
    """ASGI middleware. Mount via `app.add_middleware(PerfMiddleware)`.

    Scope-type 'lifespan' and 'websocket' pass through untouched;
    only 'http' scopes are timed.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        start = time.perf_counter()
        status_holder: dict[str, int] = {"code": 0}

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            entry: dict[str, Any] = {
                "method": method,
                "path": path,
                "status": status_holder["code"],
                "duration_ms": round(duration_ms, 3),
            }
            _request_log.append(entry)
            # Only emit INFO for requests over 50ms so the typical
            # sub-ms /api/playback/state poll doesn't drown the log.
            if duration_ms >= 50.0:
                log.info(
                    "perf: %s %s -> %d in %.1fms",
                    method, path, status_holder["code"], duration_ms,
                )
