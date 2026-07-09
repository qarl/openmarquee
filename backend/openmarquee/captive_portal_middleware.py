"""Redirect OS captive-portal detection probes to the web UI.

Spec §4.2: "When a phone connects to the WiFi network, the OS detects the
captive portal and automatically opens the web UI in a browser." dnsmasq
on ap0 resolves EVERY hostname to the device (address=/#/10.0.0.1), so
the phone's OS captive-portal check — a fixed probe URL per platform —
lands here instead of on the internet. The OS decides "is there a portal?"
from that probe's response:

  * Android  GET /generate_204          expects HTTP 204 (no body)
  * iOS/macOS GET /hotspot-detect.html   expects a "Success" page
  * Windows  GET /connecttest.txt        expects "Microsoft Connect Test"
  * Firefox  GET /canonical.html         expects a fixed redirect/body

If the probe gets ANYTHING other than its expected success response, the
OS concludes a captive portal is present and shows the "Sign in to
network" affordance / auto-opens the portal. Returning a 302 to the web
UI is the reliable, cross-platform trigger (exactly how hotel/airport
wifi works): the OS sees the non-success response AND is handed the page
to open.

The redirect target is the ABSOLUTE AP-gateway URL (http://10.0.0.1/),
not a bare "/". A path-only Location resolves against the *probe* URL, so
the portal would open at http://captive.apple.com/ — functional (wildcard
DNS points it back at us) but the browsing ORIGIN would be a spoofed
public host. Any client-side state the onboarding flow writes (PWA
install, localStorage, a stashed bearer token) would then live under that
throwaway origin and be lost the moment the phone leaves the AP. Pinning
the absolute device IP keeps the origin stable + is RFC1918 so the Fqdn
redirect skips it.

Implemented as a plain ASGI middleware (not BaseHTTPMiddleware) because it
runs on EVERY request but matters for only ~8 rare paths — the pass-through
path must be as cheap as possible on the Pi Zero 2 W (no per-request anyio
task group). Positioned OUTSIDE Auth (probes must not be bearer-gated) and
OUTSIDE Fqdn (a probe carries an EXTERNAL Host like captive.apple.com,
which the Tailscale-FQDN redirect would otherwise rewrite) but INSIDE Perf
(so probes are still timed / counted in the ring).
"""

from __future__ import annotations

import logging
import os

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

# Well-known per-platform captive-portal probe paths. dnsmasq points every
# host at us, so path is a sufficient + robust signal — nothing else on
# the device serves these. Kept as a frozenset for O(1) lookup.
DEFAULT_PROBE_PATHS: frozenset[str] = frozenset(
    {
        # Android / ChromeOS (connectivitycheck.gstatic.com, clients3.google.com)
        "/generate_204",
        "/gen_204",
        # iOS / macOS (captive.apple.com)
        "/hotspot-detect.html",
        "/library/test/success.html",
        # Windows NCSI (www.msftconnecttest.com, www.msftncsi.com)
        "/connecttest.txt",
        "/ncsi.txt",
        # Firefox (detectportal.firefox.com) + NetworkManager
        "/canonical.html",
        "/success.txt",
    }
)

# The captive-portal landing on the AP gateway (SYSTEM_SPEC §4.1: device
# at 10.0.0.1). Points at the unauth wifi-entry onboarding page (P0-1d
# part 2) — a lightweight standalone page, deliberately NOT the heavy
# editor SPA, which the iOS Captive Network Assistant mini-browser renders
# poorly. Absolute (not "/") so the browsing origin stays on the device IP
# rather than the spoofed probe host, keeping any onboarding client-state
# usable off-AP. Env-overridable.
DEFAULT_PORTAL_URL = os.environ.get(
    "OPENMARQUEE_CAPTIVE_PORTAL_URL", "http://10.0.0.1/onboarding.html"
)

_PROBE_METHODS = frozenset({"GET", "HEAD"})


class CaptivePortalMiddleware:
    """302-redirect OS captive-portal probes to the setup web UI.

    Args:
        app: the wrapped ASGI app.
        portal_url: absolute URL the OS should open (default
            ``http://10.0.0.1/``). Injectable for tests / part 2.
        probe_paths: the set of probe paths to intercept (injectable).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        portal_url: str = DEFAULT_PORTAL_URL,
        probe_paths: frozenset[str] = DEFAULT_PROBE_PATHS,
    ) -> None:
        self.app = app
        self._portal_url = portal_url
        self._probe_paths = probe_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Cheapest possible pass-through: only HTTP GET/HEAD to a known
        # probe path is intercepted; everything else goes straight to the
        # wrapped app with no allocation.
        if (
            scope["type"] == "http"
            and scope.get("method") in _PROBE_METHODS
            and scope.get("path") in self._probe_paths
        ):
            # 302 (not 301) so nothing caches the portal detour past
            # onboarding. Absolute Location keeps the origin on the device.
            response = RedirectResponse(url=self._portal_url, status_code=302)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
