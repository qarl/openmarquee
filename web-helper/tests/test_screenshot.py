"""Tests for the Playwright-backed screenshot worker hardening.

These tests exercise `screenshot.render_screenshot` WITHOUT a real
browser. Playwright is faked two ways:

  * `sys.modules` gets stub `playwright` / `playwright.async_api`
    modules, so the worker's deferred `from playwright.async_api import`
    lines resolve even on a host with no Playwright installed.
  * `screenshot._ensure_browser` is monkeypatched to hand back a fake
    Browser whose context/page/screenshot are all in-memory fakes.

That keeps the suite runnable with no real Playwright/Chromium, matching
the rest of the web-helper tests. The worker is async; rather than pull
in a `pytest-asyncio` plugin, each test drives its coroutine through a
plain `asyncio.run(...)`.
"""

import asyncio
import sys
import types

import pytest

from openmarquee_web_helper import screenshot as screenshot_mod


# --------------------------------------------------------------------------
# Playwright fakes
# --------------------------------------------------------------------------


class _FakePlaywrightError(Exception):
    """Stand-in for `playwright.async_api.Error`."""


class _FakePlaywrightTimeoutError(_FakePlaywrightError):
    """Stand-in for `playwright.async_api.TimeoutError`."""


@pytest.fixture(autouse=True)
def fake_playwright_module(monkeypatch):
    """Install stub `playwright` modules so the worker's deferred imports
    resolve without a real Playwright install."""
    async_api = types.ModuleType("playwright.async_api")
    async_api.Error = _FakePlaywrightError
    async_api.TimeoutError = _FakePlaywrightTimeoutError
    # `async_playwright` is only touched by `_ensure_browser`, which the
    # tests monkeypatch -- a placeholder keeps the attribute present.
    async_api.async_playwright = None

    pkg = types.ModuleType("playwright")
    pkg.async_api = async_api

    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    yield


@pytest.fixture(autouse=True)
def reset_semaphore(monkeypatch):
    """Give each test a fresh render semaphore so cap state never leaks."""
    monkeypatch.setattr(
        screenshot_mod,
        "_render_semaphore",
        asyncio.Semaphore(screenshot_mod.MAX_CONCURRENT_RENDERS),
    )
    yield


class _FakePage:
    """A fake Playwright Page. `screenshot` is the slow, observable step."""

    def __init__(self, on_render, seen):
        # `on_render` is an async callback run inside `screenshot()` --
        # the concurrency test uses it to watch how many renders overlap.
        # `seen` records call arguments for assertion.
        self._on_render = on_render
        self._seen = seen

    async def goto(self, url, wait_until, timeout):
        self._seen["wait_until"] = wait_until
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def screenshot(self, type):
        if self._on_render is not None:
            await self._on_render()
        return b"fake-png-bytes"


class _FakeContext:
    """A fake Playwright BrowserContext."""

    def __init__(self, on_render, seen):
        self._on_render = on_render
        self._seen = seen
        self.closed = False

    async def new_page(self):
        return _FakePage(self._on_render, self._seen)

    async def close(self):
        self.closed = True


class _FakeBrowser:
    """A fake Playwright Browser that always reports connected."""

    def __init__(self, on_render=None, seen=None):
        self._on_render = on_render
        self._seen = seen if seen is not None else {}

    def is_connected(self):
        return True

    async def new_context(self, viewport):
        return _FakeContext(self._on_render, self._seen)

    async def close(self):
        return None


# --------------------------------------------------------------------------
# H1 -- concurrency cap
# --------------------------------------------------------------------------


def test_render_screenshot_caps_concurrency(monkeypatch):
    """Firing more renders than the cap never runs more than
    MAX_CONCURRENT_RENDERS of them at once."""
    state = {"active": 0, "observed_max": 0}

    async def on_render():
        # Count this render as active, hold briefly so siblings overlap,
        # then release. The semaphore must keep `active` from exceeding
        # the cap.
        state["active"] += 1
        state["observed_max"] = max(state["observed_max"], state["active"])
        await asyncio.sleep(0.02)
        state["active"] -= 1

    async def fake_ensure_browser():
        return _FakeBrowser(on_render=on_render)

    monkeypatch.setattr(screenshot_mod, "_ensure_browser", fake_ensure_browser)

    async def drive():
        n = 6
        return await asyncio.gather(
            *(screenshot_mod.render_screenshot("https://example.com", 800, 600)
              for _ in range(n))
        )

    results = asyncio.run(drive())

    assert results == [b"fake-png-bytes"] * 6
    assert state["observed_max"] <= screenshot_mod.MAX_CONCURRENT_RENDERS
    # Sanity: with 6 renders the cap should actually have been reached.
    assert state["observed_max"] == screenshot_mod.MAX_CONCURRENT_RENDERS


