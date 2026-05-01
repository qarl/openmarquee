from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from openmarquee.content import ContentItem, ImageSlide, TextBox, TextLayer, TextSlide, VideoSlide


def _make_slide(*, name="x", text="x", **layer_kwargs):
    """Helper: construct a single-layer TextSlide for tests that read/write
    one layer's fields. Accepts the old flat kwargs (text=, font_family=,
    auto_mode= …) and routes them into text_layers[0]; slide-level kwargs
    (name=, duration_ms=, transition=, …) stay at the root."""
    slide_kwargs = {}
    for k in ("duration_ms", "transition", "transition_ms",
              "background_color", "background_image_slide_id",
              "background_video_slide_id"):
        if k in layer_kwargs:
            slide_kwargs[k] = layer_kwargs.pop(k)
    return TextSlide(
        name=name,
        text_layers=[TextLayer(text=text, **layer_kwargs)],
        **slide_kwargs,
    )


def test_text_slide_auto_mode_defaults_to_none():
    assert _make_slide().text_layers[0].auto_mode is None


def test_v3_1_per_layer_extensions_default_when_omitted():
    """§5.10a v3.1 (qarl 2026-05-01, accordion-editor handoff): the
    new per-layer fields (name/weight/outline/opacity/anchor/visible/
    locked/motion/blend) must populate to defaults on a TextLayer
    constructed without them — old envelopes load cleanly, no
    SCHEMA_VERSION bump needed."""
    layer = TextLayer(text="x")
    assert layer.name == ""
    assert layer.weight is None
    assert layer.outline is False
    assert layer.opacity == 1.0
    assert layer.anchor == "center"
    assert layer.visible is True
    assert layer.locked is False
    assert layer.motion == "static"
    assert layer.blend == "normal"


def test_text_slide_accepts_auto_mode_options():
    for mode in ("time", "date", "day"):
        slide = _make_slide(auto_mode=mode)
        assert slide.text_layers[0].auto_mode == mode


def test_text_slide_rejects_unknown_auto_mode():
    with pytest.raises(ValidationError):
        TextLayer(text="x", auto_mode="weather")  # type: ignore[arg-type]


def test_text_slide_minimal_construction():
    slide = _make_slide(name="Today's Special", text="Pulled Pork $8.99")
    assert slide.type == "text_slide"
    assert slide.name == "Today's Special"
    assert slide.text_layers[0].text == "Pulled Pork $8.99"
    assert slide.duration_ms == 5000
    assert slide.text_layers[0].text_color == "#FFFFFF"
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
        TextLayer(text="x", font_size_px=2)  # below 4


def test_text_color_must_be_hex():
    with pytest.raises(ValidationError):
        TextLayer(text="x", text_color="red")


def test_text_color_normalizes_to_uppercase():
    """Hex colors should canonicalize to uppercase regardless of input case,
    so `#ffaa00` and `#FFAA00` dedupe as the same value."""
    layer_lower = TextLayer(text="x", text_color="#ffaa00")
    layer_mixed = TextLayer(text="x", text_color="#FfAa00")
    assert layer_lower.text_color == "#FFAA00"
    assert layer_mixed.text_color == "#FFAA00"


def test_background_color_normalizes_to_uppercase():
    slide = TextSlide(name="x", text="x", background_color="#abcdef")
    assert slide.background_color == "#ABCDEF"


def test_font_size_upper_bound_enforced():
    with pytest.raises(ValidationError):
        TextLayer(text="x", font_size_px=4096)


def test_name_length_capped():
    with pytest.raises(ValidationError):
        TextSlide(name="x" * 201, text="x")


def test_text_length_capped():
    with pytest.raises(ValidationError):
        TextLayer(text="x" * 10_001)


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


def test_text_slide_background_video_id_round_trips(tmp_path):
    """Phase 5b: TextSlide carries an optional reference to a VideoSlide
    whose frames the device composites text over at playback time. Field
    is None on a default slide and round-trips cleanly when set."""
    from uuid import uuid4

    bg_video_id = uuid4()
    slide = TextSlide(
        name="Happy Hour",
        text="4PM-6PM",
        background_video_slide_id=bg_video_id,
    )
    assert slide.background_video_slide_id == bg_video_id
    # Default-no-bg-video slide stays None, not missing.
    plain = TextSlide(name="x", text="x")
    assert plain.background_video_slide_id is None

    # JSON round-trip preserves the id.
    restored = TextSlide.model_validate_json(slide.model_dump_json())
    assert restored.background_video_slide_id == bg_video_id


