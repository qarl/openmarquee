#!/usr/bin/env python3
"""render-app-icons.py — regenerate the app icons as the square portrait
LED-dot tile (qarl-approved 2026-07-08, rev 3).

The icon is the openMarquee dot-matrix mark rendered TILE-ALONE as the real
sign's shape: a SHARP-cornered (90°) PORTRAIT panel (~768x1360 ≈ 9:16),
centred in the square icon canvas with side margins, amber (#ffb43c) LED
dots on a checkerboard over a near-black tile with an amber edge.

Why a per-size PIL step (not the old rsvg-from-one-SVG path in
render-pwa-icons.sh): the column count adapts to size so the dots stay
DISTINCT rather than turning to amber mush at favicon sizes — 4 columns at
180/192/512, 2 columns at 16/32. A single scaled SVG can't do that.

Geometry mirrors ``_draw_dot_matrix_mark`` in
``images/openmarquee/stage-openmarquee/01-plymouth-theme/files/generate_splash.py``
(the canonical brand-mark source); the colours are the same brand tokens.
Keep the two in sync if the mark ever changes. The horizontal boot-splash
lockup (mark + OPEN/Marquee) is a SEPARATE, approved asset — not touched here.

Requires Pillow. Outputs are committed under ui/icons/ so a fresh clone
doesn't need Pillow to serve them. Run from the repo's ui/ dir (or root):
    python3 ui/scripts/render-app-icons.py
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

# Brand tokens — canonical values from generate_splash.py (--om-accent /
# --om-led-bg / --om-accent-glow). Keep in sync with that file.
ACCENT = (0xFF, 0xB4, 0x3C)  # LED-dot amber
LED_BG = (0x05, 0x06, 0x08)  # near-black dot-matrix tile background
# --om-accent-glow ≈ rgba(255,180,60,.35) composited over LED_BG.
ACCENT_GLOW = tuple(round(c * 0.35 + b * 0.65) for c, b in zip((0xFF, 0xB4, 0x3C), LED_BG))

# The real displayed panel is ~768w × 1360h at rotation=90.
PANEL_AR = 768 / 1360  # ≈ 0.565 (portrait, taller than wide)

_ICONS_DIR = os.path.join(os.path.dirname(__file__), "..", "icons")


def _tile_box(size: int, maskable: bool) -> tuple[float, float, float, float, float]:
    """Portrait tile geometry inside a square `size` canvas. Returns
    (left, top, width, height, edge_width). Maskable insets more so the
    tile survives Android's inscribed mask; the dark LED background bleeds
    to the canvas edges regardless."""
    vmargin = 0.12 if maskable else 0.07
    tile_h = size * (1 - 2 * vmargin)
    tile_w = tile_h * PANEL_AR
    left = (size - tile_w) / 2
    top = size * vmargin
    edge_w = max(2, round(tile_w * 0.10))
    return left, top, tile_w, tile_h, edge_w


def _draw_portrait_tile(draw: ImageDraw.ImageDraw, size: int, cols: int, maskable: bool) -> None:
    left, top, tile_w, tile_h, edge_w = _tile_box(size, maskable)
    box = (round(left), round(top), round(left + tile_w), round(top + tile_h))
    # SHARP rectangle (90° corners) — no rounding, per qarl rev 3.
    draw.rectangle(box, fill=LED_BG)
    draw.rectangle(box, outline=ACCENT, width=edge_w)

    # Interior dot field, inset 2/16 of the tile width (matches the mark).
    inset = tile_w * (2.0 / 16.0)
    in_left, in_top = left + inset, top + inset
    in_w, in_h = tile_w - 2 * inset, tile_h - 2 * inset
    pitch = in_w / cols
    rows = max(1, round(in_h / pitch))
    half = pitch / 2.0
    dot_r = half * 0.44
    glow_r = dot_r * 1.9
    for row in range(rows):
        for col in range(cols):
            if (col + row) % 2 != 0:  # checkerboard
                continue
            cx = in_left + (col + 0.5) * pitch
            cy = in_top + (row + 0.5) * pitch
            draw.ellipse([cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r], fill=ACCENT_GLOW)
            draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=ACCENT)


def render_png(size: int, cols: int, *, maskable: bool = False) -> Image.Image:
    img = Image.new("RGBA", (size, size), (*LED_BG, 255))
    _draw_portrait_tile(ImageDraw.Draw(img), size, cols, maskable)
    return img


def render_svg(cols: int = 4) -> str:
    """Vector twin of the 512 tile for the SVG-favicon slot. Same geometry
    so a browser that prefers the SVG gets the identical mark."""
    size = 512
    left, top, tile_w, tile_h, edge_w = _tile_box(size, maskable=False)
    hexc = lambda c: "#%02x%02x%02x" % c  # noqa: E731
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{size}" height="{size}">',
        f'<rect x="0" y="0" width="{size}" height="{size}" fill="{hexc(LED_BG)}"/>',
        # Sharp portrait tile: fill + amber stroke (stroke is centred on the
        # path, so inset by half the edge width to keep it inside the box).
        f'<rect x="{left + edge_w / 2:.1f}" y="{top + edge_w / 2:.1f}" '
        f'width="{tile_w - edge_w:.1f}" height="{tile_h - edge_w:.1f}" '
        f'fill="{hexc(LED_BG)}" stroke="{hexc(ACCENT)}" stroke-width="{edge_w}"/>',
    ]
    inset = tile_w * (2.0 / 16.0)
    in_left, in_top = left + inset, top + inset
    in_w, in_h = tile_w - 2 * inset, tile_h - 2 * inset
    pitch = in_w / cols
    rows = max(1, round(in_h / pitch))
    dot_r = (pitch / 2.0) * 0.44
    for row in range(rows):
        for col in range(cols):
            if (col + row) % 2 != 0:
                continue
            cx = in_left + (col + 0.5) * pitch
            cy = in_top + (row + 0.5) * pitch
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{dot_r:.1f}" fill="{hexc(ACCENT)}"/>'
            )
    parts.append("</svg>\n")
    return "\n".join(parts)


def main() -> None:
    icons = os.path.abspath(_ICONS_DIR)
    os.makedirs(icons, exist_ok=True)
    # (filename, size, cols, maskable) — 4-col at the big sizes; 2-col at
    # favicon sizes so the dots stay distinct.
    targets = [
        ("apple-touch-icon.png", 180, 4, False),
        ("icon-192.png", 192, 4, False),
        ("icon-512-maskable.png", 512, 4, True),
        ("favicon.png", 32, 2, False),
    ]
    for name, size, cols, maskable in targets:
        render_png(size, cols, maskable=maskable).save(os.path.join(icons, name))
        print(f"  -> ui/icons/{name} ({size}x{size}, {cols}-col{', maskable' if maskable else ''})")
    with open(os.path.join(icons, "monogram.svg"), "w", encoding="utf-8") as f:
        f.write(render_svg(cols=4))
    print("  -> ui/icons/monogram.svg (square portrait, 4-col)")
    print("render-app-icons: done")


if __name__ == "__main__":
    main()
