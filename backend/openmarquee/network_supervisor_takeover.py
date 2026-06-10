"""P1.2-B take-over orchestrator: the moment the supervisor takes
wlan0 from NetworkManager.

Composes the pre-flight check (Tailscale survival), the rollback
watchdog (self-healing if STA fails to re-establish in N min),
and the flip itself (NM unmanage drop-in + reload + render
wpa_supplicant config + enable wpa_supplicant@wlan0.service).

Per QA dispatch 2026-06-10: rollback watchdog MUST be armed
BEFORE the take-over flip and MUST revert the NM-unmanage if the
supervisor fails to re-establish STA within 3 min. Self-healing
— "do not depend on me noticing."

Env-gated: the entire take-over evaluation is bypassed unless
`OPENMARQUEE_NETWORK_TAKEOVER_ENABLED=1`. Default: off. Operators
opt in per-device via a systemd drop-in for the backend service.
On Mac dev (no /etc/NetworkManager), even with the env var set
the orchestrator refuses to proceed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from openmarquee.network_supervisor import NetworkSupervisor

log = logging.getLogger(__name__)

# Defaults match the existing system/ layout + QA's spec.
DEFAULT_NM_UNMANAGE_WLAN0_PATH = Path("/etc/NetworkManager/conf.d/openmarquee-wlan0-unmanaged.conf")
DEFAULT_WPA_CONF_PATH = Path("/etc/wpa_supplicant/wpa_supplicant-wlan0.conf")
DEFAULT_NM_DIR = Path("/etc/NetworkManager")

# Per QA dispatch §"Reminder on the rollback watchdog": N=3 min.
DEFAULT_ROLLBACK_TIMEOUT_S = 180.0

# Subprocess hop timeouts.
# P1.2-B.3 (2026-06-10): split the netctl per-op timeout into
# fast + slow buckets. The previous flat 15s cap killed
# wpa-enable-wlan0 mid-flip on the dev Pi — `systemctl enable
# --now wpa_supplicant@wlan0.service` under boot load takes
# 20-30s including service-state transitions. Fast ops (file
# writes, reloads, stops) still cap at 15s; the slow class
# (enable/restart on systemd units) gets 60s.
NETCTL_FAST_TIMEOUT_S = 15.0
NETCTL_SLOW_TIMEOUT_S = 60.0
NM_RELOAD_TIMEOUT_S = NETCTL_FAST_TIMEOUT_S
TAILSCALE_STATUS_TIMEOUT_S = 5.0

TAKEOVER_ENV_FLAG = "OPENMARQUEE_NETWORK_TAKEOVER_ENABLED"


# ============================================================
# Tailscale pre-flight check
# ============================================================


@dataclass
class TailscalePreflightResult:
    ok: bool
    reason: str
    backend_state: str | None = None
    local_node_ip: str | None = None


def parse_tailscale_status_json(raw: str) -> TailscalePreflightResult:
    """Pure parser for `tailscale status --json` output.

    Returns ok=True iff:
      * JSON parses
      * top-level `BackendState == "Running"`
      * top-level `Self.TailscaleIPs` is a non-empty list (the local
        node has a tailnet IP assigned)

    Pure function, testable on every host without `tailscale`.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return TailscalePreflightResult(ok=False, reason=f"json_decode_error: {e}")
    backend_state = data.get("BackendState")
    if backend_state != "Running":
        return TailscalePreflightResult(
            ok=False,
            reason=f"backend_state_not_running: {backend_state!r}",
            backend_state=backend_state,
        )
    self_node = data.get("Self") or {}
    ips = self_node.get("TailscaleIPs") or []
    if not ips:
        return TailscalePreflightResult(
            ok=False,
            reason="local_node_has_no_tailscale_ip",
            backend_state=backend_state,
        )
    return TailscalePreflightResult(
        ok=True,
        reason="ok",
        backend_state=backend_state,
        local_node_ip=str(ips[0]),
    )


