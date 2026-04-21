from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from openmarquee.content import TextSlide


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
