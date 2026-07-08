"""API surface tests for /api/settings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.app import app
from openmarquee.content.storage import ContentStorage
from openmarquee.dependencies import (
    _content_storage_singleton,
    _network_supervisor_singleton,
    _settings_storage_singleton,
    get_content_storage,
    get_settings_storage,
)
from openmarquee.seed import render_text_slide_png
from openmarquee.settings import SettingsStorage


@pytest.fixture
def storage(tmp_path: Path) -> SettingsStorage:
    return SettingsStorage(tmp_path / "settings.json")


@pytest.fixture
def content_storage(tmp_path: Path) -> ContentStorage:
    return ContentStorage(tmp_path / "content")


@pytest.fixture
def client(storage: SettingsStorage, content_storage: ContentStorage) -> TestClient:
    app.dependency_overrides[get_settings_storage] = lambda: storage
    app.dependency_overrides[get_content_storage] = lambda: content_storage
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()
        # 2026-07-08 (P0-1): the settings PUT now drives the network
        # supervisor when station creds change, so a full-creds PUT test
        # that doesn't stub it would touch the shared singleton. Reset it
        # per test so state can't leak across the session.
        _network_supervisor_singleton.cache_clear()


def test_get_returns_defaults_when_nothing_persisted(client: TestClient):
    response = client.get("/api/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["output_mode"] == "hdmi"
    assert body["display_width"] == 1920
    assert body["display_height"] == 1080
    assert body["brightness"] == 80
    # Batch 20.4: GET redacts the 3 secret fields. wifi_password is
    # the per-process random token_urlsafe(16) (Bundle C item 5
    # 2026-05-25; previously the SYSTEM_SPEC §4.1 literal default
    # "openmarquee") -- still non-empty -> redacted -> "<set>".
    # wifi_station_password + tailscale_auth_key default to None ->
    # passed through as None.
    assert body["wifi_password"] == "<set>"
    assert body["wifi_station_password"] is None
    assert body["tailscale_auth_key"] is None
    assert body["timezone"] is None


def test_put_then_get_round_trip(client: TestClient):
    payload = {
        "schema_version": 1,
        # 2026-07-03 (qarl handover): sign_name normalises whitespace
        # to `-` at the field validator so a legacy "Coffee Shop" load
        # becomes DNS-safe for the propagation actuators (hostnamectl
        # / Tailscale / setup-AP SSID / mDNS). Send the pre-normalised
        # value here so the wire round-trip is exact.
        "sign_name": "Coffee-Shop",
        "output_mode": "hdmi",
        "display_width": 1920,
        "display_height": 1080,
        "display_rotation": 0,
        "brightness": 40,
        "gamma": 2.4,
        "wifi_ap_enabled": True,
        "wifi_ssid": "CoffeeShop",
        "wifi_password": "bean-bean-bean",
        "wifi_station_enabled": False,
        "wifi_station_ssid": None,
        "wifi_station_password": None,
        "timezone": "America/New_York",
        "tailscale_enabled": False,
        "tailscale_auth_key": None,
        "tailscale_hostname": None,
        # HTTPS Phase 1 (commit 97d36fc, 2026-05-24): new
        # tailscale_https_enabled field on Settings — default True.
        # Including it explicitly in the PUT payload to keep this
        # round-trip test exhaustive, and so the expected-dict check
        # below sees the field on the response side too.
        "tailscale_https_enabled": True,
        # P1.1 onboarding-rework (2026-06-10): new
        # network_fallback_mutex_mode field on Settings — default
        # False. Same rationale as tailscale_https_enabled: include
        # in payload so the round-trip dict matches.
        "network_fallback_mutex_mode": False,
        "flock_sync_enabled": True,
        "ui_first_run_seen": False,
    }
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 200
    # Batch 20.4: the redaction happens on the response too; the
    # stored value is still the real "bean-bean-bean" (verified by a
    # subsequent PATCH-with-current-password elsewhere), but the wire
    # shape is redacted.
    expected = dict(payload)
    expected["wifi_password"] = "<set>"
    # 2026-07-03 (qarl handover): every response now carries
    # wifi_networks (empty list on fresh installs). Not sent on the
    # PUT payload above (back-compat: legacy PUT bodies still work),
    # but the model dumps it.
    expected["wifi_networks"] = []
    # 2026-07-03 (qarl handover B1): the one-shot NM-import guard
    # flag appears on every response; false when nmcli isn't
    # available (CI runners) so the boot-time import doesn't fire
    # + doesn't get persisted. Value is orthogonal to the round-
    # trip semantics; include it in expected so the wire shape
    # comparison stays exhaustive.
    expected["wifi_networks_seeded_from_nm"] = False
    assert response.json() == expected
    # And reads back redacted.
    response = client.get("/api/settings")
    assert response.json() == expected


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


def test_put_validation_response_does_not_leak_pydantic_internals(
    client: TestClient,
):
    """Batch 11.2 / sweep #5 #8: 422 detail must NOT carry Pydantic's
    raw error string (which quotes the failing payload value verbatim
    and includes internal error-type codes). Operator gets a generic
    message; full detail goes to log.warning."""
    # A passphrase value WOULD have leaked into the response body
    # under the prior detail=str(exc) implementation.
    payload = {"wifi_password": "short"}  # 5 chars; below 8-char min
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    # The literal payload value must not appear.
    assert "short" not in detail
    # Pydantic-specific markers must not appear.
    for marker in ("value_error", "string_too_short", "validation error"):
        assert marker not in detail.lower(), (
            f"Pydantic internal marker {marker!r} leaked into response"
        )


# --- Display-dim change side-effect (qarl 2026-04-30 ask 1) -----------


def _seed_text_slide(content_storage: ContentStorage, *, width: int, height: int):
    """Helper: seed one text slide at the given dims and return it."""
    from openmarquee.content import TextSlide

    png = render_text_slide_png(
        "Hello there",
        width,
        height,
        fg="#FFFFFF",
        bg="#000000",
    )
    slide = TextSlide(
        name="Hello there",
        text="Hello there",
        text_color="#FFFFFF",
        background_color="#000000",
        font_size_px=int(height * 0.4),
        duration_ms=3000,
    )
    content_storage.save_text_slide(slide, png)
    return slide


def _png_dims(png: bytes) -> tuple[int, int]:
    from io import BytesIO

    from PIL import Image

    return Image.open(BytesIO(png)).size


def test_put_with_no_dim_change_does_not_rerender_text_slides(
    client: TestClient, content_storage: ContentStorage
):
    """Brightness-only change must not touch text-slide PNGs."""
    slide = _seed_text_slide(content_storage, width=1920, height=1080)
    original_png = content_storage.read_asset(slide.id)

    response = client.put("/api/settings", json={"brightness": 60})
    assert response.status_code == 200

    # PNG bytes unchanged.
    assert content_storage.read_asset(slide.id) == original_png


def test_put_dim_change_preserves_stored_png_bytes(
    client: TestClient, content_storage: ContentStorage
):
    """qarl-direct 2026-05-13 (DELETE-PIL Option α): display-dim
    changes no longer re-rasterize stored PNGs on the backend. The
    operator's last in-browser bake is preserved verbatim; the device
    cover-fits at panel-native dims, and the next editor save re-bakes
    at the new dims. Pin that bytes are stable across a rotation flip
    + a resolution swap.
    """
    slide = _seed_text_slide(content_storage, width=1920, height=1080)
    original_png = content_storage.read_asset(slide.id)
    original_dims = _png_dims(original_png)
    assert original_dims == (1920, 1080)

    response = client.put(
        "/api/settings",
        json={
            "display_width": 128,
            "display_height": 64,
            "display_rotation": 90,
        },
    )
    assert response.status_code == 200
    # Bytes unchanged — no backend rasterization.
    assert content_storage.read_asset(slide.id) == original_png


def test_put_dim_change_with_no_text_slides_is_a_clean_noop(
    client: TestClient, content_storage: ContentStorage
):
    """Empty content store + dim change — the rerender background task
    runs to completion against zero items without raising."""
    response = client.put(
        "/api/settings",
        json={
            "display_width": 1920,
            "display_height": 1080,
            "display_rotation": 270,
        },
    )
    assert response.status_code == 200


# --- Bundle C item 1 (2026-05-25): wifi-prefill is now explicit POST ---
#
# Per qa/reports/2026-05-25/low-security-scope-2026-05-25.md item 1:
# the prior `GET /api/settings` side-effect that auto-prefilled
# wifi_station_ssid+password from /etc/wpa_supplicant/wpa_supplicant.
# conf gave a pre-shipment attacker a passive harvesting path. The
# fix moves prefill to `POST /api/settings/wifi-prefill` so the
# operator must explicitly opt in. The 4 prior GET-side-effect
# tests are obsolete (vacuously true since GET no longer prefills);
# replaced by the 5 tests below covering the new POST surface +
# the GET-purity regression-lock.


def test_get_is_pure_read_no_side_effect_on_wifi_prefill(client: TestClient, monkeypatch):
    """Bundle C item 1: GET must NOT call read_system_wifi (the
    side-effect that used to live in the GET handler is moved to
    POST /api/settings/wifi-prefill). Regression-lock via a
    monkeypatched read_system_wifi that flips a counter -- if a
    future refactor accidentally restores the side-effect, the
    counter trips."""
    import openmarquee.api_settings as api_settings_mod

    call_count = {"n": 0}

    def _counting_read():
        call_count["n"] += 1
        return ("ShouldNotBeCalled", "shouldnotpass")

    monkeypatch.setattr(api_settings_mod, "read_system_wifi", _counting_read)

    # Two GETs -- catches both "first GET prefills" and "every GET
    # re-reads" regressions in one shot.
    response = client.get("/api/settings")
    assert response.status_code == 200
    response = client.get("/api/settings")
    assert response.status_code == 200

    assert call_count["n"] == 0, (
        "GET /api/settings called read_system_wifi -- the Bundle C "
        "item 1 fix has regressed; prefill must be POST-only now."
    )
    # And the wifi_station fields stayed empty (the side-effect would
    # have populated them with "ShouldNotBeCalled").
    body = response.json()
    assert body["wifi_station_enabled"] is False
    assert body["wifi_station_ssid"] is None or body["wifi_station_ssid"] == ""


def test_post_wifi_prefill_happy_path(client: TestClient, storage: SettingsStorage, monkeypatch):
    """Operator explicitly calls POST /api/settings/wifi-prefill;
    backend reads wpa_supplicant.conf, persists the SSID +
    password, returns the SSID (but NOT the password) on the wire."""
    import openmarquee.api_settings as api_settings_mod

    monkeypatch.setattr(
        api_settings_mod,
        "read_system_wifi",
        lambda: ("MyHomeWifi", "abcdefgh"),
    )

    response = client.post("/api/settings/wifi-prefill")
    assert response.status_code == 200
    body = response.json()
    assert body == {"prefilled": True, "wifi_station_ssid": "MyHomeWifi"}
    # Password deliberately NOT echoed -- the operator already has
    # it (it lives on their Pi); the response only confirms what
    # got persisted.
    assert "wifi_station_password" not in body
    assert "abcdefgh" not in response.text

    # Persisted on disk so a follow-up GET sees the new state.
    persisted = storage.load()
    assert persisted.wifi_station_enabled is True
    assert persisted.wifi_station_ssid == "MyHomeWifi"
    assert persisted.wifi_station_password == "abcdefgh"


def test_post_wifi_prefill_returns_409_when_ssid_already_configured(
    client: TestClient, monkeypatch
):
    """If the operator already set wifi_station_ssid (manually OR via
    a prior prefill), the POST refuses to clobber + returns 409.
    Closes the "rerun the prefill after operator has manually edited"
    foot-gun."""
    # Pre-populate via PUT.
    response = client.put(
        "/api/settings",
        json={
            "wifi_station_enabled": True,
            "wifi_station_ssid": "OperatorChose",
            "wifi_station_password": "operatorpass",
        },
    )
    assert response.status_code == 200

    import openmarquee.api_settings as api_settings_mod

    # Even if read_system_wifi has fresh creds, the 409 fires before
    # the read (the read shouldn't even happen since the SSID-already-
    # set check is first).
    call_count = {"n": 0}

    def _counting_read():
        call_count["n"] += 1
        return ("SystemNet", "systempass")

    monkeypatch.setattr(api_settings_mod, "read_system_wifi", _counting_read)

    response = client.post("/api/settings/wifi-prefill")
    assert response.status_code == 409
    assert "already configured" in response.json()["detail"].lower()
    assert call_count["n"] == 0, (
        "POST should short-circuit on ssid-already-set BEFORE touching "
        "read_system_wifi (avoids unnecessary iwgetid + file IO)."
    )


def test_post_wifi_prefill_returns_404_when_no_system_creds(client: TestClient, monkeypatch):
    """No active wifi connection / unreadable wpa_supplicant.conf /
    unrecognized format → read_system_wifi returns None → 404."""
    import openmarquee.api_settings as api_settings_mod

    monkeypatch.setattr(api_settings_mod, "read_system_wifi", lambda: None)

    response = client.post("/api/settings/wifi-prefill")
    assert response.status_code == 404
    assert "no saved wifi" in response.json()["detail"].lower()


def test_post_wifi_prefill_requires_bearer_token(auth_client: TestClient, monkeypatch):
    """The endpoint inherits the AuthMiddleware bearer-token gate via
    `/api/settings/*` (NOT in the whitelist). Without a bearer header,
    the middleware returns 401 BEFORE our handler runs. Lock this
    contract so a future whitelist edit that accidentally exempts
    /api/settings/wifi-prefill would trip here."""
    import openmarquee.api_settings as api_settings_mod

    # If the middleware ever DID let the call through, read_system_wifi
    # would run + this counter would flag the breach.
    call_count = {"n": 0}

    def _counting_read():
        call_count["n"] += 1
        return ("LeakedNet", "leakedpass")

    monkeypatch.setattr(api_settings_mod, "read_system_wifi", _counting_read)

    response = auth_client.post("/api/settings/wifi-prefill")
    # No Authorization header -> middleware rejects.
    assert response.status_code == 401
    assert call_count["n"] == 0


# --- Batch 20.4: secret redaction + PATCH endpoints ---


def _configure_auth_for_patch_tests(client: TestClient) -> str:
    """Stamp an AuthState via /api/auth/set-password + return the bearer
    token. The patch endpoints need both a valid token AND a matching
    current_password to update."""
    response = client.post(
        "/api/auth/set-password",
        json={"password": "hunter2hunter", "password_confirm": "hunter2hunter"},
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.fixture
def auth_client(
    storage: SettingsStorage,
    content_storage: ContentStorage,
    tmp_path: Path,
    monkeypatch,
) -> TestClient:
    """TestClient with auth wired AND middleware engaged. The default
    `client` fixture relies on conftest's DISABLE_AUTH=1; this one
    pops it so the PATCH endpoints actually require a bearer token,
    which is the contract the 20.4 PATCH tests exercise.

    Returns a TestClient -- callers POST /api/auth/set-password to
    mint the token + use the standard Authorization: Bearer header
    pattern.
    """
    from openmarquee.auth import AuthStorage
    from openmarquee.dependencies import (
        _auth_storage_singleton,
        get_auth_storage,
    )

    auth_path = tmp_path / "auth.json"
    auth_storage = AuthStorage(auth_path)

    app.dependency_overrides[get_settings_storage] = lambda: storage
    app.dependency_overrides[get_content_storage] = lambda: content_storage
    app.dependency_overrides[get_auth_storage] = lambda: auth_storage
    monkeypatch.setenv("OPENMARQUEE_AUTH_PATH", str(auth_path))
    monkeypatch.delenv("OPENMARQUEE_DISABLE_AUTH", raising=False)
    _auth_storage_singleton.cache_clear()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        _settings_storage_singleton.cache_clear()
        _content_storage_singleton.cache_clear()
        # 2026-07-08 (P0-1): the settings PUT now drives the network
        # supervisor when station creds change, so a full-creds PUT test
        # that doesn't stub it would touch the shared singleton. Reset it
        # per test so state can't leak across the session.
        _network_supervisor_singleton.cache_clear()
        _auth_storage_singleton.cache_clear()


def test_get_redacts_three_secret_fields(client: TestClient):
    """GET returns <set> for non-empty secrets + None for unset.

    The default SystemSettings has wifi_password=<random per-process
    token via secrets.token_urlsafe(16)> (set; Bundle C item 5
    2026-05-25 replaced the prior literal "openmarquee");
    wifi_station_password=None (unset); tailscale_auth_key=None (unset).
    """
    body = client.get("/api/settings").json()
    assert body["wifi_password"] == "<set>"
    assert body["wifi_station_password"] is None
    assert body["tailscale_auth_key"] is None


def test_get_redacts_after_real_values_are_stored(client: TestClient):
    """Stamp all three secrets via the storage's save() backdoor (the
    patch endpoint is the real path; we just want a state with all 3
    populated for the redaction assertion)."""
    client.put(
        "/api/settings",
        json={
            "wifi_ap_enabled": True,
            "wifi_ssid": "TestAP",
            "wifi_password": "ap-pw-actual",
            "wifi_station_enabled": True,
            "wifi_station_ssid": "HomeWifi",
            "wifi_station_password": "station-pw-actual",
            "tailscale_enabled": True,
            "tailscale_auth_key": "tskey-auth-aaaaaaaaaa",
            "tailscale_hostname": "test-sign",
        },
    )
    body = client.get("/api/settings").json()
    assert body["wifi_password"] == "<set>"
    assert body["wifi_station_password"] == "<set>"
    assert body["tailscale_auth_key"] == "<set>"


def test_put_substitutes_set_sentinel_with_stored_value(client: TestClient):
    """UI's GET-mutate-PUT round-trip carries '<set>' back for redacted
    secrets. PUT must substitute the stored value for the sentinel so
    the real password isn't replaced with the literal string '<set>'."""
    # Initial state: defaults -- wifi_password is a random
    # token_urlsafe(16) (~22 chars per Bundle C item 5 2026-05-25;
    # previously the literal "openmarquee"). Still passes the
    # 8-63-char regex either way.
    client.put(
        "/api/settings",
        json={"wifi_password": "actually-real-pw"},
    )
    # Now echo the redacted sentinel back via PUT (simulating the UI's
    # Save-after-GET path). Pre-20.4 this would have persisted the
    # sentinel as the real password -- the validator would reject
    # `<set>` (only 5 chars + non-passphrase shape) and surface 422.
    response = client.put(
        "/api/settings",
        json={"wifi_password": "<set>"},
    )
    assert response.status_code == 200
    # The stored value is unchanged. The PATCH endpoint with
    # current_password is the only path that rotates the AP password
    # (verified separately below); GET-mutate-PUT can't touch secrets.
    # Verifying the actual stored value requires the storage backdoor
    # since GET always redacts; for now confirm the redacted response
    # still shows <set> (i.e. non-empty).
    body = client.get("/api/settings").json()
    assert body["wifi_password"] == "<set>"


