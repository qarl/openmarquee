from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    get_content_storage,
    get_mock_renderer,
)
from openmarquee.rendering.mock import MockRenderer


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
        # `with TestClient(app)` runs the lifespan context — matters because
        # the app's shutdown hook stops the playback loop cleanly.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()


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


# POST /dev/play/{id} was retired alongside the playback engine
# (Phase 5+) — the route + its tests lived here through the early
# phases as the only way to push a stored content item through the
# MockRenderer; PlaybackLoop now drives that continuously off the
# default playlist. The /dev/preview surface (HTML page + frame.png)
# is still useful and tested above.
