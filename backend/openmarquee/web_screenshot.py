"""Web-slide screenshot producer — renders one screenshot of a
WebSlide's URL ON-DEVICE and saves it as the slide's asset.

A WebSlide is "an image slide whose asset.png is auto-refreshed from an
on-device render of an operator-supplied URL". The Pi renders the page
itself with headless Chromium (`openmarquee.web_render`); this module
is the playback-side driver — it calls the renderer and overwrites the
slide's `asset.png` in place. (Historically the render happened
off-device — the sign fetched PNGs from a helper service the operator
ran; that is gone.)

The single entry point is `fetch_web_screenshot` — an async coroutine
the playback loop fire-and-forgets (`asyncio.create_task`) when a Web
slide's slot comes round and its screenshot is stale. It NEVER raises
out to its caller: every failure (a render error, a timeout, an
invalid URL, a save error) is caught, logged, and reported as a
`False` return. On failure the slide keeps its last-good `asset.png`
(or the create-time placeholder if no render has ever succeeded), so a
flaky render degrades gracefully rather than breaking playback.

Two structural points keep the on-device render off the playback path:

  - The Chromium render is a blocking, multi-second operation. It is
    run OFF the event loop via `asyncio.to_thread`, so the playback
    loop keeps painting while a render is in flight (the producer is
    fire-and-forget — `_loop` never awaits it).

  - A process-wide lock serializes renders: only one Chromium runs at
    a time. Two concurrent headless browsers would each claim hundreds
    of MB and blow the Pi's tight (~426 MB) memory budget. A second
    Web slide's refresh simply waits its turn — invisible, since the
    producer is fire-and-forget and the slide shows its last-good
    asset meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from openmarquee.content import WebSlide
from openmarquee.web_render import WebRenderError, render_web_png

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage

log = logging.getLogger(__name__)

# Process-wide single-flight lock for on-device renders. Only one
# Chromium may run at a time — two concurrent headless browsers would
# each claim hundreds of MB and OOM the ~426 MB Pi. asyncio.Lock binds
# to the running loop lazily (on first await), and the producer always
# runs on the backend's single event loop, so a module-level instance
# is correct. A render is bounded by web_render's 45s kill-and-reap
# timeout, so a waiter blocks at most ~that long.
_render_lock = asyncio.Lock()

# Slide ids whose most recent render FAILED. The first failure for an
# id logs at WARNING (the operator must see a broken Web slide);
# subsequent consecutive failures for the SAME id log at DEBUG, so a
# persistently-failing URL on a short refresh interval doesn't emit a
# WARNING every refresh forever. A success clears the id so a later
# failure warns afresh. Module-level (process-lifetime) — resets on
# restart, the right cadence for a "this slide is broken" signal.
_failed_slide_ids: set = set()


def _log_fetch_failure(slide: WebSlide, msg: str, *args) -> None:
    """Log a render failure with the first-fail-WARNING-then-DEBUG
    throttle. The first failure for `slide.id` logs WARNING; subsequent
    consecutive failures for the same id log DEBUG."""
    if slide.id in _failed_slide_ids:
        log.debug(msg + " (throttled)", *args)
    else:
        _failed_slide_ids.add(slide.id)
        log.warning(msg, *args)


async def fetch_web_screenshot(
    slide: WebSlide,
    storage: ContentStorage,
    width: int,
    height: int,
) -> bool:
    """Render `slide.url` on-device and save the screenshot as the
    slide's `asset.png`.

    Args:
        slide: the WebSlide to refresh. Its `url` is the page to render
            and its `id` is the storage key the screenshot is written
            under (via `storage.save_web`).
        storage: the content store. On a successful render this is
            called as `save_web(slide, png_bytes=<bytes>)` — the API
            that rewrites the envelope + asset.png in place.
        width, height: the render size in pixels — the sign's live
            display resolution. The caller (the playback loop) passes
            `renderer.width` / `renderer.height`, which the renderer
            reports rotation-aware, so the render matches the screen
            (landscape, or portrait when the sign is rotated).

    Returns:
        True if a screenshot was rendered and saved; False on any
        failure (a render error, a timeout, an invalid URL, a save
        error). NEVER raises — the playback loop fire-and-forgets this
        coroutine, so a raised exception would only surface as an
        unretrieved-task warning. On a False return the slide keeps its
        previous asset.png.
    """
    try:
        # The render is blocking + multi-second: run it OFF the event
        # loop. The lock serializes renders process-wide so only one
        # Chromium is ever resident. render_web_png validates the URL,
        # bounds Chromium with its own kill-and-reap timeout, and
        # PNG-signature-checks the result — it returns valid PNG bytes
        # or raises.
        async with _render_lock:
            png_bytes = await asyncio.to_thread(render_web_png, slide.url, width, height)
    except ValueError as exc:
        # render_web_png rejected the URL before spawning Chromium
        # (non-http/https scheme, control chars, a userinfo component).
        # A well-formed WebSlide is URL-validated at the model layer,
        # so this is a belt-and-suspenders path.
        _log_fetch_failure(
            slide,
            "web-screenshot: slide id=%s has an invalid URL %s; keeping last-good asset (%s)",
            slide.id,
            slide.url,
            exc,
        )
        return False
    except WebRenderError as exc:
        _log_fetch_failure(
            slide,
            "web-screenshot: on-device render failed for slide id=%s "
            "url=%s; keeping last-good asset (%s)",
            slide.id,
            slide.url,
            exc,
        )
        return False
    except Exception:
        # Defensive catch-all — the playback loop fire-and-forgets this
        # coroutine, so nothing must escape. log.exception always emits
        # the traceback — an unexpected error is worth seeing each time.
        _failed_slide_ids.add(slide.id)
        log.exception(
            "web-screenshot: unexpected error rendering slide id=%s "
            "url=%s; keeping last-good asset",
            slide.id,
            slide.url,
        )
        return False

    try:
        storage.save_web(slide, png_bytes=png_bytes)
    except Exception:
        # A save failure leaves the previous asset.png in place
        # (save_web drops the cache entry before the write, so a failed
        # write doesn't leave stale state). Log + report failure rather
        # than crash the fire-and-forget task.
        _failed_slide_ids.add(slide.id)
        log.exception(
            "web-screenshot: rendered slide id=%s but failed to save it; keeping last-good asset",
            slide.id,
        )
        return False

    # A success clears the throttle entry so the NEXT failure for this
    # id warns again rather than being DEBUG-suppressed.
    _failed_slide_ids.discard(slide.id)
    log.info(
        "web-screenshot: refreshed slide id=%s url=%s (%d bytes)",
        slide.id,
        slide.url,
        len(png_bytes),
    )
    return True


__all__ = ["fetch_web_screenshot"]
