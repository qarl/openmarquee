from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from openmarquee.content import ContentItem, ImageSlide, TextSlide, VideoSlide


def test_text_slide_minimal_construction():
    slide = TextSlide(name="Today's Special", text="Pulled Pork $8.99")
    assert slide.type == "text_slide"
    assert slide.name == "Today's Special"
    assert slide.text == "Pulled Pork $8.99"
    assert slide.duration_ms == 5000
    assert slide.text_color == "#FFFFFF"
    assert slide.background_color == "#000000"


def test_text_slide_generates_unique_ids():
    a = TextSlide(name="a", text="a")
    b = TextSlide(name="b", text="b")
    assert isinstance(a.id, UUID)
    assert isinstance(b.id, UUID)
    assert a.id != b.id


def test_created_at_is_utc_aware():
    slide = TextSlide(name="x", text="x")
    assert isinstance(slide.created_at, datetime)
    assert slide.created_at.tzinfo == UTC


def test_duration_minimum_enforced():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", duration_ms=50)  # below 100


def test_duration_accepts_minimum():
    slide = TextSlide(name="x", text="x", duration_ms=100)
    assert slide.duration_ms == 100


def test_font_size_minimum_enforced():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", font_size_px=2)  # below 4


def test_text_color_must_be_hex():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", text_color="red")


def test_text_color_normalizes_to_uppercase():
    """Hex colors should canonicalize to uppercase regardless of input case,
    so `#ffaa00` and `#FFAA00` dedupe as the same value."""
    slide_lower = TextSlide(name="x", text="x", text_color="#ffaa00")
    slide_mixed = TextSlide(name="x", text="x", text_color="#FfAa00")
    assert slide_lower.text_color == "#FFAA00"
    assert slide_mixed.text_color == "#FFAA00"


def test_background_color_normalizes_to_uppercase():
    slide = TextSlide(name="x", text="x", background_color="#abcdef")
    assert slide.background_color == "#ABCDEF"


def test_font_size_upper_bound_enforced():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", font_size_px=4096)


def test_name_length_capped():
    with pytest.raises(ValidationError):
        TextSlide(name="x" * 201, text="x")


def test_text_length_capped():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x" * 10_001)


def test_type_literal_rejects_other_strings():
    with pytest.raises(ValidationError):
        TextSlide(type="image", name="x", text="x")


# --- ImageSlide ---


def test_image_slide_minimal_construction():
    item = ImageSlide(name="Logo")
    assert item.type == "image"
    assert item.name == "Logo"
    assert item.duration_ms == 5000
    assert isinstance(item.id, UUID)
    assert isinstance(item.created_at, datetime)
    assert item.created_at.tzinfo == UTC


def test_image_slide_duration_floor_enforced():
    with pytest.raises(ValidationError):
        ImageSlide(name="x", duration_ms=50)


def test_image_slide_name_capped():
    with pytest.raises(ValidationError):
        ImageSlide(name="x" * 201)


# --- Transition fields (text + image) ---


def test_text_slide_default_transition_is_cut():
    slide = TextSlide(name="x", text="x")
    assert slide.transition == "cut"
    assert slide.transition_ms == 500


def test_text_slide_accepts_fade():
    slide = TextSlide(name="x", text="x", transition="fade", transition_ms=300)
    assert slide.transition == "fade"
    assert slide.transition_ms == 300


def test_text_slide_rejects_unknown_transition():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", transition="zoom")


def test_text_slide_rejects_negative_transition_ms():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", transition_ms=-1)


def test_text_slide_rejects_excessive_transition_ms():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", transition_ms=10_000)


def test_image_slide_supports_transition():
    img = ImageSlide(name="x", transition="fade", transition_ms=250)
    assert img.transition == "fade"
    assert img.transition_ms == 250


# --- Discriminated union dispatch ---


def test_content_item_union_routes_text_slide_on_deserialize():
    adapter = TypeAdapter(ContentItem)
    payload = {"type": "text_slide", "name": "x", "text": "x"}
    item = adapter.validate_python(payload)
    assert isinstance(item, TextSlide)


def test_content_item_union_routes_image_on_deserialize():
    adapter = TypeAdapter(ContentItem)
    payload = {"type": "image", "name": "Logo"}
    item = adapter.validate_python(payload)
    assert isinstance(item, ImageSlide)


def test_video_slide_minimal_construction():
    video = VideoSlide(name="Promo")
    assert video.type == "video"
    assert video.name == "Promo"
    assert video.pipeline == "h264_mp4"


def test_video_slide_accepts_raw_frames_pipeline():
    """Reopened in the ffmpeg.wasm spike commit: the browser can now
    extract concatenated RGB888 frames via /spike.html. Production-grade
    wiring of raw_frames into the upload path follows."""
    video = VideoSlide(name="Panel Promo", pipeline="raw_frames")
    assert video.pipeline == "raw_frames"


def test_video_slide_rejects_unknown_pipeline():
    with pytest.raises(ValidationError):
        VideoSlide(name="x", pipeline="vp9")  # type: ignore[arg-type]


def test_content_item_union_routes_video_on_deserialize():
    adapter = TypeAdapter(ContentItem)
    item = adapter.validate_python({"type": "video", "name": "Promo"})
    assert isinstance(item, VideoSlide)


def test_content_item_union_rejects_unknown_type():
    adapter = TypeAdapter(ContentItem)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "nope_not_a_real_type", "name": "x"})


def test_background_color_must_be_hex():
    with pytest.raises(ValidationError):
        TextSlide(name="x", text="x", background_color="not-a-color")


def test_round_trip_through_json():
    """Serializing to JSON and back preserves all fields incl. the type discriminator."""
    original = TextSlide(
        name="Sale",
        text="50% OFF",
        duration_ms=3000,
        font_family="Helvetica",
        font_size_px=72,
        text_color="#FFFFFF",
        background_color="#FF0000",
    )
    payload = original.model_dump_json()
    restored = TextSlide.model_validate_json(payload)
    assert restored == original
    assert restored.type == "text_slide"
