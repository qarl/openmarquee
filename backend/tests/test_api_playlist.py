from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _playlist_storage_singleton,
    get_playlist_storage,
)
from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
    PlaylistStorage,
)


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


# --- Legacy single-playlist endpoint (operates on the default by id) ---


def test_get_empty_playlist_returns_empty_default(client: TestClient):
    response = client.get("/api/playlist")
    assert response.status_code == 200
    body = response.json()
    assert body["item_ids"] == []
    assert body["items"] == []
    assert body["id"] == str(DEFAULT_PLAYLIST_ID)


def test_put_then_get_round_trips_order(client: TestClient):
    a, b, c = str(uuid4()), str(uuid4()), str(uuid4())
    response = client.put("/api/playlist", json={"item_ids": [c, a, b]})
    assert response.status_code == 200
    assert response.json()["item_ids"] == [c, a, b]

    response = client.get("/api/playlist")
    assert response.json()["item_ids"] == [c, a, b]


def test_put_with_empty_list_clears_the_playlist(client: TestClient):
    a = str(uuid4())
    client.put("/api/playlist", json={"item_ids": [a]})
    response = client.put("/api/playlist", json={"item_ids": []})
    assert response.status_code == 200
    assert response.json()["item_ids"] == []


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


# --- Multi-playlist (id-keyed) endpoints ---


def test_get_playlists_returns_default_playlist_initially(client: TestClient):
    """Even on a fresh device the collection contains the default playlist."""
    response = client.get("/api/playlists")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["playlists"], list)
    ids = [p["id"] for p in body["playlists"]]
    assert str(DEFAULT_PLAYLIST_ID) in ids


def test_post_creates_a_new_playlist_with_a_fresh_id(client: TestClient):
    a, b = str(uuid4()), str(uuid4())
    response = client.post(
        "/api/playlists", json={"name": "lunch", "item_ids": [a, b]}
    )
    assert response.status_code == 201
    body = response.json()
    new_id = body["id"]
    # Server-assigned id, not the default.
    assert new_id != str(DEFAULT_PLAYLIST_ID)
    assert body["name"] == "lunch"
    assert body["item_ids"] == [a, b]
    # And it shows up in the collection.
    coll = client.get("/api/playlists").json()
    assert any(p["id"] == new_id for p in coll["playlists"])


def test_get_playlist_by_id_404_when_missing(client: TestClient):
    response = client.get(f"/api/playlists/{uuid4()}")
    assert response.status_code == 404


def test_put_playlist_by_id_replaces_name_and_items(client: TestClient):
    # Create one to update.
    created = client.post("/api/playlists", json={"name": "old"}).json()
    pid = created["id"]
    a = str(uuid4())
    response = client.put(
        f"/api/playlists/{pid}", json={"name": "new", "item_ids": [a]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == pid  # id immutable
    assert body["name"] == "new"
    assert body["item_ids"] == [a]


def test_put_playlist_by_id_preserves_name_when_omitted(client: TestClient):
    """PUT with only items (no name) should keep the existing name."""
    created = client.post("/api/playlists", json={"name": "morning"}).json()
    pid = created["id"]
    a = str(uuid4())
    response = client.put(f"/api/playlists/{pid}", json={"item_ids": [a]})
    assert response.json()["name"] == "morning"


def test_delete_playlist_by_id_removes_it(client: TestClient):
    created = client.post("/api/playlists", json={"name": "lunch"}).json()
    pid = created["id"]
    response = client.delete(f"/api/playlists/{pid}")
    assert response.status_code == 204
    # No longer in the collection.
    coll = client.get("/api/playlists").json()
    ids = [p["id"] for p in coll["playlists"]]
    assert pid not in ids


def test_delete_playlist_by_id_404_when_missing(client: TestClient):
    response = client.delete(f"/api/playlists/{uuid4()}")
    assert response.status_code == 404


def test_legacy_and_id_endpoints_see_the_same_default_playlist(
    client: TestClient,
):
    """Setting via /api/playlist (legacy) should be readable via
    /api/playlists/{DEFAULT_PLAYLIST_ID}, and vice versa."""
    a, b = str(uuid4()), str(uuid4())
    client.put("/api/playlist", json={"item_ids": [a, b]})

    via_new = client.get(f"/api/playlists/{DEFAULT_PLAYLIST_ID}").json()
    assert via_new["item_ids"] == [a, b]

    c = str(uuid4())
    client.put(
        f"/api/playlists/{DEFAULT_PLAYLIST_ID}", json={"item_ids": [c]}
    )
    via_legacy = client.get("/api/playlist").json()
    assert via_legacy["item_ids"] == [c]


def test_rename_does_not_change_id(client: TestClient):
    """The headline guarantee: an id stays stable across renames so any
    schedule rule referencing the playlist keeps working."""
    created = client.post("/api/playlists", json={"name": "old name"}).json()
    pid = created["id"]
    renamed = client.put(
        f"/api/playlists/{pid}", json={"name": "new name"}
    ).json()
    assert renamed["id"] == pid
    assert renamed["name"] == "new name"
