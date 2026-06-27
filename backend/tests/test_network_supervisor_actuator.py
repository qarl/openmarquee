"""P1.2-B HostapdChannelActuator + P1.3 WifiPowerSaveActuator tests.

Cover the pure helpers (channel-substitute, iw output parse) +
the actuators' flows with subprocess + socket hops monkeypatched,
plus one real-socket protocol smoke for the shared `_netctl_send`.
"""

from __future__ import annotations

import contextlib
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from openmarquee.network_supervisor import ChannelFollowDecision
from openmarquee.network_supervisor_actuator import (
    HostapdActuationError,
    HostapdChannelActuator,
    WifiPowerSaveActuationError,
    WifiPowerSaveActuator,
    _netctl_send,
    _parse_iw_dev_info_channel,
    _run_netctl_wifi_powersave_off,
    _substitute_channel,
)

# ============================================================
# _substitute_channel
# ============================================================


class TestSubstituteChannel:
    def test_replaces_existing_channel_line(self):
        conf = "interface=ap0\ndriver=nl80211\nchannel=6\nssid=test\n"
        out = _substitute_channel(conf, 11)
        assert "channel=11" in out
        assert "channel=6" not in out

    def test_handles_whitespace_in_channel_line(self):
        conf = "interface=ap0\nchannel = 6\nssid=test\n"
        out = _substitute_channel(conf, 1)
        assert "channel=1" in out
        assert "channel = 6" not in out

    def test_inserts_before_ssid_when_channel_absent(self):
        conf = "interface=ap0\ndriver=nl80211\nssid=test\n"
        out = _substitute_channel(conf, 6)
        assert "channel=6" in out
        # Inserted before ssid= line.
        chan_idx = out.find("channel=6")
        ssid_idx = out.find("ssid=test")
        assert chan_idx < ssid_idx

    def test_appends_when_neither_present(self):
        conf = "interface=ap0\ndriver=nl80211\n"
        out = _substitute_channel(conf, 6)
        assert "channel=6" in out

    def test_only_changes_top_level_channel_line(self):
        # Defensive: a `channel=` substring inside a comment must not
        # be substituted (the pattern is multiline-anchored).
        conf = "# legacy: channel=6 was the default\ninterface=ap0\nchannel=1\nssid=test\n"
        out = _substitute_channel(conf, 11)
        assert "channel=11" in out
        # The comment should be preserved (pattern requires
        # line-start anchor); only the real channel= line changes.
        assert "# legacy: channel=6" in out


# ============================================================
# _parse_iw_dev_info_channel
# ============================================================


@pytest.mark.parametrize(
    "iw_output,expected",
    [
        ("\tchannel 6 (2437 MHz), width: 20 MHz", 6),
        ("\tchannel 11 (2462 MHz), width: 20 MHz, center1: 2462 MHz", 11),
        ("Interface ap0\n\ttype AP\n\tchannel 1 (2412 MHz)\n", 1),
        # No channel line.
        ("Interface ap0\n\ttype AP\n", None),
        # Empty.
        ("", None),
        # Malformed (no parens).
        ("channel 6", None),
    ],
)
def test_parse_iw_dev_info_channel(iw_output, expected):
    assert _parse_iw_dev_info_channel(iw_output) == expected


# ============================================================
# HostapdChannelActuator — happy path + each failure shape
# ============================================================


@pytest.fixture
def hostapd_conf_file(tmp_path: Path) -> Path:
    p = tmp_path / "hostapd.conf"
    p.write_text("interface=ap0\ndriver=nl80211\nchannel=6\nssid=test\n")
    return p


def _decision(target: int = 11, reason: str = "follow_sta") -> ChannelFollowDecision:
    return ChannelFollowDecision(
        target_channel=target,
        regenerate_needed=True,
        reason=reason,
    )


def _mock_subprocess_run(returncode: int = 0, stderr: bytes = b"", stdout: bytes = b""):
    def _impl(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return _impl


def _netctl_ok_recorder(monkeypatch) -> list[str]:
    """P1.2-B.2: monkeypatch the actuator's netctl-socket call to
    record the payload (the new hostapd.conf bytes) without touching
    a real Unix socket. Returns the list of payloads passed.
    """
    captured: list[str] = []

    def _stub(new_conf, **kwargs):
        captured.append(new_conf)

    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._run_netctl_hostapd_write_and_restart",
        _stub,
    )
    return captured


def _netctl_raising(monkeypatch, exc):
    """Make the netctl call raise the given exception."""

    def _stub(new_conf, **kwargs):
        raise exc

    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._run_netctl_hostapd_write_and_restart",
        _stub,
    )


