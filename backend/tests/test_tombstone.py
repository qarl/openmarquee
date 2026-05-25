"""Unit tests for the tombstone log (recently-deleted content breadcrumbs)."""

import json
from datetime import UTC, datetime, timedelta
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
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 2, 1, tzinfo=UTC)
    storage.add(cid, now=t0)
    storage.add(cid, now=t1)
    loaded = storage.load()
    assert len(loaded.tombstones) == 1
    assert loaded.tombstones[0].deleted_at == t1


def test_list_active_filters_out_expired(tmp_path: Path):
    storage = TombstoneStorage(tmp_path / "t.json", ttl_days=30)
    fresh = uuid4()
    stale = uuid4()
    now = datetime(2026, 4, 24, tzinfo=UTC)
    storage.add(fresh, now=now - timedelta(days=5))
    storage.add(stale, now=now - timedelta(days=40))
    active = storage.list_active(now=now)
    assert {t.content_id for t in active} == {fresh}


def test_naive_deleted_at_coerces_to_utc_and_list_active_doesnt_raise(
    tmp_path: Path,
):
    """Round-24 correctness regression: a naive (no-offset) deleted_at
    in tombstones.json must coerce to aware UTC at load time.

    Pre-fix the bare `datetime` field accepted naive ISO without
    raising; list_active's `t.deleted_at >= cutoff` then raised
    TypeError("can't compare offset-naive and offset-aware
    datetimes") since cutoff is aware UTC. Operator scenario:
    restored tombstones.json from a backup tool that strips offsets,
    OR hand-edited, OR imported from an older/peer device that
    wrote naive. Every subsequent sync round + prune call 500'd
    silently.

    Test: hand-write a tombstones.json with a NAIVE deleted_at;
    load + list_active; assert no exception + the value is treated
    as aware-UTC.
    """
    path = tmp_path / "tombstones.json"
    cid = uuid4()
    naive_iso = "2026-04-24T12:00:00"  # NO offset
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tombstones": [
                    {"content_id": str(cid), "deleted_at": naive_iso},
                ],
            }
        )
    )

    storage = TombstoneStorage(path, ttl_days=30)
    log = storage.load()
    # Load succeeded (no ValidationError → quarantine).
    assert len(log.tombstones) == 1
    # Coerced to aware UTC at the field validator.
    loaded_ts = log.tombstones[0].deleted_at
    assert loaded_ts.tzinfo is not None, (
        "naive deleted_at must be coerced to aware (else list_active "
        "raises TypeError on the aware-vs-naive compare)"
    )
    assert loaded_ts == datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC), (
        "coercion must interpret naive ISO as UTC (the only safe "
        "default since the file is always WRITTEN as UTC by save())"
    )

    # CRITICAL ASSERTION: list_active doesn't raise. Pre-fix this
    # raised TypeError("can't compare offset-naive and offset-aware
    # datetimes"), 500'ing every sync round indefinitely.
    now = datetime(2026, 4, 25, tzinfo=UTC)  # 1 day after the delete
    active = storage.list_active(now=now)  # would raise pre-fix
    assert {t.content_id for t in active} == {cid}, (
        "freshly-coerced tombstone within TTL window must be active"
    )

    # CRITICAL ASSERTION 2: prune_expired also doesn't raise.
    # Pre-fix this hit the same TypeError. Use a "now" past the TTL
    # so the coerced tombstone should actually prune (confirms both
    # the no-raise path AND the comparison-uses-coerced-tz behavior).
    far_future = datetime(2026, 6, 1, tzinfo=UTC)  # > 30 days after
    removed = storage.prune_expired(now=far_future)
    assert removed == 1, "expired (coerced-tz) tombstone must prune"


def test_prune_expired_removes_from_disk(tmp_path: Path):
    storage = TombstoneStorage(tmp_path / "t.json", ttl_days=30)
    now = datetime(2026, 4, 24, tzinfo=UTC)
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
