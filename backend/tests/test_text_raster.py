"""Tests for the extracted text_raster module: font fallback
chain, LRU cache behavior, measure_centered geometry."""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from openmarquee.text_raster import (
    BUNDLED_FONT_FILES,
    bundled_fonts_dir,
    clear_font_cache,
    font_cache_info,
    load_font,
    measure_centered,
)


@pytest.fixture(autouse=True)
def _isolate_font_cache():
    """Each test starts with a fresh cache so hit/miss counts are
    deterministic. The cache is module-level state -- without this
    a prior test's load_font leaks into the next test's
    expectations."""
    clear_font_cache()
    yield
    clear_font_cache()


def test_bundled_font_files_has_expected_entries():
    """Sanity: the 23 bundled @font-face families we ship to the
    browser editor are also resolvable server-side. If a new family
    is added to ui/fonts/ the operator picker exposes it but the
    backend will fall through to DejaVuSans -- visible quality
    regression."""
    assert "Inter" in BUNDLED_FONT_FILES
    assert "Bebas Neue" in BUNDLED_FONT_FILES
    assert len(BUNDLED_FONT_FILES) == 23


def test_bundled_fonts_dir_resolves_inside_repo():
    """ui/fonts/ alongside backend/. Resolves to a real directory
    in the dev tree so load_font's bundled-family path actually
    finds files."""
    d = bundled_fonts_dir()
    assert d.name == "fonts"
    assert d.parent.name == "ui"


def test_load_font_bundled_family_returns_truetype():
    """Bundled family -> path-based truetype load."""
    font = load_font("Inter", 32, 100)
    # truetype produces a FreeTypeFont; load_default returns
    # ImageFont. We don't pin the exact class but we do want a
    # font we can measure with.
    assert font is not None
    img = Image.new("RGB", (200, 50))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), "Hi", font=font)
    assert bbox[2] > bbox[0]  # non-empty width


def test_load_font_falls_back_to_dejavu_on_unknown_family():
    """Unknown family (not in BUNDLED_FONT_FILES, not a path) ->
    last-resort DejaVuSans path. Pillow ships DejaVuSans, so this
    is reliable on dev machines and CI."""
    font = load_font("ThisFontDoesNotExist", 24, 100)
    assert font is not None


def test_load_font_size_defaults_when_size_px_is_none():
    """size_px=None -> max(8, int(canvas_height * 0.4)). Verify
    the fallback runs without crashing and produces a sized
    font."""
    font = load_font("Inter", None, 400)
    # Expected size = max(8, 160) = 160.
    img = Image.new("RGB", (500, 500))
    draw = ImageDraw.Draw(img)
    bbox = draw.textbbox((0, 0), "M", font=font)
    # Approx: cap height at size 160 should be substantially
    # taller than at size 8.
    h = bbox[3] - bbox[1]
    assert h > 50  # plenty of slack vs the 160 default


def test_load_font_size_floor_when_canvas_tiny():
    """size_px=None + canvas_height=10 -> max(8, 4) = 8. Floor
    keeps fonts legible even on misconfigured surfaces."""
    font = load_font(None, None, 10)
    assert font is not None  # didn't crash on tiny size


def test_load_font_cache_hits_on_repeat_call():
    """The LRU cache is the whole point of the extraction --
    Pillow's ImageFont.truetype is the dominant cost on cold
    slides. Two calls with the same family+size should produce
    exactly 1 cache miss + 1 hit."""
    info_before = font_cache_info()
    assert info_before.hits == 0
    assert info_before.misses == 0

    load_font("Inter", 32, 100)
    info_after_first = font_cache_info()
    assert info_after_first.misses == 1
    assert info_after_first.hits == 0

    load_font("Inter", 32, 100)
    info_after_second = font_cache_info()
    assert info_after_second.misses == 1  # no new miss
    assert info_after_second.hits == 1


def test_load_font_cache_distinguishes_sizes():
    """Same family at different sizes -> separate cache entries
    (ImageFont locks size at load, so each size needs its own
    instance)."""
    load_font("Inter", 16, 100)
    load_font("Inter", 32, 100)
    info = font_cache_info()
    assert info.misses == 2
    assert info.hits == 0


def test_load_font_cache_returns_same_object_on_hit():
    """A cache hit gives back the SAME ImageFont instance --
    important because we'd otherwise burn time re-parsing the
    same .ttf for every auto-render tick."""
    f1 = load_font("Inter", 32, 100)
    f2 = load_font("Inter", 32, 100)
    assert f1 is f2


def test_clear_font_cache_resets_state():
    """clear_font_cache() drops all entries -- tests rely on this
    for isolation, but the same hook is useful for runtime resource
    pressure (e.g. font hot-swap)."""
    load_font("Inter", 32, 100)
    assert font_cache_info().misses == 1
    clear_font_cache()
    assert font_cache_info().misses == 0
    assert font_cache_info().hits == 0


def test_measure_centered_centers_text_in_canvas():
    """Geometric invariant: returned (x, y) places the text bbox
    in the middle of (width, height). The auto-render hot path
    relies on this -- clock readouts unaligned vertically would be
    visible regression."""
    img = Image.new("RGB", (400, 200))
    draw = ImageDraw.Draw(img)
    font = load_font("Inter", 24, 200)
    w, h, x, y = measure_centered(draw, "Hello", font, 400, 200)
    # Re-measure to verify x/y land the text such that its bbox
    # center is at the canvas center.
    bbox = draw.textbbox((x, y), "Hello", font=font)
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    assert abs(cx - 200) <= 1
    assert abs(cy - 100) <= 1
    assert w > 0
    assert h > 0


def test_measure_centered_empty_text_doesnt_crash():
    """Empty text -> w=0, h=0; centering math still well-defined."""
    img = Image.new("RGB", (400, 200))
    draw = ImageDraw.Draw(img)
    font = load_font("Inter", 24, 200)
    w, h, x, y = measure_centered(draw, "", font, 400, 200)
    assert w == 0
    # Don't pin h because Pillow returns ascent for empty strings
    # on some backends.
    assert isinstance(x, int)
    assert isinstance(y, int)
