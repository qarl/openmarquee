from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _playlist_storage_singleton,
    get_playlist_storage,
)
from openmarquee.playlist import PlaylistStorage


@pytest.fixture
def storage(tmp_path: Path) -> PlaylistStorage:
    return PlaylistStorage(tmp_path / "playlist.json")


@pytest.fixture
def client(storage: PlaylistStorage) -> TestClient:
    app.dependency_overrides[get_playlist_storage] = lambda: storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _playlist_storage_singleton.cache_clear()


def test_get_empty_playlist_returns_empty_list(client: TestClient):
    response = client.get("/api/playlist")
    assert response.status_code == 200
    assert response.json() == {"item_ids": []}


def test_put_then_get_round_trips_order(client: TestClient):
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    response = client.put("/api/playlist", json={"item_ids": [c, a, b]})
    assert response.status_code == 200
    assert response.json() == {"item_ids": [c, a, b]}

    response = client.get("/api/playlist")
    assert response.json() == {"item_ids": [c, a, b]}


def test_put_with_empty_list_clears_the_playlist(client: TestClient):
    a = str(uuid4())
    client.put("/api/playlist", json={"item_ids": [a]})
    response = client.put("/api/playlist", json={"item_ids": []})
    assert response.status_code == 200
    assert response.json() == {"item_ids": []}


def test_put_rejects_non_uuid_strings(client: TestClient):
    response = client.put("/api/playlist", json={"item_ids": ["not-a-uuid"]})
    assert response.status_code == 422


def test_put_with_duplicate_ids_preserves_them_verbatim(client: TestClient):
    """The constructor doesn't dedup — round-trip should preserve whatever
    the UI sent, even if it's silly. (Playlist.append dedups, but PUT replaces
    the whole list and trusts the caller.)"""
    a = str(uuid4())
    response = client.put("/api/playlist", json={"item_ids": [a, a, a]})
    assert response.status_code == 200
    assert response.json()["item_ids"] == [a, a, a]


def test_put_persists_across_requests(client: TestClient, storage: PlaylistStorage):
    a = str(uuid4())
    client.put("/api/playlist", json={"item_ids": [a]})
    # Direct read from storage proves the PUT actually wrote to disk.
    persisted = storage.load()
    assert [str(item_id) for item_id in persisted.item_ids] == [a]
