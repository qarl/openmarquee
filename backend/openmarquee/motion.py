"""Per-layer motion effects for text slides (spec step 3a).

Implements the six operator-facing motion values as per-tick transforms
on a layer's pre-rendered RGBA bitmap. Composer in `compose_motion_frame`
ties background + per-layer rendering + per-layer transform + composite
into one slide-sized RGB frame the renderer can flush.

Spec: docs/text-layer-motion-spec.md (5e75cf5).

Effects:
- `ticker`  — linear horizontal travel, np.roll wrap inside the box.
- `breathe` — sine scale around box center, glyph-bbox cropped for perf,
              preserves operator-placed offset by orbiting around box
              center rather than glyph center.
- `pulse`   — sine alpha modulation, intensity sets the depth of swing.
- `bounce`  — sine vertical bob inside the box, no wrap (clipped).
- `shake`   — deterministic Gaussian micro-jitter, seeded from layer.id
              + motion_phase so reloads / editor preview / device all
              produce the same offset sequence.
- `blink`   — square-wave on/off opacity, intensity sets frequency.

All effects clip to the layer's box rect. The shared global tick clock
(`elapsed_s` from PlaybackLoop) feeds `compute_phase` along with the
layer's `motion_phase` field — two layers with the same effect and
phase=0 / phase=0.5 run in opposition.
"""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
from PIL import Image

from openmarquee.auto_render import _load_background
from openmarquee.seed import _draw_text_into

if TYPE_CHECKING:
    from uuid import UUID

    from openmarquee.content import TextLayer, TextSlide

log = logging.getLogger(__name__)


# Ticker travels through one full box-width per cycle. At intensity=50
# the cycle is ~3 s (per the spec's intensity=50 default table); at 0
# it slows to ~6 s, at 100 it speeds to ~1 s.
def _ticker_freq(intensity: int) -> float:
    cycle_s = max(0.5, 6.0 - 0.05 * intensity)
    return 1.0 / cycle_s


# Blink frequency table (per spec): 0.5 Hz slow → 1 Hz default → 4 Hz fast.
# Piecewise-linear so intensity=50 lands exactly on 1 Hz; the upper half
# climbs steeper to reach 4 Hz at 100.
def _blink_freq(intensity: int) -> float:
    if intensity <= 50:
        return 0.5 + 0.01 * intensity
    return 1.0 + 0.06 * (intensity - 50)


def _effect_freq(motion: str, intensity: int) -> float:
    """Cycle frequency in Hz for the named effect. Used by compute_phase
    so a single shared monotonic clock drives every layer's animation."""
    if motion == "ticker":
        return _ticker_freq(intensity)
    if motion in ("breathe", "pulse", "bounce"):
        # Spec defaults to 1 Hz for these, intensity modulates amplitude
        # not frequency.
        return 1.0
    if motion == "shake":
        # 10 Hz random-walk; intensity modulates amplitude, not freq.
        return 10.0
    if motion == "blink":
        return _blink_freq(intensity)
    return 1.0


def compute_phase(elapsed_s: float, freq_hz: float, motion_phase: float) -> float:
    """Cycle position in [0, 1) for a layer at `elapsed_s` on the shared
    global clock. `motion_phase` (0.0–1.0 from the layer's schema field)
    is added so two layers with the same effect can be set out-of-sync."""
    return (elapsed_s * freq_hz + motion_phase) % 1.0


# --- per-effect transforms ---


def _apply_ticker(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
) -> Image.Image:
    """Linear horizontal travel inside the box. Per cycle, the box's
    contents drift `box_w` pixels leftward and wrap. np.roll on the
    box-cropped region produces the marquee wrap-around — text leaves
    the left edge and re-enters from the right."""
    bx, by, bw, bh = box_px
    if bw <= 0 or bh <= 0:
        return layer_rgba
    shift_px = int(round(-phase * bw))
    box_region = layer_rgba.crop((bx, by, bx + bw, by + bh))
    arr = np.array(box_region)
    arr = np.roll(arr, shift_px, axis=1)
    rolled = Image.fromarray(arr, mode="RGBA")
    out = Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))
    out.paste(rolled, (bx, by))
    return out


