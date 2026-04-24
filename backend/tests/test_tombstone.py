"""Unit tests for the tombstone log (recently-deleted content breadcrumbs)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.tombstone import (
    TOMBSTONE_SCHEMA_VERSION,
    TombstoneLog,
    TombstoneStorage,
)


@pytest.fixture
def storage(tmp_path: Path) -> TombstoneStorage:
    return TombstoneStorage(tmp_path / "tombstones.json")


def test_empty_log_serializes_with_schema_version():
    log = TombstoneLog()
    dumped = json.loads(log.model_dump_json())
    assert dumped == {
        "schema_version": TOMBSTONE_SCHEMA_VERSION,
        "tombstones": [],
    }


def test_load_on_missing_file_returns_empty_log(storage: TombstoneStorage):
    log = storage.load()
    assert log.tombstones == []
    assert log.schema_version == TOMBSTONE_SCHEMA_VERSION


def test_add_then_load_round_trips(storage: TombstoneStorage):
    cid = uuid4()
    stone = storage.add(cid)
    loaded = storage.load()
    assert [t.content_id for t in loaded.tombstones] == [cid]
    assert loaded.tombstones[0].deleted_at == stone.deleted_at


def test_add_same_id_twice_refreshes_timestamp(storage: TombstoneStorage):
    cid = uuid4()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 2, 1, tzinfo=timezone.utc)
    storage.add(cid, now=t0)
    storage.add(cid, now=t1)
    loaded = storage.load()
    assert len(loaded.tombstones) == 1
    assert loaded.tombstones[0].deleted_at == t1


def test_list_active_filters_out_expired(tmp_path: Path):
    storage = TombstoneStorage(tmp_path / "t.json", ttl_days=30)
    fresh = uuid4()
    stale = uuid4()
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    storage.add(fresh, now=now - timedelta(days=5))
    storage.add(stale, now=now - timedelta(days=40))
    active = storage.list_active(now=now)
    assert {t.content_id for t in active} == {fresh}


def test_prune_expired_removes_from_disk(tmp_path: Path):
    storage = TombstoneStorage(tmp_path / "t.json", ttl_days=30)
    now = datetime(2026, 4, 24, tzinfo=timezone.utc)
    storage.add(uuid4(), now=now - timedelta(days=40))
    storage.add(uuid4(), now=now - timedelta(days=5))
    removed = storage.prune_expired(now=now)
    assert removed == 1
    # Survivor persists.
    assert len(storage.load().tombstones) == 1


def test_save_is_atomic_leaves_no_tmp(tmp_path: Path, storage: TombstoneStorage):
    storage.add(uuid4())
    assert (tmp_path / "tombstones.json").exists()
    assert not (tmp_path / "tombstones.json.tmp").exists()


def test_save_creates_parent_directory_if_missing(tmp_path: Path):
    storage = TombstoneStorage(tmp_path / "nested" / "deep" / "t.json")
    storage.add(uuid4())
    assert (tmp_path / "nested" / "deep" / "t.json").exists()


def test_load_parses_hand_written_file(tmp_path: Path):
    path = tmp_path / "tombstones.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tombstones": [
                    {
                        "content_id": "22222222-2222-2222-2222-222222222222",
                        "deleted_at": "2026-04-24T12:00:00+00:00",
                    }
                ],
            }
        )
    )
    log = TombstoneStorage(path).load()
    assert len(log.tombstones) == 1
    assert str(log.tombstones[0].content_id) == "22222222-2222-2222-2222-222222222222"
