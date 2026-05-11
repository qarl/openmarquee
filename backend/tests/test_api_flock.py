"""Integration tests for the flock REST API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _flock_storage_singleton,
    _flock_sync_singleton,
    _tombstone_storage_singleton,
    get_content_storage,
    get_flock_storage,
    get_flock_sync,
    get_tombstone_storage,
)
from openmarquee.flock import FlockStorage
from openmarquee.flock_sync import NotifyKind
from openmarquee.tombstone import TombstoneStorage


# Minimal valid 1x1 PNG for content upload endpoints (Pillow-generated).
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nG"
    "P4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


@pytest.fixture
def storage(tmp_path: Path) -> FlockStorage:
    return FlockStorage(tmp_path / "flock.json")


@pytest.fixture
def client(storage: FlockStorage) -> TestClient:
    app.dependency_overrides[get_flock_storage] = lambda: storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _flock_storage_singleton.cache_clear()


@pytest.fixture
def manifest_client(tmp_path: Path):
    """Client with flock + content + tombstone overrides — for manifest tests
    that need to exercise all three together."""
    flock = FlockStorage(tmp_path / "flock.json")
    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    app.dependency_overrides[get_flock_storage] = lambda: flock
    app.dependency_overrides[get_content_storage] = lambda: content
    app.dependency_overrides[get_tombstone_storage] = lambda: tombstones
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _flock_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()
        _tombstone_storage_singleton.cache_clear()


def test_get_empty_flock_returns_empty_peer_list(client: TestClient):
    response = client.get("/api/flock")
    assert response.status_code == 200
    body = response.json()
    assert body["peers"] == []
    assert body["schema_version"] == 1


def test_discover_returns_empty_when_no_tailscale(client: TestClient, monkeypatch):
    """Phase B.5: dev box / macOS without `tailscale` in PATH gets
    an empty candidates list + source='none'. UI falls back to the
    manual-typed address path that Phase A already supports."""
    import openmarquee.api_flock as mod

    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)
    response = client.get("/api/flock/discover")
    assert response.status_code == 200
    body = response.json()
    assert body == {"candidates": [], "source": "none"}


def test_discover_parses_tailscale_status_json(client: TestClient, monkeypatch):
    """Happy path: tailscale binary exists, returns the documented
    JSON shape; we parse + filter Peer table to (hostname, address)
    tuples. DNSName trailing-dot stripped + lowercased to match
    FlockPeer.address normalization."""
    import openmarquee.api_flock as mod

    payload = {
        "Self": {"HostName": "this", "DNSName": "this.tn.ts.net."},
        "Peer": {
            "key1": {
                "HostName": "lobby",
                "DNSName": "Lobby.tn.ts.net.",
                "Online": True,
            },
            "key2": {
                # Offline peer: filtered out per default.
                "HostName": "store",
                "DNSName": "store.tn.ts.net.",
                "Online": False,
            },
            "key3": {
                "HostName": "cafeteria",
                "DNSName": "cafeteria.tn.ts.net.",
                "Online": True,
            },
        },
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    response = client.get("/api/flock/discover")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "tailscale"
    # Two online peers, sorted alphabetically by hostname.
    addrs = [c["address"] for c in body["candidates"]]
    assert addrs == ["cafeteria.tn.ts.net", "lobby.tn.ts.net"]
    hostnames = [c["hostname"] for c in body["candidates"]]
    assert hostnames == ["cafeteria", "lobby"]
    # None already in flock (this client fixture has empty storage).
    assert all(c["already_in_flock"] is False for c in body["candidates"])


def test_discover_marks_already_added_peers(client: TestClient, monkeypatch):
    """Already-flocked peers come back with already_in_flock=True
    so the UI can disable them rather than offer a re-add (which
    would 409 anyway)."""
    import openmarquee.api_flock as mod

    # Pre-populate the flock with one of the candidates.
    client.post("/api/flock", json={"address": "lobby.tn.ts.net"})

    payload = {
        "Peer": {
            "key1": {
                "HostName": "lobby",
                "DNSName": "lobby.tn.ts.net.",
                "Online": True,
            },
        },
    }

    class FakeProc:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    body = client.get("/api/flock/discover").json()
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["already_in_flock"] is True


def test_discover_handles_tailscale_failure_gracefully(
    client: TestClient, monkeypatch
):
    """tailscale binary present but returns non-zero (network blip,
    daemon down) — fallback to empty + source='none'. No 500."""
    import openmarquee.api_flock as mod

    class FakeProc:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    body = client.get("/api/flock/discover").json()
    assert body == {"candidates": [], "source": "none"}


def test_discover_handles_malformed_json(client: TestClient, monkeypatch):
    """tailscale binary returns 0 but stdout isn't valid JSON
    (transient garbage, version mismatch) — fallback gracefully."""
    import openmarquee.api_flock as mod

    class FakeProc:
        returncode = 0
        stdout = "not json at all"

    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/tailscale")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    body = client.get("/api/flock/discover").json()
    assert body == {"candidates": [], "source": "none"}


def test_post_adds_a_peer(client: TestClient):
    response = client.post("/api/flock", json={"address": "lobby.ts.net"})
    assert response.status_code == 201
    body = response.json()
    assert body["address"] == "lobby.ts.net"
    assert body["sync"] is False
    assert body["id"]  # UUID generated server-side


def test_post_rejects_duplicate_address_as_409(client: TestClient):
    client.post("/api/flock", json={"address": "lobby.ts.net"})
    response = client.post("/api/flock", json={"address": "lobby.ts.net"})
    assert response.status_code == 409
    assert "already" in response.json()["detail"]


def test_post_duplicate_response_does_not_leak_exception_string(
    client: TestClient,
):
    """Batch 11.2 / sweep #5 #8: the 409 detail must NOT carry the
    raw ValueError message (which currently includes the address +
    quoting from f-string interpolation). Generic operator-helpful
    message only; internal detail goes to the WARNING log."""
    client.post("/api/flock", json={"address": "lobby.ts.net"})
    response = client.post("/api/flock", json={"address": "lobby.ts.net"})
    assert response.status_code == 409
    detail = response.json()["detail"]
    # Specifically NOT containing the address value (quoted-form leak)
    # nor the ValueError keyword "peer with address".
    assert "lobby.ts.net" not in detail
    assert "peer with address" not in detail.lower()


def test_post_validates_empty_address(client: TestClient):
    response = client.post("/api/flock", json={"address": ""})
    assert response.status_code == 422


def test_post_validates_address_too_long(client: TestClient):
    response = client.post("/api/flock", json={"address": "x" * 254})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad_address",
    ["http://foo", "foo/bar", "a b", "foo@bar", "foo:notaport"],
)
def test_post_rejects_malformed_addresses_as_422(
    client: TestClient, bad_address: str
):
    response = client.post("/api/flock", json={"address": bad_address})
    assert response.status_code == 422


def test_post_accepts_host_with_port(client: TestClient):
    response = client.post("/api/flock", json={"address": "100.64.1.5:9877"})
    assert response.status_code == 201
    assert response.json()["address"] == "100.64.1.5:9877"


def test_post_strips_whitespace_and_lowercases(client: TestClient):
    # Operator pastes from a Tailscale UI with stray whitespace + mixed
    # case — don't punish them with a 422, just normalize.
    response = client.post(
        "/api/flock", json={"address": "  Lobby.TS.Net  "}
    )
    assert response.status_code == 201
    assert response.json()["address"] == "lobby.ts.net"
    # And a follow-up POST with the canonical form is a 409 duplicate.
    dup = client.post("/api/flock", json={"address": "lobby.ts.net"})
    assert dup.status_code == 409


def test_patch_toggles_sync_flag(client: TestClient):
    created = client.post("/api/flock", json={"address": "lobby.ts.net"}).json()
    peer_id = created["id"]
    response = client.patch(f"/api/flock/{peer_id}", json={"sync": True})
    assert response.status_code == 200
    assert response.json()["sync"] is True


def test_patch_can_update_name(client: TestClient):
    created = client.post("/api/flock", json={"address": "lobby.ts.net"}).json()
    peer_id = created["id"]
    response = client.patch(f"/api/flock/{peer_id}", json={"name": "Lobby Sign"})
    assert response.status_code == 200
    assert response.json()["name"] == "Lobby Sign"


def test_patch_returns_404_for_unknown_peer(client: TestClient):
    response = client.patch(f"/api/flock/{uuid4()}", json={"sync": True})
    assert response.status_code == 404


def test_delete_removes_peer(client: TestClient):
    created = client.post("/api/flock", json={"address": "lobby.ts.net"}).json()
    peer_id = created["id"]
    response = client.delete(f"/api/flock/{peer_id}")
    assert response.status_code == 204
    # Follow-up GET reports an empty list.
    assert client.get("/api/flock").json()["peers"] == []


def test_delete_returns_404_for_unknown_peer(client: TestClient):
    response = client.delete(f"/api/flock/{uuid4()}")
    assert response.status_code == 404


class _RecordingFlockSync:
    """Captures notify_peers / ingest_push / gossip_add / apply_hello /
    probe_peer_name / announce_sync_to_peer calls so tests can assert
    the routes actually hook them. Stub-shaped — none of the methods
    do anything beyond recording."""

    def __init__(self):
        self.pushes: list[tuple[UUID, NotifyKind]] = []
        self.ingests: list[tuple[UUID, NotifyKind, str, datetime]] = []
        # Phase B gossip-on-add: gossip_add fires from POST /api/flock,
        # apply_hello fires from POST /api/flock/hello.
        self.gossips: list[str] = []
        self.hellos: list[str] = []
        self.probes: list[str] = []
        self.sync_announces: list[tuple[str, bool]] = []

    async def notify_peers(self, content_id, kind):
        self.pushes.append((content_id, kind))

    async def ingest_push(self, content_id, kind, sender_address, at):
        self.ingests.append((content_id, kind, sender_address, at))

    async def gossip_add(self, address):
        self.gossips.append(address)

    def apply_hello(self, address):
        self.hellos.append(address)
        return True

    async def probe_peer_name(self, address):
        self.probes.append(address)

    async def announce_sync_to_peer(self, address, sync):
        self.sync_announces.append((address, sync))


@pytest.fixture
def recording_client(tmp_path: Path):
    """TestClient with flock + content + tombstone + a recording sync stub."""
    flock = FlockStorage(tmp_path / "flock.json")
    # Register the peer the notify tests push from — the allowlist check
    # on /api/flock/notify refuses senders we don't know.
    flock.add(address="peer.ts.net")
    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    recorder = _RecordingFlockSync()

    app.dependency_overrides[get_flock_storage] = lambda: flock
    app.dependency_overrides[get_content_storage] = lambda: content
    app.dependency_overrides[get_tombstone_storage] = lambda: tombstones
    app.dependency_overrides[get_flock_sync] = lambda: recorder
    try:
        with TestClient(app) as test_client:
            yield test_client, recorder, flock
    finally:
        app.dependency_overrides.clear()
        _flock_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()
        _tombstone_storage_singleton.cache_clear()
        _flock_sync_singleton.cache_clear()


_TEST_NOTIFY_AT = "2026-04-24T12:00:00+00:00"


def test_post_flock_schedules_gossip_add(recording_client):
    """SYSTEM_SPEC §13: when an operator adds a peer via POST /api/flock,
    the route schedules a gossip_add background task carrying the new
    peer's address. The fan-out itself (hello-ping new peer + forward-
    notify existing peers) is covered by FlockSync tests; here we just
    verify the route-level wiring."""
    client, recorder, _ = recording_client
    response = client.post("/api/flock", json={"address": "newpeer.ts.net"})
    assert response.status_code == 201
    # Background task ran inline under TestClient.
    assert "newpeer.ts.net" in recorder.gossips


def test_hello_endpoint_delegates_to_sync(recording_client):
    """POST /api/flock/hello calls FlockSync.apply_hello with the
    introduced peer's address. Returns 204."""
    client, recorder, _ = recording_client
    response = client.post(
        "/api/flock/hello", json={"address": "stranger.ts.net"}
    )
    assert response.status_code == 204
    assert recorder.hellos == ["stranger.ts.net"]


