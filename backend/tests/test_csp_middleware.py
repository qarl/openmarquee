"""Tests for the Content-Security-Policy middleware.

Covers the ASGI middleware in isolation (no FastAPI app needed) plus
end-to-end smoke against the real app via TestClient -- the latter
catches stack-order regressions (CSP must wrap Auth's 401 responses
too, not just successful 200s).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from openmarquee.csp_middleware import DEFAULT_CSP_POLICY, CSPMiddleware

# ---------- Unit tests: ASGI middleware in isolation ----------


async def _capture_send(messages: list[dict]) -> Callable[[dict], Awaitable[None]]:
    async def send(message: dict) -> None:
        messages.append(message)

    return send


def _run(coro: Any) -> Any:
    """Tiny sync driver -- avoids depending on pytest-asyncio for the
    pure-ASGI cases below."""
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


async def _noop_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _fake_inner_app_200(scope: dict, receive: Any, send: Any) -> None:
    """Trivial inner app that responds 200 with no body, no headers."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _fake_inner_app_401_with_existing_headers(scope, receive, send) -> None:
    """Inner app that responds 401 with pre-existing headers -- proves
    CSP appends rather than replaces."""
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [(b"www-authenticate", b'Bearer realm="openMarquee"')],
        }
    )
    await send({"type": "http.response.body", "body": b"unauthorized"})


def test_default_policy_contains_required_directives():
    """The default policy must mention every directive class we audited
    -- regressing one of these is how an XSS would escape."""
    for directive in (
        "default-src 'self'",
        "script-src 'self' 'wasm-unsafe-eval' blob:",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ):
        assert directive in DEFAULT_CSP_POLICY, f"missing {directive!r}"


def test_default_policy_does_not_grant_unsafe_eval_to_scripts():
    """`'wasm-unsafe-eval'` is fine (wasm-bindgen needs it). Plain
    `'unsafe-eval'` would re-open the script-eval attack surface."""
    assert "'unsafe-eval'" not in DEFAULT_CSP_POLICY.replace("'wasm-unsafe-eval'", "")


def test_default_policy_does_not_grant_unsafe_inline_to_scripts():
    """Inline styles are unavoidable (5 served HTML shells); inline
    scripts must stay banned -- that's the actual XSS surface."""
    # Pull just the script-src directive
    for d in DEFAULT_CSP_POLICY.split(";"):
        d = d.strip()
        if d.startswith("script-src"):
            assert "'unsafe-inline'" not in d, "script-src must NOT allow unsafe-inline"
            return
    pytest.fail("script-src directive not found")


def test_middleware_appends_header_on_200():
    middleware = CSPMiddleware(_fake_inner_app_200)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/"}
    _run(middleware(scope, _noop_receive, send))

    start_messages = [m for m in messages if m["type"] == "http.response.start"]
    assert len(start_messages) == 1
    headers = dict(start_messages[0]["headers"])
    assert headers[b"content-security-policy"] == DEFAULT_CSP_POLICY.encode("ascii")


def test_middleware_appends_header_on_401_without_clobbering_existing():
    """An inner middleware (AuthMiddleware) that 401s with an existing
    WWW-Authenticate header must keep that header AND get CSP added."""
    middleware = CSPMiddleware(_fake_inner_app_401_with_existing_headers)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/anything"}
    _run(middleware(scope, _noop_receive, send))

    start_messages = [m for m in messages if m["type"] == "http.response.start"]
    assert len(start_messages) == 1
    headers = start_messages[0]["headers"]
    # Both headers present; CSP appended at the end.
    assert (b"www-authenticate", b'Bearer realm="openMarquee"') in headers
    assert any(
        name == b"content-security-policy" and val == DEFAULT_CSP_POLICY.encode("ascii")
        for name, val in headers
    )


def test_middleware_report_only_flips_header_name():
    middleware = CSPMiddleware(_fake_inner_app_200, report_only=True)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/"}
    _run(middleware(scope, _noop_receive, send))

    headers = dict(messages[0]["headers"])
    assert b"content-security-policy" not in headers
    assert headers[b"content-security-policy-report-only"] == DEFAULT_CSP_POLICY.encode("ascii")


def test_middleware_passes_lifespan_through_unmodified():
    """Non-HTTP scopes (lifespan, websocket) must not be touched -- ASGI
    spec violation otherwise."""
    seen_scopes: list[dict] = []

    async def inner(scope: dict, receive: Any, send: Any) -> None:
        seen_scopes.append(scope)

    middleware = CSPMiddleware(inner)
    scope = {"type": "lifespan"}
    _run(middleware(scope, _noop_receive, lambda m: None))  # noqa: ARG005
    assert seen_scopes == [{"type": "lifespan"}]


def test_middleware_accepts_custom_policy():
    custom = "default-src 'none'"
    middleware = CSPMiddleware(_fake_inner_app_200, policy=custom)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/"}
    _run(middleware(scope, _noop_receive, send))

    headers = dict(messages[0]["headers"])
    assert headers[b"content-security-policy"] == custom.encode("ascii")


# ---------- End-to-end: real app via TestClient ----------


def _make_client() -> TestClient:
    """Fresh TestClient against the real app. conftest.py sets
    OPENMARQUEE_DISABLE_AUTH=1 so any route returns 200, but the CSP
    header is added regardless of auth outcome -- the auth-on path is
    covered explicitly below."""
    from openmarquee.app import app

    return TestClient(app)


def test_csp_header_present_on_healthz():
    client = _make_client()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("content-security-policy") == DEFAULT_CSP_POLICY


def test_csp_header_present_on_401_when_auth_enabled(monkeypatch, tmp_path):
    """Stack-order regression test: CSP must wrap Auth, so a 401
    response must STILL carry the CSP header."""
    # Re-enable auth for this test only. Point at an empty tmp dir so
    # the storage starts unconfigured -- bearer requests will 401.
    monkeypatch.delenv("OPENMARQUEE_DISABLE_AUTH", raising=False)
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    # Clear the auth-storage singleton's lru_cache so the new env var
    # is observed (same pattern as test_auth.py).
    from openmarquee.dependencies import _auth_storage_singleton

    _auth_storage_singleton.cache_clear()
    try:
        client = _make_client()
        # /api/system/info requires a token; with no AuthState configured,
        # it 401s ("password not configured").
        resp = client.get("/api/system/info")
        assert resp.status_code == 401
        assert resp.headers.get("content-security-policy") == DEFAULT_CSP_POLICY
    finally:
        _auth_storage_singleton.cache_clear()


def test_csp_header_uses_report_only_when_env_set(monkeypatch):
    """OPENMARQUEE_CSP_REPORT_ONLY=1 must flip to the report-only
    header on a fresh app import."""
    import importlib

    import openmarquee.app as app_module

    monkeypatch.setenv("OPENMARQUEE_CSP_REPORT_ONLY", "1")
    try:
        # Force re-import so the middleware re-reads the env var at
        # add_middleware-time.
        importlib.reload(app_module)
        client = TestClient(app_module.app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.headers.get("content-security-policy") is None
        assert resp.headers.get("content-security-policy-report-only") == DEFAULT_CSP_POLICY
    finally:
        # Restore the un-report-only app for any test that imports app
        # later in the run.
        monkeypatch.delenv("OPENMARQUEE_CSP_REPORT_ONLY", raising=False)
        importlib.reload(app_module)
