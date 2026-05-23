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

from openmarquee.content import WebSlide
from openmarquee.content.storage import ContentStorage
from openmarquee import web_screenshot
from openmarquee.web_screenshot import fetch_web_screenshot
from openmarquee.web_render import WebRenderError

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


def _install_render(monkeypatch, *, png=None, raise_exc=None, calls=None,
                    on_call=None):
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

    _install_render(
        monkeypatch, raise_exc=WebRenderError("Chromium crashed")
    )

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

    _install_render(
        monkeypatch, raise_exc=ValueError("unsupported URL scheme")
    )

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
async def test_first_failure_warns_repeat_failure_debugs(
    tmp_path, monkeypatch, caplog
):
    """The first failure for a slide id logs WARNING; a second
    consecutive failure for the same id logs DEBUG — so a persistently
    broken URL on a short refresh interval doesn't WARNING-spam."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    _install_render(
        monkeypatch, raise_exc=WebRenderError("render failed")
    )

    with caplog.at_level(logging.DEBUG, logger="openmarquee.web_screenshot"):
        ok1 = await fetch_web_screenshot(slide, storage, 1360, 768)
        first_records = list(caplog.records)
        caplog.clear()
        ok2 = await fetch_web_screenshot(slide, storage, 1360, 768)
        second_records = list(caplog.records)

    assert ok1 is False and ok2 is False
    # First failure -> WARNING.
    assert any(
        r.levelno == logging.WARNING and "render failed" in r.message
        for r in first_records
    )
    # Second failure for the SAME id -> DEBUG, not WARNING.
    failure_lines = [
        r for r in second_records if "render failed" in r.message
    ]
    assert failure_lines
    assert all(r.levelno == logging.DEBUG for r in failure_lines)


@pytest.mark.asyncio
async def test_success_clears_the_failure_throttle(
    tmp_path, monkeypatch, caplog
):
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
    assert any(
        r.levelno == logging.WARNING and "down again" in r.message
        for r in caplog.records
    )


# --- memory-pressure gate (postmortem mitigation #3, 2026-05-23) ----------
#
# fetch_web_screenshot reads /proc/meminfo before acquiring the
# render lock and skips the cycle when MemAvailable is below floor
# OR SwapUsed is above ceiling. The skip returns False (same
# contract as every other failure path — keeps last-good asset).
#
# Mocking pattern: monkeypatch `web_screenshot._read_meminfo` to
# return a chosen `(mem_available_mb, swap_used_mb)` tuple, or None
# to simulate the fail-open path (dev macOS, no /proc/meminfo).


@pytest.mark.asyncio
async def test_skips_when_mem_available_under_floor(
    tmp_path, monkeypatch, caplog
):
    """MemAvailable below the 80 MB default floor -> skip + False +
    no render call. INFO-level log naming the pressure and thresholds
    so the operator sees the timeline."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setattr(
        web_screenshot, "_read_meminfo", lambda: (70, 10)
    )

    with caplog.at_level(logging.INFO, logger="openmarquee.web_screenshot"):
        ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert calls == []  # render never invoked
    assert storage.read_asset(slide.id) == before  # asset untouched
    assert any(
        "skipping render" in r.message
        and "MemAvailable=70MB" in r.message
        and "floor=80MB" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_skips_when_swap_used_over_ceiling(
    tmp_path, monkeypatch, caplog
):
    """SwapUsed above the 30 MB default ceiling -> skip + False + no
    render call, even when MemAvailable is comfortable."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)
    before = storage.read_asset(slide.id)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setattr(
        web_screenshot, "_read_meminfo", lambda: (200, 40)
    )

    with caplog.at_level(logging.INFO, logger="openmarquee.web_screenshot"):
        ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert calls == []
    assert storage.read_asset(slide.id) == before
    assert any(
        "skipping render" in r.message
        and "SwapUsed=40MB" in r.message
        and "ceiling=30MB" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_proceeds_when_memory_ok(tmp_path, monkeypatch):
    """Comfortable headroom -> render runs as usual."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setattr(
        web_screenshot, "_read_meminfo", lambda: (200, 10)
    )

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is True
    assert calls == [("https://status.example.com", 1360, 768)]


