from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.playlist import Playlist, PlaylistStorage


def test_empty_load_returns_empty_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    assert storage.load().item_ids == []


def test_save_then_load_round_trips_order(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b, c = uuid4(), uuid4(), uuid4()
    storage.save(Playlist(item_ids=[c, a, b]))
    loaded = storage.load()
    assert loaded.item_ids == [c, a, b]


def test_save_creates_parent_directory_if_missing(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "deeply" / "nested" / "playlist.json")
    storage.save(Playlist(item_ids=[uuid4()]))
    assert (tmp_path / "deeply" / "nested" / "playlist.json").exists()


def test_atomic_write_leaves_no_tmp_files(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.save(Playlist(item_ids=[uuid4()]))
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_overwrites_previous_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.save(Playlist(item_ids=[uuid4(), uuid4()]))
    new_ids = [uuid4()]
    storage.save(Playlist(item_ids=new_ids))
    assert storage.load().item_ids == new_ids


def test_invalid_json_raises_on_load(tmp_path: Path):
    import json

    path = tmp_path / "playlist.json"
    path.write_text("this is not JSON")
    storage = PlaylistStorage(path)
    with pytest.raises(json.JSONDecodeError):
        storage.load()


def test_append_skips_duplicates():
    playlist = Playlist()
    item_id = uuid4()
    playlist.append(item_id)
    playlist.append(item_id)
    assert playlist.item_ids == [item_id]


def test_append_preserves_insertion_order():
    playlist = Playlist()
    a, b, c = uuid4(), uuid4(), uuid4()
    playlist.append(a)
    playlist.append(b)
    playlist.append(c)
    assert playlist.item_ids == [a, b, c]


def test_remove_no_op_when_id_absent():
    playlist = Playlist()
    playlist.remove(uuid4())  # should not raise
    assert playlist.item_ids == []


def test_remove_drops_only_the_named_id():
    a, b, c = uuid4(), uuid4(), uuid4()
    playlist = Playlist(item_ids=[a, b, c])
    playlist.remove(b)
    assert playlist.item_ids == [a, c]
