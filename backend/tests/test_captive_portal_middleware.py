"""P0-1d: CaptivePortalMiddleware — redirect OS captive-portal probes to
the web UI so a phone joining the setup AP auto-opens the portal."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from openmarquee.captive_portal_middleware import (
    DEFAULT_PORTAL_URL,
    DEFAULT_PROBE_PATHS,
    CaptivePortalMiddleware,
)


def _isolated_client(*, extra_middleware=None, **mw_kwargs) -> TestClient:
    async def catch_all(request):
        return PlainTextResponse("real-content")

    app = Starlette(routes=[Route("/{rest:path}", catch_all, methods=["GET", "HEAD", "POST"])])
    # extra_middleware are added FIRST (so they end up INNER of
    # CaptivePortal, matching production where CaptivePortal wraps
    # Fqdn/Auth). Each entry is (cls, kwargs).
    for cls, kwargs in extra_middleware or []:
        app.add_middleware(cls, **kwargs)
    app.add_middleware(CaptivePortalMiddleware, **mw_kwargs)
    return TestClient(app)


@pytest.mark.parametrize("probe", sorted(DEFAULT_PROBE_PATHS))
def test_every_probe_path_302s_to_portal(probe):
    client = _isolated_client()
    r = client.get(probe, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == DEFAULT_PORTAL_URL


def test_default_portal_url_is_absolute_ap_gateway_onboarding():
    # Absolute (not "/") keeps the browsing origin on the real device IP
    # rather than the spoofed probe host, and points at the lightweight
    # unauth wifi-entry page (not the heavy SPA) — see the module docstring.
    assert DEFAULT_PORTAL_URL == "http://10.0.0.1/onboarding.html"


def test_head_probe_also_redirects():
    client = _isolated_client()
    r = client.head("/generate_204", follow_redirects=False)
    assert r.status_code == 302


def test_non_probe_path_passes_through():
    client = _isolated_client()
    r = client.get("/api/playback/state", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "real-content"


def test_post_to_probe_path_is_not_redirected():
    # Only the OS's GET/HEAD probes are intercepted; a POST (nothing does
    # this, but be conservative) falls through untouched.
    client = _isolated_client()
    r = client.post("/generate_204", follow_redirects=False)
    assert r.status_code == 200
    assert r.text == "real-content"


def test_portal_url_is_configurable():
    client = _isolated_client(portal_url="http://10.0.0.1/onboarding.html")
    r = client.get("/hotspot-detect.html", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "http://10.0.0.1/onboarding.html"


# --- ordering proofs (the WHOLE POINT of where it sits in the stack) ---


class _StubAuth(BaseHTTPMiddleware):
    """Stands in for AuthMiddleware: 401s everything. Proves CaptivePortal
    runs OUTSIDE auth (a probe must never see a 401)."""

    async def dispatch(self, request, call_next):
        return PlainTextResponse("unauth", status_code=401)


def test_probe_bypasses_auth_but_non_probe_is_gated():
    client = _isolated_client(extra_middleware=[(_StubAuth, {})])
    # Probe → 302: CaptivePortal (outer) intercepts before auth runs.
    assert client.get("/generate_204", follow_redirects=False).status_code == 302
    # Non-probe → 401: proves auth IS live + inner (not a no-op test).
    assert client.get("/api/anything", follow_redirects=False).status_code == 401


def test_probe_is_not_tailscale_rewritten_by_fqdn():
    from openmarquee.fqdn_redirect_middleware import FqdnRedirectMiddleware

    async def fake_fqdn():
        return "sign.tail-abc123.ts.net"

    class _Settings:
        tailscale_https_enabled = True

    client = _isolated_client(
        extra_middleware=[
            (
                FqdnRedirectMiddleware,
                {"fqdn_resolver": fake_fqdn, "settings_resolver": lambda: _Settings()},
            )
        ]
    )
    # A probe carries an EXTERNAL Host; CaptivePortal (outer) must 302 it
    # to the portal, NOT let Fqdn 301 it to the (unreachable-on-AP) FQDN.
    r = client.get(
        "/generate_204",
        headers={"host": "captive.apple.com"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "ts.net" not in r.headers["location"]
    # Sanity: a NON-probe with the same external host DOES get Fqdn's 301,
    # proving Fqdn is active + inner (so the test above really exercises
    # the ordering, not a disabled Fqdn).
    r2 = client.get(
        "/some-page",
        headers={"host": "captive.apple.com"},
        follow_redirects=False,
    )
    assert r2.status_code == 301
    assert "ts.net" in r2.headers["location"]


# --- registration in the real app ---


def test_real_app_probe_redirects_and_healthz_still_served():
    from openmarquee.app import app

    with TestClient(app) as client:
        r = client.get("/generate_204", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == DEFAULT_PORTAL_URL
        # A real unauth route must not be swallowed by the middleware.
        assert client.get("/healthz", follow_redirects=False).status_code == 200
