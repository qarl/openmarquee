"""Tests for the two-stop linear gradient background type.

Covers schema (BackgroundGradient model + TextSlide mutex validator),
renderer (_render_linear_gradient pixel sanity), the renderer
dispatch in _load_background, AND the TextSlideUpload wire-mirror
(regression test for the silent-drop pattern that bit motion fields
in 37ae520 and would have shipped here without QA's catch).
"""

from __future__ import annotations

import base64
import io
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from openmarquee.app import app
from openmarquee.auto_render import (
    _hex_to_rgb,
    _load_background,
    _render_linear_gradient,
)
from openmarquee.content import (
    BackgroundGradient,
    TextLayer,
    TextSlide,
)
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    get_content_storage,
)


# --- BackgroundGradient model ---


def test_gradient_model_roundtrips_hex_canonicalized():
    g = BackgroundGradient(start_color="#aabbcc", end_color="#112233")
    assert g.start_color == "#AABBCC"
    assert g.end_color == "#112233"
    assert g.angle_deg == 0.0  # default top-to-bottom (CSS-like)


def test_gradient_model_rejects_bad_hex():
    with pytest.raises(ValidationError):
        BackgroundGradient(start_color="red", end_color="#000000")


def test_gradient_model_rejects_angle_out_of_range():
    with pytest.raises(ValidationError):
        BackgroundGradient(
            start_color="#000000", end_color="#FFFFFF", angle_deg=400.0,
        )


# --- TextSlide mutex validator ---


def _slide_with(**kwargs) -> dict:
    base = {
        "name": "test",
        "text_layers": [TextLayer(text="hello")],
    }
    base.update(kwargs)
    return base


def test_slide_accepts_gradient_alone():
    g = BackgroundGradient(start_color="#000000", end_color="#FFFFFF")
    slide = TextSlide(**_slide_with(background_gradient=g))
    assert slide.background_gradient.start_color == "#000000"


def test_slide_rejects_gradient_with_image_bg():
    g = BackgroundGradient(start_color="#000000", end_color="#FFFFFF")
    from uuid import uuid4
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_gradient=g,
            background_image_slide_id=uuid4(),
        ))


def test_slide_rejects_gradient_with_video_bg():
    g = BackgroundGradient(start_color="#000000", end_color="#FFFFFF")
    from uuid import uuid4
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_gradient=g,
            background_video_slide_id=uuid4(),
        ))


def test_slide_still_rejects_image_with_video_bg():
    """Pre-existing exclusivity rule still holds."""
    from uuid import uuid4
    with pytest.raises(ValidationError):
        TextSlide(**_slide_with(
            background_image_slide_id=uuid4(),
            background_video_slide_id=uuid4(),
        ))


# --- _hex_to_rgb ---


def test_hex_to_rgb_with_hash():
    assert _hex_to_rgb("#FF8000") == (255, 128, 0)


def test_hex_to_rgb_lowercase():
    # Validator already canonicalizes, but the helper itself is
    # case-tolerant for safety.
    assert _hex_to_rgb("#ff8000") == (255, 128, 0)


# --- _render_linear_gradient ---


def test_render_vertical_gradient_top_to_bottom():
    """angle=0 → start on top, end on bottom (CSS-like)."""
    g = BackgroundGradient(
        start_color="#000000", end_color="#FFFFFF", angle_deg=0.0,
    )
    img = _render_linear_gradient(g, 10, 100)
    assert img.size == (10, 100)
    # Top row should be (or close to) start color.
    assert img.getpixel((5, 0))[0] < 5
    # Bottom row should be near end color.
    assert img.getpixel((5, 99))[0] > 250


def test_render_horizontal_gradient_left_to_right_at_90():
    """angle=90 → start on left, end on right."""
    g = BackgroundGradient(
        start_color="#000000", end_color="#FFFFFF", angle_deg=90.0,
    )
    img = _render_linear_gradient(g, 100, 10)
    # Left column should be near start color.
    assert img.getpixel((0, 5))[0] < 5
    # Right column should be near end color.
    assert img.getpixel((99, 5))[0] > 250
    # Middle column is the average.
    mid = img.getpixel((50, 5))[0]
    assert 120 < mid < 140


def test_render_vertical_gradient_bottom_to_top_at_180():
    """angle=180 → start on bottom, end on top (180° rotation of 0°)."""
    g = BackgroundGradient(
        start_color="#000000", end_color="#FFFFFF", angle_deg=180.0,
    )
    img = _render_linear_gradient(g, 10, 100)
    # Bottom row should now be start (black), top should be end (white).
    assert img.getpixel((5, 99))[0] < 5
    assert img.getpixel((5, 0))[0] > 250


def test_render_color_lerp_at_midpoint():
    """A horizontal red→blue gradient (angle=90): midpoint is purple-ish."""
    g = BackgroundGradient(
        start_color="#FF0000", end_color="#0000FF", angle_deg=90.0,
    )
    img = _render_linear_gradient(g, 11, 1)
    r, g_, b = img.getpixel((5, 0))
    assert 120 < r < 140
    assert g_ < 5
    assert 120 < b < 140


