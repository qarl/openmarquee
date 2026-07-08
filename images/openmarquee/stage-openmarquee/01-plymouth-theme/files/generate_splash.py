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

The splash reproduces the dashboard sidebar's stacked wordmark lockup
(``<span class="om-wordmark om-wordmark-stack">`` in ``ui/index.html``):
a dot-matrix LED mark on the left, then "OPEN" stacked over "Marquee".

Run it from anywhere with Pillow available (the repo backend venv at
/opt/openmarquee/venv has it, and so does a typical dev host)::

    python3 generate_splash.py

All brand values below are the verified CSS custom properties from
``ui/styles.css`` (the dashboard ``:root`` / ``.om-wordmark`` rules,
around lines 2600-2841) -- keep them in sync if the brand palette moves.
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFont

# --- Brand palette (verified against ui/styles.css :root + .om-wordmark) ---
BG          = (0x0e, 0x0e, 0x10)   # splash background (Plymouth .script matches)
ACCENT      = (0xff, 0xb4, 0x3c)   # --om-accent       LED-dot amber
TEXT        = (0xe7, 0xea, 0xee)   # --om-text         "Marquee" near-white
LED_BG      = (0x05, 0x06, 0x08)   # --om-led-bg       dot-matrix tile background
# --om-text-fade is rgba(231,234,238,.50); the splash background is opaque, so
# "OPEN" is that 50%-alpha grey composited over BG -> a flat mid-grey.
TEXT_FADE   = tuple(round(t * 0.5 + b * 0.5) for t, b in zip(TEXT, BG))
# --om-line-2 is rgba(255,255,255,.14): the faint inset edge on the mark tile,
# composited over the LED background.
LINE_2      = tuple(round(0xff * 0.14 + b * 0.86) for b in LED_BG)
# --om-accent-glow is rgba(255,180,60,.35): the faint halo around each LED dot,
# composited over the LED background.
ACCENT_GLOW = tuple(round(g * 0.35 + b * 0.65)
                    for g, b in zip((0xff, 0xb4, 0x3c), LED_BG))

# --- Output geometry ------------------------------------------------------
SPLASH_W, SPLASH_H = 1360, 768          # native HDMI splash size
SPINNER_SIZE = 64                       # square spinner canvas

# Render the lockup on a supersampled canvas, then downsample, so the
# rounded-rect corners, dot-matrix circles and glyph edges come out
# cleanly anti-aliased.
SS = 4

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "openmarquee")


# --- Font discovery -------------------------------------------------------
# The lockup needs two typefaces:
#   * a BOLD SANS for "Marquee" (the dominant line),
#   * a MONOSPACE for "OPEN" (the spaced-out, faded upper line).
# Prefer the DejaVu faces: that is what ships on the Pi (the `fonts-dejavu`
# apt package) so the committed artwork matches the platform's idea of the
# typefaces. On a macOS dev host DejaVu is usually absent, so fall back to
# the system options. PIL's bitmap default is the last resort.
_SANS_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",   # Pi / Debian
    "/Library/Fonts/DejaVuSans-Bold.ttf",                     # mac (manual)
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",      # macOS
    "/System/Library/Fonts/Helvetica.ttc",                    # macOS
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_MONO_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",  # Pi / Debian
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",       # Pi / Debian
    "/Library/Fonts/DejaVuSansMono.ttf",                         # mac (manual)
    "/System/Library/Fonts/SFNSMono.ttf",                        # macOS
    "/System/Library/Fonts/Supplemental/Menlo.ttc",              # macOS
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)


def _first_existing(candidates: tuple[str, ...]) -> str | None:
    """Return the first path in ``candidates`` that exists, or None."""
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_font(size: int, family: str = "sans") -> ImageFont.FreeTypeFont:
    """Return a font at ``size`` px for ``family`` ("sans" bold or "mono").

    Tries the known platform paths in order; PIL's built-in bitmap font is
    the last resort so the PNGs are always valid even with no TrueType
    fonts installed.
    """
    candidates = _SANS_CANDIDATES if family == "sans" else _MONO_CANDIDATES
    path = _first_existing(candidates)
    if path is not None:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    print(f"WARNING: no {family} font found; using PIL default.",
          file=sys.stderr)
    return ImageFont.load_default()


def font_path_used(family: str = "sans") -> str:
    """Report which font file load_font() picks for ``family`` (run log)."""
    candidates = _SANS_CANDIDATES if family == "sans" else _MONO_CANDIDATES
    return _first_existing(candidates) or "<PIL default bitmap font>"


