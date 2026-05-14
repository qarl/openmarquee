"""Playback-time rendering for auto-mode text slides.

A TextSlide with `auto_mode` set carries its styling + a placeholder
text (for the pallet thumbnail), but the PNG that actually plays on
the device is composited here each tick: current time / date / day in
the operator's configured timezone, drawn on top of the slide's
background (solid color OR a referenced ImageSlide's PNG) using the
slide's font / color / size metadata.

The stored `asset.png` on disk stays as the placeholder preview; the
playback loop asks this module for a fresh RGB frame every second (for
time) or once per minute (for date / day) instead.

Module is pure Pillow + stdlib so the test suite runs without hitting
a device — tests pass a `now` in and assert the rendered bytes.
"""

from __future__ import annotations

import io
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from PIL import Image

from openmarquee.content import TextSlide

log = logging.getLogger(__name__)


# Default auto_format per mode — used when the slide has auto_format=None.
_DEFAULT_FORMAT = {
    "time": "time_hm",
    "date": "date_iso",
    "day": "day_long",
}


def render_auto_text_for_layer(layer, now: datetime) -> str:
    """Per-layer variant of `render_auto_text`. Returns the visible
    string for one TextLayer at `now` — handles auto-mode formatting
    if the layer has it set, otherwise returns `layer.text`.

    Hoisted out so the unified per-tick composer (motion.py) can drive
    auto-mode rendering for ANY layer in a multi-layer slide, not just
    text_layers[0]. The slide-level wrapper `render_auto_text` is kept
    as a compat shim for the older single-layer callers.

    `now` is expected to already be in the target timezone — this fn
    doesn't convert. Callers should pass `datetime.now(ZoneInfo(tz))`.
    """
    if not getattr(layer, "auto_mode", None):
        return getattr(layer, "text", "")

    fmt = layer.auto_format or _DEFAULT_FORMAT.get(layer.auto_mode)

    if layer.auto_mode == "time":
        if fmt == "time_hms":
            return now.strftime("%H:%M:%S")
        return now.strftime("%H:%M")

    if layer.auto_mode == "date":
        if fmt == "date_iso":
            return now.strftime("%Y-%m-%d")
        if fmt == "date_medium":
            # e.g. "Apr 21" — strip the leading zero off %d without
            # relying on the %-d glibc extension (works on Linux + Mac
            # but not portable to Windows; tests run on Mac).
            return now.strftime("%b ") + str(now.day)
        # date_long default: "April 21, 2026"
        return now.strftime("%B ") + f"{now.day}, {now.year}"

    if layer.auto_mode == "day":
        if fmt == "day_short":
            return now.strftime("%a")
        return now.strftime("%A")

    # Unknown mode — fall through to typed text so playback doesn't crash
    # if a future mode ships ahead of this helper.
    return layer.text


def render_auto_text(slide: TextSlide, now: datetime) -> str:
    """Slide-level compat wrapper for `render_auto_text_for_layer`.

    Reads text_layers[0] only — single-layer behavior preserved for
    test_auto_render.py + any ad-hoc string-only callers. Multi-
    layer auto-mode rendering goes through
    `render_auto_text_for_layer` directly via the unified composer
    (motion.compose_motion_frame).
    """
    return render_auto_text_for_layer(slide.text_layers[0], now)


def resolve_timezone(tz_name: str | None) -> ZoneInfo:
    """Coerce a tz string (e.g. 'America/Los_Angeles') to a ZoneInfo.

    Empty / None / invalid → UTC, so a broken settings file doesn't
    crash the playback loop — slides render in UTC with a log line.
    """
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        log.warning("auto_render: unknown timezone %r, falling back to UTC", tz_name)
        return ZoneInfo("UTC")


# --- internals ---


# Perf counters (Batch 8.1 + 8.6).
# load_background_calls: per-entry; bumps even on solid / gradient /
#   pattern branches.
# png_decodes: actual Image.open + convert in the image-bg branch.
# image_bg_cache_hits (Batch 8.6): LRU hits on the (slide_id, w, h)
#   path -- delta vs png_decodes is the cache-effectiveness signal.
_stats: dict[str, int] = {
    "load_background_calls": 0,
    "png_decodes": 0,
    "image_bg_cache_hits": 0,
}


def stats_snapshot() -> dict[str, int]:
    return dict(_stats)


# Image-bg LRU (Batch 8.6). Keyed by (slide_id, width, height).
# Compose_motion_frame ALREADY has a per-slide background_cache
# parameter that caches the loaded bg across the frames of one
# slide; the LRU here covers the seam BETWEEN slides -- when the
# playback loop swaps between two slides that both reference the
# same image-bg (e.g. all clock slides share a brick-wall bg), each
# slide entry hits this cache instead of re-decoding the PNG.
#
# 4 entries (Batch 8.fix). Sweep review note: typical reels carry
# 2-5 distinct image-bg references. 4 covers the median + leaves a
# little headroom; the 8-entry default would have parked ~50 MB at
# 1080p (4×8.4 MB × 1.5 for RGB+resize), which is meaningful on
# Pi Zero 2 W. clear_image_bg_cache() is the safety valve if a
# future operator-driven scenario blows past 4.
_IMAGE_BG_LRU_MAX = 4
_image_bg_cache: OrderedDict[tuple[UUID, int, int], Image.Image] = OrderedDict()


def clear_image_bg_cache() -> None:
    """Drop the LRU. Test hook + safety valve."""
    _image_bg_cache.clear()


def load_background(
    slide: TextSlide,
    width: int,
    height: int,
    read_asset: Callable[[UUID], bytes] | None,
) -> Image.Image:
    """Build the background layer: image slide ref, gradient, or solid fill."""
    _stats["load_background_calls"] += 1
    if slide.background_image_slide_id is not None and read_asset is not None:
        cache_key = (slide.background_image_slide_id, width, height)
        cached = _image_bg_cache.get(cache_key)
        if cached is not None:
            _stats["image_bg_cache_hits"] += 1
            # Move to most-recently-used end so the OrderedDict
            # naturally evicts the LRU entry on overflow.
            _image_bg_cache.move_to_end(cache_key)
            return cached
        try:
            png = read_asset(slide.background_image_slide_id)
            _stats["png_decodes"] += 1
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.size != (width, height):
                img = img.resize((width, height), resample=Image.Resampling.NEAREST)
            _image_bg_cache[cache_key] = img
            if len(_image_bg_cache) > _IMAGE_BG_LRU_MAX:
                _image_bg_cache.popitem(last=False)
            return img
        except FileNotFoundError:
            log.warning(
                "auto_render: background slide %s missing for auto slide %s",
                slide.background_image_slide_id,
                slide.id,
            )
        except Exception:
            log.exception(
                "auto_render: failed to load background for auto slide %s",
                slide.id,
            )
    # DELETE-PIL phase 3b (qarl-direct 2026-05-13): the Python pattern
    # rasterizer is gone -- bg-system.js + Canvas2D bake the operator's
    # chosen pattern into the stored PNG. If a slide reaches this
    # backend code path with background_pattern set, the stored PNG
    # already has the pattern composited in; the solid-color fallback
    # here is just a safety net for legacy/edge cases.
    return Image.new("RGB", (width, height), slide.background_color)
