"""Tests for the ASGI body-cap middleware.

Bundle B2 piece 3. Closes B1's known gap: Pydantic Field(max_length)
rejects with 422 at validation time but Starlette buffers the FULL
body first. This middleware 413s at Content-Length read so a
hostile multi-MB POST never allocates the buffer.

Layered defense: ASGI 413 (this) + Pydantic 422 (B1) at distinct
gates. Tests confirm both gates fire at their proper layer.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from openmarquee._body_cap_middleware import (
    DEFAULT_CAP_BYTES,
    ROUTE_BODY_CAPS,
    BodyCapMiddleware,
    _cap_for_path,
    _content_length_from_headers,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _noop_receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _fake_inner_200(scope, receive, send):
    """Echo handler -- always returns 200. Used to confirm requests
    that DON'T trip the cap pass through unchanged."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_cap_for_path_uses_route_cap_when_matched():
    """Per-route caps win over the default."""
    assert _cap_for_path("/api/content/text-slides") == ROUTE_BODY_CAPS["/api/content/text-slides"]
    # Path prefix match: subpaths under a configured prefix get the
    # same cap.
    assert (
        _cap_for_path("/api/content/text-slides/some-id")
        == ROUTE_BODY_CAPS["/api/content/text-slides"]
    )


def test_cap_for_path_falls_back_to_default_for_unlisted_route():
    """Routes not in ROUTE_BODY_CAPS get DEFAULT_CAP_BYTES (1 MB)."""
    assert _cap_for_path("/api/auth/login") == DEFAULT_CAP_BYTES
    assert _cap_for_path("/some/other/path") == DEFAULT_CAP_BYTES


def test_cap_for_path_guards_against_prefix_bleed():
    """A future `/api/content/text-slides-archive` route must NOT
    silently inherit the text-slides 15 MB cap. _cap_for_path() must
    use exact-match or `prefix + "/"`, not bare startswith."""
    # Hypothetical sibling routes that lexically extend a configured
    # prefix but are logically distinct.
    assert _cap_for_path("/api/content/text-slides-archive") == DEFAULT_CAP_BYTES
    assert _cap_for_path("/api/content/imageserver") == DEFAULT_CAP_BYTES
    assert _cap_for_path("/api/content/videos2") == DEFAULT_CAP_BYTES
    # Sanity: the matched-prefix cases still work.
    assert _cap_for_path("/api/content/text-slides") == ROUTE_BODY_CAPS["/api/content/text-slides"]
    assert (
        _cap_for_path("/api/content/text-slides/abc") == ROUTE_BODY_CAPS["/api/content/text-slides"]
    )


def test_content_length_parser_handles_normal_values():
    assert _content_length_from_headers([(b"content-length", b"1024")]) == 1024
    assert _content_length_from_headers([(b"Content-Length", b"0")]) == 0


def test_content_length_parser_rejects_negative():
    """Negative Content-Length is malformed; return None so the
    middleware falls through to pass-through rather than miscompare."""
    assert _content_length_from_headers([(b"content-length", b"-5")]) is None


def test_content_length_parser_returns_none_for_unparseable():
    assert _content_length_from_headers([(b"content-length", b"not-a-number")]) is None


def test_content_length_parser_returns_none_when_absent():
    assert _content_length_from_headers([(b"host", b"example.com")]) is None


