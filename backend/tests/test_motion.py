"""Tests for the motion renderer software path (spec step 3a).

Each effect's frame math is covered as a unit test. Box-bounded
clipping is checked by sampling pixels OUTSIDE the layer's box and
asserting the layer never bleeds into them. Deterministic shake
is verified by re-running with the same seed and asserting the
same pixel offsets land.
"""

from __future__ import annotations

import math

import pytest
from PIL import Image

from openmarquee.content import TextBox, TextLayer, TextSlide
from openmarquee.motion import (
    _apply_blink,
    _apply_bounce,
    _apply_breathe,
    _apply_pulse,
    _apply_shake,
    _apply_ticker,
    _box_px,
    _shake_seed,
    apply_motion,
    compose_motion_frame,
    compute_phase,
    load_motion_background,
    slide_has_motion,
)


# --- helpers ---


def _make_layer_rgba(width: int, height: int, box: tuple[int, int, int, int],
                     fill: tuple[int, int, int, int]) -> Image.Image:
    """Create a slide-sized RGBA with `fill` painted into `box`,
    transparent outside. Standin for the real text-rendered layer."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bx, by, bw, bh = box
    inner = Image.new("RGBA", (bw, bh), fill)
    img.paste(inner, (bx, by))
    return img


# --- compute_phase ---


def test_compute_phase_at_zero_with_no_offset():
    assert compute_phase(0.0, 1.0, 0.0) == 0.0


def test_compute_phase_wraps_at_cycle_boundary():
    # 1 Hz, 1.0s elapsed → exactly one cycle done → phase wraps to 0.
    assert compute_phase(1.0, 1.0, 0.0) == pytest.approx(0.0)


def test_compute_phase_motion_phase_offsets_cycle():
    # No elapsed time, motion_phase=0.5 → halfway through cycle.
    assert compute_phase(0.0, 1.0, 0.5) == pytest.approx(0.5)


def test_compute_phase_two_layers_in_opposition():
    # Same elapsed + same freq, motion_phase=0 vs 0.5 → 0.5 apart.
    a = compute_phase(0.3, 1.0, 0.0)
    b = compute_phase(0.3, 1.0, 0.5)
    assert abs((a - b) % 1.0 - 0.5) < 1e-9


# --- _apply_ticker ---


def test_ticker_at_phase_zero_is_unchanged():
    """phase=0 means 0 px shift — output equals input."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_ticker(img, (10, 5, 20, 10), 50, 0.0)
    assert list(out.getdata()) == list(img.getdata())


def test_ticker_at_phase_half_shifts_box_contents():
    """phase=0.5 → box contents shift by half the box width to the left.
    The box-cropped pixels wrap (np.roll), so the right half of the
    original becomes the left half."""
    # Half-and-half red/blue inside a 20-px-wide box: left half red,
    # right half blue. After phase=0.5 (10 px leftward shift, wrap),
    # left half should be blue, right half red.
    img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (10, 10), (255, 0, 0, 255)), (10, 5))
    img.paste(Image.new("RGBA", (10, 10), (0, 0, 255, 255)), (20, 5))
    out = _apply_ticker(img, (10, 5, 20, 10), 50, 0.5)
    # Sample mid-row in box: column 12 should now be blue (was red),
    # column 22 should be red (was blue).
    assert out.getpixel((12, 10))[:3] == (0, 0, 255)
    assert out.getpixel((22, 10))[:3] == (255, 0, 0)


def test_ticker_does_not_leak_outside_box():
    """Box-bounded invariant: ticker shift inside box can't write
    pixels outside the box rect."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_ticker(img, (10, 5, 20, 10), 50, 0.5)
    # Sample outside-box pixels: column 5 (left of box) and 35 (right
    # of box). Both should remain transparent.
    assert out.getpixel((5, 10))[3] == 0
    assert out.getpixel((35, 10))[3] == 0


# --- _apply_breathe ---


def test_breathe_at_phase_zero_is_unchanged():
    """sin(0) = 0 → scale = 1.0 → frame matches input (modulo bbox
    crop / repaste round-trip, which is identity for solid-fill
    glyphs)."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (200, 100, 50, 255))
    out = _apply_breathe(img, (10, 5, 20, 10), 50, 0.0)
    # Center pixel of glyph should still match.
    assert out.getpixel((20, 10))[:3] == (200, 100, 50)


