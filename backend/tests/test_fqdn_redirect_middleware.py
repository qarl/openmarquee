"""Unit tests for FqdnRedirectMiddleware.

Each test builds a minimal FastAPI app with just the middleware
wrapped around a `/healthz` route, so we exercise the middleware in
isolation -- no Perf / Auth / route plumbing dragging in. Resolvers
are passed as fakes (no real `tailscale status --json` subprocess,
no real settings file).
"""

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openmarquee.fqdn_redirect_middleware import FqdnRedirectMiddleware

_FQDN = "fireplacesign.tail71c768.ts.net"


class _FakeSettings:
    def __init__(self, tailscale_https_enabled: bool = True) -> None:
        self.tailscale_https_enabled = tailscale_https_enabled


def _build_app(
    fqdn: str | None = _FQDN,
    https_enabled: bool = True,
) -> TestClient:
    """Build a minimal FastAPI app wrapped in the middleware under test."""

    async def fqdn_resolver() -> str | None:
        return fqdn

    def settings_resolver() -> Any:
        return _FakeSettings(tailscale_https_enabled=https_enabled)

    app = FastAPI()
    app.add_middleware(
        FqdnRedirectMiddleware,
        fqdn_resolver=fqdn_resolver,
        settings_resolver=settings_resolver,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/foo/bar")
    async def foo_bar() -> dict[str, str]:
        return {"path": "/foo/bar"}

    # TestClient won't follow 301s by default; we want to assert
    # against the redirect itself, not the eventual destination.
    return TestClient(app, follow_redirects=False)


def test_short_name_redirects_to_fqdn():
    client = _build_app()
    # TestClient's `Host` header overrides the URL's host for the
    # request line; this simulates the operator typing `http://
    # fireplacesign/healthz` and the browser sending Host: fireplacesign.
    response = client.get("/healthz", headers={"Host": "fireplacesign"})
    assert response.status_code == 301
    assert response.headers["location"] == f"https://{_FQDN}/healthz"


def test_arbitrary_hostname_redirects_to_fqdn():
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "some-other-name.local"})
    assert response.status_code == 301
    assert response.headers["location"] == f"https://{_FQDN}/healthz"


def test_fqdn_passes_through():
    client = _build_app()
    response = client.get("/healthz", headers={"Host": _FQDN})
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_fqdn_passes_through_case_insensitive():
    """Hostname compare must be case-insensitive -- a browser may
    uppercase the Host header even though the cert is for the
    lowercased FQDN."""
    client = _build_app()
    response = client.get("/healthz", headers={"Host": _FQDN.upper()})
    assert response.status_code == 200


def test_localhost_passes_through():
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "localhost"})
    assert response.status_code == 200


def test_loopback_ip_passes_through():
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "127.0.0.1"})
    assert response.status_code == 200


def test_captive_portal_ap_ip_passes_through():
    """10.0.0.1 is the dnsmasq-served AP gateway. Operators on the
    captive portal must NOT be redirected to a tailnet FQDN that
    doesn't exist for them yet (they're still configuring the device).
    """
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "10.0.0.1"})
    assert response.status_code == 200


def test_lan_ip_passes_through():
    """LAN IPs (192.168.x.x) are Chrome secure contexts already, so
    `getUserMedia` works without HTTPS. Don't churn the operator's
    LAN bookmark."""
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "192.168.1.69"})
    assert response.status_code == 200


def test_tailscale_cgnat_ip_passes_through():
    """Tailscale CGNAT (100.64.0.0/10) -- also a Chrome secure context
    via IP-literal, so an operator using the tailnet IP directly
    instead of the FQDN shouldn't get redirected."""
    client = _build_app()
    response = client.get("/healthz", headers={"Host": "100.64.1.2"})
    assert response.status_code == 200


def test_https_disabled_passes_through():
    """Operator opt-out: tailscale_https_enabled=False disables the
    redirect entirely (even if Tailscale is up + FQDN is known)."""
    client = _build_app(https_enabled=False)
    response = client.get("/healthz", headers={"Host": "fireplacesign"})
    assert response.status_code == 200


def test_tailscale_down_passes_through():
    """No FQDN means no redirect target. Backend still serves HTTP
    so the operator at least gets the camera-banner workaround text."""
    client = _build_app(fqdn=None)
    response = client.get("/healthz", headers={"Host": "fireplacesign"})
    assert response.status_code == 200


def test_query_string_preserved_across_redirect():
    client = _build_app()
    response = client.get(
        "/foo/bar?baz=qux&spam=eggs",
        headers={"Host": "fireplacesign"},
    )
    assert response.status_code == 301
    assert response.headers["location"] == (f"https://{_FQDN}/foo/bar?baz=qux&spam=eggs")


def test_path_preserved_across_redirect():
    client = _build_app()
    response = client.get(
        "/foo/bar",
        headers={"Host": "fireplacesign"},
    )
    assert response.status_code == 301
    assert response.headers["location"] == f"https://{_FQDN}/foo/bar"


def test_settings_resolver_failure_passes_through(caplog: pytest.LogCaptureFixture):
    """If the settings file is mid-rewrite (write race) and the
    resolver throws, the middleware logs + passes through rather
    than 500-ing or redirect-looping the operator."""

    async def fqdn_resolver() -> str | None:
        return _FQDN

    def broken_settings_resolver() -> Any:
        raise RuntimeError("settings file mid-rewrite")

    app = FastAPI()
    app.add_middleware(
        FqdnRedirectMiddleware,
        fqdn_resolver=fqdn_resolver,
        settings_resolver=broken_settings_resolver,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    client = TestClient(app, follow_redirects=False)
    response = client.get("/healthz", headers={"Host": "fireplacesign"})
    assert response.status_code == 200
