import json
import os
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from openmarquee.content import (
    ImageSlide,
    StreamSlide,
    TextLayer,
    TextSlide,
    VideoSlide,
    WebSlide,
)
from openmarquee.content.storage import (
    SCHEMA_VERSION,
    ContentStorage,
    _migrate_legacy_stream_item,
)


def _make_slide(**overrides) -> TextSlide:
    """Helper: builds a single-layer TextSlide for storage tests. Schema v3
    routed text fields off the slide root into text_layers — accept the
    flat kwargs the tests were already using and shuttle them into the
    canonical layer."""
    layer_keys = {
        "text", "text_color", "font_family", "font_size_px",
        "font_size_pct", "auto_mode", "auto_format", "box",
    }
    layer = {"text": overrides.pop("text", "Hello, world")}
    for k in list(overrides.keys()):
        if k in layer_keys:
            layer[k] = overrides.pop(k)
    return TextSlide(
        name=overrides.pop("name", "Test Slide"),
        text_layers=[TextLayer(**layer)],
        **overrides,
    )


def test_storage_creates_root_directory(tmp_path: Path):
    root = tmp_path / "content"
    assert not root.exists()
    ContentStorage(root)
    assert root.is_dir()


def test_save_then_load_round_trips_text_slide(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide(name="Specials", text="Pulled Pork $8.99", duration_ms=3000)
    storage.save_text_slide(slide, png=b"\x89PNG\r\nfake")

    loaded = storage.load(slide.id)
    # load() populates updated_at from the envelope (output-only mirror,
    # added 2026-04-30 for frontend cachebust). Compare ignoring it.
    assert loaded.model_copy(update={"updated_at": None}) == slide
    assert loaded.updated_at is not None


def test_read_asset_returns_exact_bytes(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    png = b"\x89PNG\r\n" + bytes(range(256))
    storage.save_text_slide(slide, png)

    assert storage.read_asset(slide.id) == png


def test_asset_path_points_at_png(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")
    assert storage.asset_path(slide.id) == tmp_path / str(slide.id) / "asset.png"
    assert storage.asset_path(slide.id).exists()


def test_save_overwrites_existing_item(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide_v1 = _make_slide(name="v1", text="first")
    storage.save_text_slide(slide_v1, b"\x89PNG\r\nv1")

    slide_v2 = TextSlide(
        id=slide_v1.id,
        name="v2",
        text_layers=[TextLayer(text="second")],
    )
    storage.save_text_slide(slide_v2, b"\x89PNG\r\nv2")

    loaded = storage.load(slide_v1.id)
    assert loaded.name == "v2"
    assert loaded.text_layers[0].text == "second"
    assert storage.read_asset(slide_v1.id) == b"\x89PNG\r\nv2"


def test_load_missing_raises_file_not_found(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage.load(uuid4())


def test_read_asset_missing_raises_file_not_found(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage.read_asset(uuid4())


def test_read_asset_missing_after_envelope_raises_file_not_found(tmp_path: Path):
    """An item whose envelope exists but whose asset was manually removed
    should raise — don't silently return an empty PNG."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")
    (tmp_path / str(slide.id) / "asset.png").unlink()

    with pytest.raises(FileNotFoundError):
        storage.read_asset(slide.id)


def test_exists_true_after_save(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    assert not storage.exists(slide.id)
    storage.save_text_slide(slide, b"\x89PNG")
    assert storage.exists(slide.id)


def test_exists_false_after_delete(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")
    storage.delete(slide.id)
    assert not storage.exists(slide.id)


def test_list_all_empty_when_no_items(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    assert storage.list_all() == []


def test_list_all_empty_when_root_missing_at_runtime(tmp_path: Path):
    """SD card swap, manual cleanup, e2e reset — root can vanish under us."""
    import shutil

    storage = ContentStorage(tmp_path)
    shutil.rmtree(tmp_path)
    assert storage.list_all() == []


def test_list_all_returns_all_saved_items(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide_a = _make_slide(name="a", text="a")
    slide_b = _make_slide(name="b", text="b")
    storage.save_text_slide(slide_a, b"\x89PNG_a")
    storage.save_text_slide(slide_b, b"\x89PNG_b")

    ids = {item.id for item in storage.list_all()}
    assert ids == {slide_a.id, slide_b.id}


def test_list_all_ignores_non_uuid_subdirs(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    # An editor-scratch dir or stray file should not break list_all.
    (tmp_path / "scratch").mkdir()
    (tmp_path / ".DS_Store").write_text("")

    items = storage.list_all()
    assert len(items) == 1
    assert items[0].id == slide.id


def test_list_all_ignores_uuid_dirs_without_envelope(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    orphan_dir = tmp_path / str(uuid4())
    orphan_dir.mkdir()
    assert storage.list_all() == []


def test_delete_removes_item(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    storage.delete(slide.id)

    assert not (tmp_path / str(slide.id)).exists()
    with pytest.raises(FileNotFoundError):
        storage.load(slide.id)


def test_delete_missing_raises_file_not_found(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage.delete(uuid4())


def test_envelope_contains_schema_version(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    envelope = json.loads((tmp_path / str(slide.id) / "item.json").read_text())
    assert envelope["schema_version"] == SCHEMA_VERSION
    assert envelope["item"]["type"] == "text_slide"


def test_load_rejects_wrong_schema_version(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    # Tamper: bump the schema version on disk to something future.
    envelope_path = tmp_path / str(slide.id) / "item.json"
    envelope = json.loads(envelope_path.read_text())
    envelope["schema_version"] = SCHEMA_VERSION + 99
    envelope_path.write_text(json.dumps(envelope))

    with pytest.raises(ValueError, match="schema_version"):
        storage.load(slide.id)


def test_load_migrates_legacy_motion_scroll_to_ticker(tmp_path: Path):
    """Cross-module contract for the additive motion migration
    (docs/text-layer-motion-spec.md, step 1): a v3 envelope on disk
    with the legacy `motion="scroll"` value loads as a TextLayer with
    `motion="ticker"`. No SCHEMA_VERSION bump — the field_validator
    on TextLayer.motion handles the rename in-place. The next save()
    on the loaded item drains the disk value to "ticker" too."""
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    # Hand-tamper the envelope to simulate an old item written before
    # the rename — set the layer's motion to the legacy "scroll" value
    # and strip the two new fields so the test also confirms defaults
    # populate on load (the additive part of the migration).
    envelope_path = tmp_path / str(slide.id) / "item.json"
    envelope = json.loads(envelope_path.read_text())
    layer = envelope["item"]["text_layers"][0]
    layer["motion"] = "scroll"
    layer.pop("motion_intensity", None)
    layer.pop("motion_phase", None)
    envelope_path.write_text(json.dumps(envelope))

    loaded = storage.load(slide.id)
    assert loaded.text_layers[0].motion == "ticker"
    assert loaded.text_layers[0].motion_intensity == 50
    assert loaded.text_layers[0].motion_phase == 0.0

    # Saving the loaded item drains the rename to disk: the on-disk
    # envelope's layer.motion becomes "ticker".
    storage.save_text_slide(loaded, b"\x89PNG")
    after = json.loads(envelope_path.read_text())
    assert after["item"]["text_layers"][0]["motion"] == "ticker"


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")

    item_dir = tmp_path / str(slide.id)
    tmp_files = list(item_dir.glob("*.tmp"))
    assert tmp_files == []


# --- Image slides: round-trip and union dispatch ---


def test_save_and_load_image_round_trip(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    image = ImageSlide(name="Logo", duration_ms=3000)
    png = b"\x89PNG" + b"image-payload"
    storage.save_image(image, png)

    loaded = storage.load(image.id)
    assert isinstance(loaded, ImageSlide)
    assert loaded.model_copy(update={"updated_at": None}) == image
    assert loaded.updated_at is not None
    assert storage.read_asset(image.id) == png


def test_list_all_returns_mixed_variants(tmp_path: Path):
    """Text slides and image slides in the same list, each as the right type."""
    storage = ContentStorage(tmp_path)
    text = TextSlide(name="t", text="t")
    image = ImageSlide(name="i")
    storage.save_text_slide(text, b"\x89PNG_text")
    storage.save_image(image, b"\x89PNG_image")

    items = storage.list_all()
    assert len(items) == 2
    by_type = {item.type: item for item in items}
    assert isinstance(by_type["text_slide"], TextSlide)
    assert isinstance(by_type["image"], ImageSlide)


def test_save_generic_dispatches_to_correct_type(tmp_path: Path):
    """storage.save(item, png) works without knowing the variant up front."""
    storage = ContentStorage(tmp_path)
    text = TextSlide(name="t", text="t")
    image = ImageSlide(name="i")
    storage.save(text, b"text-png")
    storage.save(image, b"image-png")
    assert isinstance(storage.load(text.id), TextSlide)
    assert isinstance(storage.load(image.id), ImageSlide)


# --- video ---


_FAKE_MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 120


def test_save_video_writes_thumbnail_and_mp4_side_by_side(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    video = VideoSlide(name="Promo")
    storage.save_video(video, thumbnail_png=b"\x89PNG_thumb", video_bytes=_FAKE_MP4)

    # Thumbnail reachable via the existing asset path.
    assert storage.asset_path(video.id).read_bytes() == b"\x89PNG_thumb"
    # Video reachable via the new video path.
    assert storage.video_path(video.id) == tmp_path / str(video.id) / "asset.mp4"
    assert storage.read_video(video.id) == _FAKE_MP4


def test_load_roundtrips_video_slide(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    video = VideoSlide(name="Promo", transition="fade", transition_ms=300)
    storage.save_video(video, thumbnail_png=b"\x89PNG", video_bytes=_FAKE_MP4)
    loaded = storage.load(video.id)
    assert isinstance(loaded, VideoSlide)
    assert loaded.model_copy(update={"updated_at": None}) == video
    assert loaded.updated_at is not None


def test_save_video_rolls_back_on_partial_failure(tmp_path: Path, monkeypatch):
    """If the MP4 write blows up mid-save, the whole item dir is removed
    so list_all() doesn't return an envelope whose /video endpoint 404s."""
    storage = ContentStorage(tmp_path)
    video = VideoSlide(name="Promo")

    # First call writes the thumbnail fine; second call (the mp4) raises.
    original = ContentStorage._atomic_write_bytes
    call_count = {"n": 0}

    def flaky(path, content):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise OSError("disk full")
        original(path, content)

    monkeypatch.setattr(ContentStorage, "_atomic_write_bytes", staticmethod(flaky))

    with pytest.raises(OSError):
        storage.save_video(video, b"\x89PNG", _FAKE_MP4)

    # Nothing lingers — list_all() treats this as "no such item."
    assert not (tmp_path / str(video.id)).exists()
    assert storage.list_all() == []


def test_list_all_surfaces_video_items(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    text = TextSlide(name="t", text="t")
    video = VideoSlide(name="v")
    storage.save_text_slide(text, b"\x89PNG_text")
    storage.save_video(video, b"\x89PNG_thumb", _FAKE_MP4)

    items = storage.list_all()
    by_type = {item.type: item for item in items}
    assert isinstance(by_type["text_slide"], TextSlide)
    assert isinstance(by_type["video"], VideoSlide)


def test_read_video_missing_raises(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    with pytest.raises(FileNotFoundError):
        storage.read_video(uuid4())


def test_delete_removes_video_dir_including_mp4(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    video = VideoSlide(name="Promo")
    storage.save_video(video, b"\x89PNG", _FAKE_MP4)
    storage.delete(video.id)
    assert not storage.video_path(video.id).exists()


def test_save_stamps_envelope_with_updated_at(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    storage.save_text_slide(slide, b"\x89PNG")
    stamp = storage.read_updated_at(slide.id)
    # Tz-aware and recent (within a few seconds of now).
    assert stamp.tzinfo is not None
    assert (datetime.now(UTC) - stamp).total_seconds() < 5


def test_save_accepts_explicit_updated_at_for_peer_ingest(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    fixed = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    storage.save(slide, b"\x89PNG", updated_at=fixed)
    assert storage.read_updated_at(slide.id) == fixed


def test_read_updated_at_falls_back_to_mtime_for_pre_flock_envelopes(tmp_path: Path):
    # Emulate an envelope persisted before the updated_at field was added.
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    item_dir = tmp_path / str(slide.id)
    item_dir.mkdir()
    envelope_path = item_dir / "item.json"
    envelope_path.write_text(
        json.dumps(
            {"schema_version": SCHEMA_VERSION, "item": slide.model_dump(mode="json")}
        )
    )
    (item_dir / "asset.png").write_bytes(b"\x89PNG")
    # Force a known mtime so the assertion is deterministic.
    epoch = 1700000000
    os.utime(envelope_path, (epoch, epoch))
    stamp = storage.read_updated_at(slide.id)
    assert stamp == datetime.fromtimestamp(epoch, tz=UTC)


# --- Batch 7.1: mtime cache tests ---


def test_list_all_reuses_cache_for_unchanged_envelopes(tmp_path: Path):
    """First list_all parses all 3 envelopes; second list_all is a
    pure stat-and-reuse path -- envelope_validates counter must not
    move on the cache-hit pass."""
    storage = ContentStorage(tmp_path / "content")
    storage.save(_make_slide(text="One"), b"\x89PNG-1")
    storage.save(_make_slide(text="Two"), b"\x89PNG-2")
    storage.save(_make_slide(text="Three"), b"\x89PNG-3")
    # Reset the class-level counter so the assertion delta is clean.
    ContentStorage._stats["envelope_validates"] = 0
    storage.list_all()  # cold path -> 3 validates
    assert ContentStorage._stats["envelope_validates"] == 3
    storage.list_all()  # warm path -> 0 validates
    assert ContentStorage._stats["envelope_validates"] == 3


def test_list_all_revalidates_only_changed_envelopes(tmp_path: Path):
    """Edit one item; next list_all should re-validate exactly that
    one (the other two are still cached by unchanged mtime)."""
    storage = ContentStorage(tmp_path / "content")
    a = _make_slide(text="Alpha")
    b = _make_slide(text="Bravo")
    c = _make_slide(text="Charlie")
    storage.save(a, b"PNG-A")
    storage.save(b, b"PNG-B")
    storage.save(c, b"PNG-C")
    storage.list_all()  # warm the cache
    ContentStorage._stats["envelope_validates"] = 0
    # Edit just `b` -- save() invalidates only its cache entry.
    storage.save(_make_slide(id=b.id, text="Bravo-edited"), b"PNG-B2")
    storage.list_all()
    assert ContentStorage._stats["envelope_validates"] == 1


def test_save_invalidates_cache_entry(tmp_path: Path):
    """After save() with same id + different content, the next load
    must return the new content -- not the stale cached value."""
    storage = ContentStorage(tmp_path / "content")
    original = _make_slide(text="Original")
    storage.save(original, b"PNG-1")
    storage.list_all()  # populate cache
    storage.save(_make_slide(id=original.id, text="Rewritten"), b"PNG-2")
    # Re-load via list_all path; the cached "Original" must be gone.
    fresh = storage.load(original.id)
    assert fresh.text_layers[0].text == "Rewritten"


def test_delete_invalidates_cache_entry(tmp_path: Path):
    """After delete(), a subsequent load() of the same id must raise
    FileNotFoundError -- not return the stale cached value."""
    storage = ContentStorage(tmp_path / "content")
    slide = _make_slide(text="Doomed")
    storage.save(slide, b"PNG")
    storage.list_all()  # populate cache
    storage.delete(slide.id)
    with pytest.raises(FileNotFoundError):
        storage.load(slide.id)


# --- stream (STREAM/VLC slice 6) -------------------------------------------


def test_save_stream_writes_envelope_and_placeholder(tmp_path: Path):
    """save_stream generates a synthetic 'stream' thumbnail card (the
    slide carries no operator-supplied image) and persists it as the
    standard asset.png."""
    storage = ContentStorage(tmp_path)
    slide = StreamSlide(name="Q3 Live", stream_url="rtsp://laptop:8554/live")
    storage.save_stream(slide)

    png = storage.asset_path(slide.id).read_bytes()
    # A valid PNG (signature) decodable to the placeholder card dims.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(BytesIO(png)).size == (640, 360)


def test_load_roundtrips_stream_slide(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = StreamSlide(
        name="Q3 Live",
        stream_url="rtsp://laptop:8554/live",
        duration_ms=15_000,
        on_unreachable="black",
        transition="fade",
        transition_ms=300,
    )
    storage.save_stream(slide)
    loaded = storage.load(slide.id)
    assert isinstance(loaded, StreamSlide)
    assert loaded.model_copy(update={"updated_at": None}) == slide
    assert loaded.updated_at is not None


def test_list_all_surfaces_stream_items(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    text = TextSlide(name="t", text="t")
    stream = StreamSlide(name="v", stream_url="rtsp://h:8554/x")
    storage.save_text_slide(text, b"\x89PNG_text")
    storage.save_stream(stream)

    by_type = {item.type: item for item in storage.list_all()}
    assert isinstance(by_type["stream"], StreamSlide)
    assert isinstance(by_type["text_slide"], TextSlide)


# --- legacy vlc_stream -> stream migration (STREAM-rename slice 1) ----------


def test_migrate_legacy_stream_item_remaps_type_and_url():
    """A legacy vlc_stream item dict is remapped to the stream shape:
    `type` -> "stream" and `rtsp_url` -> `stream_url`."""
    legacy = {
        "type": "vlc_stream",
        "id": str(uuid4()),
        "name": "Old Live",
        "rtsp_url": "rtsp://h/x",
        "duration_ms": 10_000,
        "on_unreachable": "hold_last_frame",
    }
    migrated = _migrate_legacy_stream_item(legacy)
    assert migrated["type"] == "stream"
    assert migrated["stream_url"] == "rtsp://h/x"
    assert "rtsp_url" not in migrated
    # Pure helper — the input dict is not mutated.
    assert legacy["type"] == "vlc_stream"
    assert "rtsp_url" in legacy


def test_migrate_legacy_stream_item_leaves_other_types_unchanged():
    """A non-vlc_stream item is returned untouched (identity)."""
    text_item = {"type": "text_slide", "id": str(uuid4()), "name": "t"}
    assert _migrate_legacy_stream_item(text_item) is text_item


@pytest.mark.parametrize("item", [[], "not-a-dict", None, 42])
def test_migrate_legacy_stream_item_tolerates_non_dict(item):
    """A malformed envelope whose `item` is not a dict (list / str /
    None / …) does NOT raise AttributeError on the unconditional
    `.get` — the guard returns it unchanged so the downstream
    discriminated-union validation produces the clean ValidationError."""
    assert _migrate_legacy_stream_item(item) is item


def test_load_migrates_legacy_vlc_stream_envelope(tmp_path: Path):
    """A pre-rename envelope on disk ({"type": "vlc_stream",
    "rtsp_url": ...}) loads back as a valid StreamSlide — the
    discriminated-union load path remaps it before validation."""
    storage = ContentStorage(tmp_path)
    item_id = uuid4()
    legacy_envelope = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(UTC).isoformat(),
        "item": {
            "type": "vlc_stream",
            "id": str(item_id),
            "name": "Legacy Live",
            "rtsp_url": "rtsp://h/x",
            "duration_ms": 12_000,
            "on_unreachable": "black",
            "transition": "cut",
            "transition_ms": 500,
            "created_at": datetime.now(UTC).isoformat(),
        },
    }
    item_dir = tmp_path / str(item_id)
    item_dir.mkdir()
    (item_dir / "item.json").write_text(json.dumps(legacy_envelope, indent=2))

    loaded = storage.load(item_id)
    assert isinstance(loaded, StreamSlide)
    assert loaded.type == "stream"
    assert loaded.stream_url == "rtsp://h/x"
    assert loaded.id == item_id


# --- web (Web slide P1) ----------------------------------------------------


def test_save_web_writes_envelope_and_placeholder(tmp_path: Path):
    """save_web with no screenshot generates a synthetic placeholder
    card (no screenshot has arrived yet) and persists it as the
    standard asset.png."""
    storage = ContentStorage(tmp_path)
    slide = WebSlide(name="Status", url="https://status.example.com")
    storage.save_web(slide)

    png = storage.asset_path(slide.id).read_bytes()
    # A valid PNG (signature) decodable to the placeholder card dims.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(BytesIO(png)).size == (640, 360)


def test_save_web_with_explicit_png_writes_those_bytes(tmp_path: Path):
    """save_web with explicit png_bytes (the P3 producer's path) writes
    those bytes verbatim instead of the placeholder."""
    storage = ContentStorage(tmp_path)
    slide = WebSlide(name="Status", url="https://status.example.com")
    screenshot = b"\x89PNG\r\n\x1a\n-screenshot-bytes"
    storage.save_web(slide, screenshot)

    assert storage.asset_path(slide.id).read_bytes() == screenshot


def test_load_roundtrips_web_slide(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = WebSlide(
        name="Status",
        url="https://status.example.com",
        refresh_interval_s=600,
        duration_ms=15_000,
        transition="fade",
        transition_ms=300,
    )
    storage.save_web(slide)
    loaded = storage.load(slide.id)
    assert isinstance(loaded, WebSlide)
    assert loaded.model_copy(update={"updated_at": None}) == slide
    assert loaded.updated_at is not None


def test_list_all_surfaces_web_items(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    text = TextSlide(name="t", text="t")
    web = WebSlide(name="w", url="https://h/x")
    storage.save_text_slide(text, b"\x89PNG_text")
    storage.save_web(web)

    by_type = {item.type: item for item in storage.list_all()}
    assert isinstance(by_type["web"], WebSlide)
    assert isinstance(by_type["text_slide"], TextSlide)
