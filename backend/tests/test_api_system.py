"""API surface tests for /api/system/info (Phase B.1).

Covers the format-mode helper exhaustively (pure function, no I/O),
the uptime formatter's two-unit-truncation contract, and the /info
endpoint behavior on a dev box where /proc/* sources aren't
available — confirms the SELF_PLACEHOLDER-shaped fallbacks fire and
the source field accurately reports "fallback".

The /proc-source happy paths (real model from /proc/device-tree,
real signal from /proc/net/wireless, real uptime from /proc/uptime)
are exercised on actual hardware via QA's flock-health-probe live-
fire script — vitest-style mocking the filesystem here would just
re-test our mocks.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from openmarquee.api_system import (
    _FALLBACK_MODEL,
    _FALLBACK_SIGNAL,
    _FALLBACK_UPTIME,
    _format_mode,
    _format_uptime,
)
from openmarquee.app import app
from openmarquee.dependencies import (
    _settings_storage_singleton,
    get_settings_storage,
)
from openmarquee.settings import SettingsStorage, SystemSettings


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


# --- _format_mode (pure function) ---


def test_format_mode_hdmi_uses_height_only():
    """HDMI is operator-spoken in resolution-class terms (1080p)."""
    assert _format_mode("hdmi", 1920, 1080) == "hdmi-1080"


# --- _format_uptime (pure function) ---


def test_format_uptime_under_minute_seconds_only():
    """Boot-recent: 'Nm 0s' would read silly when N=0."""
    assert _format_uptime(45) == "45s"


def test_format_uptime_minutes_with_seconds_residual():
    assert _format_uptime(305) == "5m 5s"


def test_format_uptime_hours_with_minutes_residual():
    """3h 15m, not 3h 15m 12s — two-unit truncation."""
    assert _format_uptime(3600 * 3 + 60 * 15 + 12) == "3h 15m"


def test_format_uptime_days_with_hours_residual():
    """The example FlockPeer.uptime docstring: '4d 7h'."""
    assert _format_uptime(86400 * 4 + 3600 * 7) == "4d 7h"


def test_format_uptime_zero_is_zero_seconds():
    assert _format_uptime(0) == "0s"


# --- /api/system/info endpoint behavior ---


def test_info_exposes_device_id_when_identity_present(client: TestClient, tmp_path, monkeypatch):
    """qarl 2026-05-12: /api/system/info exposes the MySignXXX
    device_id from /var/openmarquee/identity.json. Point the reader
    at a fixture file to verify the field round-trips through the
    Pydantic wire model."""
    identity_path = tmp_path / "identity.json"
    identity_path.write_text('{"device_id": "MySign7K2"}')
    monkeypatch.setenv("OPENMARQUEE_IDENTITY_PATH", str(identity_path))
    response = client.get("/api/system/info")
    assert response.status_code == 200
    body = response.json()
    assert body["device_id"] == "MySign7K2"


def test_tailscale_up_returns_auth_url_from_stub(client: TestClient, tmp_path, monkeypatch):
    """qarl 2026-05-12 (arc 4): POST /api/system/tailscale/up spawns
    `tailscale up`, parses the auth URL, returns it. Use a bash stub
    so the test doesn't need a real Tailscale install."""
    import stat as _stat

    stub = tmp_path / "tailscale"
    stub.write_text(
        "#!/usr/bin/env bash\necho 'visit https://login.tailscale.com/a/teststub42' >&2\nsleep 30\n"
    )
    stub.chmod(stub.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    response = client.post("/api/system/tailscale/up")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "pending"
    assert body["auth_url"] == "https://login.tailscale.com/a/teststub42"


def test_tailscale_status_authenticated_from_stub(client: TestClient, tmp_path, monkeypatch):
    import stat as _stat

    stub = tmp_path / "tailscale"
    blob = (
        '{"BackendState":"Running","Self":{"HostName":"mysign7k2","TailscaleIPs":["100.64.1.2"]}}'
    )
    stub.write_text(f"#!/usr/bin/env bash\ncat <<'EOF'\n{blob}\nEOF\n")
    stub.chmod(stub.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    response = client.get("/api/system/tailscale/status")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "authenticated"
    assert body["hostname"] == "mysign7k2"
    assert body["ipv4"] == "100.64.1.2"


def test_info_device_id_null_when_identity_absent(client: TestClient, tmp_path, monkeypatch):
    """Off-device dev path: identity.json doesn't exist; the field
    is null. UI falls back to OS hostname there."""
    monkeypatch.setenv("OPENMARQUEE_IDENTITY_PATH", str(tmp_path / "does-not-exist.json"))
    response = client.get("/api/system/info")
    assert response.status_code == 200
    assert response.json()["device_id"] is None


def test_info_returns_fallback_payload_on_dev_box(client: TestClient):
    """On a dev laptop without /proc/* sources, /info returns the
    SELF_PLACEHOLDER-matching values + source='fallback'. This is
    the path the demo + every developer hits, so it has to be
    correct."""
    response = client.get("/api/system/info")
    assert response.status_code == 200
    body = response.json()

    # /proc readers all returned None on a Mac → all three fallback.
    # On a Linux CI runner with /proc/uptime available, we'd see
    # "mixed" instead with a real uptime. Accept both shapes:
    assert body["source"] in ("fallback", "mixed")

    # The mode field is always populated from settings (no /proc
    # involvement). Default settings: hdmi 1920×1080 → 'hdmi-1080'.
    assert body["mode"] == "hdmi-1080"

    # When source is 'fallback', the other three are the SELF_
    # PLACEHOLDER constants.
    if body["source"] == "fallback":
        assert body["model"] == _FALLBACK_MODEL
        assert body["signal"] == _FALLBACK_SIGNAL
        assert body["uptime"] == _FALLBACK_UPTIME


def test_info_mode_reflects_settings_changes(client: TestClient):
    """Save settings; /info's mode follows display dims."""
    payload = SystemSettings(
        output_mode="hdmi",
        display_width=1280,
        display_height=720,
    ).model_dump(mode="json")
    put = client.put("/api/settings", json=payload)
    assert put.status_code == 200

    info = client.get("/api/system/info").json()
    assert info["mode"] == "hdmi-720"


def test_info_exposes_rotation_applied_display_dims(client: TestClient):
    """B1 follow-up (qarl 2026-04-29): flock peers query each other's
    /api/system/info for display_rotation when rendering thumbs. The
    response carries the rotation-applied effective width/height plus
    the raw rotation int — landscape 1920×1080 rotated 90° → 1080×1920,
    rotation=90."""
    payload = SystemSettings(
        output_mode="hdmi",
        display_width=1920,
        display_height=1080,
        display_rotation=90,
    ).model_dump(mode="json")
    client.put("/api/settings", json=payload)

    info = client.get("/api/system/info").json()
    assert info["display_width"] == 1080
    assert info["display_height"] == 1920
    assert info["display_rotation"] == 90


def test_info_no_cors_header_for_same_origin(client: TestClient):
    """Batch 11.3 / sweep #5 #4: same-origin request (no Origin header)
    gets NO access-control-allow-origin header -- browser doesn't need
    one for same-origin reads. The wildcard ACAO that used to be set
    here was the sweep #5 #4 vulnerability."""
    response = client.get("/api/system/info")
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_info_passes_unrotated_dims_through_when_rotation_is_zero(client: TestClient):
    payload = SystemSettings(
        output_mode="hdmi",
        display_width=1280,
        display_height=720,
        display_rotation=0,
    ).model_dump(mode="json")
    client.put("/api/settings", json=payload)

    info = client.get("/api/system/info").json()
    assert info["display_width"] == 1280
    assert info["display_height"] == 720
    assert info["display_rotation"] == 0


def test_info_signal_in_range_when_present(client: TestClient):
    """Whatever /proc/net/wireless reports (or the fallback), signal
    must be in [0, 100]. The Pydantic model doesn't enforce a range
    on the response side; this catches a parser regression."""
    info = client.get("/api/system/info").json()
    assert 0 <= info["signal"] <= 100


# --- Batch 11.3 / sweep #5 #4: CORS allowlist tests ---


def test_info_no_cors_for_unknown_origin(client: TestClient):
    """Cross-origin GET from a non-allowlisted origin gets no ACAO."""
    response = client.get(
        "/api/system/info",
        headers={"Origin": "https://attacker.example.com"},
    )
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_info_cors_for_localhost_origin(client: TestClient):
    response = client.get(
        "/api/system/info",
        headers={"Origin": "http://localhost:9000"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ("http://localhost:9000")
    assert response.headers.get("vary") == "Origin"


def test_info_cors_for_captive_portal_ap_origin(client: TestClient):
    """192.168.4.1 is the captive-portal AP gateway (SYSTEM_SPEC §4.1) --
    the operator's phone hits it directly during setup. Builtin allow."""
    response = client.get(
        "/api/system/info",
        headers={"Origin": "http://192.168.4.1"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == ("http://192.168.4.1")


# ============================================================
# PR3 (2026-06-27) — /api/system/render-system-card-preview
# ============================================================


class _RecordingRenderer:
    """Test double for the Renderer protocol's system-card methods.
    Just records the calls so tests can assert on them without
    booting the real IPC subprocess."""

    def __init__(self):
        self.render_calls: list[dict] = []
        self.clear_calls: int = 0
        # A minimal subset of the wider Renderer protocol so
        # get_renderer's callers don't blow up on attribute access.
        self.width = 1920
        self.height = 1080

    def render_system_card(self, params: dict) -> None:
        self.render_calls.append(dict(params))

    def clear_system_card(self) -> None:
        self.clear_calls += 1


@pytest.fixture
def recording_renderer(monkeypatch):
    renderer = _RecordingRenderer()
    # api_system does `from openmarquee.dependencies import get_renderer`
    # inside the handler, so we patch the dependencies-module symbol.
    from openmarquee import dependencies as deps

    monkeypatch.setattr(deps, "get_renderer", lambda: renderer)
    return renderer


class TestRenderSystemCardPreview:
    def test_happy_path_setup_card(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={
                "kind": "SETUP",
                "ssid": "openMarquee-Setup",
                "pin": "4827",
                "qr_payload": "WIFI:T:WPA;S:openMarquee-Setup;P:4827;;",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "rendered"}
        assert len(recording_renderer.render_calls) == 1
        assert recording_renderer.render_calls[0]["kind"] == "SETUP"
        assert recording_renderer.render_calls[0]["ssid"] == "openMarquee-Setup"

    def test_omits_none_fields(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "CONNECTING", "target_ssid": "HomeWiFi"},
        )
        assert response.status_code == 200
        params = recording_renderer.render_calls[0]
        assert params == {"kind": "CONNECTING", "target_ssid": "HomeWiFi"}
        # None fields must NOT appear as explicit null in the params
        assert "ssid" not in params
        assert "pin" not in params

    def test_rejects_unknown_kind(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "NOT_A_KIND"},
        )
        assert response.status_code == 422
        assert recording_renderer.render_calls == []

    def test_rejects_unknown_variant(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "DEGRADED", "variant": "not_a_variant"},
        )
        assert response.status_code == 422

    def test_rejects_oversize_qr_payload(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "SETUP", "qr_payload": "A" * 500},
        )
        assert response.status_code == 422

    def test_rejects_oversize_ssid(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "SETUP", "ssid": "S" * 200},
        )
        assert response.status_code == 422

    def test_rejects_negative_ttl(self, client: TestClient, recording_renderer):
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "BOOT", "ttl_ms": -1},
        )
        assert response.status_code == 422

    def test_maps_kind_to_uppercase(self, client: TestClient, recording_renderer):
        """Case tolerance: the Rust side uses UPPERCASE serde
        rename; the endpoint accepts either case for convenience
        but normalises."""
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "setup"},
        )
        assert response.status_code == 200
        assert recording_renderer.render_calls[0]["kind"] == "SETUP"

    def test_502_on_renderer_failure(self, client: TestClient, monkeypatch):
        class BustedRenderer:
            width = 1920
            height = 1080

            def render_system_card(self, params: dict) -> None:
                raise RuntimeError("subprocess dead")

        from openmarquee import dependencies as deps

        monkeypatch.setattr(deps, "get_renderer", lambda: BustedRenderer())
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "SETUP"},
        )
        assert response.status_code == 502
        assert "subprocess dead" in response.text