# --------------------------------------------------------------------------
# M5 -- recycle a wedged browser
# --------------------------------------------------------------------------


def test_render_failure_recycles_browser(monkeypatch):
    """A render that raises tears the shared browser down (so the next
    request relaunches) and still propagates the original error."""
    shutdown_calls = {"n": 0}

    async def fake_shutdown_browser():
        shutdown_calls["n"] += 1

    async def boom_on_render():
        raise screenshot_mod.ScreenshotError("page crashed mid-render")

    async def fake_ensure_browser():
        return _FakeBrowser(on_render=boom_on_render)

    monkeypatch.setattr(screenshot_mod, "shutdown_browser", fake_shutdown_browser)
    monkeypatch.setattr(screenshot_mod, "_ensure_browser", fake_ensure_browser)

    with pytest.raises(screenshot_mod.ScreenshotError):
        asyncio.run(
            screenshot_mod.render_screenshot("https://example.com", 800, 600)
        )

    # The wedged browser was torn down so the next request gets a fresh one.
    assert shutdown_calls["n"] == 1


def test_render_failure_in_cleanup_does_not_mask_error(monkeypatch):
    """A failure INSIDE shutdown_browser must not hide the real error."""

    async def failing_shutdown_browser():
        raise RuntimeError("cleanup itself failed")

    async def boom_on_render():
        raise screenshot_mod.ScreenshotError("original render failure")

    async def fake_ensure_browser():
        return _FakeBrowser(on_render=boom_on_render)

    monkeypatch.setattr(
        screenshot_mod, "shutdown_browser", failing_shutdown_browser
    )
    monkeypatch.setattr(screenshot_mod, "_ensure_browser", fake_ensure_browser)

    # The caller still sees the ScreenshotError, not the cleanup RuntimeError.
    with pytest.raises(screenshot_mod.ScreenshotError, match="original render"):
        asyncio.run(
            screenshot_mod.render_screenshot("https://example.com", 800, 600)
        )


# --------------------------------------------------------------------------
# M2 -- drop networkidle
# --------------------------------------------------------------------------


def test_render_uses_wait_until_load(monkeypatch):
    """`goto` is called with wait_until='load', not 'networkidle' --
    'networkidle' hangs on never-idle live dashboards."""
    seen = {}

    async def fake_ensure_browser():
        return _FakeBrowser(seen=seen)

    monkeypatch.setattr(screenshot_mod, "_ensure_browser", fake_ensure_browser)

    asyncio.run(
        screenshot_mod.render_screenshot("https://example.com", 800, 600)
    )

    assert seen["wait_until"] == "load"


# --------------------------------------------------------------------------
# M4 -- Docker /dev/shm hardening
# --------------------------------------------------------------------------


def test_ensure_browser_passes_disable_dev_shm_usage(monkeypatch):
    """Chromium is launched with --disable-dev-shm-usage so a 64MB Docker
    /dev/shm does not crash headless Chromium ("Target closed")."""
    seen = {}

    class _FakeChromium:
        async def launch(self, headless, args):
            seen["headless"] = headless
            seen["args"] = args
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self):
            return None

    class _FakeAsyncPlaywrightCM:
        async def start(self):
            return _FakePlaywright()

    def fake_async_playwright():
        return _FakeAsyncPlaywrightCM()

    # `_ensure_browser` reaches `playwright.async_api.async_playwright`
    # through the stub module installed by `fake_playwright_module`.
    monkeypatch.setattr(
        sys.modules["playwright.async_api"],
        "async_playwright",
        fake_async_playwright,
    )
    # Force a relaunch: clear any browser left from an earlier test.
    monkeypatch.setattr(screenshot_mod, "_browser", None)
    monkeypatch.setattr(screenshot_mod, "_playwright", None)

    asyncio.run(screenshot_mod._ensure_browser())
    # Reset the singletons so this fake browser does not leak to siblings.
    screenshot_mod._browser = None
    screenshot_mod._playwright = None

    assert seen["headless"] is True
    assert "--disable-dev-shm-usage" in seen["args"]


# --------------------------------------------------------------------------
# Module constants
# --------------------------------------------------------------------------


def test_module_constants_are_sane():
    """The concurrency cap is a small positive int."""
    assert isinstance(screenshot_mod.MAX_CONCURRENT_RENDERS, int)
    assert screenshot_mod.MAX_CONCURRENT_RENDERS >= 1
