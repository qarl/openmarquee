"""Network supervisor — single owner of radio state for AP+STA concurrency.

Lives inside the FastAPI process (per
docs/onboarding-rework-plan.md §B / P1.1 + spec
~/project/openmarquee/qa/spec-onboarding-ap-sta-concurrent-2026-06-10.md
§"Recommended regime"). Replaces the previous architecture where
multiple systemd units (NM, hostapd, dnsmasq, openmarquee-best-wifi
timer) raced each other for radio control.

P1.1 ships the SKELETON:
  * SupervisorState enum + pure functional state-transition logic
  * Frequency-to-channel math (Python mirror of the shell snippet
    in system/openmarquee-ap0-setup.sh)
  * Channel-follow engine (computes target hostapd channel + has a
    dry-run mode for tests; supervisor calls write_hostapd_config
    on a real Pi)
  * Diagnostics ring buffer (5-min sliding window, in-memory)
  * Fallback mode flag plumbing
    (settings.network_fallback_mutex_mode)
  * wpa_supplicant control-socket client (read-only / event-listen;
    the active write-side that takes wlan0 over from NM is a
    follow-up commit)

P1.1 EXPLICITLY DEFERS (subsequent commits):
  * NM unmanage of wlan0 + actual wpa_supplicant-direct take-over
  * wifi_station.py shim
  * nmcli connection profile migration to wpa_supplicant blocks
  * Scan-and-pick-best (r60 best-wifi.sh stays for now)
  * iw event ring buffer (currently this module ingests events but
    doesn't snapshot dmesg)
  * Marquee status surface IPC (P4)

The fallback mode flag (`network_fallback_mutex_mode`) is the safety
net: when true, the supervisor enforces mutual exclusion (AP off
when STA up) rather than the spec's preferred concurrent regime.
QA can flip the flag at runtime if firmware bugs surface; no
reinstall required (per QA-DISPATCH 2026-06-10).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

# Only project import: a stdlib-only leaf that derives the device's real
# mDNS URL (http://<hostname>.local) so the CONNECTED card shows the same
# hostname-derived address as the boot identity card, not a hardcoded
# "openmarquee.local". No import cycle (mdns imports os/socket only).
from openmarquee.mdns import mdns_url

log = logging.getLogger(__name__)

# ============================================================
# Constants — match the spec + the existing system/ configs.
# ============================================================

# 5-min sliding window for the diagnostics ring buffer (per spec
# §"Diagnostics" — "captured continuously to a ring buffer").
DIAGNOSTICS_WINDOW_SECONDS = 300.0

# Path the supervisor uses to persist last-known state across reboots
# (state machine resumes at the right entry point after a power cycle).
#
# P1.2-B.2 (2026-06-10): moved from /var/lib/openmarquee/ to
# /var/openmarquee/. The backend service runs with
# ReadWritePaths=/var/openmarquee under ProtectSystem=strict; the
# original /var/lib/ path was NOT in the sandbox so save_persisted_state
# raised "Read-only file system" on every transition during QA's verify.
# /var/openmarquee/ is where every other state file lives (playlist,
# settings, auth) so this is consistent with existing convention.
DEFAULT_STATE_FILE = Path("/var/openmarquee/network-state.json")

# wpa_supplicant control socket. Even when NM manages wlan0, NM uses
# a singleton wpa_supplicant whose socket is at this path on Bookworm,
# so the read-only listener works in either regime.
DEFAULT_WPA_CTRL_PATH = Path("/var/run/wpa_supplicant/wlan0")

# Hysteresis threshold for "candidate SSID is meaningfully stronger
# than current" — re-implementation of the r60 best-wifi.sh contract.
# Units are NM SIGNAL percentage-points (0-100), NOT raw dBm — per
# the r60 BLOCKER fix.
DEFAULT_HYSTERESIS_SIGNAL = 8


# ============================================================
# State machine
# ============================================================


class SupervisorState(StrEnum):
    """Product-level state machine per spec §"Onboarding state machine".

    Values are stringly-typed so JSON serialisation (for the state
    file + API responses + ring-buffer events) is human-readable.
    All serialisation paths use ``.value`` explicitly, so the
    str+Enum → StrEnum migration is wire-compatible.
    """

    # No stored creds yet. AP up, portal active. Marquee shows QR.
    SETUP = "SETUP"
    # AP up; STA mid-association. Portal shows progress.
    CONNECTING = "CONNECTING"
    # STA up + grace window (~2 min). AP follows STA channel so the
    # phone-in-the-portal sees the confirmation. Default 120s; may
    # be cut short by an explicit operator advance to ONLINE.
    LINGER = "LINGER"
    # STA only. AP torn down. Default steady state.
    ONLINE = "ONLINE"
    # STA lost. AP comes back, retry with backoff.
    DEGRADED = "DEGRADED"


class SupervisorEvent(StrEnum):
    """External events that drive state transitions."""

    HAS_STORED_CREDENTIALS = "HAS_STORED_CREDENTIALS"
    NO_STORED_CREDENTIALS = "NO_STORED_CREDENTIALS"
    STA_ASSOCIATED = "STA_ASSOCIATED"
    STA_AUTH_FAILED = "STA_AUTH_FAILED"
    STA_DISCONNECTED = "STA_DISCONNECTED"
    # 2026-07-01 (audit 4b follow-up): wpa_supplicant couldn't find
    # the target SSID in its scan. Classifies the DEGRADED-card
    # variant as "not_found" or, when the last iw-scan saw the SSID
    # only on 5 GHz, "not_found_or_5ghz". No state-machine
    # transition yet — the supervisor records the classification for
    # the NEXT DEGRADED render. Wiring an ONLINE/CONNECTING →
    # DEGRADED edge on this event is a separate PR.
    STA_SSID_NOT_FOUND = "STA_SSID_NOT_FOUND"
    # 2026-07-01 (audit 4b follow-up): wpa_supplicant's scan itself
    # failed (interface disappeared, firmware crash, EBUSY). No
    # classification — logged for diagnostics; the supervisor keeps
    # whatever variant was last recorded.
    STA_SCAN_FAILED = "STA_SCAN_FAILED"
    LINGER_TIMER_EXPIRED = "LINGER_TIMER_EXPIRED"
    OPERATOR_REQUESTED_SETUP_MODE = "OPERATOR_REQUESTED_SETUP_MODE"
    SETUP_MODE_TIMER_EXPIRED = "SETUP_MODE_TIMER_EXPIRED"


def next_state(
    current: SupervisorState,
    event: SupervisorEvent,
    *,
    fallback_mutex: bool = False,
) -> SupervisorState | None:
    """Pure functional state-transition table.

    Returns the new state, or None if the (state, event) pair has no
    defined transition (caller logs + ignores). `fallback_mutex`
    controls whether the supervisor's concurrent-AP+STA branch
    (default) OR the comitup mutex-AP/STA branch is in effect:
    concurrent permits LINGER (AP up during the grace window);
    mutex jumps straight to ONLINE on STA up.

    The transition table is the SINGLE source of truth for state-
    machine behavior. Unit tests pin every (state, event,
    fallback_mutex) tuple so a future refactor can't silently change
    the contract.
    """
    s = current
    e = event
    # SETUP → CONNECTING when stored creds arrive (operator submitted
    # via portal) or device boots with creds already on disk. Same
    # transition in both concurrent + mutex regimes.
    if s == SupervisorState.SETUP and e == SupervisorEvent.HAS_STORED_CREDENTIALS:
        return SupervisorState.CONNECTING
    # CONNECTING → LINGER (concurrent) or ONLINE (mutex) on
    # successful STA association.
    if s == SupervisorState.CONNECTING and e == SupervisorEvent.STA_ASSOCIATED:
        return SupervisorState.ONLINE if fallback_mutex else SupervisorState.LINGER
    # CONNECTING → SETUP on auth failure (keep AP up; surface error
    # in portal). In both regimes.
    if s == SupervisorState.CONNECTING and e == SupervisorEvent.STA_AUTH_FAILED:
        return SupervisorState.SETUP
    # 2026-07-02 (audit 4b close-out): CONNECTING → SETUP when the
    # target SSID isn't in the scan. Mirrors the STA_AUTH_FAILED
    # edge — keep the portal up so the operator can pick a different
    # network or unmask a hidden SSID. The SETUP card carries the
    # classified variant (`not_found` vs `not_found_or_5ghz`) so
    # the on-glass reason banner is specific.
    if s == SupervisorState.CONNECTING and e == SupervisorEvent.STA_SSID_NOT_FOUND:
        return SupervisorState.SETUP
    # LINGER → ONLINE when the grace timer expires.
    if s == SupervisorState.LINGER and e == SupervisorEvent.LINGER_TIMER_EXPIRED:
        return SupervisorState.ONLINE
    # LINGER → DEGRADED if STA drops during the grace window.
    if s == SupervisorState.LINGER and e == SupervisorEvent.STA_DISCONNECTED:
        return SupervisorState.DEGRADED
    # ONLINE → DEGRADED on STA loss.
    if s == SupervisorState.ONLINE and e == SupervisorEvent.STA_DISCONNECTED:
        return SupervisorState.DEGRADED
    # DEGRADED → CONNECTING (retry).
    if s == SupervisorState.DEGRADED and e == SupervisorEvent.STA_ASSOCIATED:
        return SupervisorState.ONLINE if fallback_mutex else SupervisorState.LINGER
    # Operator-requested setup mode from any non-SETUP state.
    if e == SupervisorEvent.OPERATOR_REQUESTED_SETUP_MODE and s != SupervisorState.SETUP:
        return SupervisorState.SETUP
    # Setup-mode auto-off timer brings us back to attempting
    # association if creds exist; supervisor decides where to go
    # (CONNECTING if creds, ONLINE if mutex+no-bg, etc). Default:
    # ONLINE — caller can override based on context.
    if s == SupervisorState.SETUP and e == SupervisorEvent.SETUP_MODE_TIMER_EXPIRED:
        return SupervisorState.ONLINE
    # No-stored-creds + we're somewhere other than SETUP → snap back
    # to SETUP (consistency).
    if e == SupervisorEvent.NO_STORED_CREDENTIALS and s != SupervisorState.SETUP:
        return SupervisorState.SETUP
    return None


# ============================================================
# Frequency-to-channel math (mirror of the shell snippet)
# ============================================================


def freq_to_channel(freq_mhz: int) -> int | None:
    """Mirror of system/openmarquee-ap0-setup.sh's awk + integer-div
    block. Returns None for 5 GHz or out-of-band frequencies (the
    BCM43438 is 2.4 GHz only; 5 GHz responses mean wlan0 saw a
    network it can't actually join).

    Pinned by P1.0 tests
    (backend/tests/test_p1_0_onboarding_diagnostics.py); this Python
    function is the authoritative reference now that the supervisor
    consumes it.
    """
    if 2412 <= freq_mhz <= 2484:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2412) // 5 + 1
    return None


# ============================================================
# Diagnostics ring buffer
# ============================================================


@dataclass
class DiagnosticEvent:
    """One entry in the diagnostics ring buffer.

    `timestamp` is `time.monotonic()` (steady clock, not affected by
    system-clock skew or NTP jumps). For wall-clock display in API
    responses, we render relative seconds-ago at snapshot time.
    """

    timestamp: float
    source: str  # 'wpa_supplicant' | 'hostapd' | 'state_machine' | 'channel_follow' | 'dmesg'
    severity: str  # 'info' | 'warn' | 'error'
    message: str

    def to_dict(self, *, now: float | None = None) -> dict:
        """JSON-serialisable form for API responses. `now` is the
        reference time for the relative-seconds-ago field; defaults
        to time.monotonic() at call.
        """
        ref = now if now is not None else time.monotonic()
        return {
            "seconds_ago": max(0.0, ref - self.timestamp),
            "source": self.source,
            "severity": self.severity,
            "message": self.message,
        }


class DiagnosticsRingBuffer:
    """5-minute sliding window of network-supervisor events.

    Per spec §"Diagnostics for the intermittent reports":
    "captured continuously to a ring buffer." The buffer is in-
    memory + bounded by time, not by entry count, so a quiet boot
    holds nothing while a chatty wedge can fill it freely. Older
    entries fall off as new ones arrive (or as snapshot() runs the
    expiry check).
    """

    def __init__(self, window_seconds: float = DIAGNOSTICS_WINDOW_SECONDS):
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.window_seconds = window_seconds
        self._events: deque[DiagnosticEvent] = deque()

    def push(
        self,
        source: str,
        severity: str,
        message: str,
        *,
        now: float | None = None,
    ) -> None:
        ts = now if now is not None else time.monotonic()
        self._events.append(
            DiagnosticEvent(timestamp=ts, source=source, severity=severity, message=message)
        )
        self._evict_expired(now=ts)

    def _evict_expired(self, *, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0].timestamp < cutoff:
            self._events.popleft()

    def snapshot(self, *, now: float | None = None) -> list[DiagnosticEvent]:
        """Return a list (oldest-first) of events still inside the
        window. Evicts expired entries first so caller doesn't see
        stale data even if no push happened recently.
        """
        ref = now if now is not None else time.monotonic()
        self._evict_expired(now=ref)
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)


# ============================================================
# Channel-follow engine
# ============================================================


@dataclass
class ChannelFollowDecision:
    """Output of `decide_channel_follow`. `target_channel` is the
    channel hostapd should be on; `regenerate_needed` is True if the
    hostapd config needs to be rewritten + service restarted.
    """

    target_channel: int | None
    regenerate_needed: bool
    reason: str


def decide_channel_follow(
    sta_freq_mhz: int | None,
    current_ap_channel: int | None,
    *,
    fallback_channel: int = 6,
) -> ChannelFollowDecision:
    """Pure decision function: given the STA's current frequency
    and the AP's currently-configured channel, decide what the AP
    should do.

    Cases:
    1. STA not associated (freq=None) → AP stays on fallback_channel
       (channel=6 by default); no regeneration needed if already
       there.
    2. STA on 5 GHz or unknown freq → AP can't follow; stay on
       fallback. Caller may want to surface this to the operator.
    3. STA on 2.4 GHz with channel == current_ap_channel → no
       regeneration needed.
    4. STA on 2.4 GHz with channel != current_ap_channel → regenerate
       hostapd.conf with the new channel + restart hostapd.

    The function is PURE (no IO); the supervisor wraps it with the
    actual config write + systemctl restart. Tests exercise every
    case.
    """
    if sta_freq_mhz is None:
        return ChannelFollowDecision(
            target_channel=fallback_channel,
            regenerate_needed=(current_ap_channel != fallback_channel),
            reason="sta_not_associated",
        )
    sta_chan = freq_to_channel(sta_freq_mhz)
    if sta_chan is None:
        # 5 GHz STA on a 2.4-only radio is structurally impossible
        # on BCM43438 — this branch covers a debug/mocked scenario
        # or a future dual-band port.
        return ChannelFollowDecision(
            target_channel=fallback_channel,
            regenerate_needed=(current_ap_channel != fallback_channel),
            reason="sta_freq_not_2_4ghz",
        )
    if sta_chan == current_ap_channel:
        return ChannelFollowDecision(
            target_channel=sta_chan,
            regenerate_needed=False,
            reason="already_on_target",
        )
    return ChannelFollowDecision(
        target_channel=sta_chan,
        regenerate_needed=True,
        reason="follow_sta",
    )


# ============================================================
# Safety contracts (re-implementation of r60 best-wifi.sh)
# ============================================================


def hysteresis_allows_switch(
    candidate_signal: int,
    current_signal: int,
    *,
    threshold: int = DEFAULT_HYSTERESIS_SIGNAL,
) -> bool:
    """Re-implementation of the r60 hysteresis contract: don't switch
    unless the candidate's signal is STRICTLY better than the
    current's by `threshold` percentage-points.

    Units are NM SIGNAL (0-100), NOT raw dBm. The r60 dispatch
    flagged this as a BLOCKER fix because the original code treated
    the 8-point threshold as dBm.

    Acceptance criterion per QA-DISPATCH 2026-06-10 §C.6 — protects
    QA's remote-access SSH during dev by preventing oscillation
    between SSIDs of similar strength.
    """
    if not (0 <= candidate_signal <= 100):
        raise ValueError(f"candidate_signal must be in [0, 100]; got {candidate_signal}")
    if not (0 <= current_signal <= 100):
        raise ValueError(f"current_signal must be in [0, 100]; got {current_signal}")
    return candidate_signal - current_signal >= threshold


def in_band_ssh_guard_safe_to_switch(
    *,
    has_tailscale_session: bool,
    has_lan_only_session: bool,
) -> bool:
    """Re-implementation of the r60 in-band SSH safety contract.

    Refuse to switch wlan0's connection if the only active SSH
    session would be broken by the switch — i.e. the SSH is
    in-band over the very wifi we're about to drop. Tailscale
    sessions are safe (tailnet survives the wifi blip).

    The supervisor calls this BEFORE executing a wlan0 connection
    change. The argument names mirror what `who` + `last` + a
    Tailscale presence-check return.

    Acceptance criterion per QA-DISPATCH 2026-06-10 §C.6 — these
    contracts protect QA's remote-access SSH during dev.
    """
    if has_tailscale_session:
        return True
    # No tailscale: safe iff there's no LAN-only session to lock out.
    return not has_lan_only_session


# ============================================================
# State persistence
# ============================================================


@dataclass
class PersistedState:
    """Subset of supervisor state that survives a reboot. Written
    atomically to `DEFAULT_STATE_FILE` so a crash mid-write doesn't
    leave a half-parsed file.

    P1.2-B (2026-06-10) added `takeover_active` + `rollback_fired_at`
    so the take-over orchestrator can resume idempotently across
    backend restarts and so a recently-fired rollback prevents an
    immediate re-attempt.
    """

    state: SupervisorState
    last_sta_ssid: str | None = None
    last_sta_channel: int | None = None
    last_transition_monotonic: float | None = None
    boot_counter_consumed: bool = False
    # P1.2-B: persisted across restarts so the orchestrator's
    # idempotency check can short-circuit when a take-over is
    # already live, and so a recently-fired rollback enforces a
    # cooldown before the next attempt.
    takeover_active: bool = False
    rollback_fired_at: float | None = None
    schema_version: int = 2


def load_persisted_state(path: Path = DEFAULT_STATE_FILE) -> PersistedState | None:
    """Read the supervisor state file. Returns None on
    missing-file / parse-error (caller starts from SETUP by
    default). Defensive: any deserialisation problem is logged and
    treated as no-state-on-disk so a corrupt file can never wedge
    the supervisor at boot.
    """
    try:
        contents = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        log.warning("network-supervisor: state file read failed (%s); ignoring", e)
        return None
    try:
        data = json.loads(contents)
        state = SupervisorState(data["state"])
        return PersistedState(
            state=state,
            last_sta_ssid=data.get("last_sta_ssid"),
            last_sta_channel=data.get("last_sta_channel"),
            last_transition_monotonic=data.get("last_transition_monotonic"),
            boot_counter_consumed=data.get("boot_counter_consumed", False),
            takeover_active=data.get("takeover_active", False),
            rollback_fired_at=data.get("rollback_fired_at"),
            schema_version=data.get("schema_version", 1),
        )
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        log.warning(
            "network-supervisor: state file %s is malformed (%s); starting from SETUP",
            path,
            e,
        )
        return None


def save_persisted_state(state: PersistedState, path: Path = DEFAULT_STATE_FILE) -> None:
    """Atomically write the supervisor state file. Writes to a sibling
    .tmp + rename to guarantee readers never see a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "state": state.state.value,
        "last_sta_ssid": state.last_sta_ssid,
        "last_sta_channel": state.last_sta_channel,
        "last_transition_monotonic": state.last_transition_monotonic,
        "boot_counter_consumed": state.boot_counter_consumed,
        "takeover_active": state.takeover_active,
        "rollback_fired_at": state.rollback_fired_at,
        "schema_version": state.schema_version,
    }
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