class TestClearSystemCardPreview:
    def test_happy_path(self, client: TestClient, recording_renderer):
        response = client.post("/api/system/clear-system-card-preview")
        assert response.status_code == 200
        assert response.json() == {"status": "cleared"}
        assert recording_renderer.clear_calls == 1

    def test_502_on_renderer_failure(self, client: TestClient, monkeypatch):
        class BustedRenderer:
            width = 1920
            height = 1080

            def clear_system_card(self) -> None:
                raise RuntimeError("subprocess dead")

        from openmarquee import dependencies as deps

        monkeypatch.setattr(deps, "get_renderer", lambda: BustedRenderer())
        response = client.post("/api/system/clear-system-card-preview")
        assert response.status_code == 502


# ============================================================
# PR3 fix-pass F3 (2026-07-01) — the preview endpoint must
# return 502 when the render actually landed on the Mock (i.e.
# the AutoFallback had already been demoted). Prior tests
# monkey-patched a bare-raising renderer + bypassed
# AutoFallbackRenderer entirely, so they green-lit a path
# production never takes. This suite drives through the REAL
# AutoFallback with a subprocess-dead primary → the wrapper
# falls back on the render_frame path, and then card ops paint
# on the mock while `is_in_fallback` reports True. The preview
# endpoint sees the fallback signal and returns 502.
# ============================================================


