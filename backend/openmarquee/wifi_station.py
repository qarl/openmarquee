"""Wifi station-mode applier (NetworkManager / nmcli backend).

When the operator submits home-wifi credentials via the settings UI,
this module drives `nmcli` to create or update the wlan0 connection
profile and bring the Pi onto the home network. NM persists the
profile across reboots so a subsequent power-cycle re-associates
without re-running the applier.

History: 771345b first shipped a `wpa_supplicant@wlan0` -> systemctl
restart implementation; the dev-Pi smoke surfaced that Pi OS Lite
trixie defaults to NetworkManager (not dhcpcd+wpa_supplicant), so
the systemctl path was a dead end. This module is the rewrite.

Design choices:

- **NetworkManager is the actual stack.** Pi OS Lite trixie installs
  NM by default + uses singleton `wpa_supplicant.service` as NM's
  internal backend (not the templated `wpa_supplicant@wlan0` unit).
  All operations go through `nmcli` so NM stays in charge of wlan0;
  nothing here touches `/etc/wpa_supplicant/*` or systemctl-bounces
  any unit.

- **AP stays up.** This module uses CONNECTION-level nmcli
  operations (`device wifi connect`, `connection delete`), NOT
  device-level operations (`device disconnect`). The latter would
  free wlan0 entirely + could cascade into ap0 since both share
  the BCM43438 radio. Connection-level ops only touch the
  specific connection profile + its associated device, leaving
  ap0 (a separate `iw add ... type __ap` virtual interface,
  managed by `openmarquee-ap0.service`) untouched. The
  AP-coexistence-with-NM question is task #99, scoped separately.

- **Idempotent on re-submit.** Before issuing `nmcli connect`, query
  `nmcli -t -f NAME,DEVICE connection show --active` for wlan0's
  current connection name. If it matches the requested SSID + the
  device state is "100 (connected)", we no-op + return "connected"
  immediately. Same creds re-submitted means zero subprocess
  cost beyond the two short status queries.

- **Per-interface lock.** `_APPLY_LOCK` serializes apply() calls so
  two operator-mashed Saves don't race nmcli against itself.

- **Sudo scope is narrow.** Exactly four nmcli subcommands:
  `device wifi connect *`, `connection delete *`, `connection up *`,
  `connection down *`. Read-only queries (`nmcli device status`,
  `nmcli connection show`) run as the openmarquee user without
  sudo — NM grants read access to the `netdev` group by default
  on Pi OS / Debian.

- **nmcli exit code is authoritative.** nmcli returns non-zero on
  wrong-password, ssid-not-in-range, no-NM-running. We capture the
  exit code + stderr, set wifi_station_state="failed" with the
  stderr as the detail. No "claim success on subprocess error".

The apply path runs in a background thread (kicked from
`api_settings.py`'s PUT/PATCH handlers) so the HTTP response
returns immediately. The UI polls
`GET /api/settings/wifi-station-state` for the live status.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Absolute path matches the sudoers Cmnd spec literally. On Pi OS
# trixie nmcli installs to /usr/bin/nmcli; verified via
# `which nmcli` on the dev Pi.
_NMCLI_BIN = "/usr/bin/nmcli"

# wlan0 is the BCM43438 radio's primary interface on Pi Zero 2 W;
# the AP runs on ap0 (separate virtual interface). Hardcoded for
# now; future multi-radio devices would parameterize.
_STATION_IFNAME = "wlan0"


# --- Status singleton -------------------------------------------------------


@dataclasses.dataclass
class WifiStationState:
    """In-memory status surface for the UI to poll.

    `state` is monotonic within a single attempt:
        idle -> connecting -> (connected | failed)

    A subsequent apply() resets it to `connecting` again. The model
    is intentionally simple -- no history; the UI polls + displays
    the current value.
    """

    state: str = "idle"  # idle | connecting | connected | failed | disabled
    detail: Optional[str] = None  # human-readable explanation
    ssid: Optional[str] = None  # which network we're connected/connecting to


_STATE = WifiStationState()
_STATE_LOCK = threading.Lock()  # protects _STATE reads + writes
_APPLY_LOCK = threading.Lock()  # serializes apply() calls (per-interface)


def current_state() -> WifiStationState:
    """Snapshot of the current state. Safe to call from any thread."""
    with _STATE_LOCK:
        return dataclasses.replace(_STATE)


def _set_state(state: str, detail: Optional[str] = None, ssid: Optional[str] = None) -> None:
    with _STATE_LOCK:
        _STATE.state = state
        _STATE.detail = detail
        _STATE.ssid = ssid


# --- nmcli shellouts (overridable for tests) --------------------------------


@dataclasses.dataclass
class _NmcliResult:
    returncode: int
    stdout: str
    stderr: str


def _run_nmcli(args: list[str], *, sudo: bool = False, timeout: int = 30) -> _NmcliResult:
    """Run nmcli with the given args. `sudo=True` prepends `sudo -n`
    (NOPASSWD; failure to grant raises a CalledProcessError-equivalent
    via the returncode). Returns the captured result regardless of
    exit code — callers inspect `returncode` to decide success."""
    cmd = ([_NMCLI_BIN] + args)
    if sudo:
        cmd = ["sudo", "-n"] + cmd
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _NmcliResult(
            returncode=124,
            stdout=exc.stdout.decode() if exc.stdout else "",
            stderr=f"nmcli timed out after {timeout}s",
        )
    except FileNotFoundError:
        return _NmcliResult(
            returncode=127,
            stdout="",
            stderr=f"nmcli not found at {_NMCLI_BIN}",
        )
    return _NmcliResult(
        returncode=result.returncode,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
    )


# Module-level handles so tests can monkey-patch them.
nmcli_runner: Callable[..., _NmcliResult] = _run_nmcli


def _active_connection_for_device() -> Optional[str]:
    """Return the NAME of the active connection on wlan0, or None if
    wlan0 has no active connection. Uses the terse output format so
    we can parse without fancy nmcli output gymnastics.

    `nmcli -t -f NAME,DEVICE connection show --active`
    emits lines like:
        Wired connection 1:eth0
        pikazo:wlan0
    """
    result = nmcli_runner(
        ["-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
        sudo=False,
    )
    if result.returncode != 0:
        log.warning("nmcli active-connection query failed: %s", result.stderr)
        return None
    for line in result.stdout.splitlines():
        # nmcli's terse format uses ':' as separator; escape with '\:'
        # in field values. SSIDs containing ':' are exotic enough we
        # split on the LAST ':' (the DEVICE column).
        if ":" not in line:
            continue
        name, device = line.rsplit(":", 1)
        if device.strip() == _STATION_IFNAME:
            return name.strip()
    return None


def _device_state() -> str:
    """Return the wlan0 device state string from nmcli, e.g.
    '100 (connected)' / '30 (disconnected)' / '20 (unavailable)'.

    Returns '' if the query fails or wlan0 isn't recognized.
    """
    result = nmcli_runner(
        ["-t", "-f", "GENERAL.STATE", "device", "show", _STATION_IFNAME],
        sudo=False,
    )
    if result.returncode != 0:
        return ""
    # Output: "GENERAL.STATE:100 (connected)"
    for line in result.stdout.splitlines():
        if ":" in line and line.startswith("GENERAL.STATE"):
            return line.split(":", 1)[1].strip()
    return ""


def _is_device_connected() -> bool:
    """True if `nmcli device show wlan0` reports the connected state
    (numeric 100). Strict match -- intermediate states like
    'connecting (prepare)' do NOT satisfy this."""
    return _device_state().startswith("100 ")


def _wifi_connect(ssid: str, password: str) -> _NmcliResult:
    """`nmcli device wifi connect "<ssid>" password "<pw>" ifname wlan0`.

    NM creates a new connection profile named <ssid> (or replaces an
    existing one with the same SSID name) + brings it up. Returns
    the nmcli result; callers check returncode for success.
    """
    return nmcli_runner(
        [
            "device", "wifi", "connect", ssid,
            "password", password,
            "ifname", _STATION_IFNAME,
        ],
        sudo=True,
        timeout=45,
    )


def _connection_delete(name: str) -> _NmcliResult:
    """`nmcli connection delete "<name>"`. Removes the profile so a
    follow-up connect to a different SSID doesn't tangle with a
    stale auto-connect."""
    return nmcli_runner(
        ["connection", "delete", name],
        sudo=True,
        timeout=15,
    )


def _poll_for_connection(
    target_ssid: str,
    timeout_sec: int = 30,
    poll_interval_sec: float = 1.0,
) -> bool:
    """Loop until wlan0's active connection matches `target_ssid` AND
    the device state is fully connected. Returns False on timeout."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        active = _active_connection_for_device()
        if active == target_ssid and _is_device_connected():
            return True
        time.sleep(poll_interval_sec)
    return False


