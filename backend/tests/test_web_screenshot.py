"""Tests for the Web-slide screenshot producer.

`fetch_web_screenshot` renders a WebSlide's URL ON-DEVICE (headless
Chromium, via `openmarquee.web_render.render_web_png`) and saves the
result as the slide's asset.png. It must never raise — every failure
path is caught, logged, and reported as a `False` return, leaving the
slide's previous asset untouched.

`render_web_png` is mocked (monkeypatched on the `web_screenshot`
module) so the suite runs without Chromium installed. The real
ContentStorage is used against a tmp_path so the asset write is
exercised end to end.
"""

import asyncio
import logging
import time

import pytest

from openmarquee import web_screenshot
from openmarquee.content import WebSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.web_render import WebRenderError
from openmarquee.web_screenshot import fetch_web_screenshot

# A tiny but valid 1x1 PNG — enough that ContentStorage.save_web writes
# it verbatim (save_web with explicit bytes doesn't re-decode).
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000"
    "00907753de0000000c4944415408d76360606000000000040001"
    "5c0c02b00000000049454e44ae426082"
)


@pytest.fixture(autouse=True)
def _clear_failure_throttle():
    """The failure-throttle set is module-level (process-lifetime).
    Clear it around each test so one test's failures don't leak into
    another's first-fail-WARNING expectation."""
    web_screenshot._failed_slide_ids.clear()
    yield
    web_screenshot._failed_slide_ids.clear()


def _install_render(monkeypatch, *, png=None, raise_exc=None, calls=None, on_call=None):
    """Patch `web_screenshot.render_web_png` with a synchronous fake.

    `png` is returned; `raise_exc` is raised instead. `calls` (a list)
    records each `(url, width, height)`. `on_call` is an optional hook
    invoked inside the fake (used by the serialization test).
    """

    def _fake_render(url, width, height):
        if calls is not None:
            calls.append((url, width, height))
        if on_call is not None:
            on_call()
        if raise_exc is not None:
            raise raise_exc
        return png if png is not None else _PNG_1x1

    monkeypatch.setattr(web_screenshot, "render_web_png", _fake_render)


def _web_slide(**kwargs) -> WebSlide:
    kwargs.setdefault("name", "Status")
    kwargs.setdefault("url", "https://status.example.com")
    return WebSlide(**kwargs)


# --- success path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_render_writes_asset_on_success(tmp_path, monkeypatch):
    """A successful render -> the PNG bytes land in the slide's
    asset.png and the function returns True."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    # Seed a placeholder so save_web overwrites a real prior asset.
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is True
    assert storage.read_asset(slide.id) == _PNG_1x1
    # render_web_png was called once with the slide URL + display dims.
    assert calls == [("https://status.example.com", 1360, 768)]


@pytest.mark.asyncio
async def test_render_uses_passed_display_dims(tmp_path, monkeypatch):
    """The width/height handed in (the live, rotation-aware display
    resolution) are passed straight to render_web_png — a portrait
    resolution renders portrait."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, calls=calls)

    ok = await fetch_web_screenshot(slide, storage, 768, 1360)

    assert ok is True
    assert calls == [("https://status.example.com", 768, 1360)]


# --- failure paths: never raise, asset untouched --------------------------


@pytest.mark.asyncio
async def test_render_error_leaves_asset_untouched(tmp_path, monkeypatch):
    """A WebRenderError from the render -> False, no raise, and the
    prior asset is untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    _install_render(monkeypatch, raise_exc=WebRenderError("Chromium crashed"))

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_invalid_url_leaves_asset_untouched(tmp_path, monkeypatch):
    """A ValueError from the render (render_web_png rejected the URL)
    -> False, no raise, asset untouched."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    _install_render(monkeypatch, raise_exc=ValueError("unsupported URL scheme"))

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_unexpected_error_does_not_raise(tmp_path, monkeypatch):
    """An unexpected exception from the render is caught -> False, no
    raise out (the playback loop fire-and-forgets this coroutine)."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    _install_render(monkeypatch, raise_exc=RuntimeError("boom"))

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert storage.read_asset(slide.id) == before


@pytest.mark.asyncio
async def test_save_failure_does_not_raise(tmp_path, monkeypatch):
    """A failure inside save_web is caught -> False, no raise out."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    _install_render(monkeypatch, png=_PNG_1x1)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(storage, "save_web", _boom)

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False


# --- single-flight: renders are serialized --------------------------------


@pytest.mark.asyncio
async def test_renders_are_serialized(tmp_path, monkeypatch):
    """The process-wide lock serializes renders — two concurrent
    producers never run render_web_png at the same time (two resident
    Chromium processes would OOM the Pi)."""
    storage = ContentStorage(tmp_path)
    slide_a = _web_slide(name="A", url="https://a.example.com")
    slide_b = _web_slide(name="B", url="https://b.example.com")
    storage.save_web(slide_a)
    storage.save_web(slide_b)

    concurrent = 0
    max_concurrent = 0

    def _on_call():
        nonlocal concurrent, max_concurrent
        concurrent += 1
        max_concurrent = max(max_concurrent, concurrent)
        time.sleep(0.05)  # hold the "render" open so an overlap shows
        concurrent -= 1

    _install_render(monkeypatch, png=_PNG_1x1, on_call=_on_call)

    results = await asyncio.gather(
        fetch_web_screenshot(slide_a, storage, 1360, 768),
        fetch_web_screenshot(slide_b, storage, 1360, 768),
    )

    assert results == [True, True]
    assert max_concurrent == 1  # never two renders at once


