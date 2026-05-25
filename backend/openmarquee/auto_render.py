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
# Kept as a separate table from _FORMATTERS so a future UI surface that
# needs to know "what's the default format for mode X" has a single
# source of truth (rather than scraping it back out of the dispatch
# fallback semantics).
_DEFAULT_FORMAT = {
    "time": "time_hm",
    "date": "date_iso",
    "day": "day_long",
}


def _format_date_medium(n: datetime) -> str:
    """e.g. "Apr 21" — strip the leading zero off %d without relying on
    the %-d glibc extension (works on Linux + Mac but not portable to
    Windows; tests run on Mac)."""
    return n.strftime("%b ") + str(n.day)


def _format_date_long(n: datetime) -> str:
    """e.g. "April 21, 2026" -- the mode's default."""
    return n.strftime("%B ") + f"{n.day}, {n.year}"


# Dispatch table: (auto_mode, auto_format) -> formatter callable.
# (mode, None) entries are the mode's default-format renderers; the
# `_DEFAULT_FORMAT.get(mode)` resolution upstream still runs first so
# the canonical default-format name is preserved for any caller that
# inspects the resolved fmt, but lookup then falls through to the
# None entry. Adding a new auto_format becomes a 1-line table edit.
_FORMATTERS: dict[tuple[str, str | None], Callable[[datetime], str]] = {
    ("time", "time_hms"): lambda n: n.strftime("%H:%M:%S"),
    ("time", None): lambda n: n.strftime("%H:%M"),
    ("date", "date_iso"): lambda n: n.strftime("%Y-%m-%d"),
    ("date", "date_medium"): _format_date_medium,
    ("date", None): _format_date_long,
    ("day", "day_short"): lambda n: n.strftime("%a"),
    ("day", None): lambda n: n.strftime("%A"),
}


def render_auto_text_for_layer(layer, now: datetime) -> str:
    """Per-layer variant of `render_auto_text`. Returns the visible
    string for one TextLayer at `now` — handles auto-mode formatting
    if the layer has it set, otherwise returns `layer.text`.

    Returns the formatted string for one TextLayer at `now`. Post-
    DELETE-PIL the Rust sidecar reads each layer's auto_mode +
    auto_format via begin_slide and renders the formatted string
    itself; Python's role here is the auto-format spec, not the
    composite.

    `now` is expected to already be in the target timezone — this fn
    doesn't convert. Callers should pass `datetime.now(ZoneInfo(tz))`.
    """
    if not getattr(layer, "auto_mode", None):
        return getattr(layer, "text", "")

    mode = layer.auto_mode
    fmt = layer.auto_format or _DEFAULT_FORMAT.get(mode)
    # Try explicit (mode, fmt); fall back to the mode's default-format
    # entry at (mode, None). If both miss, mode is unknown -- fall
    # through to typed text so playback doesn't crash if a future
    # mode ships ahead of this helper.
    formatter = _FORMATTERS.get((mode, fmt)) or _FORMATTERS.get((mode, None))
    if formatter is None:
        return layer.text
    return formatter(now)


def render_auto_text(slide: TextSlide, now: datetime) -> str:
    """Slide-level compat wrapper for `render_auto_text_for_layer`.

    Reads text_layers[0] only — single-layer behavior preserved for
    test_auto_render.py + any ad-hoc string-only callers. Multi-
    layer auto-mode rendering is the Rust sidecar's job; this helper
    is kept only for legacy tests + diagnostic callers.
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
# Used by the Python `load_background` helper (whose callers are now
# legacy / diagnostic only post-DELETE-PIL -- the Rust sidecar reads
# image backgrounds itself via its own asset loader). Kept for the
# auto_render tests + any ad-hoc tooling that still pulls slide
# backgrounds through Python.
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
