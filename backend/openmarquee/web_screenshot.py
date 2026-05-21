"""Web-slide screenshot producer — fetches one screenshot from the
render helper and saves it as a WebSlide's asset.

A WebSlide is "an image slide whose asset.png is auto-refreshed from
the render helper" (`web-helper/`). The Raspberry Pi can't run a
browser, so the operator runs the helper on their own machine; this
module is the sign-side fetcher that pulls a fresh screenshot and
overwrites the slide's `asset.png` in place.

The single entry point is `fetch_web_screenshot` — an async coroutine
the playback loop fire-and-forgets (`asyncio.create_task`) when a Web
slide's slot comes round and its screenshot is stale. It NEVER raises
out to its caller: every failure (no helper configured, network
error, timeout, non-200, empty body) is caught, logged, and reported
as a `False` return. On failure the slide keeps its last-good
`asset.png` (or the create-time placeholder if no screenshot has ever
succeeded), so a flaky helper degrades gracefully rather than
breaking playback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from openmarquee.content import WebSlide

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage

log = logging.getLogger(__name__)

# Total request budget for one screenshot fetch. The helper renders a
# real page in headless Chromium, which can take up to ~10s (its own
# /shot handler maps a slow page to a 504). Give it comfortable
# headroom above that so a merely-slow page isn't clipped, while a
# truly dead helper still fails in bounded time — the playback loop
# never awaits this coroutine, but a timeout still bounds the in-flight
# window so a wedged helper can't pin a refresh slot forever.
WEB_SCREENSHOT_TIMEOUT_S = 20.0


async def fetch_web_screenshot(
    slide: WebSlide,
    storage: "ContentStorage",
    web_helper_url: str,
    web_helper_token: str,
    width: int,
    height: int,
) -> bool:
    """Fetch one screenshot of `slide.url` from the render helper and
    save it as the slide's `asset.png`.

    Args:
        slide: the WebSlide to refresh. Its `url` is the page to shoot
            and its `id` is the storage key the screenshot is written
            under (via `storage.save_web`).
        storage: the content store. On a successful fetch this is
            called as `save_web(slide, png_bytes=<bytes>)` — the P1
            API that rewrites the envelope + asset.png in place.
        web_helper_url: the helper's base address (Settings
            `web_helper_url`, e.g. `http://192.168.1.50:8888`). Empty
            string means no helper is configured — a clean no-op.
        web_helper_token: the shared bearer token (Settings
            `web_helper_token`). Sent as `Authorization: Bearer ...`;
            an empty token still sends the header (the helper rejects
            it with 401, which this function logs and tolerates).
        width, height: the panel dimensions — the viewport the helper
            renders the page at. The caller passes `renderer.width` /
            `renderer.height`, mirroring the Stream-slide path.

    Returns:
        True if a screenshot was fetched and saved; False on any
        failure (unconfigured helper, network error, timeout, non-200
        response, empty body) or a save error. NEVER raises — the
        playback loop fire-and-forgets this coroutine, so a raised
        exception would only surface as an unretrieved-task warning.
        On a False return the slide keeps its previous asset.png.
    """
    helper_url = (web_helper_url or "").strip()
    if not helper_url:
        # No helper configured — the slide just shows its placeholder /
        # last-good screenshot. One quiet log line, not a warning: an
        # unconfigured helper is a valid operator state, not an error.
        log.info(
            "web-screenshot: no render helper configured "
            "(web_helper_url empty); slide id=%s keeps its current "
            "asset",
            slide.id,
        )
        return False

    shot_url = f"{helper_url.rstrip('/')}/shot"
    params = {"url": slide.url, "w": width, "h": height}
    headers = {"Authorization": f"Bearer {web_helper_token}"}

    try:
        async with httpx.AsyncClient(timeout=WEB_SCREENSHOT_TIMEOUT_S) as client:
            response = await client.get(shot_url, params=params, headers=headers)
        if response.status_code != 200:
            log.warning(
                "web-screenshot: helper returned HTTP %d for slide "
                "id=%s url=%s; keeping last-good asset",
                response.status_code,
                slide.id,
                slide.url,
            )
            return False
        png_bytes = response.content
        if not png_bytes:
            log.warning(
                "web-screenshot: helper returned an empty body for "
                "slide id=%s url=%s; keeping last-good asset",
                slide.id,
                slide.url,
            )
            return False
    except httpx.TimeoutException as exc:
        log.warning(
            "web-screenshot: helper request timed out after %.0fs for "
            "slide id=%s url=%s; keeping last-good asset (%s)",
            WEB_SCREENSHOT_TIMEOUT_S,
            slide.id,
            slide.url,
            exc,
        )
        return False
    except httpx.HTTPError as exc:
        log.warning(
            "web-screenshot: helper request failed for slide id=%s "
            "url=%s; keeping last-good asset (%s)",
            slide.id,
            slide.url,
            exc,
        )
        return False
    except Exception:
        # Defensive catch-all — the playback loop fire-and-forgets this
        # coroutine, so nothing must escape. (httpx errors are covered
        # above; this guards anything unexpected, e.g. a bad helper_url
        # that slips past validation.)
        log.exception(
            "web-screenshot: unexpected error fetching slide id=%s "
            "url=%s; keeping last-good asset",
            slide.id,
            slide.url,
        )
        return False

    try:
        storage.save_web(slide, png_bytes=png_bytes)
    except Exception:
        # A save failure leaves the previous asset.png in place
        # (save_web -> save drops the cache entry before the write, so
        # a failed write doesn't leave stale state). Log + report
        # failure rather than crash the fire-and-forget task.
        log.exception(
            "web-screenshot: fetched a screenshot for slide id=%s but "
            "failed to save it; keeping last-good asset",
            slide.id,
        )
        return False

    log.info(
        "web-screenshot: refreshed slide id=%s url=%s (%d bytes)",
        slide.id,
        slide.url,
        len(png_bytes),
    )
    return True


__all__ = ["fetch_web_screenshot", "WEB_SCREENSHOT_TIMEOUT_S"]
