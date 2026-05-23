"""Tests for backend/openmarquee/wifi_station.py (nmcli backend).

The apply path shells out to `nmcli` — mocked here so the test
runs on Mac with no NetworkManager / no Pi. What we DO verify:
  - Idempotent re-submit (same SSID already active) -> no destructive op.
  - Different SSID submit -> delete-then-connect dance.
  - apply_disabled removes the active connection profile.
  - nmcli non-zero exit -> state='failed' with stderr in detail.
  - Poll-timeout when device-state never reaches 100 -> 'failed'.
  - has_settings_changed diff helper.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def reset_module():
    """Re-import wifi_station so each test starts with a fresh
    module-global state (the _STATE singleton + the nmcli_runner
    handle) regardless of test order."""
    import openmarquee.wifi_station
    importlib.reload(openmarquee.wifi_station)
    yield openmarquee.wifi_station


def _nmcli_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> object:
    """Build a fake _NmcliResult for monkey-patched nmcli_runner returns."""
    import openmarquee.wifi_station as ws
    return ws._NmcliResult(returncode=returncode, stdout=stdout, stderr=stderr)


def test_apply_enabled_idempotent_when_already_connected(reset_module) -> None:
    """If wlan0 is already on the requested SSID with state '100
    (connected)', apply_enabled MUST NOT issue a connect or delete --
    just snaps state to 'connected' and returns True."""
    ws = reset_module
    target_ssid = "pikazo"

    def fake_runner(args, *, sudo=False, timeout=30):
        # `nmcli -t -f NAME,DEVICE connection show --active` -> "pikazo:wlan0"
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout=f"{target_ssid}:wlan0\n")
        # `nmcli -t -f GENERAL.STATE device show wlan0` -> "100 (connected)"
        if "GENERAL.STATE" in args and "show" in args:
            return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
        # rescan is best-effort and not gated on this test's idempotent
        # path -- but the new apply path doesn't call rescan when
        # idempotent-short-circuiting, so it should NOT appear.
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            raise AssertionError(
                "rescan must NOT fire on idempotent-already-connected path"
            )
        # Anything else (e.g. wifi connect) MUST NOT be called.
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "Picasso!", poll_timeout_sec=1)
    assert ok is True
    state = ws.current_state()
    assert state.state == "connected"
    assert state.ssid == target_ssid


def test_apply_enabled_new_ssid_deletes_old_then_connects(reset_module) -> None:
    """When wlan0 is on a DIFFERENT active connection, apply_enabled
    must delete the old profile BEFORE issuing the new connect (so
    nmcli's auto-fallback can't reuse stale creds on failure)."""
    ws = reset_module
    new_ssid = "newnet"
    old_ssid = "oldnet"
    call_log: list = []

    def fake_runner(args, *, sudo=False, timeout=30):
        call_log.append((tuple(args), sudo))
        if "connection" in args and "show" in args and "--active" in args:
            # First query: still on oldnet.
            # After we delete + connect, the next query reports newnet.
            if any(call[0][:5] == ("device", "wifi", "connect", new_ssid, "password")
                   for call in call_log):
                return _nmcli_result(stdout=f"{new_ssid}:wlan0\n")
            return _nmcli_result(stdout=f"{old_ssid}:wlan0\n")
        if "GENERAL.STATE" in args:
            return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["connection", "delete", old_ssid]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(new_ssid, "newpassword", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is True

    # The delete-old MUST appear in the call log BEFORE the
    # wifi-connect-new (sequential ordering pin).
    delete_idx = next(
        i for i, (args, _) in enumerate(call_log)
        if args[:3] == ("connection", "delete", old_ssid)
    )
    connect_idx = next(
        i for i, (args, _) in enumerate(call_log)
        if args[:3] == ("device", "wifi", "connect")
    )
    assert delete_idx < connect_idx, (
        f"delete-old must precede connect-new; log: {call_log}"
    )

    state = ws.current_state()
    assert state.state == "connected"
    assert state.ssid == new_ssid


def test_apply_enabled_nmcli_failure_sets_failed_state(reset_module) -> None:
    """When `nmcli device wifi connect` returns non-zero (wrong
    password, ssid out of range), state -> 'failed' with the
    stderr verbatim in detail. No 'success on subprocess error'."""
    ws = reset_module
    target_ssid = "homenet"

    def fake_runner(args, *, sudo=False, timeout=30):
        if "connection" in args and "show" in args and "--active" in args:
            # Not currently connected to anything.
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            return _nmcli_result(stdout="GENERAL.STATE:30 (disconnected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            return _nmcli_result(
                returncode=4,
                stderr=(
                    "Error: Connection activation failed: (7) Secrets "
                    "were required, but not provided.\n"
                ),
            )
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "wrongpassword", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is False
    state = ws.current_state()
    assert state.state == "failed"
    assert state.detail is not None
    assert "Secrets were required" in state.detail
    assert state.ssid == target_ssid


def test_apply_enabled_poll_timeout_when_state_never_reaches_100(
    reset_module,
) -> None:
    """If nmcli connect returns 0 but the device GENERAL.STATE never
    reaches '100 (connected)' within the poll budget, state ->
    'failed' with 'no association within Ns' detail."""
    ws = reset_module
    target_ssid = "slownet"

    def fake_runner(args, *, sudo=False, timeout=30):
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            # Stuck in connecting state forever.
            return _nmcli_result(stdout="GENERAL.STATE:50 (connecting)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "rightpassword", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is False
    state = ws.current_state()
    assert state.state == "failed"
    assert "association" in (state.detail or "")


def test_apply_disabled_deletes_active_connection(reset_module) -> None:
    """apply_disabled removes the active wlan0 connection profile
    (uses `connection delete`, NOT `device disconnect` -- the latter
    would free the whole device + might cascade into ap0)."""
    ws = reset_module
    current_ssid = "old-managed-network"
    call_log: list = []

    def fake_runner(args, *, sudo=False, timeout=30):
        call_log.append((tuple(args), sudo))
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout=f"{current_ssid}:wlan0\n")
        if args[:3] == ["connection", "delete", current_ssid]:
            return _nmcli_result(returncode=0)
        # device disconnect would be a BUG -- explicit guard.
        if args[:2] == ["device", "disconnect"]:
            raise AssertionError(
                "apply_disabled used device disconnect; must use connection delete"
            )
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ws.apply_disabled()
    state = ws.current_state()
    assert state.state == "disabled"
    # Verify the delete fired.
    assert any(
        args[:3] == ("connection", "delete", current_ssid)
        for args, _ in call_log
    )


def test_apply_disabled_when_no_active_connection_is_a_noop(
    reset_module,
) -> None:
    """If wlan0 has no active connection (e.g., already disabled,
    or never enabled), apply_disabled silently no-ops + sets state
    to 'disabled'. No subprocess shells beyond the status query."""
    ws = reset_module
    call_log: list = []

    def fake_runner(args, *, sudo=False, timeout=30):
        call_log.append((tuple(args), sudo))
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout="")  # nothing active
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ws.apply_disabled()
    state = ws.current_state()
    assert state.state == "disabled"
    # Only the status query fired; no destructive op.
    assert len(call_log) == 1


def test_apply_enabled_uses_sudo_only_for_destructive_ops(
    reset_module,
) -> None:
    """Read-only queries (`connection show --active`, `device show
    wlan0`) run WITHOUT sudo. Destructive operations (`device wifi
    connect`, `connection delete`) run WITH sudo. Validates the
    sudoers grant boundary -- the sudoers fragment must match the
    actual code paths."""
    ws = reset_module
    target_ssid = "anothernet"
    sudo_log: list = []

    def fake_runner(args, *, sudo=False, timeout=30):
        sudo_log.append((tuple(args), sudo))
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ws.apply_enabled(target_ssid, "rightpassword", poll_timeout_sec=1, rescan_settle_sec=0)

    for args, sudo in sudo_log:
        if args[:3] == ("device", "wifi", "connect"):
            assert sudo is True, "wifi connect MUST use sudo"
        elif args[:2] == ("connection", "delete"):
            assert sudo is True, "connection delete MUST use sudo"
        elif args[:4] == ("device", "wifi", "rescan", "ifname"):
            # Rescan: NM allows scan ops without sudo (verified
            # 2026-05-14 on dev Pi). The sudoers fragment does NOT
            # need to grant rescan.
            assert sudo is False, "rescan MUST NOT use sudo"
        else:
            # Status queries: no sudo.
            assert sudo is False, f"read-only nmcli must not sudo: {args}"


# --- Task #468 (2026-05-14): rescan + unavailable-radio detection ---


def test_apply_enabled_rescans_before_connect(reset_module) -> None:
    """A `nmcli device wifi rescan ifname wlan0` must fire BEFORE the
    `nmcli device wifi connect` so a stale scan cache doesn't fast-
    fail with 'No network with SSID found'. Pin the rescan-before-
    connect ordering with a call-log index comparison."""
    ws = reset_module
    target_ssid = "freshnet"
    call_log: list = []
    connect_fired = False

    def fake_runner(args, *, sudo=False, timeout=30):
        nonlocal connect_fired
        call_log.append(tuple(args))
        if "connection" in args and "show" in args and "--active" in args:
            # After the connect call lands, report wlan0 on target_ssid
            # so the poll-for-connection loop satisfies + returns True.
            if connect_fired:
                return _nmcli_result(stdout=f"{target_ssid}:wlan0\n")
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            if connect_fired:
                return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
            return _nmcli_result(stdout="GENERAL.STATE:30 (disconnected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            connect_fired = True
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "password", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is True

    rescan_idx = next(
        i for i, args in enumerate(call_log)
        if args[:4] == ("device", "wifi", "rescan", "ifname")
    )
    connect_idx = next(
        i for i, args in enumerate(call_log)
        if args[:3] == ("device", "wifi", "connect")
    )
    assert rescan_idx < connect_idx, (
        f"rescan must precede connect; log: {call_log}"
    )


def test_apply_enabled_rescan_failure_is_best_effort(reset_module) -> None:
    """If `nmcli device wifi rescan` returns non-zero (e.g. NM busy,
    scan interval not elapsed yet), the connect attempt MUST still
    happen. The rescan is a cache-refresh hint, NOT a gate."""
    ws = reset_module
    target_ssid = "anyhownet"
    call_log: list = []
    connect_fired = False

    def fake_runner(args, *, sudo=False, timeout=30):
        nonlocal connect_fired
        call_log.append(tuple(args))
        if "connection" in args and "show" in args and "--active" in args:
            if connect_fired:
                return _nmcli_result(stdout=f"{target_ssid}:wlan0\n")
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            if connect_fired:
                return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
            return _nmcli_result(stdout="GENERAL.STATE:30 (disconnected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            # rescan fails with the typical 'Scanning not allowed
            # while unavailable' / 'Device or resource busy' shape.
            return _nmcli_result(
                returncode=4,
                stderr="Error: Scanning not allowed while unavailable\n",
            )
        if args[:3] == ["device", "wifi", "connect"]:
            connect_fired = True
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "password", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is True

    # Connect MUST appear despite rescan failure.
    assert any(
        args[:3] == ("device", "wifi", "connect")
        for args in call_log
    ), "connect must fire even when rescan errored"


def test_apply_enabled_radio_unavailable_fast_fails(reset_module) -> None:
    """When wlan0 is in state '20 (unavailable)' -- rfkill, missing
    brcmfmac, etc. -- apply_enabled MUST NOT waste cycles on rescan
    + connect (both would surface the same error with a misleading
    'No network with SSID found' shape). Surface an actionable
    detail immediately."""
    ws = reset_module
    target_ssid = "pikazo"
    call_log: list = []

    def fake_runner(args, *, sudo=False, timeout=30):
        call_log.append(tuple(args))
        if "connection" in args and "show" in args and "--active" in args:
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            return _nmcli_result(stdout="GENERAL.STATE:20 (unavailable)\n")
        # Anything beyond the two read-only queries is a bug.
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            raise AssertionError(
                "rescan must NOT fire when device is unavailable"
            )
        if args[:3] == ["device", "wifi", "connect"]:
            raise AssertionError(
                "connect must NOT fire when device is unavailable"
            )
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "password", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is False
    state = ws.current_state()
    assert state.state == "failed"
    # The detail must reference the actionable diagnosis: rfkill /
    # driver / power-cycle. Generic "nmcli exited" would be useless.
    detail = state.detail or ""
    assert "unavailable" in detail.lower()
    assert "rfkill" in detail.lower() or "driver" in detail.lower()
    assert state.ssid == target_ssid


def test_apply_enabled_disconnected_state_proceeds_normally(reset_module) -> None:
    """State 30 (disconnected) is the normal idle state when wlan0
    has no active connection but the radio is fine. apply_enabled
    must NOT fast-fail on this -- it should run rescan + connect."""
    ws = reset_module
    target_ssid = "homenet"
    call_log: list = []
    connect_fired = False

    def fake_runner(args, *, sudo=False, timeout=30):
        nonlocal connect_fired
        call_log.append(tuple(args))
        if "connection" in args and "show" in args and "--active" in args:
            if connect_fired:
                return _nmcli_result(stdout=f"{target_ssid}:wlan0\n")
            return _nmcli_result(stdout="")
        if "GENERAL.STATE" in args:
            if connect_fired:
                return _nmcli_result(stdout="GENERAL.STATE:100 (connected)\n")
            return _nmcli_result(stdout="GENERAL.STATE:30 (disconnected)\n")
        if args[:4] == ["device", "wifi", "rescan", "ifname"]:
            return _nmcli_result(returncode=0)
        if args[:3] == ["device", "wifi", "connect"]:
            connect_fired = True
            return _nmcli_result(returncode=0)
        raise AssertionError(f"unexpected nmcli call: {args}")

    ws.nmcli_runner = MagicMock(side_effect=fake_runner)
    ok = ws.apply_enabled(target_ssid, "password", poll_timeout_sec=1, rescan_settle_sec=0)
    assert ok is True
    state = ws.current_state()
    assert state.state == "connected"
    # Both rescan + connect should have fired (the path's full flow).
    assert any(args[:4] == ("device", "wifi", "rescan", "ifname") for args in call_log)
    assert any(args[:3] == ("device", "wifi", "connect") for args in call_log)


def test_device_state_code_parses_numeric_prefix() -> None:
    """The pre-check relies on parsing the leading integer code from
    nmcli's state string (the parenthetical text is human prose).
    Pin the parser shape so a future nmcli format-change is loud."""
    from openmarquee.wifi_station import _device_state_code
    assert _device_state_code("100 (connected)") == 100
    assert _device_state_code("20 (unavailable)") == 20
    assert _device_state_code("30 (disconnected)") == 30
    assert _device_state_code("50 (connecting (config))") == 50
    assert _device_state_code("") is None
    assert _device_state_code("(unrecognized)") is None


# --- has_settings_changed tests (preserved from prior version) -------------


def test_has_settings_changed_detects_toggle_on() -> None:
    from openmarquee.wifi_station import has_settings_changed
    assert has_settings_changed(
        prev_enabled=False, prev_ssid=None, prev_password=None,
        new_enabled=True, new_ssid="net", new_password="pw1234567890",
    )


def test_has_settings_changed_detects_toggle_off() -> None:
    from openmarquee.wifi_station import has_settings_changed
    assert has_settings_changed(
        prev_enabled=True, prev_ssid="net", prev_password="pw1234567890",
        new_enabled=False, new_ssid="net", new_password="pw1234567890",
    )


def test_has_settings_changed_detects_creds_diff_when_enabled() -> None:
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
    from openmarquee.wifi_station import has_settings_changed
    assert not has_settings_changed(
        prev_enabled=False, prev_ssid=None, prev_password=None,
        new_enabled=False, new_ssid=None, new_password=None,
    )


def test_has_settings_changed_stable_when_enabled_with_same_creds() -> None:
    from openmarquee.wifi_station import has_settings_changed
    assert not has_settings_changed(
        prev_enabled=True, prev_ssid="net", prev_password="pw1234567890",
        new_enabled=True, new_ssid="net", new_password="pw1234567890",
    )