class TestHostapdChannelActuator:
    def test_happy_path_rewrites_config_and_verifies(self, hostapd_conf_file: Path, monkeypatch):
        """P1.2-B.2: write+restart goes through the netctl socket
        daemon; new hostapd.conf is the payload. Post-verify via
        `iw dev ap0 info`."""
        payloads = _netctl_ok_recorder(monkeypatch)

        def _iw_dispatch(cmd, **kwargs):
            assert cmd[0] == "iw"
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"Interface ap0\n\tchannel 11 (2462 MHz), width: 20 MHz\n",
                stderr=b"",
            )

        monkeypatch.setattr(subprocess, "run", _iw_dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        actuator(_decision(11))
        # The new config (with channel=11 substituted) was the netctl payload.
        assert len(payloads) == 1
        assert "channel=11" in payloads[0]
        assert "channel=6" not in payloads[0]

    def test_raises_when_target_channel_is_none(self, hostapd_conf_file: Path):
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="target_channel is None"):
            actuator(ChannelFollowDecision(target_channel=None, regenerate_needed=True, reason="x"))

    def test_raises_when_conf_path_unreadable(self, tmp_path: Path):
        nonexistent = tmp_path / "missing-hostapd.conf"
        actuator = HostapdChannelActuator(hostapd_conf_path=nonexistent)
        with pytest.raises(HostapdActuationError, match="failed to read"):
            actuator(_decision(11))

    def test_raises_when_netctl_daemon_returns_err(self, hostapd_conf_file: Path, monkeypatch):
        """P1.2-B.2: daemon ERR response (e.g. systemctl restart
        hostapd failed inside the helper) propagates as
        HostapdActuationError."""
        _netctl_raising(
            monkeypatch,
            HostapdActuationError(
                "netctl hostapd-write-and-restart: helper rc=1: hostapd unit failed"
            ),
        )
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="hostapd-write-and-restart"):
            actuator(_decision(11))

    def test_raises_on_post_verify_mismatch(self, hostapd_conf_file: Path, monkeypatch):
        """The load-bearing P1.2-A.1 NIT (P1.2-B BLOCKER): systemctl
        restart returning 0 does NOT guarantee hostapd is beaconing
        on the target channel. iw post-verify catches the mismatch +
        raises so the supervisor doesn't advance _current_ap_channel
        optimistically.
        """
        _netctl_ok_recorder(monkeypatch)

        def _iw_dispatch(cmd, **kwargs):
            assert cmd[0] == "iw"
            # hostapd actually beaconing on ch 6, NOT target ch 11.
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"Interface ap0\n\tchannel 6 (2437 MHz)\n",
                stderr=b"",
            )

        monkeypatch.setattr(subprocess, "run", _iw_dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="post-verify mismatch"):
            actuator(_decision(11))

    def test_raises_when_iw_binary_missing(self, hostapd_conf_file: Path, monkeypatch):
        _netctl_ok_recorder(monkeypatch)

        def _iw_dispatch(cmd, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "iw")

        monkeypatch.setattr(subprocess, "run", _iw_dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="iw binary not found"):
            actuator(_decision(11))

    def test_raises_on_netctl_timeout(self, hostapd_conf_file: Path, monkeypatch):
        """P1.2-B.2: timeout inside the socket call (e.g. daemon
        hung) propagates as HostapdActuationError."""
        _netctl_raising(
            monkeypatch,
            HostapdActuationError("netctl hostapd-write-and-restart: response timed out after 15s"),
        )
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="timed out"):
            actuator(_decision(11))


# ============================================================
# Socket-protocol contract (P1.2-B.2): the new config goes through
# the netctl socket as payload, not via argv. The daemon spawned by
# the systemd template receives subcommand on line 1 + payload
# bytes after.
# ============================================================


def test_actuator_passes_new_conf_as_netctl_payload(hostapd_conf_file, monkeypatch):
    """P1.2-B.2: the actuator's call to
    _run_netctl_hostapd_write_and_restart passes the new conf as
    the first arg (which the helper sends over the socket as the
    payload after the subcommand line). This pins the privilege-
    boundary contract."""
    payloads = _netctl_ok_recorder(monkeypatch)

    def _iw_dispatch(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=b"\tchannel 11 (2462 MHz)\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", _iw_dispatch)
    actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
    actuator(_decision(11))
    assert len(payloads) == 1
    # The payload is a str (the actuator's helper handles the
    # encode-and-send) and contains the substituted channel.
    assert "channel=11" in payloads[0]
    assert "channel=6" not in payloads[0]