def _draw_tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int],
                       text: str, font: ImageFont.FreeTypeFont,
                       fill: tuple[int, int, int],
                       tracking: int) -> int:
    """Draw ``text`` one glyph at a time with extra ``tracking`` px between
    glyphs (negative = tighter). Returns the x advance consumed."""
    x = xy[0]
    for ch in text:
        draw.text((x, xy[1]), ch, font=font, fill=fill)
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


def _draw_dot_matrix_mark(draw: ImageDraw.ImageDraw, box: tuple[int, ...],
                          radius: int) -> None:
    """Draw the dot-matrix LED mark inside ``box`` (left, top, right, bottom).

    Reproduces ``.om-wordmark.om-wordmark-stack .om-mark`` from styles.css:
    a rounded-rect LED tile (--om-led-bg) with a 1px-equivalent inset edge
    (--om-line-2), and an interior dot matrix inset 2/16 of the tile width.
    The interior dots are amber (--om-accent) on a 2px checkerboard pitch
    -- one dot per cell where (col + row) is even -- each with a faint
    accent-glow halo. The real menu mark interior is ~12px wide / ~6 dot
    columns; we keep ~6 columns so the splash density matches the menu.
    """
    left, top, right, bottom = box
    tile_w = right - left
    tile_h = bottom - top

    # The LED tile: rounded rect, near-black, faint inset light edge.
    draw.rounded_rectangle(box, radius=radius, fill=LED_BG)
    draw.rounded_rectangle(box, radius=radius, outline=ACCENT,
                           width=max(2, round(tile_w * 0.075)))

    # Interior dot field, inset 2/16 of the tile width on every side (the
    # CSS `inset: 2px` on a 16px-wide tile).
    inset = round(tile_w * (2.0 / 16.0))
    in_left, in_top = left + inset, top + inset
    in_w = tile_w - 2 * inset
    in_h = tile_h - 2 * inset

    # ~6 dot columns across the interior, matching the menu's ~12px / 2px
    # pitch. The pitch (one dot per cell) follows from the column count;
    # rows are however many of that same pitch fit the (taller) interior.
    cols = 6
    pitch = in_w / cols
    rows = max(1, round(in_h / pitch))

    # Each dot is a small filled circle. The CSS radial gradient paints a
    # dot ~38% of the way out of a 2px (half-cell) radius -> a dot radius
    # of roughly 0.38 of the half-cell. The glow is a slightly larger,
    # fainter disc drawn underneath.
    half = pitch / 2.0
    dot_r = half * 0.38
    glow_r = dot_r * 1.9

    for row in range(rows):
        for col in range(cols):
            # Checkerboard: a dot only where (col + row) is even.
            if (col + row) % 2 != 0:
                continue
            cx = in_left + (col + 0.5) * pitch
            cy = in_top + (row + 0.5) * pitch
            # Faint accent glow halo, then the bright dot on top.
            draw.ellipse([cx - glow_r, cy - glow_r,
                          cx + glow_r, cy + glow_r], fill=ACCENT_GLOW)
            draw.ellipse([cx - dot_r, cy - dot_r,
                          cx + dot_r, cy + dot_r], fill=ACCENT)


