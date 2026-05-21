"""Tests for the Web-slide screenshot producer (Web slide P3).

`fetch_web_screenshot` pulls one screenshot from the render helper and
saves it as a WebSlide's asset.png. It must never raise — every
failure path is caught, logged, and reported as a `False` return,
leaving the slide's previous asset untouched.

httpx is mocked by swapping `httpx.AsyncClient` for a fake whose `get`
returns a canned response (or raises). The real ContentStorage is
used against a tmp_path so the asset write is exercised end to end.
"""

import httpx
import pytest

from openmarquee.content import WebSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.web_screenshot import (
    WEB_SCREENSHOT_TIMEOUT_S,
    fetch_web_screenshot,
)

# A tiny but valid 1x1 PNG — enough that ContentStorage.save_web writes
# it verbatim (save_web with explicit bytes doesn't re-decode).
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
    "00907753de0000000c4944415408d76360606000000000040001"
    "5c0c02b00000000049454e44ae426082"
)


class _FakeResponse:
    """Minimal stand-in for an httpx.Response."""

    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


def _fake_client_class(*, response=None, raise_exc=None, calls=None):
    """Build a fake `httpx.AsyncClient` class.

    `response` is returned from `get`; `raise_exc` is raised instead.
    `calls` (a list) records each (url, params, headers) for assertion.
    """

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, headers=None):
            if calls is not None:
                calls.append(
                    {
                        "url": url,
                        "params": params,
                        "headers": headers,
                        "timeout": self._timeout,
                    }
                )
            if raise_exc is not None:
                raise raise_exc
            return response

    return _FakeAsyncClient


def _web_slide(**kwargs) -> WebSlide:
    kwargs.setdefault("name", "Status")
    kwargs.setdefault("url", "https://status.example.com")
    return WebSlide(**kwargs)


@pytest.mark.asyncio
async def test_fetch_writes_asset_on_http_200(tmp_path, monkeypatch):
    """A 200 with image bytes -> the bytes land in the slide's asset.png
    and the function returns True."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    # Seed a placeholder so save_web overwrites a real prior asset.
    storage.save_web(slide)

    calls: list[dict] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            response=_FakeResponse(200, _PNG_1x1), calls=calls
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok-123", 1920, 1080
    )

    assert ok is True
    assert storage.read_asset(slide.id) == _PNG_1x1
    # The helper was hit at /shot with the slide URL + panel dims and
    # the bearer token.
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "http://helper.local:8888/shot"
    assert call["params"] == {
        "url": "https://status.example.com",
        "w": 1920,
        "h": 1080,
    }
    assert call["headers"] == {"Authorization": "Bearer tok-123"}
    assert call["timeout"] == WEB_SCREENSHOT_TIMEOUT_S


@pytest.mark.asyncio
async def test_fetch_strips_trailing_slash_on_helper_url(
    tmp_path, monkeypatch
):
    """A helper URL with a trailing slash still produces a single-slash
    /shot path."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list[dict] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            response=_FakeResponse(200, _PNG_1x1), calls=calls
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888/  ", "tok", 800, 480
    )

    assert ok is True
    assert calls[0]["url"] == "http://helper.local:8888/shot"


@pytest.mark.asyncio
async def test_non_200_leaves_asset_untouched(tmp_path, monkeypatch):
    """A non-200 response -> False, and the prior asset is untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(response=_FakeResponse(502, b"page load failed")),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_empty_body_leaves_asset_untouched(tmp_path, monkeypatch):
    """A 200 with an empty body -> False, asset untouched (an empty PNG
    would render as a broken slide)."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(response=_FakeResponse(200, b"")),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_network_error_leaves_asset_untouched(tmp_path, monkeypatch):
    """A network error -> False, no raise, asset untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            raise_exc=httpx.ConnectError("connection refused")
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_timeout_leaves_asset_untouched(tmp_path, monkeypatch):
    """A request timeout -> False, no raise, asset untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            raise_exc=httpx.ReadTimeout("helper too slow")
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_empty_helper_url_is_a_clean_no_op(tmp_path, monkeypatch):
    """An empty web_helper_url -> False with NO HTTP attempt (the helper
    is simply unconfigured), asset untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    calls: list[dict] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            response=_FakeResponse(200, _PNG_1x1), calls=calls
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "", "tok", 1920, 1080
    )

    assert ok is False
    assert calls == []  # no HTTP request attempted
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_whitespace_only_helper_url_is_a_clean_no_op(
    tmp_path, monkeypatch
):
    """A whitespace-only web_helper_url is treated as unconfigured."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list[dict] = []
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(
            response=_FakeResponse(200, _PNG_1x1), calls=calls
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "   ", "tok", 1920, 1080
    )

    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_save_failure_does_not_raise(tmp_path, monkeypatch):
    """A failure inside save_web is caught -> False, no raise out."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        _fake_client_class(response=_FakeResponse(200, _PNG_1x1)),
    )

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_web", _boom)

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