def test_hello_endpoint_idempotent_for_known_peer(recording_client):
    """Duplicate hellos for the same address are 204 — gossip races
    can land the same introduction twice. apply_hello's idempotent
    semantics handle the no-op; the route just returns success."""
    client, recorder, _ = recording_client
    client.post("/api/flock/hello", json={"address": "stranger.ts.net"})
    response = client.post(
        "/api/flock/hello", json={"address": "stranger.ts.net"}
    )
    assert response.status_code == 204
    assert recorder.hellos == ["stranger.ts.net", "stranger.ts.net"]


def test_hello_endpoint_rejects_malformed_address(recording_client):
    """Same SSRF-shape gating as POST /api/flock — schemes, paths,
    spaces all rejected at the wire layer before apply_hello sees
    anything."""
    client, recorder, _ = recording_client
    for bad in ("http://foo", "foo/bar", "a b", "foo@bar"):
        response = client.post("/api/flock/hello", json={"address": bad})
        assert response.status_code == 422, f"expected 422 for {bad!r}"
    # apply_hello not called for any of them.
    assert recorder.hellos == []


def test_hello_endpoint_schedules_name_probe_on_first_hello(recording_client):
    """Phase B.4: when an inbound hello adds a never-seen peer
    (apply_hello returns True), schedule the same probe_peer_name
    backfill that POST /api/flock does. Without it, the peer would
    appear address-only with no sign_name until the next pull-worker
    tick (potentially never if sync=False stays the default).

    Loop-safety: probe_peer_name only reads /api/settings, doesn't
    gossip — no cascade risk."""
    client, recorder, _ = recording_client
    response = client.post(
        "/api/flock/hello", json={"address": "stranger.ts.net"}
    )
    assert response.status_code == 204
    # apply_hello returned True (recorder default), so the route
    # scheduled probe_peer_name as a background task.
    assert "stranger.ts.net" in recorder.probes