def _lockup_metrics(draw: ImageDraw.ImageDraw, target_lockup_w: int) -> dict:
    """Size the [ dot-matrix mark ][ gap ][ "OPEN" over "Marquee" ] lockup
    so the whole group is ``target_lockup_w`` px wide. Returns the fonts,
    per-glyph tracking, text bboxes and stack/mark dims.

    Pure sizing — uses ``draw`` only for font metrics, so a scratch canvas
    works. Shared by the splash + the mark-only asset so both are
    pixel-faithful to the same geometry.
    """
    # "Marquee" is the dominant line; "OPEN" is 0.62x its size, all-caps,
    # monospace, widely tracked (CSS letter-spacing 0.22em). Pick the
    # "Marquee" font size, derive everything else, then scale the whole
    # lockup to hit target_lockup_w.
    marq_size = 132 * SS  # provisional; rescaled below
    open_size = round(marq_size * 0.62)  # CSS font-size: 0.62em

    marq_font = load_font(marq_size, "sans")
    open_font = load_font(open_size, "mono")

    # Tracking, scaled to font size so it survives the rescale below.
    # "Marquee": CSS letter-spacing -0.015em (slightly tight).
    marq_track = -round(marq_size * 0.015)
    # "OPEN": CSS letter-spacing 0.22em (wide). Mono advance already
    # includes glyph width, so this is pure extra spread.
    open_track = round(open_size * 0.22)

    marq_w = _tracked_width(draw, "Marquee", marq_font, marq_track)
    open_w = _tracked_width(draw, "OPEN", open_font, open_track)
    text_w = max(marq_w, open_w)

    # Stack metrics. line-height: 1, with "OPEN" sitting above "Marquee"
    # separated by CSS margin-bottom: 0.4em (0.4 of the OPEN size).
    marq_box = draw.textbbox((0, 0), "Marquee", font=marq_font)
    open_box = draw.textbbox((0, 0), "OPEN", font=open_font)
    marq_h = marq_box[3] - marq_box[1]
    open_h = open_box[3] - open_box[1]
    gap_lines = round(open_size * 0.40)  # CSS margin-bottom: 0.4em
    stack_h = open_h + gap_lines + marq_h

    # Mark : gap : text proportions, from the menu CSS. The stacked mark is
    # 16 units wide with a 5-unit gap to the text (.om-wordmark-stack
    # gap: 5px; .om-mark width: 16px). The mark height = full stack height.
    mark_w = round(stack_h * (16.0 / 32.0))  # 16-wide vs a ~32-tall stack
    inner_gap = round(mark_w * (5.0 / 16.0))

    lockup_w = mark_w + inner_gap + text_w

    # Rescale the whole lockup so it lands on the target width. Re-load the
    # fonts at the scaled sizes (Pillow fonts are immutable per size).
    scale = target_lockup_w / lockup_w
    marq_size = max(8, round(marq_size * scale))
    open_size = max(6, round(marq_size * 0.62))
    marq_font = load_font(marq_size, "sans")
    open_font = load_font(open_size, "mono")
    marq_track = -round(marq_size * 0.015)
    open_track = round(open_size * 0.22)

    marq_w = _tracked_width(draw, "Marquee", marq_font, marq_track)
    open_w = _tracked_width(draw, "OPEN", open_font, open_track)
    text_w = max(marq_w, open_w)

    marq_box = draw.textbbox((0, 0), "Marquee", font=marq_font)
    open_box = draw.textbbox((0, 0), "OPEN", font=open_font)
    marq_h = marq_box[3] - marq_box[1]
    open_h = open_box[3] - open_box[1]
    gap_lines = round(open_size * 0.40)
    stack_h = open_h + gap_lines + marq_h

    mark_w = round(stack_h * (16.0 / 32.0))
    inner_gap = round(mark_w * (5.0 / 16.0))
    lockup_w = mark_w + inner_gap + text_w

    return {
        "marq_font": marq_font,
        "open_font": open_font,
        "marq_track": marq_track,
        "open_track": open_track,
        "marq_box": marq_box,
        "open_box": open_box,
        "open_h": open_h,
        "gap_lines": gap_lines,
        "stack_h": stack_h,
        "mark_w": mark_w,
        "inner_gap": inner_gap,
        "lockup_w": lockup_w,
    }


def _draw_lockup(
    draw: ImageDraw.ImageDraw, m: dict, lockup_x: int, stack_top: int
) -> None:
    """Draw the dot-matrix mark + "OPEN" over "Marquee" at (lockup_x,
    stack_top) using the metrics from ``_lockup_metrics``."""
    # --- The dot-matrix mark --------------------------------------------
    mark_radius = 0  # qarl: square corners, not rounded
    # qarl: the mark box bottom aligns with the "Marquee" baseline (the
    # bottom of the "M"), NOT the "q" descender. "M" is a cap with no
    # descender — (inked top of Marquee) + (M bbox bottom - Marquee bbox
    # top) lands exactly on the baseline.
    mbb = draw.textbbox((0, 0), "M", font=m["marq_font"])
    mark_bottom = stack_top + m["open_h"] + m["gap_lines"] + (mbb[3] - m["marq_box"][1])
    _draw_dot_matrix_mark(
        draw,
        (lockup_x, stack_top, lockup_x + m["mark_w"], mark_bottom),
        mark_radius,
    )

    # --- The stacked text ------------------------------------------------
    # Left-aligned to the same x, just right of the mark + gap.
    text_x = lockup_x + m["mark_w"] + m["inner_gap"]

    # "OPEN": top line, monospace, faded mid-grey, widely tracked. Drawn at
    # the top of the stack (textbbox origin offset removed so it sits flush).
    open_y = stack_top - m["open_box"][1]
    _draw_tracked_text(draw, (text_x, open_y), "OPEN", m["open_font"], TEXT_FADE, m["open_track"])

    # "Marquee": bottom line, bold sans, near-white, slightly tight. One
    # uniform color -- the stacked "M" is NOT accent (variant-G call).
    marq_y = stack_top + m["open_h"] + m["gap_lines"] - m["marq_box"][1]
    _draw_tracked_text(draw, (text_x, marq_y), "Marquee", m["marq_font"], TEXT, m["marq_track"])


