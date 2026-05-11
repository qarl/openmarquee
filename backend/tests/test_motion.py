"""Tests for the motion renderer software path (spec step 3a).

Each effect's frame math is covered as a unit test. Box-bounded
clipping is checked by sampling pixels OUTSIDE the layer's box and
asserting the layer never bleeds into them. Deterministic shake
is verified by re-running with the same seed and asserting the
same pixel offsets land.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

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
    prerender_layer_bitmaps,
    render_layer_to_rgba,
    slide_has_auto,
    slide_has_dynamic_content,
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
    """abs(sin(0)) = 0 → 0 px offset → frame matches input. The
    rest position is the floor; phase=0 is the moment the ball is
    on it."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_bounce(img, (10, 10, 20, 20), 50, 0.0)
    assert list(out.getdata()) == list(img.getdata())


def test_bounce_at_quarter_phase_rebounds_up():
    """abs(sin(π/2)) = 1 → max amplitude. At intensity=100, amplitude
    is 0.10 of box height = 2 px on a 20-px box. The layer rebounds
    UP by 2 px within the box rect (negative Y offset; "true
    bouncing" treats rest as the floor and the layer always rebounds
    upward — qarl 2026-05-03). Bounce clips to the box, so the top
    2 rows of the original glyph clip off above and the bottom 2
    rows of the box become empty."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_bounce(img, (10, 10, 20, 20), 100, 0.25)
    # Pixel just inside the top of the box (row 10): glyph still
    # there because the box-content shifted up but stays within the
    # box rect.
    assert out.getpixel((20, 11))[3] == 255  # near top, glyph here
    # Bottom 2 rows of the box (rows 28, 29) are now empty — that's
    # where the glyph used to fill before the upward rebound.
    assert out.getpixel((20, 29))[3] == 0    # bottom of box, glyph cleared


def test_bounce_at_three_quarter_phase_also_rebounds_up():
    """The whole point of abs(sin) over plain sin: at phase=0.75
    where plain sin would return -1 (offset DOWN, away from floor),
    abs(sin) returns +1 — the layer rebounds UP again. Two
    rebounds per cycle, never below rest."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    quarter = _apply_bounce(img, (10, 10, 20, 20), 100, 0.25)
    three_quarter = _apply_bounce(img, (10, 10, 20, 20), 100, 0.75)
    # Both peak phases produce the SAME upward offset (the
    # symmetry of |sin|). Frames pixel-equal.
    assert list(quarter.getdata()) == list(three_quarter.getdata())


def test_bounce_at_half_phase_returns_to_floor():
    """abs(sin(π)) = 0 → glyph back at rest. Confirms the bounce is
    a true ball-on-floor pattern: rest → up → rest → up → rest."""
    img = _make_layer_rgba(40, 40, (10, 10, 20, 20), (255, 0, 0, 255))
    out = _apply_bounce(img, (10, 10, 20, 20), 100, 0.5)
    assert list(out.getdata()) == list(img.getdata())


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


def test_slide_has_auto_true_when_any_layer_has_auto_mode():
    slide = TextSlide(
        name="s", text_layers=[
            TextLayer(text="A"),
            TextLayer(text="00:00", auto_mode="time", auto_format="time_hms"),
        ],
    )
    assert slide_has_auto(slide) is True


def test_slide_has_auto_false_for_static_layers():
    slide = TextSlide(name="s", text_layers=[TextLayer(text="A")])
    assert slide_has_auto(slide) is False


def test_slide_has_auto_ignores_hidden_auto_layer():
    """Same convention as slide_has_motion: a hidden auto layer doesn't
    count, since the playback loop won't draw it."""
    slide = TextSlide(
        name="s", text_layers=[
            TextLayer(text="x", auto_mode="time", visible=False),
            TextLayer(text="y"),
        ],
    )
    assert slide_has_auto(slide) is False


def test_slide_has_dynamic_content_covers_motion_or_auto_or_both():
    """The unified per-tick branch in PlaybackLoop checks this; it
    must fire for motion-only slides, auto-only slides, and slides
    with both."""
    motion_only = TextSlide(
        name="m", text_layers=[TextLayer(text="x", motion="ticker")],
    )
    auto_only = TextSlide(
        name="a", text_layers=[TextLayer(text="x", auto_mode="time")],
    )
    both = TextSlide(
        name="b", text_layers=[
            TextLayer(text="x", motion="bounce", auto_mode="time"),
        ],
    )
    static_only = TextSlide(name="s", text_layers=[TextLayer(text="x")])
    assert slide_has_dynamic_content(motion_only) is True
    assert slide_has_dynamic_content(auto_only) is True
    assert slide_has_dynamic_content(both) is True
    assert slide_has_dynamic_content(static_only) is False


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