# --- Public apply entry points ----------------------------------------------


def apply_disabled() -> None:
    """Operator turned station-mode off: bring down + delete any
    openMarquee-managed connection on wlan0 so the Pi reverts to
    AP-only mode. AP runs on ap0 (separate virtual interface) and
    is untouched.

    We use `connection delete` (not `device disconnect`) because the
    latter frees the whole wlan0 device + could cascade into the
    AP's ap0 (shared radio). Profile deletion only frees the
    specific connection; wlan0 stays in NM's hand.
    """
    with _APPLY_LOCK:
        log.info("wifi station: disabling")
        active = _active_connection_for_device()
        if active:
            result = _connection_delete(active)
            if result.returncode != 0:
                log.warning(
                    "wifi station: connection delete %r failed: %s",
                    active, result.stderr,
                )
        _set_state("disabled", detail=None, ssid=None)


def apply_enabled(ssid: str, password: str, poll_timeout_sec: int = 30) -> bool:
    """Bring wlan0 onto the given home WiFi via nmcli. Idempotent:
    if wlan0 is already associated with `ssid`, the function
    no-ops + returns True. Otherwise:

    1. Set state -> 'connecting'.
    2. If wlan0 is on a different active connection, `nmcli
       connection delete` it (so the new connect doesn't auto-fall
       back to the old profile on failure).
    3. `nmcli device wifi connect` to bring up the new profile.
    4. Poll device-state for the connected (100) state, up to
       `poll_timeout_sec`.
    5. Set state -> 'connected' on success, 'failed' on
       nmcli-non-zero OR poll-timeout.

    Returns True on success, False on any failure. The caller
    (apply_in_background) doesn't actually consume the return value
    -- the in-memory _STATE singleton is the contract surface.
    """
    with _APPLY_LOCK:
        # Idempotent short-circuit.
        active = _active_connection_for_device()
        if active == ssid and _is_device_connected():
            log.info("wifi station: already connected to ssid=%r; no-op", ssid)
            _set_state("connected", detail=None, ssid=ssid)
            return True

        _set_state("connecting", detail=None, ssid=ssid)

        # If we're connected to a different SSID, delete that
        # connection profile first so nmcli's auto-fallback doesn't
        # pick it up if the new connect fails.
        if active and active != ssid:
            log.info(
                "wifi station: removing prior connection %r before switching to %r",
                active, ssid,
            )
            result = _connection_delete(active)
            if result.returncode != 0:
                log.warning(
                    "wifi station: failed to delete %r (continuing anyway): %s",
                    active, result.stderr,
                )

        # Connect. nmcli returns 0 on success, non-zero on:
        # password wrong, ssid not in range, NM stopped, etc.
        log.info("wifi station: connecting to ssid=%r", ssid)
        result = _wifi_connect(ssid, password)
        if result.returncode != 0:
            # nmcli's stderr is usually a single useful line like
            # "Error: Connection activation failed: (7) Secrets were
            # required, but not provided." -- pass through verbatim
            # (no creds in the stderr).
            detail = result.stderr.strip() or f"nmcli exited {result.returncode}"
            log.error("wifi station: nmcli connect failed: %s", detail)
            _set_state("failed", detail=detail, ssid=ssid)
            return False

        # Poll. nmcli's `device wifi connect` is largely synchronous
        # (it waits for association before returning 0), but the
        # device's GENERAL.STATE can lag a beat. The poll is cheap
        # insurance.
        if _poll_for_connection(ssid, timeout_sec=poll_timeout_sec):
            _set_state("connected", detail=None, ssid=ssid)
            log.info("wifi station: associated with ssid=%r", ssid)
            return True
        _set_state(
            "failed",
            detail=f"no association within {poll_timeout_sec}s",
            ssid=ssid,
        )
        log.warning(
            "wifi station: timed out waiting for connection to ssid=%r", ssid,
        )
        return False


