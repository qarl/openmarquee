"""Font loading + text measurement helpers for the server-side
renderers.

Extracted from auto_render.py so:
  * the same font-resolution rules are reusable from motion.py +
    seed.py without circular imports (seed.py used to reach into
    auto_render at runtime — moved here so the dependency is
    one-way: auto_render and seed both depend on text_raster);
  * ImageFont.truetype() calls -- the dominant cost in
    auto-render hot paths -- can be amortized through an LRU
    cache keyed on (resolved path, size_px).

The cache assumes ImageFont instances are safe to share across
draw calls. They are: Pillow's drawing API treats them as
read-only metadata + glyph atlas. We size the cache at 128 entries
which covers (23 bundled fonts) * (handful of common sizes) with
slack to spare.
"""

from __future__ import annotations

import functools
from pathlib import Path

from PIL import ImageDraw, ImageFont

# Map the UI font-family strings to the bundled TTF filenames under
# ui/fonts/. The browser editor uses these names via @font-face;
# server-side rendering (auto-render, seed text PNGs, motion
# layers) has to resolve them to actual file paths so Pillow picks
# the same face the operator picked in the UI.
BUNDLED_FONT_FILES: dict[str, str] = {
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


def bundled_fonts_dir() -> Path:
    """`ui/fonts/` alongside `backend/` in the repo layout."""
    return Path(__file__).resolve().parent.parent.parent / "ui" / "fonts"


@functools.lru_cache(maxsize=128)
def cached_truetype(path_or_name: str, size: int) -> ImageFont.ImageFont:
    """LRU-cached ImageFont.truetype(). Raises OSError on miss --
    callers must handle it. The unwrapped truetype is the dominant
    cost on cold auto-render slides; caching it means after the
    first slide of each (family, size) pair the work is a dict
    lookup."""
    return ImageFont.truetype(path_or_name, size=size)


def load_font(
    family: str | None, size_px: int | None, canvas_height: int,
) -> ImageFont.ImageFont:
    """Pick a best-effort font. Size defaults to ~40% of canvas
    height so the auto value reads across a HUB75 panel without
    the operator having to pick a size explicitly for every auto
    slide."""
    size = size_px if size_px else max(8, int(canvas_height * 0.4))
    if family:
        # 1. Bundled @font-face family — load the matching TTF by path.
        bundled = BUNDLED_FONT_FILES.get(family)
        if bundled:
            path = bundled_fonts_dir() / bundled
            try:
                return cached_truetype(str(path), size)
            except OSError:
                pass
        # 2. Raw path or system-registered name — let Pillow try.
        try:
            return cached_truetype(family, size)
        except OSError:
            pass
    # Last resort: PIL's bundled bitmap font ignores size but
    # always loads.
    try:
        return cached_truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def measure_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    """Return (text_w, text_h, top_left_x, top_left_y) for
    centered text. Pillow 10+: textbbox is the canonical sizing
    API."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (width - text_w) // 2 - bbox[0]
    y = (height - text_h) // 2 - bbox[1]
    return text_w, text_h, x, y


def clear_font_cache() -> None:
    """Drop the LRU cache. Useful for tests that mock filesystem
    state or want to count cold-load calls."""
    cached_truetype.cache_clear()


def font_cache_info() -> functools._CacheInfo:
    """Expose the cache stats for tests + perf instrumentation."""
    return cached_truetype.cache_info()