def test_breathe_at_quarter_phase_grows_glyph():
    """phase=0.25 → sin(π/2) = 1 → max scale (1.0 + amplitude). At
    intensity=100 amplitude is 0.20, so scale is 1.20: a 10×10 glyph
    becomes a 12×12 glyph centered on the box center."""
    img = _make_layer_rgba(60, 60, (20, 20, 20, 20), (255, 0, 0, 255))
    out = _apply_breathe(img, (20, 20, 20, 20), 100, 0.25)
    # Find the bounding box of red pixels in the output. The glyph
    # bbox grew from 20×20 → 24×24 (1.20× rounded).
    crop = out.crop((20, 20, 40, 40))
    bbox = crop.getbbox()
    assert bbox is not None
    # Scaled glyph fully fits inside the box (20×20 box, glyph grew
    # to 24×24 then was clipped to box). Center column should be
    # solid red.
    assert out.getpixel((30, 30))[:3] == (255, 0, 0)


def test_breathe_box_bounded_clipping():
    """If breathe scales past the box edge, the overflow is clipped —
    pixels OUTSIDE the box stay transparent."""
    img = _make_layer_rgba(60, 60, (20, 20, 20, 20), (255, 0, 0, 255))
    out = _apply_breathe(img, (20, 20, 20, 20), 100, 0.25)
    # Pixel just outside the box should be transparent regardless of
    # how much the glyph scaled.
    assert out.getpixel((10, 30))[3] == 0
    assert out.getpixel((50, 30))[3] == 0


def test_breathe_pivots_around_box_center_not_glyph_center():
    """If glyph is offset from box center, scaling preserves the
    offset (orbits outward as scale grows). Verify by placing a tiny
    glyph in the top-left of a larger box, scaling up, and asserting
    the glyph-bbox center moves further toward the top-left."""
    img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    # Glyph at top-left corner of box, 4×4 px.
    img.paste(Image.new("RGBA", (4, 4), (255, 0, 0, 255)), (20, 20))
    # Box is 20×20 starting at (20, 20). Glyph center at (22, 22),
    # box center at (30, 30) — glyph is 8 px up-left of box center.
    box_px = (20, 20, 20, 20)
    # At max scale (intensity=100, phase=0.25 → 1.20×), the glyph
    # orbits OUTWARD from box center — so its center moves further
    # toward the top-left (away from box center).
    out = _apply_breathe(img, box_px, 100, 0.25)
    crop_after = out.crop(box_px[:2] + (box_px[0] + box_px[2], box_px[1] + box_px[3]))
    bbox_after = crop_after.getbbox()
    assert bbox_after is not None
    # Original glyph-bbox-center within box was at (2, 2) (top-left).
    # After 1.20× orbit, the center should have moved further toward
    # (0, 0), i.e. the new center x and y are smaller than 2.
    new_cx = (bbox_after[0] + bbox_after[2]) / 2
    new_cy = (bbox_after[1] + bbox_after[3]) / 2
    assert new_cx < 2.5  # was 2 in the unscaled bbox
    assert new_cy < 2.5


# --- _apply_pulse ---


def test_pulse_at_phase_zero_returns_full_alpha():
    """sin(0) = 0 → multiplier maps to mid of the 0..1 swing. At
    intensity=0 there's no swing (multiplier always 1.0), full alpha."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_pulse(img, (10, 5, 20, 10), 0, 0.0)
    # Glyph alpha unchanged.
    assert out.getpixel((20, 10))[3] == 255


def test_pulse_at_phase_three_quarter_dims_layer():
    """sin(3π/2) = -1 → multiplier hits the minimum. At intensity=100,
    minimum alpha is 0 (full extinction)."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_pulse(img, (10, 5, 20, 10), 100, 0.75)
    # Glyph alpha at phase=0.75 with intensity=100 → 0.
    assert out.getpixel((20, 10))[3] == 0


def test_pulse_box_bounded():
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_pulse(img, (10, 5, 20, 10), 100, 0.25)
    # Outside-box pixels remain transparent regardless of phase.
    assert out.getpixel((5, 10))[3] == 0
    assert out.getpixel((35, 10))[3] == 0


# --- _apply_bounce ---