# ============================================================
# wpa_supplicant control-socket client (read-only listener)
# ============================================================


class WpaSupplicantSocketClient:
    """DGRAM client for wpa_supplicant's control socket.

    Even when NM manages wlan0, NM uses a singleton wpa_supplicant
    whose socket lives at `/var/run/wpa_supplicant/wlan0` on
    Bookworm. This client connects + sends `ATTACH` to subscribe to
    unsolicited events (CTRL-EVENT-CONNECTED, -DISCONNECTED, etc.).

    P1.1: read-only / observe-only. The supervisor records events
    into the diagnostics ring buffer and uses them to drive the
    state machine. The active write-side (`SET_NETWORK`, `RECONNECT`)
    that takes over wlan0 from NM is a follow-up commit.

    The client is designed to be mock-friendly: the constructor
    takes the socket path; tests substitute a temp path + send
    synthetic events to test the parser.
    """

    def __init__(
        self,
        ctrl_path: Path = DEFAULT_WPA_CTRL_PATH,
        *,
        local_socket_dir: Path | None = None,
    ):
        self.ctrl_path = ctrl_path
        # wpa_supplicant requires the CLIENT to also bind to a path
        # so the daemon can send unsolicited replies. Tests substitute
        # local_socket_dir; production gets a fresh tempdir per
        # supervisor lifetime.
        self.local_socket_dir = local_socket_dir
        self._sock: socket.socket | None = None
        self._local_path: Path | None = None

    def connect(self) -> None:
        """Open the DGRAM socket + bind to a local path + ATTACH for
        unsolicited events. Raises FileNotFoundError if
        wpa_supplicant isn't running (the ctrl socket doesn't exist).
        """
        if self._sock is not None:
            return
        if self.local_socket_dir is None:
            self.local_socket_dir = Path(tempfile.mkdtemp(prefix="openmarquee-wpactrl-"))
        self._local_path = self.local_socket_dir / "ctrl"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(str(self._local_path))
        sock.connect(str(self.ctrl_path))
        sock.settimeout(0.5)
        # Subscribe to unsolicited events.
        sock.send(b"ATTACH")
        with contextlib.suppress(TimeoutError):
            _reply = sock.recv(4096)  # noqa: F841 — ATTACH reply is OK\n
        self._sock = sock

    def receive_event(self) -> str | None:
        """Non-blocking poll for one event. Returns None on no event
        available within the recv timeout. Events come as text like
        `<3>CTRL-EVENT-CONNECTED - Connection to <bssid>...` per
        wpa_supplicant's protocol.
        """
        if self._sock is None:
            return None
        try:
            data = self._sock.recv(4096)
        except TimeoutError:
            return None
        return data.decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.send(b"DETACH")
            self._sock.close()
            self._sock = None
        if self._local_path is not None and self._local_path.exists():
            with contextlib.suppress(OSError):
                self._local_path.unlink()