def generate_splash() -> str:
    """Render the stacked-wordmark lockup splash and save splash.png.

    The lockup is the dashboard sidebar wordmark, left-to-right:
        [ dot-matrix mark ]  [ gap ]  [ "OPEN" over "Marquee" ]
    Returns the output path.
    """
    # Work supersampled, then downscale once at the end.
    W, H = SPLASH_W * SS, SPLASH_H * SS
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Render the lockup large + prominent: target ~60% of the 1360px panel
    # width for the whole [mark + gap + text] group.
    target_lockup_w = int(SPLASH_W * 0.60 * SS)
    m = _lockup_metrics(draw, target_lockup_w)

    # Horizontal: centered. Vertical: a bit above center so the spinner
    # (drawn by the Plymouth script) has room below.
    lockup_x = (W - m["lockup_w"]) // 2
    lockup_cy = int(H * 0.42)  # vertical center of the lockup
    stack_top = lockup_cy - m["stack_h"] // 2  # top of "OPEN"
    _draw_lockup(draw, m, lockup_x, stack_top)

    # Downsample to native size with a high-quality filter.
    final = img.resize((SPLASH_W, SPLASH_H), Image.LANCZOS)
    out = os.path.join(OUT_DIR, "splash.png")
    final.save(out)
    return out


def generate_mark() -> str:
    """Render the wordmark lockup ALONE on a TRANSPARENT, tight-cropped,
    high-res canvas (mark.png) — the renderer bakes this in and blits it on
    the boot identity card (2026-07-07, qarl). Same geometry + fonts as the
    splash lockup, so it is pixel-faithful to the approved splash artwork
    (no re-approximation). Kept high-res so it stays crisp scaled down for
    both the portrait panel and a landscape HDMI boot card.

    The LED tile is opaque (its own near-black interior); the "OPEN"/
    "Marquee" glyphs + mark are drawn over transparency. The boot card's
    background is the same #0e0e10 as the splash, so the pre-composited
    faded "OPEN" grey lands identically on-glass.
    """
    # Size on a scratch draw (font metrics only), same supersampled target
    # width as the splash lockup so the glyph proportions match exactly.
    scratch = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    target_lockup_w = int(SPLASH_W * 0.60 * SS)
    m = _lockup_metrics(scratch, target_lockup_w)

    # Small transparent margin so the amber border / dot glow isn't clipped.
    pad = max(1, round(m["stack_h"] * 0.05))
    canvas = Image.new("RGBA", (m["lockup_w"] + 2 * pad, m["stack_h"] + 2 * pad), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    _draw_lockup(draw, m, pad, pad)

    # Tight-crop to the inked bbox (glow included), then downsample the
    # supersampled render to a crisp hi-res asset (~1/2 the SS render →
    # lockup ~1600px wide, ample for portrait + landscape boot cards).
    bbox = canvas.getbbox()
    if bbox:
        canvas = canvas.crop(bbox)
    down = max(1, SS // 2)
    if down > 1:
        canvas = canvas.resize(
            (max(1, canvas.width // down), max(1, canvas.height // down)),
            Image.LANCZOS,
        )
    out = os.path.join(OUT_DIR, "mark.png")
    canvas.save(out)
    return out


def generate_spinner() -> str:
    """Render the amber activity arc and save spinner.png. Returns path."""
    # spinner kept at its original amber per the 2026-05-21 logo-fix
    # dispatch -- spinner.png is unchanged; only the splash adopts the
    # corrected brand accent #ffb43c.
    SPINNER_ACCENT = (0xff, 0xb8, 0x4d)

    # Supersample for smooth arc edges.
    S = SPINNER_SIZE * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # A partial ring: a ~270-degree arc, thick stroke, amber. Rotating
    # this each frame reads as an indeterminate "working" spinner.
    stroke = int(S * 0.13)
    pad = stroke // 2 + int(S * 0.06)
    box = [pad, pad, S - pad, S - pad]
    draw.arc(box, start=0, end=270, fill=SPINNER_ACCENT + (255,), width=stroke)

    final = img.resize((SPINNER_SIZE, SPINNER_SIZE), Image.LANCZOS)
    out = os.path.join(OUT_DIR, "spinner.png")
    final.save(out)
    return out


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"sans font: {font_path_used('sans')}")
    print(f"mono font: {font_path_used('mono')}")
    splash = generate_splash()
    print(f"wrote {splash}")
    mark = generate_mark()
    print(f"wrote {mark}")
    spinner = generate_spinner()
    print(f"wrote {spinner}")


if __name__ == "__main__":
    main()
