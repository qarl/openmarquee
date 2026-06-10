"""P1.2-B active channel-follow actuator.

Replaces P1.2-A's observe-only `_default_actuator` (which only
logged) with a real hostapd-config rewrite + systemctl restart +
post-verify via `iw dev ap0 info`. Per the P1.2-A.1 actuator
contract: return normally on success, raise on failure. The
post-verify NIT promoted from sacred review of P1.2-A.1 is the
critical correctness piece — `systemctl restart hostapd.service`
returning 0 does NOT guarantee hostapd is actually beaconing on
the target channel.

Sync subprocess: the actuator fires from inside the supervisor's
sync apply_sta_freq, which is called from the observe loop's
async tick. We use blocking `subprocess.run` because:
  1. apply_sta_freq is sync (changing it to async would be a
     larger refactor touching the entire supervisor API + tests),
  2. The actuator fires only on STA frequency CHANGE — boot
     association + rare router CSA. ~5s of blocking once per
     change is acceptable.

Cross-platform safety: on Mac dev the actuator never runs (the
supervisor only installs it after a successful take-over flip;
the take-over orchestrator checks /etc/NetworkManager existence
first). For unit testing, the subprocess hops are run through
small wrappers tests monkeypatch.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

from openmarquee.network_supervisor import ChannelFollowDecision

log = logging.getLogger(__name__)

DEFAULT_HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")
DEFAULT_AP_IFACE = "ap0"
SYSTEMCTL_TIMEOUT_S = 15.0
IW_VERIFY_TIMEOUT_S = 5.0


class HostapdActuationError(RuntimeError):
    """Raised when the actuator fails any step. Caller (the
    supervisor's apply_sta_freq) treats this as "did NOT advance
    current_ap_channel"; the next poll retries."""


def _substitute_channel(conf_text: str, target_channel: int) -> str:
    """Pure substitution: replace the `channel=<N>` line in a hostapd
    config with `channel=<target_channel>`. If no `channel=` line is
    present, append one before the `ssid=` line (defensive — a
    legitimate hostapd.conf always has channel set).

    Pure function, testable on every host.
    """
    pattern = re.compile(r"^channel\s*=\s*\d+\s*$", re.MULTILINE)
    new_line = f"channel={target_channel}"
    if pattern.search(conf_text):
        return pattern.sub(new_line, conf_text)
    ssid_match = re.search(r"^ssid\s*=", conf_text, re.MULTILINE)
    if ssid_match:
        idx = ssid_match.start()
        return conf_text[:idx] + new_line + "\n" + conf_text[idx:]
    return conf_text + "\n" + new_line + "\n"


def _parse_iw_dev_info_channel(iw_output: str) -> int | None:
    """Extract the channel number from `iw dev <iface> info` output.

    Canonical line: `channel 6 (2437 MHz), width: 20 MHz, ...`

    Pure function, testable.
    """
    match = re.search(r"channel\s+(\d+)\s*\(", iw_output)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _systemctl_restart(unit: str, *, timeout_s: float = SYSTEMCTL_TIMEOUT_S) -> None:
    """Run `systemctl restart <unit>` (blocking) with a timeout.
    Raises HostapdActuationError on non-zero exit / timeout / spawn
    failure."""
    try:
        result = subprocess.run(
            ["systemctl", "restart", unit],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise HostapdActuationError(f"systemctl binary not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise HostapdActuationError(
            f"systemctl restart {unit} timed out after {timeout_s:.0f}s"
        ) from e
    if result.returncode != 0:
        raise HostapdActuationError(
            f"systemctl restart {unit} failed (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')!r}"
        )


def _iw_dev_info(iface: str, *, timeout_s: float = IW_VERIFY_TIMEOUT_S) -> str:
    """Run `iw dev <iface> info` + return stdout. Raises
    HostapdActuationError on failure."""
    try:
        result = subprocess.run(
            ["iw", "dev", iface, "info"],
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise HostapdActuationError(f"iw binary not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise HostapdActuationError(f"iw dev {iface} info timed out after {timeout_s:.0f}s") from e
    if result.returncode != 0:
        raise HostapdActuationError(
            f"iw dev {iface} info failed (rc={result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')!r}"
        )
    return result.stdout.decode("utf-8", errors="replace")


class HostapdChannelActuator:
    """Active channel-follow actuator.

    Flow on each call:
      1. Read current hostapd.conf
      2. Substitute `channel=<N>` with the decision's target channel
      3. Atomic write via tempfile + os.replace
      4. systemctl restart hostapd.service
      5. POST-VERIFY: `iw dev ap0 info` -> parse channel -> match target
      6. Raise HostapdActuationError on any failure (caller does
         not advance _current_ap_channel; next poll retries)

    The post-verify (step 5) is the P1.2-A.1 sacred-review NIT
    promoted to load-bearing for P1.2-B per QA dispatch 2026-06-10.

    Instance is callable so it slots directly into the supervisor's
    `channel_follow_actuator: Callable[[ChannelFollowDecision], None]`
    contract.
    """

    def __init__(
        self,
        hostapd_conf_path: Path = DEFAULT_HOSTAPD_CONF,
        ap_iface: str = DEFAULT_AP_IFACE,
    ):
        self.hostapd_conf_path = hostapd_conf_path
        self.ap_iface = ap_iface

    def __call__(self, decision: ChannelFollowDecision) -> None:
        if decision.target_channel is None:
            raise HostapdActuationError(
                f"decision.target_channel is None (reason={decision.reason}); refusing"
            )

        # 1. Read current hostapd.conf.
        try:
            current_text = self.hostapd_conf_path.read_text()
        except OSError as e:
            raise HostapdActuationError(f"failed to read {self.hostapd_conf_path}: {e}") from e

        # 2. Substitute channel.
        new_text = _substitute_channel(current_text, decision.target_channel)

        # 3. Atomic write: tempfile in same dir + os.replace.
        try:
            tmp_dir = self.hostapd_conf_path.parent
            tmp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=str(tmp_dir),
                delete=False,
                prefix=".",
                suffix=".hostapd.conf.tmp",
            ) as tmp:
                tmp.write(new_text)
                tmp_name = tmp.name
            os.replace(tmp_name, self.hostapd_conf_path)
        except OSError as e:
            raise HostapdActuationError(f"failed to write {self.hostapd_conf_path}: {e}") from e

        # 4. Restart hostapd.
        _systemctl_restart("hostapd.service")

        # 5. Post-verify via iw — the load-bearing reality check.
        iw_output = _iw_dev_info(self.ap_iface)
        actual_channel = _parse_iw_dev_info_channel(iw_output)
        if actual_channel != decision.target_channel:
            raise HostapdActuationError(
                f"post-verify mismatch on {self.ap_iface}: target="
                f"{decision.target_channel} actual={actual_channel}; "
                "hostapd restarted but radio not on target channel"
            )
        log.info(
            "hostapd-actuator: channel=%d verified on %s",
            decision.target_channel,
            self.ap_iface,
        )
