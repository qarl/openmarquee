"""P1.2-A API: GET /api/network-supervisor/state observability."""

from __future__ import annotations

import os
from pathlib import Path  # noqa: F401 — kept for SettingsStorage fixture typing

import pytest
from fastapi.testclient import TestClient

# Test fixtures use OPENMARQUEE_DISABLE_AUTOSTART=1 implicitly via
# conftest; the supervisor singleton is still constructed lazily on
# first /api/network-supervisor/state call, so we test the API
# without booting the observe-loop.
os.environ.setdefault("OPENMARQUEE_DISABLE_AUTOSTART", "1")
os.environ.setdefault("OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR", "1")

from openmarquee.app import app
from openmarquee.dependencies import (
    _network_supervisor_singleton,
    _settings_storage_singleton,
    get_settings_storage,
)
from openmarquee.settings import SettingsStorage


@pytest.fixture
def storage(tmp_path: Path) -> SettingsStorage:
    return SettingsStorage(tmp_path / "settings.json")


@pytest.fixture
def client(storage: SettingsStorage) -> TestClient:
    app.dependency_overrides[get_settings_storage] = lambda: storage
    # Reset the supervisor singleton so each test gets a fresh
    # instance reading from THIS test's settings storage.
    _network_supervisor_singleton.cache_clear()
    _settings_storage_singleton.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()
        _network_supervisor_singleton.cache_clear()


def test_get_state_returns_default_shape(client: TestClient):
    # The endpoint never calls save_persisted_state (read-only),
    # and load_persisted_state returns None for missing files, so
    # we don't need to redirect the state-file path on Mac.
    response = client.get("/api/network-supervisor/state")
    assert response.status_code == 200
    body = response.json()
    # Default state is SETUP (no persisted state file + default flag).
    assert body["state"] == "SETUP"
    assert body["fallback_mutex_mode"] is False
    assert body["current_sta_freq_mhz"] is None
    assert body["current_ap_channel"] is None
    assert isinstance(body["diagnostics"], list)
    # Boot event should be present.
    assert any("booted" in ev["message"] for ev in body["diagnostics"])


def test_state_endpoint_is_read_only(client: TestClient):
    # P1.2-A is observe-only: no PUT/POST mutations. Spec'd
    # behaviour, regression-locked.
    response = client.put("/api/network-supervisor/state", json={"state": "ONLINE"})
    assert response.status_code in (404, 405)  # not allowed
    response = client.post("/api/network-supervisor/state", json={"state": "ONLINE"})
    assert response.status_code in (404, 405)
