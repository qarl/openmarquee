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
import math
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

import numpy as np

if TYPE_CHECKING:
    from openmarquee.content import BackgroundPattern
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

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

    Reads text_layers[0] only — single-layer behavior is preserved
    for older callers (compose_auto_frame and the test surface that
    imports this name). Multi-layer auto-mode rendering goes through
    `render_auto_text_for_layer` directly via the unified composer.
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


def compose_auto_frame(
    slide: TextSlide,
    width: int,
    height: int,
    now: datetime,
    read_asset: Callable[[UUID], bytes] | None = None,
) -> Image.Image:
    """Render a fresh RGB frame for an auto-mode slide at `now`.

    NOTE (2026-05-02): the playback loop no longer calls this — the
    unified per-tick composer in `motion.compose_motion_frame` handles
    auto AND motion in one pass (with proper multi-layer support and
    auto+motion combination). This function is kept as a single-layer
    compat shim for `test_auto_render.py` and any ad-hoc callers; it
    is NOT the production render path. New work should target
    `compose_motion_frame` instead.

    Background resolution order:
      1. If slide.background_image_slide_id is set AND read_asset is
         provided, load that slide's PNG and resize-fit to (width, height).
      2. Else fill with slide.background_color.

    Text drawing:
      - render_auto_text → current string
      - Slide's font_family (if available system-side) + font_size_pct
        of box width (or font_size_px as fallback)
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
        # Spec 5.10a v3.1.2: pct is of BOX WIDTH, not slide height.
        # Resizing the box visibly resizes the text -- operators
        # expected that math (B3, 2026-05-05). Mirrors the
        # _draw_text_into convention in seed.py and the editor canvas
        # preview in ui/src/editor.js.
        box_w_frac = float(getattr(getattr(layer, "box", None), "w", 1.0))
        px_box_w = max(1, int(box_w_frac * width))
        size_px = max(4, int(round(px_box_w * layer.font_size_pct / 100)))
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
    """Build the background layer: image slide ref, gradient, or solid fill."""
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
    if slide.background_pattern is not None:
        try:
            return render_pattern(
                slide.background_pattern, width, height,
            )
        except Exception:
            log.exception(
                "auto_render: pattern render failed for slide %s; "
                "falling back to solid background_color",
                slide.id,
            )
    return Image.new("RGB", (width, height), slide.background_color)


# --- procedural pattern renderers ---
#
# 11 patterns from the bg-system.js handoff (qarl 2026-05-03). Each is
# a numpy-vectorized translation of the corresponding CSS `build(a, b,
# density)` function. Visual parity with the editor canvas matters
# (operator picks pattern in editor, expects device to render the
# same thing); pixel-perfect parity does not (PIL antialiasing differs
# slightly from CSS).
#
# All pattern renderers run ONCE at slide entry, get baked into the
# bg cache, ride along with the GPU compositor's primary plane for
# free. Per-frame cost is zero. Numpy keeps the one-shot cost
# negligible at 1080p (each pattern under ~10 ms on a Pi Zero 2 W).
#
# `density` is a normalized 0..1 knob whose meaning is per-pattern;
# see BackgroundPattern's docstring for the per-pattern range.


def _lerp(a: float, b: float, t: float) -> float:
    """CSS-side bg-system.js lerp: clamp t to [0, 1], map to [a, b]."""
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


def render_pattern(
    pattern: BackgroundPattern, width: int, height: int,
) -> Image.Image:
    """Dispatch to the matching pattern renderer. Falls back to a
    solid color_a fill on an unrecognized pattern name (forward-compat
    with future patterns added to the schema before all clients
    update)."""
    p = pattern.pattern
    a = pattern.color_a
    b = pattern.color_b
    d = pattern.density
    if p == "solid":
        return Image.new("RGB", (width, height), a)
    if p == "gradient":
        return _render_pattern_gradient(a, b, d, width, height)
    if p == "dots":
        return _render_pattern_dots(a, b, d, width, height)
    if p == "halftone":
        return _render_pattern_halftone(a, b, d, width, height)
    if p == "stripes":
        return _render_pattern_stripes(a, b, d, width, height)
    if p == "scanlines":
        return _render_pattern_scanlines(a, b, d, width, height)
    if p == "checker":
        return _render_pattern_checker(a, b, d, width, height)
    if p == "rings":
        return _render_pattern_rings(a, b, d, width, height)
    if p == "rays":
        return _render_pattern_rays(a, b, d, width, height)
    if p == "confetti":
        return _render_pattern_confetti(a, b, d, width, height)
    if p == "bricks":
        return _render_pattern_bricks(a, b, d, width, height)
    if p == "grid":
        return _render_pattern_grid(a, b, d, width, height)
    log.warning("auto_render: unknown pattern %r; falling back to color_a", p)
    return Image.new("RGB", (width, height), a)


def _render_pattern_gradient(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Two-stop linear gradient. Density 0..1 maps to angle 0..270deg
    per the bg-system.js convention (`density 0 → 0deg, 0.5 → ~135deg,
    1 → 270deg`). 0deg = top→bottom; 90deg = left→right; 180deg =
    bottom→top; 270deg = right→left."""
    angle_deg = round(_lerp(0.0, 270.0, density))
    rad = math.radians(angle_deg)
    dx = math.sin(rad)
    dy = math.cos(rad)
    xs = np.arange(width, dtype=np.float32).reshape(1, width)
    ys = np.arange(height, dtype=np.float32).reshape(height, 1)
    proj = xs * dx + ys * dy
    proj_min = min(0.0, dx * (width - 1)) + min(0.0, dy * (height - 1))
    proj_max = max(0.0, dx * (width - 1)) + max(0.0, dy * (height - 1))
    span = proj_max - proj_min
    if span < 1e-6:
        return Image.new("RGB", (width, height), color_a)
    t = np.clip((proj - proj_min) / span, 0.0, 1.0)
    a_rgb = _hex_to_rgb(color_a)
    b_rgb = _hex_to_rgb(color_b)
    r = (a_rgb[0] + t * (b_rgb[0] - a_rgb[0])).astype(np.uint8)
    g = (a_rgb[1] + t * (b_rgb[1] - a_rgb[1])).astype(np.uint8)
    b_arr = (a_rgb[2] + t * (b_rgb[2] - a_rgb[2])).astype(np.uint8)
    return Image.fromarray(np.stack([r, g, b_arr], axis=-1), mode="RGB")


