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

A third point gates the spawn itself on system memory pressure
(postmortem mitigation #3, 2026-05-23): `fetch_web_screenshot`
reads /proc/meminfo BEFORE acquiring the render lock and skips the
cycle when MemAvailable < OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB
(default 80 MB) OR SwapUsed > OPENMARQUEE_WEB_RENDER_SWAP_CEILING_MB
(default 30 MB). The skip is invisible to the sign (last-good
asset.png stays on screen) but breaks the swap-thrash → brcmfmac
SDIO CMD53 → WiFi-wedge chain that drove the 2026-05-23 outage.
The helper fails open on non-Linux (no /proc/meminfo) so dev
environments and CI keep rendering.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from openmarquee.content import WebSlide
from openmarquee.web_render import WebRenderError, render_web_png

if TYPE_CHECKING:
    from openmarquee.content.storage import ContentStorage

log = logging.getLogger(__name__)

# Memory-pressure gate thresholds (postmortem mitigation #3,
# 2026-05-23). The Pi Zero 2 W has only ~426 MB total RAM; spawning
# chromium-headless-shell adds ~100-150 MB peak. Sustained pressure
# manifests as brcmfmac SDIO CMD53 errors (the chronic WiFi
# instability substrate). Skip the render when MemAvailable is below
# the floor OR SwapUsed is above the ceiling — the slide keeps its
# previous asset.png, the sign keeps painting it, and the operator
# sees the skip in the INFO log timeline.
_MEM_FLOOR_MB_DEFAULT = 80
_SWAP_CEILING_MB_DEFAULT = 30
# /proc/meminfo lines look like `MemAvailable:    123456 kB`. The
# regex anchors both columns so a renamed/reformatted key is a hard
# parse failure rather than a silent miss.
_MEMINFO_LINE_RE = re.compile(r"^(\S+):\s+(\d+)\s+kB\s*$")
_MEMINFO_PATH = Path("/proc/meminfo")


def _mem_floor_mb() -> int:
    """Override default via OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB.

    Non-int or negative values fall back to the default — neither
    silently makes the gate unreachable nor inverts its meaning.
    """
    raw = os.environ.get("OPENMARQUEE_WEB_RENDER_MEM_FLOOR_MB")
    if not raw:
        return _MEM_FLOOR_MB_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _MEM_FLOOR_MB_DEFAULT
    return value if value >= 0 else _MEM_FLOOR_MB_DEFAULT


def _swap_ceiling_mb() -> int:
    """Override default via OPENMARQUEE_WEB_RENDER_SWAP_CEILING_MB.

    Non-int or negative values fall back to the default — a negative
    ceiling would make every render skip (swap_used >= 0 > negative),
    which is a foot-gun the operator almost certainly didn't intend.
    """
    raw = os.environ.get("OPENMARQUEE_WEB_RENDER_SWAP_CEILING_MB")
    if not raw:
        return _SWAP_CEILING_MB_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return _SWAP_CEILING_MB_DEFAULT
    return value if value >= 0 else _SWAP_CEILING_MB_DEFAULT


def _read_meminfo() -> tuple[int, int] | None:
    """Read /proc/meminfo, return (mem_available_mb, swap_used_mb).

    Fail-open: returns None on any failure (non-Linux host, missing
    keys, parse error). Callers interpret None as "can't measure ->
    don't skip" so dev environments without /proc/meminfo (macOS) and
    any future weird-Linux scenarios keep rendering. The gate is a
    safety mitigation, not a hard correctness gate — do NOT "fix"
    this to fail-closed without re-thinking dev/CI consequences.
    """
    try:
        text = _MEMINFO_PATH.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    kv: dict[str, int] = {}
    for line in text.splitlines():
        m = _MEMINFO_LINE_RE.match(line)
        if m:
            kv[m.group(1)] = int(m.group(2))
    try:
        mem_available_kb = kv["MemAvailable"]
        swap_total_kb = kv["SwapTotal"]
        swap_free_kb = kv["SwapFree"]
    except KeyError:
        return None
    return mem_available_kb // 1024, (swap_total_kb - swap_free_kb) // 1024


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
    # Memory-pressure gate (postmortem mitigation #3, 2026-05-23).
    # Skip the Chromium spawn entirely when the Pi is already swap-
    # thrashing — adding a ~100-150 MB browser to a ~20-60 MB-free
    # box is what drives the brcmfmac SDIO instability. The gate
    # runs BEFORE the _render_lock acquire so a skipped refresh is a
    # cheap fast-path (no contention with another in-flight render)
    # and returns False so the slide keeps its last-good asset.png —
    # consistent with every other failure path in this function.
    mem = _read_meminfo()
    if mem is not None:
        mem_available_mb, swap_used_mb = mem
        floor = _mem_floor_mb()
        ceiling = _swap_ceiling_mb()
        if mem_available_mb < floor or swap_used_mb > ceiling:
            log.info(
                "web-screenshot: skipping render for slide id=%s "
                "(url=%s) — memory pressure: MemAvailable=%dMB "
                "(floor=%dMB), SwapUsed=%dMB (ceiling=%dMB); "
                "keeping last-good asset",
                slide.id,
                slide.url,
                mem_available_mb,
                floor,
                swap_used_mb,
                ceiling,
            )
            return False

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