def parse_wpa_event(raw: str) -> tuple[SupervisorEvent | None, dict]:
    """Parse a wpa_supplicant unsolicited event line into the
    supervisor's event vocabulary. Returns (event_kind_or_None,
    extracted_fields). Unrecognised events return (None, {}).

    Pure function — testable without a real wpa_supplicant. Pinned
    by unit tests against the canonical event strings.
    """
    # Strip the priority prefix like "<3>" that wpa_supplicant
    # prepends to unsolicited events. The format is `<N>BODY` where
    # N is the message verbosity level.
    body = raw.lstrip()
    if body.startswith("<") and ">" in body[:5]:
        body = body[body.index(">") + 1 :]
    body = body.strip()

    fields: dict[str, str] = {}
    if body.startswith("CTRL-EVENT-CONNECTED"):
        # `CTRL-EVENT-CONNECTED - Connection to <bssid> completed [id=N id_str=...]`
        for token in body.split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k.lstrip("[")] = v.rstrip("]")
        return SupervisorEvent.STA_ASSOCIATED, fields
    if body.startswith("CTRL-EVENT-DISCONNECTED"):
        for token in body.split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v.strip()
        return SupervisorEvent.STA_DISCONNECTED, fields
    if body.startswith("CTRL-EVENT-AUTH-REJECT") or body.startswith(
        "CTRL-EVENT-SSID-TEMP-DISABLED"
    ):
        return SupervisorEvent.STA_AUTH_FAILED, fields
    # 2026-07-01 (audit 4b follow-up): the target SSID isn't in the
    # scan. `CTRL-EVENT-NETWORK-NOT-FOUND` is the string
    # wpa_supplicant actually emits (wpa_ctrl.c); `CTRL-EVENT-SSID-
    # NOT-FOUND` is accepted as a defensive alias because the spec-
    # facing name in the QA audit + several downstream docs uses
    # the SSID-NOT-FOUND phrasing. Fold both here so a rename on
    # either side doesn't silently drop the classification.
    if body.startswith("CTRL-EVENT-NETWORK-NOT-FOUND") or body.startswith(
        "CTRL-EVENT-SSID-NOT-FOUND"
    ):
        for token in body.split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v.strip().strip('"')
        return SupervisorEvent.STA_SSID_NOT_FOUND, fields
    if body.startswith("CTRL-EVENT-SCAN-FAILED"):
        for token in body.split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v.strip()
        return SupervisorEvent.STA_SCAN_FAILED, fields
    return None, fields


# ============================================================
# Pre-staged wifi detection (Jason-handover, 2026-07-02)
# ============================================================


