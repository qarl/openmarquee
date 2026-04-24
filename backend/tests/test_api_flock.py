"""Integration tests for the flock REST API."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
    _flock_storage_singleton,
    get_flock_storage,
)
from openmarquee.flock import FlockStorage


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


def test_get_empty_flock_returns_empty_peer_list(client: TestClient):
    response = client.get("/api/flock")
    assert response.status_code == 200
    body = response.json()
    assert body["peers"] == []
    assert body["schema_version"] == 1


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


def test_post_validates_empty_address(client: TestClient):
    response = client.post("/api/flock", json={"address": ""})
    assert response.status_code == 422


def test_post_validates_address_too_long(client: TestClient):
    response = client.post("/api/flock", json={"address": "x" * 254})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "bad_address",
    ["http://foo", "foo:8080", "foo/bar", "a b", "foo@bar"],
)
def test_post_rejects_malformed_addresses_as_422(
    client: TestClient, bad_address: str
):
    response = client.post("/api/flock", json={"address": bad_address})
    assert response.status_code == 422


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