class TestPreviewEndpointReportsFallbackTruthfully:
    def _make_fallen_wrapper(self):
        """AutoFallbackRenderer whose primary raises on
        render_frame → the wrapper is-in-fallback after one video
        frame. Card ops then forward to the mock without raising."""
        import tempfile
        from pathlib import Path

        from openmarquee.dependencies import AutoFallbackRenderer
        from openmarquee.rendering.mock import MockRenderer
        from openmarquee.rendering.rust_renderer import (
            RustRendererSubprocessError,
        )

        class _PrimaryDeadOnVideo:
            width = 1920
            height = 1080

            def render_frame(self, frame, **kwargs):
                raise RustRendererSubprocessError("simulated on render_frame")

            def end_external_frames(self):
                pass

            def render_system_card(self, params):
                # Never reached — the wrapper's fallback catches
                # this via the video path before any card call.
                raise AssertionError("should not be reached after fallback")

            def clear_system_card(self):
                raise AssertionError("should not be reached after fallback")

        def _mock_factory():
            tmp = Path(tempfile.mkdtemp(prefix="pr3-f3-")) / "preview.png"
            return MockRenderer(width=1920, height=1080, output_path=tmp)

        wrapper = AutoFallbackRenderer(
            primary=_PrimaryDeadOnVideo(),
            mock_factory=_mock_factory,
        )
        wrapper.render_frame(b"\x00" * (1920 * 1080 * 3))
        assert wrapper.is_in_fallback is True
        return wrapper

    def test_render_preview_returns_502_when_wrapper_in_fallback(
        self, client: TestClient, monkeypatch
    ):
        """The wrapper has already fallen back to the mock (video
        subprocess died). The card render succeeds on the mock but
        `is_in_fallback` is True → the preview endpoint returns 502
        so QA glass-verifies on truthful responses."""
        from openmarquee import dependencies as deps

        wrapper = self._make_fallen_wrapper()
        monkeypatch.setattr(deps, "get_renderer", lambda: wrapper)
        response = client.post(
            "/api/system/render-system-card-preview",
            json={"kind": "SETUP"},
        )
        assert response.status_code == 502
        assert "fallback" in response.text.lower()
        # Card DID paint on the mock (success semantics for the
        # supervisor path), so the mock recorded the call — the
        # endpoint reports 502 to the CALLER regardless.
        assert wrapper._mock.system_card_calls == [{"kind": "SETUP"}]

    def test_clear_preview_returns_502_when_wrapper_in_fallback(
        self, client: TestClient, monkeypatch
    ):
        from openmarquee import dependencies as deps

        wrapper = self._make_fallen_wrapper()
        monkeypatch.setattr(deps, "get_renderer", lambda: wrapper)
        response = client.post("/api/system/clear-system-card-preview")
        assert response.status_code == 502
        assert "fallback" in response.text.lower()