def test_hello_endpoint_skips_name_probe_for_known_peer(recording_client):
    """Idempotent hello (peer already in flock) -> apply_hello returns
    False -> no probe scheduled. Avoids re-probing every time a peer
    gossips us about an existing peer in a 3+-device flock."""
    client, recorder, _ = recording_client
    # Override apply_hello to return False (already-known case).
    recorder.apply_hello = lambda address: (
        recorder.hellos.append(address) or False
    )
    response = client.post(
        "/api/flock/hello", json={"address": "known.ts.net"}
    )
    assert response.status_code == 204
    assert recorder.hellos == ["known.ts.net"]
    # No probe — apply_hello returned False so nothing was newly added.
    assert recorder.probes == []


def test_hello_endpoint_does_not_require_known_sender(recording_client):
    """Unlike /notify and /sync-announce (which 403 senders not in the
    flock), /hello accepts addresses we don't yet know about — that's
    the entire point of an introduction protocol. A new peer reaching
    out for the first time MUST work."""
    client, recorder, flock = recording_client
    # The recording_client fixture pre-adds peer.ts.net; stranger.ts.net
    # is genuinely unknown. Should still 204.
    response = client.post(
        "/api/flock/hello", json={"address": "stranger.ts.net"}
    )
    assert response.status_code == 204
    assert recorder.hellos == ["stranger.ts.net"]


