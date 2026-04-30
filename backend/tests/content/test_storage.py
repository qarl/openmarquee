import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.content import ImageSlide, TextSlide, VideoSlide
from openmarquee.content.storage import SCHEMA_VERSION, ContentStorage


def _make_slide(**overrides) -> TextSlide:
    kwargs = {"name": "Test Slide", "text": "Hello, world"}
    kwargs.update(overrides)
    return TextSlide(**kwargs)


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

    slide_v2 = TextSlide(id=slide_v1.id, name="v2", text="second")
    storage.save_text_slide(slide_v2, b"\x89PNG\r\nv2")

    loaded = storage.load(slide_v1.id)
    assert loaded.name == "v2"
    assert loaded.text == "second"
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
    assert (datetime.now(timezone.utc) - stamp).total_seconds() < 5


def test_save_accepts_explicit_updated_at_for_peer_ingest(tmp_path: Path):
    storage = ContentStorage(tmp_path)
    slide = _make_slide()
    fixed = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
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
    assert stamp == datetime.fromtimestamp(epoch, tz=timezone.utc)
