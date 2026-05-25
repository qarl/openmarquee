"""ASGI body-cap middleware: 413 oversized POST/PUT bodies before parsing.

2026-05-25 Bundle B2 piece 3: closes B1's known gap. Pydantic's
`Field(max_length=N)` from B1 (api.py base64 caps) protects against
downstream cost (b64decode RAM 2nd copy, DB blob write of a giant
blob) but does NOT prevent the upload-RAM-exhaustion DoS:
Starlette's JSON parser calls `await request.body()` which buffers
the ENTIRE request body in memory BEFORE Pydantic sees the field.
A hostile 1 GB POST allocates 1 GB before the 422 fires.

This middleware sits OUTERMOST in the ASGI stack and rejects with
413 Payload Too Large BEFORE any body bytes are read, based on the
declared Content-Length header. Together with B1's max_length caps,
this provides layered defense at distinct gates:

  ASGI middleware (this file): 413 at Content-Length read, no body
    bytes buffered yet -> RAM-DoS bound

  Pydantic Field(max_length=N) (api.py): 422 at field validation,
    body already buffered (but bounded by the ASGI gate above) ->
    downstream-cost bound

Per-route ceilings are sized by content type. Routes not in the
map get a small default cap; the captive-portal device only
serves a small known set of POST endpoints + the existence of an
unauth POST surface should NOT permit large bodies.

Caveats:

- Content-Length absent: rejected with 413 (chunked-transfer
  encoding). The captive-portal UI uses fetch() which always sets
  Content-Length on JSON bodies; streaming uploads are not a
  supported shape. Without this fail-closed behavior, a hostile
  client could send a chunked-transfer POST and bypass the gate
  entirely.

- Content-Length spoofed lower than actual body: the actual body
  read still happens in Starlette; the spoof would let a body
  larger than the cap through. Starlette's body buffer is itself
  bounded by httpx/h11 per-message limits, so the practical
  exploit window is small. A future hardening pass could enforce
  by COUNTING bytes during read instead of trusting the header --
  out of scope for B2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Per-route Content-Length ceilings, in BYTES. Sized to match B1's
# Pydantic Field max_length values + small JSON envelope overhead.
# Routes not matching any prefix get DEFAULT_CAP.
ROUTE_BODY_CAPS: dict[str, int] = {
    # B1: TextSlideUpload.png_base64 max_length = 14_000_000 bytes of
    # b64 string. Body is JSON envelope around that field + a few
    # other small fields; 15 MB cap leaves comfortable headroom.
    "/api/content/text-slides": 15 * 1024 * 1024,
    # B1: ImageSlideUpload.image_base64 max_length = 27_000_000.
    "/api/content/images": 28 * 1024 * 1024,
    # B1: VideoSlideUpload has BOTH png_base64 (14 MB cap) +
    # mp4_base64 (270 MB cap); combined max body ~285 MB. Round to
    # 290 MB.
    "/api/content/videos": 290 * 1024 * 1024,
}

# Default cap for unlisted routes. 1 MB is plenty for the captive-
# portal's JSON request shapes (login, settings PATCH, flock add,
# etc.) -- typically <1 KB. The cap kicks in only when an unauth
# route gets a multi-MB POST, which is itself anomalous traffic.
DEFAULT_CAP_BYTES: int = 1 * 1024 * 1024


def _cap_for_path(path: str) -> int:
    """Return the body-size cap (bytes) for the request path. Exact
    match or `prefix + "/"`; a bare startswith would let a future
    `/api/content/images-archive` silently inherit the images cap."""
    for prefix, cap in ROUTE_BODY_CAPS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return cap
    return DEFAULT_CAP_BYTES


def _content_length_from_headers(headers: list[tuple[bytes, bytes]]) -> int | None:
    """Return the Content-Length value as int, or None if absent /
    unparseable / negative."""
    for name, value in headers:
        if name.lower() == b"content-length":
            try:
                length = int(value.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                return None
            return length if length >= 0 else None
    return None


class BodyCapMiddleware:
    """ASGI middleware that 413s oversized request bodies before any
    body bytes are read. Sits outermost in the stack so the gate
    fires before AuthMiddleware (no point arguing auth on a request
    we're about to throw away) and before any handler.

    Only inspects POST / PUT / PATCH (the only methods that carry a
    body openMarquee accepts). GET / HEAD / DELETE / OPTIONS pass
    through unchanged.
    """

    _BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "").upper()
        if method not in self._BODY_METHODS:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        cap = _cap_for_path(path)
        content_length = _content_length_from_headers(scope.get("headers") or [])
        # Fail-closed: absent / unparseable Content-Length is treated as
        # "exceeds the cap" so chunked-transfer encoding cannot bypass
        # the gate. The captive-portal UI always sets Content-Length on
        # JSON request bodies, so this is operator-invisible in normal
        # operation.
        if content_length is None or content_length > cap:
            await _send_413(send, path=path, cap=cap, declared=content_length)
            return
        await self.app(scope, receive, send)


async def _send_413(
    send: Callable[[dict], Awaitable[None]],
    *,
    path: str,
    cap: int,
    declared: int | None,
) -> None:
    """Emit a JSON 413 with a clear operator-debugging message."""
    import json

    if declared is None:
        detail = (
            f"request body without Content-Length is rejected by the "
            f"{cap}-byte cap for {path} (chunked-transfer not supported)"
        )
    else:
        detail = f"request body of {declared} bytes exceeds the {cap}-byte cap for {path}"
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
