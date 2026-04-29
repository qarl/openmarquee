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


def test_format_mode_hub75_uses_widthxheight():
    assert _format_mode("hub75", 128, 64) == "hub75-128x64"


def test_format_mode_hdmi_uses_height_only():
    """HDMI is operator-spoken in resolution-class terms (1080p)."""
    assert _format_mode("hdmi", 1920, 1080) == "hdmi-1080"


def test_format_mode_ws281x_strip_when_one_dimension_is_one():
    """1×N or N×1 is a strip; the dim-pair is operator-noise."""
    assert _format_mode("ws281x", 1, 64) == "ws281x-strip"
    assert _format_mode("ws281x", 64, 1) == "ws281x-strip"


def test_format_mode_ws281x_matrix_keeps_dims():
    """A 16×16 matrix wired from a strip is still ws281x but not a strip."""
    assert _format_mode("ws281x", 16, 16) == "ws281x-16x16"


def test_format_mode_composite_uses_widthxheight():
    """Composite analog out — small enough that operators care about both
    dims, so the WxH form sticks."""
    assert _format_mode("composite", 720, 480) == "composite-720x480"


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
    """Save settings to a hub75 panel; /info's mode follows."""
    payload = SystemSettings(
        output_mode="hub75",
        display_width=128,
        display_height=64,
    ).model_dump(mode="json")
    put = client.put("/api/settings", json=payload)
    assert put.status_code == 200

    info = client.get("/api/system/info").json()
    assert info["mode"] == "hub75-128x64"


def test_info_mode_reflects_ws281x_strip_settings(client: TestClient):
    """1×64 ws281x strip → 'ws281x-strip', not 'ws281x-1x64'."""
    payload = SystemSettings(
        output_mode="ws281x",
        display_width=1,
        display_height=64,
    ).model_dump(mode="json")
    client.put("/api/settings", json=payload)

    info = client.get("/api/system/info").json()
    assert info["mode"] == "ws281x-strip"


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


def test_info_sets_cors_header_so_peers_can_read_it(client: TestClient):
    """Peer flock UIs fetch each other's /api/system/info to render
    per-peer thumbs at the correct rotation. That's a cross-origin GET,
    so the response must carry Access-Control-Allow-Origin: * (matching
    the thumbnail endpoint's pattern)."""
    response = client.get("/api/system/info")
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


def test_info_passes_unrotated_dims_through_when_rotation_is_zero(client: TestClient):
    payload = SystemSettings(
        output_mode="hub75",
        display_width=128,
        display_height=64,
        display_rotation=0,
    ).model_dump(mode="json")
    client.put("/api/settings", json=payload)

    info = client.get("/api/system/info").json()
    assert info["display_width"] == 128
    assert info["display_height"] == 64
    assert info["display_rotation"] == 0


def test_info_signal_in_range_when_present(client: TestClient):
    """Whatever /proc/net/wireless reports (or the fallback), signal
    must be in [0, 100]. The Pydantic model doesn't enforce a range
    on the response side; this catches a parser regression."""
    info = client.get("/api/system/info").json()
    assert 0 <= info["signal"] <= 100
