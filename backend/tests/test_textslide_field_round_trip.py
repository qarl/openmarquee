"""Generic regression test: every TextSlide-only field round-trips
through TextSlideUpload.

This test class is the "fifth-bite preventer." Three times in 36
hours we shipped a new TextSlide field (motion_intensity, motion_
phase, then background_gradient, then background_pattern) and forgot
to mirror it on TextSlideUpload — Pydantic's `extra="ignore"` (the
default) silently drops unknown keys on the way IN, so PUT/POST
round-tripped them as null on the way OUT.

The pattern is structural: any new field on TextSlide that isn't
also on TextSlideUpload will slip through the existing tests because
those tests construct TextSlide directly and bypass the wire layer.

This test walks `TextSlide.model_fields` and asserts every field has
a matching slot on `TextSlideUpload`. When the next field is added
to TextSlide and forgotten on the upload model, this test fails
loudly — the actual round-trip behavior of the new field can stay
in its own feature-specific test suite, but the silent-drop is
caught here.
"""

from __future__ import annotations

from openmarquee.api import TextSlideUpload
from openmarquee.content import TextSlide

# Fields on TextSlide that intentionally do NOT appear on the wire
# model. These are server-assigned (id, created_at, updated_at) or
# discriminator (`type`). Adding to this set requires a comment
# justifying why the field is server-only.
_TEXTSLIDE_FIELDS_NOT_ON_UPLOAD = frozenset(
    {
        # Discriminator — implicit on the upload route.
        "type",
        # Server-assigned at storage time.
        "id",
        "created_at",
        "updated_at",
    }
)


def test_every_textslide_field_round_trips_through_upload():
    """Walk TextSlide.model_fields and assert every non-server-only
    field has a corresponding slot on TextSlideUpload. Any field
    added to TextSlide without a mirror on TextSlideUpload trips
    this test before it ships."""
    slide_fields = set(TextSlide.model_fields.keys())
    upload_fields = set(TextSlideUpload.model_fields.keys())
    missing_on_upload = slide_fields - upload_fields - _TEXTSLIDE_FIELDS_NOT_ON_UPLOAD
    assert not missing_on_upload, (
        f"TextSlide fields missing on TextSlideUpload: {sorted(missing_on_upload)}. "
        f"Adding a field to TextSlide without mirroring it on the wire "
        f"model causes Pydantic to silently drop the field on POST/PUT, "
        f"which has bitten this codebase 4 times in 36 hours. Either "
        f"add the field to TextSlideUpload (the usual fix) or add it to "
        f"_TEXTSLIDE_FIELDS_NOT_ON_UPLOAD with a comment explaining why."
    )


def test_upload_only_fields_are_explicitly_documented():
    """Inverse direction: TextSlideUpload has fields TextSlide
    doesn't (png_base64). That's expected — the upload model adds
    transport-only fields. This test pins the set so a stray new
    field on TextSlideUpload that should ALSO be on TextSlide gets
    flagged."""
    slide_fields = set(TextSlide.model_fields.keys())
    upload_fields = set(TextSlideUpload.model_fields.keys())
    upload_only = upload_fields - slide_fields
    expected_upload_only = {"png_base64"}
    assert upload_only == expected_upload_only, (
        f"TextSlideUpload has unexpected upload-only fields: "
        f"{sorted(upload_only - expected_upload_only)}. If this is a new "
        f"transport-only field, add it to expected_upload_only here. If "
        f"it should be on TextSlide too, add it to TextSlide and remove "
        f"from this set."
    )


def test_text_layer_outline_and_drop_shadow_default_false():
    """r51: both text-effect bools default to False so legacy slides
    (and unspecified-field uploads) get the conservative "effects off"
    behavior. Operators opt in via the editor toggles."""
    from openmarquee.content import TextLayer

    layer = TextLayer(text="hello")
    assert layer.outline is False
    assert layer.drop_shadow is False


def test_text_layer_drop_shadow_round_trips_through_dump_and_validate():
    """r51: drop_shadow survives a model_dump → model_validate cycle
    in both states. This is the wire-level round trip the renderer +
    on-disk envelope rely on."""
    from openmarquee.content import TextLayer

    for value in (True, False):
        layer = TextLayer(text="hi", drop_shadow=value)
        dumped = layer.model_dump(mode="json")
        assert dumped["drop_shadow"] is value
        rebuilt = TextLayer.model_validate(dumped)
        assert rebuilt.drop_shadow is value
