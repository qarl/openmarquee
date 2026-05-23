from pathlib import Path
from uuid import UUID, uuid4

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


# --- Multi-playlist (id-keyed) endpoints ---


def test_put_rejects_non_uuid_strings(client: TestClient):
    """Pydantic validation on the wire format -- legacy coverage
    preserved against the new endpoint."""
    created = client.post("/api/playlists", json={"name": "x"}).json()
    pid = created["id"]
    response = client.put(
        f"/api/playlists/{pid}",
        json={"item_ids": ["not-a-uuid"]},
    )
    assert response.status_code == 422


def test_put_with_empty_list_clears_the_playlist(client: TestClient):
    a = str(uuid4())
    created = client.post(
        "/api/playlists",
        json={"name": "x", "item_ids": [a]},
    ).json()
    pid = created["id"]
    response = client.put(f"/api/playlists/{pid}", json={"item_ids": []})
    assert response.status_code == 200
    assert response.json()["item_ids"] == []


def test_put_with_duplicate_ids_preserves_them_verbatim(client: TestClient):
    """PUT replaces the whole list verbatim -- no server-side dedup."""
    created = client.post("/api/playlists", json={"name": "x"}).json()
    pid = created["id"]
    a = str(uuid4())
    response = client.put(
        f"/api/playlists/{pid}",
        json={"item_ids": [a, a, a]},
    )
    assert response.status_code == 200
    assert response.json()["item_ids"] == [a, a, a]


def test_put_persists_across_requests(client: TestClient, storage: PlaylistStorage):
    """Direct disk read proves PUT actually wrote, not just round-tripped."""
    created = client.post("/api/playlists", json={"name": "x"}).json()
    pid = created["id"]
    a = str(uuid4())
    client.put(f"/api/playlists/{pid}", json={"item_ids": [a]})
    persisted = storage.get_by_id(UUID(pid))
    assert persisted is not None
    assert [str(item.item_id) for item in persisted.items] == [a]


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
    response = client.post("/api/playlists", json={"name": "lunch", "item_ids": [a, b]})
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
    response = client.put(f"/api/playlists/{pid}", json={"name": "new", "item_ids": [a]})
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


def test_rename_does_not_change_id(client: TestClient):
    """The headline guarantee: an id stays stable across renames so any
    schedule rule referencing the playlist keeps working."""
    created = client.post("/api/playlists", json={"name": "old name"}).json()
    pid = created["id"]
    renamed = client.put(f"/api/playlists/{pid}", json={"name": "new name"}).json()
    assert renamed["id"] == pid
    assert renamed["name"] == "new name"


# --- Default-playlist shorthand `/api/playlist` (SYSTEM_SPEC §6) ---


def test_get_api_playlist_alias_returns_the_default_playlist(client: TestClient):
    """Shorthand GET `/api/playlist` returns the same body as the
    UUID-explicit `GET /api/playlists/{DEFAULT_PLAYLIST_ID}` form.

    Regression gate for the spec §6 alias semantics — the two surfaces
    must round-trip byte-identical so clients can treat the shorthand
    as a drop-in replacement for the UUID form.
    """
    alias_body = client.get("/api/playlist").json()
    explicit_body = client.get(f"/api/playlists/{DEFAULT_PLAYLIST_ID}").json()
    assert alias_body == explicit_body
    # Sanity-check: the alias really resolved to the default playlist.
    assert alias_body["id"] == str(DEFAULT_PLAYLIST_ID)


def test_put_api_playlist_alias_round_trips_through_default_storage(
    client: TestClient,
):
    """Shorthand PUT `/api/playlist` writes to the same storage slot
    as `PUT /api/playlists/{DEFAULT_PLAYLIST_ID}`.

    Verifies that a write via the shorthand is visible immediately on
    the explicit GET path — i.e. there's no parallel default-playlist
    storage created by the alias, just delegation.
    """
    a, b = str(uuid4()), str(uuid4())
    put_body = client.put("/api/playlist", json={"name": "via-alias", "item_ids": [a, b]}).json()
    assert put_body["id"] == str(DEFAULT_PLAYLIST_ID)
    assert put_body["name"] == "via-alias"
    assert put_body["item_ids"] == [a, b]
    # The same write is visible on the UUID-explicit GET.
    explicit_body = client.get(f"/api/playlists/{DEFAULT_PLAYLIST_ID}").json()
    assert explicit_body == put_body


def test_put_api_playlist_alias_preserves_name_when_omitted(client: TestClient):
    """The shorthand PUT inherits the UUID-explicit handler's
    name-preservation contract: omitting `name` keeps whatever the
    default playlist already had."""
    # Seed a name via the alias to make the test independent of bootstrap.
    client.put("/api/playlist", json={"name": "seeded", "item_ids": []})
    a = str(uuid4())
    # Now PUT items only — name should stay "seeded".
    body = client.put("/api/playlist", json={"item_ids": [a]}).json()
    assert body["name"] == "seeded"
    assert body["item_ids"] == [a]


def test_get_api_playlist_alias_404_when_default_was_deleted(client: TestClient):
    """The alias inherits the UUID-explicit handler's 404 contract:
    after the default playlist is deleted (and before the next content
    upload re-creates it), shorthand GET surfaces a 404 — matching the
    docstring on `get_default_playlist`."""
    deleted = client.delete(f"/api/playlists/{DEFAULT_PLAYLIST_ID}")
    assert deleted.status_code == 204
    assert client.get("/api/playlist").status_code == 404
