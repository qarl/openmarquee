"""301-redirect non-FQDN requests to the canonical Tailscale HTTPS URL.

Solves the Chrome secure-context problem: `navigator.mediaDevices` is
only available on HTTPS, `localhost`, and IP literals over plain HTTP.
Operators who type `http://fireplacesign/` (short MagicDNS name) hit
plain-HTTP-on-hostname, the API is undefined, and the Live panel's
camera-unavailable banner fires. The fix is a 301 to
`https://fireplacesign.tail-abc123.ts.net/` -- Tailscale's
`tailscale serve` (provisioned by `system/openmarquee-tailscale.sh`)
terminates TLS with a Let's Encrypt cert for that FQDN, the browser
accepts it as a secure context, and getUserMedia works.

The redirect runs as middleware (not a route handler) so it fires for
every URL the operator might type, not just `/`. Positioned between
PerfMiddleware and AuthMiddleware in app.py: Perf still timestamps the
301 (observability on "how often is the short-name hit?"), Auth does
NOT run (no point hashing a bearer token against a request we're
throwing away).

The redirect is conditional -- a comprehensive skip-list keeps every
path that already satisfies Chrome's secure-context rule from getting
unnecessarily redirected:

1. Tailscale not available (no FQDN target) -- defensive no-op.
2. tailscale_https_enabled = False -- operator opt-out.
3. localhost / 127.0.0.1 / ::1 -- dev / loopback.
4. Hostname IS the FQDN already -- already on the canonical URL.
5. RFC1918 private ranges (10.x, 172.16-31.x, 192.168.x) --
   captive-portal AP IP (10.0.0.1) + home-LAN IPs. Chrome treats
   these as secure contexts, so redirecting would be net-negative UX
   (loses the friendly LAN URL the operator already has open).
6. Tailscale CGNAT range (100.64.0.0/10) -- tailnet IP literal, also
   a Chrome secure context.

The FQDN resolver + settings resolver are both injected so tests can
fake them without an actual Tailscale daemon or settings file.
"""

import ipaddress
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp

log = logging.getLogger(__name__)


class _SettingsLike(Protocol):
    tailscale_https_enabled: bool


# Tailscale's CGNAT carrier-grade range -- the 100.64.0.0/10 block
# Tailscale uses for tailnet IPs. Operators occasionally reach the
# device via its tailnet IP literal (instead of the FQDN); those
# requests already satisfy Chrome's secure-context rule (IP literal)
# so skip the redirect.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# Localhost variants the redirect should pass through. IP literals are
# handled by the `_is_private_or_loopback_ip` check below; we list the loopback
# string forms explicitly here because `ipaddress` won't parse them.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


class FqdnRedirectMiddleware(BaseHTTPMiddleware):
    """301-redirect to the canonical Tailscale FQDN HTTPS URL.

    Args:
        app: ASGI app to wrap.
        fqdn_resolver: Async callable returning the device's own
            Tailscale FQDN (or None when Tailscale isn't up). In
            production, this is `openmarquee._tailscale_self.
            get_self_fqdn`. Tests pass a fake returning a fixed string
            (or None to exercise the "Tailscale down" skip).
        settings_resolver: Sync callable returning a settings object
            with a `tailscale_https_enabled` bool attribute. Pattern
            matches AuthMiddleware's `auth_storage_resolver` -- per-
            request lookup so test settings changes take effect
            without restarting the app.
    """

    def __init__(
        self,
        app: ASGIApp,
        fqdn_resolver: Callable[[], Awaitable[str | None]],
        settings_resolver: Callable[[], _SettingsLike],
    ) -> None:
        super().__init__(app)
        self._fqdn_resolver = fqdn_resolver
        self._settings_resolver = settings_resolver

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        host = (request.url.hostname or "").lower()
        if not host:
            # No Host header -- can't decide. Defer to the app; AuthMiddleware
            # may 401 or the route may 400, but the redirect doesn't fire.
            return await call_next(request)

        # Skip-list: anything Chrome already treats as secure-context, or
        # cases where redirect would be wrong / impossible.
        if host in _LOOPBACK_HOSTNAMES or _is_private_or_loopback_ip(host):
            return await call_next(request)

        try:
            settings = self._settings_resolver()
        except Exception:
            # Settings unavailable (e.g. during a re-load race) -- be
            # defensive and pass through rather than 301-loop the operator.
            log.exception("FqdnRedirectMiddleware: settings_resolver failed")
            return await call_next(request)

        if not getattr(settings, "tailscale_https_enabled", True):
            return await call_next(request)

        try:
            fqdn = await self._fqdn_resolver()
        except Exception:
            # Defense in depth: production get_self_fqdn already swallows
            # subprocess errors and returns None, but if a custom resolver
            # raises (e.g. in test scaffolding gone wrong), pass through
            # rather than 500 the operator's request.
            log.exception("FqdnRedirectMiddleware: fqdn_resolver failed")
            return await call_next(request)
        if not fqdn:
            # Tailscale not up yet (or HTTPS not provisioned). Can't
            # redirect without a target.
            return await call_next(request)

        if host == fqdn:
            # Already on the canonical URL. Tailscaled stripped TLS and
            # proxied the request to us with Host preserved.
            return await call_next(request)

        # Build the redirect target. Preserve path + query verbatim; force
        # scheme=https + the canonical FQDN host. Strip any explicit port
        # the original URL had -- the canonical URL uses the default 443.
        target_path = request.url.path
        target_query = request.url.query
        target = f"https://{fqdn}{target_path}"
        if target_query:
            target = f"{target}?{target_query}"
        return RedirectResponse(url=target, status_code=301)


def _is_private_or_loopback_ip(host: str) -> bool:
    """True for RFC1918, loopback IPs, or Tailscale CGNAT — all of which
    are Chrome secure contexts and should pass through the redirect.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_private:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _TAILSCALE_CGNAT