def _dot_grid_mask(
    width: int, height: int, tile: int, radius: int,
    offset_x: int = 0, offset_y: int = 0,
) -> np.ndarray:
    """Boolean mask: True wherever a pixel falls inside a dot of
    radius `radius` centered in each `tile`-sized cell. Used by the
    dot, halftone, and confetti patterns. `offset_x/y` shifts the
    grid origin (halftone uses this for the second offset layer)."""
    if tile <= 0:
        return np.zeros((height, width), dtype=bool)
    xs = (np.arange(width) - offset_x) % tile - tile / 2
    ys = (np.arange(height) - offset_y) % tile - tile / 2
    dx = xs.reshape(1, width)
    dy = ys.reshape(height, 1)
    return (dx * dx + dy * dy) <= (radius * radius)


def _solid_fill(width: int, height: int, color_hex: str) -> np.ndarray:
    """uint8 (H, W, 3) array filled with `color_hex`. Base layer for
    most patterns."""
    rgb = _hex_to_rgb(color_hex)
    arr = np.empty((height, width, 3), dtype=np.uint8)
    arr[..., 0] = rgb[0]
    arr[..., 1] = rgb[1]
    arr[..., 2] = rgb[2]
    return arr


def _apply_mask(
    base: np.ndarray, mask: np.ndarray, color_hex: str,
) -> np.ndarray:
    """Where `mask` is True, set base pixels to `color_hex`. In-place
    on `base`."""
    rgb = _hex_to_rgb(color_hex)
    base[mask, 0] = rgb[0]
    base[mask, 1] = rgb[1]
    base[mask, 2] = rgb[2]
    return base


