"""P1.2-B active channel-follow actuator.

Replaces P1.2-A's observe-only `_default_actuator` (which only
logged) with a real hostapd-config rewrite + systemctl restart +
post-verify via `iw dev ap0 info`. Per the P1.2-A.1 actuator
contract: return normally on success, raise on failure. The
post-verify NIT promoted from sacred review of P1.2-A.1 is the
critical correctness piece — `systemctl restart hostapd.service`
returning 0 does NOT guarantee hostapd is actually beaconing on
the target channel.

P1.2-B.1: the hostapd.conf rewrite + systemctl restart go through
the privileged netctl helper (sudo grant in
system/openmarquee-sudoers + script in system/openmarquee-netctl).
The backend's NoNewPrivileges + ProtectSystem=strict makes direct
file writes to /etc and systemctl invocations fail otherwise.

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

import contextlib
import logging
import re
import socket as _socket
import subprocess
from pathlib import Path

from openmarquee.network_supervisor import ChannelFollowDecision

log = logging.getLogger(__name__)

DEFAULT_HOSTAPD_CONF = Path("/etc/hostapd/hostapd.conf")
DEFAULT_AP_IFACE = "ap0"
NETCTL_SOCKET_PATH = "/run/openmarquee/netctl.sock"
NETCTL_TIMEOUT_S = 15.0
IW_VERIFY_TIMEOUT_S = 5.0


class HostapdActuationError(RuntimeError):
    """Raised when the actuator fails any step. Caller (the
    supervisor's apply_sta_freq) treats this as "did NOT advance
    current_ap_channel"; the next poll retries."""


class WifiPowerSaveActuationError(RuntimeError):
    """Raised when the supervisor's STA_ASSOCIATED-driven
    re-fire of openmarquee-wifi-powersave-off.sh fails. Caller
    (the supervisor's apply_event) emits a diagnostic warn line
    and continues; the next reassociation will retry."""


def _netctl_send(
    subcommand: str,
    payload: bytes,
    *,
    timeout_s: float,
    error_cls: type[RuntimeError],
) -> None:
    """Privilege-boundary invocation via the socket-activated root
    daemon (P1.2-B.2). Shared core for every netctl call: pipes
    `<subcommand>\\n<payload>` into the daemon and asserts a
    single-line OK response.

    Parametric on `error_cls` so each caller raises its own typed
    exception (HostapdActuationError, WifiPowerSaveActuationError,
    ...) without sharing a parent. Tests monkeypatch _netctl_send
    once and exercise both call sites.
    """
    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        try:
            sock.connect(NETCTL_SOCKET_PATH)
        except FileNotFoundError as e:
            raise error_cls(f"netctl socket not found at {NETCTL_SOCKET_PATH}: {e}") from e
        except OSError as e:
            raise error_cls(f"netctl socket connect failed: {e}") from e
        try:
            sock.sendall(subcommand.encode("ascii") + b"\n")
            if payload:
                sock.sendall(payload)
            sock.shutdown(_socket.SHUT_WR)
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: send failed ({e})") from e
        chunks: list[bytes] = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(c) for c in chunks) > 8192:
                    break
        except TimeoutError as e:
            raise error_cls(
                f"netctl {subcommand}: response timed out after {timeout_s:.0f}s"
            ) from e
        except OSError as e:
            raise error_cls(f"netctl {subcommand}: recv failed ({e})") from e
        response = b"".join(chunks).decode("utf-8", errors="replace").rstrip("\n")
        if response == "OK":
            return
        if response.startswith("ERR "):
            raise error_cls(f"netctl {subcommand}: {response[4:]}")
        if not response:
            raise error_cls(f"netctl {subcommand}: empty response (daemon closed before reply?)")
        raise error_cls(f"netctl {subcommand}: unexpected response {response!r}")
    finally:
        with contextlib.suppress(Exception):
            sock.close()


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


def _run_netctl_hostapd_write_and_restart(
    new_conf: str,
    *,
    timeout_s: float = NETCTL_TIMEOUT_S,
) -> None:
    """P1.2-B.2 hostapd-write-and-restart wrapper. Delegates to
    `_netctl_send` for the socket protocol; raises
    HostapdActuationError on any failure.

    Sync (blocking) to match the actuator's blocking semantics
    inside the supervisor's sync apply_sta_freq. The actuator
    fires only on STA frequency CHANGE (boot association + rare
    router CSA) so ~5s of blocking once per change is acceptable.
    """
    _netctl_send(
        "hostapd-write-and-restart",
        new_conf.encode("utf-8"),
        timeout_s=timeout_s,
        error_cls=HostapdActuationError,
    )


