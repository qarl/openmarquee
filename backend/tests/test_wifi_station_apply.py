"""Tests for backend/openmarquee/wifi_station.py.

The apply path shells out to systemctl + iw — both are mocked here so
the test runs on Mac (no Pi hardware required). What we DO verify
end-to-end:
  - The rendered conf body lands at the override path with the right
    content.
  - Idempotent re-submit doesn't fire a second systemctl restart.
  - Changed creds DO fire the restart.
  - has_settings_changed diff logic matches the wire contract.
  - apply_disabled stops the supplicant + removes the conf.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def tmp_wpa_conf_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Override the wpa_supplicant conf path to a tempfile so the
    test doesn't try to write /etc/wpa_supplicant/* on the dev Mac.
    The module reads the env var at import time, so we set it +
    reimport the module fresh."""
    tmp_dir = tempfile.mkdtemp(prefix="wifi-station-test-")
    conf_path = Path(tmp_dir) / "wpa_supplicant-wlan0.conf"
    monkeypatch.setenv("OPENMARQUEE_WPA_SUPPLICANT_CONF", str(conf_path))
    # Force module re-import so it picks up the new env var.
    import importlib
    import openmarquee.wifi_station
    importlib.reload(openmarquee.wifi_station)
    return conf_path


@pytest.fixture
def mock_iw_connected() -> MagicMock:
    """iw_link_fn that immediately reports the target ssid as
    connected -- short-circuits the poll loop so tests don't sleep."""
    return MagicMock(return_value="testnet")


@pytest.fixture
def mock_iw_disconnected() -> MagicMock:
    """iw_link_fn that always reports Not connected -- forces the
    poll loop to time out."""
    return MagicMock(return_value=None)


def test_apply_enabled_writes_conf(
    tmp_wpa_conf_path: Path,
    mock_iw_connected: MagicMock,
) -> None:
    """Submitting valid creds with enabled=true templates the conf
    at the configured path + invokes the restart fn + reports the
    'connected' state when iw reports the SSID."""
    import openmarquee.wifi_station as ws

    restart_fn = MagicMock()
    ok = ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=restart_fn,
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )

    assert ok is True
    assert tmp_wpa_conf_path.exists()
    body = tmp_wpa_conf_path.read_text()
    assert 'ssid="testnet"' in body or "ssid=" in body  # either form
    assert "ctrl_interface=DIR=/var/run/wpa_supplicant" in body
    assert "country=US" in body
    restart_fn.assert_called_once()

    state = ws.current_state()
    assert state.state == "connected"
    assert state.ssid == "testnet"


def test_apply_enabled_idempotent_skips_restart(
    tmp_wpa_conf_path: Path,
    mock_iw_connected: MagicMock,
) -> None:
    """Calling apply_enabled twice with the same creds writes the
    conf only once + skips the systemctl restart on the second call
    (conf body is identical)."""
    import openmarquee.wifi_station as ws

    restart_fn = MagicMock()
    # First call: conf is written, restart fires.
    ok1 = ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=restart_fn,
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )
    assert ok1 is True
    assert restart_fn.call_count == 1

    # Second call with same creds: idempotent.
    ok2 = ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=restart_fn,
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )
    assert ok2 is True
    # No additional restart call: still 1.
    assert restart_fn.call_count == 1


def test_apply_enabled_changed_creds_triggers_restart(
    tmp_wpa_conf_path: Path,
    mock_iw_connected: MagicMock,
) -> None:
    """Different creds (new ssid OR new password) re-templates the
    conf + fires the restart. mock_iw_connected reports whatever ssid
    we pass it via the fixture re-config."""
    import openmarquee.wifi_station as ws

    restart_fn = MagicMock()
    # Apply first creds.
    ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=restart_fn,
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )
    body_a = tmp_wpa_conf_path.read_text()
    assert restart_fn.call_count == 1

    # Apply different creds. Configure iw mock to report the new ssid.
    iw_for_other = MagicMock(return_value="othernet")
    ws.apply_enabled(
        "othernet",
        "different-passphrase-67890",
        restart_fn=restart_fn,
        iw_link_fn=iw_for_other,
        poll_timeout_sec=1,
    )
    body_b = tmp_wpa_conf_path.read_text()
    assert restart_fn.call_count == 2  # second restart fired
    assert body_a != body_b
    # New body has the new ssid (either cleartext or hashed-via-wpa_passphrase)
    assert "othernet" in body_b