# ============================================================
# P1.3 (2026-06-27) WifiPowerSaveActuator + _run_netctl_wifi_powersave_off
# + shared _netctl_send core
# ============================================================


def _netctl_wps_ok_recorder(monkeypatch) -> list[tuple[str, bytes]]:
    """Capture invocations of the shared netctl core for the
    WifiPowerSaveActuator path. Returns the list of (subcommand,
    payload) tuples passed."""
    captured: list[tuple[str, bytes]] = []

    def _stub(subcommand, payload, *, timeout_s, error_cls):
        captured.append((subcommand, payload))

    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._netctl_send",
        _stub,
    )
    return captured


class TestNetctlSendShared:
    """Pins the shared `_netctl_send` contract used by both
    hostapd-write-and-restart and wifi-powersave-off wrappers. The
    happy path uses a real Unix-domain server in a thread so the
    actual wire protocol is exercised (not just the function shape).
    """

    @pytest.fixture
    def fake_server(self):
        """Spin up a one-shot Unix-domain server that captures the
        client's first line + remaining payload, replies with a
        configurable response, and closes.

        Uses a short-prefix tempdir directly (not pytest.tmp_path)
        because AF_UNIX paths are capped at ~104 chars on macOS
        and pytest's nested tmp_path easily blows past that.
        """
        # Short prefix; sun_path on macOS caps at 104.
        sock_dir = Path(tempfile.mkdtemp(prefix="nm-"))
        sock_path = sock_dir / "s"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        srv.listen(1)
        srv.settimeout(5.0)

        received: dict = {"subcommand": None, "payload": None}
        responses = {"reply": b"OK\n"}

        def _serve():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                first, _, rest = data.partition(b"\n")
                received["subcommand"] = first.decode("ascii", errors="replace")
                received["payload"] = rest
                conn.sendall(responses["reply"])

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        try:
            yield sock_path, received, responses, thread
        finally:
            with contextlib.suppress(OSError):
                srv.close()
            thread.join(timeout=2.0)
            with contextlib.suppress(OSError):
                sock_path.unlink()
            with contextlib.suppress(OSError):
                sock_dir.rmdir()

    def test_happy_path_sends_subcommand_and_payload(self, fake_server, monkeypatch):
        sock_path, received, _responses, _thread = fake_server
        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator.NETCTL_SOCKET_PATH",
            str(sock_path),
        )
        _netctl_send(
            "wifi-powersave-off",
            b"",
            timeout_s=5.0,
            error_cls=WifiPowerSaveActuationError,
        )
        assert received["subcommand"] == "wifi-powersave-off"
        assert received["payload"] == b""

    def test_payload_is_forwarded_after_subcommand_line(self, fake_server, monkeypatch):
        sock_path, received, _responses, _thread = fake_server
        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator.NETCTL_SOCKET_PATH",
            str(sock_path),
        )
        _netctl_send(
            "hostapd-write-and-restart",
            b"channel=11\nssid=test\n",
            timeout_s=5.0,
            error_cls=HostapdActuationError,
        )
        assert received["subcommand"] == "hostapd-write-and-restart"
        assert received["payload"] == b"channel=11\nssid=test\n"

    def test_err_response_raises_typed_error(self, fake_server, monkeypatch):
        sock_path, _received, responses, _thread = fake_server
        responses["reply"] = b"ERR daemon-side-failure\n"
        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator.NETCTL_SOCKET_PATH",
            str(sock_path),
        )
        with pytest.raises(WifiPowerSaveActuationError, match="daemon-side-failure"):
            _netctl_send(
                "wifi-powersave-off",
                b"",
                timeout_s=5.0,
                error_cls=WifiPowerSaveActuationError,
            )

    def test_unexpected_response_raises_typed_error(self, fake_server, monkeypatch):
        sock_path, _received, responses, _thread = fake_server
        responses["reply"] = b"NOTOK weird\n"
        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator.NETCTL_SOCKET_PATH",
            str(sock_path),
        )
        with pytest.raises(HostapdActuationError, match="unexpected response"):
            _netctl_send(
                "hostapd-write-and-restart",
                b"x",
                timeout_s=5.0,
                error_cls=HostapdActuationError,
            )

    def test_missing_socket_raises_typed_error(self, monkeypatch):
        """FileNotFoundError on connect is mapped through error_cls.

        Short-prefix tempdir for the same AF_UNIX path-length reason
        as `fake_server` — even the ABSENT path must fit in sun_path.
        """
        sock_dir = Path(tempfile.mkdtemp(prefix="nm-"))
        missing = sock_dir / "nope"
        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator.NETCTL_SOCKET_PATH",
            str(missing),
        )
        try:
            with pytest.raises(WifiPowerSaveActuationError, match="netctl socket not found"):
                _netctl_send(
                    "wifi-powersave-off",
                    b"",
                    timeout_s=1.0,
                    error_cls=WifiPowerSaveActuationError,
                )
        finally:
            with contextlib.suppress(OSError):
                sock_dir.rmdir()