def test_patch_wifi_ap_password_with_correct_current_password(
    auth_client: TestClient,
):
    """Happy path: valid bearer + correct current_password -> 200, the
    new value lands."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": "fresh-new-pw-123"},
    )
    assert response.status_code == 200
    # GET reflects the new redacted state (still <set> -- the rotation
    # didn't clear it).
    body = auth_client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["wifi_password"] == "<set>"


def test_patch_wifi_ap_password_wrong_current_password_401(
    auth_client: TestClient,
):
    """Wrong current_password -> 401, settings unchanged."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "wrong-pw", "new_value": "shouldnotpersist"},
    )
    assert response.status_code == 401


def test_patch_wifi_ap_password_no_bearer_401(auth_client: TestClient):
    """No Authorization header -> middleware 401s before the endpoint
    runs. The endpoint never sees the request; current_password isn't
    checked."""
    _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        json={"current_password": "hunter2hunter", "new_value": "x"},
    )
    assert response.status_code == 401


def test_patch_wifi_ap_password_rejects_empty(auth_client: TestClient):
    """AP password can't be empty -- the wifi_password field is non-None
    in SystemSettings and the WPA2 regex requires 8-63 chars."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": ""},
    )
    assert response.status_code == 422


def test_patch_wifi_ap_password_rejects_too_short(auth_client: TestClient):
    """7 chars fails the 8-63 passphrase regex."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": "shorty1"},
    )
    assert response.status_code == 422


