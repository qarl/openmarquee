import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from openmarquee.playlist import (
    DEFAULT_PLAYLIST_ID,
    DEFAULT_PLAYLIST_NAME,
    PLAYLIST_SCHEMA_VERSION,
    Playlist,
    PlaylistCollection,
    PlaylistStorage,
)


# --- Legacy single-playlist API (operates on the default playlist by id) ---


def test_empty_load_returns_empty_default_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    pl = storage.load()
    assert pl.item_ids == []
    assert pl.id == DEFAULT_PLAYLIST_ID
    assert pl.name == DEFAULT_PLAYLIST_NAME


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


def test_save_overwrites_previous_default_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    storage.save(Playlist(item_ids=[uuid4(), uuid4()]))
    new_ids = [uuid4()]
    storage.save(Playlist(item_ids=new_ids))
    assert storage.load().item_ids == new_ids


def test_invalid_json_raises_on_load(tmp_path: Path):
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


# --- Multi-playlist (id-keyed) API ---


def test_load_all_returns_default_only_when_no_file(tmp_path: Path):
    """A fresh device bootstraps with just the default playlist present."""
    storage = PlaylistStorage(tmp_path / "playlist.json")
    coll = storage.load_all()
    assert len(coll.playlists) == 1
    assert coll.playlists[0].id == DEFAULT_PLAYLIST_ID
    assert coll.playlists[0].name == DEFAULT_PLAYLIST_NAME
    assert coll.schema_version == PLAYLIST_SCHEMA_VERSION


def test_set_then_get_playlist_round_trips(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    pl = Playlist(name="lunch", items=[])
    pl.append(a)
    pl.append(b)
    storage.set_by_id(pl)

    loaded = storage.get_by_id(pl.id)
    assert loaded is not None
    assert loaded.item_ids == [a, b]
    assert loaded.name == "lunch"


def test_get_by_id_returns_none_for_unknown_id(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    assert storage.get_by_id(uuid4()) is None


def test_set_playlist_does_not_affect_other_playlists(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    pl1 = Playlist(name="weekday", items=[])
    pl1.append(a)
    pl2 = Playlist(name="weekend", items=[])
    pl2.append(b)
    storage.set_by_id(pl1)
    storage.set_by_id(pl2)
    assert storage.get_by_id(pl1.id).item_ids == [a]
    assert storage.get_by_id(pl2.id).item_ids == [b]


def test_delete_playlist_removes_only_that_one(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    pl1 = Playlist(name="a", items=[])
    pl1.append(uuid4())
    pl2 = Playlist(name="b", items=[])
    pl2.append(uuid4())
    storage.set_by_id(pl1)
    storage.set_by_id(pl2)
    assert storage.delete_by_id(pl1.id) is True
    remaining_ids = storage.all_ids()
    assert pl1.id not in remaining_ids
    assert pl2.id in remaining_ids


def test_delete_playlist_returns_false_for_unknown_id(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    assert storage.delete_by_id(uuid4()) is False


# --- Legacy single-playlist API ↔ multi-playlist API consistency ---


def test_legacy_save_load_operates_on_default_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))

    # Both APIs see the same data.
    assert storage.load().item_ids == [a, b]
    assert storage.get_by_id(DEFAULT_PLAYLIST_ID).item_ids == [a, b]


# --- Migration from legacy on-disk formats ---


def test_loads_legacy_v1_unnamed_format_as_default_playlist(tmp_path: Path):
    """Pre-Phase-5 file (`{"item_ids": [...]}`) migrates to the default playlist."""
    a, b = uuid4(), uuid4()
    legacy = {"item_ids": [str(a), str(b)]}
    path = tmp_path / "playlist.json"
    path.write_text(json.dumps(legacy))

    storage = PlaylistStorage(path)
    default_pl = storage.get_by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    assert default_pl.item_ids == [a, b]
    # And the legacy load() API still works.
    assert storage.load().item_ids == [a, b]


def test_v3_dict_keyed_migrates_to_v4_list_with_uuids(tmp_path: Path):
    """v3: {playlists: {name: {items: [...]}}}. v4: list with {id, name, items}.
    Default playlist gets the constant DEFAULT_PLAYLIST_ID; others get fresh."""
    a, b = uuid4(), uuid4()
    path = tmp_path / "playlist.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "playlists": {
                    "default": {"items": [{"item_id": str(a)}]},
                    "lunch": {"items": [{"item_id": str(b)}]},
                },
            }
        )
    )
    storage = PlaylistStorage(path)
    coll = storage.load_all()
    # Both playlists present.
    assert len(coll.playlists) == 2
    default_pl = coll.by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    # Migration-rename: a v3 "default" key the operator never touched
    # gets renamed to "Welcome" on upgrade so the fleet's display
    # names stay uniform across fresh-installs and upgrades.
    assert default_pl.name == "Welcome"
    assert default_pl.item_ids == [a]
    # Lunch playlist preserved with its name and a fresh UUID.
    lunch = coll.by_name("lunch")
    assert lunch is not None
    assert lunch.id != DEFAULT_PLAYLIST_ID
    assert lunch.item_ids == [b]


