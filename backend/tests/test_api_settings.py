"""API surface tests for /api/settings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.dependencies import (
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
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()


def test_get_returns_defaults_when_nothing_persisted(client: TestClient):
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["output_mode"] == "hdmi"
    assert body["display_width"] == 128
    assert body["display_height"] == 96
    assert body["brightness"] == 80
    assert body["wifi_password"] == "openmarquee"  # SYSTEM_SPEC §4.1
    assert body["timezone"] is None


def test_put_then_get_round_trip(client: TestClient):
    payload = {
        "schema_version": 1,
        "sign_name": "Coffee Shop",
        "output_mode": "hub75",
        "display_width": 192,
        "display_height": 64,
        "display_rotation": 0,
        "brightness": 40,
        "gamma": 2.4,
        "wifi_ssid": "CoffeeShop",
        "wifi_password": "bean-bean-bean",
        "timezone": "America/New_York",
        "tailscale_enabled": False,
        "tailscale_auth_key": None,
        "tailscale_hostname": None,
    }
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    assert response.json() == payload

    # And reads back verbatim.
    response = client.get("/api/settings")
    assert response.json() == payload


def test_put_rejects_bad_output_mode(client: TestClient):
    payload = {"output_mode": "vga"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_brightness_out_of_range(client: TestClient):
    payload = {"brightness": 150}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_ssid_over_32_bytes(client: TestClient):
    payload = {"wifi_ssid": "x" * 33}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_too_short_wifi_password(client: TestClient):
    payload = {"wifi_password": "short"}  # 5 chars
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_rejects_empty_wifi_password(client: TestClient):
    """Empty passphrase isn't a WPA2 passphrase. UI must send the current
    stored value on Save (GET returns it verbatim) — no "no change" sentinel."""
    payload = {"wifi_password": ""}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422


def test_put_accepts_timezone_and_persists_it(client: TestClient):
    payload = {"timezone": "Europe/Paris"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    response = client.get("/api/settings")
    assert response.json()["timezone"] == "Europe/Paris"


def test_put_rejects_garbage_timezone(client: TestClient):
    payload = {"timezone": "DROP TABLE tz;"}
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422
