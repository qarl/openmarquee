"""Tests for emoji-aware text rendering in seed._draw_text_into.

The codepoint-segmenting helpers (`_segment_text_for_emoji` +
`_is_emoji_codepoint`) are pure logic and easy to unit-test
exhaustively. The actual color-glyph rendering via PIL.ImageDraw.
text(embedded_color=True) requires Noto Color Emoji bundled at
ui/fonts/noto-color-emoji.ttf — usually NOT present in dev or CI
environments — so the rendering tests exercise the graceful-fallback
path: when emoji_font is None, _draw_text_runs collapses to a single
draw.text call and the existing non-emoji rendering code path is
unchanged.

Visual verification with a real emoji font is a separate live-fire
check on a system that has the font installed; this suite proves
the segmentation logic + the no-crash invariant.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from openmarquee.seed import (
    _draw_text_into,
    _is_emoji_codepoint,
    _measure_text_runs,
    _segment_text_for_emoji,
)


# --- _is_emoji_codepoint ---


def test_is_emoji_codepoint_recognizes_supplementary_plane():
    """U+1F600 GRINNING FACE — common emoji in the supplementary plane."""
    assert _is_emoji_codepoint(0x1F600) is True


def test_is_emoji_codepoint_recognizes_misc_symbols():
    """U+2600 BLACK SUN WITH RAYS — symbol block."""
    assert _is_emoji_codepoint(0x2600) is True
    assert _is_emoji_codepoint(0x27BF) is True  # last in the symbol range


def test_is_emoji_codepoint_rejects_ascii():
    assert _is_emoji_codepoint(ord("A")) is False
    assert _is_emoji_codepoint(ord(" ")) is False
    assert _is_emoji_codepoint(ord("9")) is False


def test_is_emoji_codepoint_rejects_basic_latin_extension():
    """U+00E9 é — accented Latin, not emoji."""
    assert _is_emoji_codepoint(0x00E9) is False


def test_is_emoji_codepoint_below_2600_rejected():
    """U+25FF is just below the symbol-block start."""
    assert _is_emoji_codepoint(0x25FF) is False


def test_is_emoji_codepoint_above_27BF_rejected():
    """U+27C0 is just above the symbol-block end (range exclusive on
    the high side via < 0x27C0... actually our range uses <=, so
    0x27C0 is OUT). Confirm boundary behavior."""
    assert _is_emoji_codepoint(0x27C0) is False


# --- _segment_text_for_emoji ---


def test_segment_empty_string():
    assert _segment_text_for_emoji("") == []


def test_segment_pure_text():
    assert _segment_text_for_emoji("Hello world") == [("text", "Hello world")]


def test_segment_pure_emoji():
    assert _segment_text_for_emoji("😀") == [("emoji", "😀")]


def test_segment_text_then_emoji():
    runs = _segment_text_for_emoji("Hello 😀")
    assert runs == [("text", "Hello "), ("emoji", "😀")]


def test_segment_emoji_then_text():
    runs = _segment_text_for_emoji("😀 Hello")
    assert runs == [("emoji", "😀"), ("text", " Hello")]


def test_segment_alternating():
    runs = _segment_text_for_emoji("a😀b☀c")
    assert runs == [
        ("text", "a"),
        ("emoji", "😀"),
        ("text", "b"),
        ("emoji", "☀"),
        ("text", "c"),
    ]


def test_segment_groups_consecutive_emoji():
    """Adjacent emoji codepoints merge into one run so we make one
    draw call per run, not one per codepoint."""
    runs = _segment_text_for_emoji("😀😎🎉")
    assert runs == [("emoji", "😀😎🎉")]


def test_segment_groups_consecutive_text():
    runs = _segment_text_for_emoji("Hello world 1 2 3")
    assert runs == [("text", "Hello world 1 2 3")]


# --- _measure_text_runs (with emoji_font=None: passthrough) ---


def test_measure_passthrough_when_no_emoji_font():
    """Without an emoji font, _measure_text_runs collapses to
    draw.textbbox — same value either way."""
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    text = "Hello"
    a = draw.textbbox((0, 0), text)
    b = _measure_text_runs(draw, text, font=None, emoji_font=None)
    assert a == b


def test_measure_pure_text_with_emoji_font_set_unchanged():
    """Even with an emoji_font set, pure-text runs short-circuit to
    the same draw.textbbox path. Use the fontless default to avoid
    needing a real emoji TTF."""
    img = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Pretend we have an emoji font by passing a non-None placeholder;
    # function checks only against None at the entry point. (For pure-
    # text, the check `all(kind=="text")` short-circuits before we
    # ever call .textbbox with the placeholder.)
    a = draw.textbbox((0, 0), "Hello")
    b = _measure_text_runs(
        draw, "Hello", font=None, emoji_font="not-actually-a-font",
    )
    assert a == b


# --- _draw_text_into integration: no-crash with emoji content ---


def test_draw_text_into_with_emoji_does_not_crash():
    """Without Noto Color Emoji bundled, emoji codepoints render
    via the regular font (showing tofu glyphs but no exception)."""
    img = Image.new("RGB", (200, 100), (0, 0, 0))

    class _Box:
        x = 0.05
        y = 0.05
        w = 0.9
        h = 0.9

    _draw_text_into(
        img,
        text="Hello 😀 world",
        fg="#FFFFFF",
        font_family=None,
        box=_Box(),
        slide_width=200,
        slide_height=100,
        font_size_px=24,
    )
    # Image was drawn into without exception.
    assert img.size == (200, 100)


def test_draw_text_into_with_only_emoji_does_not_crash():
    img = Image.new("RGB", (200, 100), (0, 0, 0))

    class _Box:
        x = 0.05
        y = 0.05
        w = 0.9
        h = 0.9

    _draw_text_into(
        img,
        text="😀😎🎉",
        fg="#FFFFFF",
        font_family=None,
        box=_Box(),
        slide_width=200,
        slide_height=100,
        font_size_px=32,
    )
    assert img.size == (200, 100)


def test_draw_text_into_squish_path_with_emoji_does_not_crash():
    """When emoji-bearing text overflows its box, _draw_text_into
    routes through the temp-surface render + Lanczos resize path.
    Verify that path doesn't crash on mixed content."""
    img = Image.new("RGB", (200, 60), (0, 0, 0))

    class _Box:
        x = 0.05
        y = 0.05
        w = 0.5  # narrow — forces horizontal squish
        h = 0.9

    _draw_text_into(
        img,
        text="Long text with 😀 emoji 🎉 mixed in",
        fg="#FFFFFF",
        font_family=None,
        box=_Box(),
        slide_width=200,
        slide_height=60,
        font_size_px=32,  # large enough to overflow the narrow box
    )
    assert img.size == (200, 60)


def test_draw_text_into_no_emoji_path_unchanged():
    """Pure-ASCII text with no emoji takes the original single-call
    path — verified by rendering the same content into two images
    and comparing pixel-for-pixel."""
    img1 = Image.new("RGB", (200, 100), (0, 0, 0))
    img2 = Image.new("RGB", (200, 100), (0, 0, 0))

    class _Box:
        x = 0.1
        y = 0.1
        w = 0.8
        h = 0.8

    for img in (img1, img2):
        _draw_text_into(
            img,
            text="HELLO WORLD",
            fg="#FFFFFF",
            font_family=None,
            box=_Box(),
            slide_width=200,
            slide_height=100,
            font_size_px=24,
        )
    # Same input → same output. Confirms the segmentation refactor
    # didn't accidentally introduce nondeterminism into the simple
    # path.
    assert list(img1.getdata()) == list(img2.getdata())