def test_notify_endpoint_delegates_to_sync(recording_client):
    client, recorder, _ = recording_client
    cid = uuid4()
    response = client.post(
        "/api/flock/notify",
        json={
            "content_id": str(cid),
            "kind": "updated",
            "sender_address": "peer.ts.net",
            "at": _TEST_NOTIFY_AT,
        },
    )
    assert response.status_code == 204
    assert len(recorder.ingests) == 1
    cid_seen, kind_seen, sender_seen, at_seen = recorder.ingests[0]
    assert cid_seen == cid
    assert kind_seen == "updated"
    assert sender_seen == "peer.ts.net"
    assert at_seen == datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)


def test_notify_endpoint_rejects_unknown_kind(recording_client):
    client, _, _ = recording_client
    response = client.post(
        "/api/flock/notify",
        json={
            "content_id": str(uuid4()),
            "kind": "bogus",
            "sender_address": "peer.ts.net",
            "at": _TEST_NOTIFY_AT,
        },
    )
    assert response.status_code == 422


def test_notify_endpoint_rejects_malformed_sender_address(recording_client):
    client, _, _ = recording_client
    response = client.post(
        "/api/flock/notify",
        json={
            "content_id": str(uuid4()),
            "kind": "updated",
            "sender_address": "http://evil/",
            "at": _TEST_NOTIFY_AT,
        },
    )
    assert response.status_code == 422


