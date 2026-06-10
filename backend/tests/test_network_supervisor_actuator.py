"""P1.2-B HostapdChannelActuator tests.

Cover the pure helpers (channel-substitute, iw output parse) +
the actuator's flow with subprocess hops monkeypatched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openmarquee.network_supervisor import ChannelFollowDecision
from openmarquee.network_supervisor_actuator import (
    HostapdActuationError,
    HostapdChannelActuator,
    _parse_iw_dev_info_channel,
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