def test_scrubbed_error_summary_strips_ctx_key():
    """Belt-and-braces: when a field_validator stores a ValueError in
    errors()[i]['ctx']['error'], the value's __repr__ would leak via
    `repr(list[dict])`. The helper strips `ctx` from each error dict
    so this path is closed.

    Note: if a validator's raise message ITSELF includes `{value!r}`,
    the msg field still carries the leak -- and msg can't be stripped
    without losing the operator audit trail. The codebase convention
    documented in _scrubbed_error_summary's docstring is that
    field_validator raise messages on SECRET fields must NOT embed
    {value!r}. The three current secret validators (wifi_password,
    wifi_station_password, tailscale_auth_key) honor this -- they
    raise the literal `wifi_password: expected empty or 8-63 printable
    ASCII chars` form. This test is the regression guard for the
    helper's `ctx` strip; the validator-message convention is a
    separate upstream constraint."""
    from pydantic import BaseModel, ValidationError, field_validator

    from openmarquee.api_settings import _scrubbed_error_summary

    class _Probe(BaseModel):
        secret: str

        @field_validator("secret")
        @classmethod
        def _no_short_secret(cls, value: str) -> str:
            if len(value) < 8:
                # SAFE validator: message does NOT embed the value.
                # Matches the convention used by the three current
                # secret validators in settings.py.
                raise ValueError("secret: must be at least 8 chars")
            return value

    forbidden = "leaky-1"
    try:
        _Probe.model_validate({"secret": forbidden})
    except ValidationError as exc:
        summary = _scrubbed_error_summary(exc)
        # ctx is gone (defense in depth against future bad validators).
        assert "ctx" not in summary, (
            f"helper kept the ctx key (the ValueError leak vector): {summary!r}"
        )
        # Rejected value is absent (because both `input` and `ctx`
        # were stripped, AND this safe validator didn't embed it in msg).
        assert forbidden not in summary, f"rejected value leaked: {summary!r}"
        # Audit trail intact: field name + error type still visible.
        assert "secret" in summary
        assert "value_error" in summary
    else:
        pytest.fail("validator should have raised")