@pytest.mark.parametrize(
    "method",
    ["GET", "HEAD", "DELETE", "OPTIONS"],
)
def test_middleware_passes_non_body_methods_through(method):
    """GET / HEAD / DELETE / OPTIONS don't carry meaningful bodies
    in openMarquee's API surface; the middleware should not gate them
    even if a Content-Length is present (would be misleading anyway)."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": "/api/content/text-slides",
        "headers": [(b"content-length", b"99999999999")],
    }
    _run(middleware(scope, _noop_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [200], (
        f"{method} with absurd content-length should pass through; got status {statuses}"
    )


def test_middleware_passes_under_cap_post_through():
    """A POST with Content-Length under the route's cap reaches the
    inner handler (200)."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/content/text-slides",
        "headers": [(b"content-length", b"1024")],
    }
    _run(middleware(scope, _noop_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [200]


def test_middleware_413s_oversize_post_for_text_slides():
    """A POST with Content-Length above the text-slides cap (15 MB)
    rejects with 413 BEFORE the body is read."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []
    receive_called = {"n": 0}

    async def send(message):
        sent.append(message)

    async def counting_receive():
        receive_called["n"] += 1
        return await _noop_receive()

    oversize = ROUTE_BODY_CAPS["/api/content/text-slides"] + 1
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/content/text-slides",
        "headers": [(b"content-length", str(oversize).encode("ascii"))],
    }
    _run(middleware(scope, counting_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413], f"expected 413; got {statuses}"
    # Load-bearing: no body bytes read on the rejection path.
    assert receive_called["n"] == 0, (
        f"receive() called {receive_called['n']} times on a rejected request; "
        f"the body buffer should not have been touched"
    )


def test_middleware_413_response_includes_actionable_detail():
    """The 413 body should tell the operator WHICH cap was tripped +
    by how much, so debugging is operator-friendly."""
    import json

    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    cap = ROUTE_BODY_CAPS["/api/content/videos"]
    declared = cap + 1
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/content/videos",
        "headers": [(b"content-length", str(declared).encode("ascii"))],
    }
    _run(middleware(scope, _noop_receive, send))
    body_msg = next(m for m in sent if m["type"] == "http.response.body")
    payload = json.loads(body_msg["body"].decode("utf-8"))
    assert "videos" in payload["detail"], f"413 detail should reference the route; got {payload}"
    assert str(cap) in payload["detail"], f"413 detail should reference the cap; got {payload}"


def test_middleware_413s_post_with_absent_content_length():
    """Fail-closed: a POST without Content-Length (chunked-transfer
    encoding) is rejected with 413. Without this, a hostile client
    could bypass the gate entirely by switching to chunked transfer.
    The captive-portal UI always sets Content-Length on JSON bodies,
    so legitimate operators are unaffected."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []
    receive_called = {"n": 0}

    async def send(message):
        sent.append(message)

    async def counting_receive():
        receive_called["n"] += 1
        return await _noop_receive()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/content/text-slides",
        # No content-length header at all.
        "headers": [(b"content-type", b"application/json")],
    }
    _run(middleware(scope, counting_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413], f"expected fail-closed 413; got {statuses}"
    assert receive_called["n"] == 0


def test_middleware_413s_post_with_unparseable_content_length():
    """Unparseable Content-Length is treated like absent (fail-closed)."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/content/text-slides",
        "headers": [(b"content-length", b"not-a-number")],
    }
    _run(middleware(scope, _noop_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413]


def test_middleware_413s_oversize_post_against_default_cap():
    """Unlisted routes use DEFAULT_CAP_BYTES (1 MB). A 5 MB POST to
    a route not in ROUTE_BODY_CAPS should 413."""
    middleware = BodyCapMiddleware(_fake_inner_200)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"content-length", b"5000000")],  # 5 MB > 1 MB default
    }
    _run(middleware(scope, _noop_receive, send))
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [413]


def test_middleware_routes_get_correct_per_type_caps():
    """Spot-check each configured route has a sensible cap relative
    to B1's max_length values. Locks the bundle-A/B1/B2 relationship
    so a future B1 max_length tweak surfaces the need to update the
    body cap in lockstep."""
    # text-slides cap >= 14 MB (B1's png_base64 max_length)
    assert ROUTE_BODY_CAPS["/api/content/text-slides"] >= 14_000_000
    # images cap >= 27 MB (B1's image_base64 max_length)
    assert ROUTE_BODY_CAPS["/api/content/images"] >= 27_000_000
    # videos cap >= 270 MB + 14 MB (B1's mp4 + png both at cap)
    assert ROUTE_BODY_CAPS["/api/content/videos"] >= 284_000_000


# ---- End-to-end via the real app ----


def test_app_413s_oversize_text_slide_post_before_body_read(tmp_path, monkeypatch):
    """Wired-in test through the real ASGI app + middleware stack.
    POSTing a Content-Length above the cap returns 413 (NOT 422 --
    that would mean Starlette buffered the body + Pydantic ran)."""
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(tmp_path / "auth.json"))
    monkeypatch.setenv("OPENMARQUEE_DISABLE_AUTH", "1")
    from fastapi.testclient import TestClient

    from openmarquee.app import app

    client = TestClient(app)
    # Use a real-shaped JSON body with a tiny png_base64 + a fake
    # Content-Length header well above the cap.
    payload = {
        "name": "x",
        "text_layers": [{"text": "x"}],
        "png_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(),
    }
    import json as _json

    body = _json.dumps(payload).encode("utf-8")
    # TestClient sets Content-Length automatically from the body
    # length. To exercise the 413 path with a real body cap, send
    # a body whose actual length exceeds the cap. 16 MB > 15 MB cap.
    huge_b64 = "A" * (16 * 1024 * 1024)
    payload["png_base64"] = huge_b64
    body = _json.dumps(payload).encode("utf-8")
    resp = client.post(
        "/api/content/text-slides",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413, (
        f"expected 413 from body-cap middleware; got {resp.status_code} "
        f"(if 422, the ASGI gate didn't fire and Pydantic caught it instead)"
    )
