import json
from pathlib import Path
from uuid import uuid4

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


def test_playlist_with_both_items_and_item_ids_emits_no_duplicate_key(
    tmp_path: Path,
):
    """P2 regression (2026-05-21): a playlist dict carrying BOTH `items`
    and an `item_ids` echo — the exact shape a previously-saved
    playlist.json has, since `item_ids` is a serialized computed_field —
    must not retain `item_ids` as an extra field. extra="allow" would
    otherwise re-emit it on the next save as a DUPLICATE JSON key."""
    a, b = uuid4(), uuid4()
    raw = {
        "id": str(uuid4()),
        "name": "Free Your Sign",
        "items": [{"item_id": str(a)}, {"item_id": str(b)}],
        # The echo a prior model_dump_json() wrote alongside `items`.
        "item_ids": [str(a), str(b)],
    }
    pl = Playlist.model_validate(raw)
    # The echo must NOT survive as an extra field.
    assert "item_ids" not in (pl.__pydantic_extra__ or {})
    # ...so the dumped JSON carries the key exactly once.
    assert pl.model_dump_json().count('"item_ids"') == 1
    # `items` stays authoritative; the echo is discarded, not merged.
    assert pl.item_ids == [a, b]


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


def test_invalid_json_is_quarantined_and_starts_fresh(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """Corrupt-JSON recovery contract (Batch 19.2 / sweep #10 #4):
    load() must NOT raise on garbage input -- a single bad on-disk
    file would otherwise lock the backend into a crash loop. Instead,
    the bad file gets renamed to `<name>.corrupt-<UTC>` and the
    storage starts fresh from defaults. Operator sees the WARNING
    in the log; next save() overwrites the original path with a
    valid file."""
    path = tmp_path / "playlist.json"
    path.write_text("this is not JSON")
    storage = PlaylistStorage(path)

    with caplog.at_level("WARNING", logger="openmarquee._storage_recovery"):
        playlist = storage.load()

    # No exception. Returned playlist is the bootstrap default (empty).
    assert playlist.item_ids == []

    # The bad file was renamed to a timestamped quarantine sibling.
    quarantined = list(tmp_path.glob("playlist.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "this is not JSON"

    # WARNING log emitted citing the path + parse error.
    assert any("failed to parse" in rec.message for rec in caplog.records), (
        f"expected a WARNING about parse failure, got: {[r.message for r in caplog.records]}"
    )


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
    # gets renamed to DEFAULT_PLAYLIST_NAME on upgrade so the fleet's
    # display names stay uniform across fresh-installs and upgrades.
    # (2026-07-13: that name is "Free Your Sign" -- qarl aligned the name
    # with the FREE YOUR SIGN reel that's been the default content.)
    from openmarquee.playlist import DEFAULT_PLAYLIST_NAME

    assert default_pl.name == DEFAULT_PLAYLIST_NAME == "Free Your Sign"
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
    # Default playlist uses the CURRENT name so the round-trip is identity —
    # a legacy default name ("default"/"Welcome"/"Demo") would be coerced to
    # DEFAULT_PLAYLIST_NAME on load; that migration is covered separately.
    p1 = Playlist(id=DEFAULT_PLAYLIST_ID, name=DEFAULT_PLAYLIST_NAME, items=[])
    p1.append(uuid4())
    p2 = Playlist(name="weekend", items=[])
    p2.append(uuid4())
    p2.append(uuid4())
    coll = PlaylistCollection(playlists=[p1, p2])
    storage.save_all(coll)

    loaded = storage.load_all()
    names = {p.name for p in loaded.playlists}
    assert names == {DEFAULT_PLAYLIST_NAME, "weekend"}
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
            PlaylistItem(item_id=uuid4(), transition="blinds", transition_ms=520),
            PlaylistItem(item_id=uuid4(), transition="shutter", transition_ms=460),
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
    assert loaded.items[9].transition == "blinds"
    assert loaded.items[9].transition_ms == 520
    assert loaded.items[10].transition == "shutter"
    assert loaded.items[10].transition_ms == 460


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
    # r52: cut-clamps-to-0 invariant — pre-r52 this asserted 500 (the
    # field default), but the model_validator now coerces cut entries
    # to ms=0. The pre-r52 default of 500 was always wrong for cut
    # (cuts are instantaneous); the migration writes the new canonical
    # 0 transparently.
    assert all(i.transition_ms == 0 for i in loaded.items)


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


# --- 2026-05-25: forward-compat extras preservation across migrations ---
#
# Mirrors schedule.py's MIGRATION_HANDLED_TOP_LEVEL + **extras splat
# pattern (test_v1_migration_preserves_unknown_top_level_fields +
# test_v1_migration_explicit_kwargs_not_shadowed_by_extras_splat in
# test_schedule.py). PlaylistCollection has model_config =
# ConfigDict(extra="allow") but Pydantic only preserves extras via
# model_validate, NOT via direct __init__. The v1/v2/v3 migration
# paths use explicit kwargs so without **extras splat any forward-
# compat top-level field on a pre-v4 file was silently dropped.


def test_v1_migration_preserves_unknown_top_level_fields(tmp_path: Path):
    """Forward-compat lock: any unknown top-level field in a v1 file
    (item_ids-only shape) must survive migration AND survive the
    persist-to-disk + reload round-trip."""
    path = tmp_path / "playlist.json"
    a = str(uuid4())
    path.write_text(
        json.dumps(
            {
                "item_ids": [a],
                "future_v5_field": "hypothetical-forward-compat-value",
                "future_v5_nested": {"hint": "preserve me too"},
            }
        )
    )
    storage = PlaylistStorage(path)
    storage.load_all()

    # On-disk shape: extras survive AND legacy item_ids got cleaned.
    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == PLAYLIST_SCHEMA_VERSION
    assert on_disk.get("future_v5_field") == "hypothetical-forward-compat-value"
    assert on_disk.get("future_v5_nested") == {"hint": "preserve me too"}
    # Legacy key cleaned (the v1->v4 transform converted it to playlists).
    assert "item_ids" not in on_disk

    # Reload round-trip: a SECOND load (now of the v4 file) must still
    # surface the extras via PlaylistCollection.model_validate's extra="allow".
    storage2 = PlaylistStorage(path)
    reloaded = storage2.load_all()
    assert reloaded.model_extra is not None
    assert reloaded.model_extra.get("future_v5_field") == "hypothetical-forward-compat-value"
    assert reloaded.model_extra.get("future_v5_nested") == {"hint": "preserve me too"}


def test_v2_migration_preserves_unknown_top_level_fields(tmp_path: Path):
    """Forward-compat lock for v2 (dict-keyed-by-name + item_ids per
    playlist). Same shape as v1 but the migration path is different
    (lines 448-482 of playlist.py, not the v1 branch at lines 432-446).
    Both paths now share the same **extras splat carve-out."""
    path = tmp_path / "playlist.json"
    a = str(uuid4())
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "playlists": {
                    "default": {"item_ids": [a]},
                },
                "future_v5_field": "v2 preserve",
            }
        )
    )
    storage = PlaylistStorage(path)
    loaded = storage.load_all()

    # Extra survived in-memory.
    assert loaded.model_extra is not None
    assert loaded.model_extra.get("future_v5_field") == "v2 preserve"

    # And persisted to disk so the next read also surfaces it.
    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == PLAYLIST_SCHEMA_VERSION
    assert on_disk.get("future_v5_field") == "v2 preserve"


