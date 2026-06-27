"""P2 (2026-06-27) API: captive-portal onboarding endpoints.

POST /api/onboarding/submit-credentials + GET /api/onboarding/status.
Both endpoints are unauth — verified by issuing requests without a
bearer token.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OPENMARQUEE_DISABLE_AUTOSTART", "1")
os.environ.setdefault("OPENMARQUEE_DISABLE_NETWORK_SUPERVISOR", "1")

from openmarquee.app import app
from openmarquee.dependencies import (
    _network_supervisor_singleton,
    _settings_storage_singleton,
    get_settings_storage,
)
from openmarquee.network_supervisor import SupervisorState
from openmarquee.settings import SettingsStorage


@pytest.fixture
def storage(tmp_path: Path) -> SettingsStorage:
    return SettingsStorage(tmp_path / "settings.json")


@pytest.fixture
def client(storage: SettingsStorage, monkeypatch) -> TestClient:
    """A fresh supervisor singleton + settings storage per test.

    wifi_station.apply_in_background is monkey-patched to a no-op so
    the POST handler doesn't spawn a real nmcli thread that would
    try to exec the nmcli binary during tests.
    """

    def _apply_noop(*args, **kwargs):
        class _FakeThread:
            def start(self):
                pass

        return _FakeThread()

    monkeypatch.setattr(
        "openmarquee.api_onboarding.wifi_station.apply_in_background",
        _apply_noop,
    )
    app.dependency_overrides[get_settings_storage] = lambda: storage
    _network_supervisor_singleton.cache_clear()
    _settings_storage_singleton.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()
        _network_supervisor_singleton.cache_clear()


# ============================================================
# POST /api/onboarding/submit-credentials
# ============================================================


def test_submit_credentials_happy_path(client: TestClient, storage: SettingsStorage):
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "open-sesame"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "status": "submitted",
        "poll_url": "/api/onboarding/status",
    }


def test_submit_credentials_persists_to_settings(client: TestClient, storage: SettingsStorage):
    client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "open-sesame"},
    )
    settings = storage.load()
    assert settings.wifi_station_enabled is True
    assert settings.wifi_station_ssid == "home-wifi"
    assert settings.wifi_station_password == "open-sesame"


def test_submit_credentials_drives_supervisor_into_connecting(
    client: TestClient, storage: SettingsStorage
):
    # Verify by fetching the status endpoint right after POST.
    client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "open-sesame"},
    )
    status = client.get("/api/onboarding/status").json()
    assert status["state"] == SupervisorState.CONNECTING.value


def test_submit_credentials_rejects_short_password(client: TestClient):
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "short"},
    )
    assert response.status_code == 422


def test_submit_credentials_rejects_empty_ssid(client: TestClient):
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "", "password": "open-sesame"},
    )
    assert response.status_code == 422


def test_submit_credentials_rejects_oversize_ssid(client: TestClient):
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "x" * 33, "password": "open-sesame"},
    )
    assert response.status_code == 422


def test_submit_credentials_is_unauth(client: TestClient):
    """The portal user has no bearer token — explicitly verify the
    handler answers without one (the allowlist carve-out is active)."""
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "open-sesame"},
        # No Authorization header.
    )
    assert response.status_code != 401
    assert response.status_code != 403


# ============================================================
# GET /api/onboarding/status
# ============================================================


def test_status_returns_initial_setup_state(client: TestClient):
    response = client.get("/api/onboarding/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == SupervisorState.SETUP.value
    # wifi_station hasn't been driven, so applier reports idle.
    assert body["wifi_station_state"] == "idle"


def test_status_does_not_leak_wifi_station_detail(client: TestClient):
    """Sacred-review NIT (PR2): the wifi_station.detail field carries
    free-form nmcli stderr; on an unauth captive-portal surface we
    do NOT expose it. The portal user sees high-level state
    transitions; authenticated operators see detail via the auth-
    gated /api/system/wifi-station-state."""
    body = client.get("/api/onboarding/status").json()
    assert "wifi_station_detail" not in body


# ============================================================
# QA cross-lane review (PR2 BLOCKER B1): the unauth POST surface
# MUST NOT echo the submitted password in 422 response bodies.
# ============================================================


def test_submit_credentials_422_does_not_echo_password(client: TestClient):
    """QA cross-lane review (PR2 BLOCKER B1): the submitted password
    MUST NOT appear anywhere in the 422 response body. We trigger an
    ssid-side validation failure (so the request body still contains
    a unique canary password) and assert the canary is absent from
    the response.
    """
    canary = "leak-canary-zzqqww-abc-7331-unique"
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "x" * 33, "password": canary},  # ssid too long
    )
    assert response.status_code == 422, response.text
    assert canary not in response.text, (
        "422 must not echo the submitted password (got body: " + response.text + ")"
    )


def test_submit_credentials_422_keeps_useful_field_info(client: TestClient):
    """Defensive 422 sanitisation must still tell the portal WHICH
    field tripped + WHAT was wrong — just not echo the value."""
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "", "password": "open-sesame"},
    )
    assert response.status_code == 422
    body = response.json()
    assert "detail" in body
    # The error MUST identify the field by name so the portal can
    # highlight it.
    errs = body["detail"]
    assert any("ssid" in str(err.get("loc", "")) for err in errs)
    assert any(err.get("msg") for err in errs)


# ============================================================
# QA cross-lane review (PR2 NIT N2): state-gate submit-credentials
# so LAN-side unauth attackers cannot overwrite credentials once
# the device is online.
# ============================================================


def test_submit_credentials_rejected_in_online_state(client: TestClient):
    """ONLINE means the AP is torn down — the legitimate portal
    surface no longer exists. Any unauth POST from the home LAN is
    not the portal user; reject it."""
    from openmarquee.dependencies import get_network_supervisor
    from openmarquee.network_supervisor import SupervisorEvent

    sup = get_network_supervisor()
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)  # → CONNECTING
    sup.apply_event(SupervisorEvent.STA_ASSOCIATED)  # → LINGER
    sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)  # → ONLINE
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "leak-rig", "password": "evil-pass-1234"},
    )
    assert response.status_code == 409


def test_submit_credentials_rejected_in_linger_state(client: TestClient):
    """LINGER also implies STA is up + the device is reachable on
    home wifi. Reject unauth submit-credentials here per QA N2."""
    from openmarquee.dependencies import get_network_supervisor
    from openmarquee.network_supervisor import SupervisorEvent

    sup = get_network_supervisor()
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "leak-rig", "password": "evil-pass-1234"},
    )
    assert response.status_code == 409


def test_submit_credentials_accepted_in_degraded_state(client: TestClient):
    """DEGRADED brings the AP back up (recovery path) — the
    legitimate portal user is back. Accept it."""
    from openmarquee.dependencies import get_network_supervisor
    from openmarquee.network_supervisor import SupervisorEvent

    sup = get_network_supervisor()
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    sup.apply_event(SupervisorEvent.STA_ASSOCIATED)
    sup.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
    sup.apply_event(SupervisorEvent.STA_DISCONNECTED)  # → DEGRADED
    response = client.post(
        "/api/onboarding/submit-credentials",
        json={"ssid": "home-wifi", "password": "open-sesame"},
    )
    assert response.status_code == 200


def test_status_is_unauth(client: TestClient):
    response = client.get("/api/onboarding/status")
    assert response.status_code != 401
    assert response.status_code != 403


def test_status_reflects_supervisor_transition(client: TestClient):
    """Drive the supervisor into LINGER via direct event dispatch,
    then verify the status endpoint reflects it."""
    from openmarquee.dependencies import get_network_supervisor
    from openmarquee.network_supervisor import SupervisorEvent

    sup = get_network_supervisor()
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    sup.apply_event(SupervisorEvent.STA_ASSOCIATED)

    body = client.get("/api/onboarding/status").json()
    assert body["state"] == SupervisorState.LINGER.value
