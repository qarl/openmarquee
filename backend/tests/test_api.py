import base64
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_playlist_storage
from openmarquee.playlist import PlaylistStorage

_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake-payload"


@pytest.fixture
def storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def playlist_storage(tmp_path: Path) -> PlaylistStorage:
    return PlaylistStorage(tmp_path / "playlist.json")


@pytest.fixture
def client(storage: ContentStorage, playlist_storage: PlaylistStorage) -> TestClient:
    app.dependency_overrides[get_content_storage] = lambda: storage
    app.dependency_overrides[get_playlist_storage] = lambda: playlist_storage
    try:
        # `with TestClient(app)` runs the lifespan context — matters because
        # the app's shutdown hook stops the playback loop cleanly.
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        # Defense in depth: drop the lru_cache'd singletons so a later test
        # without an override doesn't pick up a torn-down tmp_path.
        from openmarquee.dependencies import (
            _content_storage_singleton,
            _playlist_storage_singleton,
        )

        _content_storage_singleton.cache_clear()
        _playlist_storage_singleton.cache_clear()


def _upload_payload(**overrides) -> dict:
    payload = {
        "name": "Test Slide",
        "text": "Hello, world",
        "png_base64": base64.b64encode(_FAKE_PNG).decode(),
    }
    payload.update(overrides)
    return payload


# --- POST /api/content/text-slides ---


