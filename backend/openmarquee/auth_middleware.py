"""ASGI middleware that gates HTTP requests on a valid bearer token
(Batch 20.1 / phase A.1).

Mounted AFTER PerfMiddleware (so perf timing still wraps auth-rejected
requests; useful for "how often are we 401-ing"). Mounted BEFORE the
FastAPI router so unauth requests never reach the endpoints.

Whitelist:
  - /healthz                  (oncall + deploy health gate)
  - / + /welcome + /login     (UI shells; the actual UI loads the
                              welcome/login forms unauthenticated and
                              hits /api/auth/* directly)
  - /static/*                 (built UI assets; safe to serve)
  - /fonts/*                  (font files referenced by styles.css)
  - /api/auth/status          (no-op probe used to pick which screen)
  - /api/auth/set-password    (welcome-flow first-time set)
  - /api/auth/login           (login flow)
  - Peer-callable flock endpoints (the spec requires unauth peer-to-
    peer sync via Tailscale magic-DNS; tailnet ACLs are the auth
    layer there):
      /api/flock/manifest
      /api/flock/notify
      /api/flock/sync-announce
      /api/flock/hello
      /api/flock/asset/*  (peer fetches the actual content bytes)

Everything else requires `Authorization: Bearer <token>` with a token
matching the current AuthState.token_version.

If auth is NOT configured (AuthState is None), every NON-whitelist
request returns 401 with `detail="authentication required"` -- the UI
should redirect to /welcome instead of hitting protected endpoints
before the operator picks a password.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

from openmarquee.auth import AuthStorage, verify_token

log = logging.getLogger(__name__)

# Exact-match paths that bypass auth. Everything else falls through to
# the bearer check (with prefix-match for /static/, /fonts/,
# /api/flock/asset/).
_WHITELIST_EXACT: frozenset[str] = frozenset(
    {
        "/healthz",
        "/",
        # UI shells served by the StaticFiles mount. These are inert HTML
        # pages -- the actual editor only opens after main.js fetches the
        # token-gated /api endpoints, which still require auth. (20.2: the
        # captive-portal phone flow needs to reach welcome / login / set-
        # password BEFORE the operator has a token; the StaticFiles mount
        # exposes them with the literal .html paths.)
        "/welcome",  # 20.1: kept for /welcome (no .html) form
        "/welcome.html",  # 20.2: actual served path
        "/login",
        "/login.html",
        "/set-password.html",
        "/index.html",
        "/spike.html",  # ffmpeg-wasm spike page, exercised by smoke.spec.js
        "/styles.css",  # main stylesheet referenced by index.html
        "/favicon.ico",
        "/api/auth/status",
        "/api/auth/set-password",
        "/api/auth/login",
        # Peer-callable flock endpoints -- the spec authorizes them via
        # Tailscale ACL, not bearer token (a peer device doesn't have
        # the local operator's password).
        "/api/flock/manifest",
        "/api/flock/notify",
        "/api/flock/sync-announce",
        "/api/flock/hello",
        # Bug 5 (qarl 2026-05-16): cross-flock readable thumbnails. The
        # flock panel on each device renders one tile per peer with a
        # live PNG of what that peer is currently playing — fetched
        # directly from the peer's /api/playback/current-thumbnail
        # (cover art) or /current-frame (live composite). The peer
        # endpoint already implements an Origin allowlist (see
        # cors_headers_for_origin + the operator's flock-membership
        # list in api_playback.py); that CORS allowlist + tailnet
        # ACL together are the auth boundary for cross-flock reads.
        # Header-bearer auth doesn't apply: our local operator's
        # token isn't signed against a peer's AuthState.
        "/api/playback/current-thumbnail",
        "/api/playback/current-frame",
    }
)

# Prefix-match paths. Order doesn't matter; first-match wins on the
# any() iteration below.
_WHITELIST_PREFIX: tuple[str, ...] = (
    "/static/",
    "/fonts/",
    # 20.2: esbuild's output dir, mounted under the StaticFiles UI
    # root. Contains main.js + welcome.js + login.js + set-password.js +
    # ffmpeg-worker.js + chunk-*.js. The browser fetches these BEFORE
    # main.js can attach an Authorization header; gating them would
    # 401 the editor itself on every load.
    "/dist/",
    # Live-takeover test harness — ui/test/fake-camera.html + the
    # bundled fixture.mp4 (Phase 12.x dispatch). Same auth shape as
    # /dist/: the HTML loads unauthenticated, then its inline JS reads
    # the operator's existing bearer token from localStorage and uses
    # it on the /api/live/* calls. The page itself exposes no secrets.
    # Naturally absent from a production image — ui/dist/ ships, ui/
    # /test/ doesn't, so this prefix has no security cost there.
    "/test/",
    # Peer-fetch route: /api/flock/asset/<UUID>/<filename>. Same
    # tailnet-ACL rationale as the flock list above.
    "/api/flock/asset/",
)

# Bug 3+4 (qarl 2026-05-16): media routes that the operator's browser
# reaches via bare `<img src>` / `<video src>` tags. Those tags do NOT
# carry an Authorization header (no fetch wrapper to inject one), so
# the strict-bearer gate 401s every thumbnail + slide-asset request
# in the dashboard. These routes ALSO accept a `?token=...` query-
# string fallback (the UI helper appends it client-side). Header
# auth still works; query-param is the additive escape hatch.
#
# Security note: tokens in URLs leak to browser history, server logs,
# proxy-cache stores, and Referer headers on outgoing clicks. The
# threat surface widened on 2026-05-24 when Path A (commit 9ed1bc3)
# made the token a real argon2-verified secret -- pre-Path-A, the
# token's "secret" half didn't matter (the version prefix was the
# only gate), so leakage was inert. Post-Path-A:
#
#   * `Cache-Control: no-store` on query-authed responses prevents
#     local + intermediary caching of the URL + body. (2026-05-24
#     security audit MEDIUM finding 3.)
#   * `Referrer-Policy: no-referrer` on query-authed responses
#     prevents the token from leaking when the user clicks an
#     outgoing link in the page chrome around the media tag.
#   * The per-route allowlist (`_is_media_route` + the SUFFIXES
#     tuple below) bounds query-string-auth to binary-blob endpoints
#     only -- list / metadata / settings / auth endpoints all
#     require the Authorization header. This is the original Bug-3+4
#     design; documented here so a future widening surfaces the
#     trade-off rather than silently introducing query-auth on a
#     route that doesn't need it.
#
# If the operator-experience need ever broadens beyond `<img>/<video>`
# tags (e.g., to a route the UI fetches from JavaScript), revisit
# with cookie-based session auth instead of widening this allowlist.
#
# Route shape match: prefix is exact, the rest is `/<uuid>/<suffix>`.
# Suffix gate so we don't accept query-param auth for /api/content
# (list) or /api/content/{id} (metadata) — only the binary blobs.
_MEDIA_ROUTE_PREFIX: str = "/api/content/"
_MEDIA_ROUTE_SUFFIXES: tuple[str, ...] = ("/asset", "/video")


class AuthMiddleware:
    """Bearer-token gate. The whitelist above carves out UI assets +
    the auth endpoints themselves + peer-callable flock endpoints
    that authenticate via Tailscale ACL rather than the operator
    password.
    """

    def __init__(
        self,
        app: Any,
        auth_storage_resolver: Callable[[], AuthStorage],
    ) -> None:
        """`auth_storage_resolver` is called per-request so the
        middleware always reaches the current AuthStorage. Resolving
        eagerly at __init__ time would capture whatever singleton
        existed at app-import time -- which breaks tests that point
        OPENMARQUEE_AUTH_PATH at a tmp dir AFTER the import."""
        self.app = app
        self.auth_resolver = auth_storage_resolver

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # 20.1 test-bypass: OPENMARQUEE_DISABLE_AUTH=1 short-circuits
        # the gate. Mirrors OPENMARQUEE_DISABLE_SEED / DISABLE_DEV /
        # DISABLE_PULL_WORKER / DISABLE_AUTOSTART -- env-gated test
        # affordances so the pre-existing 100+ TestClient suites
        # don't need to mint bearer tokens. Production systemd unit
        # never sets this; the captive-portal threat model assumes
        # the gate is always on.
        if os.environ.get("OPENMARQUEE_DISABLE_AUTH") == "1":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if _is_whitelisted(path):
            await self.app(scope, receive, send)
            return

        token = _bearer_from_headers(scope.get("headers") or [])
        # Bug 3+4 query-param fallback for media routes: bare <img>/<video>
        # src can't carry an Authorization header, so the UI appends
        # ?token=<bearer>. Header auth still wins when both are present;
        # `query_auth_used` then stays False and the response gets no
        # extra hardening (no leakage path -> nothing to harden).
        query_auth_used = False
        if not token and _is_media_route(path):
            query_token = _token_from_query(scope.get("query_string") or b"")
            if query_token:
                token = query_token
                query_auth_used = True
        state = self.auth_resolver().load()
        if not await verify_token(token, state):
            await _send_401(send, state is None)
            return

        # 2026-05-24 security audit MEDIUM finding 3: harden responses
        # that authed via the query-string token fallback. Wrap send to
        # inject Cache-Control: no-store + Referrer-Policy: no-referrer
        # on the response start message. Header-bearer requests skip
        # this wrap -- they have no URL-leak surface to mitigate.
        if query_auth_used:
            send = _wrap_send_no_store_no_referrer(send)

        await self.app(scope, receive, send)


def _is_whitelisted(path: str) -> bool:
    if path in _WHITELIST_EXACT:
        return True
    return any(path.startswith(p) for p in _WHITELIST_PREFIX)


def _is_media_route(path: str) -> bool:
    """True when `path` matches `/api/content/<uuid>/<suffix>` for a
    suffix in `_MEDIA_ROUTE_SUFFIXES`. Restricts query-param token auth
    to binary-blob endpoints; metadata + list routes still require the
    Authorization header."""
    if not path.startswith(_MEDIA_ROUTE_PREFIX):
        return False
    return any(path.endswith(s) for s in _MEDIA_ROUTE_SUFFIXES)


def _token_from_query(query_string: bytes) -> str:
    """Pull `token=<value>` from the URL-encoded query string. Returns
    "" when missing/malformed. Uses urllib.parse so we don't have to
    hand-roll percent-decoding."""
    from urllib.parse import parse_qs

    try:
        decoded = query_string.decode("ascii")
    except UnicodeDecodeError:
        return ""
    values = parse_qs(decoded, keep_blank_values=False).get("token")
    if not values:
        return ""
    return values[0].strip()


def _bearer_from_headers(headers: list[tuple[bytes, bytes]]) -> str:
    """Extract the bearer token from the ASGI header list. Returns ""
    for missing / malformed."""
    for name, value in headers:
        if name.lower() == b"authorization":
            try:
                raw = value.decode("ascii")
            except UnicodeDecodeError:
                return ""
            parts = raw.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
            return ""
    return ""


def _wrap_send_no_store_no_referrer(send: Any) -> Any:
    """Wrap an ASGI `send` so the response start message carries
    Cache-Control: no-store + Referrer-Policy: no-referrer (2026-05-24
    security audit MEDIUM finding 3).

    Applied ONLY to requests that authed via the ?token=... query-
    string fallback. Header-bearer-authed requests bypass the wrap.

    Behavior:
    - On the http.response.start message, strip any pre-existing
      Cache-Control or Referrer-Policy headers and append our values.
      Stripping (rather than just appending) yields a canonical
      single-header response. RFC 7234 §5.2 requires compliant caches
      to honor the most-restrictive directive when multiple Cache-
      Control header lines combine into a comma-list, so a duplicate
      wouldn't actually weaken protection -- but stripping defends
      against misconfigured intermediaries that pick first-or-last
      rather than combining.
    - On http.response.body and other messages, pass through unchanged.
    """

    async def wrapped(message: dict) -> None:
        if message.get("type") == "http.response.start":
            headers = [
                (name, value)
                for (name, value) in (message.get("headers") or [])
                if name.lower() not in (b"cache-control", b"referrer-policy")
            ]
            headers.append((b"cache-control", b"no-store"))
            headers.append((b"referrer-policy", b"no-referrer"))
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


async def _send_401(send: Any, not_configured: bool) -> None:
    """Emit a JSON 401. Different `detail` strings let the UI
    distinguish "you need to log in" from "the device hasn't been
    set up yet"."""
    detail = (
        "authentication not configured -- visit /welcome"
        if not_configured
        else "authentication required"
    )
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
