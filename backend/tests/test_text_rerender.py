"""Unit tests for openmarquee.text_rerender."""

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from openmarquee.content import ImageSlide, TextLayer, TextSlide, VideoSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.seed import render_text_slide_png
from openmarquee.text_rerender import (
    effective_dims,
    rerender_text_slides_for_dims,
)


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


def _make_text_slide(
    storage: ContentStorage, *, width: int, height: int, text: str = "Hello"
) -> TextSlide:
    png = render_text_slide_png(text, width, height, fg="#FFFFFF", bg="#000000")
    slide = TextSlide(
        name=text,
        background_color="#000000",
        duration_ms=3000,
        text_layers=[
            TextLayer(
                text=text,
                text_color="#FFFFFF",
                font_size_px=int(height * 0.4),
            ),
        ],
    )
    storage.save_text_slide(slide, png)
    return slide


def _png_dims(png: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(png)).size


def test_effective_dims_swaps_at_90_and_270():
    assert effective_dims(0, 1920, 1080) == (1920, 1080)
    assert effective_dims(90, 1920, 1080) == (1080, 1920)
    assert effective_dims(180, 1920, 1080) == (1920, 1080)
    assert effective_dims(270, 1920, 1080) == (1080, 1920)


def test_rerender_resizes_every_text_slide_to_new_dims(storage: ContentStorage):
    a = _make_text_slide(storage, width=1920, height=1080, text="A")
    b = _make_text_slide(storage, width=1920, height=1080, text="B")

    count = rerender_text_slides_for_dims(storage, rotation=90, width=1920, height=1080)
    assert count == 2
    assert _png_dims(storage.read_asset(a.id)) == (1080, 1920)
    assert _png_dims(storage.read_asset(b.id)) == (1080, 1920)


def test_rerender_skips_text_with_video_bg(storage: ContentStorage):
    """text+video-bg slides are composited live at playback; their stored
    PNG is a static thumbnail. Re-rendering would lose the video frame
    overlay — operator's editor save handles this on next edit."""
    # Need a VideoSlide first to reference. VideoSlide expects an MP4 +
    # thumbnail; we don't actually play it, just need the id.
    fake_mp4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 120
    video = VideoSlide(name="bg", duration_ms=5000)
    thumbnail_png = render_text_slide_png(
        " ", 1920, 1080, fg="#FFFFFF", bg="#000000"
    )
    storage.save_video(video, thumbnail_png, fake_mp4)

    text_with_video_bg = TextSlide(
        name="over-video",
        background_color="#000000",
        background_video_slide_id=video.id,
        duration_ms=3000,
        text_layers=[
            TextLayer(
                text="Over video",
                text_color="#FFFFFF",
                font_size_px=400,
            ),
        ],
    )
    original_png = render_text_slide_png(
        text_with_video_bg.text_layers[0].text,
        1920,
        1080,
        fg="#FFFFFF",
        bg="#000000",
    )
    storage.save_text_slide(text_with_video_bg, original_png)

    count = rerender_text_slides_for_dims(
        storage, rotation=0, width=128, height=64
    )
    # Only the video thumb's storage write happens here, NOT a text-slide
    # rerender. The text-with-video-bg slide is skipped.
    assert count == 0
    # And its asset.png is unchanged (still landscape 1920×1080).
    assert _png_dims(storage.read_asset(text_with_video_bg.id)) == (1920, 1080)


def test_rerender_preserves_image_bg_composite(storage: ContentStorage):
    """A text+image-bg slide should re-render with the image background
    re-composited under the text at the new dims."""
    # Seed an image slide as the bg.
    bg_png = render_text_slide_png(" ", 1920, 1080, fg="#FFFFFF", bg="#FF0000")
    bg = ImageSlide(name="red bg", duration_ms=5000)
    storage.save_image(bg, bg_png)

    text_over_bg = TextSlide(
        name="over-image",
        background_color="#000000",
        background_image_slide_id=bg.id,
        duration_ms=3000,
        text_layers=[
            TextLayer(
                text="OVER",
                text_color="#FFFFFF",
                font_size_px=int(1080 * 0.4),
            ),
        ],
    )
    composed = render_text_slide_png(
        text_over_bg.text_layers[0].text,
        1920,
        1080,
        fg="#FFFFFF",
        bg="#000000",
        background_image_path=storage.asset_path(bg.id),
    )
    storage.save_text_slide(text_over_bg, composed)

    count = rerender_text_slides_for_dims(
        storage, rotation=0, width=128, height=64
    )
    assert count == 1
    # New PNG is at the smaller dims.
    assert _png_dims(storage.read_asset(text_over_bg.id)) == (128, 64)


def test_rerender_returns_zero_on_empty_store(storage: ContentStorage):
    assert rerender_text_slides_for_dims(storage, rotation=0, width=64, height=32) == 0


def test_rerender_threads_box_through(storage: ContentStorage):
    """qarl 2026-04-30 §5.10a: the rerender side-effect must use each
    slide's box when re-rendering. With a half-width box, the resulting
    PNG has text-bearing pixels concentrated in the half the box covers,
    not spread across the slide."""
    from openmarquee.content import TextBox
    from openmarquee.content import TextSlide

    slide = TextSlide(
        name="X",
        background_color="#000000",
        duration_ms=3000,
        text_layers=[
            TextLayer(
                text="X",
                text_color="#FFFFFF",
                font_size_px=200,
                box=TextBox(x=0.5, y=0.1, w=0.4, h=0.4),
            ),
        ],
    )
    layer = slide.text_layers[0]
    initial_png = render_text_slide_png(
        layer.text,
        100,
        100,
        fg=layer.text_color,
        bg=slide.background_color,
        box=layer.box,
    )
    storage.save_text_slide(slide, initial_png)

    count = rerender_text_slides_for_dims(
        storage, rotation=0, width=200, height=200
    )
    assert count == 1
    rendered = Image.open(BytesIO(storage.read_asset(slide.id))).convert("RGB")
    assert rendered.size == (200, 200)
    pixels = rendered.load()
    top_right = 0
    rest = 0
    for y in range(200):
        for x in range(200):
            if pixels[x, y] == (0, 0, 0):
                continue
            if x >= 100 and y < 100:
                top_right += 1
            else:
                rest += 1
    assert top_right > rest, (
        f"rerender ignored box: top_right={top_right} rest={rest}"
    )


def test_rerender_triggers_horizontal_squish_when_text_overflows_new_width(
    storage: ContentStorage,
):
    """Locking in the squish-path coverage at the rerender layer (not just
    the seed-time render). A 1920×1080 slide with text that fits at the
    landscape font size should re-render at 64×32 with the same text
    horizontally compressed into the smaller canvas."""
    long_text = "GRAND OPENING THIS SATURDAY"
    # Render fresh at 1920×1080 (fits comfortably).
    slide = _make_text_slide(storage, width=1920, height=1080, text=long_text)
    assert _png_dims(storage.read_asset(slide.id)) == (1920, 1080)

    # Squeeze to 64×32 — the natural width at height*0.4 font is way over
    # canvas width, so the squish branch in render_text_slide_png runs.
    count = rerender_text_slides_for_dims(storage, rotation=0, width=64, height=32)
    assert count == 1
    new_png = storage.read_asset(slide.id)
    assert _png_dims(new_png) == (64, 32)
    # Smoke-check: PNG actually has rendered text (not just background).
    img = Image.open(BytesIO(new_png)).convert("RGB")
    assert len(set(img.getdata())) > 1