@pytest.mark.asyncio
async def test_proceeds_when_meminfo_unavailable(tmp_path, monkeypatch):
    """_read_meminfo returns None (dev macOS, missing /proc/meminfo)
    -> fail-open, render runs. The gate is a safety mitigation, not a
    correctness gate; refusing to render on every dev machine would
    break CI for no benefit."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setattr(web_screenshot, "_read_meminfo", lambda: None)

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is True
    assert calls == [("https://status.example.com", 1360, 768)]


@pytest.mark.asyncio
async def test_env_var_overrides_floor(tmp_path, monkeypatch):
    """OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB raises the floor; readings
    that would have passed the default 80 now skip. Same shape applies
    to OPENMARQUEE_WEB_RENDER_SWAP_CEILING_MB (symmetric envelope —
    one env-var test fences the lookup; static-parse fences both
    names exist)."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setenv("OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB", "200")
    # 150 MB available would pass the default 80 floor; with the env
    # override at 200 it must now skip.
    monkeypatch.setattr(
        web_screenshot, "_read_meminfo", lambda: (150, 10)
    )

    ok = await fetch_web_screenshot(slide, storage, 1360, 768)

    assert ok is False
    assert calls == []


@pytest.mark.asyncio
async def test_skip_does_not_acquire_render_lock(tmp_path, monkeypatch):
    """The gate fires BEFORE the _render_lock acquire. A wedged
    in-flight render (lock held by another task) must not block the
    skip path — the skip returns immediately regardless of lock
    contention. This pins the postmortem-named invariant: a skip is
    a cheap fast-path, not a serialized one."""
    storage = ContentStorage(tmp_path)
    slide = _web_slide()
    storage.save_web(slide)

    calls: list = []
    _install_render(monkeypatch, png=_PNG_1x1, calls=calls)
    monkeypatch.setattr(
        web_screenshot, "_read_meminfo", lambda: (10, 99)
    )

    # Hold the render lock from a separate task that never releases.
    # If the gate ran AFTER the lock acquire, fetch_web_screenshot
    # would block forever waiting on the lock.
    await web_screenshot._render_lock.acquire()
    try:
        # 0.5s is generous — the skip path is microseconds in practice.
        # asyncio.wait_for raises TimeoutError if the call blocks.
        ok = await asyncio.wait_for(
            fetch_web_screenshot(slide, storage, 1360, 768),
            timeout=0.5,
        )
    finally:
        web_screenshot._render_lock.release()

    assert ok is False
    assert calls == []  # render never invoked


# --- /proc/meminfo parser ------------------------------------------------


def test_read_meminfo_returns_none_off_linux(monkeypatch, tmp_path):
    """When /proc/meminfo is absent (the dev macOS host running this
    suite), the helper returns None — the fail-open signal to the
    gate. Forces the path explicitly via a missing tmp file so this
    test is deterministic on either host."""
    monkeypatch.setattr(
        web_screenshot, "_MEMINFO_PATH", tmp_path / "no-such-file"
    )
    assert web_screenshot._read_meminfo() is None


def test_read_meminfo_parses_valid_format(monkeypatch, tmp_path):
    """A well-formed /proc/meminfo parses to the expected (mem_mb,
    swap_used_mb) tuple. Numbers: 122880 kB = 120 MB; SwapUsed =
    SwapTotal - SwapFree = 102400 - 71680 = 30720 kB = 30 MB."""
    fake = tmp_path / "meminfo"
    fake.write_text(
        "MemTotal:         425984 kB\n"
        "MemFree:           20480 kB\n"
        "MemAvailable:     122880 kB\n"
        "SwapTotal:        102400 kB\n"
        "SwapFree:          71680 kB\n"
    )
    monkeypatch.setattr(web_screenshot, "_MEMINFO_PATH", fake)
    assert web_screenshot._read_meminfo() == (120, 30)


def test_read_meminfo_returns_none_on_missing_keys(monkeypatch, tmp_path):
    """A meminfo with the lines but missing one of the three required
    keys (a future kernel rename, say) -> None, fail-open. The
    refactor lands a clean signal rather than a KeyError surfacing
    up the playback path."""
    fake = tmp_path / "meminfo"
    # No SwapTotal/SwapFree — the helper can't compute swap_used.
    fake.write_text(
        "MemAvailable:     122880 kB\n"
    )
    monkeypatch.setattr(web_screenshot, "_MEMINFO_PATH", fake)
    assert web_screenshot._read_meminfo() is None