# ============================================================
# PR3 fix-pass S2 (2026-07-01) — byte-length clamp on _check_len.
# ============================================================


def test_check_len_uses_utf8_byte_length():
    """Regression pin for the codepoint-vs-byte clamp fix. A 40-char
    ASCII SSID is 40 bytes (fits MAX_SSID_LEN=40). A 21-char UTF-8
    string of 2-byte grapheme runs is 42 bytes (over cap) — must
    reject with 422."""
    from openmarquee.api_system import _MAX_SSID_LEN, _check_len

    # 40 ASCII chars = 40 bytes — passes.
    _check_len("ssid", "A" * _MAX_SSID_LEN, _MAX_SSID_LEN)
    # 21 é chars = 42 bytes — must raise.
    import pytest as _pytest

    with _pytest.raises(Exception) as excinfo:  # HTTPException
        _check_len("ssid", "é" * 21, _MAX_SSID_LEN)
    assert "40-byte cap" in str(excinfo.value.detail)


# ============================================================
# PR3 fix-pass S2 (2026-07-01) — 401 tests for both preview
# endpoints when auth is engaged (production shape). Piggy-backs
# on the same env-flip pattern the auth suite uses so the middle-
# ware actually gates.
# ============================================================


@pytest.fixture
def client_auth_engaged(tmp_path: Path):
    """TestClient with OPENMARQUEE_DISABLE_AUTH unset so the auth
    middleware gates. Isolated AuthStorage path per test. See
    backend/tests/test_auth.py::client for the pattern."""
    import os as _os
    from unittest.mock import patch

    from openmarquee.dependencies import _auth_storage_singleton

    auth_path = tmp_path / "auth.json"
    _auth_storage_singleton.cache_clear()
    with patch.dict(
        _os.environ,
        {"OPENMARQUEE_AUTH_PATH": str(auth_path)},
    ):
        _os.environ.pop("OPENMARQUEE_DISABLE_AUTH", None)
        try:
            from openmarquee.app import app

            with TestClient(app) as c:
                yield c
        finally:
            _os.environ["OPENMARQUEE_DISABLE_AUTH"] = "1"
    _auth_storage_singleton.cache_clear()


