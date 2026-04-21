"""API tests for POST /api/backgrounds/generate."""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _playlist_storage_singleton,
    _settings_storage_singleton,
    get_content_storage,
    get_playlist_storage,
    get_settings_storage,
)
from openmarquee.playlist import PlaylistStorage
from openmarquee.settings import SettingsStorage, SystemSettings


def _real_png_bytes(size: int = 1024) -> bytes:
    """Square PNG at the size OpenAI's Images API would return. Colored
    so downscale_to_panel produces a non-trivial final asset we can
    verify round-trips."""
    img = Image.new("RGB", (size, size), (120, 200, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def playlist_storage(tmp_path: Path) -> PlaylistStorage:
    return PlaylistStorage(tmp_path / "playlist.json")


@pytest.fixture
def settings_storage(tmp_path: Path) -> SettingsStorage:
    store = SettingsStorage(tmp_path / "settings.json")
    store.save(SystemSettings(display_width=128, display_height=96))
    return store


@pytest.fixture
def client(
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    settings_storage: SettingsStorage,
) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_playlist_storage] = lambda: playlist_storage
    app.dependency_overrides[get_settings_storage] = lambda: settings_storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _content_storage_singleton.cache_clear()
        _playlist_storage_singleton.cache_clear()
        _settings_storage_singleton.cache_clear()


def test_generate_returns_503_when_no_api_key(client: TestClient, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post(
        "/api/backgrounds/generate", json={"prompt": "abstract gradient"}
    )
    assert response.status_code == 503
    assert "OPENAI_API_KEY" in response.json()["detail"]


def test_generate_saves_image_and_appends_to_playlist(
    client: TestClient,
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "openmarquee.api_backgrounds.generate_png_via_openai",
        lambda prompt, key: _real_png_bytes(),
    )

    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "minimal sunrise gradient, signage-friendly"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "image"
    assert body["name"].startswith("Background — ")

    # Asset landed on disk at panel dimensions (128×96).
    from uuid import UUID
    item_id = UUID(body["id"])
    asset = storage.read_asset(item_id)
    png = Image.open(io.BytesIO(asset))
    assert png.size == (128, 96)

    # And is appended to the default playlist.
    assert playlist_storage.load().item_ids == [item_id]


def test_generate_rejects_empty_prompt(client: TestClient, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    response = client.post("/api/backgrounds/generate", json={"prompt": ""})
    assert response.status_code == 422


def test_generate_accepts_optional_name_override(
    client: TestClient,
    monkeypatch,
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        "openmarquee.api_backgrounds.generate_png_via_openai",
        lambda prompt, key: _real_png_bytes(),
    )
    response = client.post(
        "/api/backgrounds/generate",
        json={"prompt": "x", "name": "My Custom Background"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "My Custom Background"


def test_generate_maps_openai_error_to_502(client: TestClient, monkeypatch):
    from openmarquee.backgrounds import OpenAIError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def boom(prompt, key):
        raise OpenAIError("content policy violation: 'firearms'")

    monkeypatch.setattr("openmarquee.api_backgrounds.generate_png_via_openai", boom)
    response = client.post("/api/backgrounds/generate", json={"prompt": "gun"})
    assert response.status_code == 502
    assert "content policy" in response.json()["detail"]