# --- L3: per-failure WARNING-then-DEBUG throttle --------------------------


@pytest.mark.asyncio
async def test_first_failure_warns_repeat_failure_debugs(tmp_path, monkeypatch, caplog):
    """The first failure for a slide id logs WARNING; a second
    consecutive failure for the same id logs DEBUG — so a persistently
    broken URL on a short refresh interval doesn't WARNING-spam."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    _install_render(monkeypatch, raise_exc=WebRenderError("render failed"))

    with caplog.at_level(logging.DEBUG, logger="openmarquee.web_screenshot"):
        ok1 = await fetch_web_screenshot(slide, storage, 1360, 768)
        first_records = list(caplog.records)
        caplog.clear()
        ok2 = await fetch_web_screenshot(slide, storage, 1360, 768)
        second_records = list(caplog.records)

    assert ok1 is False and ok2 is False
    # First failure -> WARNING.
    assert any(r.levelno == logging.WARNING and "render failed" in r.message for r in first_records)
    # Second failure for the SAME id -> DEBUG, not WARNING.
    failure_lines = [r for r in second_records if "render failed" in r.message]
    assert failure_lines
    assert all(r.levelno == logging.DEBUG for r in failure_lines)


@pytest.mark.asyncio
async def test_success_clears_the_failure_throttle(tmp_path, monkeypatch, caplog):
    """A success between failures clears the throttle entry, so the
    next failure for that id WARNINGs afresh rather than being
    DEBUG-suppressed."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    # 1) A failure -> marks the id throttled.
    _install_render(monkeypatch, raise_exc=WebRenderError("down"))
    await fetch_web_screenshot(slide, storage, 1360, 768)
    assert slide.id in web_screenshot._failed_slide_ids

    # 2) A success -> clears the throttle entry.
    _install_render(monkeypatch, png=_PNG_1x1)
    ok = await fetch_web_screenshot(slide, storage, 1360, 768)
    assert ok is True
    assert slide.id not in web_screenshot._failed_slide_ids

    # 3) A later failure WARNINGs again (not DEBUG-suppressed).
    _install_render(monkeypatch, raise_exc=WebRenderError("down again"))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="openmarquee.web_screenshot"):
        await fetch_web_screenshot(slide, storage, 1360, 768)
    assert any(r.levelno == logging.WARNING and "down again" in r.message for r in caplog.records)


# r12 (2026-05-26) memory audit: the throttle map went from bare
# `set` to `dict[UUID, float]` + TTL prune-on-access. These tests
# pin the new bounding behavior so a future refactor that drops
# the prune resurrects the leak.


def test_failed_slide_throttle_prunes_entries_older_than_ttl(monkeypatch):
    """Entries with a timestamp older than _FAILED_SLIDE_TTL_SECONDS
    drop from the throttle map on the next prune call. Pre-r12 the
    set was unbounded; failing slides accumulated entries forever."""

    # Seed the throttle map with 3 entries: 2 stale (older than the
    # TTL), 1 fresh. _prune_failed_slide_ids should drop the 2.
    now = 1_000_000.0
    ttl = web_screenshot._FAILED_SLIDE_TTL_SECONDS
    stale_a = uuid.uuid4()
    stale_b = uuid.uuid4()
    fresh = uuid.uuid4()
    web_screenshot._failed_slide_ids[stale_a] = now - ttl - 1
    web_screenshot._failed_slide_ids[stale_b] = now - ttl - 60
    web_screenshot._failed_slide_ids[fresh] = now - ttl + 60  # within TTL

    web_screenshot._prune_failed_slide_ids(now=now)

    assert stale_a not in web_screenshot._failed_slide_ids
    assert stale_b not in web_screenshot._failed_slide_ids
    assert fresh in web_screenshot._failed_slide_ids


@pytest.mark.asyncio
async def test_failed_slide_throttle_refreshes_timestamp_on_repeated_failure(tmp_path, monkeypatch):
    """A repeat failure for the SAME id refreshes the timestamp,
    keeping the entry within the TTL window. This is the
    'persistently broken slide stays throttled' contract — without
    the refresh, a long-broken slide would WARN every TTL period.

    Pin the refresh by stamping a fake `time.monotonic()` so the
    test doesn't depend on real wall-clock + verify the entry's
    timestamp moves forward across two failures."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    fake_clock = [1000.0]
    monkeypatch.setattr(web_screenshot.time, "monotonic", lambda: fake_clock[0])
    _install_render(monkeypatch, raise_exc=WebRenderError("down"))

    # First failure at t=1000.
    await fetch_web_screenshot(slide, storage, 1360, 768)
    t1 = web_screenshot._failed_slide_ids[slide.id]
    assert t1 == 1000.0

    # Second failure at t=2000 — entry should refresh, not duplicate.
    fake_clock[0] = 2000.0
    await fetch_web_screenshot(slide, storage, 1360, 768)
    t2 = web_screenshot._failed_slide_ids[slide.id]
    assert t2 == 2000.0
    assert t2 > t1
    # Map size stays at 1 across both failures (refresh, not append).
    assert len(web_screenshot._failed_slide_ids) == 1


import uuid  # noqa: E402  (bottom to avoid disturbing existing import order)
