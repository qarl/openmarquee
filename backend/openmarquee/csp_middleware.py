"""Content-Security-Policy ASGI middleware.

Adds a Content-Security-Policy (or -Report-Only) header to every HTTP
response, including responses produced by inner middleware (e.g. the
AuthMiddleware's 401s). Mounted between PerfMiddleware (outer) and
AuthMiddleware (inner) so the perf ring still measures CSP overhead
and CSP still wraps auth-rejected responses.

The default policy is derived from the openMarquee operator UI's
actual asset shape (audited 2026-05-24):

  - Same-origin scripts only (`'self'`) — no CDN, no analytics.
  - `'wasm-unsafe-eval'` so wasm-bindgen's `import`/`WebAssembly.
    instantiate` path can run (parity harness + future renderer-wasm).
  - `'unsafe-inline'` on style-src because 5 of the served HTML
    shells (login.html, parity-harness.html, set-password.html,
    welcome.html, fake-camera.html) carry inline `<style>` blocks.
    Refactoring out would be 5-file churn for marginal gain — inline
    *style* can't execute script in modern CSPs (the IE-era
    `expression()` attack is dead).
  - `blob:` on img-src + media-src + worker-src because the editor
    uses `URL.createObjectURL` for thumbnails / video previews /
    ffmpeg-worker spawn.
  - `data:` on img-src for tiny favicons + similar inline data URIs.
  - `frame-ancestors 'none'` — we never expect to be iframed.
  - `object-src 'none'` — no Flash/applet attack surface.

Operators can override the policy by passing a custom `policy` arg
to the middleware constructor (the env-var report-only toggle still
applies regardless of which policy is in force).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# Single source of truth for the default policy. Tests + app.py
# both import this constant so the directive set has exactly one
# place to update.
DEFAULT_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'wasm-unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self'; "
    "connect-src 'self' blob:; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class CSPMiddleware:
    """ASGI middleware that stamps a Content-Security-Policy header
    on every HTTP response. Non-HTTP scopes (lifespan, websocket) are
    passed through unchanged.
    """

    def __init__(
        self,
        app: Any,
        policy: str = DEFAULT_CSP_POLICY,
        report_only: bool = False,
    ) -> None:
        self.app = app
        self.policy = policy
        # Pre-encode once at construction so per-request hot path
        # only does a list-append, no string work.
        self._header_name: bytes = (
            b"content-security-policy-report-only" if report_only else b"content-security-policy"
        )
        self._header_value: bytes = policy.encode("ascii")

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_csp(message: dict) -> None:
            if message["type"] == "http.response.start":
                # Copy the headers list -- the inner app may have
                # passed a tuple or borrowed-list, and we must not
                # mutate state the inner code might reuse.
                headers = list(message.get("headers") or [])
                headers.append((self._header_name, self._header_value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_csp)