def apply_in_background(
    enabled: bool,
    ssid: Optional[str],
    password: Optional[str],
) -> threading.Thread:
    """Dispatch the apply() to a background thread so the HTTP handler
    can return immediately. Returns the started thread so callers
    (mostly tests) can join() if they need to await completion.
    """
    if not enabled:
        thread = threading.Thread(target=apply_disabled, daemon=True)
    else:
        if not ssid or not password:
            # Settings validator already rejects this combination, but
            # belt-and-braces: don't pass None into apply_enabled.
            log.warning(
                "wifi station: apply_in_background called with enabled=true "
                "but missing ssid/password; no-op"
            )
            _set_state(
                "failed",
                detail="missing ssid or password",
                ssid=ssid,
            )
            thread = threading.Thread(target=lambda: None, daemon=True)
            thread.start()
            return thread
        thread = threading.Thread(
            target=apply_enabled,
            args=(ssid, password),
            daemon=True,
        )
    thread.start()
    return thread


def has_settings_changed(
    prev_enabled: bool,
    prev_ssid: Optional[str],
    prev_password: Optional[str],
    new_enabled: bool,
    new_ssid: Optional[str],
    new_password: Optional[str],
) -> bool:
    """Did the wifi_station_* surface change in a way that requires an
    apply()? Helper for the api_settings PUT handler.

    - Toggle change in either direction -> True.
    - Stays enabled but ssid or password changed -> True.
    - Stays disabled -> False (no work needed).
    - Stays enabled with same creds -> False (apply would idempotent-
      short-circuit anyway, but skipping the thread spawn is cleaner).
    """
    if prev_enabled != new_enabled:
        return True
    if not new_enabled:
        return False
    return (prev_ssid != new_ssid) or (prev_password != new_password)