def _apply_breathe(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
) -> Image.Image:
    """Sine scale around box center. The glyph bbox (tight rect of
    non-transparent pixels inside the box) is cropped first so the
    resize cost is proportional to ink, not box area — at 1080 p with
    a 700 × 200 glyph that's a 11× win over resizing the full box.

    Pivot is the BOX center, not the glyph center. If the operator
    placed text off-center within the box, the offset is preserved
    through the cycle (text orbits outward as it grows, inward as it
    shrinks)."""
    bx, by, bw, bh = box_px
    if bw <= 0 or bh <= 0:
        return layer_rgba
    amplitude = (intensity / 100.0) * 0.20  # ±20 % at intensity=100
    s = 1.0 + amplitude * math.sin(2 * math.pi * phase)
    box_region = layer_rgba.crop((bx, by, bx + bw, by + bh))
    glyph_bbox = box_region.getbbox()
    if glyph_bbox is None:
        # Empty layer (no ink): nothing to scale.
        return layer_rgba
    gx, gy, gx2, gy2 = glyph_bbox
    gw, gh = gx2 - gx, gy2 - gy
    glyph = box_region.crop(glyph_bbox)
    new_w = max(1, int(round(gw * s)))
    new_h = max(1, int(round(gh * s)))
    if (new_w, new_h) == (gw, gh):
        scaled = glyph
    else:
        scaled = glyph.resize((new_w, new_h), resample=Image.Resampling.NEAREST)
    # Box center, glyph center → offset → orbit at scale s.
    box_cx = bw / 2
    box_cy = bh / 2
    glyph_cx = gx + gw / 2
    glyph_cy = gy + gh / 2
    dx = glyph_cx - box_cx
    dy = glyph_cy - box_cy
    new_cx = box_cx + s * dx
    new_cy = box_cy + s * dy
    paste_x = int(round(new_cx - new_w / 2))
    paste_y = int(round(new_cy - new_h / 2))
    out_box = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out_box.paste(scaled, (paste_x, paste_y), scaled)
    out = Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))
    out.paste(out_box, (bx, by))
    return out


def _apply_pulse(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
) -> Image.Image:
    """Sine alpha modulation. Intensity controls the depth of the
    swing: at 0 the layer barely breathes around full opacity, at 100
    it dips all the way to transparent."""
    bx, by, bw, bh = box_px
    if bw <= 0 or bh <= 0:
        return layer_rgba
    min_a = 1.0 - intensity / 100.0
    s = (math.sin(2 * math.pi * phase) + 1) / 2  # 0..1
    a = min_a + (1.0 - min_a) * s
    box_region = layer_rgba.crop((bx, by, bx + bw, by + bh))
    arr = np.array(box_region)
    arr[:, :, 3] = (arr[:, :, 3].astype(np.float32) * a).astype(np.uint8)
    out_box = Image.fromarray(arr, mode="RGBA")
    out = Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))
    out.paste(out_box, (bx, by))
    return out


def _apply_bounce(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
) -> Image.Image:
    """Sine vertical bob inside the box. Unlike `ticker`, the bob does
    NOT wrap — exiting the top/bottom box edge clips. Operators expect
    bouncing text to disappear if it bounces too high, not to teleport
    to the bottom."""
    bx, by, bw, bh = box_px
    if bw <= 0 or bh <= 0:
        return layer_rgba
    amplitude = (intensity / 100.0) * 0.10  # ±10 % box height at 100
    offset_px = int(round(amplitude * bh * math.sin(2 * math.pi * phase)))
    box_region = layer_rgba.crop((bx, by, bx + bw, by + bh))
    out_box = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out_box.paste(box_region, (0, offset_px))
    out = Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))
    out.paste(out_box, (bx, by))
    return out


def _shake_seed(layer_id: str, motion_phase: float, step: int) -> int:
    """Stable seed for the deterministic shake RNG. Same layer + same
    phase + same step → same Gaussian draw, so frame drops don't
    desync the shake pattern across the device and the editor preview.
    Different layers produce different sequences (so multi-layer shake
    doesn't look mechanically identical)."""
    blob = f"{layer_id}:{motion_phase:.6f}:{step}".encode()
    return int.from_bytes(hashlib.md5(blob).digest()[:8], "big", signed=False)


