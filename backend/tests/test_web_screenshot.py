"""Tests for the Web-slide screenshot producer (Web slide P3).

`fetch_web_screenshot` pulls one screenshot from the render helper and
saves it as a WebSlide's asset.png. It must never raise — every
failure path is caught, logged, and reported as a `False` return,
leaving the slide's previous asset untouched.

httpx is mocked by swapping `httpx.AsyncClient` for a fake whose
`stream` yields a canned response (or raises). The real ContentStorage
is used against a tmp_path so the asset write is exercised end to end.
"""

import logging

import httpx
import pytest

from openmarquee.content import WebSlide
from openmarquee.content.storage import ContentStorage
from openmarquee import web_screenshot
from openmarquee.web_screenshot import (
    WEB_SCREENSHOT_MAX_BYTES,
    WEB_SCREENSHOT_TIMEOUT_S,
    fetch_web_screenshot,
)

# A tiny but valid 1x1 PNG — enough that ContentStorage.save_web writes
# it verbatim (save_web with explicit bytes doesn't re-decode). Starts
# with the 8-byte PNG signature the producer's magic-byte check looks
# for.
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
    "00907753de0000000c4944415408d76360606000000000040001"
    "5c0c02b00000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _clear_failure_throttle():
    """C3/L3: the failure-throttle set is module-level (process-
    lifetime). Clear it around each test so one test's failures don't
    leak into another's first-fail-WARNING expectation."""
    web_screenshot._failed_slide_ids.clear()
    yield
    web_screenshot._failed_slide_ids.clear()


class _FakeResponse:
    """Minimal stand-in for an httpx.Response used in streaming mode.

    `content` is the full body; `aiter_bytes` yields it in `chunk_size`
    pieces so the producer's accumulate-and-cap path is exercised.
    `headers` is a plain dict (httpx headers are dict-like for `.get`).
    """

    def __init__(self, status_code: int, content: bytes, *,
                 headers=None, chunk_size: int = 64 * 1024):
        self.status_code = status_code
        self.content = content
        self.headers = headers if headers is not None else {}
        self._chunk_size = chunk_size

    async def aiter_bytes(self):
        for i in range(0, len(self.content), self._chunk_size):
            yield self.content[i:i + self._chunk_size]
        # An empty body yields nothing — match httpx (the producer's
        # `if not png_bytes` then handles the empty case).


def _fake_client_class(*, response=None, raise_exc=None, calls=None):
    """Build a fake `httpx.AsyncClient` class.

    `response` is yielded from `stream`; `raise_exc` is raised instead.
    `calls` (a list) records each (url, params, headers) for assertion.
    """

    class _StreamCtx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, params=None, headers=None):
            if calls is not None:
                calls.append(
                    {
                        "method": method,
                        "url": url,
                        "params": params,
                        "headers": headers,
                        "timeout": self._timeout,
                    }
                )
            if raise_exc is not None:
                raise raise_exc
            return _StreamCtx(response)

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


# --- C3/M3: response-body guards (size cap + PNG magic bytes) --------------


