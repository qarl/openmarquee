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


def test_flock_round_trip_preserves_unknown_envelope_and_peer_fields(
    tmp_path: Path,
):
    """Round-13 forward-compat regression (closes the backend
    forward-compat series). Unknown ENVELOPE-LEVEL fields (e.g. a
    future top-level stat from backend N+1) AND unknown PER-PEER
    fields (e.g. a future capabilities list) must SURVIVE the
    FlockStorage.add / remove / update cycle.

    Pre-fix, Flock.model_validate ran under default extra="ignore"
    so both kinds of unknown were silently dropped on every load.
    Operator clicked remove-peer once -> every OTHER peer lost the
    forward-compat field too (and the envelope-level unknown was
    gone) because the whole list re-serialized via model_dump_json.

    Test: seed flock.json with an envelope-level unknown + two peers
    each carrying a different per-peer unknown. Exercise an add()
    cycle (which triggers the full load+save round-trip), assert
    every unknown survives. Then exercise remove() + update() cycles
    and re-assert.
    """
    path = tmp_path / "flock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                # Envelope-level forward-compat field.
                "_future_envelope_field": {
                    "added_in": "N+1",
                    "feature_flag": "flock_v2_health_summary",
                },
                "peers": [
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "address": "lobby.ts.net",
                        "name": "Lobby Sign",
                        "sync": True,
                        "added_at": "2026-04-22T12:00:00+00:00",
                        "last_seen_at": None,
                        # Per-peer forward-compat field (object shape).
                        "_future_capabilities": ["hdr", "p3", "60fps"],
                    },
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "address": "kitchen.ts.net",
                        "name": "Kitchen Sign",
                        "sync": False,
                        "added_at": "2026-04-22T12:01:00+00:00",
                        "last_seen_at": None,
                        # Per-peer forward-compat field (scalar shape).
                        "_future_pixel_density": 2.5,
                    },
                ],
            }
        )
    )
    storage = FlockStorage(path)

    # Cycle 1: add() triggers full load+save round-trip.
    storage.add(address="garage.ts.net")
    persisted = json.loads(path.read_text())
    assert persisted["_future_envelope_field"] == {
        "added_in": "N+1",
        "feature_flag": "flock_v2_health_summary",
    }, "envelope-level forward-compat field must survive add()"
    by_addr = {p["address"]: p for p in persisted["peers"]}
    assert by_addr["lobby.ts.net"]["_future_capabilities"] == [
        "hdr",
        "p3",
        "60fps",
    ], "per-peer forward-compat field must survive add() (object shape)"
    assert by_addr["kitchen.ts.net"]["_future_pixel_density"] == 2.5, (
        "per-peer forward-compat field must survive add() (scalar shape)"
    )
    assert "garage.ts.net" in by_addr, "the newly-added peer must be present (positive baseline)"

    # Cycle 2: remove() — the dispatch's documented attack on the
    # bug ("click remove-peer once -> all OTHER peers lose the
    # field"). Remove the garage peer just added; both lobby +
    # kitchen forward-compat fields must STILL be there afterwards.
    garage_peer_id = next(p.id for p in storage.load().peers if p.address == "garage.ts.net")
    storage.remove(garage_peer_id)
    persisted = json.loads(path.read_text())
    by_addr = {p["address"]: p for p in persisted["peers"]}
    assert persisted["_future_envelope_field"] == {
        "added_in": "N+1",
        "feature_flag": "flock_v2_health_summary",
    }, "envelope-level forward-compat must survive remove()"
    assert by_addr["lobby.ts.net"]["_future_capabilities"] == [
        "hdr",
        "p3",
        "60fps",
    ], "per-peer forward-compat must survive remove() (the bug scenario)"
    assert by_addr["kitchen.ts.net"]["_future_pixel_density"] == 2.5

    # Cycle 3: update() — touches a single peer's known fields but
    # round-trips the whole list, so unknowns must still survive.
    lobby_peer_id = next(p.id for p in storage.load().peers if p.address == "lobby.ts.net")
    storage.update(lobby_peer_id, name="Lobby Display")
    persisted = json.loads(path.read_text())
    by_addr = {p["address"]: p for p in persisted["peers"]}
    assert by_addr["lobby.ts.net"]["name"] == "Lobby Display", (
        "update() must take effect (positive baseline)"
    )
    assert by_addr["lobby.ts.net"]["_future_capabilities"] == [
        "hdr",
        "p3",
        "60fps",
    ], "per-peer forward-compat must survive update()"
    assert by_addr["kitchen.ts.net"]["_future_pixel_density"] == 2.5, (
        "OTHER peer's forward-compat must survive update() too"
    )
    assert persisted["_future_envelope_field"] == {
        "added_in": "N+1",
        "feature_flag": "flock_v2_health_summary",
    }, "envelope-level forward-compat must survive update()"


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
