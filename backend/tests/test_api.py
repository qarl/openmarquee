import asyncio
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
from openmarquee.playlist import DEFAULT_PLAYLIST_ID, PlaylistStorage


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
    """Build a TextSlideUpload payload. Accepts the old flat `text=`,
    `text_color=`, `font_family=`, `auto_mode=`, `auto_format=`, `box=`
    kwargs and routes them into text_layers[0]; slide-level kwargs
    (name=, duration_ms=, background_color=, transition=, …) stay at
    the root. Schema v3 contract — see SYSTEM_SPEC §5.10a."""
    layer_keys = {
        "text",
        "text_color",
        "font_family",
        "font_size_px",
        "font_size_pct",
        "auto_mode",
        "auto_format",
        "box",
        "motion",
        "motion_intensity",
        "motion_phase",
    }
    layer = {"text": overrides.pop("text", "Hello, world")}
    for k in list(overrides.keys()):
        if k in layer_keys:
            layer[k] = overrides.pop(k)
    payload = {
        "name": overrides.pop("name", "Test Slide"),
        "text_layers": [layer],
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
    assert body["text_layers"][0]["text"] == "Hello, world"
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
    assert response.json()["text_layers"][0]["text_color"] == "#FFAA00"


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
    assert any("exactly one of" in err.get("msg", "") for err in body["detail"]), body


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


def test_text_slide_post_persists_box_from_payload(client: TestClient):
    """qarl §5.10a fu 2026-04-30: TextSlideUpload was missing the `box`
    field, so Pydantic silently dropped it from the editor's payload
    and every save reverted to TextSlide's default. This test pins the
    POST route's box-roundtrip contract."""
    payload = _upload_payload(name="A", text="A", box={"x": 0.2, "y": 0.3, "w": 0.5, "h": 0.4})
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["text_layers"][0]["box"] == {
        "x": 0.2,
        "y": 0.3,
        "w": 0.5,
        "h": 0.4,
    }


def test_text_slide_put_persists_box_from_payload(client: TestClient):
    """Same contract on the PUT (edit-existing) route."""
    posted = client.post(
        "/api/content/text-slides", json=_upload_payload(name="A", text="A")
    ).json()
    item_id = posted["id"]
    # Default box on POST without explicit field.
    assert posted["text_layers"][0]["box"] == {
        "x": 0.1,
        "y": 0.1,
        "w": 0.8,
        "h": 0.8,
    }

    payload = _upload_payload(name="A", text="A", box={"x": 0.05, "y": 0.05, "w": 0.6, "h": 0.7})
    response = client.put(f"/api/content/text-slides/{item_id}", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["text_layers"][0]["box"] == {
        "x": 0.05,
        "y": 0.05,
        "w": 0.6,
        "h": 0.7,
    }


def test_text_slide_post_persists_motion_fields(client: TestClient):
    """Motion spec step 1+2 (commits 10e60d8 + 79df7e4): the editor
    sends motion / motion_intensity / motion_phase per text layer.
    TextLayerUpload (api.py wire model) must mirror those — without
    that mirror, Pydantic silently drops the extra fields, the route
    reconstructs the slide using TextLayer's defaults (50/0), and the
    operator's intensity/phase changes vanish on save. This is the
    regression test for that bug (qarl QA report 2026-05-02)."""
    payload = _upload_payload(
        name="P",
        text="P",
        motion="breathe",
        motion_intensity=75,
        motion_phase=0.4,
    )
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200
    layer = response.json()["text_layers"][0]
    assert layer["motion"] == "breathe"
    assert layer["motion_intensity"] == 75
    assert layer["motion_phase"] == 0.4


def test_text_slide_put_persists_motion_fields(client: TestClient):
    """Same contract on the PUT (edit-existing) route — the editor's
    autoSave path goes through PUT, so this is the route that
    actually shipped broken in 79df7e4 before the api.py fix."""
    posted = client.post(
        "/api/content/text-slides", json=_upload_payload(name="P", text="P")
    ).json()
    item_id = posted["id"]
    assert posted["text_layers"][0]["motion"] == "static"
    assert posted["text_layers"][0]["motion_intensity"] == 50
    assert posted["text_layers"][0]["motion_phase"] == 0.0

    payload = _upload_payload(
        name="P",
        text="P",
        motion="ticker",
        motion_intensity=75,
        motion_phase=0.4,
    )
    response = client.put(f"/api/content/text-slides/{item_id}", json=payload)
    assert response.status_code == 200
    layer = response.json()["text_layers"][0]
    assert layer["motion"] == "ticker"
    assert layer["motion_intensity"] == 75
    assert layer["motion_phase"] == 0.4

    # Also confirm a subsequent GET (the path QA's verifier exercises)
    # returns the round-tripped values, not stale defaults.
    fetched = client.get(f"/api/content/{item_id}").json()
    fetched_layer = fetched["text_layers"][0]
    assert fetched_layer["motion"] == "ticker"
    assert fetched_layer["motion_intensity"] == 75
    assert fetched_layer["motion_phase"] == 0.4


def test_text_slide_post_without_box_uses_model_default(client: TestClient):
    """Operator's editor that doesn't send `box` (older client, or a
    code path that legitimately omits it) gets the centered default
    rather than a 422 — exclude_none on the dump is what makes this
    work."""
    payload = _upload_payload(name="A", text="A")
    # Confidence check: helper didn't auto-inject a box on the layer.
    assert "box" not in payload["text_layers"][0]
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 200
    assert response.json()["text_layers"][0]["box"] == {
        "x": 0.1,
        "y": 0.1,
        "w": 0.8,
        "h": 0.8,
    }


def test_list_content_exposes_updated_at_for_cachebust(client: TestClient):
    """Frontend cachebust path: /api/content/{id}/asset?v={updated_at}.
    GET /api/content must return each item's storage envelope updated_at
    so a re-render bump (settings dim flip → text_rerender side-effect)
    invalidates the browser HTTP cache."""
    upload = client.post("/api/content/text-slides", json=_upload_payload(name="A", text="A"))
    assert upload.status_code == 200
    response = client.get("/api/content")
    items = response.json()
    assert items
    for item in items:
        assert "updated_at" in item
        assert item["updated_at"] is not None


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


@pytest.mark.parametrize(
    "route,payload_factory",
    [
        ("/api/content/text-slides", lambda: _upload_payload()),
        (
            "/api/content/images",
            lambda: {
                "name": "Logo",
                "image_base64": base64.b64encode(_FAKE_PNG).decode(),
            },
        ),
        (
            "/api/content/streams",
            lambda: {
                "name": "Live",
                "stream_url": "rtsp://laptop:8554/live",
                "duration_ms": 15_000,
                "on_unreachable": "black",
                "transition": "fade",
                "transition_ms": 300,
            },
        ),
        (
            "/api/content/web",
            lambda: {
                "name": "Status",
                "url": "https://status.example.com",
                "refresh_interval_s": 600,
                "duration_ms": 15_000,
                "transition": "fade",
                "transition_ms": 300,
            },
        ),
        # Video uses a separate fixture (needs the real MP4 payload) --
        # handled in a sibling test below so we don't have to import
        # _fake_mp4 at module scope here.
    ],
)
def test_upload_rolls_back_asset_on_append_to_playlist_failure(
    route: str,
    payload_factory,
    client: TestClient,
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-17 correctness regression: when the playlist append
    raises after storage.save_* succeeded, the just-written asset
    must be rolled back so the operator's UI retry doesn't end up
    with two tiles for one intended upload (list_full_library
    surfaces orphans in the pallet).

    Round-27 update: the helper moved INTO PlaylistStorage as
    append_item_to_default. Monkeypatch the method on the storage
    instance the dependency-override fixture has wired up.

    Test shape: monkeypatch append_item_to_default to raise OSError,
    POST the upload, assert 500 surfaces AND the asset is NOT on
    disk afterward (rollback worked).
    """

    def raising_append(*args, **kwargs):
        raise OSError("simulated NFS hiccup mid-playlist-save")

    monkeypatch.setattr(playlist_storage, "append_item_to_default", raising_append)

    client_no_raise = TestClient(app, raise_server_exceptions=False)
    response = client_no_raise.post(route, json=payload_factory())
    assert response.status_code == 500, (
        f"{route}: expected 500 from simulated playlist failure, got {response.status_code}"
    )

    # CRITICAL: no orphan asset on disk. Pre-fix the asset would
    # remain in storage (orphan in list_full_library) since the
    # error happened AFTER storage.save_*.
    monkeypatch.undo()
    all_items = storage.list_all()
    assert all_items == [], (
        f"{route}: expected zero stored items after rollback, got "
        f"{[type(i).__name__ for i in all_items]}"
    )


def test_upload_video_rolls_back_assets_on_append_to_playlist_failure(
    client: TestClient,
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-17 regression (video flavor): video uploads write TWO
    files (thumbnail PNG + asset MP4). Rollback must clean up the
    whole item directory, not just one file. Same shape as the
    parametrized test above, factored out because the payload
    construction needs the _video_payload helper / _fake_mp4 bytes.

    Round-27 update: same monkeypatch shift as the parametrized test
    above (helper moved INTO PlaylistStorage)."""

    def raising_append(*args, **kwargs):
        raise OSError("simulated NFS hiccup mid-playlist-save")

    monkeypatch.setattr(playlist_storage, "append_item_to_default", raising_append)

    client_no_raise = TestClient(app, raise_server_exceptions=False)
    response = client_no_raise.post("/api/content/videos", json=_video_payload())
    assert response.status_code == 500

    monkeypatch.undo()
    all_items = storage.list_all()
    assert all_items == [], (
        f"expected zero stored items after video rollback, got "
        f"{[type(i).__name__ for i in all_items]}"
    )


def test_delete_content_item_rolls_back_tombstone_on_storage_failure(
    client: TestClient,
    storage: ContentStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-16 correctness regression: if storage.delete raises mid-
    flight (NFS hiccup, etc.) after the tombstone has already been
    persisted, the rollback path MUST remove the tombstone so the
    operator's retry restarts cleanly.

    Pre-fix the tombstone stayed committed -- the local asset/envelope
    remained on disk AND peers learned `deleted` on next sync via the
    persisted tombstone. The operator's UI retry could not recover.
    """
    from openmarquee.dependencies import (
        _tombstone_storage_singleton,
        get_tombstone_storage,
    )
    from openmarquee.tombstone import TombstoneStorage

    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    _tombstone_storage_singleton.cache_clear()
    app.dependency_overrides[get_tombstone_storage] = lambda: tombstones

    try:
        upload = client.post("/api/content/text-slides", json=_upload_payload())
        item_id = UUID(upload.json()["id"])

        # Pre-condition: no tombstone for this id yet.
        pre = {t.content_id for t in tombstones.load().tombstones}
        assert item_id not in pre

        # Force storage.delete to raise mid-flight. This simulates the
        # NFS-hiccup scenario the rollback path is for.
        def raising_delete(*args, **kwargs):
            raise OSError("simulated mid-delete storage failure")

        monkeypatch.setattr(storage, "delete", raising_delete)

        # The handler raises the underlying OSError; FastAPI converts to
        # 500. (TestClient with raise_server_exceptions=True surfaces
        # the exception directly; set to False so we observe the 500.)
        client_no_raise = TestClient(app, raise_server_exceptions=False)
        response = client_no_raise.delete(f"/api/content/{item_id}")
        assert response.status_code == 500, (
            f"expected 500 from the simulated storage failure, got {response.status_code}"
        )

        # CRITICAL ASSERTION: tombstone was rolled back. Pre-fix this
        # would FAIL (tombstone was committed before the destructive
        # steps started, never removed on partial failure).
        post = {t.content_id for t in tombstones.load().tombstones}
        assert item_id not in post, (
            "tombstone must be rolled back when storage.delete fails "
            "(otherwise peers learn `deleted` while local stays stale)"
        )

        # Sanity: content still on disk since delete failed.
        monkeypatch.undo()
        assert storage.exists(item_id), (
            "content must remain on disk since the simulated delete failed"
        )
    finally:
        app.dependency_overrides.pop(get_tombstone_storage, None)
        _tombstone_storage_singleton.cache_clear()


def test_delete_content_item_keeps_tombstone_when_playlist_update_fails(
    client: TestClient,
    storage: ContentStorage,
    playlist_storage: PlaylistStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Round-28 correctness regression (the path r16 missed). When
    storage.delete SUCCEEDS but the subsequent playlist-update FAILS,
    the tombstone MUST stay on disk (don't roll it back).

    Rationale: storage.delete succeeded -> content is gone from disk
    -> peer-sync MUST learn about the deletion via the tombstone,
    otherwise peers re-propagate the content. Rolling back the
    tombstone in this path (r16's behavior) would silently lose the
    deletion: content gone + tombstone gone + dangling playlist ref +
    peer-sync NEVER hears about it.

    Operator-visible after the fix: 204 (deletion is otherwise
    complete), dangling playlist ref logged + cleaned on next prune.
    Pre-r28 (r16's single try-block): 500 + tombstone rolled back +
    peers re-propagate the content + operator's retry hits 404 (the
    handler's `if not storage.exists` short-circuit) -- dangling ref
    never cleaned.

    Test mocks playlist_storage.remove_item_from_default to raise;
    asserts 204 returned + tombstone STAYS (so peer-sync can
    propagate).
    """
    from openmarquee.dependencies import (
        _tombstone_storage_singleton,
        get_tombstone_storage,
    )
    from openmarquee.tombstone import TombstoneStorage

    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    _tombstone_storage_singleton.cache_clear()
    app.dependency_overrides[get_tombstone_storage] = lambda: tombstones

    try:
        upload = client.post("/api/content/text-slides", json=_upload_payload())
        item_id = UUID(upload.json()["id"])
        assert storage.exists(item_id)

        # Pre-condition: no tombstone for this id yet.
        pre = {t.content_id for t in tombstones.load().tombstones}
        assert item_id not in pre

        # Force playlist-update to raise mid-flight. storage.delete
        # will succeed first; then this fires.
        def raising_remove(item_id_arg):
            raise OSError("simulated NFS hiccup on playlist update")

        monkeypatch.setattr(playlist_storage, "remove_item_from_default", raising_remove)

        response = client.delete(f"/api/content/{item_id}")
        # r28 contract: 204 because the deletion is otherwise complete
        # (content gone + tombstone set + peers will learn). The
        # dangling playlist ref is recoverable on next prune.
        assert response.status_code == 204, (
            f"expected 204 (deletion otherwise complete), got {response.status_code}"
        )

        # CRITICAL ASSERTION: tombstone STAYS so peer-sync propagates
        # the deletion. Pre-r28 (r16's single try-block) the rollback
        # would have removed the tombstone here -- content gone,
        # tombstone gone, peer-sync silently loses the deletion.
        post = {t.content_id for t in tombstones.load().tombstones}
        assert item_id in post, (
            "tombstone must STAY when only playlist-update fails "
            "(content is already gone; peer-sync needs the tombstone "
            "to propagate the deletion). Pre-r28 the tombstone got "
            "rolled back here and the deletion was silently lost."
        )

        # Sanity: content really is gone from disk.
        assert not storage.exists(item_id), (
            "storage.delete should have succeeded before the playlist-update failure"
        )
    finally:
        app.dependency_overrides.pop(get_tombstone_storage, None)
        _tombstone_storage_singleton.cache_clear()


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
    client.put(
        f"/api/playlists/{DEFAULT_PLAYLIST_ID}",
        json={"item_ids": [str(c), str(b), str(a)]},
    )

    response = client.get("/api/content").json()
    assert [item["name"] for item in response] == ["C", "B", "A"]


# --- video upload ---


def _mp4_box(tag: bytes, payload: bytes = b"") -> bytes:
    """Build a single MP4 box: 4-byte big-endian size + 4-byte tag + payload."""
    size = 8 + len(payload)
    return size.to_bytes(4, "big") + tag + payload


def _trak_box(handler: bytes = b"vide") -> bytes:
    """A `trak` box carrying the mdia/hdlr structure the upload
    validator walks. `handler` is the 4-byte hdlr handler_type:
    `b"vide"` for a video trak, `b"soun"` for audio. See
    `_count_video_traks_in_mp4` in api.py.
    """
    # hdlr body: 1-byte version + 3-byte flags + 4-byte pre_defined
    # + 4-byte handler_type. 12 bytes is the minimum the validator
    # (and the rust demuxer) require.
    hdlr = _mp4_box(b"hdlr", b"\x00" * 8 + handler)
    return _mp4_box(b"trak", _mp4_box(b"mdia", hdlr))


def _fake_mp4(handlers: tuple[bytes, ...] = (b"vide",)) -> bytes:
    """Smallest valid MP4 byte stream that passes the ftyp +
    video-trak upload checks. Default is a single video trak (the
    minimal playable shape). Pass e.g. `(b"vide", b"soun")` for a
    video+audio multi-trak file, or `(b"soun",)` / `()` to build
    rejection fixtures.

    Layout:
      [ftyp box: 'isom' major brand, minor version 0]
      [moov box: one trak per entry in `handlers`, each
       trak -> mdia -> hdlr advertising that handler_type]
    """
    ftyp = _mp4_box(b"ftyp", b"isom" + b"\x00\x00\x00\x00")
    moov_payload = b"".join(_trak_box(h) for h in handlers)
    moov = _mp4_box(b"moov", moov_payload)
    return ftyp + moov


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


def test_post_video_accepts_video_plus_audio_mp4(client: TestClient):
    """Multi-trak (2026-05-20): a video+audio MP4 is now fully
    supported — the rust demuxer's select_video_mdia picks the
    video trak and ignores the audio. The upload gate must accept
    it (it previously rejected any non-single-trak file). The
    'coffee' clip qarl tried failed exactly here."""
    payload = _video_payload(
        mp4_base64=base64.b64encode(_fake_mp4((b"vide", b"soun"))).decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 200, response.json()


def test_post_video_rejects_audio_only_mp4(client: TestClient):
    """An MP4 with traks but no 'vide'-handler trak (e.g. an
    audio-only file) has nothing to play and is rejected — the
    rust select_video_mdia would bail with 'no video trak'."""
    payload = _video_payload(
        mp4_base64=base64.b64encode(_fake_mp4((b"soun", b"soun"))).decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "video trak" in detail.lower(), f"detail: {detail!r}"


def test_post_video_rejects_zero_trak_mp4(client: TestClient):
    """An MP4 with a valid ftyp + moov but zero trak children is
    rejected (no video trak at all). Less common than the audio-
    only case but still a degenerate input the rust would fail on
    at runtime."""
    payload = _video_payload(
        mp4_base64=base64.b64encode(_fake_mp4(())).decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "video trak" in detail.lower()


def test_post_video_rejects_malformed_box_structure(client: TestClient):
    """Bug 8 / Fix C: a file that passes the ftyp check but has
    truncated / corrupt box headers (no recoverable moov) gets a
    distinct error message pointing operators at re-export."""
    # ftyp followed by garbage bytes — the box walker hits an invalid
    # size field and returns -1 (malformed).
    payload = _video_payload(
        mp4_base64=base64.b64encode(
            _mp4_box(b"ftyp", b"isom" + b"\x00\x00\x00\x00")
            + b"\x00\x00\x00\x01garbg"  # size=1 (below 8 → malformed)
        ).decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "malformed" in detail.lower() or "no video trak" in detail.lower()


def test_post_video_accepts_single_trak_mp4(client: TestClient):
    """A synthetic single-video-trak MP4 passes validation. Locks
    the happy-path for the upload check."""
    payload = _video_payload(
        mp4_base64=base64.b64encode(_fake_mp4((b"vide",))).decode("ascii"),
    )
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 200, response.json()


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
    # The new MP4 must carry a video trak to pass upload validation.
    # The 2 trailing bytes (< an 8-byte box header) are ignored by
    # the box walker but make the bytes differ from the original
    # payload so the round-trip read_video() assertion still proves
    # replacement.
    new_mp4 = _fake_mp4() + b"\xab\xcd"

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


# --- stream (STREAM/VLC slice 8) -------------------------------------------


def test_post_stream_creates_slide_and_appends_to_playlist(
    client: TestClient, playlist_storage: PlaylistStorage
):
    response = client.post(
        "/api/content/streams",
        json={
            "name": "Q3 Live",
            "stream_url": "rtsp://laptop:8554/live",
            "duration_ms": 15_000,
            "on_unreachable": "black",
            "transition": "fade",
            "transition_ms": 300,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "stream"
    assert body["stream_url"] == "rtsp://laptop:8554/live"
    assert body["on_unreachable"] == "black"
    # Appended to the default playlist like every other slide type.
    assert UUID(body["id"]) in playlist_storage.load().item_ids


def test_post_stream_uses_defaults_for_omitted_fields(client: TestClient):
    response = client.post(
        "/api/content/streams",
        json={"name": "Minimal", "stream_url": "rtsp://h:8554/x"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["duration_ms"] == 10_000
    assert body["on_unreachable"] == "hold_last_frame"
    assert body["transition"] == "cut"


def test_post_stream_rejects_bad_on_unreachable(client: TestClient):
    """An invalid on_unreachable value is caught by the StreamSlide
    model and surfaced as a 422 (not a 500)."""
    response = client.post(
        "/api/content/streams",
        json={
            "name": "Bad",
            "stream_url": "rtsp://h/x",
            "on_unreachable": "explode",
        },
    )
    assert response.status_code == 422


def test_get_stream_thumbnail_is_a_png(client: TestClient):
    """The synthetic 'stream' card is reachable via the standard
    asset endpoint so the editor tile renders like any other slide."""
    post = client.post(
        "/api/content/streams",
        json={"name": "Live", "stream_url": "rtsp://h:8554/x"},
    )
    item_id = post.json()["id"]
    asset = client.get(f"/api/content/{item_id}/asset")
    assert asset.status_code == 200
    assert asset.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_put_stream_updates_metadata_preserving_id(client: TestClient):
    post = client.post(
        "/api/content/streams",
        json={"name": "Before", "stream_url": "rtsp://h:8554/old"},
    )
    item_id = post.json()["id"]
    response = client.put(
        f"/api/content/streams/{item_id}",
        json={
            "name": "After",
            "stream_url": "rtsp://h:8554/new",
            "duration_ms": 20_000,
            "on_unreachable": "skip",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == item_id  # UUID preserved
    assert body["name"] == "After"
    assert body["stream_url"] == "rtsp://h:8554/new"
    assert body["on_unreachable"] == "skip"


def test_put_stream_wrong_type_returns_409(client: TestClient):
    """Updating a non-stream id via the streams route is a 409."""
    text = client.post(
        "/api/content/text-slides",
        json=_upload_payload(name="a-text-slide"),
    )
    text_id = text.json()["id"]
    response = client.put(
        f"/api/content/streams/{text_id}",
        json={"name": "x", "stream_url": "rtsp://h/x"},
    )
    assert response.status_code == 409


def test_put_stream_unknown_id_returns_404(client: TestClient):
    response = client.put(
        f"/api/content/streams/{uuid4()}",
        json={"name": "ghost", "stream_url": "rtsp://h/x"},
    )
    assert response.status_code == 404


# --- web (Web slide P1) ----------------------------------------------------


def test_post_web_creates_slide_and_appends_to_playlist(
    client: TestClient, playlist_storage: PlaylistStorage
):
    response = client.post(
        "/api/content/web",
        json={
            "name": "Status Page",
            "url": "https://status.example.com",
            "refresh_interval_s": 600,
            "duration_ms": 15_000,
            "transition": "fade",
            "transition_ms": 300,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "web"
    assert body["url"] == "https://status.example.com"
    assert body["refresh_interval_s"] == 600
    # Appended to the default playlist like every other slide type.
    assert UUID(body["id"]) in playlist_storage.load().item_ids


def test_post_web_uses_defaults_for_omitted_fields(client: TestClient):
    response = client.post(
        "/api/content/web",
        json={"name": "Minimal", "url": "https://h/x"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refresh_interval_s"] == 3600
    assert body["duration_ms"] == 10_000
    assert body["transition"] == "cut"


def test_post_web_rejects_non_http_url(client: TestClient):
    """A file:// url is operator-supplied and never a valid web page —
    rejected with a clean 400 (not a 422 or a 500)."""
    response = client.post(
        "/api/content/web",
        json={"name": "Bad", "url": "file:///etc/passwd"},
    )
    assert response.status_code == 400


def test_get_web_placeholder_is_a_png(client: TestClient):
    """The synthetic placeholder card is reachable via the standard
    asset endpoint before the first screenshot arrives."""
    post = client.post(
        "/api/content/web",
        json={"name": "Status", "url": "https://h/x"},
    )
    item_id = post.json()["id"]
    asset = client.get(f"/api/content/{item_id}/asset")
    assert asset.status_code == 200
    assert asset.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_put_web_updates_metadata_preserving_id(client: TestClient):
    post = client.post(
        "/api/content/web",
        json={"name": "Before", "url": "https://h/old"},
    )
    item_id = post.json()["id"]
    response = client.put(
        f"/api/content/web/{item_id}",
        json={
            "name": "After",
            "url": "https://h/new",
            "refresh_interval_s": 120,
            "duration_ms": 20_000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == item_id  # UUID preserved
    assert body["name"] == "After"
    assert body["url"] == "https://h/new"
    assert body["refresh_interval_s"] == 120


def test_put_web_rejects_non_http_url(client: TestClient):
    post = client.post(
        "/api/content/web",
        json={"name": "Status", "url": "https://h/x"},
    )
    item_id = post.json()["id"]
    response = client.put(
        f"/api/content/web/{item_id}",
        json={"name": "x", "url": "ftp://h/x"},
    )
    assert response.status_code == 400


def test_put_web_wrong_type_returns_409(client: TestClient):
    """Updating a non-web id via the web route is a 409."""
    text = client.post(
        "/api/content/text-slides",
        json=_upload_payload(name="a-text-slide"),
    )
    text_id = text.json()["id"]
    response = client.put(
        f"/api/content/web/{text_id}",
        json={"name": "x", "url": "https://h/x"},
    )
    assert response.status_code == 409


def test_put_web_unknown_id_returns_404(client: TestClient):
    response = client.put(
        f"/api/content/web/{uuid4()}",
        json={"name": "ghost", "url": "https://h/x"},
    )
    assert response.status_code == 404


# --- web (Bug W1): immediate screenshot kick on create / url change --------


def test_post_web_kicks_an_immediate_screenshot_fetch(client: TestClient):
    """Bug W1: creating a Web slide kicks an immediate screenshot fetch
    so the thumbnail/preview populate promptly instead of waiting for
    the first playback slot. Mock the kicker; assert it ran for the
    created slide."""
    from openmarquee.dependencies import get_web_screenshot_kicker

    kicked: list = []
    app.dependency_overrides[get_web_screenshot_kicker] = lambda: kicked.append
    response = client.post(
        "/api/content/web",
        json={"name": "Status", "url": "https://h/x"},
    )
    assert response.status_code == 200
    # The kicker was invoked exactly once, with the created WebSlide.
    assert len(kicked) == 1
    assert str(kicked[0].id) == response.json()["id"]
    assert kicked[0].url == "https://h/x"


def test_post_web_does_not_block_on_the_screenshot_fetch(client: TestClient):
    """CRITICAL (mirrors test_playback's "slot does not await the
    fetch"): the create response must NOT block on the screenshot
    fetch. A kicker that launches a hanging fetch task must not delay
    the POST response."""
    from openmarquee.dependencies import get_web_screenshot_kicker

    async def _hangs_forever(_slide) -> bool:
        await asyncio.sleep(3600)
        return True

    launched: list = []

    def kicker(slide) -> None:
        # Fire-and-forget a task that never finishes — exactly what a
        # slow render helper would look like. The route must return
        # without awaiting it.
        launched.append(asyncio.ensure_future(_hangs_forever(slide)))

    app.dependency_overrides[get_web_screenshot_kicker] = lambda: kicker
    try:
        response = client.post(
            "/api/content/web",
            json={"name": "Slow", "url": "https://h/slow"},
        )
        # The POST returned promptly despite the hanging fetch.
        assert response.status_code == 200
        assert len(launched) == 1
        assert not launched[0].done()  # still hanging — was not awaited
    finally:
        # Cancel the dangling task so it doesn't leak past the test.
        for task in launched:
            task.cancel()


def test_post_web_succeeds_even_when_the_kick_producer_fails(
    client: TestClient,
):
    """Bug W1 edge: if the render helper is unreachable at create time
    the producer fails internally — creation must NOT fail. The
    fire-and-forget kick must not surface a producer failure as a 500
    or an unhandled task exception."""
    from openmarquee.dependencies import get_web_screenshot_kicker

    async def _failing_producer(_slide) -> bool:
        raise RuntimeError("render helper unreachable")

    crashed: list = []

    def kicker(slide) -> None:
        # Mirror the playback loop's fire-and-forget + done-callback:
        # a producer that raises is consumed by the done-callback so it
        # never surfaces as an unretrieved-exception warning.
        task = asyncio.ensure_future(_failing_producer(slide))

        def _on_done(t: "asyncio.Task") -> None:
            if not t.cancelled() and t.exception() is not None:
                crashed.append(t.exception())

        task.add_done_callback(_on_done)

    app.dependency_overrides[get_web_screenshot_kicker] = lambda: kicker
    response = client.post(
        "/api/content/web",
        json={"name": "Unreachable", "url": "https://h/down"},
    )
    # Creation succeeded despite the producer failing.
    assert response.status_code == 200
    item_id = response.json()["id"]
    # The slide really exists and is fetchable.
    assert client.get(f"/api/content/{item_id}").status_code == 200
    # The producer failure was consumed by the done-callback, not
    # raised — belt-and-suspenders proof it can't crash the request.
    assert len(crashed) == 1


def test_put_web_url_change_kicks_a_screenshot_fetch(client: TestClient):
    """Bug W1: changing a Web slide's url re-shoots the screenshot so
    the thumbnail reflects the new page."""
    from openmarquee.dependencies import get_web_screenshot_kicker

    post = client.post(
        "/api/content/web",
        json={"name": "Before", "url": "https://h/old"},
    )
    item_id = post.json()["id"]

    kicked: list = []
    app.dependency_overrides[get_web_screenshot_kicker] = lambda: kicked.append
    response = client.put(
        f"/api/content/web/{item_id}",
        json={"name": "Before", "url": "https://h/new"},
    )
    assert response.status_code == 200
    # The url changed — a re-shot was kicked for the updated slide.
    assert len(kicked) == 1
    assert kicked[0].url == "https://h/new"
    assert str(kicked[0].id) == item_id


def test_put_web_metadata_only_edit_does_not_kick_a_fetch(client: TestClient):
    """Bug W1: a PUT that changes only name/duration (url unchanged)
    must NOT re-shoot — the existing screenshot is still valid."""
    from openmarquee.dependencies import get_web_screenshot_kicker

    post = client.post(
        "/api/content/web",
        json={"name": "Before", "url": "https://h/same"},
    )
    item_id = post.json()["id"]

    kicked: list = []
    app.dependency_overrides[get_web_screenshot_kicker] = lambda: kicked.append
    response = client.put(
        f"/api/content/web/{item_id}",
        json={
            "name": "After",
            "url": "https://h/same",  # unchanged
            "duration_ms": 20_000,
        },
    )
    assert response.status_code == 200
    # url unchanged — no re-shot.
    assert kicked == []


# ---- 2026-05-25 Bundle B item 9: per-route body caps ----
#
# Pydantic Field(max_length=N) on the base64 string field rejects
# oversize uploads with 422 at validation time, bounding RAM
# allocation on a hostile multi-MB POST. Caps are sized per content
# type: text-slide PNGs (~14 MB base64), video MP4s (~270 MB
# base64), image PNGs/JPGs (~27 MB base64). Generous enough that
# no legitimate operator upload hits the cap, tight enough that a
# malicious LAN/tailnet POST can't blow RAM via unauth-write surface.


def test_upload_text_slide_rejects_oversize_png_base64(client: TestClient):
    """A png_base64 string above the 14 MB Pydantic cap must 422 at
    validation, BEFORE the handler tries to b64decode + open as PNG.
    Sized at 14.5 MB so it passes the OUTER ASGI body-cap (15 MB,
    Bundle B2) but trips the INNER Pydantic Field max_length. This
    pins the inner gate; B2's body-cap tests pin the outer."""
    payload = _upload_payload(name="Oversize")
    payload["png_base64"] = "A" * 14_500_000
    response = client.post("/api/content/text-slides", json=payload)
    assert response.status_code == 422
    # The Pydantic error must reference the field so operators
    # debugging an "upload failed" message can find the culprit.
    body_text = response.text
    assert "png_base64" in body_text, (
        f"422 detail should reference png_base64 field; got: {body_text[:300]}"
    )


def test_upload_text_slide_accepts_payload_under_cap(client: TestClient):
    """Belt-and-suspenders: a payload comfortably under the 14 MB
    cap still works. Without this, a future tightening of the cap
    that broke real uploads would only surface via the operator
    noticing -- this test fences the floor."""
    response = client.post("/api/content/text-slides", json=_upload_payload(name="Normal"))
    assert response.status_code == 200, response.text


def test_upload_image_rejects_oversize_image_base64(client: TestClient):
    """ImageSlide image_base64 Pydantic cap is 27 MB. Sized at 27.5
    MB to pass the OUTER ASGI body-cap (28 MB, Bundle B2) but trip
    the INNER Pydantic Field max_length."""
    payload = {
        "name": "OversizeImage",
        "image_base64": "A" * 27_500_000,
    }
    response = client.post("/api/content/images", json=payload)
    assert response.status_code == 422
    assert "image_base64" in response.text


def test_upload_video_rejects_oversize_mp4_base64(client: TestClient):
    """VideoSlide mp4_base64 Pydantic cap is 270 MB. Sized at 275 MB
    to pass the OUTER ASGI body-cap (290 MB, Bundle B2) but trip the
    INNER Pydantic Field max_length."""
    payload = {
        "name": "OversizeVideo",
        "png_base64": base64.b64encode(_FAKE_PNG).decode(),
        "mp4_base64": "A" * 275_000_000,
    }
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 422
    assert "mp4_base64" in response.text


def test_upload_video_rejects_oversize_thumbnail_png(client: TestClient):
    """VideoSlide.png_base64 (thumbnail) shares the same 14 MB
    Pydantic cap as TextSlide.png_base64. Sized at 14.5 MB to pass
    the OUTER body-cap (290 MB for videos, plenty of room) but trip
    the INNER Pydantic Field max_length."""
    payload = {
        "name": "OversizeThumbnail",
        "png_base64": "A" * 14_500_000,
        "mp4_base64": "AAAA",  # bogus but small (test isolates png_base64 cap)
    }
    response = client.post("/api/content/videos", json=payload)
    assert response.status_code == 422
    assert "png_base64" in response.text
