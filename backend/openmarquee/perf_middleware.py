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

import contextvars
import logging
import time
import uuid
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

# Bounded ring -- the most-recent N requests survive. 256 ≈ a few
# minutes of typical fleet UI poll traffic; sweep baseline capture
# uses ~30s windows so the ring never overflows in practice.
_REQUEST_LOG_MAX = 256
_request_log: deque[dict[str, Any]] = deque(maxlen=_REQUEST_LOG_MAX)


# 16.3 / sweep #8 A4: per-request correlation id. The middleware
# pushes the incoming X-Request-ID (or a generated 12-char hex) into
# this ContextVar before calling the downstream app; the
# RequestIdLogFilter (below) reads it on every LogRecord so every
# log line emitted while handling a request gets a request_id tag.
# Echoed back to the client as X-Request-ID so phone / UI traces can
# join across the wire.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdLogFilter(logging.Filter):
    """Stamp every LogRecord with `record.request_id` from the
    ContextVar. Attached to root-logger handlers by app.py's
    _configure_logging; the configured format
    `%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s`
    consumes that attribute. Outside a request scope the ContextVar's
    default of "-" renders, so log calls during startup / shutdown
    don't KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def recent_requests() -> list[dict[str, Any]]:
    """Snapshot of the request ring, oldest-first."""
    return list(_request_log)


def clear_request_log() -> None:
    """Drop the ring. Test hook only."""
    _request_log.clear()


def _coerce_request_id(headers: list[tuple[bytes, bytes]]) -> str:
    """Pull an inbound X-Request-ID from the ASGI scope's header list,
    OR mint a fresh 12-char hex id. ASCII-clean range only -- ignore
    weird inputs rather than reflect them back into logs / responses.
    """
    for name, value in headers:
        if name.lower() == b"x-request-id":
            try:
                candidate = value.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            # Keep it tight: alnum + dash, up to 64 chars. Reject
            # anything that smells like an injection vector before it
            # reaches a log handler.
            if 1 <= len(candidate) <= 64 and all(c.isalnum() or c == "-" for c in candidate):
                return candidate
    return uuid.uuid4().hex[:12]


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

        # 16.3: per-request correlation id. Pull from inbound
        # X-Request-ID header or mint a new one; pushed into the
        # ContextVar so any log emitted during this request gets
        # tagged (via RequestIdLogFilter), and echoed back in the
        # response so the caller can correlate.
        request_id = _coerce_request_id(scope.get("headers") or [])
        token = request_id_var.set(request_id)

        async def send_wrapper(message: dict) -> None:
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status", 0)
                # Inject X-Request-ID into the outbound headers.
                # Headers list is a mutable list of (bytes, bytes)
                # tuples in ASGI; safe to append.
                headers = list(message.get("headers") or [])
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
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
                "request_id": request_id,
            }
            _request_log.append(entry)
            # Only emit INFO for requests over 50ms so the typical
            # sub-ms /api/playback/state poll doesn't drown the log.
            if duration_ms >= 50.0:
                log.info(
                    "perf: %s %s -> %d in %.1fms [%s]",
                    method,
                    path,
                    status_holder["code"],
                    duration_ms,
                    request_id,
                )
            request_id_var.reset(token)
