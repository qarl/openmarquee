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
import socket
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)

# ============================================================
# Constants — match the spec + the existing system/ configs.
# ============================================================

# 5-min sliding window for the diagnostics ring buffer (per spec
# §"Diagnostics" — "captured continuously to a ring buffer").
DIAGNOSTICS_WINDOW_SECONDS = 300.0

# Path the supervisor uses to persist last-known state across reboots
# (state machine resumes at the right entry point after a power cycle).
# /var/lib/openmarquee/ is the canonical openMarquee mutable state dir.
DEFAULT_STATE_FILE = Path("/var/lib/openmarquee/network-state.json")

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


class SupervisorState(str, Enum):
    """Product-level state machine per spec §"Onboarding state machine".

    Values are stringly-typed so JSON serialisation (for the state
    file + API responses + ring-buffer events) is human-readable.
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


class SupervisorEvent(str, Enum):
    """External events that drive state transitions."""

    HAS_STORED_CREDENTIALS = "HAS_STORED_CREDENTIALS"
    NO_STORED_CREDENTIALS = "NO_STORED_CREDENTIALS"
    STA_ASSOCIATED = "STA_ASSOCIATED"
    STA_AUTH_FAILED = "STA_AUTH_FAILED"
    STA_DISCONNECTED = "STA_DISCONNECTED"
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
    """

    state: SupervisorState
    last_sta_ssid: str | None = None
    last_sta_channel: int | None = None
    last_transition_monotonic: float | None = None
    boot_counter_consumed: bool = False
    schema_version: int = 1


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
    return None, fields


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
    ):
        self.config = config
        self.diagnostics = diagnostics or DiagnosticsRingBuffer()
        self._channel_follow_actuator = channel_follow_actuator or self._default_actuator
        # State: start from on-disk OR default SETUP.
        persisted = load_persisted_state(config.state_file)
        if persisted is not None:
            self._state = persisted.state
            self._last_sta_ssid = persisted.last_sta_ssid
            self._last_sta_channel = persisted.last_sta_channel
        else:
            self._state = SupervisorState.SETUP
            self._last_sta_ssid = None
            self._last_sta_channel = None
        self._current_ap_channel: int | None = None
        self._current_sta_freq_mhz: int | None = None
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
        self._persist()
        return self._state

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

    def _persist(self) -> None:
        try:
            save_persisted_state(
                PersistedState(
                    state=self._state,
                    last_sta_ssid=self._last_sta_ssid,
                    last_sta_channel=self._last_sta_channel,
                    last_transition_monotonic=time.monotonic(),
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
