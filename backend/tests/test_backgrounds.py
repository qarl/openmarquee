"""Unit tests for openmarquee.backgrounds (HTTP wrapper + image helper)."""

import base64
import io

import httpx
import pytest
from PIL import Image

from openmarquee.backgrounds import (
    OPENAI_IMAGES_URL,
    OpenAIError,
    downscale_to_panel,
    generate_png_via_openai,
)


def _fake_png(w: int = 1024, h: int = 1024, color=(100, 200, 50)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_generate_png_posts_to_images_url_with_auth_header(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(
            200,
            payload={
                "data": [
                    {"b64_json": base64.b64encode(_fake_png()).decode("ascii")}
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    result = generate_png_via_openai("abstract gradient", "sk-test")
    assert result[:8] == b"\x89PNG\r\n\x1a\n"
    assert captured["url"] == OPENAI_IMAGES_URL
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["prompt"] == "abstract gradient"
    assert captured["json"]["model"] == "gpt-image-1"


def test_generate_png_surfaces_openai_error_detail(monkeypatch):
    def fake_post(*args, **kwargs):
        return _FakeResponse(
            400,
            payload={"error": {"message": "Your request was rejected: firearms"}},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(OpenAIError) as exc_info:
        generate_png_via_openai("banned prompt", "sk-test")
    assert "firearms" in str(exc_info.value)
    assert "400" in str(exc_info.value)


def test_generate_png_handles_non_json_error(monkeypatch):
    def fake_post(*args, **kwargs):
        return _FakeResponse(502, payload=None, text="Bad Gateway")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(OpenAIError) as exc_info:
        generate_png_via_openai("x", "sk-test")
    assert "Bad Gateway" in str(exc_info.value)


def test_generate_png_handles_unexpected_shape(monkeypatch):
    def fake_post(*args, **kwargs):
        return _FakeResponse(200, payload={"no_data_key": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(OpenAIError) as exc_info:
        generate_png_via_openai("x", "sk-test")
    assert "unexpected" in str(exc_info.value).lower()


def test_generate_png_wraps_network_failure(monkeypatch):
    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(OpenAIError) as exc_info:
        generate_png_via_openai("x", "sk-test")
    assert "network" in str(exc_info.value).lower()


# --- downscale_to_panel ---


def test_downscale_produces_png_at_requested_dimensions():
    out = downscale_to_panel(_fake_png(1024, 1024), width=128, height=96)
    img = Image.open(io.BytesIO(out))
    assert img.size == (128, 96)


def test_downscale_preserves_aspect_via_letterbox():
    # A portrait source → letterboxed onto a landscape panel.
    src = _fake_png(512, 1024, color=(10, 20, 30))
    out = downscale_to_panel(src, width=192, height=96)
    img = Image.open(io.BytesIO(out))
    # Corners of the landscape canvas are letterbox (black), center is the
    # resized content (the bright color).
    assert img.size == (192, 96)
    assert img.getpixel((0, 0)) == (0, 0, 0)
    center_color = img.getpixel((96, 48))
    assert center_color != (0, 0, 0)
