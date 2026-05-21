"""The Playwright-backed screenshot worker.

This module is imported LAZILY -- only the first time a real render is
performed (see `app.render_screenshot` indirection). That keeps the
`playwright` import out of the import path of the FastAPI app and the
test suite, so the tests run on a host without Playwright/Chromium
installed.

Browser lifecycle: a single headless Chromium is launched once and
reused across requests; each request gets a fresh browser *context*
(isolated cookies/cache) and page. Launching Chromium per-request costs
~1-2s, which the openMarquee playback loop cannot absorb -- reuse keeps
`/shot` responsive. The browser is launched lazily on first use and
guarded by an asyncio lock so concurrent first requests do not race.
"""

import asyncio

# Page-load budget. `goto` raises a Playwright TimeoutError past this.
_GOTO_TIMEOUT_MS = 20_000

# Extra fixed settle after the load event, to let JS-rendered content
# (SPAs, late-painting widgets) finish before the screenshot is taken.
_SETTLE_MS = 750

# Cap on concurrent renders. Each in-flight render holds a Chromium
# context + page; left unbounded, a burst of `/shot` requests piles up
# contexts and blows host memory. Requests past the cap queue on the
# semaphore -- harmless, since the producer's HTTP timeout bounds the
# wait.
MAX_CONCURRENT_RENDERS = 3


class ScreenshotTimeout(Exception):
    """Page load exceeded the timeout budget. Maps to HTTP 504."""


class ScreenshotError(Exception):
    """Page failed to load for a non-timeout reason. Maps to HTTP 502."""


# Module-level singletons for the reused Playwright + browser. Populated
# lazily by `_ensure_browser`.
_playwright = None
_browser = None
_browser_lock = asyncio.Lock()

# Bounds the number of renders running at once (see MAX_CONCURRENT_RENDERS).
_render_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)


async def _ensure_browser():
    """Launch the shared Chromium once; return the reused Browser handle."""
    global _playwright, _browser

    if _browser is not None and _browser.is_connected():
        return _browser

    async with _browser_lock:
        # Re-check inside the lock -- another coroutine may have launched
        # the browser while we were waiting.
        if _browser is not None and _browser.is_connected():
            return _browser

        # Deferred import: pulling Playwright in only when an actual
        # render happens keeps it off the test/import path.
        from playwright.async_api import async_playwright

        _playwright = await async_playwright().start()
        # `--disable-dev-shm-usage`: Docker's default /dev/shm is 64MB,
        # which headless Chromium overruns and crashes ("Target closed").
        # Writing shared memory to /tmp instead is the standard hardening
        # flag -- also helps the pipx path on memory-constrained hosts.
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        return _browser


async def render_screenshot(url: str, width: int, height: int) -> bytes:
    """Render `url` at the given viewport and return PNG bytes.

    Drives headless Chromium: open a fresh isolated context + page at the
    requested viewport, navigate, wait for the load event, settle briefly,
    then take a full-viewport PNG screenshot.

    Concurrency is capped by a module-level semaphore; a burst past the
    cap simply queues here.

    Raises:
        ScreenshotTimeout: the page did not load within the budget.
        ScreenshotError: the page failed to load for another reason.
    """
    # Import the Playwright error types lazily, alongside the browser.
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    # Cap concurrent renders: each one holds a Chromium context + page.
    async with _render_semaphore:
        browser = await _ensure_browser()

        try:
            context = await browser.new_context(
                viewport={"width": width, "height": height},
            )
            try:
                page = await context.new_page()
                try:
                    # `load` waits for sub-resources but NOT for the
                    # network to go idle -- `networkidle` hangs the full
                    # timeout on long-poll / websocket / polling pages
                    # (i.e. most live dashboards). The fixed `_SETTLE_MS`
                    # below still gives JS-rendered content time to paint.
                    await page.goto(
                        url, wait_until="load", timeout=_GOTO_TIMEOUT_MS
                    )
                except PlaywrightTimeoutError as exc:
                    raise ScreenshotTimeout(
                        f"timed out loading {url} after {_GOTO_TIMEOUT_MS} ms"
                    ) from exc
                except PlaywrightError as exc:
                    raise ScreenshotError(
                        f"failed to load {url}: {exc}"
                    ) from exc

                # A short extra settle for late-painting widgets.
                await page.wait_for_timeout(_SETTLE_MS)

                return await page.screenshot(type="png")
            finally:
                # Always tear down the per-request context; the browser
                # stays up for reuse.
                await context.close()
        except Exception:
            # A browser that wedged connected-but-unresponsive passes
            # `is_connected()`, so `_ensure_browser` would never relaunch
            # it -- every later request would fail too. Tear it down here
            # (best-effort) so the NEXT request gets a fresh browser.
            # This is cleanup only: re-raise the original error so the
            # caller still maps it to 502/504.
            try:
                await shutdown_browser()
            except Exception:
                # A failure inside cleanup must not mask the real error.
                pass
            raise


async def shutdown_browser() -> None:
    """Close the shared browser + Playwright. Called from app shutdown."""
    global _playwright, _browser

    if _browser is not None:
        try:
            await _browser.close()
        finally:
            _browser = None

    if _playwright is not None:
        try:
            await _playwright.stop()
        finally:
            _playwright = None