class TestRunNetctlWifiPowersaveOff:
    """The thin wrapper around _netctl_send for the
    wifi-powersave-off subcommand. Pins the call shape so the
    privileged-side ALLOWLIST entry + this wrapper stay in
    lock-step."""

    def test_passes_subcommand_and_empty_payload(self, monkeypatch):
        captured = _netctl_wps_ok_recorder(monkeypatch)
        _run_netctl_wifi_powersave_off()
        assert len(captured) == 1
        subcommand, payload = captured[0]
        assert subcommand == "wifi-powersave-off"
        assert payload == b""

    def test_raises_wifi_power_save_error_on_failure(self, monkeypatch):
        def _stub(subcommand, payload, *, timeout_s, error_cls):
            raise error_cls("simulated daemon failure")

        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator._netctl_send",
            _stub,
        )
        with pytest.raises(WifiPowerSaveActuationError, match="simulated daemon failure"):
            _run_netctl_wifi_powersave_off()


class TestWifiPowerSaveActuator:
    """Wraps `_run_netctl_wifi_powersave_off` as a callable so the
    supervisor's `power_save_actuator` slot accepts it directly."""

    def test_is_callable_no_args(self, monkeypatch):
        captured = _netctl_wps_ok_recorder(monkeypatch)
        actuator = WifiPowerSaveActuator()
        actuator()
        assert len(captured) == 1
        assert captured[0][0] == "wifi-powersave-off"

    def test_propagates_netctl_error(self, monkeypatch):
        def _stub(subcommand, payload, *, timeout_s, error_cls):
            raise error_cls("netctl wifi-powersave-off: helper rc=1: oops")

        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator._netctl_send",
            _stub,
        )
        actuator = WifiPowerSaveActuator()
        with pytest.raises(WifiPowerSaveActuationError, match="helper rc=1"):
            actuator()

    def test_timeout_s_override_threads_through(self, monkeypatch):
        captured: list[float] = []

        def _stub(subcommand, payload, *, timeout_s, error_cls):
            captured.append(timeout_s)

        monkeypatch.setattr(
            "openmarquee.network_supervisor_actuator._netctl_send",
            _stub,
        )
        actuator = WifiPowerSaveActuator(timeout_s=3.5)
        actuator()
        assert captured == [3.5]


# ============================================================
# P1.3: the renamed hostapd-actuator log line MUST start with
# `[network-supervisor]` so QA's grep pattern catches it (spec
# §Diagnostics smoking-gun divergence).
# ============================================================


def _decision_channel(channel: int) -> ChannelFollowDecision:
    return ChannelFollowDecision(
        target_channel=channel,
        regenerate_needed=True,
        reason="follow_sta",
    )


def test_hostapd_actuator_emits_supervisor_tagged_log_line(tmp_path, monkeypatch, caplog):
    """P1.3 spec §Diagnostics: the hostapd-started channel log line
    MUST match `[network-supervisor]`-prefixed format so any
    divergence between the STA channel (logged by apply_sta_freq)
    and the AP channel (logged here) is visible in a single grep."""
    conf = tmp_path / "hostapd.conf"
    conf.write_text("interface=ap0\ndriver=nl80211\nchannel=6\nssid=test\n")

    def _stub_netctl(subcommand, payload, *, timeout_s, error_cls):
        return None

    monkeypatch.setattr(
        "openmarquee.network_supervisor_actuator._netctl_send",
        _stub_netctl,
    )

    def _iw_dispatch(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=b"\tchannel 11 (2462 MHz), width: 20 MHz\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", _iw_dispatch)
    actuator = HostapdChannelActuator(hostapd_conf_path=conf)
    with caplog.at_level("INFO", logger="openmarquee.network_supervisor_actuator"):
        actuator(_decision_channel(11))

    matched = [rec.getMessage() for rec in caplog.records if "hostapd-started" in rec.getMessage()]
    assert len(matched) == 1, f"expected one hostapd-started line, got: {matched}"
    line = matched[0]
    assert line.startswith("[network-supervisor]"), line
    assert "source=channel_follow" in line
    assert "channel=11" in line
    assert "iface=ap0" in line
    assert "verified=true" in line
