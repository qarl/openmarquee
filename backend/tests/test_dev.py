import base64
import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    get_content_storage,
    get_mock_renderer,
)
from openmarquee.rendering.mock import MockRenderer


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    """Encode a solid-color RGB image as PNG."""
    image = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def renderer(tmp_path: Path) -> MockRenderer:
    return MockRenderer(8, 8, tmp_path / "preview.png")


@pytest.fixture
def client(storage: ContentStorage, renderer: MockRenderer) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_mock_renderer] = lambda: renderer
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()


def _upload_text_slide(client: TestClient, png: bytes, name: str = "Slide") -> UUID:
    response = client.post(
        "/api/content/text-slides",
        json={
            "name": name,
            "text": "x",
            "png_base64": base64.b64encode(png).decode(),
        },
    )
    assert response.status_code == 200, response.text
    return UUID(response.json()["id"])


# --- /dev/preview HTML page ---


def test_preview_page_serves_html(client: TestClient):
    response = client.get("/dev/preview")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<html" in response.text
    assert "/dev/preview/frame.png" in response.text


# --- /dev/preview/frame.png ---


def test_preview_frame_404_before_any_render(client: TestClient):
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 404


def test_preview_frame_returns_png_after_render(client: TestClient, renderer: MockRenderer):
    renderer.render_frame(bytes((255, 0, 0)) * (renderer.width * renderer.height))
    response = client.get("/dev/preview/frame.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


# --- POST /dev/play/{id} ---


def test_play_item_pushes_asset_to_renderer(client: TestClient, renderer: MockRenderer):
    """Upload a 8x8 red PNG, play it, MockRenderer's last_frame is all red."""
    item_id = _upload_text_slide(client, _png_bytes(8, 8, (255, 0, 0)))

    response = client.post(f"/dev/play/{item_id}")
    assert response.status_code == 204

    expected = bytes((255, 0, 0)) * (renderer.width * renderer.height)
    assert renderer.last_frame == expected


def test_play_item_resizes_when_asset_dimensions_differ(client: TestClient, renderer: MockRenderer):
    """A 16x16 PNG is rescaled to the renderer's 8x8 native size."""
    item_id = _upload_text_slide(client, _png_bytes(16, 16, (0, 255, 0)))

    response = client.post(f"/dev/play/{item_id}")
    assert response.status_code == 204
    assert renderer.last_frame == bytes((0, 255, 0)) * (renderer.width * renderer.height)


def test_play_item_404_when_missing(client: TestClient):
    response = client.post(f"/dev/play/{uuid4()}")
    assert response.status_code == 404


def test_play_item_writes_preview_png(client: TestClient, renderer: MockRenderer, tmp_path: Path):
    item_id = _upload_text_slide(client, _png_bytes(8, 8, (0, 0, 255)))

    client.post(f"/dev/play/{item_id}")

    assert renderer.output_path.exists()
    img = Image.open(renderer.output_path)
    assert img.size == (renderer.width, renderer.height)
    assert img.getpixel((0, 0)) == (0, 0, 255)


def test_play_item_handles_rgba_asset(client: TestClient, renderer: MockRenderer):
    """An RGBA PNG (with alpha channel) should be flattened to RGB and play."""
    rgba = Image.new("RGBA", (8, 8), (255, 128, 0, 200))
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    item_id = _upload_text_slide(client, buf.getvalue())

    response = client.post(f"/dev/play/{item_id}")
    assert response.status_code == 204
    assert renderer.last_frame is not None
    # First pixel: PIL flattens RGBA → RGB with no alpha compositing,
    # so we get the source color verbatim.
    assert renderer.last_frame[:3] == bytes((255, 128, 0))


def test_play_item_422_for_corrupt_asset(
    client: TestClient, storage: ContentStorage, renderer: MockRenderer
):
    """A non-image asset on disk should 422, not 500."""
    item_id = _upload_text_slide(client, _png_bytes(8, 8, (0, 0, 0)))
    # Corrupt the asset on disk.
    storage.asset_path(item_id).write_bytes(b"this is not a PNG")

    response = client.post(f"/dev/play/{item_id}")
    assert response.status_code == 422
    assert "not a valid image" in response.json()["detail"]