def test_render_system_card_preview_requires_auth(client_auth_engaged: TestClient):
    """The /api/system/render-system-card-preview endpoint is NOT
    in the auth_middleware allowlist — an unauthenticated POST must
    return 401 so an attacker on the LAN can't drive card state on
    a production sign."""
    response = client_auth_engaged.post(
        "/api/system/render-system-card-preview",
        json={"kind": "SETUP"},
    )
    assert response.status_code == 401


def test_clear_system_card_preview_requires_auth(client_auth_engaged: TestClient):
    """Companion 401 pin for the clear-preview endpoint."""
    response = client_auth_engaged.post("/api/system/clear-system-card-preview")
    assert response.status_code == 401


# ── 2026-07-16 (F2, handover-blocker): clicking Enable Tailscale brought the
# node up for THAT BOOT ONLY. `openmarquee-tailscale.service` reads
# `tailscale_enabled` from settings.json on every boot, and NOTHING in the
# repo ever wrote that field except the Settings checkbox echoing itself — so
# a sign whose operator enabled Tailscale but never ticked-and-saved the box
# read False and got taken back off the tailnet on its first reboot. Worse,
# the checkbox is disabled until the station radio is on, and that radio was
# itself wrong on an NM-provisioned sign, so there was no path to True at all.
# Scenario: Jason unboxes the sign, enables Tailscale, it works, he reboots,
# and we lose remote support to a customer's device.