def test_v4_envelope_loads_unchanged(tmp_path: Path):
    """A file already in v4 form should round-trip without rewriting."""
    storage = PlaylistStorage(tmp_path / "playlist.json")
    pl = Playlist(name="weekend", items=[])
    storage.set_by_id(pl)

    # Read raw and confirm format.
    raw = json.loads((tmp_path / "playlist.json").read_text())
    assert raw["schema_version"] == PLAYLIST_SCHEMA_VERSION
    assert isinstance(raw["playlists"], list)
    # Each entry has id + name + items.
    for entry in raw["playlists"]:
        assert "id" in entry
        assert "name" in entry
        assert "items" in entry


def test_collection_round_trips_via_load_all_save_all(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    p1 = Playlist(id=DEFAULT_PLAYLIST_ID, name="default", items=[])
    p1.append(uuid4())
    p2 = Playlist(name="weekend", items=[])
    p2.append(uuid4())
    p2.append(uuid4())
    coll = PlaylistCollection(playlists=[p1, p2])
    storage.save_all(coll)

    loaded = storage.load_all()
    names = {p.name for p in loaded.playlists}
    assert names == {"default", "weekend"}
    weekend = loaded.by_name("weekend")
    assert weekend is not None
    assert len(weekend.item_ids) == 2


# --- v3+ transitions on the playlist ---


def test_playlist_items_round_trip_with_transition_fields(tmp_path: Path):
    from openmarquee.playlist import PlaylistItem

    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    pl = Playlist(
        items=[
            PlaylistItem(item_id=a, transition="fade", transition_ms=300),
            PlaylistItem(item_id=b, transition="cut", transition_ms=0),
        ]
    )
    storage.save(pl)

    loaded = storage.load()
    assert loaded.items[0].item_id == a
    assert loaded.items[0].transition == "fade"
    assert loaded.items[0].transition_ms == 300
    assert loaded.items[1].transition == "cut"


def test_playlist_item_accepts_palette_transitions(tmp_path: Path):
    """Regression: every transition added to TextSlide/ImageSlide/VideoSlide
    Literals must also be accepted by PlaylistItem — the runtime dispatch
    in playback.py reads the transition off PlaylistItem, not the content
    slide. Pre-2026-04-28 the two Literals could drift; this locks them.
    Extend the asserted set as new transitions land in the 2026-04-28
    palette-expansion batch."""
    from openmarquee.playlist import PlaylistItem

    storage = PlaylistStorage(tmp_path / "playlist.json")
    a = uuid4()
    pl = Playlist(
        items=[
            PlaylistItem(item_id=a, transition="scroll", transition_ms=400),
            PlaylistItem(item_id=uuid4(), transition="flip", transition_ms=350),
            PlaylistItem(item_id=uuid4(), transition="marquee", transition_ms=600),
            PlaylistItem(item_id=uuid4(), transition="dissolve", transition_ms=450),
            PlaylistItem(item_id=uuid4(), transition="pixelate", transition_ms=550),
            PlaylistItem(item_id=uuid4(), transition="halftone", transition_ms=500),
            PlaylistItem(item_id=uuid4(), transition="scanline", transition_ms=400),
            PlaylistItem(item_id=uuid4(), transition="glitch", transition_ms=350),
            PlaylistItem(item_id=uuid4(), transition="push", transition_ms=480),
        ]
    )
    storage.save(pl)

    loaded = storage.load()
    assert loaded.items[0].transition == "scroll"
    assert loaded.items[0].transition_ms == 400
    assert loaded.items[1].transition == "flip"
    assert loaded.items[1].transition_ms == 350
    assert loaded.items[2].transition == "marquee"
    assert loaded.items[2].transition_ms == 600
    assert loaded.items[3].transition == "dissolve"
    assert loaded.items[3].transition_ms == 450
    assert loaded.items[4].transition == "pixelate"
    assert loaded.items[4].transition_ms == 550
    assert loaded.items[5].transition == "halftone"
    assert loaded.items[5].transition_ms == 500
    assert loaded.items[6].transition == "scanline"
    assert loaded.items[6].transition_ms == 400
    assert loaded.items[7].transition == "glitch"
    assert loaded.items[7].transition_ms == 350
    assert loaded.items[8].transition == "push"
    assert loaded.items[8].transition_ms == 480


def test_playlist_item_ids_is_a_derived_view_over_items():
    from openmarquee.playlist import PlaylistItem

    a, b = uuid4(), uuid4()
    pl = Playlist(
        items=[
            PlaylistItem(item_id=a, transition="fade"),
            PlaylistItem(item_id=b),
        ]
    )
    assert pl.item_ids == [a, b]


def test_v2_on_disk_migrates_with_default_transitions(tmp_path: Path):
    """Existing SD cards have `item_ids` at each playlist level (schema_version=2)."""
    path = tmp_path / "playlist.json"
    a, b = str(uuid4()), str(uuid4())
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "playlists": {
                    "default": {"item_ids": [a, b]},
                },
            }
        )
    )
    storage = PlaylistStorage(path)
    loaded = storage.load()
    assert [str(i) for i in loaded.item_ids] == [a, b]
    # Migrated items get the default transitions.
    assert all(i.transition == "cut" for i in loaded.items)
    assert all(i.transition_ms == 500 for i in loaded.items)


