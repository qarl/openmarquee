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


# --- multi-playlist endpoints ---


def test_get_playlists_returns_empty_collection_initially(client: TestClient):
    response = client.get("/api/playlists")
    assert response.status_code == 200
    body = response.json()
    assert body["playlists"] == {}


def test_put_named_playlist_creates_it(client: TestClient, storage: PlaylistStorage):
    a, b = str(uuid4()), str(uuid4())
    response = client.put("/api/playlists/lunch", json={"item_ids": [a, b]})
    assert response.status_code == 200
    assert response.json() == {"item_ids": [a, b]}
    # And it shows up in the collection.
    coll = client.get("/api/playlists").json()
    assert "lunch" in coll["playlists"]


def test_get_named_playlist_returns_empty_for_unknown_name(client: TestClient):
    response = client.get("/api/playlists/nope")
    assert response.status_code == 200
    assert response.json() == {"item_ids": []}


def test_delete_named_playlist_removes_it(client: TestClient):
    a = str(uuid4())
    client.put("/api/playlists/lunch", json={"item_ids": [a]})
    response = client.delete("/api/playlists/lunch")
    assert response.status_code == 204
    coll = client.get("/api/playlists").json()
    assert "lunch" not in coll["playlists"]


def test_delete_named_playlist_404_when_missing(client: TestClient):
    response = client.delete("/api/playlists/nope")
    assert response.status_code == 404


def test_legacy_and_multi_endpoints_see_the_same_default_playlist(
    client: TestClient,
):
    """Setting via /api/playlist (legacy) should be readable via
    /api/playlists/default (new), and vice versa."""
    a, b = str(uuid4()), str(uuid4())
    client.put("/api/playlist", json={"item_ids": [a, b]})

    via_new = client.get("/api/playlists/default").json()
    assert via_new["item_ids"] == [a, b]

    c = str(uuid4())
    client.put("/api/playlists/default", json={"item_ids": [c]})
    via_legacy = client.get("/api/playlist").json()
    assert via_legacy["item_ids"] == [c]