def _probe_existing_wifi_connection(*, timeout_s: float = 5.0) -> str | None:
    """Return the SSID of an already-active wlan0 wifi connection, or
    None if not connected / not detectable.

    2026-07-02 (Jason handover, qarl handover-critical): the supervisor
    persisted a SETUP boot state on devices whose wifi was staged via
    nmcli (i.e. a pre-existing NM profile before onboarding shipped)
    — SETUP mode painted a dead PIN/QR card because take-over is off
    so no AP was actually up, and the device just sat there wedged.
    This helper answers the "am I already online?" question so
    `NetworkSupervisor.probe_and_seed_from_existing_connection` can
    skip the onboarding chain entirely and go straight to ONLINE.

    Uses nmcli's terse output — stable-column contract:
      wlan0:wifi:connected:MyHomeWifi
      wlan0:wifi:disconnected:
      wlan0:wifi:unavailable:

    Fails soft: no nmcli binary, non-zero return, timeout, missing
    columns → returns None. Callers must not rely on this returning
    a value for correctness — it's an optimization to skip
    onboarding when wifi is already up.

    `timeout_s` bounds the subprocess so a wedged nmcli can't stall
    supervisor construction indefinitely.
    """
    if not shutil.which("nmcli"):
        return None
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = _split_nmcli_terse_row(line)
        if len(parts) < 4:
            continue
        device, dev_type, state, connection = parts[0], parts[1], parts[2], parts[3]
        # DEVICE is stable "wlan0" on Pi OS; TYPE stays "wifi"; STATE
        # is "connected" only when NM has an IP + carrier. An empty
        # CONNECTION column means no active profile (e.g. hotspot AP
        # would show as its own connection name — filtered by the
        # SSID probe below, but nmcli only exposes an AP profile as
        # DEVICE=wlan0 if the operator ran ap-mode, which we ignore
        # by requiring a real CONNECTION name here anyway).
        if device == "wlan0" and dev_type == "wifi" and state == "connected" and connection:
            return connection
    return None


