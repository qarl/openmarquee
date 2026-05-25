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
#
# `report-uri /api/system/csp-report` (2026-05-25 Bundle A item 3)
# lights up future CSP violations in the device journal instead of
# leaving them visible only in a connected browser's devtools. The
# endpoint is unauth-whitelisted in auth_middleware.py + logs the
# report body at WARNING via api_system.py's POST handler. No
# persistence -- journald rotation handles retention.
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
    "form-action 'self'; "
    "report-uri /api/system/csp-report"
)

# 2026-05-25 Bundle A item 8: response-header defense-in-depth.
#
# X-Content-Type-Options: nosniff blocks browser MIME-sniffing that
# could reinterpret a same-origin response (e.g., a JSON blob with
# user-supplied content) as an executable type. We always set
# Content-Type explicitly, so this is belt-and-suspenders.
#
# Referrer-Policy: strict-origin-when-cross-origin prevents
# operator-visible URLs (which may contain content IDs or other
# session-meaningful path components) from leaking to off-origin
# destinations on click-through. Same-origin navigation still gets
# the full Referer for in-app analytics affordances.
#
# These two never need per-request variance, so they're a single
# pre-encoded byte tuple appended in the response-start handler.
_DEFENSE_IN_DEPTH_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
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
                # Bundle A item 8: nosniff + Referrer-Policy as
                # DEFAULTS. If an inner middleware or handler already
                # set a value (e.g., AuthMiddleware's query-auth wrap
                # sets Referrer-Policy: no-referrer, which is stricter
                # than our default), preserve it -- downstream wins so
                # specific-case hardening doesn't get clobbered by the
                # outer baseline.
                existing_names = {name.lower() for (name, _) in headers}
                for name, value in _DEFENSE_IN_DEPTH_HEADERS:
                    if name not in existing_names:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_csp)