def test_v1_unnamed_on_disk_migrates_to_default_playlist(tmp_path: Path):
    """Oldest format: `{item_ids: [...]}` with no envelope at all."""
    path = tmp_path / "playlist.json"
    a = str(uuid4())
    path.write_text(json.dumps({"item_ids": [a]}))
    storage = PlaylistStorage(path)
    loaded = storage.load()
    assert len(loaded.items) == 1
    assert str(loaded.items[0].item_id) == a
    assert loaded.items[0].transition == "cut"


def test_list_in_playlist_order_patches_transitions_onto_items(tmp_path: Path):
    """The playlist owns transitions; the content item's own transition
    fields are legacy-ignored when the item appears via list_in_playlist_order."""
    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistItem, list_in_playlist_order

    storage = ContentStorage(tmp_path / "content")
    slide = TextSlide(name="x", text="x", transition="cut", transition_ms=500)
    storage.save_text_slide(slide, b"\x89PNG")

    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    playlist_storage.save(
        Playlist(
            items=[
                PlaylistItem(
                    item_id=slide.id, transition="fade", transition_ms=250
                )
            ]
        )
    )

    ordered = list_in_playlist_order(storage, playlist_storage)
    assert len(ordered) == 1
    # The playlist's transition wins over the content's.
    assert ordered[0].transition == "fade"
    assert ordered[0].transition_ms == 250


