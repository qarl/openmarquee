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
from typing import Any, Callable

from openmarquee.auth import AuthStorage, verify_token

log = logging.getLogger(__name__)

# Exact-match paths that bypass auth. Everything else falls through to
# the bearer check (with prefix-match for /static/, /fonts/,
# /api/flock/asset/).
_WHITELIST_EXACT: frozenset[str] = frozenset({
    "/healthz",
    "/",
    # UI shells served by the StaticFiles mount. These are inert HTML
    # pages -- the actual editor only opens after main.js fetches the
    # token-gated /api endpoints, which still require auth. (20.2: the
    # captive-portal phone flow needs to reach welcome / login / set-
    # password BEFORE the operator has a token; the StaticFiles mount
    # exposes them with the literal .html paths.)
    "/welcome",          # 20.1: kept for /welcome (no .html) form
    "/welcome.html",     # 20.2: actual served path
    "/login",
    "/login.html",
    "/set-password.html",
    "/index.html",
    "/spike.html",       # ffmpeg-wasm spike page, exercised by smoke.spec.js
    "/styles.css",       # main stylesheet referenced by index.html
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
})

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
    # Peer-fetch route: /api/flock/asset/<UUID>/<filename>. Same
    # tailnet-ACL rationale as the flock list above.
    "/api/flock/asset/",
)


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
        state = self.auth_resolver().load()
        if not verify_token(token, state):
            await _send_401(send, state is None)
            return

        await self.app(scope, receive, send)


def _is_whitelisted(path: str) -> bool:
    if path in _WHITELIST_EXACT:
        return True
    return any(path.startswith(p) for p in _WHITELIST_PREFIX)


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
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ],
    })
    await send({"type": "http.response.body", "body": body})