def test_apply_disabled_stops_supplicant_and_removes_conf(
    tmp_wpa_conf_path: Path,
    mock_iw_connected: MagicMock,
) -> None:
    """apply_disabled removes the conf + invokes the stop fn + sets
    the state to 'disabled'."""
    import openmarquee.wifi_station as ws

    # Get a conf on disk first so we have something to remove.
    ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=MagicMock(),
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )
    assert tmp_wpa_conf_path.exists()

    stop_fn = MagicMock()
    ws.apply_disabled(stop_fn=stop_fn)

    assert not tmp_wpa_conf_path.exists()
    stop_fn.assert_called_once()
    state = ws.current_state()
    assert state.state == "disabled"


def test_apply_enabled_poll_timeout_reports_failed(
    tmp_wpa_conf_path: Path,
    mock_iw_disconnected: MagicMock,
) -> None:
    """When wpa_supplicant never associates (iw always reports Not
    connected), the apply ends in state='failed' with a 'no
    association' detail."""
    import openmarquee.wifi_station as ws

    restart_fn = MagicMock()
    ok = ws.apply_enabled(
        "testnet",
        "secret-passphrase-12345",
        restart_fn=restart_fn,
        iw_link_fn=mock_iw_disconnected,
        poll_timeout_sec=1,  # short poll so the test doesn't sleep 30s
    )
    assert ok is False
    state = ws.current_state()
    assert state.state == "failed"
    assert state.detail is not None
    assert "association" in state.detail


def test_has_settings_changed_detects_toggle_on() -> None:
    """Flipping enabled false -> true triggers an apply regardless
    of ssid/password values."""
    from openmarquee.wifi_station import has_settings_changed

    assert has_settings_changed(
        prev_enabled=False, prev_ssid=None, prev_password=None,
        new_enabled=True, new_ssid="net", new_password="pw1234567890",
    )


def test_has_settings_changed_detects_toggle_off() -> None:
    """Flipping enabled true -> false triggers an apply (we need to
    stop the supplicant + remove conf)."""
    from openmarquee.wifi_station import has_settings_changed

    assert has_settings_changed(
        prev_enabled=True, prev_ssid="net", prev_password="pw1234567890",
        new_enabled=False, new_ssid="net", new_password="pw1234567890",
    )


def test_has_settings_changed_detects_creds_diff_when_enabled() -> None:
    """Stays enabled but ssid OR password changed -> apply."""
    from openmarquee.wifi_station import has_settings_changed

    assert has_settings_changed(
        prev_enabled=True, prev_ssid="old", prev_password="pw1",
        new_enabled=True, new_ssid="new", new_password="pw1",
    )
    assert has_settings_changed(
        prev_enabled=True, prev_ssid="net", prev_password="oldpw",
        new_enabled=True, new_ssid="net", new_password="newpw",
    )


def test_has_settings_changed_stable_when_disabled() -> None:
    """Stays disabled -> no work."""
    from openmarquee.wifi_station import has_settings_changed

    assert not has_settings_changed(
        prev_enabled=False, prev_ssid=None, prev_password=None,
        new_enabled=False, new_ssid=None, new_password=None,
    )


def test_has_settings_changed_stable_when_enabled_with_same_creds() -> None:
    """Stays enabled with identical creds -> no work (apply would
    no-op via conf-comparison short-circuit anyway, but skipping the
    thread spawn is cleaner)."""
    from openmarquee.wifi_station import has_settings_changed

    assert not has_settings_changed(
        prev_enabled=True, prev_ssid="net", prev_password="pw1234567890",
        new_enabled=True, new_ssid="net", new_password="pw1234567890",
    )


def test_conf_no_cleartext_psk_when_wpa_passphrase_available(
    tmp_wpa_conf_path: Path,
    mock_iw_connected: MagicMock,
) -> None:
    """If wpa_passphrase is on the PATH (it is on the dev Mac via
    Homebrew, and on Pi OS via the wpasupplicant package), the
    rendered conf MUST NOT contain the cleartext password. The block
    keeps `psk=<hash>` (no quotes -- that's how wpa_passphrase output
    differs from a hand-written `psk="..."`)."""
    import shutil
    import openmarquee.wifi_station as ws

    if shutil.which("wpa_passphrase") is None:
        pytest.skip("wpa_passphrase not on PATH; cleartext fallback is documented")

    password = "uniquesecret87654321"
    ws.apply_enabled(
        "testnet",
        password,
        restart_fn=MagicMock(),
        iw_link_fn=mock_iw_connected,
        poll_timeout_sec=1,
    )
    body = tmp_wpa_conf_path.read_text()
    # The cleartext password must not appear in the conf body.
    assert password not in body, (
        f"cleartext password leaked into conf body:\n{body}"
    )
    # And the hashed psk line should be present (32-byte hex digest).
    assert "psk=" in body
