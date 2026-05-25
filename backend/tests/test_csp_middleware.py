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
        "connect-src 'self' blob:",
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


# ---- 2026-05-25 Bundle A item 8: nosniff + Referrer-Policy on every response ----


def test_csp_policy_includes_report_uri_directive():
    """Bundle A item 3: DEFAULT_CSP_POLICY must carry a report-uri
    directive pointing at /api/system/csp-report so browsers POST
    violation reports back to the device journal."""
    assert "report-uri /api/system/csp-report" in DEFAULT_CSP_POLICY


def test_middleware_appends_nosniff_and_referrer_policy_on_200():
    """Bundle A item 8: every response carries X-Content-Type-Options:
    nosniff + Referrer-Policy: strict-origin-when-cross-origin
    alongside the CSP header."""
    middleware = CSPMiddleware(_fake_inner_app_200)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/"}
    _run(middleware(scope, _noop_receive, send))

    start_messages = [m for m in messages if m["type"] == "http.response.start"]
    assert len(start_messages) == 1
    headers = dict(start_messages[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"referrer-policy"] == b"strict-origin-when-cross-origin"


def test_middleware_appends_nosniff_and_referrer_policy_on_401():
    """Defense-in-depth headers stamped on auth-rejected responses
    too (same shape as the CSP-on-401 test above -- the wrap fires
    on the response-start regardless of status code)."""
    middleware = CSPMiddleware(_fake_inner_app_401_with_existing_headers)
    messages: list[dict] = []

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {"type": "http", "method": "GET", "path": "/api/anything"}
    _run(middleware(scope, _noop_receive, send))

    headers = [m for m in messages if m["type"] == "http.response.start"][0]["headers"]
    assert (b"x-content-type-options", b"nosniff") in headers
    assert (b"referrer-policy", b"strict-origin-when-cross-origin") in headers


def test_nosniff_and_referrer_policy_present_on_healthz():
    """End-to-end via the real app: /healthz response carries the
    Bundle A item 8 headers."""
    from fastapi.testclient import TestClient

    from openmarquee.app import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


# ---- 2026-05-25 Bundle A item 3: /api/system/csp-report POST handler ----


def test_csp_report_accepts_unauth_post_and_logs_at_warning(monkeypatch, tmp_path, caplog):
    """Browsers POST CSP violations to /api/system/csp-report unauth
    (the report-uri carve-out in _WHITELIST_EXACT). Handler logs at
    WARNING + returns 204 + does NOT throw on a normal report shape."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    # Configure auth so the middleware is exercising the carve-out
    # (without a configured AuthState the "not configured" 401 would
    # short-circuit before the whitelist check is meaningful).
    from fastapi.testclient import TestClient

    from openmarquee.app import app

    client = TestClient(app)
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )

    report_body = (
        '{"csp-report":{"violated-directive":"script-src",'
        '"blocked-uri":"https://evil.example/x.js"}}'
    )
    with caplog.at_level("WARNING", logger="openmarquee.api_system"):
        resp = client.post(
            "/api/system/csp-report",
            data=report_body,
            headers={"Content-Type": "application/csp-report"},
        )
    assert resp.status_code == 204, f"expected 204 unauth (carve-out); got {resp.status_code}"
    assert any("csp-report" in rec.message for rec in caplog.records), (
        f"expected WARNING with 'csp-report' marker; got {[r.message for r in caplog.records]}"
    )
    assert any("violated-directive" in rec.message for rec in caplog.records), (
        "WARNING body should include the violated-directive field for forensics"
    )


def test_csp_report_handler_tolerates_empty_body(monkeypatch, tmp_path, caplog):
    """Handler must not throw on a 0-byte POST -- browsers shouldn't
    send this, but the unauth surface is wider than just browsers."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    from fastapi.testclient import TestClient

    from openmarquee.app import app

    client = TestClient(app)
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    with caplog.at_level("WARNING", logger="openmarquee.api_system"):
        resp = client.post("/api/system/csp-report", data=b"")
    assert resp.status_code == 204
    assert any("empty body" in rec.message for rec in caplog.records)


def test_csp_report_handler_truncates_oversized_body(monkeypatch, tmp_path, caplog):
    """An attacker on the LAN/tailnet could try to log-flood via the
    unauth POST surface. Body cap = 1024 chars in the log line keeps
    journald from filling on hostile traffic."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    from fastapi.testclient import TestClient

    from openmarquee.app import app

    client = TestClient(app)
    client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    huge = "X" * 5000
    with caplog.at_level("WARNING", logger="openmarquee.api_system"):
        resp = client.post("/api/system/csp-report", data=huge)
    assert resp.status_code == 204
    logged = "\n".join(rec.message for rec in caplog.records if "csp-report" in rec.message)
    assert "...(truncated)" in logged, (
        "oversized body should be truncated with marker; got: " + logged[:200]
    )
    assert len(logged) < 5000, "logged line should be capped well below the input size"
