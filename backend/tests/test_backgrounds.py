"""Unit tests for openmarquee.backgrounds (provider abstractions + the
Pollinations.ai implementation + the downscale helper)."""

import io

import httpx
import pytest
from PIL import Image

from openmarquee.backgrounds import (
    PROVIDERS,
    BackgroundGenError,
    BackgroundProviderUnknown,
    PollinationsProvider,
    default_provider_name,
    downscale_to_panel,
    resolve_provider,
)


def _fake_image_bytes(w: int = 256, h: int = 256, color=(100, 200, 50)) -> bytes:
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text


# --- registry ---


def test_pollinations_is_the_shipped_default_provider():
    assert default_provider_name() == "pollinations"
    assert "pollinations" in PROVIDERS


def test_resolve_provider_falls_back_to_default_when_name_is_none():
    provider = resolve_provider(None)
    assert provider.name == "pollinations"


def test_resolve_provider_honors_env_override(monkeypatch):
    monkeypatch.setenv("OPENMARQUEE_IMAGEGEN_PROVIDER", "pollinations")
    provider = resolve_provider(None)
    assert provider.name == "pollinations"


def test_resolve_provider_raises_on_unknown_name():
    with pytest.raises(BackgroundProviderUnknown) as exc:
        resolve_provider("dall-e")
    assert "dall-e" in str(exc.value)


# --- Pollinations ---


def test_pollinations_url_encodes_the_prompt_into_the_path(monkeypatch):
    captured = {}

    def fake_get(url, *, params, timeout):
        captured["url"] = url
        captured["params"] = params
        return _FakeResponse(200, content=_fake_image_bytes())

    monkeypatch.setattr(httpx, "get", fake_get)
    PollinationsProvider().generate("abstract gradient / pastel, minimal")
    assert captured["url"].startswith("https://image.pollinations.ai/prompt/")
    # Spaces, slashes, commas all survive as percent-escapes (not raw chars).
    encoded = captured["url"].rsplit("/", 1)[-1]
    assert " " not in encoded
    assert "/" not in encoded
    assert "," not in encoded
    assert captured["params"]["nologo"] == "true"
    assert captured["params"]["width"] == 1024
    assert captured["params"]["height"] == 1024


def test_pollinations_returns_raw_image_bytes(monkeypatch):
    image = _fake_image_bytes()

    def fake_get(*args, **kwargs):
        return _FakeResponse(200, content=image)

    monkeypatch.setattr(httpx, "get", fake_get)
    out = PollinationsProvider().generate("x")
    assert out == image


def test_pollinations_maps_non_2xx_to_backgroundgenerror(monkeypatch):
    def fake_get(*args, **kwargs):
        return _FakeResponse(503, text="service overloaded")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(BackgroundGenError) as exc:
        PollinationsProvider().generate("x")
    assert "503" in str(exc.value)
    assert "overloaded" in str(exc.value)


def test_pollinations_maps_network_failure_to_backgroundgenerror(monkeypatch):
    def fake_get(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(BackgroundGenError) as exc:
        PollinationsProvider().generate("x")
    assert "network" in str(exc.value).lower()


def test_pollinations_rejects_empty_response(monkeypatch):
    def fake_get(*args, **kwargs):
        return _FakeResponse(200, content=b"")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(BackgroundGenError) as exc:
        PollinationsProvider().generate("x")
    assert "empty" in str(exc.value).lower()


# --- downscale_to_panel ---


def test_downscale_produces_png_at_requested_dimensions():
    out = downscale_to_panel(_fake_image_bytes(1024, 1024), width=128, height=96)
    img = Image.open(io.BytesIO(out))
    assert img.size == (128, 96)
    assert img.format == "PNG"


def test_downscale_accepts_jpeg_input_from_pollinations():
    """Pollinations returns JPEG; verify PIL auto-detects without a hint."""
    jpeg_in = _fake_image_bytes(800, 600, color=(30, 60, 90))
    out = downscale_to_panel(jpeg_in, width=128, height=96)
    img = Image.open(io.BytesIO(out))
    assert img.size == (128, 96)


def test_downscale_preserves_aspect_via_letterbox():
    src = _fake_image_bytes(512, 1024, color=(10, 20, 30))
    out = downscale_to_panel(src, width=192, height=96)
    img = Image.open(io.BytesIO(out))
    assert img.size == (192, 96)
    # Corners are letterbox black.
    assert img.getpixel((0, 0)) == (0, 0, 0)
    # Center has the source content (non-black).
    center_color = img.getpixel((96, 48))
    assert center_color != (0, 0, 0)