@pytest.mark.asyncio
async def test_html_error_page_with_200_is_rejected(
    tmp_path, monkeypatch, caplog
):
    """C3/M3: a 200 whose body is an HTML error page (not PNG magic) is
    rejected — save_web is NOT called, the failure is logged, no raise,
    and the prior asset is untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    save_calls: list = []
    orig_save = storage.save_web
    monkeypatch.setattr(
        storage, "save_web",
        lambda *a, **k: (save_calls.append((a, k)), orig_save(*a, **k))[1],
    )

    html = b"<html><body>502 Bad Gateway</body></html>"
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(response=_FakeResponse(200, html)),
    )

    with caplog.at_level(logging.WARNING, logger="openmarquee.web_screenshot"):
        ok = await fetch_web_screenshot(
            slide, storage, "http://helper.local:8888", "tok", 1920, 1080
        )

    assert ok is False
    assert save_calls == []  # save_web never invoked for a non-PNG body
    assert storage.read_asset(slide.id) == before
    assert any("non-PNG" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_oversize_body_is_rejected_without_buffering(
    tmp_path, monkeypatch
):
    """C3/M3: a body larger than the cap is rejected, and the producer
    never buffers it all — it aborts the chunk loop the moment the
    accumulated total exceeds WEB_SCREENSHOT_MAX_BYTES."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    # A response that, if fully read, would be ~3x the cap. Track how
    # many bytes the producer actually pulled via aiter_bytes.
    pulled = 0
    cap = WEB_SCREENSHOT_MAX_BYTES
    chunk = b"\x00" * (1024 * 1024)  # 1 MiB chunks

    class _HugeResponse:
        status_code = 200
        headers: dict = {}  # no Content-Length -> exercises the cap path

        async def aiter_bytes(self):
            nonlocal pulled
            # Yield ~3x the cap one chunk at a time; the producer must
            # break out long before the generator is exhausted.
            for _ in range((cap * 3) // len(chunk)):
                pulled += len(chunk)
                yield chunk

    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(response=_HugeResponse()),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert storage.read_asset(slide.id) == before
    # The producer aborted early: it pulled only a hair over the cap,
    # never the full 3x body. (One chunk of slack for the chunk that
    # tips total past the cap.)
    assert pulled <= cap + len(chunk), (
        f"producer buffered {pulled} bytes — should abort near the "
        f"{cap}-byte cap"
    )


@pytest.mark.asyncio
async def test_oversize_content_length_is_rejected_early(
    tmp_path, monkeypatch
):
    """C3/M3: a Content-Length header that exceeds the cap is rejected
    BEFORE the body is read at all — aiter_bytes is never iterated."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    iterated = False

    class _DeclaredHugeResponse:
        status_code = 200
        headers = {"Content-Length": str(WEB_SCREENSHOT_MAX_BYTES + 1)}

        async def aiter_bytes(self):
            nonlocal iterated
            iterated = True
            yield b"never reached"

    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(response=_DeclaredHugeResponse()),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is False
    assert iterated is False  # rejected on the header, body never read
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_genuine_png_under_cap_still_saves(tmp_path, monkeypatch):
    """C3/M3: a real PNG body (PNG magic, under the cap) still saves —
    the new guards don't reject a valid screenshot."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(
            response=_FakeResponse(
                200, _PNG_1x1,
                headers={"Content-Length": str(len(_PNG_1x1))},
            )
        ),
    )

    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )

    assert ok is True
    assert storage.read_asset(slide.id) == _PNG_1x1


# --- C3/L3: per-failure WARNING-then-DEBUG throttle ------------------------


@pytest.mark.asyncio
async def test_first_failure_warns_repeat_failure_debugs(
    tmp_path, monkeypatch, caplog
):
    """C3/L3: the first failure for a slide id logs WARNING; a second
    consecutive failure for the same id logs DEBUG — so a down helper
    on a short refresh interval doesn't WARNING-spam forever."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(
            raise_exc=httpx.ConnectError("connection refused")
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="openmarquee.web_screenshot"):
        ok1 = await fetch_web_screenshot(
            slide, storage, "http://helper.local:8888", "tok", 1920, 1080
        )
        first_records = list(caplog.records)
        caplog.clear()
        ok2 = await fetch_web_screenshot(
            slide, storage, "http://helper.local:8888", "tok", 1920, 1080
        )
        second_records = list(caplog.records)

    assert ok1 is False and ok2 is False
    # First failure -> WARNING.
    assert any(
        r.levelno == logging.WARNING and "request failed" in r.message
        for r in first_records
    )
    # Second failure for the SAME id -> DEBUG, not WARNING.
    failure_lines = [
        r for r in second_records if "request failed" in r.message
    ]
    assert failure_lines
    assert all(r.levelno == logging.DEBUG for r in failure_lines)


@pytest.mark.asyncio
async def test_success_clears_the_failure_throttle(
    tmp_path, monkeypatch, caplog
):
    """C3/L3: a success between failures clears the throttle entry, so
    the next failure for that id WARNINGs afresh rather than being
    DEBUG-suppressed."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    # 1) A failure -> marks the id throttled.
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(
            raise_exc=httpx.ConnectError("down")
        ),
    )
    await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )
    assert slide.id in web_screenshot._failed_slide_ids

    # 2) A success -> clears the throttle entry.
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(response=_FakeResponse(200, _PNG_1x1)),
    )
    ok = await fetch_web_screenshot(
        slide, storage, "http://helper.local:8888", "tok", 1920, 1080
    )
    assert ok is True
    assert slide.id not in web_screenshot._failed_slide_ids

    # 3) A later failure WARNINGs again (not DEBUG-suppressed).
    monkeypatch.setattr(
        httpx, "AsyncClient",
        _fake_client_class(
            raise_exc=httpx.ConnectError("down again")
        ),
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="openmarquee.web_screenshot"):
        await fetch_web_screenshot(
            slide, storage, "http://helper.local:8888", "tok", 1920, 1080
        )
    assert any(
        r.levelno == logging.WARNING and "request failed" in r.message
        for r in caplog.records
    )
