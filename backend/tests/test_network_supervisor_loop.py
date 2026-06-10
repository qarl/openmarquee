"""P1.2-A observe-only loop tests.

Cover:
  * parse_iw_freq_mhz on canonical `iw dev wlan0 info` output and
    the not-associated case.
  * supervisor_observe_loop happy-path + cancel-and-shutdown via
    a mocked socket client.
  * Mac-dev path (no `iw` binary, no /var/run/wpa_supplicant) — loop
    keeps running + state machine remains driveable via apply_event.

The actual `iw` subprocess + real wpa_supplicant socket are NOT
exercised here (would need a Linux fixture). Those land in the
P1.2-A live-fire soak on openMarqueeDev.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from openmarquee.network_supervisor import (
    NetworkSupervisor,
    SupervisorConfig,
    SupervisorEvent,
    SupervisorState,
)
from openmarquee.network_supervisor_loop import (
    STA_FREQ_POLL_INTERVAL_S,
    WPA_POLL_INTERVAL_S,
    _iw_binary_present,
    parse_iw_freq_mhz,
    supervisor_observe_loop,
)

# ============================================================
# parse_iw_freq_mhz — pure parser tests
# ============================================================


@pytest.mark.parametrize(
    "iw_output,expected",
    [
        # Canonical associated case.
        (
            "Connected to 11:22:33:44:55:66 (on wlan0)\n"
            "\tSSID: pikazo\n"
            "\tfreq: 2462\n"
            "\tRX: 12345 bytes (98 packets)\n"
            "\tTX: 67890 bytes (54 packets)\n"
            "\tsignal: -55 dBm\n"
            "\tbss flags: short-preamble short-slot-time\n"
            "\tdtim period: 2\n"
            "\tbeacon int: 100\n",
            None,  # `iw dev wlan0 link` shape, no `(<freq> MHz)` pattern
        ),
        # `iw dev wlan0 info` shape — has `channel <N> (<freq> MHz)` line.
        (
            "Interface wlan0\n"
            "\tifindex 3\n"
            "\twdev 0x1\n"
            "\taddr aa:bb:cc:dd:ee:ff\n"
            "\tssid pikazo\n"
            "\ttype managed\n"
            "\twiphy 0\n"
            "\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz\n"
            "\ttxpower 31.00 dBm\n",
            2437,
        ),
        (
            "\tchannel 11 (2462 MHz), width: 20 MHz, center1: 2462 MHz",
            2462,
        ),
        (
            "\tchannel 1 (2412 MHz), width: 20 MHz",
            2412,
        ),
        # Not associated — no freq line.
        ("Interface wlan0\n\ttype managed\n", None),
        # Empty output.
        ("", None),
        # Malformed value inside parens.
        ("channel 6 (banana MHz)", None),
    ],
)
def test_parse_iw_freq_mhz(iw_output, expected):
    assert parse_iw_freq_mhz(iw_output) == expected


# ============================================================
# supervisor_observe_loop — happy path + cancel
# ============================================================


class _MockSocketClient:
    """In-memory stand-in for WpaSupplicantSocketClient. The loop
    instantiates THIS class instead of the real one when injected.
    """

    def __init__(self, *, fail_connect: bool = False, events: list[str] | None = None):
        self.fail_connect = fail_connect
        self.events = events or []
        self.connected = False
        self.closed = False

    def connect(self):
        if self.fail_connect:
            raise FileNotFoundError(self.fail_connect)
        self.connected = True

    def receive_event(self) -> str | None:
        if self.events:
            return self.events.pop(0)
        return None

    def close(self):
        self.closed = True


def _make_supervisor(tmp_path: Path) -> NetworkSupervisor:
    return NetworkSupervisor(
        config=SupervisorConfig(state_file=tmp_path / "network-state.json"),
    )


@pytest.mark.asyncio
async def test_loop_runs_then_cancels(tmp_path: Path, monkeypatch):
    """The loop should start, run at least one cycle, and exit
    cleanly on cancel. Mock the socket client so the loop doesn't
    touch the real wpa_supplicant ctrl path.
    """
    sup = _make_supervisor(tmp_path)
    socket_client = _MockSocketClient(events=[])

    def _mock_client_ctor(*args, **kwargs):
        return socket_client

    monkeypatch.setattr(
        "openmarquee.network_supervisor_loop.WpaSupplicantSocketClient", _mock_client_ctor
    )
    # iw poll always returns None (no `iw` binary on Mac dev / no
    # association on Pi pre-creds).

    async def _no_freq():
        return None

    monkeypatch.setattr("openmarquee.network_supervisor_loop.poll_sta_freq_mhz", _no_freq)

    task = asyncio.create_task(
        supervisor_observe_loop(
            sup,
            wpa_poll_interval_s=0.01,
            sta_freq_poll_interval_s=0.05,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket_client.connected is True
    assert socket_client.closed is True


@pytest.mark.asyncio
async def test_loop_drives_state_machine_from_wpa_events(tmp_path: Path, monkeypatch):
    """End-to-end: mocked socket emits a CTRL-EVENT-CONNECTED;
    supervisor should transition out of SETUP."""
    sup = _make_supervisor(tmp_path)
    # Pre-arm: pretend we have stored credentials so SETUP ->
    # CONNECTING is allowed; then the CTRL-EVENT-CONNECTED triggers
    # CONNECTING -> LINGER.
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    assert sup.current_state == SupervisorState.CONNECTING

    socket_client = _MockSocketClient(
        events=[
            "<3>CTRL-EVENT-CONNECTED - Connection to 11:22:33:44:55:66 completed [id=0 id_str=]\n",
        ],
    )
    monkeypatch.setattr(
        "openmarquee.network_supervisor_loop.WpaSupplicantSocketClient",
        lambda *a, **k: socket_client,
    )

    async def _no_freq():
        return None

    monkeypatch.setattr("openmarquee.network_supervisor_loop.poll_sta_freq_mhz", _no_freq)

    task = asyncio.create_task(supervisor_observe_loop(sup, wpa_poll_interval_s=0.01))
    # Give the loop time to drain the queued event.
    for _ in range(20):
        await asyncio.sleep(0.02)
        if sup.current_state == SupervisorState.LINGER:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sup.current_state == SupervisorState.LINGER


@pytest.mark.asyncio
async def test_loop_survives_missing_wpa_socket(tmp_path: Path, monkeypatch):
    """Mac-dev path: socket connect raises FileNotFoundError; loop
    should log + sleep + retry without crashing."""
    sup = _make_supervisor(tmp_path)

    def _failing_client(*args, **kwargs):
        return _MockSocketClient(fail_connect=True)

    monkeypatch.setattr(
        "openmarquee.network_supervisor_loop.WpaSupplicantSocketClient", _failing_client
    )

    async def _no_freq():
        return None

    monkeypatch.setattr("openmarquee.network_supervisor_loop.poll_sta_freq_mhz", _no_freq)

    task = asyncio.create_task(supervisor_observe_loop(sup, wpa_poll_interval_s=0.01))
    await asyncio.sleep(0.1)
    # The loop should still be running (failed connect doesn't
    # cancel; the loop retries on its own cadence).
    assert not task.done()
    # State machine should still be driveable directly.
    sup.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
    assert sup.current_state == SupervisorState.CONNECTING
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_polls_sta_freq_periodically(tmp_path: Path, monkeypatch):
    """STA-freq poll should fire on its own cadence + drive
    apply_sta_freq."""
    sup = _make_supervisor(tmp_path)
    socket_client = _MockSocketClient()
    monkeypatch.setattr(
        "openmarquee.network_supervisor_loop.WpaSupplicantSocketClient",
        lambda *a, **k: socket_client,
    )
    poll_count = [0]

    async def _stub_freq():
        poll_count[0] += 1
        # Channel 11 freq.
        return 2462

    monkeypatch.setattr("openmarquee.network_supervisor_loop.poll_sta_freq_mhz", _stub_freq)

    task = asyncio.create_task(
        supervisor_observe_loop(sup, wpa_poll_interval_s=0.005, sta_freq_poll_interval_s=0.02)
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Should have polled at least 3 times (100ms / 20ms interval - 1
    # initial offset).
    assert poll_count[0] >= 3, f"poll_count={poll_count[0]}"
    # supervisor saw channel 11.
    assert sup.current_sta_freq_mhz == 2462


# ============================================================
# Constants + smoke for the binary-presence probe
# ============================================================


def test_intervals_are_sensible_defaults():
    # Wpa events drive state-machine latency; sub-second is plenty.
    assert 0.0 < WPA_POLL_INTERVAL_S <= 1.0
    # STA freq only changes on reassociation / CSA; tight polling
    # wastes a subprocess call per second otherwise.
    assert 1.0 < STA_FREQ_POLL_INTERVAL_S <= 60.0


def test_iw_binary_present_returns_bool():
    # Doesn't matter which value; the function must not raise.
    assert isinstance(_iw_binary_present(), bool)