def test_v3_migration_preserves_unknown_top_level_fields(tmp_path: Path):
    """Forward-compat lock for v3 (dict-keyed-by-name + items list
    with item-side transitions). Same migration branch as v2 but
    with the items-vs-item_ids distinction inside each playlist;
    confirms the trailing `extras` splat applies regardless of which
    sub-shape the v2-or-v3 branch took."""
    path = tmp_path / "playlist.json"
    a = str(uuid4())
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "playlists": {
                    "default": {
                        "items": [
                            {
                                "item_id": a,
                                "transition": "fade",
                                "transition_ms": 250,
                            }
                        ],
                    },
                },
                "future_v5_field": "v3 preserve",
            }
        )
    )
    storage = PlaylistStorage(path)
    loaded = storage.load_all()

    assert loaded.model_extra is not None
    assert loaded.model_extra.get("future_v5_field") == "v3 preserve"
    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == PLAYLIST_SCHEMA_VERSION
    assert on_disk.get("future_v5_field") == "v3 preserve"


def test_v1_migration_explicit_kwargs_not_shadowed_by_extras_splat(tmp_path: Path):
    """Carve-out lock: the explicit kwargs we pass to PlaylistCollection
    (playlists) plus the pre-v4 consumed fields (item_ids, schema_version,
    playlists) must NOT end up in the **extras splat, otherwise we'd
    get a Python double-kwarg TypeError on `playlists`, OR re-stamp
    schema_version=1 onto the migrated collection, OR carry the legacy
    `item_ids` forward as an extra on the v4 model.

    Exercises a v1 payload that includes every shadow-risk field PLUS
    an unknown extra, and asserts: (a) no exception, (b) extras carve-
    out worked (unknown survives), (c) the v4 model has the
    correctly-migrated schema_version (not the v1 leftover)."""
    from openmarquee.playlist import _coerce_to_collection

    a = str(uuid4())
    data = {
        "schema_version": 1,  # v1; default PLAYLIST_SCHEMA_VERSION must win
        "item_ids": [a],  # v1 array; replaced by migrated default Playlist
        "future_v5_field": "preserve me",
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is True
    # Migration won: explicit + default values used, not the v1 leftovers.
    assert collection.schema_version == PLAYLIST_SCHEMA_VERSION
    assert len(collection.playlists) == 1
    assert collection.playlists[0].id == DEFAULT_PLAYLIST_ID
    assert len(collection.playlists[0].items) == 1
    # Extra survived.
    assert collection.model_extra is not None
    assert collection.model_extra.get("future_v5_field") == "preserve me"
    # Carve-out worked: the legacy item_ids + schema_version did NOT
    # survive as extras (which would have been doubly-wrong because
    # `playlists` is also explicit, and an extras-side `playlists`
    # would have raised a TypeError on the splat).
    assert "item_ids" not in collection.model_extra
    assert "schema_version" not in collection.model_extra
    assert "playlists" not in collection.model_extra


def test_list_for_playback_patches_transitions_onto_items(tmp_path: Path):
    """The playlist owns transitions; the content item's own transition
    fields are legacy-ignored when the item appears via list_for_playback."""
    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistItem, list_for_playback

    storage = ContentStorage(tmp_path / "content")
    slide = TextSlide(name="x", text="x", transition="cut", transition_ms=500)
    storage.save_text_slide(slide, b"\x89PNG")

    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    playlist_storage.save(
        Playlist(items=[PlaylistItem(item_id=slide.id, transition="fade", transition_ms=250)])
    )

    ordered = list_for_playback(storage, playlist_storage)
    assert len(ordered) == 1
    # The playlist's transition wins over the content's.
    assert ordered[0].transition == "fade"
    assert ordered[0].transition_ms == 250


def test_list_for_playback_silently_drops_stale_playlist_refs(tmp_path: Path):
    """Load-bearing forgiveness: if a PlaylistItem.item_id no longer
    has matching content in storage (operator deleted the slide, or
    `rm -rf content/<uuid>/` happened mid-playlist), the stale ref is
    silently skipped. Without this, the playback loop would KeyError
    on the next fetch and crash. Lock the contract so a future
    refactor of _playlist_ordered_prefix that swaps the
    `in items_by_id` guard for an unconditional lookup can't
    silently regress us back to brittleness."""
    from uuid import uuid4

    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistItem, list_for_playback

    storage = ContentStorage(tmp_path / "content")
    real = TextSlide(name="real", text="real")
    storage.save_text_slide(real, b"\x89PNG")
    # Stale ref: uuid that points to no saved content.
    ghost_id = uuid4()

    playlist_storage = PlaylistStorage(tmp_path / "playlist.json")
    playlist_storage.save(
        Playlist(
            items=[
                PlaylistItem(item_id=ghost_id),
                PlaylistItem(item_id=real.id),
            ]
        )
    )

    ordered = list_for_playback(storage, playlist_storage)
    # Ghost is dropped; real comes through. No exception raised.
    assert [item.id for item in ordered] == [real.id]


def test_list_full_library_returns_items_from_non_default_playlists(
    tmp_path: Path,
):
    """Pre-2026-04-28 bug: the trailing-items pool excluded items in
    non-default playlists, so non-default-playlist content was
    invisible to /api/content + the UI pallets. Surfaced when seed.py
    started seeding the Freedom playlist alongside Welcome -- Freedom's
    slides were missing from /api/content. Fixed by treating "anything
    not yet in the anchor-playlist prefix" as the extras pool.
    """
    from openmarquee.content import TextSlide
    from openmarquee.content.storage import ContentStorage
    from openmarquee.playlist import PlaylistItem, list_full_library

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

    ordered = list_full_library(storage, playlist_storage)
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


def test_concurrent_appends_dont_lose_items(tmp_path: Path):
    """Round-27 concurrency regression: PlaylistStorage's
    append_item_to_default does load+mutate+save. Pre-r27 (when the
    helper lived in api.py as load+append+save at the call layer) two
    concurrent POST /api/content/images requests could interleave:

      T1 load_all -> T2 load_all (both see N items)
      T1 mutate (append T1_id; N+1 items in T1's copy)
      T2 mutate (append T2_id; N+1 items in T2's copy -- T1_id NOT in it)
      T1 save_all (writes N+1 with T1_id)
      T2 save_all (writes N+1 with T2_id -- T1_id WIPED FROM DISK)

    The lost item means the operator's just-uploaded slide vanishes
    from the playlist (content envelope still on disk, just
    unreferenced). Permanent corruption, invisible until somebody
    notices the slide isn't showing.

    Note on PUT-vs-POST: the dispatch also describes a "Tab 1
    drag-reorder PUT vs Tab 2 POST append" scenario, but PUT
    semantics are wholesale-replace -- the lock can't prevent
    Tab 1's PUT (built from a minutes-old browser view) from
    overwriting Tab 2's just-appended item. That's a different
    bug-class (needs optimistic-concurrency via If-Match etag),
    out of scope for the lock fix. The lock DOES prevent the
    concurrent-load+mutate+save race tested here.

    Test: fire N concurrent append_item_to_default calls with
    distinct ids. All N must land on disk. Pre-r27 ~30-50% are lost
    to the race depending on thread scheduling.
    """
    import threading

    storage = PlaylistStorage(tmp_path / "playlist.json")
    n_concurrent = 30
    item_ids = [uuid4() for _ in range(n_concurrent)]
    barrier = threading.Barrier(n_concurrent)

    def append_one(item_id):
        barrier.wait()
        storage.append_item_to_default(item_id)

    threads = [threading.Thread(target=append_one, args=(i,)) for i in item_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    persisted = set(storage.load().item_ids)
    missing = set(item_ids) - persisted
    assert not missing, (
        f"{len(missing)}/{n_concurrent} items lost to the "
        f"load-mutate-save race. Sample missing: "
        f"{list(missing)[:5]}"
    )


# --- r52: cut-clamps-to-0 model validator -----------------------------------


def test_playlistitem_cut_clamps_transition_ms_to_zero_on_construction():
    """r52: a PlaylistItem with transition='cut' coerces transition_ms to 0.

    Pre-r52 the UI hard-coded 500 ms for every entry including cut, so
    legacy storage JSON has plenty of `cut` + non-zero entries on disk.
    The model_validator clamps rather than rejects so a load round-trip
    cleans them up silently.
    """
    from uuid import uuid4

    from openmarquee.playlist import PlaylistItem

    item = PlaylistItem(item_id=uuid4(), transition="cut", transition_ms=500)
    assert item.transition == "cut"
    assert item.transition_ms == 0


def test_playlistitem_non_cut_preserves_transition_ms():
    """Non-cut kinds keep operator-set transition_ms unchanged."""
    from uuid import uuid4

    from openmarquee.playlist import PlaylistItem

    item = PlaylistItem(item_id=uuid4(), transition="fade", transition_ms=750)
    assert item.transition == "fade"
    assert item.transition_ms == 750


def test_playlistitem_cut_already_zero_no_op():
    """Cut + 0 ms is the canonical shape; clamp is a no-op."""
    from uuid import uuid4

    from openmarquee.playlist import PlaylistItem

    item = PlaylistItem(item_id=uuid4(), transition="cut", transition_ms=0)
    assert item.transition == "cut"
    assert item.transition_ms == 0


def test_playlistitem_cut_default_ms_clamps_to_zero():
    """Default `transition_ms=500` on a cut entry is also clamped."""
    from uuid import uuid4

    from openmarquee.playlist import PlaylistItem

    # transition defaults to "cut"; transition_ms defaults to 500 per the
    # field. The validator should still clamp.
    item = PlaylistItem(item_id=uuid4())
    assert item.transition == "cut"
    assert item.transition_ms == 0


# --- Default-playlist rename migration (default/Welcome/Demo → "Free Your Sign") ---


@pytest.mark.parametrize("legacy_name", ["default", "Welcome", "Demo"])
def test_v4_default_playlist_legacy_name_coerced_to_current(legacy_name: str):
    """A v4 device first seeded under an older default name upgrades its
    DEFAULT_PLAYLIST_ID playlist to the current DEFAULT_PLAYLIST_NAME on load,
    and flags the migration so the caller persists it. This is the fleet-
    convergence path for already-deployed devices: their files are v4, so the
    v2/v3 dict-keyed coercion never fires for them."""
    from openmarquee.playlist import _coerce_to_collection

    data = {
        "schema_version": PLAYLIST_SCHEMA_VERSION,
        "playlists": [
            {"id": str(DEFAULT_PLAYLIST_ID), "name": legacy_name, "items": []},
        ],
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is True
    default_pl = collection.by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    assert default_pl.name == DEFAULT_PLAYLIST_NAME == "Free Your Sign"


def test_v4_default_playlist_already_current_is_not_migrated():
    """A v4 device already on the blessed name loads untouched — no spurious
    was_migrated=True (which would rewrite the file on every restart)."""
    from openmarquee.playlist import _coerce_to_collection

    data = {
        "schema_version": PLAYLIST_SCHEMA_VERSION,
        "playlists": [
            {"id": str(DEFAULT_PLAYLIST_ID), "name": DEFAULT_PLAYLIST_NAME, "items": []},
        ],
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is False
    assert collection.by_id(DEFAULT_PLAYLIST_ID).name == DEFAULT_PLAYLIST_NAME


def test_v4_rename_is_scoped_to_default_id_only():
    """The v4 rename touches ONLY the DEFAULT_PLAYLIST_ID playlist — a
    non-default playlist an operator happened to name 'Demo' keeps its name,
    and the collection is not flagged as migrated."""
    from openmarquee.playlist import _coerce_to_collection

    other_id = uuid4()
    data = {
        "schema_version": PLAYLIST_SCHEMA_VERSION,
        "playlists": [
            {"id": str(DEFAULT_PLAYLIST_ID), "name": DEFAULT_PLAYLIST_NAME, "items": []},
            {"id": str(other_id), "name": "Demo", "items": []},
        ],
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is False
    other = collection.by_id(other_id)
    assert other is not None and other.name == "Demo"


def test_v4_rename_scoped_when_a_non_default_shares_a_legacy_name():
    """Stronger scoping: the default ITSELF carries a legacy name (so the
    migration fires) AND a non-default playlist also carries a legacy name.
    Only the DEFAULT_PLAYLIST_ID playlist flips to DEFAULT_PLAYLIST_NAME; the
    operator's like-named playlist keeps its name."""
    from openmarquee.playlist import _coerce_to_collection

    other_id = uuid4()
    data = {
        "schema_version": PLAYLIST_SCHEMA_VERSION,
        "playlists": [
            {"id": str(DEFAULT_PLAYLIST_ID), "name": "Demo", "items": []},
            {"id": str(other_id), "name": "Welcome", "items": []},
        ],
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is True
    assert collection.by_id(DEFAULT_PLAYLIST_ID).name == DEFAULT_PLAYLIST_NAME
    other = collection.by_id(other_id)
    assert other is not None and other.name == "Welcome"


def test_v3_only_the_default_key_is_coerced():
    """The v2/v3 dict-keyed migration coerces ONLY the "default" key (the only
    name that was ever a v2/v3 default key) to the constant id +
    DEFAULT_PLAYLIST_NAME. A v4-era display name used as a key ("Welcome"/"Demo")
    is NOT treated as the default — it stays a regular playlist with a fresh id."""
    from openmarquee.playlist import _coerce_to_collection

    coll, migrated = _coerce_to_collection(
        {"schema_version": 3, "playlists": {"default": {"items": []}, "lunch": {"items": []}}}
    )
    assert migrated is True
    default_pl = coll.by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    assert default_pl.name == DEFAULT_PLAYLIST_NAME == "Free Your Sign"
    lunch = coll.by_name("lunch")
    assert lunch is not None and lunch.id != DEFAULT_PLAYLIST_ID

    for non_default_key in ("Demo", "Welcome"):
        coll, _ = _coerce_to_collection(
            {"schema_version": 3, "playlists": {non_default_key: {"items": []}}}
        )
        kept = coll.by_name(non_default_key)
        assert kept is not None and kept.id != DEFAULT_PLAYLIST_ID, (
            f"{non_default_key!r} must NOT be promoted to the default identity"
        )


def test_v3_mixed_dict_does_not_swap_default_identity():
    """Regression for the v2/v3-widening hazard: a stale mixed dict where an
    operator playlist is keyed "Demo" (ordered first) and the REAL default is
    keyed "default". The real default must keep DEFAULT_PLAYLIST_ID and become
    "Free Your Sign"; the "Demo" playlist keeps its name + a fresh id. Widening
    the v2/v3 match to the full legacy set would wrongly let "Demo" (first in the
    dict) claim the default identity."""
    from openmarquee.playlist import _coerce_to_collection

    user_item, real_item = uuid4(), uuid4()
    data = {
        "schema_version": 3,
        "playlists": {
            "Demo": {"items": [{"item_id": str(user_item)}]},
            "default": {"items": [{"item_id": str(real_item)}]},
        },
    }
    collection, was_migrated = _coerce_to_collection(data)
    assert was_migrated is True
    default_pl = collection.by_id(DEFAULT_PLAYLIST_ID)
    assert default_pl is not None
    assert default_pl.name == DEFAULT_PLAYLIST_NAME
    assert default_pl.item_ids == [real_item]
    demo = collection.by_name("Demo")
    assert demo is not None
    assert demo.id != DEFAULT_PLAYLIST_ID
    assert demo.item_ids == [user_item]
