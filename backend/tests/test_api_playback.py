import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content import TextSlide
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _mock_renderer_singleton,
    _playback_loop_singleton,
    get_content_storage,
    get_mock_renderer,
    get_playback_loop,
)
from openmarquee.playback import PlaybackLoop
from openmarquee.rendering.mock import MockRenderer


def _png_bytes(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def renderer(tmp_path: Path) -> MockRenderer:
    return MockRenderer(8, 8, tmp_path / "preview.png")


@pytest.fixture
def loop(storage: ContentStorage, renderer: MockRenderer) -> PlaybackLoop:
    return PlaybackLoop(
        renderer=renderer,
        fetch_items=storage.list_all,
        read_asset=storage.read_asset,
        empty_playlist_poll_seconds=0.01,
    )


@pytest.fixture
def client(storage: ContentStorage, renderer: MockRenderer, loop: PlaybackLoop) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_mock_renderer] = lambda: renderer
    app.dependency_overrides[get_playback_loop] = lambda: loop
    try:
        # `with TestClient(app)` triggers the app's lifespan context — needed
        # here because the lifespan shutdown calls get_playback_loop().stop(),
        # which (via the override) unwinds the fixture's loop cleanly.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _mock_renderer_singleton.cache_clear()
        _playback_loop_singleton.cache_clear()


def test_state_returns_not_running_initially(client: TestClient):
    response = client.get("/api/playback/state")
    assert response.status_code == 200
    assert response.json() == {"is_running": False, "current_item_id": None}


def test_start_returns_204_and_state_flips_to_running(client: TestClient):
    response = client.post("/api/playback/start")
    assert response.status_code == 204
    # Check via the state endpoint — it runs on the same event loop as the
    # playback task, so it sees the true state (cross-thread attribute peeks
    # from pytest's main thread can race the portal's loop).
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is True
    client.post("/api/playback/stop")


def test_stop_returns_204_and_state_flips_to_not_running(client: TestClient):
    client.post("/api/playback/start")
    response = client.post("/api/playback/stop")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is False
    assert state["current_item_id"] is None


def test_start_then_start_is_idempotent(client: TestClient):
    client.post("/api/playback/start")
    response = client.post("/api/playback/start")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is True
    client.post("/api/playback/stop")


def test_stop_when_not_running_is_idempotent(client: TestClient):
    response = client.post("/api/playback/stop")
    assert response.status_code == 204
    state = client.get("/api/playback/state").json()
    assert state["is_running"] is False


def test_state_reports_current_item_id_while_playing(
    client: TestClient, storage: ContentStorage
):
    """With a real item in storage, start the loop and poll state — the
    backend should surface the currently-rendering slide's id."""
    slide = TextSlide(name="x", text="x", duration_ms=1000)
    storage.save_text_slide(slide, _png_bytes(8, 8, (255, 0, 0)))

    client.post("/api/playback/start")

    # Poll for up to 2s while the portal's event loop schedules the task.
    deadline = time.time() + 2.0
    current_id = None
    while time.time() < deadline:
        state = client.get("/api/playback/state").json()
        current_id = state["current_item_id"]
        if current_id is not None:
            break
        time.sleep(0.05)

    assert current_id == str(slide.id)
    client.post("/api/playback/stop")
