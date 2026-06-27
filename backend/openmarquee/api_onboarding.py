"""P2 (2026-06-27) captive-portal onboarding API.

Two endpoints exposed to the unauthenticated portal user (joined to
the openMarquee-Setup AP):

  POST /api/onboarding/submit-credentials
      Accept {ssid, password} JSON; persist via settings; kick the
      wifi_station.apply_in_background nmcli flow; emit
      HAS_STORED_CREDENTIALS into the supervisor's state machine.
      Returns {"status": "submitted", "poll_url": "/api/onboarding/status"}
      so the portal can spin up its "connecting…" page that polls
      the status endpoint.

  GET /api/onboarding/status
      Return the supervisor's current state + the wifi_station
      applier's nmcli status so the portal can render
      SETUP/CONNECTING/LINGER/ONLINE/DEGRADED transitions live (per
      spec §"Captive portal plumbing": "respond to the form POST
      first with a 'connecting' page that polls a status endpoint,
      then kick off association"). Read-only.

Both endpoints are added to the auth middleware's allowlist:
the captive-portal user has NOT been authenticated by the spec's
design (proximity PIN via QR is the auth model — see spec §"The
display is the onboarding UI"). A future PR may scope these to the
ap0 interface only; for now the allowlist matches the existing
welcome/login carve-outs.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from openmarquee import wifi_station
from openmarquee.dependencies import get_network_supervisor, get_settings_storage
from openmarquee.network_supervisor import NetworkSupervisor, SupervisorEvent
from openmarquee.settings import SettingsStorage

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])

SupervisorDep = Annotated[NetworkSupervisor, Depends(get_network_supervisor)]
StorageDep = Annotated[SettingsStorage, Depends(get_settings_storage)]


class SubmitCredentialsRequest(BaseModel):
    """Portal POST body. Field constraints mirror
    `SystemSettings.wifi_station_ssid` /
    `wifi_station_password` validators so a bad payload fails
    here (HTTP 422) with the same shape rules that the settings
    layer enforces."""

    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=63)


class SubmitCredentialsResponse(BaseModel):
    status: str
    poll_url: str


class OnboardingStatusResponse(BaseModel):
    """Status payload the portal polls every 1s. Shape kept stable
    so the portal JS can read it without bumps.

    Sacred-review NIT (PR2): `wifi_station.current_state().detail`
    is intentionally NOT exposed here. The `detail` field carries
    free-form strings from nmcli stderr; while nmcli should not
    echo the password back, the unauth captive-portal surface is
    not the place to take that risk. The portal UI renders state
    transitions (SETUP / CONNECTING / LINGER / ONLINE / DEGRADED)
    + the high-level wifi_station_state value, which is sufficient
    for the user-facing experience. Authenticated operators see
    the detail via /api/system/wifi-station-state.
    """

    state: str
    wifi_station_state: str
    wifi_station_ssid: str | None = None


@router.post("/submit-credentials", response_model=SubmitCredentialsResponse)
async def submit_credentials(
    body: SubmitCredentialsRequest,
    supervisor: SupervisorDep,
    storage: StorageDep,
) -> SubmitCredentialsResponse:
    """POST-then-associate: persist creds + kick the nmcli apply +
    drive the supervisor's HAS_STORED_CREDENTIALS event so the
    portal's status poll sees SETUP -> CONNECTING immediately.

    The actual STA association runs in a background thread
    (wifi_station.apply_in_background). When wpa_supplicant emits
    CTRL-EVENT-CONNECTED, the supervisor's observe loop translates
    it into STA_ASSOCIATED and the state machine transitions to
    LINGER. Spec §"Captive portal plumbing": "respond to the form
    POST first with a 'connecting' page that polls a status
    endpoint, then kick off association."
    """
    settings = storage.load()
    updated = settings.model_copy(
        update={
            "wifi_station_enabled": True,
            "wifi_station_ssid": body.ssid,
            "wifi_station_password": body.password,
        }
    )
    try:
        # The settings model's own validators run here (ssid /
        # password format checks). Re-raise as HTTP 422 if
        # anything trips.
        updated.model_validate(updated.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    storage.save(updated)

    # Kick the nmcli apply in a background thread (matches the
    # existing api_settings PATCH /api/settings handler's pattern).
    wifi_station.apply_in_background(
        enabled=True,
        ssid=body.ssid,
        password=body.password,
    )

    # Drive the supervisor's state machine so the portal's status
    # poll sees SETUP -> CONNECTING right away (don't wait for
    # wpa_supplicant's CTRL-EVENT-CONNECTED which arrives several
    # seconds later when nmcli completes association).
    supervisor.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)

    return SubmitCredentialsResponse(
        status="submitted",
        poll_url="/api/onboarding/status",
    )


@router.get("/status", response_model=OnboardingStatusResponse)
async def get_onboarding_status(supervisor: SupervisorDep) -> OnboardingStatusResponse:
    """Return supervisor state + wifi_station applier status so the
    portal can render the SETUP -> CONNECTING -> LINGER -> ONLINE
    transition without requiring the user to re-load the page.

    Polled at ~1Hz by the portal during the connecting flow.
    """
    sta = wifi_station.current_state()
    return OnboardingStatusResponse(
        state=supervisor.current_state.value,
        wifi_station_state=sta.state,
        wifi_station_ssid=sta.ssid,
    )