def test_text_slide_rejects_both_image_and_video_backgrounds():
    """Phase 5b: a slide can have one background source (color, image,
    or video) — not two layered references at once. The editor's
    bg-picker is a radio so a malformed payload would have to come
    from a hand-rolled API client; reject at the model boundary."""
    from uuid import uuid4

    with pytest.raises(ValidationError, match="image and a video"):
        TextSlide(
            name="x",
            text="x",
            background_image_slide_id=uuid4(),
            background_video_slide_id=uuid4(),
        )


# --- TextBox (SYSTEM_SPEC §5.10a) -------------------------------------


def test_text_box_default_is_centered_with_10pct_margin_all_sides():
    """qarl 2026-04-30 revision: default {0.1, 0.1, 0.8, 0.8} so the
    centered box has 10% margin on ALL four sides (was {0.1, 0.1, 0.9,
    0.9} which had x+w=1 and y+h=1, no right/bottom margin)."""
    box = TextBox()
    assert (box.x, box.y, box.w, box.h) == (0.1, 0.1, 0.8, 0.8)


def test_text_slide_default_layer_box_matches():
    """A freshly-created TextSlide carries one default-box layer without
    the operator having to construct one explicitly."""
    slide = TextSlide(name="x")
    assert len(slide.text_layers) == 1
    box = slide.text_layers[0].box
    assert (box.x, box.y, box.w, box.h) == (0.1, 0.1, 0.8, 0.8)


def test_text_box_rejects_w_below_min():
    with pytest.raises(ValidationError):
        TextBox(w=0.05)


def test_text_box_rejects_h_above_max():
    with pytest.raises(ValidationError):
        TextBox(h=0.95)


def test_text_box_rejects_x_negative():
    with pytest.raises(ValidationError):
        TextBox(x=-0.1)


def test_text_box_rejects_extending_past_right_edge():
    """w=0.9 + x=0.5 → 1.4, off the slide on the right."""
    with pytest.raises(ValidationError, match="right edge"):
        TextBox(x=0.5, w=0.9)


def test_text_box_rejects_extending_past_bottom_edge():
    with pytest.raises(ValidationError, match="bottom edge"):
        TextBox(y=0.5, h=0.9)


def test_text_box_accepts_edge_aligned_box():
    """x=0.1, w=0.9 sums to exactly 1.0 — should NOT trip the
    'past the right edge' check."""
    box = TextBox(x=0.1, y=0.1, w=0.9, h=0.9)
    assert box.x == 0.1


def test_text_box_round_trips_through_json():
    box = TextBox(x=0.2, y=0.3, w=0.5, h=0.4)
    raw = box.model_dump_json()
    restored = TextBox.model_validate_json(raw)
    assert restored == box


def test_text_slide_with_custom_layer_box_round_trips():
    slide = TextSlide(
        name="x",
        text_layers=[
            TextLayer(text="x", box=TextBox(x=0.2, y=0.2, w=0.5, h=0.5)),
        ],
    )
    raw = slide.model_dump_json()
    restored = TextSlide.model_validate_json(raw)
    assert restored.text_layers[0].box == slide.text_layers[0].box


def test_text_slide_multi_layer_round_trips_in_order():
    """Layers preserve array order through the JSON round-trip — index 0
    is the bottom layer at render time."""
    slide = TextSlide(
        name="stacked",
        text_layers=[
            TextLayer(text="bottom", text_color="#FF0000"),
            TextLayer(text="middle", text_color="#00FF00"),
            TextLayer(text="top", text_color="#0000FF"),
        ],
    )
    raw = slide.model_dump_json()
    restored = TextSlide.model_validate_json(raw)
    assert [layer.text for layer in restored.text_layers] == [
        "bottom",
        "middle",
        "top",
    ]
    assert [layer.text_color for layer in restored.text_layers] == [
        "#FF0000",
        "#00FF00",
        "#0000FF",
    ]
