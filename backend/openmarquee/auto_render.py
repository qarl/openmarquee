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
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from openmarquee.content import TextSlide

log = logging.getLogger(__name__)


# Default auto_format per mode — used when the slide has auto_format=None.
_DEFAULT_FORMAT = {
    "time": "time_hm",
    "date": "date_iso",
    "day": "day_long",
}


def render_auto_text(slide: TextSlide, now: datetime) -> str:
    """Return the visible text for an auto-mode slide at the given `now`.

    `now` is expected to already be in the target timezone — this fn
    doesn't convert. Callers should pass `datetime.now(ZoneInfo(tz))`.

    Non-auto slides get their `text` field back unchanged so the same
    entry point works for the whole rendering path.

    Schema v3 (qarl 2026-05-01): per-text fields live on text_layers[0].
    Multi-layer auto-mode composition lands in phase 2 of the layered
    rollout — for now this reads layer[0] and matches single-layer
    behavior. (compose_auto_frame likewise.)
    """
    layer = slide.text_layers[0]
    if not layer.auto_mode:
        return layer.text

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


def compose_auto_frame(
    slide: TextSlide,
    width: int,
    height: int,
    now: datetime,
    read_asset: Callable[[UUID], bytes] | None = None,
) -> Image.Image:
    """Render a fresh RGB frame for an auto-mode slide at `now`.

    Background resolution order:
      1. If slide.background_image_slide_id is set AND read_asset is
         provided, load that slide's PNG and resize-fit to (width, height).
      2. Else fill with slide.background_color.

    Text drawing:
      - render_auto_text → current string
      - Slide's font_family (if available system-side) + font_size_px
      - text_color; centered within the frame
    """
    # Base canvas.
    base = _load_background(slide, width, height, read_asset)

    # Current string.
    value = render_auto_text(slide, now)

    # Font: try slide.font_family first (as a TTF file path / PIL-loadable
    # name), fall back to the platform default font. We bias toward the
    # fallback so tests + first-boot devices never crash on a missing
    # font — the rendered text might be blockier than the editor preview
    # but that's a visible-quality issue, not a correctness one.
    # Prefer the relative metric so the auto-render keeps proportional
    # sizing across resolution changes. Fall back to absolute px on old
    # slides that haven't been re-saved with the new field.
    layer = slide.text_layers[0]
    if layer.font_size_pct is not None:
        size_px = max(4, int(round(height * layer.font_size_pct / 100)))
    else:
        size_px = layer.font_size_px
    font = _load_font(layer.font_family, size_px, height)

    draw = ImageDraw.Draw(base)
    text_w, text_h, text_x, text_y = _measure_centered(draw, value, font, width, height)
    # Drop-shadow-ish: a 1-pixel black outline so the text reads on
    # mid-tone backgrounds without relying on a matched shadow color.
    color = layer.text_color
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        draw.text((text_x + dx, text_y + dy), value, fill="#000000", font=font)
    draw.text((text_x, text_y), value, fill=color, font=font)

    # Silence unused return so future callers can read the bbox if needed.
    _ = (text_w, text_h)
    return base


# --- internals ---


def _load_background(
    slide: TextSlide,
    width: int,
    height: int,
    read_asset: Callable[[UUID], bytes] | None,
) -> Image.Image:
    """Build the background layer: image slide reference or solid fill."""
    if slide.background_image_slide_id is not None and read_asset is not None:
        try:
            png = read_asset(slide.background_image_slide_id)
            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.size != (width, height):
                img = img.resize((width, height), resample=Image.Resampling.NEAREST)
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
    return Image.new("RGB", (width, height), slide.background_color)


# Map the UI font-family strings to the bundled TTF filenames under
# ui/fonts/. The browser editor uses these names via @font-face; server-
# side auto-render (clock/date/day slides) has to resolve them to actual
# file paths so Pillow picks the same face the operator picked in the UI.
_BUNDLED_FONT_FILES = {
    "Inter": "inter.ttf",
    "Oswald": "oswald.ttf",
    "Bebas Neue": "bebas-neue.ttf",
    "Roboto Slab": "roboto-slab.ttf",
    "Caveat Brush": "caveat-brush.ttf",
    "Permanent Marker": "permanent-marker.ttf",
    "Cinzel": "cinzel.ttf",
    "UnifrakturCook": "unifrakturcook.ttf",
    "Rye": "rye.ttf",
    "Pacifico": "pacifico.ttf",
    "Sedgwick Ave Display": "sedgwick-ave-display.ttf",
    "Bowlby One SC": "bowlby-one-sc.ttf",
    "Anton": "anton.ttf",
    "Archivo Black": "archivo-black.ttf",
    "Alfa Slab One": "alfa-slab-one.ttf",
    "Playfair Display": "playfair-display.ttf",
    "DM Serif Display": "dm-serif-display.ttf",
    "VT323": "vt323.ttf",
    "JetBrains Mono": "jetbrains-mono.ttf",
    "Space Mono": "space-mono.ttf",
    "Caveat": "caveat.ttf",
    "Reenie Beanie": "reenie-beanie.ttf",
    "Shadows Into Light": "shadows-into-light.ttf",
}


def _bundled_fonts_dir() -> Path:
    """`ui/fonts/` alongside `backend/` in the repo layout."""
    return Path(__file__).resolve().parent.parent.parent / "ui" / "fonts"


def _load_font(family: str | None, size_px: int | None, canvas_height: int) -> ImageFont.ImageFont:
    """Pick a best-effort font. Size defaults to ~40% of canvas height
    so the auto value reads across a HUB75 panel without the operator
    having to pick a size explicitly for every auto slide."""
    size = size_px if size_px else max(8, int(canvas_height * 0.4))
    if family:
        # 1. Bundled @font-face family — load the matching TTF by path.
        bundled = _BUNDLED_FONT_FILES.get(family)
        if bundled:
            path = _bundled_fonts_dir() / bundled
            try:
                return ImageFont.truetype(str(path), size=size)
            except OSError:
                pass
        # 2. Raw path or system-registered name — let Pillow try.
        try:
            return ImageFont.truetype(family, size=size)
        except OSError:
            pass
    # Last resort: PIL's bundled bitmap font ignores size but always loads.
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _measure_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """Return (text_w, text_h, top_left_x, top_left_y) for centered text."""
    # Pillow 10+: textbbox is the canonical sizing API.
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2 - bbox[0]
    y = (height - text_h) // 2 - bbox[1]
    return text_w, text_h, x, y