def _run_netctl_wifi_powersave_off(
    *,
    timeout_s: float = NETCTL_TIMEOUT_S,
) -> None:
    """P1.3 (2026-06-27) re-fire the boot-oneshot's power-save-off
    logic on every STA reassociation. brcmfmac resets power_save on
    reassoc per spec §A#2; the supervisor wires this into its
    STA_ASSOCIATED handler. The netctl daemon runs the existing
    /opt/openmarquee/system/openmarquee-wifi-powersave-off.sh
    verbatim so iface coverage + logger tag stay in lock-step with
    the boot oneshot.

    Raises WifiPowerSaveActuationError on any failure; the caller
    (supervisor.apply_event) emits a warn diagnostic and continues.
    """
    _netctl_send(
        "wifi-powersave-off",
        b"",
        timeout_s=timeout_s,
        error_cls=WifiPowerSaveActuationError,
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

        # 1. Read current hostapd.conf. Read is permitted under
        #    ProtectSystem=strict (it only blocks writes); we render
        #    the new content in-process then hand both off to netctl.
        try:
            current_text = self.hostapd_conf_path.read_text()
        except OSError as e:
            raise HostapdActuationError(f"failed to read {self.hostapd_conf_path}: {e}") from e

        # 2. Substitute channel.
        new_text = _substitute_channel(current_text, decision.target_channel)

        # 3+4. Atomic write + systemctl restart through the privileged
        #      netctl helper (P1.2-B.1). The backend can't write /etc
        #      directly under ProtectSystem=strict; sudo to netctl
        #      crosses the privilege boundary.
        _run_netctl_hostapd_write_and_restart(new_text)

        # 5. Post-verify via iw — the load-bearing reality check.
        iw_output = _iw_dev_info(self.ap_iface)
        actual_channel = _parse_iw_dev_info_channel(iw_output)
        if actual_channel != decision.target_channel:
            raise HostapdActuationError(
                f"post-verify mismatch on {self.ap_iface}: target="
                f"{decision.target_channel} actual={actual_channel}; "
                "hostapd restarted but radio not on target channel"
            )
        # P1.3 (2026-06-27): named "hostapd-started" log line in the
        # spec's smoking-gun format. QA's grep pattern
        # `journalctl -u openmarquee-backend | grep
        # '\[network-supervisor\]'` now surfaces both STA-channel and
        # AP-channel lines in the same stream; any divergence between
        # the channel reported here and the STA channel logged by the
        # supervisor's apply_sta_freq IS the channel mismatch the
        # spec §Diagnostics calls out.
        log.info(
            "[network-supervisor] source=channel_follow severity=info "
            "message=hostapd-started channel=%d iface=%s verified=true",
            decision.target_channel,
            self.ap_iface,
        )


class WifiPowerSaveActuator:
    """P1.3 (2026-06-27) STA-reassociation power-save-off actuator.

    The boot service openmarquee-wifi-powersave-off.service sets
    `iw dev wlan0 set power_save off` at boot. brcmfmac resets the
    setting on every reassociation (spec §A#2 — "It can be reset
    by reassociation. Make this a boot-time service, not a
    one-off."), so the supervisor wires a callable here onto its
    STA_ASSOCIATED handler.

    Default-active (unlike HostapdChannelActuator which is gated on
    take-over). Rationale: power_save=off is idempotent + non-
    destructive + the boot oneshot already enforces it on the same
    box, so re-firing on reassoc doesn't fight anything. Falls back
    silently if the netctl socket isn't present (e.g. dev hosts
    without the daemon installed).

    Instance is callable so it slots directly into the supervisor's
    `power_save_actuator: Callable[[], None]` contract.
    """

    def __init__(self, *, timeout_s: float = NETCTL_TIMEOUT_S):
        self.timeout_s = timeout_s

    def __call__(self) -> None:
        _run_netctl_wifi_powersave_off(timeout_s=self.timeout_s)