def _split_nmcli_terse_row(line: str) -> list[str]:
    """Split nmcli --terse output on unescaped ':'. nmcli escapes
    embedded colons with '\\:' — an SSID like `Home:Router` shows
    up as `wlan0:wifi:connected:Home\\:Router`. Reversing that
    escape here means the returned CONNECTION column is the actual
    SSID even for edge-case names."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line) and line[i + 1] == ":":
            buf.append(":")
            i += 2
            continue
        if ch == ":":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


# ============================================================
# The Supervisor (orchestrator)
# ============================================================


@dataclass
class SupervisorConfig:
    """Runtime configuration the supervisor needs. Built from settings
    + env at startup. Kept as a small dataclass so tests can pass
    synthetic configs without touching settings module."""

    fallback_mutex_mode: bool = False
    linger_seconds: float = 120.0
    setup_mode_auto_off_seconds: float = 1800.0
    state_file: Path = field(default_factory=lambda: DEFAULT_STATE_FILE)
    wpa_ctrl_path: Path = field(default_factory=lambda: DEFAULT_WPA_CTRL_PATH)


class NetworkSupervisor:
    """The orchestrator. Owns the state machine, the wpa_supplicant
    socket client, the diagnostics ring buffer, and the channel-
    follow engine.

    P1.1 surface:
      - `current_state` property
      - `snapshot_diagnostics()` → list[DiagnosticEvent]
      - `apply_event(SupervisorEvent)` → drives the state machine
        + records diagnostics + persists state
      - `apply_sta_freq(freq_mhz)` → records the STA freq + asks the
        channel-follow engine if regeneration is needed
      - `lifespan_start()` / `lifespan_stop()` async lifecycle hooks
        for FastAPI integration

    The actual wpa_supplicant socket polling + hostapd-restart side-
    effects are stubbed in P1.1 (logged via diagnostics ring buffer
    + the `_channel_follow_actuator` strategy slot). Follow-up
    commits wire them to real subprocess + systemctl calls.
    """

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        diagnostics: DiagnosticsRingBuffer | None = None,
        channel_follow_actuator: Callable[[ChannelFollowDecision], None] | None = None,
        power_save_actuator: Callable[[], None] | None = None,
        ap_lifecycle_actuator: object | None = None,
        system_card_publisher: object | None = None,
    ):
        self.config = config
        self.diagnostics = diagnostics or DiagnosticsRingBuffer()
        self._channel_follow_actuator = channel_follow_actuator or self._default_actuator
        # P1.3 (2026-06-27): STA_ASSOCIATED re-fires the boot-oneshot
        # power-save-off. Default actuator is observe-only (log line);
        # dependencies.py wires the active netctl-driven actuator
        # when a netctl socket is present. Spec §A#2:
        # "[power_save] can be reset by reassociation."
        self._power_save_actuator = power_save_actuator or self._default_power_save_actuator
        # P2 (2026-06-27): AP up/down lifecycle. The supervisor's
        # transition-effect dispatch calls .stop() / .start() on
        # this object. Default actuator is observe-only (log-only).
        # dependencies.py wires the netctl-driven HostapdLifecycle-
        # Actuator on production hosts. Object protocol (not
        # callable): must expose .stop() -> None and .start() ->
        # None methods so a single instance handles both directions.
        self._ap_lifecycle_actuator = ap_lifecycle_actuator or self._default_ap_lifecycle_actuator()
        # PR3 (2026-06-27) system-card publisher. _on_transition
        # calls .render(params) on non-ONLINE transitions and
        # .clear() on ONLINE (all paths). Default is an observe-
        # only stub that emits a diagnostic line so a supervisor
        # without a real Renderer wired (dev / test) still produces
        # a grep-able transition trail. dependencies.py wires the
        # SystemCardPublisher adapter around the RustRenderer on
        # production.
        self._system_card_publisher = system_card_publisher or self._default_system_card_publisher()
        # State: start from on-disk OR default SETUP.
        persisted = load_persisted_state(config.state_file)
        if persisted is not None:
            self._state = persisted.state
            self._last_sta_ssid = persisted.last_sta_ssid
            self._last_sta_channel = persisted.last_sta_channel
            self._takeover_active = persisted.takeover_active
            self._rollback_fired_at = persisted.rollback_fired_at
        else:
            self._state = SupervisorState.SETUP
            self._last_sta_ssid = None
            self._last_sta_channel = None
            self._takeover_active = False
            self._rollback_fired_at = None
        # 2026-07-01 (onboarding audit 4b): the DEGRADED card's
        # `variant` field (schema: "lost" | "auth_fail" |
        # "not_found_or_5ghz") lets the renderer surface WHY the sign
        # is off-net rather than a generic "wifi lost." Pre-fix the
        # supervisor hard-coded variant="lost" for every DEGRADED
        # publish; here we track the last STA-level reason so the
        # next DEGRADED render picks it up.
        #
        # None means "haven't seen a classifying event yet"; the
        # renderer falls back to "lost" copy in that case (see
        # `_system_card_params_for_state` below). Cleared on
        # STA_ASSOCIATED so a successful reconnect resets the story
        # -- a later drop doesn't inherit the prior reason.
        #
        # Not persisted across supervisor restarts: a reboot mid-
        # DEGRADED loses the specific reason + falls back to "lost",
        # which is the correct conservative story (we don't know
        # what caused it after a reboot).
        #
        # 2026-07-01 (audit 4b follow-up): the `not_found_or_5ghz`
        # variant's classifier now IS wired — wpa_supplicant's
        # CTRL-EVENT-NETWORK-NOT-FOUND (see parse_wpa_event above)
        # maps to STA_SSID_NOT_FOUND. On that event we consult the
        # last iw-scan band table below to decide "not_found" vs
        # "not_found_or_5ghz" for the DEGRADED card.
        self._last_degraded_variant: str | None = None
        # 2026-07-01 (audit 4b follow-up): per-SSID band table
        # populated from the last `iw dev wlan0 scan`. Values are
        # "2.4" | "5" — matching WifiNetwork.band. Cleared to {}
        # when no scan has run. `record_scan_bands()` overwrites
        # this whole dict per scan (a network that disappeared
        # should stop classifying).
        self._last_scan_bands: dict[str, str] = {}
        self._current_ap_channel: int | None = None
        self._current_sta_freq_mhz: int | None = None
        # P2 (2026-06-27): LINGER timer. monotonic timestamp set on
        # entry into LINGER; the observe loop calls
        # `check_linger_timeout()` each tick + fires
        # LINGER_TIMER_EXPIRED when (now - entered_at) >=
        # linger_seconds. Cleared on exit from LINGER.
        #
        # Sacred-review BLOCKER fix (PR2): if we resumed from disk
        # mid-LINGER (crashed/rebooted while in LINGER), seed the
        # entered-at to NOW so the grace window restarts from boot.
        # Without this seed, _linger_entered_at stays None and
        # check_linger_timeout() returns False forever — the
        # supervisor wedges in LINGER + the AP stays up until an
        # STA_DISCONNECTED bumps to DEGRADED, contradicting the
        # spec's "ONLINE is steady state" intent. Re-arming from
        # NOW is the safest interpretation: a fresh grace window,
        # not a stale one.
        self._linger_entered_at: float | None = (
            time.monotonic() if self._state == SupervisorState.LINGER else None
        )
        # P1.2-B (2026-06-10): one-shot hook fired on the FIRST
        # STA_ASSOCIATED after the take-over orchestrator installs
        # it. See set_takeover_associated_hook for the wiring
        # rationale.
        self._takeover_associated_hook: Callable[[], object] | None = None
        self._emit(
            "state_machine",
            "info",
            f"supervisor booted in state={self._state.value} "
            f"fallback_mutex_mode={config.fallback_mutex_mode}",
        )

    @property
    def current_state(self) -> SupervisorState:
        return self._state

    @property
    def current_sta_freq_mhz(self) -> int | None:
        return self._current_sta_freq_mhz

    @property
    def current_ap_channel(self) -> int | None:
        return self._current_ap_channel

    @property
    def last_sta_ssid(self) -> str | None:
        """SSID of the last STA (station-mode) association, or None if the
        device hasn't connected to a WiFi network this session. Read by
        the boot identity card (app.py) to show which network to join to
        reach the sign's URL."""
        return self._last_sta_ssid

    # P1.2-B take-over state accessors / mutators -----------------

    @property
    def takeover_active(self) -> bool:
        """True iff the supervisor has executed a successful take-over
        flip in this session OR resumed from a persisted state where
        a prior take-over was active. The take-over orchestrator
        consults this to skip re-attempting; the observe loop uses
        it to install the active actuator vs the observe-only stub.
        """
        return self._takeover_active

    @property
    def rollback_fired_at(self) -> float | None:
        """time.monotonic() at the moment the rollback watchdog last
        fired, or None if no rollback has ever fired in the persisted
        history. The orchestrator uses this to enforce a cooldown
        window before re-attempting take-over."""
        return self._rollback_fired_at

    def set_takeover_active(self, active: bool) -> None:
        """Called by the take-over orchestrator on a successful flip
        (STA associated within the watchdog window) or on rollback.
        Persists immediately so a backend crash mid-window doesn't
        lose the marker."""
        self._takeover_active = active
        if active:
            # Successful take-over implicitly clears the rollback
            # marker — the cooldown is satisfied.
            self._rollback_fired_at = None
        self._emit(
            "takeover",
            "info",
            f"takeover_active={active} rollback_fired_at={self._rollback_fired_at}",
        )
        self._persist()

    def set_takeover_associated_hook(self, hook: Callable[[], object] | None) -> None:
        """P1.2-B (2026-06-10): the take-over orchestrator
        installs a one-shot hook here after a successful flip; the
        supervisor fires it on the FIRST STA_ASSOCIATED event after
        installation, then clears the slot so it never re-fires.

        Sacred-review BLOCKER fix: without this wiring,
        `mark_associated` was defined but unreachable, so the
        rollback watchdog would fire 3 min after a SUCCESSFUL flip
        because the supervisor never told the orchestrator that STA
        had associated. The hook closes that loop.

        `hook` may be sync OR async; the supervisor handles both:
        sync hooks are invoked directly; async hooks are scheduled
        via `asyncio.create_task` on the running event loop. Passing
        `None` clears the slot (used by rollback to disarm the
        association hook).
        """
        self._takeover_associated_hook = hook

    def set_channel_follow_actuator(
        self,
        actuator: Callable[[ChannelFollowDecision], None],
    ) -> None:
        """Replace the channel-follow actuator at runtime. P1.2-B uses
        this to swap the observe-only stub for the active
        `HostapdChannelActuator` after a successful take-over flip.

        Per the actuator contract documented on `apply_sta_freq`:
        actuators MUST return normally on success and raise on
        failure. The new actuator takes effect on the NEXT
        apply_sta_freq call.
        """
        self._channel_follow_actuator = actuator
        self._emit(
            "channel_follow",
            "info",
            f"actuator replaced (class={actuator.__class__.__name__})",
        )

    def set_power_save_actuator(
        self,
        actuator: Callable[[], None],
    ) -> None:
        """Replace the power-save-off actuator at runtime. P1.3
        (2026-06-27) uses this so dependencies.py can wire the
        netctl-driven actuator only on hosts where the daemon
        socket is present; tests + dev hosts without the daemon
        keep the observe-only stub.

        The new actuator takes effect on the NEXT STA_ASSOCIATED
        event. Contract: actuators MUST return normally on success
        and raise on failure; the supervisor catches the exception
        and emits a warn diagnostic.
        """
        self._power_save_actuator = actuator
        self._emit(
            "power_save",
            "info",
            f"actuator replaced (class={actuator.__class__.__name__})",
        )

    def mark_rollback_fired(self) -> None:
        """Called by the orchestrator's rollback() — records that a
        rollback just fired so the cooldown check at evaluate_
        preconditions returns False until the cooldown window
        elapses."""
        self._takeover_active = False
        self._rollback_fired_at = time.monotonic()
        self._emit(
            "takeover",
            "warn",
            f"rollback fired at monotonic={self._rollback_fired_at:.1f}; "
            "takeover_active=False persisted",
        )
        self._persist()

    def snapshot_diagnostics(self) -> list[DiagnosticEvent]:
        return self.diagnostics.snapshot()

    def _emit(self, source: str, severity: str, message: str) -> None:
        """P1.2-A (2026-06-10): dual-emit helper. Pushes to the
        diagnostics ring buffer (for the API endpoint) AND emits a
        parseable line to the Python logger (for journalctl grep
        during observe-only soak).

        Line shape on the journal:
            [network-supervisor] source=<src> severity=<sev> message=<msg>

        QA's grep pattern: `journalctl -u openmarquee-backend |
        grep '\\[network-supervisor\\]'`.

        Severity routing: info -> log.info, warn -> log.warning,
        error -> log.error. Unknown severities fall through to
        log.info.
        """
        self.diagnostics.push(source, severity, message)
        line = f"[network-supervisor] source={source} severity={severity} message={message}"
        if severity == "warn":
            log.warning(line)
        elif severity == "error":
            log.error(line)
        else:
            log.info(line)

    def apply_event(self, event: SupervisorEvent) -> SupervisorState:
        """Drive the state machine. Records the (event, state-in,
        state-out) tuple in the ring buffer + persists the new state.

        Returns the (possibly unchanged) current state. Unrecognised
        (state, event) pairs are logged + ignored.
        """
        # P1.3 (2026-06-27): fire the power-save-off re-fire BEFORE
        # the state-machine transition so it runs on EVERY
        # STA_ASSOCIATED event regardless of whether the transition
        # itself defines a (state, event) edge. brcmfmac resets
        # power_save on every reassociation; the supervisor's
        # responsibility is to put it back, not to be opinionated
        # about which states the spec considers "fresh."
        if event == SupervisorEvent.STA_ASSOCIATED:
            self._fire_power_save_on_assoc()
        # 2026-07-01 (onboarding audit 4b): record the STA-level
        # reason so the NEXT DEGRADED card publish surfaces the
        # specific variant. See `_last_degraded_variant` in
        # __init__ for the full rationale + limitations.
        if event == SupervisorEvent.STA_AUTH_FAILED:
            self._last_degraded_variant = "auth_fail"
        elif event == SupervisorEvent.STA_DISCONNECTED:
            self._last_degraded_variant = "lost"
        elif event == SupervisorEvent.STA_ASSOCIATED:
            # Successful reconnect resets the story so a later drop
            # doesn't inherit the prior reason.
            self._last_degraded_variant = None
        elif event == SupervisorEvent.STA_SSID_NOT_FOUND:
            # 2026-07-01 (audit 4b follow-up): the SSID isn't in the
            # scan. If the last iw-scan saw the target SSID *only* on
            # 5 GHz, surface the "not_found_or_5ghz" variant so the
            # card explains the 5-GHz-only case; otherwise fall
            # back to the plain "not_found" copy.
            target = self._last_sta_ssid
            band = self._last_scan_bands.get(target) if target is not None else None
            self._last_degraded_variant = "not_found_or_5ghz" if band == "5" else "not_found"
        elif event == SupervisorEvent.STA_SCAN_FAILED:
            # Scan itself failed (driver / firmware). Nothing to
            # classify — keep whatever variant was last recorded and
            # let the diagnostics ring buffer carry the failure detail.
            pass
        new_state = next_state(self._state, event, fallback_mutex=self.config.fallback_mutex_mode)
        if new_state is None:
            self._emit(
                "state_machine",
                "info",
                f"no transition for event={event.value} from state={self._state.value} (ignored)",
            )
            return self._state
        prev = self._state
        self._state = new_state
        self._emit(
            "state_machine",
            "info",
            f"transition {prev.value} -> {new_state.value} on event={event.value}",
        )
        # P2 (2026-06-27): transition-effect dispatch. Fires AP
        # up/down + arms/disarms the LINGER timer. Runs BEFORE
        # _persist so the side-effects either succeed or the warn
        # diagnostic is already in the ring buffer for QA.
        self._on_transition(prev, new_state)
        self._persist()
        # P1.2-B: fire the take-over associated hook on the first
        # STA_ASSOCIATED event after a successful flip. One-shot:
        # clear the slot immediately so subsequent associations
        # don't re-trigger the orchestrator's bookkeeping.
        if event == SupervisorEvent.STA_ASSOCIATED and self._takeover_associated_hook is not None:
            hook = self._takeover_associated_hook
            self._takeover_associated_hook = None
            try:
                result = hook()
            except Exception as e:  # noqa: BLE001 — log + continue
                self._emit(
                    "takeover",
                    "warn",
                    f"takeover_associated_hook raised synchronously: {e!r}",
                )
            else:
                # If the hook is async, schedule it on the running
                # loop. Test environments without a loop see a
                # RuntimeError and we degrade silently — tests that
                # care about the hook firing assert it directly.
                import inspect as _inspect

                if _inspect.isawaitable(result):
                    import asyncio as _asyncio

                    try:
                        _asyncio.get_running_loop().create_task(result)
                    except RuntimeError:
                        # No running loop (sync test context). Drive
                        # the coroutine to completion synchronously
                        # via asyncio.run so the hook side-effect
                        # still applies.
                        _asyncio.run(result)
        return self._state

    def record_scan_bands(self, bands: dict[str, str]) -> None:
        """2026-07-01 (audit 4b follow-up): record the per-SSID band
        table from the most recent iw-scan (see
        ``api_system._parse_iw_scan``). Overwrites the previous
        table wholesale so a network that stopped broadcasting no
        longer classifies. When STA_SSID_NOT_FOUND fires next, the
        classifier reads from here to decide "not_found" vs
        "not_found_or_5ghz" for the DEGRADED card.

        No wiring today calls this — the follow-up PR that lands
        wpa_supplicant SCAN-RESULTS ingestion (or, more cheaply, a
        periodic /api/system/wifi-scan poll) is what will feed it.
        Kept as a public method so that follow-up is a one-line
        integration.
        """
        self._last_scan_bands = dict(bands)

    def record_target_ssid(self, ssid: str | None) -> None:
        """2026-07-02 (PR #30 review FIX-FIRST): stamp the target
        SSID onto `_last_sta_ssid` at credential-submit time so a
        subsequent CONNECTING -> SETUP transition renders the
        classified reason banner WITH the network name (and so the
        band lookup keys on a real SSID instead of None, which was
        making the 5GHz classification unreachable on any never-
        connected device).

        Called from api_onboarding.submit_credentials before
        firing HAS_STORED_CREDENTIALS. Idempotent: passing None
        clears the field (matches the fresh-boot semantic).

        The observe loop's parse_wpa_event fields also carry the
        active SSID once wpa_supplicant reports it; that path
        remains as a belt-and-suspenders update (a re-scan mid-
        session can still refresh the field), but the submit-
        time write is what makes the reason banner name the
        network on a first-time setup.
        """
        self._last_sta_ssid = ssid

    def probe_and_seed_from_existing_connection(
        self,
        probe_fn: Callable[[], str | None] | None = None,
    ) -> bool:
        """2026-07-02 (Jason handover, qarl handover-critical): if
        wlan0 is already associated to a real home wifi at supervisor
        startup (pre-staged via nmcli / an existing NM profile
        BEFORE onboarding shipped, NOT through the portal submit
        flow), seed the state machine to ONLINE directly.

        Without this, a pre-staged device boots to SETUP, paints a
        dead PIN/QR card (take-over is off so no AP is up), and
        never advances — because the supervisor only tracks wifi
        arrived through its own onboarding submit event chain.
        qarl 2026-07-02: 'treat "wlan0 already connected to a home
        network" as "online, no onboarding needed," regardless of
        whether that connection came from onboarding or was pre-
        configured.'

        Idempotent + safe to re-fire: only seeds when the current
        state is SETUP + the probe returns an SSID. If already
        past SETUP (persisted ONLINE from a previous boot,
        supervisor is mid-CONNECTING, etc.) this is a no-op.
        Every boot re-runs the probe so a pre-staged connection
        that came up AFTER a prior boot's SETUP state also
        transitions correctly.

        The probe fires the SAME event chain as the onboarding
        portal (HAS_STORED_CREDENTIALS → CONNECTING → STA_
        ASSOCIATED → LINGER/ONLINE) so every transition-effect
        hook (AP teardown on ONLINE, system-card clear, etc.)
        runs as normal. LINGER is short-circuited to ONLINE
        immediately (no 2-minute grace window is needed for a
        connection that was already established before boot).

        `probe_fn` is injectable so tests exercise the seed path
        without shelling out to nmcli. Defaults to the module-
        level `_probe_existing_wifi_connection` helper.

        Returns True iff a seed happened, else False.
        """
        if self._state != SupervisorState.SETUP:
            return False
        probe = probe_fn or _probe_existing_wifi_connection
        try:
            ssid = probe()
        except Exception:
            log.exception("pre-staged wifi probe raised")
            return False
        if not ssid:
            return False
        self._emit(
            "state_machine",
            "info",
            f"pre-staged wifi detected: wlan0 already on ssid={ssid!r}; "
            "seeding SETUP -> ONLINE without onboarding submit "
            "(qarl 2026-07-02 handover-critical: skip dead SETUP card "
            "when wifi is already up)",
        )
        self.record_target_ssid(ssid)
        # Synthesize the credential + association chain so every
        # transition-effect hook (AP teardown on ONLINE, system-card
        # clear, etc.) fires exactly as if the operator had come
        # through the portal.
        self.apply_event(SupervisorEvent.HAS_STORED_CREDENTIALS)
        self.apply_event(SupervisorEvent.STA_ASSOCIATED)
        # Concurrent regime (default) lands in LINGER; short-circuit
        # to ONLINE — the 2-min grace window is meant to hold the
        # portal open long enough for the phone to see the confirmation
        # page, and a pre-staged device has no portal to hold.
        if self._state == SupervisorState.LINGER:
            self.apply_event(SupervisorEvent.LINGER_TIMER_EXPIRED)
        return True

    def apply_sta_freq(self, freq_mhz: int) -> ChannelFollowDecision:
        """Record the STA's current frequency + ask the channel-
        follow engine for the AP-side decision. Calls the actuator
        if regeneration is needed; on the actuator's successful
        return (no exception) advances ``_current_ap_channel`` to
        the decision target so the NEXT decision settles instead of
        re-firing on every poll.

        The on-success advance is what prevents the P1.2-A soak's
        "regenerate_needed=True every 10 s" log spam + (in P1.2-B
        with a live hostapd actuator) prevents an infinite
        hostapd-restart loop. The contract for custom actuators:
        return normally on success; raise on failure so we don't
        advance the cached AP-channel optimistically.
        """
        self._current_sta_freq_mhz = freq_mhz
        chan = freq_to_channel(freq_mhz)
        if chan is not None:
            self._last_sta_channel = chan
        decision = decide_channel_follow(freq_mhz, self._current_ap_channel, fallback_channel=6)
        # P1.2-A: emit channel-follow decisions to the journal as
        # well as the ring buffer. QA reads these during the
        # observe-only soak to validate the decisions against
        # journalctl reality before greenlighting the take-over.
        self._emit(
            "channel_follow",
            "info",
            f"sta_freq_mhz={freq_mhz} sta_channel={chan} "
            f"current_ap_channel={self._current_ap_channel} "
            f"target_channel={decision.target_channel} "
            f"regenerate_needed={decision.regenerate_needed} reason={decision.reason}",
        )
        if decision.regenerate_needed:
            try:
                self._channel_follow_actuator(decision)
            except Exception as e:
                # Actuator failure means the AP is NOT on the new
                # channel; keep _current_ap_channel pointing at the
                # last known-good value so the next poll retries.
                # In P1.2-B with a real hostapd actuator this means
                # we re-attempt the restart on the next freq event
                # (the supervisor's DEGRADED branch handles the
                # broader recovery).
                self._emit(
                    "channel_follow",
                    "warn",
                    f"actuator failed (target_channel={decision.target_channel} "
                    f"reason={decision.reason}): {e!r}; will retry on next freq poll",
                )
            else:
                # On successful actuation, advance the cached AP
                # channel so subsequent polls see "already on
                # target" and skip the actuator. P1.2-A.1 fix
                # (QA's soak finding: without this, every 10 s
                # poll re-emits regenerate_needed=True).
                self._current_ap_channel = decision.target_channel
        return decision

    def _default_actuator(self, decision: ChannelFollowDecision) -> None:
        """Default channel-follow actuator: log only. P1.2-A keeps
        this as the OBSERVE-ONLY actuator — no subprocess + no
        hostapd config rewrite. P1.2-B's take-over commit wires
        this to actual hostapd-config rewrite + restart.

        Returns normally — `apply_sta_freq` interprets that as
        "actuation succeeded" and advances ``_current_ap_channel``.
        For the observe-only path the advance is a SIMULATION of
        success so QA's soak log doesn't repeat regenerate_needed
        every poll; the simulation is sound because we're not
        actually making any change the simulation could disagree
        with.

        The dual-emit makes the would-have-done decision visible in
        journalctl so QA can validate it BEFORE the take-over
        actuator goes active.
        """
        self._emit(
            "channel_follow",
            "info",
            f"actuator (observe-only): would regenerate hostapd.conf "
            f"channel={decision.target_channel} + restart hostapd "
            f"(reason={decision.reason})",
        )

    def _default_power_save_actuator(self) -> None:
        """Default power-save-off actuator: log only. dependencies.py
        wires the netctl-driven actuator on hosts where the daemon
        socket is present; tests + Mac dev keep this stub. The
        observe-only line lets QA confirm the supervisor IS handling
        STA_ASSOCIATED events even in non-production environments.
        """
        self._emit(
            "power_save",
            "info",
            "actuator (observe-only): would re-fire openmarquee-wifi-powersave-off.sh on wlan0+ap0",
        )

    def _fire_power_save_on_assoc(self) -> None:
        """P1.3 (2026-06-27) STA_ASSOCIATED -> re-fire power-save-off.
        Caller is apply_event; failures of the actuator are caught +
        downgraded to a warn diagnostic so a netctl outage never
        wedges the state-machine transition path.
        """
        try:
            self._power_save_actuator()
        except Exception as e:  # noqa: BLE001 — defensive: never wedge apply_event
            self._emit(
                "power_save",
                "warn",
                f"actuator failed on STA_ASSOCIATED: {e!r}; "
                "boot-oneshot setting remains in effect until next reassoc",
            )
        else:
            self._emit(
                "power_save",
                "info",
                "actuator fired on STA_ASSOCIATED (spec §A#2: brcmfmac "
                "resets power_save on reassoc)",
            )

    # --- P2 (2026-06-27) AP lifecycle + LINGER timer ---

    class _DefaultApLifecycleActuator:
        """Observe-only AP lifecycle actuator. Production hosts
        get HostapdLifecycleActuator wired via dependencies.py;
        tests + Mac dev keep this stub so the supervisor doesn't
        touch hostapd."""

        def __init__(self, supervisor: NetworkSupervisor):
            self._supervisor = supervisor

        def stop(self) -> None:
            self._supervisor._emit(  # noqa: SLF001 — paired with the supervisor
                "ap_lifecycle",
                "info",
                "actuator (observe-only): would stop hostapd",
            )

        def start(self) -> None:
            self._supervisor._emit(  # noqa: SLF001 — paired with the supervisor
                "ap_lifecycle",
                "info",
                "actuator (observe-only): would start hostapd",
            )

    def _default_ap_lifecycle_actuator(self) -> object:
        """Factory: a back-pointed observe-only stub. Needs a method
        because the inner class closes over `self` to emit
        diagnostics through the supervisor's ring buffer.
        """
        return NetworkSupervisor._DefaultApLifecycleActuator(self)

    def set_ap_lifecycle_actuator(self, actuator: object) -> None:
        """Replace the AP lifecycle actuator at runtime. Contract:
        `actuator.stop()` and `actuator.start()` are no-arg methods
        that return None on success and raise on failure.
        """
        self._ap_lifecycle_actuator = actuator
        self._emit(
            "ap_lifecycle",
            "info",
            f"actuator replaced (class={actuator.__class__.__name__})",
        )

    # --- PR3 (2026-06-27) system-card publisher ---

    class _DefaultSystemCardPublisher:
        """Observe-only system-card publisher. Production hosts get
        the SystemCardPublisher adapter around the RustRenderer;
        tests + dev-hosts without a Renderer keep this stub so
        transitions still generate a grep-able diagnostic trail.
        """

        def __init__(self, supervisor: NetworkSupervisor):
            self._supervisor = supervisor

        def render(self, params: dict) -> None:
            self._supervisor._emit(  # noqa: SLF001 — paired with the supervisor
                "system_card",
                "info",
                f"publisher (observe-only): would render kind={params.get('kind')}",
            )

        def clear(self) -> None:
            self._supervisor._emit(  # noqa: SLF001 — paired with the supervisor
                "system_card",
                "info",
                "publisher (observe-only): would clear",
            )

    def _default_system_card_publisher(self) -> object:
        """Factory: back-pointed observe-only stub. Same pattern as
        _default_ap_lifecycle_actuator."""
        return NetworkSupervisor._DefaultSystemCardPublisher(self)

    def set_system_card_publisher(self, publisher: object) -> None:
        """Replace the system-card publisher at runtime. Contract:
        `publisher.render(params: dict)` and `publisher.clear()` —
        both return None on success and raise on failure.
        Failures are caught + downgraded to a warn diagnostic; the
        supervisor never wedges on a renderer outage.
        """
        self._system_card_publisher = publisher
        self._emit(
            "system_card",
            "info",
            f"publisher replaced (class={publisher.__class__.__name__})",
        )

    def _on_transition(self, prev: SupervisorState, new: SupervisorState) -> None:
        """P2 (2026-06-27) transition-effect dispatch.

        Spec §"Onboarding state machine": ONLINE is the default
        steady state with the AP torn down ("the setup network is
        a temporary door, not a permanent feature"). DEGRADED and
        SETUP need the AP up for recovery / portal access. So:
          * Entering ONLINE from non-ONLINE: stop hostapd.
          * Exiting ONLINE to non-ONLINE: start hostapd.

        Also arm/disarm the LINGER grace-window timer. The observe
        loop polls check_linger_timeout() each tick to fire
        LINGER_TIMER_EXPIRED when the window elapses.

        All actuator failures are caught + downgraded to a warn
        diagnostic; the state machine never wedges on a netctl
        outage.
        """
        # AP lifecycle. QA cross-lane review (PR2 NIT N1): the
        # entering-ONLINE branch must be gated on STA actually being
        # associated, otherwise the unwired SETUP_MODE_TIMER_EXPIRED
        # edge (SETUP -> ONLINE) would tear down the AP without an
        # STA association = user stranded. The two edges where STA
        # is definitely up are LINGER->ONLINE (concurrent regime,
        # STA just associated + grace expired) and CONNECTING/
        # DEGRADED->ONLINE in the mutex regime (STA just
        # associated). SETUP->ONLINE is the explicit non-STA edge —
        # the auto-off timer brings the user back from manual setup
        # mode but the radio hasn't reassociated; the supervisor
        # must NOT tear down the AP.
        sta_up_into_online = new == SupervisorState.ONLINE and prev in (
            SupervisorState.LINGER,
            SupervisorState.CONNECTING,
            SupervisorState.DEGRADED,
        )
        if sta_up_into_online:
            self._fire_ap_teardown(prev=prev)
        elif prev == SupervisorState.ONLINE and new != SupervisorState.ONLINE:
            self._fire_ap_reup(new=new)
        # LINGER timer.
        if new == SupervisorState.LINGER and prev != SupervisorState.LINGER:
            self._linger_entered_at = time.monotonic()
            self._emit(
                "linger_timer",
                "info",
                f"armed linger timer ({self.config.linger_seconds:.0f}s)",
            )
        elif prev == SupervisorState.LINGER and new != SupervisorState.LINGER:
            self._linger_entered_at = None
            self._emit("linger_timer", "info", "disarmed linger timer (left LINGER)")

        # PR3 (2026-06-27) system-card overlay. Every path to ONLINE
        # clears the card (spec §"Onboarding state machine": ONLINE
        # is the AP-off / no-overlay steady state). Every other
        # transition renders the matching card. Failures are caught
        # + downgraded to a warn diagnostic so a renderer outage
        # never wedges the state machine.
        self._fire_system_card_for_transition(prev=prev, new=new)

    def _fire_system_card_for_transition(
        self, *, prev: SupervisorState, new: SupervisorState
    ) -> None:
        """PR3 (2026-06-27) — publish a RenderSystemCard / ClearSystemCard
        matching the new state:
            SETUP      → SETUP card (portal QR + PIN)
            CONNECTING → CONNECTING card (joining <target_ssid>…)
            LINGER     → CONNECTED card (address + IP, ~2min ttl)
            ONLINE     → clear (spec: AP-off steady state)
            DEGRADED   → DEGRADED card (variant defaults to `lost`;
                         preview endpoint can render more specific
                         variants)
        """
        # Skip re-render when the state didn't actually change
        # (defensive: callers shouldn't fire _on_transition without
        # a real change, but a no-op guard costs nothing).
        if prev == new:
            return
        try:
            if new == SupervisorState.ONLINE:
                self._system_card_publisher.clear()
                self._emit(
                    "system_card",
                    "info",
                    f"cleared on {prev.value}->ONLINE (spec: ONLINE = AP off, no overlay)",
                )
                return
            params = self._system_card_params_for_state(new)
            self._system_card_publisher.render(params)
            self._emit(
                "system_card",
                "info",
                f"rendered kind={params['kind']} on {prev.value}->{new.value}",
            )
        except Exception as e:  # noqa: BLE001 — never wedge apply_event
            self._emit(
                "system_card",
                "warn",
                f"publisher failed on {prev.value}->{new.value}: {e!r}; "
                "overlay may be stale until the next transition",
            )

    def _system_card_params_for_state(self, new: SupervisorState) -> dict:
        """PR3 (2026-06-27) — build the RenderSystemCard params for a
        state. Kept small: the supervisor doesn't hold the mDNS
        name / IP / per-boot PIN in this MVP — the auth-gated
        preview endpoint (POST /api/system/render-system-card-preview)
        is where QA drives cards with rich fields. In production the
        renderer's per-kind layout falls back to sensible defaults
        (e.g. SETUP card shows the mockup's default headline copy)
        when fields are absent.
        """
        if new == SupervisorState.SETUP:
            # 2026-07-02 (audit 4b close-out): thread the classified
            # variant onto the SETUP card too. The state machine
            # routes CONNECTING → SETUP on STA_AUTH_FAILED or
            # STA_SSID_NOT_FOUND, so the SETUP card is where the
            # operator lands after a first-connect failure — the
            # renderer picks up `variant` + `target_ssid` and
            # paints a reason banner on the on-glass card. When no
            # variant is set (fresh boot, no prior failure), the
            # card renders as it did before (no banner).
            # 2026-07-07: thread the REAL setup-AP join credentials
            # (ssid = hostname, pin = the live WPA2 passphrase) so the
            # card shows the actual network + password instead of the
            # renderer's hardcoded `openMarquee-Setup` / `----`
            # placeholders. `setup_card_credentials` is a leaf helper
            # (fail-soft) so it can't wedge a transition.
            from openmarquee.setup_card import setup_card_credentials

            params: dict = {"kind": "SETUP", **setup_card_credentials()}
            if self._last_degraded_variant is not None:
                params["variant"] = self._last_degraded_variant
                if self._last_sta_ssid is not None:
                    params["target_ssid"] = self._last_sta_ssid
            return params
        if new == SupervisorState.CONNECTING:
            return {
                "kind": "CONNECTING",
                "target_ssid": self._last_sta_ssid,
            }
        if new == SupervisorState.LINGER:
            # ttl matches the LINGER grace window (2min) so the
            # renderer auto-clears if the supervisor is somehow
            # unable to emit ClearSystemCard on the LINGER→ONLINE
            # edge. Belt-and-suspenders with the max-lifetime cap.
            return {
                "kind": "CONNECTED",
                "address": mdns_url(),
                "ttl_ms": int(self.config.linger_seconds * 1000),
            }
        if new == SupervisorState.DEGRADED:
            # 2026-07-01 (audit 4b + follow-up): thread the actual
            # recorded disconnect reason from
            # `_last_degraded_variant` so the renderer picks the
            # specific variant (auth_fail / not_found /
            # not_found_or_5ghz vs the generic lost). None -> "lost"
            # default matches the renderer's fallback (system_card.rs
            # unknown_degraded_variant_falls_back_to_lost_copy test)
            # so a variant-less transition still renders sensible
            # copy.
            # 2026-07-07: the DEGRADED card also instructs "join <ssid>,
            # PIN <pin>" to rejoin the setup AP, so thread the SAME real
            # credentials + WiFi-join QR as the SETUP card (they share the
            # renderer's placeholder fallbacks otherwise).
            from openmarquee.setup_card import setup_card_credentials

            return {
                "kind": "DEGRADED",
                "variant": self._last_degraded_variant or "lost",
                "target_ssid": self._last_sta_ssid,
                **setup_card_credentials(),
            }
        # Should be unreachable — every non-ONLINE state maps above.
        return {"kind": "SETUP"}

    def _fire_ap_teardown(self, *, prev: SupervisorState) -> None:
        try:
            self._ap_lifecycle_actuator.stop()
        except Exception as e:  # noqa: BLE001
            self._emit(
                "ap_lifecycle",
                "warn",
                f"stop actuator failed on {prev.value}->ONLINE: {e!r}; "
                "AP remains up — concurrent AP+STA continues until "
                "the next stop attempt or reboot",
            )
        else:
            self._emit(
                "ap_lifecycle",
                "info",
                f"stopped hostapd on {prev.value}->ONLINE (spec "
                f'§"Onboarding state machine": ONLINE=AP off)',
            )

    def _fire_ap_reup(self, *, new: SupervisorState) -> None:
        try:
            self._ap_lifecycle_actuator.start()
        except Exception as e:  # noqa: BLE001
            self._emit(
                "ap_lifecycle",
                "warn",
                f"start actuator failed on ONLINE->{new.value}: {e!r}; "
                "AP remains down — recovery path is unavailable until "
                "the next start attempt or operator reboot",
            )
        else:
            self._emit(
                "ap_lifecycle",
                "info",
                f"started hostapd on ONLINE->{new.value} (recovery path now available)",
            )

    @property
    def linger_entered_at(self) -> float | None:
        """time.monotonic() at LINGER entry, or None when not in
        LINGER. Read by the observe loop's check_linger_timeout
        gate."""
        return self._linger_entered_at

    def check_linger_timeout(self, *, now: float | None = None) -> bool:
        """Called by the observe loop each tick. Returns True iff
        the LINGER grace window has elapsed and the loop should
        fire LINGER_TIMER_EXPIRED. Idempotent: subsequent calls
        return False until the state machine actually transitions
        out of LINGER (which clears `_linger_entered_at`).

        `now` is `time.monotonic()` by default; tests override for
        deterministic timing without sleeps.
        """
        if self._linger_entered_at is None:
            return False
        if self._state != SupervisorState.LINGER:
            # Defensive: state changed without clearing the
            # entered_at (shouldn't happen, but if it does we don't
            # want to fire the timer based on a stale stamp).
            return False
        ref = now if now is not None else time.monotonic()
        elapsed = ref - self._linger_entered_at
        return elapsed >= self.config.linger_seconds

    def _persist(self) -> None:
        try:
            save_persisted_state(
                PersistedState(
                    state=self._state,
                    last_sta_ssid=self._last_sta_ssid,
                    last_sta_channel=self._last_sta_channel,
                    last_transition_monotonic=time.monotonic(),
                    takeover_active=self._takeover_active,
                    rollback_fired_at=self._rollback_fired_at,
                ),
                path=self.config.state_file,
            )
        except OSError as e:
            log.warning("network-supervisor: state persistence failed (%s)", e)
            self.diagnostics.push(
                "state_machine",
                "warn",
                f"state persistence failed: {e}",
            )

    # ----- FastAPI lifecycle hooks -----

    async def lifespan_start(self) -> None:
        """Called from FastAPI's lifespan startup. P1.2-A: the
        actual wpa_supplicant polling loop now runs in
        `network_supervisor_loop.supervisor_observe_loop` as a
        separate asyncio task — app.py spawns it; this hook just
        announces the lifecycle boundary in the journal.
        """
        self._emit(
            "state_machine",
            "info",
            "lifespan_start (P1.2-A: observe-only; wpa_supplicant polling "
            "live + channel-follow actuator stubbed)",
        )

    async def lifespan_stop(self) -> None:
        self._emit("state_machine", "info", "lifespan_stop")


# ============================================================
# Public helpers re-exported for the API + the tests
# ============================================================


def supervisor_to_dict(supervisor: NetworkSupervisor) -> dict:
    """Render the supervisor's state for the API. Pure function on
    the supervisor's public surface; no IO."""
    now = time.monotonic()
    return {
        "state": supervisor.current_state.value,
        "current_sta_freq_mhz": supervisor.current_sta_freq_mhz,
        "current_ap_channel": supervisor.current_ap_channel,
        "fallback_mutex_mode": supervisor.config.fallback_mutex_mode,
        "diagnostics": [ev.to_dict(now=now) for ev in supervisor.snapshot_diagnostics()],
    }
