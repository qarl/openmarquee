import json
from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.playlist import (
    DEFAULT_PLAYLIST_NAME,
    PLAYLIST_SCHEMA_VERSION,
    Playlist,
    PlaylistCollection,
    PlaylistStorage,
)


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


# --- Multi-playlist API ---


def test_load_all_returns_empty_collection_when_no_file(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    coll = storage.load_all()
    assert coll.playlists == {}
    assert coll.schema_version == PLAYLIST_SCHEMA_VERSION


def test_set_then_get_playlist_round_trips(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.set_playlist("lunch", Playlist(item_ids=[a, b]))

    loaded = storage.get_playlist("lunch")
    assert loaded.item_ids == [a, b]


def test_get_playlist_returns_empty_for_unknown_name(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    assert storage.get_playlist("nope").item_ids == []


def test_set_playlist_does_not_affect_other_playlists(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.set_playlist("default", Playlist(item_ids=[a]))
    storage.set_playlist("weekend", Playlist(item_ids=[b]))
    assert storage.get_playlist("default").item_ids == [a]
    assert storage.get_playlist("weekend").item_ids == [b]


def test_delete_playlist_removes_only_that_one(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.set_playlist("a", Playlist(item_ids=[uuid4()]))
    storage.set_playlist("b", Playlist(item_ids=[uuid4()]))
    assert storage.delete_playlist("a") is True
    assert storage.all_names() == ["b"]


def test_delete_playlist_returns_false_for_unknown_name(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    assert storage.delete_playlist("nope") is False


def test_all_names_sorted_alphabetically(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.set_playlist("zebra", Playlist())
    storage.set_playlist("apple", Playlist())
    storage.set_playlist("mango", Playlist())
    assert storage.all_names() == ["apple", "mango", "zebra"]


# --- Legacy single-playlist API (back-compat) ---


def test_legacy_save_load_operates_on_default_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))

    # Both APIs see the same data.
    assert storage.load().item_ids == [a, b]
    assert storage.get_playlist(DEFAULT_PLAYLIST_NAME).item_ids == [a, b]


# --- Migration from legacy v1 format ---


def test_loads_legacy_single_playlist_format_as_default(tmp_path: Path):
    """A pre-multi-playlist file ({"item_ids": [...]}) should migrate
    transparently to the default playlist of a v2 collection."""
    a, b = uuid4(), uuid4()
    legacy = {"item_ids": [str(a), str(b)]}
    path = tmp_path / "playlist.json"
    path.write_text(json.dumps(legacy))

    storage = PlaylistStorage(path)
    assert storage.get_playlist(DEFAULT_PLAYLIST_NAME).item_ids == [a, b]
    # And the legacy load() API still works.
    assert storage.load().item_ids == [a, b]


def test_save_writes_v2_envelope_with_schema_version(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.set_playlist("default", Playlist(item_ids=[uuid4()]))

    raw = json.loads((tmp_path / "playlist.json").read_text())
    assert raw["schema_version"] == PLAYLIST_SCHEMA_VERSION
    assert "playlists" in raw
    assert "default" in raw["playlists"]


def test_collection_round_trips_via_load_all_save_all(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    coll = PlaylistCollection(
        playlists={
            "default": Playlist(item_ids=[uuid4()]),
            "weekend": Playlist(item_ids=[uuid4(), uuid4()]),
        }
    )
    storage.save_all(coll)

    loaded = storage.load_all()
    assert set(loaded.playlists) == {"default", "weekend"}
    assert len(loaded.playlists["weekend"].item_ids) == 2