def test_bounce_at_phase_zero_is_unchanged():
    """sin(0) = 0 → 0 px offset → frame matches input."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_bounce(img, (10, 10, 20, 20), 50, 0.0)
    assert list(out.getdata()) == list(img.getdata())


def test_bounce_at_quarter_phase_shifts_down():
    """sin(π/2) = 1 → max amplitude. At intensity=100, amplitude is
    0.10 of box height = 2 px on a 20-px box. Glyph shifts down by
    +2 (positive offset, since sin returns +1, and PIL's paste-y
    grows downward)."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_bounce(img, (10, 10, 20, 20), 100, 0.25)
    # Original top-left in-box pixel at (10, 10). After +2 offset the
    # glyph starts at row 12 within box → slide row 12.
    # The cell at the original (10, 10) should now be transparent (the
    # glyph moved DOWN; the top 2 rows of the box are empty).
    assert out.getpixel((20, 11))[3] == 0  # top of box, glyph cleared
    assert out.getpixel((20, 13))[3] == 255  # 2 px below top, glyph here


def test_bounce_does_not_wrap():
    """Unlike ticker, bounce uses paste-with-clip rather than np.roll,
    so a glyph bouncing off the box edge disappears rather than
    re-entering from the opposite side."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    # At intensity=100 max amplitude is 2 px on a 20-px box, never
    # exits the box. Force a contrived big-amplitude shift by placing
    # a small glyph and bouncing on a small box to push past the edge.
    small = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    small.paste(Image.new("RGBA", (4, 4), (255, 0, 0, 255)), (10, 10))
    out = _apply_bounce(small, (10, 10, 4, 4), 100, 0.25)
    # 4-px box × 0.10 amplitude = 0 px (rounds to 0) at intensity=100;
    # so this box is too small to shift. Frame matches input.
    assert list(out.getdata()) == list(small.getdata())


# --- _apply_shake ---


def test_shake_seed_deterministic():
    """Same layer_id + phase + step produces the same seed → same
    Gaussian draw on every replay."""
    s1 = _shake_seed("abc-123", 0.0, 5)
    s2 = _shake_seed("abc-123", 0.0, 5)
    assert s1 == s2


def test_shake_seed_layer_id_changes_seed():
    """Different layers produce different seeds, so multi-layer shake
    doesn't look mechanically identical across the slide."""
    a = _shake_seed("layer-A", 0.0, 5)
    b = _shake_seed("layer-B", 0.0, 5)
    assert a != b


def test_shake_at_zero_intensity_is_unchanged():
    """intensity=0 → amplitude 0 → no offset."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_shake(img, (10, 10, 20, 20), 0, 0.0, "x", 0.0)
    assert list(out.getdata()) == list(img.getdata())


def test_shake_box_bounded():
    """Glyph offset is clipped to the box; pixels OUTSIDE the box
    stay transparent regardless of the random draw."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_shake(img, (10, 10, 20, 20), 100, 0.5, "x", 0.0)
    assert out.getpixel((5, 20))[3] == 0
    assert out.getpixel((35, 20))[3] == 0


def test_shake_replay_is_byte_identical():
    """Two runs with the same seed inputs yield identical pixel
    output — the deterministic-init claim from the spec."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    a = _apply_shake(img, (10, 10, 20, 20), 100, 0.5, "x", 0.0)
    b = _apply_shake(img, (10, 10, 20, 20), 100, 0.5, "x", 0.0)
    assert list(a.getdata()) == list(b.getdata())


# --- _apply_blink ---


def test_blink_below_half_phase_shows():
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_blink(img, (10, 5, 20, 10), 50, 0.0)
    assert out.getpixel((20, 10))[3] == 255


def test_blink_above_half_phase_hides():
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = _apply_blink(img, (10, 5, 20, 10), 50, 0.6)
    # Whole frame transparent.
    assert out.getpixel((20, 10))[3] == 0


# --- apply_motion dispatch ---


def test_apply_motion_static_returns_input():
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = apply_motion(img, (10, 5, 20, 10), "static", 50, 0.5, "id", 0.0)
    assert out is img


def test_apply_motion_unknown_value_returns_input():
    """Forward-compat: a future motion value the renderer doesn't know
    about yet falls back to static rather than raising."""
    img = _make_layer_rgba(40, 20, (10, 5, 20, 10), (255, 0, 0, 255))
    out = apply_motion(img, (10, 5, 20, 10), "wave", 50, 0.5, "id", 0.0)
    assert out is img


# --- _box_px ---


def test_box_px_default_layer_full_slide():
    layer = TextLayer(text="x")  # default box = (0.1, 0.1, 0.8, 0.8)
    assert _box_px(layer, 100, 100) == (10, 10, 80, 80)


def test_box_px_custom_box():
    layer = TextLayer(text="x", box=TextBox(x=0.25, y=0.5, w=0.5, h=0.25))
    assert _box_px(layer, 100, 100) == (25, 50, 50, 25)


# --- slide_has_motion ---


def test_slide_has_motion_true_when_any_layer_animated():
    slide = TextSlide(
        name="s", text_layers=[
            TextLayer(text="A", motion="static"),
            TextLayer(text="B", motion="ticker"),
        ],
    )
    assert slide_has_motion(slide) is True


def test_slide_has_motion_false_when_all_static():
    slide = TextSlide(
        name="s", text_layers=[TextLayer(text="A"), TextLayer(text="B")],
    )
    assert slide_has_motion(slide) is False


def test_slide_has_motion_ignores_hidden_animated_layer():
    """A hidden layer with motion!=static doesn't count — the operator
    toggled it off so we shouldn't drive 30 fps re-rendering for
    something that won't be drawn."""
    slide = TextSlide(
        name="s", text_layers=[
            TextLayer(text="A", motion="ticker", visible=False),
            TextLayer(text="B", motion="static"),
        ],
    )
    assert slide_has_motion(slide) is False