def _tailscale_stub(tmp_path, monkeypatch):
    import stat as _stat

    stub = tmp_path / "tailscale"
    stub.write_text(
        "#!/usr/bin/env bash\necho 'visit https://login.tailscale.com/a/teststub42' >&2\nsleep 30\n"
    )
    stub.chmod(stub.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))
    return stub


def test_tailscale_up_persists_enabled_so_it_survives_a_reboot(
    client: TestClient, tmp_path, monkeypatch
):
    """THE FIX. Enabling must outlive the process that enabled it."""
    assert client.get("/api/settings").json()["tailscale_enabled"] is False, (
        "precondition: a fresh sign defaults to disabled"
    )
    _tailscale_stub(tmp_path, monkeypatch)

    assert client.post("/api/system/tailscale/up").status_code == 200

    assert client.get("/api/settings").json()["tailscale_enabled"] is True, (
        "the boot unit reads this field; without it the node is logged off on the next reboot"
    )


def test_tailscale_up_does_not_persist_enabled_when_the_spawn_errors(
    client: TestClient, monkeypatch
):
    """CONTROL: proves the write is gated on the spawn actually working,
    not just unconditionally stamped by the endpoint."""
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", "/nonexistent/tailscale")

    res = client.post("/api/system/tailscale/up")

    assert res.status_code == 200
    assert res.json()["state"] == "error"
    assert client.get("/api/settings").json()["tailscale_enabled"] is False, (
        "a failed enable must not claim the sign is on the tailnet"
    )


def test_tailscale_up_preserves_other_settings(client: TestClient, tmp_path, monkeypatch):
    """The persist path builds a full model; make sure it doesn't drop or
    factory-reset neighbouring values (model_copy(update=) would skip the
    validators — the settings.json quarantine hazard)."""
    body = client.get("/api/settings").json()
    body["brightness"] = 37
    body["sign_name"] = "jasonssign1"
    assert client.put("/api/settings", json=body).status_code == 200
    _tailscale_stub(tmp_path, monkeypatch)

    client.post("/api/system/tailscale/up")

    after = client.get("/api/settings").json()
    assert after["tailscale_enabled"] is True
    assert after["brightness"] == 37
    assert after["sign_name"] == "jasonssign1"


def test_tailscale_up_persists_enabled_when_the_node_is_already_authenticated(
    client: TestClient, tmp_path, monkeypatch
):
    """`tailscale up` on an already-registered node exits 0 WITHOUT printing
    an auth URL, which start_up reports as state="error". Keying the persist
    off that alone would skip exactly the sign this fix exists for: up on the
    tailnet, but with the field False, so the boot unit never provisions
    `serve` / HTTPS. Re-read the live status and persist anyway."""
    import stat as _stat

    stub = tmp_path / "tailscale"
    # Exits 0, prints no auth URL -> start_up's EOF-without-URL branch.
    stub.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub.chmod(stub.stat().st_mode | _stat.S_IXUSR | _stat.S_IXGRP | _stat.S_IXOTH)
    monkeypatch.setenv("OPENMARQUEE_TAILSCALE_BIN", str(stub))

    async def _authenticated():
        return {"state": "authenticated", "hostname": "jasonssign1", "ipv4": "100.1.2.3"}

    from openmarquee import tailscale

    monkeypatch.setattr(tailscale, "read_status", _authenticated)

    assert client.post("/api/system/tailscale/up").status_code == 200
    assert client.get("/api/settings").json()["tailscale_enabled"] is True, (
        "a node that is already up must still get its intent persisted"
    )