def test_list_in_playlist_order_include_orphans_returns_items_from_non_default_playlists(
    tmp_path: Path,
):
    """Pre-2026-04-28 bug: include_orphans=True only added items NOT in
    any playlist, so items in non-default playlists were invisible to
    /api/content + the UI pallets. Surfaced when seed.py started
    seeding the Freedom playlist alongside Welcome — Freedom's slides
    were missing from /api/content. Fixed by treating "anything not
    yet in `ordered`" as the extras pool.
    """
    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistItem, list_in_playlist_order

    storage = ContentStorage(tmp_path / "content")
    in_default = TextSlide(name="d", text="d")
    in_other = TextSlide(name="o", text="o")
    true_orphan = TextSlide(name="z", text="z")
    for slide in (in_default, in_other, true_orphan):
        storage.save_text_slide(slide, b"\x89PNG")

    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    playlist_storage.set_by_id(
        Playlist(
            id=DEFAULT_PLAYLIST_ID,
            name="Welcome",
            items=[PlaylistItem(item_id=in_default.id)],
        )
    )
    playlist_storage.set_by_id(
        Playlist(
            name="Freedom",
            items=[PlaylistItem(item_id=in_other.id)],
        )
    )
    # true_orphan is in storage but in no playlist.

    ordered = list_in_playlist_order(
        storage, playlist_storage, include_orphans=True
    )
    ids = [item.id for item in ordered]
    # Default playlist's item comes first, in playlist order.
    assert ids[0] == in_default.id
    # Both the other-playlist item AND the true orphan show up after.
    assert in_other.id in ids
    assert true_orphan.id in ids
    assert len(ids) == 3


# --- prune_dangling_refs ---


def test_prune_dangling_drops_ids_not_in_valid_set(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    kept_a, kept_b, stale = uuid4(), uuid4(), uuid4()
    storage.save(Playlist(item_ids=[kept_a, stale, kept_b]))

    pruned = storage.prune_dangling_refs({kept_a, kept_b})

    assert pruned == 1
    assert storage.load().item_ids == [kept_a, kept_b]


def test_prune_dangling_is_noop_when_everything_resolves(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))

    pruned = storage.prune_dangling_refs({a, b})

    assert pruned == 0
    assert storage.load().item_ids == [a, b]


def test_prune_dangling_prunes_across_every_playlist_in_collection(tmp_path: Path):
    """A named playlist AND the default playlist both get cleaned."""
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b, c = uuid4(), uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))  # default
    lobby = Playlist(name="lobby")
    lobby.append(b)
    lobby.append(c)
    storage.set_by_id(lobby)

    pruned = storage.prune_dangling_refs({b})  # only b is "valid"

    assert pruned == 2  # a (default) + c (lobby)
    assert storage.get_by_id(DEFAULT_PLAYLIST_ID).item_ids == [b]
    assert storage.get_by_id(lobby.id).item_ids == [b]


def test_prune_dangling_empty_valid_set_empties_every_playlist(tmp_path: Path):
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))

    pruned = storage.prune_dangling_refs(set())

    assert pruned == 2
    assert storage.load().item_ids == []


def test_prune_dangling_does_not_write_when_nothing_changes(tmp_path: Path):
    """File mtime shouldn't bump on a no-op prune — lets integrity-check
    tooling distinguish 'ran and cleaned' from 'ran and nothing to do'."""
    storage = PlaylistStorage(tmp_path / "playlist.json")
    a, b = uuid4(), uuid4()
    storage.save(Playlist(item_ids=[a, b]))
    mtime_before = (tmp_path / "playlist.json").stat().st_mtime

    pruned = storage.prune_dangling_refs({a, b})

    assert pruned == 0
    assert (tmp_path / "playlist.json").stat().st_mtime == mtime_before