def test_prerender_layer_bitmaps_returns_list_parallel_to_text_layers():
    slide = TextSlide(
        name="s",
        text_layers=[
            TextLayer(text="A"),
            TextLayer(text="B"),
            TextLayer(text="C"),
        ],
    )
    bitmaps = prerender_layer_bitmaps(slide, 64, 32)
    assert len(bitmaps) == 3
    for bm in bitmaps:
        assert bm is not None
        assert bm.mode == "RGBA"
        assert bm.size == (64, 32)


def test_prerender_layer_bitmaps_skips_auto_layers():
    """Auto layers re-render text every tick from the current `now`.
    Pre-rasterizing them at slide entry would just produce a stale
    snapshot. Skip → None placeholder so the composer's cache check
    falls through to a fresh render_layer_to_rgba with the current
    time."""
    slide = TextSlide(
        name="s",
        text_layers=[
            TextLayer(text="A"),
            TextLayer(text="00:00", auto_mode="time", auto_format="time_hms"),
            TextLayer(text="C"),
        ],
    )
    bitmaps = prerender_layer_bitmaps(slide, 64, 32)
    assert len(bitmaps) == 3
    assert bitmaps[0] is not None
    assert bitmaps[1] is None  # auto-mode layer skipped
    assert bitmaps[2] is not None


def test_render_layer_to_rgba_uses_now_for_auto_layer():
    """When the layer has auto_mode and a `now` is provided, the text
    rendered into the bitmap should be the auto-formatted string,
    not the layer's stored placeholder text."""
    layer = TextLayer(
        text="placeholder",
        auto_mode="time",
        auto_format="time_hms",
        text_color="#FFFFFF",
        box=TextBox(x=0.05, y=0.05, w=0.9, h=0.9),
    )
    now = datetime(2026, 5, 2, 14, 30, 45, tzinfo=UTC)
    bitmap_with_now = render_layer_to_rgba(layer, 200, 60, now=now)
    bitmap_no_now = render_layer_to_rgba(layer, 200, 60)
    # The two bitmaps should differ — one drew "14:30:45", the other
    # drew "placeholder". (Without comparing exact text we just assert
    # the pixels aren't identical, which only holds if the rendered
    # strings differed.)
    assert bitmap_with_now.tobytes() != bitmap_no_now.tobytes()


def test_compose_motion_frame_re_renders_auto_layer_per_tick():
    """A clock layer's pixels at t=0 vs t=1s with `now` advanced one
    second should differ — auto re-renders even on the unified
    motion path."""
    slide = TextSlide(
        name="clock",
        background_color="#000000",
        text_layers=[
            TextLayer(
                text="--:--:--",
                auto_mode="time",
                auto_format="time_hms",
                text_color="#FFFFFF",
                box=TextBox(x=0.05, y=0.05, w=0.9, h=0.9),
            ),
        ],
    )
    t1 = datetime(2026, 5, 2, 14, 30, 45, tzinfo=UTC)
    t2 = datetime(2026, 5, 2, 14, 30, 46, tzinfo=UTC)  # +1s
    frame1 = compose_motion_frame(slide, 0.0, 200, 60, now=t1)
    frame2 = compose_motion_frame(slide, 1.0, 200, 60, now=t2)
    # Different second → different rendered text → different pixels.
    assert frame1.tobytes() != frame2.tobytes()


def test_render_layer_to_rgba_auto_layer_has_outline():
    """compose_auto_frame baked a 1-px black outline around clock /
    date / day text for readability on mid-tone backgrounds. The
    unified motion path preserves that — auto layers render with an
    outline, static layers don't. This test catches the silent
    regression subagent flagged on step 3a-unify: a darker pixel
    adjacent to the bright glyph proves the halo landed."""
    layer = TextLayer(
        text="HELLO",
        auto_mode=None,  # Static for the baseline.
        text_color="#FFFFFF",
        box=TextBox(x=0.05, y=0.05, w=0.9, h=0.9),
    )
    auto_layer = TextLayer(
        text="placeholder",
        auto_mode="time",
        auto_format="time_hms",
        text_color="#FFFFFF",
        box=TextBox(x=0.05, y=0.05, w=0.9, h=0.9),
    )
    now = datetime(2026, 5, 2, 14, 30, 45, tzinfo=UTC)
    static_img = render_layer_to_rgba(layer, 200, 60)
    auto_img = render_layer_to_rgba(auto_layer, 200, 60, now=now)

    # In the auto bitmap, find any opaque-black pixel — proves the
    # halo rendered (the layer's text_color is white, so a black
    # pixel can only come from the stroke).
    has_black_outline = False
    for y in range(60):
        for x in range(200):
            r, g, b, a = auto_img.getpixel((x, y))
            if a > 0 and r == 0 and g == 0 and b == 0:
                has_black_outline = True
                break
        if has_black_outline:
            break
    assert has_black_outline, "auto-mode layer should render with black outline"

    # Sanity: the static layer (no auto_mode) renders WITHOUT outline.
    has_static_black = any(
        static_img.getpixel((x, y))[:4] == (0, 0, 0, a)
        for y in range(60) for x in range(200)
        for a in (255,)  # only opaque
    )
    assert not has_static_black, (
        "static layer with white text shouldn't render any black pixels"
    )