def _render_pattern_dots(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Evenly-spaced filled dots of color_b on a color_a background.
    Tile size shrinks with density (lerp(48, 4))."""
    tile = round(_lerp(48, 4, density))  # B12: widened from (28, 8)
    radius = max(2, round(tile * 0.22))
    base = _solid_fill(width, height, color_a)
    mask = _dot_grid_mask(width, height, tile, radius)
    _apply_mask(base, mask, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_halftone(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Two offset dot grids — printer-style halftone. Larger tile +
    bigger dots than `dots`; second layer offset by half a tile."""
    tile = round(_lerp(60, 6, density))  # B12: widened from (36, 14)
    radius = round(tile * 0.34)
    half = tile // 2
    base = _solid_fill(width, height, color_a)
    mask = _dot_grid_mask(width, height, tile, radius)
    mask |= _dot_grid_mask(width, height, tile, radius, half, half)
    _apply_mask(base, mask, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_stripes(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Diagonal 45° alternating bands. Half each tile is color_a, the
    other half is color_b. Tile size shrinks with density (lerp(40,
    10)).

    Tile is measured PERPENDICULAR to the stripe direction (matches
    CSS repeating-linear-gradient + the canvas painter in bg-
    system.js). The (x+y) projection is along a 45° axis whose
    natural unit is √2 pixels per "1" of (x+y); dividing by √2 first
    makes the modulus directly perpendicular-pixel-distance, so a
    `tile`-sized cycle in our math = `tile` perpendicular pixels on
    screen. Without the / √2 the bands were √2× too thin vs the
    editor canvas (caught in pre-commit subagent review)."""
    tile = round(_lerp(80, 4, density))  # B12: widened from (40, 10)
    half = tile / 2
    sqrt2 = math.sqrt(2)
    xs = np.arange(width, dtype=np.float32).reshape(1, width)
    ys = np.arange(height, dtype=np.float32).reshape(height, 1)
    proj = ((xs + ys) / sqrt2) % tile
    mask_b = proj >= half
    base = _solid_fill(width, height, color_a)
    _apply_mask(base, mask_b, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_scanlines(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Horizontal CRT-style 1-pixel lines of color_b on color_a, with
    `tile` pixels between each line. Tile shrinks with density
    (lerp(16, 2))."""
    tile = max(2, round(_lerp(16, 2, density)))  # B12: widened from (8, 3)
    base = _solid_fill(width, height, color_a)
    rgb = _hex_to_rgb(color_b)
    base[::tile, :, 0] = rgb[0]
    base[::tile, :, 1] = rgb[1]
    base[::tile, :, 2] = rgb[2]
    return Image.fromarray(base, mode="RGB")


def _render_pattern_checker(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Standard checker grid. Tile shrinks with density (lerp(32,
    8))."""
    tile = round(_lerp(60, 4, density))  # B12: widened from (32, 8)
    if tile <= 0:
        return Image.new("RGB", (width, height), color_a)
    xs = (np.arange(width) // tile).reshape(1, width)
    ys = (np.arange(height) // tile).reshape(height, 1)
    mask_b = ((xs + ys) % 2) == 1
    base = _solid_fill(width, height, color_a)
    _apply_mask(base, mask_b, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_grid(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Graph-paper grid -- color_a lines on color_b paper. Cell size
    shrinks with density (lerp(120, 4)). 1px lines (B17, 2026-05-05)."""
    tile = max(4, round(_lerp(120, 4, density)))  # B12: widened from (60, 12)
    base = _solid_fill(width, height, color_b)
    rgb_a = _hex_to_rgb(color_a)
    # Horizontal lines: 1px-tall stripe at every `tile`-th row.
    mask_h = (np.arange(height) % tile) == 0
    base[mask_h, :, 0] = rgb_a[0]
    base[mask_h, :, 1] = rgb_a[1]
    base[mask_h, :, 2] = rgb_a[2]
    # Vertical lines: 1px-wide stripe at every `tile`-th column.
    mask_v = (np.arange(width) % tile) == 0
    base[:, mask_v, 0] = rgb_a[0]
    base[:, mask_v, 1] = rgb_a[1]
    base[:, mask_v, 2] = rgb_a[2]
    return Image.fromarray(base, mode="RGB")


def _render_pattern_rings(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Concentric rings around the slide center. Ring period shrinks
    with density (lerp(120, 6)). Each period: color_a band of
    `half - 2` pixels, then a 2-pixel color_b ring."""
    tile = max(4, round(_lerp(120, 6, density)))  # B12: widened from (60, 18)
    half = tile // 2
    cx = width / 2
    cy = height / 2
    xs = (np.arange(width, dtype=np.float32) - cx).reshape(1, width)
    ys = (np.arange(height, dtype=np.float32) - cy).reshape(height, 1)
    dist = np.sqrt(xs * xs + ys * ys)
    period = dist % tile
    mask_b = period >= (half - 2)
    base = _solid_fill(width, height, color_a)
    _apply_mask(base, mask_b, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_rays(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Conic gradient that flips between color_a and color_b in equal
    angular slices. Slice count grows with density (4..48) and
    is always even -- an odd count joins two same-colored slices at
    the wrap seam, doubling the top ray (B15, 2026-05-05)."""
    slices = max(2, 2 * round(_lerp(2, 24, density)))  # B12: widened from (4, 16)
    cx = width / 2
    cy = height / 2
    xs = (np.arange(width, dtype=np.float32) - cx).reshape(1, width)
    ys = (np.arange(height, dtype=np.float32) - cy).reshape(height, 1)
    angle = np.arctan2(ys, xs)  # -pi..pi
    # Map to [0, 1) and assign slice index.
    norm = (angle / (2 * math.pi) + 1.0) % 1.0
    slice_idx = np.floor(norm * slices).astype(np.int32)
    mask_b = (slice_idx % 2) == 1
    base = _solid_fill(width, height, color_a)
    _apply_mask(base, mask_b, color_b)
    return Image.fromarray(base, mode="RGB")


def _render_pattern_confetti(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Deterministic full-canvas scatter of color_b dots on color_a.
    Replaces the prior 4-layer offset dot grid that had visible repeat
    seams at 1080p (B5, 2026-05-05). Particle count grows with density
    via lerp(80, 2000). Fixed PRNG seed so a given (density, width,
    height) renders the same scatter every time -- live edits feel
    stable, not random. NB: editor canvas and device backend use
    different RNG families with the same seed -- both deterministic
    per-surface but the scatters will not pixel-match. Visual
    character is the same."""
    count = max(40, round(_lerp(80, 2000, density)))
    rng = np.random.default_rng(0xC0FFE71)
    img = Image.new("RGB", (width, height), color_a)
    draw = ImageDraw.Draw(img)
    xs = rng.integers(0, width, count)
    ys = rng.integers(0, height, count)
    radii = rng.integers(2, 6, count)
    for x, y, r in zip(xs, ys, radii):
        draw.ellipse(
            [(int(x - r), int(y - r)), (int(x + r), int(y + r))],
            fill=color_b,
        )
    return img


def _render_pattern_bricks(
    color_a: str, color_b: str, density: float, width: int, height: int,
) -> Image.Image:
    """Mortar-line brick layout — mimics LED-matrix tile seams.
    Brick width shrinks with density (lerp(140, 16)); brick height is
    half the width. Mortar = 2px lines of color_b -- 1px read as a
    uniform grid at thumbnail scale and lost the offset character of
    the bricks (B16, 2026-05-05)."""
    bw = max(8, round(_lerp(140, 16, density)))  # B12: widened from (80, 32)
    bh = max(4, bw // 2)
    half = bw // 2
    base = _solid_fill(width, height, color_a)
    rgb = _hex_to_rgb(color_b)
    # Horizontal mortar: 2px-tall strip at rows where y % bh < 2.
    mask_h = (np.arange(height) % bh) < 2
    base[mask_h, :, 0] = rgb[0]
    base[mask_h, :, 1] = rgb[1]
    base[mask_h, :, 2] = rgb[2]
    # Vertical mortar: alternates between offset 0 (rows 0..bh,
    # 2*bh..3*bh, ...) and offset half (rows bh..2*bh, 3*bh..4*bh,
    # ...). Compute which "brick course" each y is in: y // bh % 2.
    course = (np.arange(height) // bh) % 2
    # For course-0 rows: vertical 2px-wide stripe where x % bw < 2.
    # For course-1 rows: vertical 2px-wide stripe where (x - half) % bw < 2.
    course_0_rows = course == 0
    course_1_rows = course == 1
    x_indices = np.arange(width)
    vert_0 = (x_indices % bw) < 2
    vert_1 = ((x_indices - half) % bw) < 2
    if course_0_rows.any():
        rows_0 = np.where(course_0_rows)[0]
        for col in np.where(vert_0)[0]:
            base[rows_0, col, 0] = rgb[0]
            base[rows_0, col, 1] = rgb[1]
            base[rows_0, col, 2] = rgb[2]
    if course_1_rows.any():
        rows_1 = np.where(course_1_rows)[0]
        for col in np.where(vert_1)[0]:
            base[rows_1, col, 0] = rgb[0]
            base[rows_1, col, 1] = rgb[1]
            base[rows_1, col, 2] = rgb[2]
    return Image.fromarray(base, mode="RGB")


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse `#RRGGBB` into (R, G, B) uint8 tuple. The TextSlide /
    BackgroundPattern validators already enforce the format, so we
    don't re-validate here."""
    s = hex_color.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# Font loading + measurement moved to text_raster.py so the
# server-side renderers + seed pipeline share a cached truetype
# loader. Underscore aliases preserved here because seed.py (and
# possibly external callers) import them by their original names.
from openmarquee.text_raster import (
    BUNDLED_FONT_FILES as _BUNDLED_FONT_FILES,
    bundled_fonts_dir as _bundled_fonts_dir,
    load_font as _load_font,
    measure_centered as _measure_centered,
)