def test_notify_endpoint_rejects_sender_not_in_flock(recording_client):
    # A node on the tailnet that we haven't added can't inject content.
    client, recorder, _ = recording_client
    response = client.post(
        "/api/flock/notify",
        json={
            "content_id": str(uuid4()),
            "kind": "updated",
            "sender_address": "stranger.ts.net",
            "at": _TEST_NOTIFY_AT,
        },
    )
    assert response.status_code == 403
    assert recorder.ingests == []


def test_notify_endpoint_accepts_case_varying_sender(recording_client):
    # Allowlist lookup is case-insensitive (DNS semantics).
    client, recorder, _ = recording_client
    response = client.post(
        "/api/flock/notify",
        json={
            "content_id": str(uuid4()),
            "kind": "updated",
            "sender_address": "PEER.TS.NET",
            "at": _TEST_NOTIFY_AT,
        },
    )
    assert response.status_code == 204
    assert recorder.ingests and recorder.ingests[0][2] == "peer.ts.net"


def test_text_slide_post_enqueues_updated_push(recording_client):
    client, recorder, _ = recording_client
    response = client.post(
        "/api/content/text-slides",
        json={
            "name": "Hook test",
            "duration_ms": 3000,
            "text_layers": [{"text": "Hook"}],
            "png_base64": _TINY_PNG_B64,
        },
    )
    assert response.status_code == 200
    slide_id = UUID(response.json()["id"])
    assert recorder.pushes == [(slide_id, "updated")]


def test_content_delete_enqueues_deleted_push(recording_client):
    client, recorder, _ = recording_client
    created = client.post(
        "/api/content/text-slides",
        json={
            "name": "Goner",
            "duration_ms": 3000,
            "text_layers": [{"text": "Goner"}],
            "png_base64": _TINY_PNG_B64,
        },
    ).json()
    slide_id = UUID(created["id"])
    response = client.delete(f"/api/content/{slide_id}")
    assert response.status_code == 204
    # First push is the create ("updated"), second is the delete ("deleted").
    kinds = [k for _, k in recorder.pushes]
    assert kinds == ["updated", "deleted"]
    assert recorder.pushes[-1] == (slide_id, "deleted")


def test_peer_ingested_content_does_not_trigger_outbound_push(
    recording_client, tmp_path: Path
):
    """Loop-prevention invariant: FlockSync ingests content via
    ContentStorage.save() DIRECTLY (not via the HTTP route), so the push
    hook — which lives on the route — never fires for ingested content.
    Without this, A→B→A→... echoes on every sync round."""
    client, recorder, _ = recording_client
    # Call ContentStorage.save() the same way flock_sync._ingest_update
    # does — bypassing /api/content/text-slides.
    from openmarquee.content import TextLayer, TextSlide

    overridden_content = app.dependency_overrides[get_content_storage]()
    slide = TextSlide(name="Ingested", text_layers=[TextLayer(text="from peer")])
    overridden_content.save(slide, b"", updated_at=datetime.now(timezone.utc))

    # A follow-up API roundtrip is needed to flush any pending backgrounds
    # (TestClient drains them synchronously at context exit).
    _ = client.get("/api/flock").status_code
    assert recorder.pushes == []


def test_manifest_empty_when_no_content(manifest_client: TestClient):
    response = manifest_client.get("/api/flock/manifest")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["entries"] == []
    assert body["tombstones"] == []