async def tailscale_preflight_check(
    *,
    timeout_s: float = TAILSCALE_STATUS_TIMEOUT_S,
) -> TailscalePreflightResult:
    """Run `tailscale status --json` + parse + return result. The
    take-over orchestrator uses this BEFORE the NM unmanage flip
    to guarantee Tailscale is the out-of-band path that survives
    the wifi blip (per QA dispatch §"Pre-flight checklist" item 1).

    On Mac dev / missing binary / non-zero exit: returns ok=False
    with a structured reason.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale",
            "status",
            "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return TailscalePreflightResult(ok=False, reason="tailscale_binary_not_found")
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        with contextlib.suppress(Exception):
            proc.kill()
        return TailscalePreflightResult(ok=False, reason=f"timeout_after_{timeout_s:.0f}s")
    if proc.returncode != 0:
        return TailscalePreflightResult(
            ok=False,
            reason=f"tailscale_status_rc={proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')!r}",
        )
    return parse_tailscale_status_json(stdout.decode("utf-8", errors="replace"))


# ============================================================
# Rollback watchdog
# ============================================================


class RollbackWatchdog:
    """Armed-before-flip self-healing timer. If the supervisor fails
    to observe STA_ASSOCIATED within `timeout_s` after arming, the
    watchdog fires `rollback_cb` to revert the take-over.

    Cancellation semantics:
      * `arm()` spawns the timer task; idempotent (re-arming a live
        watchdog is a no-op).
      * `disarm()` cancels the timer cleanly. Called by the
        orchestrator on the FIRST STA_ASSOCIATED event after the
        flip.
      * On timeout fire, `rollback_cb()` is awaited; on its return
        (success or exception), `self.fired = True` is set so the
        orchestrator's outer loop can persist the rollback marker.

    Tests use a tiny `timeout_s` + a recorded `rollback_cb` to
    verify both the disarm path and the fire path.
    """

    def __init__(
        self,
        timeout_s: float,
        rollback_cb: Callable[[], Awaitable[None]],
    ):
        self.timeout_s = timeout_s
        self.rollback_cb = rollback_cb
        self._task: asyncio.Task[None] | None = None
        self._fired = False

    @property
    def fired(self) -> bool:
        return self._fired

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done()

    async def arm(self) -> None:
        if self.armed:
            return
        self._fired = False
        self._task = asyncio.create_task(self._run(), name="network-takeover-rollback-watchdog")

    async def disarm(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self.timeout_s)
        except asyncio.CancelledError:
            raise
        # Timer expired; fire the rollback.
        log.warning(
            "network-takeover: rollback watchdog fired after %.0fs; "
            "supervisor did not observe STA_ASSOCIATED in window",
            self.timeout_s,
        )
        try:
            await self.rollback_cb()
        except Exception:
            log.exception("network-takeover: rollback_cb raised; state may be partial")
        self._fired = True


# ============================================================
# Take-over orchestrator
# ============================================================


@dataclass
class TakeoverPreconditions:
    """Aggregated pre-flight checks. all_met determines whether the
    orchestrator may proceed with the flip."""

    env_flag_set: bool
    nm_dir_present: bool
    has_credentials: bool
    tailscale: TailscalePreflightResult
    not_already_active: bool
    rollback_not_recently_fired: bool

    @property
    def all_met(self) -> bool:
        return (
            self.env_flag_set
            and self.nm_dir_present
            and self.has_credentials
            and self.tailscale.ok
            and self.not_already_active
            and self.rollback_not_recently_fired
        )

    def reasons(self) -> list[str]:
        """List of failure reasons; empty when all_met."""
        reasons: list[str] = []
        if not self.env_flag_set:
            reasons.append(f"{TAKEOVER_ENV_FLAG}_not_set")
        if not self.nm_dir_present:
            reasons.append("nm_dir_missing")
        if not self.has_credentials:
            reasons.append("settings_credentials_missing")
        if not self.tailscale.ok:
            reasons.append(f"tailscale: {self.tailscale.reason}")
        if not self.not_already_active:
            reasons.append("takeover_already_active_in_persisted_state")
        if not self.rollback_not_recently_fired:
            reasons.append("rollback_recently_fired_in_persisted_state")
        return reasons


@dataclass
class TakeoverPaths:
    """File-system paths the orchestrator writes. Defaults match the
    production layout; tests substitute tmp_path-based variants."""

    nm_unmanage_path: Path = DEFAULT_NM_UNMANAGE_WLAN0_PATH
    wpa_conf_path: Path = DEFAULT_WPA_CONF_PATH
    nm_dir: Path = DEFAULT_NM_DIR


def render_nm_unmanage_drop_in(extra_interface: str = "wlan0") -> str:
    """Render the NetworkManager drop-in that unmanages `extra_interface`
    on top of the existing `ap0` unmanage in system/NetworkManager-
    openmarquee-unmanaged.conf. Pure function, testable.

    The supervisor writes this at take-over time so the change can
    be reverted at rollback without touching the install-time
    canonical `ap0` drop-in.
    """
    return f"""# openMarquee P1.2-B take-over drop-in (auto-generated).