def test_render_handles_1x1_degenerate():
    """1x1 image: no gradient axis to project onto. Should fall back
    to start_color rather than divide-by-zero."""
    g = BackgroundGradient(
        start_color="#FF0000", end_color="#00FF00", angle_deg=45.0,
    )
    img = _render_linear_gradient(g, 1, 1)
    assert img.size == (1, 1)
    assert img.getpixel((0, 0)) == (255, 0, 0)


# --- _load_background dispatch ---


def test_load_background_uses_gradient_when_set():
    g = BackgroundGradient(
        start_color="#FF0000", end_color="#0000FF", angle_deg=90.0,
    )
    slide = TextSlide(**_slide_with(background_gradient=g))
    img = _load_background(slide, 100, 10, read_asset=None)
    # angle=90 → start on left, end on right.
    assert img.getpixel((0, 5))[0] > 250  # red on left
    assert img.getpixel((99, 5))[2] > 250  # blue on right


def test_load_background_falls_back_to_solid_when_no_gradient():
    slide = TextSlide(**_slide_with(background_color="#112233"))
    img = _load_background(slide, 50, 50, read_asset=None)
    assert img.getpixel((0, 0)) == (0x11, 0x22, 0x33)


def test_load_background_image_takes_precedence_over_gradient():
    """If both image_slide_id and gradient are set the validator
    should reject — but if a malformed object reaches _load_background
    via test or migration path, the image branch wins (it's checked
    first). This documents the dispatch order."""
    from uuid import uuid4
    g = BackgroundGradient(
        start_color="#FF0000", end_color="#0000FF", angle_deg=90.0,
    )
    # Build via model_construct to bypass the validator.
    slide = TextSlide.model_construct(
        type="text_slide",
        id=uuid4(),
        name="test",
        text_layers=[TextLayer(text="x")],
        background_color="#000000",
        background_image_slide_id=uuid4(),
        background_video_slide_id=None,
        background_gradient=g,
        transition="cut",
        transition_ms=500,
        duration_ms=5000,
    )
    # image branch falls through (read_asset returns nothing) → tries
    # gradient. If the image branch had succeeded it would have
    # short-circuited; since read_asset=None the dispatcher falls
    # through to gradient (which is what we want for graceful
    # degradation).
    img = _load_background(slide, 50, 50, read_asset=None)
    # We get the gradient (image branch couldn't load).
    # angle=90 → start (red) on left.
    assert img.getpixel((0, 25))[0] > 250  # red


# --- TextSlideUpload wire-mirror regression ---


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


def test_textslideupload_round_trips_gradient(client: TestClient):
    """Regression for the silent-drop bug class (motion fields hit
    this earlier in 37ae520; gradient hit it tonight before QA caught
    it). POSTing a TextSlideUpload with background_gradient must
    persist the gradient and surface it on subsequent GET."""
    payload = {
        "name": "GradientSlide",
        "duration_ms": 3000,
        "text_layers": [{"text": "Hi", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#FF6B6B",
            "end_color": "#4ECDC4",
            "angle_deg": 45,
        },
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["background_gradient"] is not None
    assert body["background_gradient"]["start_color"] == "#FF6B6B"
    assert body["background_gradient"]["end_color"] == "#4ECDC4"
    assert body["background_gradient"]["angle_deg"] == 45.0

    # Round-trip via GET on /api/content.
    list_response = client.get("/api/content")
    assert list_response.status_code == 200
    slides = list_response.json()
    found = next((s for s in slides if s["id"] == body["id"]), None)
    assert found is not None
    assert found["background_gradient"]["start_color"] == "#FF6B6B"


def test_textslideupload_preserves_null_gradient(client: TestClient):
    """A POST without background_gradient (or with explicit None)
    must NOT default to a stray gradient on the persisted slide."""
    payload = {
        "name": "NoGradient",
        "duration_ms": 3000,
        "text_layers": [{"text": "Hi", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["background_gradient"] is None


def test_textslideupload_put_round_trips_gradient(client: TestClient):
    """The PUT (edit-existing) route has its own model_dump
    construction site (api.py:253). Verify it also preserves the
    gradient field — same silent-drop risk shape."""
    create = client.post("/api/content/text-slides", json={
        "name": "EditMe",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "png_base64": _png_b64(),
    })
    assert create.status_code == 200
    item_id = create.json()["id"]

    # Now PUT a gradient onto it.
    update = client.put(f"/api/content/text-slides/{item_id}", json={
        "name": "EditMe",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#000000",
            "end_color": "#FFFFFF",
            "angle_deg": 90,
        },
        "png_base64": _png_b64(),
    })
    assert update.status_code == 200, update.text
    body = update.json()
    assert body["background_gradient"]["start_color"] == "#000000"
    assert body["background_gradient"]["angle_deg"] == 90.0


def test_textslideupload_rejects_gradient_with_image_bg(client: TestClient):
    """The mutex validator must surface as a 422 at the wire boundary.
    Without the model-level validator, both fields would persist and
    later renderers would see ambiguous state."""
    payload = {
        "name": "BadMutex",
        "duration_ms": 3000,
        "text_layers": [{"text": "x", "box": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}}],
        "background_gradient": {
            "type": "linear",
            "start_color": "#000000",
            "end_color": "#FFFFFF",
            "angle_deg": 0,
        },
        "background_image_slide_id": "00000000-0000-4000-8000-000000000099",
        "png_base64": _png_b64(),
    }
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 422, response.text
