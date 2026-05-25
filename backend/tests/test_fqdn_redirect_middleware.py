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

from openmarquee.fqdn_redirect_middleware import (
    FqdnRedirectMiddleware,
    _is_private_or_loopback_ip,
)

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


# ---- _is_private_or_loopback_ip direct unit coverage ----
#
# This helper IS the trusted-network discriminator for the 30s
# set-password boot-grace window (Bundle B2 item 7, api_auth.py:144).
# During grace, a fresh-boot device returns 403 to non-loopback /
# non-RFC1918 / non-CGNAT requests, and this helper alone decides
# "trusted." A refactor that mishandles IPv6 ::1, CGNAT boundaries
# (off-by-one at 100.128.0.0), or a 6to4 misclassification would
# silently widen the grace window so a malicious LAN device could
# POST set-password from any IP.
#
# Pre-r14 coverage: 4 hosts exercised via FqdnRedirectMiddleware's
# "passes-through" tests (10.0.0.1, 100.64.1.2, 127.0.0.1,
# 192.168.1.69). test_auth.py monkeypatches the helper away to test
# the grace-gate WRAPPER logic in isolation -- those monkeypatches
# stay (they're testing the gate, not the classifier). This adds
# direct contract pinning for the helper itself across the edges
# that matter.


@pytest.mark.parametrize(
    "host,expected",
    [
        # --- IPv4 loopback + RFC1918 + CGNAT (trusted) ---
        ("127.0.0.1", True),
        ("10.0.0.1", True),
        ("172.16.0.1", True),
        ("192.168.1.69", True),
        # CGNAT lower edge: 100.64.0.0/10 starts at 100.64.0.0
        ("100.64.0.0", True),
        # CGNAT upper edge: 100.64.0.0/10 ends at 100.127.255.255
        ("100.127.255.255", True),
        # --- IPv4 CGNAT off-by-one boundaries (must NOT match) ---
        # Just ABOVE the CGNAT block — common misclassification target
        # if someone widens the range to /9 (100.0.0.0/8 wrongly).
        ("100.128.0.0", False),
        # Just BELOW the CGNAT block — same off-by-one shape on the
        # low end.
        ("100.63.255.255", False),
        # --- IPv4 public ---
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        # --- IPv6 ---
        # Loopback
        ("::1", True),
        # Link-local fe80::/10 (is_private=True per Python ipaddress)
        ("fe80::1", True),
        # Unique Local Address (ULA) fc00::/7
        ("fc00::1", True),
        # Public v6 (Google DNS)
        ("2001:4860:4860::8888", False),
        # NAT64 well-known prefix 64:ff9b::/96 -- the lexical "64:"
        # might tempt a regex-based classifier to match the v4 CGNAT
        # 100.64.x pattern. The current ipaddress-based impl correctly
        # rejects this (v6, not in v4 CGNAT, not is_private).
        ("64:ff9b::", False),
        # --- Non-parseable input ---
        ("not-an-ip", False),
        ("", False),
        # Injection-shaped input: a v4 IP plus extra tokens. The
        # helper's ipaddress.ip_address() raises ValueError on the
        # whole string, so the fallback "untrusted" applies. Locks
        # against a future refactor that pre-splits/strips before
        # parsing and accidentally accepts the prefix as trusted.
        ("192.168.1.1; DROP", False),
    ],
)
def test_is_private_or_loopback_ip(host: str, expected: bool):
    assert _is_private_or_loopback_ip(host) is expected
