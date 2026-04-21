import json
from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.content import TextSlide
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
    assert loaded == slide


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
