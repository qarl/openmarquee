"""Tests for openmarquee.rendering.blend.composite_with_blend."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from openmarquee.rendering.blend import composite_with_blend


def _solid(color: tuple[int, int, int, int], size: tuple[int, int] = (4, 4)) -> Image.Image:
    img = Image.new("RGBA", size, color)
    return img


def _pixel(img: Image.Image, x: int = 0, y: int = 0) -> tuple[int, int, int, int]:
    return tuple(img.getpixel((x, y)))


def test_normal_blend_matches_alpha_composite():
    base = _solid((255, 0, 0, 255))
    top = _solid((0, 0, 255, 128))
    result = composite_with_blend(base, top, mode="normal")
    expected = base.copy()
    expected.alpha_composite(top)
    assert _pixel(result) == _pixel(expected)


def test_unknown_mode_degrades_to_normal():
    base = _solid((255, 0, 0, 255))
    top = _solid((0, 0, 255, 128))
    fallback = composite_with_blend(base, top, mode="not-a-real-mode")
    expected = base.copy()
    expected.alpha_composite(top)
    assert _pixel(fallback) == _pixel(expected)


def test_multiply_at_full_alpha_is_per_channel_product():
    # multiply: r = base * top / 255 per channel.
    # base = mid-gray (128); top = mid-gray (128); result ~= 64.
    base = _solid((128, 128, 128, 255))
    top = _solid((128, 128, 128, 255))
    result = composite_with_blend(base, top, mode="multiply")
    r, g, b, a = _pixel(result)
    # 0.502 * 0.502 = 0.252 -> 64 after rounding.
    assert r == g == b
    assert 60 <= r <= 68, f"expected ~64, got {r}"
    assert a == 255


def test_multiply_with_white_top_is_identity():
    # White top in multiply: result == base.
    base = _solid((100, 200, 50, 255))
    top = _solid((255, 255, 255, 255))
    result = composite_with_blend(base, top, mode="multiply")
    r, g, b, _ = _pixel(result)
    assert (r, g, b) == (100, 200, 50)


def test_multiply_with_black_top_is_black():
    base = _solid((100, 200, 50, 255))
    top = _solid((0, 0, 0, 255))
    result = composite_with_blend(base, top, mode="multiply")
    r, g, b, _ = _pixel(result)
    assert (r, g, b) == (0, 0, 0)


def test_screen_with_black_top_is_identity():
    # Screen: r = 1 - (1-base)*(1-top); top=0 -> result = base.
    base = _solid((100, 200, 50, 255))
    top = _solid((0, 0, 0, 255))
    result = composite_with_blend(base, top, mode="screen")
    r, g, b, _ = _pixel(result)
    assert (r, g, b) == (100, 200, 50)


def test_screen_with_white_top_is_white():
    base = _solid((100, 200, 50, 255))
    top = _solid((255, 255, 255, 255))
    result = composite_with_blend(base, top, mode="screen")
    r, g, b, _ = _pixel(result)
    assert (r, g, b) == (255, 255, 255)


def test_overlay_dark_base_uses_multiply():
    # base = 0.25 (dark), top = 0.5: overlay = 2 * 0.25 * 0.5 = 0.25.
    base = _solid((64, 64, 64, 255))  # 0.251
    top = _solid((128, 128, 128, 255))  # 0.502
    result = composite_with_blend(base, top, mode="overlay")
    r, g, b, _ = _pixel(result)
    # 2 * 0.251 * 0.502 ~= 0.252 -> ~64
    assert 60 <= r <= 68


def test_overlay_light_base_uses_screen():
    # base = 0.75 (light), top = 0.5:
    # overlay = 1 - 2 * (1-0.75) * (1-0.5) = 1 - 2 * 0.25 * 0.5 = 0.75.
    base = _solid((192, 192, 192, 255))  # 0.753
    top = _solid((128, 128, 128, 255))  # 0.502
    result = composite_with_blend(base, top, mode="overlay")
    r, g, b, _ = _pixel(result)
    assert 188 <= r <= 196


def test_transparent_top_does_not_modify_base_at_any_mode():
    base = _solid((100, 200, 50, 255))
    top = _solid((0, 255, 0, 0))  # alpha=0 -> top contributes nothing
    for mode in ("normal", "multiply", "screen", "overlay"):
        result = composite_with_blend(base, top, mode=mode)
        r, g, b, a = _pixel(result)
        assert (r, g, b) == (100, 200, 50), f"{mode} altered base under transparent top"
        assert a == 255


def test_partial_alpha_top_blends_proportionally():
    # multiply with white-top at alpha=0.5 should land roughly halfway
    # between base and base*top=base (since white*base=base for multiply).
    # i.e. essentially identity. With BLACK-top at alpha=0.5, result is
    # halfway between base and 0 = base/2.
    base = _solid((200, 200, 200, 255))
    top = _solid((0, 0, 0, 128))  # alpha=0.5
    result = composite_with_blend(base, top, mode="multiply")
    r, g, b, _ = _pixel(result)
    # base * (1 - 0.5) + 0 * 0.5 = 100. Allow 1px of rounding.
    assert 95 <= r <= 105, f"expected ~100, got {r}"


def test_size_mismatch_raises():
    base = _solid((255, 0, 0, 255), (4, 4))
    top = _solid((0, 0, 255, 255), (8, 8))
    with pytest.raises(ValueError, match="same size"):
        composite_with_blend(base, top, mode="multiply")


def test_non_rgba_input_raises():
    base = Image.new("RGB", (4, 4), (255, 0, 0))  # not RGBA
    top = _solid((0, 0, 255, 255))
    with pytest.raises(ValueError, match="RGBA"):
        composite_with_blend(base, top, mode="multiply")