def _apply_shake(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
    layer_id: str,
    motion_phase: float,
) -> Image.Image:
    """Deterministic Gaussian micro-jitter at ~10 Hz (frequency baked
    into _effect_freq). `phase` advances 10× per second; we discretize
    it into a step count so each ~100 ms tick draws one fresh offset
    rather than smearing continuously. Amplitude scales with the
    glyph height — big text shakes by more pixels, small text by
    fewer — so the perceived intensity is constant at any sign res."""
    bx, by, bw, bh = box_px
    if bw <= 0 or bh <= 0 or intensity <= 0:
        return layer_rgba
    box_region = layer_rgba.crop((bx, by, bx + bw, by + bh))
    glyph_bbox = box_region.getbbox()
    if glyph_bbox is None:
        return layer_rgba
    glyph_h = glyph_bbox[3] - glyph_bbox[1]
    amplitude_px = max(1, int(round((intensity / 100.0) * 0.04 * glyph_h)))
    step = int(phase * 1000) // 100  # ~10 distinct steps per cycle
    rng = np.random.default_rng(_shake_seed(layer_id, motion_phase, step))
    dx = int(round(rng.normal(0, amplitude_px / 2)))
    dy = int(round(rng.normal(0, amplitude_px / 2)))
    out_box = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    out_box.paste(box_region, (dx, dy))
    out = Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))
    out.paste(out_box, (bx, by))
    return out


def _apply_blink(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    intensity: int,
    phase: float,
) -> Image.Image:
    """Hard square-wave on/off opacity. 50 % duty cycle: on for the
    first half of each cycle, fully off for the second. Frequency
    comes from _effect_freq → _blink_freq."""
    if phase < 0.5:
        return layer_rgba
    return Image.new("RGBA", layer_rgba.size, (0, 0, 0, 0))


def apply_motion(
    layer_rgba: Image.Image,
    box_px: tuple[int, int, int, int],
    motion: str,
    intensity: int,
    phase: float,
    layer_id: str,
    motion_phase: float,
) -> Image.Image:
    """Dispatch to the named effect. Unknown / "static" returns the
    layer unchanged (renderer treats absent or future-extension values
    as static, matching the spec's forward-compat note)."""
    if motion == "ticker":
        return _apply_ticker(layer_rgba, box_px, intensity, phase)
    if motion == "breathe":
        return _apply_breathe(layer_rgba, box_px, intensity, phase)
    if motion == "pulse":
        return _apply_pulse(layer_rgba, box_px, intensity, phase)
    if motion == "bounce":
        return _apply_bounce(layer_rgba, box_px, intensity, phase)
    if motion == "shake":
        return _apply_shake(
            layer_rgba, box_px, intensity, phase, layer_id, motion_phase
        )
    if motion == "blink":
        return _apply_blink(layer_rgba, box_px, intensity, phase)
    return layer_rgba


# --- per-frame composer ---


def render_layer_to_rgba(
    layer: "TextLayer", width: int, height: int
) -> Image.Image:
    """Render one layer's text into a transparent slide-sized RGBA
    bitmap. Reuses `_draw_text_into` so the pixels match what the
    asset PNG already on disk shows for static layers (operators
    don't see a font / placement diff when toggling motion on)."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    _draw_text_into(
        img,
        text=getattr(layer, "text", ""),
        fg=getattr(layer, "text_color", "#FFFFFF"),
        font_family=getattr(layer, "font_family", None),
        box=getattr(layer, "box", None),
        slide_width=width,
        slide_height=height,
        font_size_pct=getattr(layer, "font_size_pct", None),
        font_size_px=getattr(layer, "font_size_px", None),
    )
    return img


def _box_px(layer: "TextLayer", width: int, height: int) -> tuple[int, int, int, int]:
    """Convert a layer's normalized box (x/y/w/h ∈ [0, 1]) to integer
    slide-pixel coords."""
    box = getattr(layer, "box", None)
    if box is None:
        return (0, 0, width, height)
    bx = int(round(float(getattr(box, "x", 0.0)) * width))
    by = int(round(float(getattr(box, "y", 0.0)) * height))
    bw = max(1, int(round(float(getattr(box, "w", 1.0)) * width)))
    bh = max(1, int(round(float(getattr(box, "h", 1.0)) * height)))
    return (bx, by, bw, bh)


def prerender_layer_bitmaps(
    slide: "TextSlide", width: int, height: int
) -> list[Image.Image | None]:
    """Rasterize each visible layer's text once into a slide-sized RGBA
    bitmap, parallel to `slide.text_layers`. Hidden layers get `None`
    in their slot so the composer's hidden-layer skip path doesn't
    consult the cache at all. Used by `_play_motion_slide` at slide
    entry so the per-tick path skips PIL's text rasterization
    entirely — animated layers just transform + alpha-composite the
    cached bitmap, static layers paste it as-is.

    The text doesn't change during a slide's playback (auto-mode
    slides go through a separate path; motion's job is to MOVE the
    pixels, not redraw them), so the cache stays valid for the
    slide's full duration. Cost shifts from per-tick to per-slide,
    which is the difference between 30 N rasterizations / sec and 1
    rasterization / slide-entry. At 1080 p with multiple animated
    layers, that's the difference between blowing the 33 ms tick
    budget and trivially fitting it.
    """
    out: list[Image.Image | None] = []
    for layer in getattr(slide, "text_layers", []):
        if not getattr(layer, "visible", True):
            out.append(None)
            continue
        out.append(render_layer_to_rgba(layer, width, height))
    return out


def load_motion_background(
    slide: "TextSlide",
    width: int,
    height: int,
    read_asset: Callable[["UUID"], bytes] | None = None,
) -> Image.Image:
    """Load this slide's background once. Hoisted out so the playback
    loop can pre-load at slide entry and pass the result into every
    `compose_motion_frame` tick via `background_cache=` — avoids
    re-reading the background PNG (or re-allocating the solid-color
    canvas) 30× per second."""
    return _load_background(slide, width, height, read_asset)


def slide_has_motion(slide: "TextSlide") -> bool:
    """True if any visible layer's `motion` is non-static — used by the
    playback loop to decide between the static/auto path and the per-
    tick re-composition path. Hidden layers don't count: an operator
    who toggled visibility off shouldn't drive 30 fps re-rendering."""
    for layer in getattr(slide, "text_layers", []):
        if not getattr(layer, "visible", True):
            continue
        if getattr(layer, "motion", "static") not in (None, "static"):
            return True
    return False