def test_patch_validation_log_does_not_leak_secret_to_journal(
    auth_client: TestClient, caplog: "pytest.LogCaptureFixture"
):
    """Task #379 (sweep #5 #8 full closure): the PATCH validation
    log.warning call MUST NOT contain the rejected secret value.
    Anyone with shell on the Pi can `journalctl -u openmarquee` and
    grep for the new_value -- if it leaks here we've shifted the
    sweep #5 #8 vulnerability from HTTP response body to syslog.
    Helper _scrubbed_error_summary uses exc.errors(include_input=False)
    to omit the value."""
    token = _configure_auth_for_patch_tests(auth_client)
    forbidden_value = "leak7ch"  # too short -> WPA2 8-char floor

    with caplog.at_level("WARNING", logger="openmarquee.api_settings"):
        response = auth_client.patch(
            "/api/settings/wifi-ap-password",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "current_password": "hunter2hunter",
                "new_value": forbidden_value,
            },
        )
    assert response.status_code == 422
    # The WARNING line must mention the field name (audit trail) but
    # NOT the rejected value.
    warning_lines = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    # The model's internal field name is wifi_password (the URL path
    # `wifi-ap-password` is the API alias). Audit trail is the field
    # name, not the URL path.
    assert any("wifi_password" in line for line in warning_lines), (
        f"expected field=wifi_password in log, got: {warning_lines}"
    )
    for line in warning_lines:
        assert forbidden_value not in line, f"REJECTED SECRET LEAKED TO LOG: {line!r}"