# Unmanages {extra_interface} so the supervisor's wpa_supplicant@wlan0
# instance owns it directly instead of NetworkManager. Removed by
# the rollback watchdog OR explicit operator opt-out.
#
# The canonical ap0 unmanage lives in the install-time drop-in:
#   /etc/NetworkManager/conf.d/openmarquee-unmanaged.conf

[keyfile]
unmanaged-devices=interface-name:{extra_interface}
"""


def render_wpa_supplicant_conf(
    ssid: str,
    psk: str,
    *,
    country: str = "US",
) -> str:
    """Render `wpa_supplicant-wlan0.conf` content from supplied
    credentials. Pure function, testable. Caller atomic-writes to
    the canonical path.

    The bare-PSK form is fine; wpa_supplicant accepts both
    `psk="passphrase"` (interpreted as ASCII passphrase, hashed at
    load) and `psk=<64-hex>` (pre-hashed). We use the passphrase
    form because the operator-set credential is the ASCII passphrase.
    """
    return f"""# openMarquee P1.2-B auto-generated.
# Take-over wpa_supplicant config for wlan0. Owner: NetworkSupervisor
# (backend/openmarquee/network_supervisor_takeover.py). Rewritten at
# every take-over attempt; do not hand-edit -- changes will be lost.

ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
country={country}

network={{
    ssid="{ssid}"
    psk="{psk}"
}}
"""


NETCTL_SOCKET_PATH = "/run/openmarquee/netctl.sock"


async def _run_netctl(
    subcommand: str,
    *,
    stdin_input: str | None = None,
    timeout_s: float = NETCTL_FAST_TIMEOUT_S,
) -> None:
    """P1.2-B.2 (2026-06-10): privilege-boundary invocation via the
    socket-activated root daemon. QA verified that
    NoNewPrivileges=true on the backend service BLOCKS sudo
    entirely; this socket transport sidesteps that — the daemon
    runs as root via a systemd template service, the backend just
    speaks the protocol over a Unix socket whose access is
    restricted to SocketGroup=openmarquee.

    Protocol (one round-trip per call):
        Client → Server:
            Line 1: <subcommand>\\n
            Optional payload: <bytes>  (write-* subcommands)
            Client closes write side (SHUT_WR)
        Server → Client:
            Line 1: "OK\\n"  on success
            OR
            Line 1: "ERR <message>\\n"  on failure

    Raises RuntimeError on connect failure / timeout / non-OK
    response. Caller (TakeoverOrchestrator / HostapdChannelActuator)
    treats any failure as "this step did not succeed" + propagates
    appropriately (rollback on attempt-time failure;
    HostapdActuationError on actuator-time failure).
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(NETCTL_SOCKET_PATH),
            timeout=timeout_s,
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"netctl socket not found at {NETCTL_SOCKET_PATH}: {e}") from e
    except TimeoutError as e:
        raise RuntimeError(f"netctl socket connect timed out after {timeout_s:.0f}s") from e
    except OSError as e:
        raise RuntimeError(f"netctl socket connect failed: {e}") from e
    try:
        writer.write(subcommand.encode("ascii") + b"\n")
        if stdin_input is not None:
            writer.write(stdin_input.encode("utf-8"))
        await writer.drain()
        writer.write_eof()
        # Read the single response line; daemon writes OK\n or
        # ERR <msg>\n then closes.
        try:
            response_line = await asyncio.wait_for(reader.readline(), timeout=timeout_s)
        except TimeoutError as e:
            raise RuntimeError(
                f"netctl {subcommand}: response read timed out after {timeout_s:.0f}s"
            ) from e
        response = response_line.decode("utf-8", errors="replace").rstrip("\n")
        if response == "OK":
            return
        if response.startswith("ERR "):
            raise RuntimeError(f"netctl {subcommand}: {response[4:]}")
        if not response:
            raise RuntimeError(f"netctl {subcommand}: empty response (daemon closed before reply?)")
        raise RuntimeError(f"netctl {subcommand}: unexpected response {response!r}")
    finally:
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


