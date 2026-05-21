"""HTTP-layer tests for the Web slide helper.

The screenshot worker is monkeypatched throughout, so these tests need
no real browser.
"""

import pytest

from openmarquee_web_helper import app as app_module

from .conftest import CANNED_PNG, TEST_TOKEN


def _auth(token: str = TEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# /healthz
# --------------------------------------------------------------------------


def test_healthz_ok_without_auth(client):
    """/healthz returns 200 and needs no token."""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --------------------------------------------------------------------------
# /shot -- auth
# --------------------------------------------------------------------------


def test_shot_no_token_is_401(client):
    """/shot with no Authorization header -> 401."""
    resp = client.get("/shot", params={"url": "https://example.com", "w": 800, "h": 600})
    assert resp.status_code == 401


def test_shot_wrong_token_is_401(client):
    """/shot with a wrong bearer token -> 401."""
    resp = client.get(
        "/shot",
        params={"url": "https://example.com", "w": 800, "h": 600},
        headers=_auth("not-the-token"),
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# /shot -- happy path with the worker monkeypatched
# --------------------------------------------------------------------------


def test_shot_happy_path_returns_canned_png(client):
    """Correct token + a mocked worker -> 200 image/png with the canned bytes."""
    seen = {}

    async def fake_render(url, width, height):
        seen["args"] = (url, width, height)
        return CANNED_PNG

    app_module.render_screenshot = fake_render

    resp = client.get(
        "/shot",
        params={"url": "https://example.com/dash", "w": 1024, "h": 768},
        headers=_auth(),
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == CANNED_PNG
    # The handler passed the validated url + viewport through to the worker.
    assert seen["args"] == ("https://example.com/dash", 1024, 768)


# --------------------------------------------------------------------------
# /shot -- request validation
# --------------------------------------------------------------------------


def test_shot_disallowed_scheme_is_400(client):
    """A file: URL -> 400 (never reaches the worker)."""

    async def fake_render(url, width, height):  # pragma: no cover
        raise AssertionError("worker should not be called for a bad scheme")

    app_module.render_screenshot = fake_render

    resp = client.get(
        "/shot",
        params={"url": "file:///etc/passwd", "w": 800, "h": 600},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_shot_missing_url_is_400(client):
    """A missing `url` param -> 400 (remapped from FastAPI's 422)."""
    resp = client.get("/shot", params={"w": 800, "h": 600}, headers=_auth())
    assert resp.status_code == 400


def test_shot_bad_dimension_is_400(client):
    """A non-positive viewport dimension -> 400."""
    resp = client.get(
        "/shot",
        params={"url": "https://example.com", "w": 0, "h": 600},
        headers=_auth(),
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# /shot -- error mapping
# --------------------------------------------------------------------------


def test_shot_timeout_maps_to_504(client):
    """A worker that raises a timeout-like error -> 504."""
    from openmarquee_web_helper.screenshot import ScreenshotTimeout

    async def fake_render(url, width, height):
        raise ScreenshotTimeout("simulated load timeout")

    app_module.render_screenshot = fake_render

    resp = client.get(
        "/shot",
        params={"url": "https://slow.example.com", "w": 800, "h": 600},
        headers=_auth(),
    )
    assert resp.status_code == 504


def test_shot_load_failure_maps_to_502(client):
    """A worker that raises a non-timeout load error -> 502."""
    from openmarquee_web_helper.screenshot import ScreenshotError

    async def fake_render(url, width, height):
        raise ScreenshotError("connection refused")

    app_module.render_screenshot = fake_render

    resp = client.get(
        "/shot",
        params={"url": "https://down.example.com", "w": 800, "h": 600},
        headers=_auth(),
    )
    assert resp.status_code == 502


def test_shot_unexpected_error_maps_to_500(client):
    """An unexpected worker exception -> 500."""

    async def fake_render(url, width, height):
        raise RuntimeError("something went sideways")

    app_module.render_screenshot = fake_render

    resp = client.get(
        "/shot",
        params={"url": "https://example.com", "w": 800, "h": 600},
        headers=_auth(),
    )
    assert resp.status_code == 500