def compose_motion_frame(
    slide: "TextSlide",
    elapsed_s: float,
    width: int,
    height: int,
    read_asset: Callable[["UUID"], bytes] | None = None,
    background_cache: Image.Image | None = None,
    layer_bitmap_cache: list[Image.Image] | None = None,
) -> Image.Image:
    """Render an RGB frame for `slide` at `elapsed_s` on the shared
    global clock. Caches background across ticks of the same slide via
    `background_cache` AND per-layer text rasterizations via
    `layer_bitmap_cache` — pass both in from `_play_motion_slide`'s
    slide-entry hooks so the per-tick path is just composite + motion.

    Per-frame work with both caches warm, sign-native (128×96), one
    animated layer:
        ~0.3 ms motion transform + ~0.5 ms alpha composite ≈ <1 ms
        on Pi Zero 2 W. 30 fps target = 33 ms budget; trivial.

    Without `layer_bitmap_cache` (cold-call), each visible layer
    re-rasterizes via PIL each frame (~1 ms at 128×96, ~5-10 ms at
    1080 p). That cold-call shape is fine for tests + ad-hoc renders;
    the playback loop's hot path always passes the cache.
    """
    if background_cache is not None and background_cache.size == (width, height):
        base = background_cache.copy()
    else:
        base = _load_background(slide, width, height, read_asset)
    if base.mode != "RGBA":
        base = base.convert("RGBA")

    slide_id = str(getattr(slide, "id", "?"))
    layers = getattr(slide, "text_layers", [])
    for idx, layer in enumerate(layers):
        if not getattr(layer, "visible", True):
            continue
        # Cache hit: reuse the pre-rasterized RGBA. Defensive
        # `idx < len(...)` and `is not None` so a cache built before a
        # hot-edit (which PlaybackLoop currently never does, but tests
        # + future callers might) falls through to a fresh rasterize
        # rather than IndexError- or AttributeError-ing.
        cached: Image.Image | None = None
        if layer_bitmap_cache is not None and idx < len(layer_bitmap_cache):
            entry = layer_bitmap_cache[idx]
            if entry is not None and entry.size == (width, height):
                cached = entry
        layer_rgba = cached if cached is not None else render_layer_to_rgba(
            layer, width, height
        )
        motion = getattr(layer, "motion", "static") or "static"
        if motion != "static":
            box_px = _box_px(layer, width, height)
            phase = compute_phase(
                elapsed_s,
                _effect_freq(motion, getattr(layer, "motion_intensity", 50)),
                float(getattr(layer, "motion_phase", 0.0)),
            )
            # Stable per-layer key for the deterministic shake seed.
            # TextLayer has no id field (schema lives only on the slide
            # variants), so we mix the slide id + layer-index — unique
            # within a slide, stable across saves, and identical-text
            # layers don't collide on shake offsets (which would
            # violate the spec's "different layers shake differently"
            # promise).
            layer_key = f"{slide_id}:{idx}"
            layer_rgba = apply_motion(
                layer_rgba,
                box_px,
                motion,
                int(getattr(layer, "motion_intensity", 50)),
                phase,
                layer_key,
                float(getattr(layer, "motion_phase", 0.0)),
            )
        base.alpha_composite(layer_rgba)

    return base.convert("RGB")