def _post_text_slide(client: TestClient, name: str) -> str:
    """Helper: seed one text slide via the real API, return its id."""
    response = client.post(
        "/api/content/text-slides",
        json={
            "name": name,
            "duration_ms": 5000,
            "text_layers": [{"text": name}],
            "png_base64": _TINY_PNG_B64,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_manifest_lists_held_content_with_type_and_timestamp(
    manifest_client: TestClient,
):
    slide_id = _post_text_slide(manifest_client, "Opening")
    response = manifest_client.get("/api/flock/manifest")
    assert response.status_code == 200
    body = response.json()
    assert len(body["entries"]) == 1
    entry = body["entries"][0]
    assert entry["content_id"] == slide_id
    assert entry["content_type"] == "text_slide"
    assert entry["updated_at"]  # ISO-8601 string, not empty


def test_manifest_surfaces_tombstone_after_delete(manifest_client: TestClient):
    slide_id = _post_text_slide(manifest_client, "Will-be-deleted")
    delete_resp = manifest_client.delete(f"/api/content/{slide_id}")
    assert delete_resp.status_code == 204

    response = manifest_client.get("/api/flock/manifest")
    body = response.json()
    assert body["entries"] == []  # item is gone
    assert len(body["tombstones"]) == 1
    assert body["tombstones"][0]["content_id"] == slide_id


def test_delete_of_unknown_id_leaves_no_tombstone(
    manifest_client: TestClient, tmp_path: Path
):
    # A DELETE on an id we never had must not mint a tombstone — peers
    # would otherwise learn about a deletion that never happened and drop
    # content they rightfully hold.
    response = manifest_client.delete(f"/api/content/{uuid4()}")
    assert response.status_code == 404
    manifest = manifest_client.get("/api/flock/manifest").json()
    assert manifest["tombstones"] == []


def test_manifest_filters_expired_tombstones(tmp_path: Path):
    # Build a manifest client whose tombstone log is seeded directly with
    # one fresh + one expired entry, and confirm only the fresh one surfaces.
    from datetime import datetime, timedelta, timezone

    from openmarquee.content.storage import ContentStorage
    from openmarquee.dependencies import (
        _content_storage_singleton,
        _flock_storage_singleton,
        _tombstone_storage_singleton,
        get_content_storage,
        get_flock_storage,
        get_tombstone_storage,
    )
    from openmarquee.flock import FlockStorage
    from openmarquee.tombstone import TOMBSTONE_TTL_DAYS, TombstoneStorage

    flock = FlockStorage(tmp_path / "flock.json")
    content = ContentStorage(tmp_path / "content")
    tombstones = TombstoneStorage(tmp_path / "tombstones.json")
    now = datetime.now(timezone.utc)
    fresh_id = uuid4()
    stale_id = uuid4()
    tombstones.add(fresh_id, now=now - timedelta(days=1))
    tombstones.add(stale_id, now=now - timedelta(days=TOMBSTONE_TTL_DAYS + 5))

    app.dependency_overrides[get_flock_storage] = lambda: flock
    app.dependency_overrides[get_content_storage] = lambda: content
    app.dependency_overrides[get_tombstone_storage] = lambda: tombstones
    try:
        with TestClient(app) as test_client:
            body = test_client.get("/api/flock/manifest").json()
    finally:
        app.dependency_overrides.clear()
        _flock_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()
        _tombstone_storage_singleton.cache_clear()

    surfaced = {t["content_id"] for t in body["tombstones"]}
    assert surfaced == {str(fresh_id)}


def test_full_lifecycle_through_the_api(client: TestClient):
    # Add two peers.
    a = client.post("/api/flock", json={"address": "lobby.ts.net"}).json()
    b = client.post("/api/flock", json={"address": "cafeteria.ts.net"}).json()
    # GET surfaces both.
    addresses = {p["address"] for p in client.get("/api/flock").json()["peers"]}
    assert addresses == {"lobby.ts.net", "cafeteria.ts.net"}
    # Toggle sync on one.
    client.patch(f"/api/flock/{a['id']}", json={"sync": True})
    # Drop the other.
    client.delete(f"/api/flock/{b['id']}")
    # Final state: one peer, synced.
    body = client.get("/api/flock").json()
    assert len(body["peers"]) == 1
    assert body["peers"][0]["id"] == a["id"]
    assert body["peers"][0]["sync"] is True
