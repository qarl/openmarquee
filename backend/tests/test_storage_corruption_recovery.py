"""Batch 19.2 / sweep #10 #4: storage classes survive corrupt JSON.

When the on-disk JSON file is malformed (half-written, hand-edited
into invalid syntax, truncated by a power-loss-during-write that
the atomic-write rename can't cover), the load() path used to raise
JSONDecodeError or Pydantic ValidationError up through service
startup. The whole backend hard-crashed; systemd would restart it
and the same crash would happen again until the operator manually
deleted the file.

19.2 adds a corruption-quarantine recovery: bad files get renamed
to `<name>.corrupt-<UTC-ISO>` + a WARN log line + the load returns
defaults. The next save() overwrites the original path.

This file tests the recovery is wired correctly across all 5
storage classes that share the JSON-on-disk pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openmarquee.flock import Flock, FlockStorage
from openmarquee.playlist import (
    DEFAULT_PLAYLIST_NAME,
    PlaylistCollection,
    PlaylistStorage,
)
from openmarquee.schedule import Schedule, ScheduleStorage
from openmarquee.settings import SettingsStorage, SystemSettings
from openmarquee.tombstone import TombstoneLog, TombstoneStorage


# --- the recovery contract, asserted per storage class ---


def _list_corrupt_quarantine(parent: Path, stem: str) -> list[Path]:
    """All `<stem>.corrupt-*` siblings of stem in parent."""
    return list(parent.glob(f"{stem}.corrupt-*"))


def test_playlist_storage_recovers_from_malformed_json(tmp_path: Path):
    path = tmp_path / "playlist.json"
    path.write_text("{ this is not valid json ]")
    storage = PlaylistStorage(path)

    # Load returns a default collection (no exception bubbles).
    collection = storage.load_all()
    assert isinstance(collection, PlaylistCollection)
    # Default has at least the DEFAULT_PLAYLIST_NAME playlist.
    assert any(p.name == DEFAULT_PLAYLIST_NAME for p in collection.playlists)

    # Bad file got quarantined.
    quarantined = _list_corrupt_quarantine(tmp_path, "playlist.json")
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ this is not valid json ]"

    # Original path is now gone (renamed). A subsequent save_all()
    # would re-create it; load_all() handles this case correctly
    # (no longer exists -> returns default).
    assert not path.exists()


def test_schedule_storage_recovers_from_malformed_json(tmp_path: Path):
    path = tmp_path / "schedule.json"
    path.write_text("garbage{")
    storage = ScheduleStorage(path)
    schedule = storage.load()
    assert isinstance(schedule, Schedule)
    assert schedule.rules == []
    assert len(_list_corrupt_quarantine(tmp_path, "schedule.json")) == 1


def test_flock_storage_recovers_from_malformed_json(tmp_path: Path):
    path = tmp_path / "flock.json"
    path.write_text("not-json")
    storage = FlockStorage(path)
    flock = storage.load()
    assert isinstance(flock, Flock)
    assert flock.peers == []
    assert len(_list_corrupt_quarantine(tmp_path, "flock.json")) == 1


def test_settings_storage_recovers_from_malformed_json(tmp_path: Path):
    path = tmp_path / "settings.json"
    path.write_text("{ truncated ")
    storage = SettingsStorage(path)
    settings = storage.load()
    assert isinstance(settings, SystemSettings)
    # SettingsStorage.load on recovery calls self.save(fresh) so the
    # original path is back in place (overwritten with valid defaults).
    assert path.exists()
    # The bad bytes are preserved at the quarantine sibling.
    assert len(_list_corrupt_quarantine(tmp_path, "settings.json")) == 1


def test_tombstone_storage_recovers_from_malformed_json(tmp_path: Path):
    path = tmp_path / "tombstones.json"
    path.write_text("nope")
    storage = TombstoneStorage(path)
    log = storage.load()
    assert isinstance(log, TombstoneLog)
    assert log.tombstones == []
    assert len(_list_corrupt_quarantine(tmp_path, "tombstones.json")) == 1


# --- Pydantic schema validation failure path ---


def test_flock_storage_recovers_from_schema_mismatch(tmp_path: Path):
    """Well-formed JSON that violates the Pydantic schema also
    quarantines. Catches the "operator edited a UUID into a string
    that isn't a UUID" failure shape. Flock is the simplest target
    because its schema is fully strict-Pydantic (no _coerce_to_*
    legacy-migration wrapper)."""
    path = tmp_path / "flock.json"
    # peers must be list[FlockPeer] -- a string fails Pydantic.
    path.write_text('{"schema_version": 1, "peers": "not-a-list"}')
    storage = FlockStorage(path)
    flock = storage.load()
    assert isinstance(flock, Flock)
    assert flock.peers == []
    assert len(_list_corrupt_quarantine(tmp_path, "flock.json")) == 1


# --- the success path still works ---


def test_storage_load_unchanged_when_file_valid(tmp_path: Path):
    """Sanity guard: the recovery wrapping doesn't alter the
    valid-file load path. Valid bytes produce a clean load + no
    quarantine sibling."""
    storage = FlockStorage(tmp_path / "flock.json")
    # Save defaults so a file exists.
    storage.save(storage.load())
    reloaded = storage.load()
    assert isinstance(reloaded, Flock)
    assert _list_corrupt_quarantine(tmp_path, "flock.json") == []
