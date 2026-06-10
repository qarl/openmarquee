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


class TestHostapdChannelActuator:
    def test_happy_path_rewrites_config_and_verifies(self, hostapd_conf_file: Path, monkeypatch):
        calls = []

        def _systemctl_stub(cmd, **kwargs):
            calls.append(("systemctl", cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        def _iw_stub(cmd, **kwargs):
            calls.append(("iw", cmd))
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=b"Interface ap0\n\tchannel 11 (2462 MHz), width: 20 MHz\n",
                stderr=b"",
            )

        # Two subprocess sites in the actuator module: systemctl_restart + iw_dev_info.
        def _dispatch(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return _systemctl_stub(cmd, **kwargs)
            if cmd[0] == "iw":
                return _iw_stub(cmd, **kwargs)
            raise AssertionError(f"unexpected subprocess: {cmd}")

        monkeypatch.setattr(subprocess, "run", _dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        actuator(_decision(11))
        # Config was rewritten.
        assert "channel=11" in hostapd_conf_file.read_text()
        assert "channel=6" not in hostapd_conf_file.read_text()
        # Subprocess hops in correct order.
        assert calls[0][0] == "systemctl"
        assert calls[1][0] == "iw"

    def test_raises_when_target_channel_is_none(self, hostapd_conf_file: Path):
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="target_channel is None"):
            actuator(ChannelFollowDecision(target_channel=None, regenerate_needed=True, reason="x"))

    def test_raises_when_conf_path_unreadable(self, tmp_path: Path):
        nonexistent = tmp_path / "missing-hostapd.conf"
        actuator = HostapdChannelActuator(hostapd_conf_path=nonexistent)
        with pytest.raises(HostapdActuationError, match="failed to read"):
            actuator(_decision(11))

    def test_raises_when_systemctl_restart_fails(self, hostapd_conf_file: Path, monkeypatch):
        def _dispatch(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"Unit not found")
            raise AssertionError(f"unexpected: {cmd}")

        monkeypatch.setattr(subprocess, "run", _dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="systemctl restart"):
            actuator(_decision(11))

    def test_raises_on_post_verify_mismatch(self, hostapd_conf_file: Path, monkeypatch):
        """The load-bearing P1.2-A.1 NIT promoted to P1.2-B BLOCKER:
        systemctl restart returning 0 does NOT guarantee hostapd is
        beaconing on the target channel. iw post-verify catches the
        mismatch + raises so the supervisor doesn't advance
        _current_ap_channel optimistically.
        """

        def _dispatch(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            if cmd[0] == "iw":
                # hostapd actually beaconing on ch 6, NOT target ch 11.
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=b"Interface ap0\n\tchannel 6 (2437 MHz)\n",
                    stderr=b"",
                )
            raise AssertionError(f"unexpected: {cmd}")

        monkeypatch.setattr(subprocess, "run", _dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="post-verify mismatch"):
            actuator(_decision(11))

    def test_raises_when_iw_binary_missing(self, hostapd_conf_file: Path, monkeypatch):
        def _dispatch(cmd, **kwargs):
            if cmd[0] == "systemctl":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            if cmd[0] == "iw":
                raise FileNotFoundError(2, "No such file or directory", "iw")
            raise AssertionError(f"unexpected: {cmd}")

        monkeypatch.setattr(subprocess, "run", _dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="iw binary not found"):
            actuator(_decision(11))

    def test_raises_on_systemctl_timeout(self, hostapd_conf_file: Path, monkeypatch):
        def _dispatch(cmd, **kwargs):
            if cmd[0] == "systemctl":
                raise subprocess.TimeoutExpired(cmd, timeout=kwargs.get("timeout", 15))
            raise AssertionError(f"unexpected: {cmd}")

        monkeypatch.setattr(subprocess, "run", _dispatch)
        actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
        with pytest.raises(HostapdActuationError, match="timed out"):
            actuator(_decision(11))


# ============================================================
# Atomicity: write happens via tempfile + rename.
# ============================================================


def test_actuator_uses_atomic_replace_on_write(hostapd_conf_file, monkeypatch):
    """Defensive: a crash mid-write must NOT leave hostapd.conf
    truncated. The actuator's atomic write (tempfile + os.replace)
    guarantees the file is either old-state or fully-new-state at
    every moment readable by other processes."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _mock_subprocess_run(
            returncode=0,
            stdout=b"\tchannel 11 (2462 MHz)\n",
        ),
    )
    original = hostapd_conf_file.read_text()
    actuator = HostapdChannelActuator(hostapd_conf_path=hostapd_conf_file)
    actuator(_decision(11))
    new = hostapd_conf_file.read_text()
    assert new != original
    assert "channel=11" in new
    # No stray tempfile left behind in the same dir.
    tmps = list(hostapd_conf_file.parent.glob(".*.tmp"))
    assert tmps == [], f"tempfile leak: {tmps}"
