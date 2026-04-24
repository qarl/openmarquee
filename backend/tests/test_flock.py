"""Unit tests for the flock (peer openMarquee devices) storage + model."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from openmarquee.flock import (
    FLOCK_SCHEMA_VERSION,
    Flock,
    FlockPeer,
    FlockStorage,
)


@pytest.fixture
def storage(tmp_path: Path) -> FlockStorage:
    return FlockStorage(tmp_path / "flock.json")


# --- Flock model ---


def test_empty_flock_serializes_with_schema_version():
    f = Flock()
    dumped = json.loads(f.model_dump_json())
    assert dumped == {"schema_version": FLOCK_SCHEMA_VERSION, "peers": []}


def test_peer_get_stable_uuid_on_construction():
    p = FlockPeer(address="lobby.ts.net")
    assert p.id is not None
    # Default: not synced, never seen, no cached name.
    assert p.sync is False
    assert p.name is None
    assert p.last_seen_at is None


def test_peer_rejects_empty_address():
    with pytest.raises(ValueError):
        FlockPeer(address="")


def test_peer_rejects_scheme_path_or_weird_chars():
    # Guards the sync HTTP client from SSRF-shaped inputs. `host:port` is
    # explicitly allowed (needed for non-default-port peers).
    for bad in ("http://foo", "foo/bar", "a b", "foo@bar", "foo:notaport"):
        with pytest.raises(ValueError):
            FlockPeer(address=bad)


def test_peer_accepts_dns_name_ipv4_and_host_port():
    assert FlockPeer(address="lobby.ts.net").address == "lobby.ts.net"
    assert FlockPeer(address="100.64.1.5").address == "100.64.1.5"
    assert FlockPeer(address="127.0.0.1:9877").address == "127.0.0.1:9877"
    assert FlockPeer(address="lobby.ts.net:8080").address == "lobby.ts.net:8080"


def test_peer_address_is_lowercased_and_stripped():
    p = FlockPeer(address="  Lobby.TS.Net  ")
    assert p.address == "lobby.ts.net"


def test_peer_added_at_is_tz_aware_utc():
    p = FlockPeer(address="lobby.ts.net")
    assert p.added_at.tzinfo is not None


# --- Storage round-trip ---


def test_load_on_missing_file_returns_empty_flock(storage: FlockStorage):
    f = storage.load()
    assert f.peers == []
    assert f.schema_version == FLOCK_SCHEMA_VERSION


def test_load_parses_hand_written_file(tmp_path: Path):
    # Locks the on-disk contract so Phase 2+ fixtures / migrations can
    # author flock.json directly without surprise pydantic rejects.
    path = tmp_path / "flock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "peers": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "address": "lobby.ts.net",
                        "name": "Lobby Sign",
                        "sync": True,
                        "added_at": "2026-04-22T12:00:00+00:00",
                        "last_seen_at": None,
                    }
                ],
            }
        )
    )
    flock = FlockStorage(path).load()
    assert len(flock.peers) == 1
    assert flock.peers[0].address == "lobby.ts.net"
    assert flock.peers[0].sync is True
    assert flock.peers[0].name == "Lobby Sign"


def test_add_then_load_round_trips(storage: FlockStorage):
    peer = storage.add(address="lobby.ts.net")
    loaded = storage.load()
    assert [p.id for p in loaded.peers] == [peer.id]
    assert loaded.peers[0].address == "lobby.ts.net"


def test_add_rejects_duplicate_address(storage: FlockStorage):
    storage.add(address="lobby.ts.net")
    with pytest.raises(ValueError, match="already in flock"):
        storage.add(address="lobby.ts.net")


def test_add_treats_case_as_equivalent_for_dedup(storage: FlockStorage):
    storage.add(address="lobby.ts.net")
    with pytest.raises(ValueError, match="already in flock"):
        storage.add(address="Lobby.TS.Net")


def test_remove_returns_true_when_peer_existed(storage: FlockStorage):
    peer = storage.add(address="lobby.ts.net")
    assert storage.remove(peer.id) is True
    assert storage.load().peers == []


def test_remove_returns_false_when_peer_absent(storage: FlockStorage):
    assert storage.remove(uuid4()) is False


def test_update_toggles_sync_flag(storage: FlockStorage):
    peer = storage.add(address="lobby.ts.net")
    updated = storage.update(peer.id, sync=True)
    assert updated is not None
    assert updated.sync is True
    assert storage.load().peers[0].sync is True


def test_update_can_stamp_last_seen(storage: FlockStorage):
    peer = storage.add(address="lobby.ts.net")
    before = peer.last_seen_at
    updated = storage.update(peer.id, mark_seen=True, name="Lobby Sign")
    assert updated.last_seen_at is not None
    assert updated.last_seen_at != before
    assert updated.name == "Lobby Sign"


def test_update_returns_none_for_unknown_peer(storage: FlockStorage):
    assert storage.update(uuid4(), sync=True) is None


def test_save_is_atomic_leaves_no_tmp(storage: FlockStorage, tmp_path: Path):
    storage.add(address="lobby.ts.net")
    assert (tmp_path / "flock.json").exists()
    # The .tmp file would only linger on crash; happy-path save cleans it up.
    assert not (tmp_path / "flock.json.tmp").exists()


def test_save_creates_parent_directory_if_missing(tmp_path: Path):
    storage = FlockStorage(tmp_path / "nested" / "deep" / "flock.json")
    storage.add(address="lobby.ts.net")
    assert (tmp_path / "nested" / "deep" / "flock.json").exists()