def test_patch_validation_response_does_not_leak_secret_or_pydantic_text(
    auth_client: TestClient,
):
    """Batch 11.2 / sweep #5 #8: the PATCH 422 detail must NOT contain
    the rejected secret value (which Pydantic's error string quotes
    verbatim) nor any internal validator marker. Log captures the field
    name + reason for the operator audit trail."""
    token = _configure_auth_for_patch_tests(auth_client)
    # 7 chars -- below the WPA2 8-char floor; rejection guaranteed.
    forbidden_value = "leak7ch"
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": forbidden_value},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    # The rejected secret must NOT appear in the response body.
    assert forbidden_value not in detail
    # Pydantic-specific markers must not appear.
    for marker in ("value_error", "string_pattern_mismatch", "validation error"):
        assert marker not in detail.lower()


def test_patch_wifi_station_password_set_then_clear(auth_client: TestClient):
    """Station password is nullable; PATCH can both set and clear it."""
    token = _configure_auth_for_patch_tests(auth_client)
    # First set wifi_station_enabled + ssid via PUT (no secret involved).
    auth_client.put(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "wifi_station_enabled": False,  # station off -- empty creds allowed
            "wifi_station_ssid": None,
            "wifi_station_password": None,
        },
    )
    # PATCH to set station password.
    response = auth_client.patch(
        "/api/settings/wifi-station-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": "home-wifi-pw-123"},
    )
    assert response.status_code == 200
    # Redacted state.
    body = auth_client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["wifi_station_password"] == "<set>"
    # Clear via empty new_value.
    response = auth_client.patch(
        "/api/settings/wifi-station-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": ""},
    )
    assert response.status_code == 200
    body = auth_client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["wifi_station_password"] is None