def test_upload_text_slide_persists_metadata_and_asset(client: TestClient, storage: ContentStorage):
    response = client.post("/api/content/text-slides", json=_upload_payload(name="Specials"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["type"] == "text_slide"
    assert body["name"] == "Specials"
    assert body["text"] == "Hello, world"
    assert body["duration_ms"] == 5000  # default

    item_id = UUID(body["id"])
    assert storage.exists(item_id)
    assert storage.read_asset(item_id) == _FAKE_PNG


def test_upload_text_slide_normalizes_color(client: TestClient):
    response = client.post(
        "/api/content/text-slides",
        json=_upload_payload(text_color="#ffaa00"),
    )
    assert response.status_code == 200
    assert response.json()["text_color"] == "#FFAA00"


def test_upload_text_slide_rejects_bad_base64(client: TestClient):
    payload = _upload_payload()
    payload["png_base64"] = "not-valid-base64!!!"
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 400
    assert "png_base64" in response.json()["detail"]


def test_upload_text_slide_rejects_invalid_color(client: TestClient):
    response = client.post(
        "/api/content/text-slides",
        json=_upload_payload(text_color="red"),
    )
    assert response.status_code == 422  # Pydantic validation error


# --- GET /api/content ---


def test_list_content_empty(client: TestClient):
    response = client.get("/api/content")
    assert response.status_code == 200
    assert response.json() == []


def test_list_content_returns_uploaded_items(client: TestClient):
    client.post("/api/content/text-slides", json=_upload_payload(name="A", text="A"))
    client.post("/api/content/text-slides", json=_upload_payload(name="B", text="B"))
    response = client.get("/api/content")
    assert response.status_code == 200
    names = {item["name"] for item in response.json()}
    assert names == {"A", "B"}


# --- GET /api/content/{id} ---


def test_get_content_item_returns_metadata(client: TestClient):
    upload = client.post("/api/content/text-slides", json=_upload_payload(name="Pulled Pork"))
    item_id = upload.json()["id"]

    response = client.get(f"/api/content/{item_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "Pulled Pork"


def test_get_content_item_404_when_missing(client: TestClient):
    response = client.get(f"/api/content/{uuid4()}")
    assert response.status_code == 404


def test_get_content_item_422_when_id_not_uuid(client: TestClient):
    response = client.get("/api/content/not-a-uuid")
    assert response.status_code == 422


# --- GET /api/content/{id}/asset ---


def test_get_asset_returns_png_bytes(client: TestClient):
    upload = client.post("/api/content/text-slides", json=_upload_payload())
    item_id = upload.json()["id"]

    response = client.get(f"/api/content/{item_id}/asset")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == _FAKE_PNG


def test_get_asset_404_when_missing(client: TestClient):
    response = client.get(f"/api/content/{uuid4()}/asset")
    assert response.status_code == 404


def test_get_asset_404_when_metadata_present_but_asset_missing(
    client: TestClient, storage: ContentStorage, tmp_path: Path
):
    """Asset endpoint should 404 cleanly even if the item.json envelope exists."""
    upload = client.post("/api/content/text-slides", json=_upload_payload())
    item_id = UUID(upload.json()["id"])
    storage.asset_path(item_id).unlink()

    response = client.get(f"/api/content/{item_id}/asset")
    assert response.status_code == 404


def test_uploads_with_duplicate_names_both_succeed(client: TestClient):
    """Names aren't unique; the id keys items, so two slides with the same
    name should coexist."""
    a = client.post("/api/content/text-slides", json=_upload_payload(name="Special"))
    b = client.post("/api/content/text-slides", json=_upload_payload(name="Special"))
    assert a.status_code == 200
    assert b.status_code == 200
    assert a.json()["id"] != b.json()["id"]
    assert len(client.get("/api/content").json()) == 2


# --- DELETE /api/content/{id} ---


def test_delete_content_item_removes_it(client: TestClient, storage: ContentStorage):
    upload = client.post("/api/content/text-slides", json=_upload_payload())
    item_id = UUID(upload.json()["id"])

    response = client.delete(f"/api/content/{item_id}")
    assert response.status_code == 204
    assert not storage.exists(item_id)


def test_delete_content_item_404_when_missing(client: TestClient):
    response = client.delete(f"/api/content/{uuid4()}")
    assert response.status_code == 404


# --- POST /api/content/images ---


def _image_payload(**overrides) -> dict:
    payload = {
        "name": "Logo",
        "png_base64": base64.b64encode(_FAKE_PNG).decode(),
    }
    payload.update(overrides)
    return payload


def test_upload_image_persists_metadata_and_asset(client: TestClient, storage: ContentStorage):
    response = client.post("/api/content/images", json=_image_payload(name="Promo"))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["type"] == "image"
    assert body["name"] == "Promo"
    assert body["duration_ms"] == 5000

    item_id = UUID(body["id"])
    assert storage.exists(item_id)
    assert storage.read_asset(item_id) == _FAKE_PNG


def test_upload_image_rejects_bad_base64(client: TestClient):
    payload = _image_payload()
    payload["png_base64"] = "not-valid-base64!!!"
    response = client.post("/api/content/images", json=payload)
    assert response.status_code == 400


def test_upload_image_rejects_name_too_long(client: TestClient):
    response = client.post("/api/content/images", json=_image_payload(name="x" * 201))
    assert response.status_code == 422


def test_list_content_returns_mixed_variants(client: TestClient):
    """Uploading a text slide and an image results in both appearing in /api/content
    with the correct `type` literal on each."""
    client.post("/api/content/text-slides", json=_upload_payload(name="Text"))
    client.post("/api/content/images", json=_image_payload(name="Image"))

    response = client.get("/api/content")
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 2
    types = {item["type"] for item in items}
    assert types == {"text_slide", "image"}


# --- Playlist auto-update on content lifecycle ---


def test_uploading_text_slide_appends_to_playlist(
    client: TestClient, playlist_storage: PlaylistStorage
):
    response = client.post("/api/content/text-slides", json=_upload_payload())
    item_id = UUID(response.json()["id"])
    assert playlist_storage.load().item_ids == [item_id]


def test_uploading_image_appends_to_playlist(client: TestClient, playlist_storage: PlaylistStorage):
    response = client.post("/api/content/images", json=_image_payload())
    item_id = UUID(response.json()["id"])
    assert playlist_storage.load().item_ids == [item_id]


def test_uploads_append_in_order(client: TestClient, playlist_storage: PlaylistStorage):
    a = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="A")).json()["id"])
    b = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="B")).json()["id"])
    c = UUID(client.post("/api/content/images", json=_image_payload(name="C")).json()["id"])
    assert playlist_storage.load().item_ids == [a, b, c]


def test_deleting_content_removes_from_playlist(
    client: TestClient, playlist_storage: PlaylistStorage
):
    a = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="A")).json()["id"])
    b = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="B")).json()["id"])
    assert playlist_storage.load().item_ids == [a, b]

    client.delete(f"/api/content/{a}")
    assert playlist_storage.load().item_ids == [b]


def test_list_content_returns_items_in_playlist_order(
    client: TestClient, playlist_storage: PlaylistStorage
):
    """GET /api/content reflects playlist order, not id-sort."""
    a = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="A")).json()["id"])
    b = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="B")).json()["id"])
    c = UUID(client.post("/api/content/text-slides", json=_upload_payload(name="C")).json()["id"])

    # Reverse the playlist order.
    client.put("/api/playlist", json={"item_ids": [str(c), str(b), str(a)]})

    response = client.get("/api/content").json()
    assert [item["name"] for item in response] == ["C", "B", "A"]