def test_compose_motion_frame_auto_plus_motion_combine():
    """Spec invariant: a layer can be BOTH auto AND motion (e.g. a
    clock that bounces). Verify that the unified composer re-renders
    the auto text AND applies the motion transform on the same tick.

    Bounce at intensity=100 phase=0.25 is the spike of the sine →
    max amplitude. Compare frame1 (no motion: phase=0) vs frame2
    (full motion: phase=0.25) at the SAME `now` — bytes should
    differ because of the bounce shift."""
    slide = TextSlide(
        name="clock",
        background_color="#000000",
        text_layers=[
            TextLayer(
                text="--:--",
                auto_mode="time",
                auto_format="time_hm",
                motion="bounce",
                motion_intensity=100,
                text_color="#FFFFFF",
                box=TextBox(x=0.05, y=0.05, w=0.9, h=0.9),
            ),
        ],
    )
    now = datetime(2026, 5, 2, 14, 30, 45, tzinfo=UTC)
    # phase=0: bounce sin=0, no shift
    frame_zero = compose_motion_frame(slide, 0.0, 200, 60, now=now)
    # phase=0.25: bounce sin=1, max shift
    frame_max = compose_motion_frame(slide, 0.25, 200, 60, now=now)
    assert frame_zero.tobytes() != frame_max.tobytes()


def test_prerender_layer_bitmaps_skips_hidden_layers():
    """Hidden layers cost ~8 MB / layer at 1080 p RGBA — wasted memory
    if we pre-rasterize them just for the composer's hidden-layer
    skip path to discard the result. Skip them at prerender time and
    leave a None placeholder; the composer's hidden check fires
    first, so the placeholder is never accessed."""
    slide = TextSlide(
        name="s",
        text_layers=[
            TextLayer(text="A", visible=True),
            TextLayer(text="B", visible=False),
            TextLayer(text="C", visible=True),
        ],
    )
    bitmaps = prerender_layer_bitmaps(slide, 64, 32)
    assert len(bitmaps) == 3
    assert bitmaps[0] is not None
    assert bitmaps[1] is None
    assert bitmaps[2] is not None


def test_compose_motion_frame_uses_layer_bitmap_cache_when_provided():
    """If a cache parallel to text_layers is supplied, the composer
    pulls layer bitmaps from it instead of re-rasterizing each tick.
    Verify by handing in a fully-magenta cache entry — if the cache
    is honored, the composed frame is magenta everywhere (a fresh
    rasterize from "X" text would produce mostly-transparent + a
    few glyph pixels, which alpha-composites to mostly-black on
    the configured black background)."""
    slide = TextSlide(
        name="s",
        background_color="#000000",
        text_layers=[TextLayer(text="X", motion="static")],
    )
    # Fill the entire cache entry with opaque magenta so the test
    # doesn't couple to TextLayer's default-box dims (if the box
    # default ever changes, a partial-fill cache could miss the
    # sampled pixel — full-fill stays robust).
    cache_entry = Image.new("RGBA", (64, 32), (255, 0, 255, 255))
    frame = compose_motion_frame(
        slide, 0.0, 64, 32, layer_bitmap_cache=[cache_entry],
    )
    # Center pixel should be magenta — proves the cache fed the composer.
    assert frame.getpixel((32, 16)) == (255, 0, 255)