class TakeoverOrchestrator:
    """The take-over machinery. Composes pre-flight + flip + watchdog
    + rollback. Designed so each step is testable + mockable.

    Lifecycle:
      * `evaluate_preconditions(supervisor, settings)` — pure check
        returning TakeoverPreconditions; orchestrator's caller
        decides whether to call attempt() based on all_met.
      * `attempt()` — async: writes NM drop-in, reloads NM, renders
        wpa_supplicant config, enables wpa_supplicant@wlan0.service,
        arms watchdog. Returns the watchdog handle.
      * Supervisor's observe loop calls `mark_associated()` on the
        first CTRL-EVENT-CONNECTED after the flip; orchestrator
        disarms the watchdog + persists takeover_active=True.
      * `rollback()` — async: deletes NM drop-in, reloads NM, stops
        wpa_supplicant@wlan0.service, sets rollback_fired_at in
        persisted state.

    Cross-platform safety: pre-flight refuses on Mac dev (NM dir
    missing). All actual subprocess calls are async + mockable.
    """

    def __init__(
        self,
        supervisor: NetworkSupervisor,
        paths: TakeoverPaths | None = None,
        *,
        rollback_timeout_s: float = DEFAULT_ROLLBACK_TIMEOUT_S,
    ):
        self.supervisor = supervisor
        self.paths = paths or TakeoverPaths()
        self.rollback_timeout_s = rollback_timeout_s
        self._watchdog: RollbackWatchdog | None = None

    @property
    def watchdog(self) -> RollbackWatchdog | None:
        return self._watchdog

    async def evaluate_preconditions(
        self,
        *,
        wifi_station_ssid: str | None,
        wifi_station_password: str | None,
        takeover_active_in_state: bool,
        rollback_fired_at: float | None,
        rollback_cooldown_s: float = 3600.0,
        env: dict[str, str] | None = None,
    ) -> TakeoverPreconditions:
        env_map = env if env is not None else os.environ
        env_flag = env_map.get(TAKEOVER_ENV_FLAG, "").strip().lower() in ("1", "true", "yes")
        nm_dir = self.paths.nm_dir.exists()
        has_creds = bool(wifi_station_ssid and wifi_station_password)
        tailscale = await tailscale_preflight_check()
        not_active = not takeover_active_in_state
        if rollback_fired_at is None:
            rollback_clear = True
        else:
            rollback_clear = (time.monotonic() - rollback_fired_at) > rollback_cooldown_s
        return TakeoverPreconditions(
            env_flag_set=env_flag,
            nm_dir_present=nm_dir,
            has_credentials=has_creds,
            tailscale=tailscale,
            not_already_active=not_active,
            rollback_not_recently_fired=rollback_clear,
        )

    async def attempt(
        self,
        *,
        wifi_station_ssid: str,
        wifi_station_password: str,
        country: str = "US",
    ) -> RollbackWatchdog:
        """Execute the take-over flip. Returns the armed watchdog
        handle so the caller can disarm() on STA_ASSOCIATED."""
        log.warning(
            "network-takeover: ATTEMPTING flip (rollback_timeout=%.0fs)",
            self.rollback_timeout_s,
        )
        # 1. ARM rollback watchdog BEFORE the flip.
        watchdog = RollbackWatchdog(
            timeout_s=self.rollback_timeout_s,
            rollback_cb=self.rollback,
        )
        await watchdog.arm()
        self._watchdog = watchdog
        try:
            # 2. Write the NM unmanage drop-in for wlan0 via the
            #    privileged netctl helper. P1.2-B.1: the backend
            #    can't write /etc directly under
            #    ProtectSystem=strict; sudo to netctl crosses the
            #    privilege boundary.
            await _run_netctl(
                "nm-write-unmanaged-wlan0",
                stdin_input=render_nm_unmanage_drop_in("wlan0"),
            )
            # 3. Reload NM so it picks up the new conf. NM stops
            #    managing wlan0 here, but its singleton
            #    wpa_supplicant.service still HOLDS the nl80211
            #    ifname (see step 4 — P1.2-B.3 fix).
            await _run_netctl("nm-reload", timeout_s=NM_RELOAD_TIMEOUT_S)
            # 4. P1.2-B.3: stop NM's singleton wpa_supplicant.service
            #    so our wpa_supplicant@wlan0.service can claim the
            #    interface. Without this, step 6's enable fails
            #    status=255 "another wpa_supplicant process already
            #    running" — NM unmanage releases NM's profile but
            #    NOT its supplicant's hold on the nl80211 ifname.
            await _run_netctl("nm-supplicant-stop", timeout_s=NETCTL_SLOW_TIMEOUT_S)
            # 5. Render wpa_supplicant-wlan0.conf with the operator's
            #    credentials. Same privilege-boundary crossing.
            await _run_netctl(
                "wpa-write-wlan0-conf",
                stdin_input=render_wpa_supplicant_conf(
                    wifi_station_ssid,
                    wifi_station_password,
                    country=country,
                ),
            )
            # 6. Enable + start wpa_supplicant@wlan0.service. SLOW
            #    timeout (60s) per P1.2-B.3: systemctl enable --now
            #    under boot load takes 20-30s including the unit's
            #    initial scan + association.
            await _run_netctl("wpa-enable-wlan0", timeout_s=NETCTL_SLOW_TIMEOUT_S)
            # 6. Install the one-shot association hook on the
            #    supervisor so the FIRST STA_ASSOCIATED event after
            #    the flip disarms the watchdog + persists
            #    takeover_active=True. Without this wiring (sacred-
            #    review BLOCKER fix), the watchdog would fire 3 min
            #    after a SUCCESSFUL flip because no path called
            #    mark_associated.
            self.supervisor.set_takeover_associated_hook(self.mark_associated)
            log.warning(
                "network-takeover: flip COMPLETE (waiting for STA_ASSOCIATED; "
                "watchdog will rollback in %.0fs if no association)",
                self.rollback_timeout_s,
            )
        except Exception:
            # On any flip-step failure, run rollback IMMEDIATELY (not
            # via watchdog) so we don't sit in a half-taken-over
            # state for 3 min.
            log.exception("network-takeover: flip raised; running immediate rollback")
            await watchdog.disarm()
            with contextlib.suppress(Exception):
                await self.rollback()
            raise
        return watchdog

    async def mark_associated(self) -> None:
        """Called by the supervisor's observe loop when the first
        STA_ASSOCIATED event arrives after a successful flip. Disarms
        the watchdog + persists the take-over success marker."""
        if self._watchdog is not None:
            await self._watchdog.disarm()
        # Persist via supervisor's existing _persist; we mark
        # takeover_active by setting a flag on the supervisor (the
        # supervisor's PersistedState carries it).
        self.supervisor.set_takeover_active(True)
        log.warning("network-takeover: STA associated; takeover_active=True persisted")

    async def rollback(self) -> None:
        """Revert the take-over. Called by the watchdog on timeout OR
        by attempt() on flip-step failure. Best-effort: each step
        suppresses its own exception so a partial state can still
        be partially repaired."""
        log.warning("network-takeover: ROLLBACK starting")
        # Clear the supervisor's association hook so a delayed
        # STA_ASSOCIATED post-rollback doesn't re-trigger
        # mark_associated against a no-longer-meaningful state.
        self.supervisor.set_takeover_associated_hook(None)
        # 1. Delete the NM drop-in (NM will pick up wlan0 again on
        #    reload). All netctl hops go through the privileged
        #    socket helper so the same daemon covers rollback.
        with contextlib.suppress(Exception):
            await _run_netctl("nm-remove-unmanaged-wlan0")
        # 2. Stop wpa_supplicant@wlan0.service (avoid contention with
        #    NM's singleton supplicant we're about to bring back).
        #    SLOW timeout per P1.2-B.3 since systemctl stop on a
        #    failing-to-associate unit can take time.
        with contextlib.suppress(Exception):
            await _run_netctl("wpa-stop-wlan0", timeout_s=NETCTL_SLOW_TIMEOUT_S)
        # 3. P1.2-B.3 (companion to attempt step 4): bring NM's
        #    singleton wpa_supplicant.service back so NM can
        #    resume managing wlan0 after the rollback.
        with contextlib.suppress(Exception):
            await _run_netctl("nm-supplicant-start", timeout_s=NETCTL_SLOW_TIMEOUT_S)
        # 4. Reload NM.
        with contextlib.suppress(Exception):
            await _run_netctl("nm-reload", timeout_s=NM_RELOAD_TIMEOUT_S)
        # 4. Persist rollback marker so we don't re-attempt
        #    immediately on next backend restart.
        self.supervisor.mark_rollback_fired()
        log.warning(
            "network-takeover: ROLLBACK complete; takeover_active=False, rollback_fired_at recorded"
        )


