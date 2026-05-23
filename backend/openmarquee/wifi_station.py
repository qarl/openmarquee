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

- **Sudo scope is narrow.** Exactly two nmcli subcommands:
  `device wifi connect *` and `connection delete *`. Read-only
  queries (`nmcli device status`, `nmcli connection show`) AND
  the pre-connect rescan (`nmcli device wifi rescan ifname
  wlan0`, per 0575572) run as the openmarquee user without
  sudo — NM grants read + scan access to the `netdev` group by
  default on Pi OS / Debian.

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
import subprocess
import threading
import time
from collections.abc import Callable

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
        idle -> rescanning -> connecting -> (connected | failed)

    Fast-fail path skips rescanning/connecting:
        idle -> failed (when wlan0 radio reports state 20=unavailable)

    A subsequent apply() resets it to `rescanning` again. The model
    is intentionally simple -- no history; the UI polls + displays
    the current value.
    """

    state: str = "idle"
    # idle | rescanning | connecting | connected | failed | disabled
    detail: str | None = None  # human-readable explanation
    ssid: str | None = None  # which network we're connected/connecting to


_STATE = WifiStationState()
_STATE_LOCK = threading.Lock()  # protects _STATE reads + writes
_APPLY_LOCK = threading.Lock()  # serializes apply() calls (per-interface)


def current_state() -> WifiStationState:
    """Snapshot of the current state. Safe to call from any thread."""
    with _STATE_LOCK:
        return dataclasses.replace(_STATE)


def _set_state(state: str, detail: str | None = None, ssid: str | None = None) -> None:
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
    cmd = [_NMCLI_BIN] + args
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


def _active_connection_for_device() -> str | None:
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


def _device_state_code(state: str) -> int | None:
    """Pull the numeric leading code from a device-state string like
    '100 (connected)' / '20 (unavailable)'. Returns None on parse
    failure (covers empty + unrecognized strings). Numeric codes are
    NM's stable contract; the parenthetical text is human prose."""
    if not state:
        return None
    head = state.split(" ", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _wifi_rescan() -> _NmcliResult:
    """`nmcli device wifi rescan ifname wlan0`. Best-effort scan
    refresh BEFORE issuing connect, so a stale NM cache doesn't
    fail-fast with 'No network with SSID "..." found'.

    Runs WITHOUT sudo: NetworkManager allows scan ops for any user
    in the `netdev` group on Pi OS / Debian (verified 2026-05-14 on
    dev Pi). The previous follow-up dispatch said to add a sudoers
    grant -- empirically NOT needed for the openmarquee user, so
    no sudoers change rides with this commit.

    Returns the _NmcliResult unchanged; callers MUST treat this as
    best-effort (a rescan that errors is fine -- proceed to connect
    anyway since NM may have already cached the SSID from a prior
    scan or from a connection-up event).
    """
    return nmcli_runner(
        ["device", "wifi", "rescan", "ifname", _STATION_IFNAME],
        sudo=False,
        timeout=10,
    )


def _wifi_connect(ssid: str, password: str) -> _NmcliResult:
    """`nmcli device wifi connect "<ssid>" password "<pw>" ifname wlan0`.

    NM creates a new connection profile named <ssid> (or replaces an
    existing one with the same SSID name) + brings it up. Returns
    the nmcli result; callers check returncode for success.
    """
    return nmcli_runner(
        [
            "device",
            "wifi",
            "connect",
            ssid,
            "password",
            password,
            "ifname",
            _STATION_IFNAME,
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
                    active,
                    result.stderr,
                )
        _set_state("disabled", detail=None, ssid=None)


def apply_enabled(
    ssid: str,
    password: str,
    poll_timeout_sec: int = 30,
    rescan_settle_sec: float = 2.0,
) -> bool:
    """Bring wlan0 onto the given home WiFi via nmcli. Idempotent:
    if wlan0 is already associated with `ssid`, the function
    no-ops + returns True. Otherwise:

    1. Idempotent fast path: if already on `ssid` + connected, no-op.
    2. Pre-check `nmcli device show wlan0` GENERAL.STATE. If the
       state is 20 (unavailable) -- rfkill, kernel module missing,
       radio off -- fast-fail with an actionable detail BEFORE
       wasting the slow connect-call (which would surface the same
       symptom as 'No network with SSID found').
    3. Set state -> 'rescanning'. Run `nmcli device wifi rescan
       ifname wlan0` (best-effort) + sleep `rescan_settle_sec` so
       NM's scan cache is fresh. Failure here is non-fatal --
       proceed to connect anyway.
    4. Set state -> 'connecting'.
    5. If wlan0 is on a different active connection, `nmcli
       connection delete` it (so the new connect doesn't auto-fall
       back to the old profile on failure).
    6. `nmcli device wifi connect` to bring up the new profile.
    7. Poll device-state for the connected (100) state, up to
       `poll_timeout_sec`.
    8. Set state -> 'connected' on success, 'failed' on
       nmcli-non-zero OR poll-timeout.

    Returns True on success, False on any failure. The caller
    (apply_in_background) doesn't actually consume the return value
    -- the in-memory _STATE singleton is the contract surface.
    """
    with _APPLY_LOCK:
        # Idempotent short-circuit (step 1).
        active = _active_connection_for_device()
        if active == ssid and _is_device_connected():
            log.info("wifi station: already connected to ssid=%r; no-op", ssid)
            _set_state("connected", detail=None, ssid=ssid)
            return True

        # Radio pre-check (step 2). State code 20 means NM cannot
        # use wlan0 at all -- typically rfkill or a missing module.
        # Surface a diagnostic IMMEDIATELY rather than waiting for
        # the slow connect-call to fail with the same root cause
        # but a misleading 'No network...' error.
        state_str = _device_state()
        code = _device_state_code(state_str)
        if code == 20:
            detail = (
                f"wlan0 radio unavailable (state {state_str!r}); "
                "check `rfkill list` for a soft/hard block, verify the "
                "brcmfmac driver loaded, or power-cycle the device"
            )
            log.error("wifi station: %s", detail)
            _set_state("failed", detail=detail, ssid=ssid)
            return False

        # Pre-connect rescan (step 3). Refreshes NM's scan cache so
        # the connect-call doesn't fast-fail with 'No network with
        # SSID found' on a stale cache. The rescan itself is best-
        # effort -- if it errors, NM may still have a cached entry
        # from a prior connection event. Don't abort the flow.
        _set_state("rescanning", detail=None, ssid=ssid)
        log.info("wifi station: rescanning before connect to ssid=%r", ssid)
        rescan_result = _wifi_rescan()
        if rescan_result.returncode != 0:
            log.info(
                "wifi station: rescan returned %d (best-effort; proceeding): %s",
                rescan_result.returncode,
                rescan_result.stderr.strip(),
            )
        if rescan_settle_sec > 0:
            time.sleep(rescan_settle_sec)

        _set_state("connecting", detail=None, ssid=ssid)

        # If we're connected to a different SSID, delete that
        # connection profile first so nmcli's auto-fallback doesn't
        # pick it up if the new connect fails.
        if active and active != ssid:
            log.info(
                "wifi station: removing prior connection %r before switching to %r",
                active,
                ssid,
            )
            result = _connection_delete(active)
            if result.returncode != 0:
                log.warning(
                    "wifi station: failed to delete %r (continuing anyway): %s",
                    active,
                    result.stderr,
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
            "wifi station: timed out waiting for connection to ssid=%r",
            ssid,
        )
        return False


def apply_in_background(
    enabled: bool,
    ssid: str | None,
    password: str | None,
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
    prev_ssid: str | None,
    prev_password: str | None,
    new_enabled: bool,
    new_ssid: str | None,
    new_password: str | None,
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