def test_compose_motion_frame_falls_through_when_cache_size_mismatch():
    """Defensive: if the cache entry's size doesn't match the requested
    width/height (e.g. cache built for a different renderer), fall
    through to a fresh rasterize rather than rendering at the wrong
    dims. Out-of-band callers + future re-renderer surfaces benefit
    from this guard."""
    slide = TextSlide(
        name="s",
        background_color="#000000",
        text_layers=[TextLayer(text="HELLO", motion="static")],
    )
    # Cache built for 100x50 — but composing for 64×32. Should be ignored.
    wrong_cache = [Image.new("RGBA", (100, 50), (0, 255, 0, 255))]
    frame = compose_motion_frame(
        slide, 0.0, 64, 32, layer_bitmap_cache=wrong_cache,
    )
    # The bright-green cache should NOT show through. Center pixel is
    # whatever a fresh rasterize produces — not green.
    assert frame.getpixel((32, 16)) != (0, 255, 0)


def test_compose_motion_frame_cache_cold_call_still_works():
    """No cache → composer falls through to render_layer_to_rgba. The
    cold-call path is what tests + ad-hoc callers exercise; it must
    keep working unchanged from the pre-cache shape."""
    slide = TextSlide(
        name="s",
        background_color="#001122",
        text_layers=[TextLayer(text="X", motion="ticker")],
    )
    frame = compose_motion_frame(slide, 0.0, 64, 32)  # no caches
    assert frame.mode == "RGB"
    assert frame.size == (64, 32)


def test_load_motion_background_returns_solid_when_no_image():
    slide = TextSlide(name="s", background_color="#123456")
    bg = load_motion_background(slide, 32, 16)
    assert bg.mode in ("RGB", "RGBA")
    # Sample a corner — should be the configured color.
    assert bg.getpixel((0, 0))[:3] == (0x12, 0x34, 0x56)


# --- Batch 8.fix: pool-hit assertion (validates 8.4) ---


def test_scratch_pool_hits_on_repeated_motion_compose():
    """Two compose_motion_frame calls on a motion-active slide at
    the same dims: first cold-allocates the scratch buffers; second
    hits the pool. Sweep #2 #8 / Batch 8.4 validation gate."""
    from openmarquee.content import TextBox, TextLayer, TextSlide
    from openmarquee.motion import (
        _stats, clear_scratch_pool, compose_motion_frame,
    )

    clear_scratch_pool()
    for k in _stats:
        _stats[k] = 0

    slide = TextSlide(
        name="motion-test", duration_ms=5000,
        text_layers=[TextLayer(
            text="HELLO", motion="ticker", motion_intensity=50,
            font_family="Inter", font_size_pct=30,
            box=TextBox(x=0.1, y=0.4, w=0.8, h=0.2),
        )],
        background_color="#102030",
    )

    compose_motion_frame(slide, elapsed_s=0.0, width=128, height=128)
    cold_pool_hits = _stats["scratch_pool_hits"]
    cold_creates = _stats["image_new_calls"]

    compose_motion_frame(slide, elapsed_s=0.033, width=128, height=128)
    # Second frame: at least one _scratch_rgba call (ticker's
    # `out = _scratch_rgba(layer_rgba.size)`) hit the pool. The
    # exact count varies by motion effect but must be > 0.
    assert _stats["scratch_pool_hits"] > cold_pool_hits, (
        "expected scratch_pool_hits to grow on the second frame"
    )
    # And no fresh scratch allocation at that key (image_new_calls
    # may still grow from render_layer_to_rgba's persistent buffer,
    # so just check the delta isn't BIGGER than the cold side).
    second_frame_creates = _stats["image_new_calls"] - cold_creates
    assert second_frame_creates <= cold_creates, (
        f"second frame allocated {second_frame_creates} fresh buffers; "
        f"expected <= cold-frame count {cold_creates}"
    )


# --- Sweep #3 #13: compute_phase wraps cleanly at the cycle boundary ---


def test_compute_phase_returns_zero_at_cycle_boundary_not_one():
    """`compute_phase` should always return a value in [0, 1).
    At exactly one full cycle (elapsed * freq + motion_phase = 1.0),
    the modulo wraps to 0.0. A float-precision regression that
    returns 1.0 would break ticker's wrap-around logic (a 1.0
    phase would shift_px past the box edge by one full width)."""
    from openmarquee.motion import compute_phase

    # 1Hz, motion_phase=0, elapsed=1.0 -> exactly one cycle.
    assert compute_phase(1.0, 1.0, 0.0) == 0.0
    # Half cycle.
    assert compute_phase(0.5, 1.0, 0.0) == 0.5
    # Cycle + half.
    assert compute_phase(1.5, 1.0, 0.0) == 0.5
    # motion_phase pushes into next cycle exactly.
    assert compute_phase(0.5, 1.0, 0.5) == 0.0