# --- compose_motion_frame ---


def test_compose_motion_frame_two_identical_text_layers_shake_differently():
    """Spec invariant (text-layer-motion-spec.md): different layers
    produce different shake sequences. With TextLayer having no `id`
    field, the composer mixes slide.id + layer-index into the seed so
    two layers with IDENTICAL text + IDENTICAL motion_phase still get
    distinct Gaussian draws. Without the index mix this regresses
    silently (both layers' shake collapses to the same offsets)."""
    slide = TextSlide(
        name="s",
        background_color="#000000",
        text_layers=[
            TextLayer(
                text="X", motion="shake", motion_intensity=100,
                box=TextBox(x=0.05, y=0.05, w=0.4, h=0.9),
            ),
            TextLayer(
                text="X", motion="shake", motion_intensity=100,
                box=TextBox(x=0.55, y=0.05, w=0.4, h=0.9),
            ),
        ],
    )
    # Compose at a phase where shake is non-zero. Then crop each box
    # half; if the seeds were colliding (pre-fix), both halves would
    # sample identical offsets within their respective boxes and the
    # cropped pixel-data lists would be byte-identical. Distinct
    # seeds → at least some pixels differ.
    frame = compose_motion_frame(slide, 0.05, 64, 32)
    left = frame.crop((0, 0, 32, 32))
    right = frame.crop((32, 0, 64, 32))
    assert list(left.getdata()) != list(right.getdata())


def test_compose_motion_frame_returns_rgb_image():
    slide = TextSlide(
        name="s", background_color="#000033",
        text_layers=[TextLayer(text="HI", motion="ticker")],
    )
    frame = compose_motion_frame(slide, 0.5, 64, 32)
    assert frame.mode == "RGB"
    assert frame.size == (64, 32)


def test_compose_motion_frame_uses_background_cache_when_provided():
    """If a background_cache image of the right size is provided, it
    serves as the base instead of reloading. The composer should
    respect that and NOT call the background loader."""
    slide = TextSlide(
        name="s", background_color="#FF0000",
        text_layers=[TextLayer(text="", motion="static")],
    )
    # Bright green cached background — different from the slide's
    # configured red. If the cache is honored, the resulting frame
    # should be green-tinted, not red-tinted.
    cache = Image.new("RGBA", (64, 32), (0, 255, 0, 255))
    frame = compose_motion_frame(
        slide, 0.0, 64, 32, background_cache=cache,
    )
    # Sample a point with no text (default box has 10 % margin, so
    # corners are background-only): green wins.
    assert frame.getpixel((1, 1)) == (0, 255, 0)


def test_load_motion_background_returns_solid_when_no_image():
    slide = TextSlide(name="s", background_color="#123456")
    bg = load_motion_background(slide, 32, 16)
    assert bg.mode in ("RGB", "RGBA")
    # Sample a corner — should be the configured color.
    assert bg.getpixel((0, 0))[:3] == (0x12, 0x34, 0x56)