# ============================================================
# Lifespan-friendly one-shot entry point
# ============================================================


async def run_takeover_attempt_if_eligible(
    *,
    supervisor: NetworkSupervisor,
    settings_storage,
    paths: TakeoverPaths | None = None,
    rollback_timeout_s: float = DEFAULT_ROLLBACK_TIMEOUT_S,
    settle_delay_s: float = 2.0,
) -> None:
    """Spawned by app.py's lifespan startup as a one-shot task.
    Evaluates preconditions; if all met, executes the take-over
    flip + installs the active hostapd actuator on the supervisor.

    Settle delay: gives the observe loop a tick to establish the
    wpa_supplicant socket connection before the orchestrator does
    its work. The delay also makes the boot-time logs less racy
    (otherwise the orchestrator's `ATTEMPTING flip` line interleaves
    with the supervisor boot line).

    All steps are best-effort + logged. Failures here do NOT crash
    the backend — they leave the supervisor in observe-only mode.
    """
    await asyncio.sleep(settle_delay_s)
    settings = settings_storage.load()
    orchestrator = TakeoverOrchestrator(
        supervisor=supervisor,
        paths=paths,
        rollback_timeout_s=rollback_timeout_s,
    )
    pre = await orchestrator.evaluate_preconditions(
        wifi_station_ssid=settings.wifi_station_ssid,
        wifi_station_password=settings.wifi_station_password,
        takeover_active_in_state=supervisor.takeover_active,
        rollback_fired_at=supervisor.rollback_fired_at,
    )
    if not pre.all_met:
        log.info(
            "network-takeover: preconditions not met; staying observe-only. reasons=%s",
            pre.reasons(),
        )
        return
    log.warning("network-takeover: preconditions met; attempting flip")
    try:
        await orchestrator.attempt(
            wifi_station_ssid=settings.wifi_station_ssid or "",
            wifi_station_password=settings.wifi_station_password or "",
        )
    except Exception:
        log.exception("network-takeover: attempt() raised; supervisor stays observe-only")
        return
    # Install the active hostapd actuator. The supervisor's
    # apply_sta_freq now drives a real hostapd restart + post-verify
    # on every STA-freq change.
    from openmarquee.network_supervisor_actuator import HostapdChannelActuator

    supervisor.set_channel_follow_actuator(HostapdChannelActuator())
