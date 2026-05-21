#!/usr/bin/env python3
"""Generate the openMarquee Plymouth boot-splash assets.

Produces two files next to this script under ``openmarquee/``:

  * ``splash.png``  -- the full-screen brand lockup (1360x768, RGB).
  * ``spinner.png`` -- a small amber activity arc (64x64, RGBA) that the
                       Plymouth script rotates each refresh frame.

This script is the *reproducible source* for the splash artwork. It is
deliberately committed alongside the rendered PNGs so a designer can
re-run it (or tweak it) without reverse-engineering the bitmap. It is
NOT shipped into the Pi image -- only the PNGs it emits are copied to
the rootfs by ``../01-run.sh``.

Run it from anywhere with Pillow available (the repo backend venv at
/opt/openmarquee/venv has it, and so does a typical dev host)::

    python3 generate_splash.py

All brand values below are the verified CSS custom properties from
``ui/login.html`` -- keep them in sync if the brand palette moves.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --- Brand palette (verified against ui/login.html :root) -----------------
BG          = (0x0e, 0x0e, 0x10)        # --bg            solid splash background
ACCENT      = (0xff, 0xb8, 0x4d)        # --accent        amber
TEXT_LIGHT  = (0xf2, 0xf2, 0xf4)        # --text          near-white ("open")
MARK_INK    = (0x1a, 0x0f, 0x00)        # near-black ink used on the amber mark

# --- Output geometry ------------------------------------------------------
SPLASH_W, SPLASH_H = 1360, 768          # native HDMI splash size
SPINNER_SIZE = 64                       # square spinner canvas

# Render the lockup on a supersampled canvas, then downsample, so the
# rounded-rect corners and glyph edges come out cleanly anti-aliased.
SS = 4

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "openmarquee")


# --- Font discovery -------------------------------------------------------
# Prefer DejaVuSans-Bold: that is what ships on the Pi (the `fonts-dejavu`
# apt package) so the committed artwork matches the platform's idea of the
# typeface. On a macOS dev host DejaVu is usually absent, so fall back to
# the system bold sans options. PIL's bitmap default is the last resort.
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Pi / Debian
    "/Library/Fonts/DejaVuSans-Bold.ttf",                     # mac (manual)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
    "/System/Library/Fonts/Helvetica.ttc",                    # macOS
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Return a bold sans-serif font at ``size`` px, trying known paths."""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # Last resort -- PIL's built-in bitmap font (not scalable, but never
    # leaves us empty-handed). The PNGs will still be valid.
    print("WARNING: no bold sans font found; using PIL default.",
          file=sys.stderr)
    return ImageFont.load_default()


def font_path_used() -> str:
    """Report which font file load_font() will pick (for the run log)."""
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return "<PIL default bitmap font>"


def _text_size(draw: ImageDraw.ImageDraw, text: str,
                font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    """Tight (width, height) of ``text`` rendered with ``font``."""
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _draw_tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int],
                       text: str, font: ImageFont.FreeTypeFont,
                       fill: tuple[int, int, int],
                       tracking: int) -> int:
    """Draw ``text`` one glyph at a time with extra ``tracking`` px between
    glyphs (negative = tighter). Returns the x advance consumed."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        box = draw.textbbox((0, 0), ch, font=font)
        x += (box[2] - box[0]) + tracking
    return x - xy[0]


def _tracked_width(draw: ImageDraw.ImageDraw, text: str,
                   font: ImageFont.FreeTypeFont, tracking: int) -> int:
    """Width of ``text`` if drawn with ``_draw_tracked_text``."""
    w = 0
    for ch in text:
        box = draw.textbbox((0, 0), ch, font=font)
        w += (box[2] - box[0]) + tracking
    return w - tracking if text else 0


def generate_splash() -> str:
    """Render the brand lockup splash and save splash.png. Returns path."""
    # Work supersampled, then downscale once at the end.
    W, H = SPLASH_W * SS, SPLASH_H * SS
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # --- The "oM" mark: an amber rounded square ---------------------------
    mark_size = 150 * SS                       # ~150px tall in final image
    radius = int(mark_size * 0.18)             # ~18% corner radius
    mark_font = load_font(int(mark_size * 0.46))

    # --- The wordmark: "open" (near-white) + "Marquee" (amber) ------------
    word_font = load_font(int(mark_size * 0.62))
    tracking = -int(2 * SS)                    # slightly tight letter-spacing
    gap = int(mark_size * 0.34)                # space between mark and word

    open_w = _tracked_width(draw, "open", word_font, tracking)
    marq_w = _tracked_width(draw, "Marquee", word_font, tracking)
    word_w = open_w + marq_w

    lockup_w = mark_size + gap + word_w

    # Horizontal: centered. Vertical: a bit above center so the spinner
    # (drawn by the Plymouth script) has room below.
    lockup_x = (W - lockup_w) // 2
    lockup_cy = int(H * 0.42)                  # vertical center of the lockup

    # Draw the amber rounded-rect mark.
    mark_top = lockup_cy - mark_size // 2
    draw.rounded_rectangle(
        [lockup_x, mark_top, lockup_x + mark_size, mark_top + mark_size],
        radius=radius, fill=ACCENT)

    # Center "oM" inside the mark.
    om_w, om_h = _text_size(draw, "oM", mark_font)
    om_box = draw.textbbox((0, 0), "oM", font=mark_font)
    om_x = lockup_x + (mark_size - om_w) // 2 - om_box[0]
    om_y = mark_top + (mark_size - om_h) // 2 - om_box[1]
    draw.text((om_x, om_y), "oM", font=mark_font, fill=MARK_INK)

    # Draw the wordmark, vertically centered on the mark.
    word_box = draw.textbbox((0, 0), "openMarquee", font=word_font)
    word_h = word_box[3] - word_box[1]
    word_x = lockup_x + mark_size + gap
    word_y = lockup_cy - word_h // 2 - word_box[1]
    consumed = _draw_tracked_text(draw, (word_x, word_y), "open",
                                  word_font, TEXT_LIGHT, tracking)
    _draw_tracked_text(draw, (word_x + consumed, word_y), "Marquee",
                       word_font, ACCENT, tracking)

    # Downsample to native size with a high-quality filter.
    final = img.resize((SPLASH_W, SPLASH_H), Image.LANCZOS)
    out = os.path.join(OUT_DIR, "splash.png")
    final.save(out)
    return out


def generate_spinner() -> str:
    """Render the amber activity arc and save spinner.png. Returns path."""
    # Supersample for smooth arc edges.
    S = SPINNER_SIZE * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # A partial ring: a ~270-degree arc, thick stroke, amber. Rotating
    # this each frame reads as an indeterminate "working" spinner.
    stroke = int(S * 0.13)
    pad = stroke // 2 + int(S * 0.06)
    box = [pad, pad, S - pad, S - pad]
    draw.arc(box, start=0, end=270, fill=ACCENT + (255,), width=stroke)

    final = img.resize((SPINNER_SIZE, SPINNER_SIZE), Image.LANCZOS)
    out = os.path.join(OUT_DIR, "spinner.png")
    final.save(out)
    return out


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"font: {font_path_used()}")
    splash = generate_splash()
    print(f"wrote {splash}")
    spinner = generate_spinner()
    print(f"wrote {spinner}")


if __name__ == "__main__":
    main()
