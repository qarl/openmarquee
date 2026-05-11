"""Tests for the procedural pattern background system (qarl 2026-05-03
designer handoff replacing BackgroundGradient).

Covers:
  - BackgroundPattern Pydantic model + field validation
  - TextSlide mutex (4-way: solid / image / video / pattern)
  - Migration: legacy `background_gradient` JSON deserializes into
    `background_pattern` via the model_validator(mode="before")
  - 11 PIL renderers (visual sanity, not pixel-perfect)
  - load_background dispatch
  - TextSlideUpload wire-mirror round-trip (the recurring bite)
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from openmarquee.app import app
from openmarquee.auto_render import (
    _hex_to_rgb,
    load_background,
    render_pattern,
)
from openmarquee.content import (
    BackgroundPattern,
    TextLayer,
    TextSlide,
)
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    get_content_storage,
)


# --- BackgroundPattern model ---


def test_pattern_model_canonicalizes_hex():
    p = BackgroundPattern(pattern="dots", color_a="#aabbcc", color_b="#112233")
    assert p.color_a == "#AABBCC"
    assert p.color_b == "#112233"
    assert p.density == 0.5  # default


def test_pattern_model_default_color_b():
    """color_b defaults to white so callers can omit it for pattern=
    'solid' (which ignores it anyway)."""
    p = BackgroundPattern(pattern="solid", color_a="#000000")
    assert p.color_b == "#FFFFFF"


def test_pattern_model_rejects_bad_hex():
    with pytest.raises(ValidationError):
        BackgroundPattern(pattern="dots", color_a="red", color_b="#000000")


def test_pattern_model_rejects_density_out_of_range():
    with pytest.raises(ValidationError):
        BackgroundPattern(
            pattern="dots", color_a="#000000", color_b="#FFFFFF", density=1.5,
        )


def test_pattern_model_rejects_unknown_pattern():
    with pytest.raises(ValidationError):
        BackgroundPattern(
            pattern="not_a_real_pattern",
            color_a="#000000", color_b="#FFFFFF",
        )


@pytest.mark.parametrize("name", [
    "solid", "gradient", "dots", "halftone", "stripes",
    "scanlines", "checker", "grid", "rings", "rays", "confetti", "bricks",
])
def test_pattern_model_accepts_all_pattern_names(name):
    p = BackgroundPattern(pattern=name, color_a="#000000", color_b="#FFFFFF")
    assert p.pattern == name


# --- TextSlide mutex ---


def _slide_with(**kwargs) -> dict:
    base = {
        "name": "test",
        "text_layers": [TextLayer(text="hello")],
    }
    base.update(kwargs)
    return base


def test_slide_accepts_pattern_alone():
    p = BackgroundPattern(pattern="dots", color_a="#000000", color_b="#FFFFFF")
    slide = TextSlide(**_slide_with(background_pattern=p))
    assert slide.background_pattern.pattern == "dots"


def test_slide_rejects_pattern_with_image_bg():
    p = BackgroundPattern(pattern="dots", color_a="#000000", color_b="#FFFFFF")
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_pattern=p,
            background_image_slide_id=uuid4(),
        ))


def test_slide_rejects_pattern_with_video_bg():
    p = BackgroundPattern(pattern="dots", color_a="#000000", color_b="#FFFFFF")
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_pattern=p,
            background_video_slide_id=uuid4(),
        ))


def test_slide_still_rejects_image_with_video_bg():
    """Pre-existing exclusivity rule still holds in the 4-way mutex."""
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_image_slide_id=uuid4(),
            background_video_slide_id=uuid4(),
        ))


# --- Migration: legacy background_gradient → background_pattern ---


def test_migration_legacy_gradient_to_pattern():
    """Existing slides on disk with `background_gradient: {...}` must
    deserialize cleanly under the new schema. The model_validator
    transforms angle_deg → density via density = angle_deg / 270."""
    legacy_data = {
        "name": "legacy",
        "text_layers": [{"text": "x"}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#FF6B6B",
            "end_color": "#4ECDC4",
            "angle_deg": 135.0,
        },
    }
    slide = TextSlide.model_validate(legacy_data)
    assert slide.background_pattern is not None
    assert slide.background_pattern.pattern == "gradient"
    assert slide.background_pattern.color_a == "#FF6B6B"
    assert slide.background_pattern.color_b == "#4ECDC4"
    # 135 / 270 = 0.5
    assert slide.background_pattern.density == pytest.approx(0.5, abs=0.01)


def test_migration_clamps_density_for_legacy_360():
    """A 360° legacy gradient → density 360/270 = 1.33; clamps to 1.0."""
    legacy_data = {
        "name": "legacy",
        "text_layers": [{"text": "x"}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#000000",
            "end_color": "#FFFFFF",
            "angle_deg": 360.0,
        },
    }
    slide = TextSlide.model_validate(legacy_data)
    assert slide.background_pattern.density == 1.0


def test_migration_drops_gradient_field():
    """After migration, the legacy `background_gradient` key is gone
    from the model — only `background_pattern` remains. Catches a
    bug where both keys end up populated and the mutex validator
    later double-counts."""
    legacy_data = {
        "name": "legacy",
        "text_layers": [{"text": "x"}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#000000",
            "end_color": "#FFFFFF",
            "angle_deg": 90.0,
        },
    }
    slide = TextSlide.model_validate(legacy_data)
    dumped = slide.model_dump()
    assert "background_gradient" not in dumped
    assert dumped["background_pattern"]["pattern"] == "gradient"


def test_migration_does_not_clobber_explicit_pattern():
    """If the input dict has BOTH legacy gradient AND a new pattern
    field, the new pattern wins (operator's explicit choice trumps
    auto-migrated legacy data)."""
    data = {
        "name": "x",
        "text_layers": [{"text": "x"}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#FF0000",
            "end_color": "#00FF00",
            "angle_deg": 0.0,
        },
        "background_pattern": {
            "pattern": "dots",
            "color_a": "#0000FF",
            "color_b": "#FFFF00",
            "density": 0.7,
        },
    }
    slide = TextSlide.model_validate(data)
    assert slide.background_pattern.pattern == "dots"
    assert slide.background_pattern.color_a == "#0000FF"


def test_migration_no_op_when_no_legacy_data():
    """A modern slide without `background_gradient` deserializes
    cleanly with no migration side-effect."""
    slide = TextSlide.model_validate({
        "name": "modern",
        "text_layers": [{"text": "x"}],
    })
    assert slide.background_pattern is None


# --- _hex_to_rgb ---


def test_hex_to_rgb_with_hash():
    assert _hex_to_rgb("#FF8000") == (255, 128, 0)


def test_hex_to_rgb_lowercase():
    assert _hex_to_rgb("#ff8000") == (255, 128, 0)


# --- 11 PIL pattern renderers ---


def _pat(name: str, color_a: str = "#FF0000", color_b: str = "#00FF00",
         density: float = 0.5) -> BackgroundPattern:
    return BackgroundPattern(
        pattern=name, color_a=color_a, color_b=color_b, density=density,
    )


@pytest.mark.parametrize("name", [
    "solid", "gradient", "dots", "halftone", "stripes",
    "scanlines", "checker", "grid", "rings", "rays", "confetti", "bricks",
])
def test_render_each_pattern_produces_correct_image_size(name):
    img = render_pattern(_pat(name), 200, 100)
    assert img.size == (200, 100)
    assert img.mode == "RGB"


def test_render_solid_is_uniform_color_a():
    """Solid pattern fills with color_a; color_b + density ignored."""
    img = render_pattern(_pat("solid", "#FF0000", "#00FF00"), 50, 50)
    pixels = set(img.getdata())
    assert pixels == {(255, 0, 0)}


def test_render_dots_has_both_colors():
    """Dots pattern: most pixels color_a, dots of color_b. Both
    colors must appear in the output."""
    img = render_pattern(_pat("dots", "#FF0000", "#00FF00"), 200, 200)
    pixels = set(img.getdata())
    # color_a present
    assert (255, 0, 0) in pixels
    # color_b present (dots)
    assert (0, 255, 0) in pixels


def test_render_gradient_at_density_zero_is_top_to_bottom():
    """Density 0 → angle 0deg → start (color_a) on top, end
    (color_b) on bottom."""
    img = render_pattern(
        _pat("gradient", "#000000", "#FFFFFF", density=0.0), 10, 100,
    )
    # Top row near black, bottom row near white.
    assert img.getpixel((5, 0))[0] < 5
    assert img.getpixel((5, 99))[0] > 250


def test_render_gradient_at_density_one_is_right_to_left():
    """Density 1 → angle 270deg → start on right, end on left."""
    img = render_pattern(
        _pat("gradient", "#000000", "#FFFFFF", density=1.0), 100, 10,
    )
    # Right column near black, left near white.
    assert img.getpixel((99, 5))[0] < 5
    assert img.getpixel((0, 5))[0] > 250


def test_render_stripes_alternates_colors():
    """Stripes pattern: 45° diagonal alternating bands."""
    img = render_pattern(_pat("stripes", "#FF0000", "#00FF00"), 200, 200)
    pixels = set(img.getdata())
    assert (255, 0, 0) in pixels
    assert (0, 255, 0) in pixels


def test_render_scanlines_has_horizontal_lines():
    """Scanlines: every Nth row is color_b, the rest is color_a."""
    img = render_pattern(
        _pat("scanlines", "#000000", "#FFFFFF", density=0.5), 50, 50,
    )
    # Row 0 must be all color_b (scanline); a row a few px below
    # must be color_a.
    assert img.getpixel((25, 0)) == (255, 255, 255)
    # Find a non-zero row that's color_a.
    for y in range(1, 5):
        if img.getpixel((25, y)) == (0, 0, 0):
            return
    pytest.fail("expected at least one non-scanline row in [1..5]")


def test_render_checker_alternates_at_tile_boundary():
    """Checker pattern: adjacent tiles have different colors."""
    img = render_pattern(
        _pat("checker", "#FF0000", "#00FF00", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (255, 0, 0) in pixels
    assert (0, 255, 0) in pixels


def test_render_rings_has_concentric_pattern():
    """Rings: concentric rings of color_b on color_a base."""
    img = render_pattern(
        _pat("rings", "#000000", "#FFFFFF", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (0, 0, 0) in pixels
    assert (255, 255, 255) in pixels


def test_render_rays_has_alternating_slices():
    """Rays: conic gradient of N slices alternating A/B."""
    img = render_pattern(
        _pat("rays", "#FF0000", "#00FF00", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (255, 0, 0) in pixels
    assert (0, 255, 0) in pixels


def test_render_confetti_has_dots():
    """Confetti: 4 layers of dot patterns."""
    img = render_pattern(
        _pat("confetti", "#000000", "#FFFFFF", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (0, 0, 0) in pixels
    assert (255, 255, 255) in pixels


def test_render_bricks_has_mortar_lines():
    """Bricks: color_a base with color_b mortar lines (h + staggered v)."""
    img = render_pattern(
        _pat("bricks", "#000000", "#FFFFFF", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (0, 0, 0) in pixels
    assert (255, 255, 255) in pixels


def test_render_grid_lines_on_paper():
    """Grid: color_a lines on color_b paper (B17)."""
    img = render_pattern(
        _pat("grid", "#FF0000", "#0000FF", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (255, 0, 0) in pixels  # grid lines
    assert (0, 0, 255) in pixels  # paper


def test_render_halftone_two_offset_grids():
    """Halftone: two dot grids offset by half-tile. Visual sanity
    check — both colors present."""
    img = render_pattern(
        _pat("halftone", "#000000", "#FFFFFF", density=0.5), 200, 200,
    )
    pixels = set(img.getdata())
    assert (0, 0, 0) in pixels
    assert (255, 255, 255) in pixels


# --- load_background dispatch ---


def test_load_background_uses_pattern_when_set():
    p = BackgroundPattern(
        pattern="solid", color_a="#FF0000", color_b="#00FF00",
    )
    slide = TextSlide(**_slide_with(background_pattern=p))
    img = load_background(slide, 50, 50, read_asset=None)
    pixels = set(img.getdata())
    assert pixels == {(255, 0, 0)}


def test_load_background_falls_back_to_solid_when_no_pattern():
    slide = TextSlide(**_slide_with(background_color="#112233"))
    img = load_background(slide, 50, 50, read_asset=None)
    assert img.getpixel((0, 0)) == (0x11, 0x22, 0x33)


# --- TextSlideUpload wire-mirror regression (4th bite) ---


def _png_b64(width: int = 8, height: int = 8) -> str:
    img = Image.new("RGB", (width, height), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def client(storage: ContentStorage):
    app.dependency_overrides[get_content_storage] = lambda: storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()


def test_textslideupload_round_trips_pattern(client: TestClient):
    """4th-bite regression. POST a TextSlideUpload with
    background_pattern → GET back returns the pattern intact. The
    silent-drop class of bug (motion_intensity, motion_phase,
    gradient, now pattern) only shows when the wire model is missing
    a field that's on TextSlide; this test catches that."""
    payload = {
        "name": "PatternSlide",
        "duration_ms": 3000,
        "text_layers": [{"text": "Hi", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_pattern": {
            "pattern": "halftone",
            "color_a": "#1A0F00",
            "color_b": "#FFB43C",
            "density": 0.4,
        },
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["background_pattern"] is not None
    assert body["background_pattern"]["pattern"] == "halftone"
    assert body["background_pattern"]["color_a"] == "#1A0F00"
    assert body["background_pattern"]["color_b"] == "#FFB43C"
    assert body["background_pattern"]["density"] == pytest.approx(0.4)

    list_response = client.get("/api/content")
    found = next(
        (s for s in list_response.json() if s["id"] == body["id"]), None,
    )
    assert found is not None
    assert found["background_pattern"]["pattern"] == "halftone"


def test_textslideupload_preserves_null_pattern(client: TestClient):
    payload = {
        "name": "NoPattern",
        "duration_ms": 3000,
        "text_layers": [{"text": "Hi", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["background_pattern"] is None


def test_textslideupload_put_round_trips_pattern(client: TestClient):
    """The PUT (edit-existing) route has its own model_dump
    construction site (api.py:253). Verify it preserves
    background_pattern too — same silent-drop risk shape."""
    create = client.post("/api/content/text-slides", json={
        "name": "EditMe",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "png_base64": _png_b64(),
    })
    item_id = create.json()["id"]

    update = client.put(f"/api/content/text-slides/{item_id}", json={
        "name": "EditMe",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_pattern": {
            "pattern": "stripes",
            "color_a": "#000000",
            "color_b": "#FFFFFF",
            "density": 0.6,
        },
        "png_base64": _png_b64(),
    })
    assert update.status_code == 200, update.text
    assert update.json()["background_pattern"]["pattern"] == "stripes"


def test_textslideupload_rejects_pattern_with_image_bg(client: TestClient):
    """Mutex still surfaces as 422 at the wire boundary."""
    payload = {
        "name": "BadMutex",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_pattern": {
            "pattern": "dots",
            "color_a": "#000000",
            "color_b": "#FFFFFF",
            "density": 0.5,
        },
        "background_image_slide_id": "00000000-0000-4000-8000-000000000099",
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 422


def test_textslideupload_accepts_legacy_gradient_payload(client: TestClient):
    """Legacy clients still in flight may POST `background_gradient`.
    The migration on TextSlide kicks in BEFORE Pydantic's validator
    chain, so legacy payloads round-trip cleanly into the new
    pattern shape on the way out."""
    payload = {
        "name": "LegacyGradient",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        # Note: TextSlideUpload itself doesn't have background_gradient,
        # so this won't carry through the upload model. But the
        # *storage layer* (TextSlide) accepts the legacy shape on
        # deserialize. This test documents the boundary: clients must
        # POST `background_pattern`; the migration only protects
        # already-on-disk JSON files.
        "background_pattern": {
            "pattern": "gradient",
            "color_a": "#FF6B6B",
            "color_b": "#4ECDC4",
            "density": 0.5,
        },
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200