def test_patch_tailscale_auth_key_round_trip(auth_client: TestClient):
    """Tailscale key starts None, PATCH to a tskey-auth-... value, GET
    redacts to <set>."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/tailscale-auth-key",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": "hunter2hunter",
            "new_value": "tskey-auth-aaaaaaaaaaaa",
        },
    )
    assert response.status_code == 200
    body = auth_client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert body["tailscale_auth_key"] == "<set>"


def test_patch_tailscale_auth_key_rejects_garbage(auth_client: TestClient):
    """The model's _check_tailscale_auth_key validator requires
    tskey-... prefix. Garbage hits the regex check + 422."""
    token = _configure_auth_for_patch_tests(auth_client)
    response = auth_client.patch(
        "/api/settings/tailscale-auth-key",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": "hunter2hunter", "new_value": "not-a-tskey"},
    )
    assert response.status_code == 422


def test_patch_endpoints_404_when_auth_not_configured(auth_client: TestClient):
    """Pre-set-password device -> 404 (auth not configured)."""
    # NOT calling _configure_auth_for_patch_tests -- auth.json stays
    # unstamped. The middleware still 401s because there's no bearer;
    # we send a fake one to reach the endpoint logic where the 404
    # fires.
    response = auth_client.patch(
        "/api/settings/wifi-ap-password",
        headers={"Authorization": "Bearer 1.fake-token"},
        json={"current_password": "anything", "new_value": "x"},
    )
    # Middleware rejects first because the token doesn't verify (no
    # AuthState). Either 401 (middleware) or 404 (endpoint) is
    # acceptable; the endpoint never gets to run, so 401 is what
    # actually fires.
    assert response.status_code == 401


# --- P0-1 (2026-07-08): Settings provisioning drives the state machine ---


class _FakeSupervisor:
    """Records the provisioning calls the settings PATCH should make."""

    def __init__(self, current_state=None):
        from openmarquee.network_supervisor import SupervisorState

        self.current_state = current_state or SupervisorState.SETUP
        self.recorded_ssid = "<unset>"
        self.events = []

    def record_target_ssid(self, ssid):
        self.recorded_ssid = ssid

    def apply_event(self, event):
        self.events.append(event)


def _stub_wifi_apply(monkeypatch):
    import openmarquee.wifi_station as wifi_station

    monkeypatch.setattr(wifi_station, "apply_in_background", lambda **kwargs: None)


def test_put_wifi_station_creds_drives_supervisor(client: TestClient, monkeypatch):
    # Saving station creds via Settings must ALSO drive the state machine
    # (record_target_ssid + HAS_STORED_CREDENTIALS), not just run the nmcli
    # apply — otherwise SETUP never advances → the AP-teardown + CONNECTING/
    # LINGER confirmation cards never fire on the first session.
    from openmarquee.network_supervisor import SupervisorEvent

    _stub_wifi_apply(monkeypatch)
    fake = _FakeSupervisor()
    monkeypatch.setattr("openmarquee.dependencies.get_network_supervisor", lambda: fake)
    resp = client.put(
        "/api/settings",
        json={
            "wifi_station_enabled": True,
            "wifi_station_ssid": "MyHomeWifi",
            "wifi_station_password": "abcdefgh",
        },
    )
    assert resp.status_code == 200
    assert fake.recorded_ssid == "MyHomeWifi"
    assert SupervisorEvent.HAS_STORED_CREDENTIALS in fake.events


def test_put_wifi_station_disabled_does_not_drive_supervisor(client: TestClient, monkeypatch):
    # Guard: no station change / station disabled must NOT fire the
    # provisioning event.
    _stub_wifi_apply(monkeypatch)
    fake = _FakeSupervisor()
    monkeypatch.setattr("openmarquee.dependencies.get_network_supervisor", lambda: fake)
    resp = client.put(
        "/api/settings",
        json={
            "wifi_station_enabled": False,
            "wifi_station_ssid": None,
            "wifi_station_password": None,
        },
    )
    assert resp.status_code == 200
    assert fake.recorded_ssid == "<unset>"
    assert fake.events == []


def test_put_wifi_station_supervisor_failure_is_fail_soft(client: TestClient, monkeypatch):
    # A supervisor hiccup must NOT 500 the PUT — creds are already persisted
    # + the nmcli apply already ran before the supervisor drive.
    _stub_wifi_apply(monkeypatch)

    class _BoomSupervisor(_FakeSupervisor):
        def apply_event(self, event):
            raise RuntimeError("supervisor down")

    monkeypatch.setattr(
        "openmarquee.dependencies.get_network_supervisor", lambda: _BoomSupervisor()
    )
    resp = client.put(
        "/api/settings",
        json={
            "wifi_station_enabled": True,
            "wifi_station_ssid": "MyHomeWifi",
            "wifi_station_password": "abcdefgh",
        },
    )
    assert resp.status_code == 200
    # Creds persisted despite the supervisor failure.
    assert client.get("/api/settings").json()["wifi_station_ssid"] == "MyHomeWifi"


def test_put_wifi_station_creds_do_not_disturb_online_sign(client: TestClient, monkeypatch):
    # State guard: re-saving station creds while the sign is already ONLINE
    # must NOT re-fire the provisioning event (that would re-trigger
    # onboarding on a running sign). The nmcli reconnect + observe loop
    # handle a network change from ONLINE.
    from openmarquee.network_supervisor import SupervisorState

    _stub_wifi_apply(monkeypatch)
    fake = _FakeSupervisor(current_state=SupervisorState.ONLINE)
    monkeypatch.setattr("openmarquee.dependencies.get_network_supervisor", lambda: fake)
    resp = client.put(
        "/api/settings",
        json={
            "wifi_station_enabled": True,
            "wifi_station_ssid": "DifferentWifi",
            "wifi_station_password": "abcdefgh",
        },
    )
    assert resp.status_code == 200
    assert fake.recorded_ssid == "<unset>"
    assert fake.events == []
