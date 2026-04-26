import base64
import io
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import get_content_storage, get_playlist_storage
from openmarquee.playlist import PlaylistStorage


def _real_png_bytes() -> bytes:
    """A genuine 1x1 PNG. The backend now PIL-verifies uploads, so the old
    "fake PNG = magic-number-plus-junk" sentinel no longer round-trips."""
    img = Image.new("RGB", (1, 1), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_FAKE_PNG = _real_png_bytes()


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


def test_upload_text_slide_rejects_both_image_and_video_bg_with_clean_422(
    client: TestClient,
):
    """Phase 5b: setting both background_image_slide_id and
    background_video_slide_id is mutually-exclusive at the model. The
    route must surface the validation as a clean 422 with a JSON-safe
    detail — pre-fix this returned 500 because the ValidationError's
    `input` carried UUID values and `ctx` carried a raw ValueError, and
    FastAPI's default JSON encoder choked on both.
    """
    payload = _upload_payload(
        background_image_slide_id="00000000-0000-4000-8000-000000000088",
        background_video_slide_id="00000000-0000-4000-8000-000000000099",
    )
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 422
    body = response.json()
    # Detail is a list of error dicts (pydantic-shape, JSON round-tripped).
    assert isinstance(body["detail"], list)
    assert any("image and a video" in err.get("msg", "") for err in body["detail"]), body


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
        "image_base64": base64.b64encode(_FAKE_PNG).decode(),
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
    payload["image_base64"] = "not-valid-base64!!!"
    response = client.post("/api/content/images", json=payload)
    assert response.status_code == 400


def test_upload_image_rejects_non_image_bytes(client: TestClient):
    """Valid base64 but not an image — backend should reject with 400 instead
    of persisting garbage that the playback engine would later have to skip."""
    payload = _image_payload()
    payload["image_base64"] = base64.b64encode(b"this is not an image").decode()
    response = client.post("/api/content/images", json=payload)
    assert response.status_code == 400
    assert "image" in response.json()["detail"].lower()


def test_upload_text_slide_rejects_non_image_bytes(client: TestClient):
    payload = _upload_payload()
    payload["png_base64"] = base64.b64encode(b"definitely not a png").decode()
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 400


def test_upload_image_accepts_real_png(client: TestClient, storage: ContentStorage):
    """Belt-and-suspenders: confirm a genuinely valid PNG round-trips."""
    import io as _io

    from PIL import Image as _Image

    img = _Image.new("RGB", (4, 4), (10, 20, 30))
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    payload = _image_payload()
    payload["image_base64"] = base64.b64encode(buf.getvalue()).decode()

    response = client.post("/api/content/images", json=payload)
    assert response.status_code == 200, response.text
    item_id = UUID(response.json()["id"])
    # Asset on disk decodes cleanly.
    saved = storage.read_asset(item_id)
    _Image.open(_io.BytesIO(saved)).verify()


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


# --- video upload ---


def _fake_mp4() -> bytes:
    """Smallest bytes that pass the ftyp-box sanity check."""
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 120


def _video_payload(**overrides) -> dict:
    payload = {
        "name": "Promo",
        "duration_ms": 4000,
        "png_base64": base64.b64encode(_FAKE_PNG).decode("ascii"),
        "mp4_base64": base64.b64encode(_fake_mp4()).decode("ascii"),
    }
    payload.update(overrides)
    return payload


def test_post_video_creates_variant_and_stores_both_assets(
    client: TestClient, storage: ContentStorage
):
    response = client.post("/api/content/videos", json=_video_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "video"
    assert body["name"] == "Promo"

    item_id = UUID(body["id"])
    # Thumbnail is stored as the standard asset.
    assert storage.asset_path(item_id).exists()
    # MP4 is stored at the video path.
    assert storage.video_path(item_id).exists()
    assert storage.read_video(item_id) == _fake_mp4()


def test_post_video_appends_to_default_playlist(
    client: TestClient, playlist_storage: PlaylistStorage
):
    response = client.post("/api/content/videos", json=_video_payload())
    item_id = UUID(response.json()["id"])
    assert playlist_storage.load().item_ids == [item_id]


def test_post_video_rejects_non_mp4_bytes(client: TestClient):
    payload = _video_payload(
        mp4_base64=base64.b64encode(b"not an mp4 file at all").decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 400
    assert "ftyp" in response.json()["detail"].lower()


def test_post_video_rejects_non_image_thumbnail(client: TestClient):
    payload = _video_payload(
        png_base64=base64.b64encode(b"not a png").decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 400


def test_get_video_serves_the_mp4_payload(client: TestClient):
    post = client.post("/api/content/videos", json=_video_payload())
    item_id = post.json()["id"]
    response = client.get(f"/api/content/{item_id}/video")
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == _fake_mp4()


def test_get_video_404_for_unknown_id(client: TestClient):
    response = client.get(f"/api/content/{uuid4()}/video")
    assert response.status_code == 404


def test_delete_video_removes_mp4(client: TestClient, storage: ContentStorage):
    post = client.post("/api/content/videos", json=_video_payload())
    item_id = UUID(post.json()["id"])
    assert storage.video_path(item_id).exists()
    response = client.delete(f"/api/content/{item_id}")
    assert response.status_code == 204
    assert not storage.video_path(item_id).exists()


def test_put_image_metadata_only_keeps_existing_bytes(client: TestClient, storage: ContentStorage):
    """Image PUT with image_base64=null preserves the stored bytes —
    operator renaming a slide shouldn't force a re-upload."""
    post = client.post("/api/content/images", json=_image_payload(name="Logo"))
    item_id = UUID(post.json()["id"])
    original_bytes = storage.read_asset(item_id)

    response = client.put(
        f"/api/content/images/{item_id}",
        json={"name": "Renamed", "duration_ms": 7000, "image_base64": None},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert response.json()["duration_ms"] == 7000
    # Bytes untouched.
    assert storage.read_asset(item_id) == original_bytes


def test_put_image_with_new_bytes_replaces_the_asset(client: TestClient, storage: ContentStorage):
    post = client.post("/api/content/images", json=_image_payload(name="Old"))
    item_id = UUID(post.json()["id"])
    new_png = _real_png_bytes()

    response = client.put(
        f"/api/content/images/{item_id}",
        json={
            "name": "Updated",
            "duration_ms": 5000,
            "image_base64": base64.b64encode(new_png).decode("ascii"),
        },
    )
    assert response.status_code == 200
    assert storage.read_asset(item_id) == new_png


def test_put_image_404s_on_unknown_id(client: TestClient):
    response = client.put(
        f"/api/content/images/{uuid4()}",
        json={"name": "ghost", "duration_ms": 5000, "image_base64": None},
    )
    assert response.status_code == 404


def test_put_image_409s_when_target_is_not_an_image(
    client: TestClient, playlist_storage: PlaylistStorage
):
    """Operator can't use the images PUT route to overwrite a text slide."""
    post = client.post("/api/content/text-slides", json=_upload_payload(name="T"))
    item_id = UUID(post.json()["id"])
    response = client.put(
        f"/api/content/images/{item_id}",
        json={"name": "nope", "duration_ms": 5000, "image_base64": None},
    )
    assert response.status_code == 409
    assert "text_slide" in response.json()["detail"]


def test_put_video_metadata_only_keeps_existing_assets(client: TestClient, storage: ContentStorage):
    """Renaming a large MP4 shouldn't force re-uploading 50 MB."""
    post = client.post("/api/content/videos", json=_video_payload())
    item_id = UUID(post.json()["id"])
    original_thumb = storage.read_asset(item_id)
    original_mp4 = storage.read_video(item_id)

    response = client.put(
        f"/api/content/videos/{item_id}",
        json={
            "name": "Renamed",
            "duration_ms": 8000,
            "png_base64": None,
            "mp4_base64": None,
        },
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert storage.read_asset(item_id) == original_thumb
    assert storage.read_video(item_id) == original_mp4


def test_put_video_with_new_assets_replaces_them(client: TestClient, storage: ContentStorage):
    post = client.post("/api/content/videos", json=_video_payload())
    item_id = UUID(post.json()["id"])

    new_thumb = _real_png_bytes()
    new_mp4 = b"\x00\x00\x00\x20ftypmp42" + b"\xab" * 120

    response = client.put(
        f"/api/content/videos/{item_id}",
        json={
            "name": "v",
            "duration_ms": 5000,
            "png_base64": base64.b64encode(new_thumb).decode("ascii"),
            "mp4_base64": base64.b64encode(new_mp4).decode("ascii"),
        },
    )
    assert response.status_code == 200
    assert storage.read_asset(item_id) == new_thumb
    assert storage.read_video(item_id) == new_mp4


def test_put_video_404s_on_unknown_id(client: TestClient):
    response = client.put(
        f"/api/content/videos/{uuid4()}",
        json={
            "name": "ghost",
            "duration_ms": 5000,
            "png_base64": None,
            "mp4_base64": None,
        },
    )
    assert response.status_code == 404
