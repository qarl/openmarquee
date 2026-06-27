"""P1.2-A observe-only loop for the network supervisor.

Runs as a single asyncio task under the FastAPI lifespan. Polls the
wpa_supplicant control socket for unsolicited events, periodically
queries `iw dev wlan0 info` for the current STA frequency, and
drives the supervisor's state machine + channel-follow engine.

**OBSERVE-ONLY.** The supervisor's default actuator only writes to
the diagnostics ring buffer + emits a parseable `[network-supervisor]
decision` line via the Python logger. No subprocess (e.g.
`hostapd` restart, `nmcli` connection switch) ever runs from this
loop in P1.2-A. The take-over moment ships as a separate commit
(P1.2-B) with its own pre-flight checklist.

Cross-platform safety: on Mac dev (no /var/run/wpa_supplicant/wlan0,
no `iw` binary), socket-connect + subprocess calls fail gracefully.
The loop logs the absence once + sleeps + retries — the state
machine still runs against persisted/default state so the API
remains responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil

from openmarquee.network_supervisor import (
    NetworkSupervisor,
    WpaSupplicantSocketClient,
    parse_wpa_event,
)

log = logging.getLogger(__name__)

# How often to poll the wpa_supplicant control socket for events.
# DGRAM recv with 500ms timeout is cheap; tighter cadence = lower
# event-to-state-transition latency.
WPA_POLL_INTERVAL_S = 0.5

# How often to poll `iw dev wlan0 info` for the STA frequency. STA
# frequency only changes on association / disassociation / router
# CSA — every 10s is plenty. Best-effort: on hosts without the `iw`
# binary, the call no-ops.
STA_FREQ_POLL_INTERVAL_S = 10.0

# Cooldown between "couldn't connect to wpa_supplicant" log messages
# so a Mac dev box doesn't spam stderr every poll cycle.
WPA_CONNECT_RETRY_INTERVAL_S = 30.0


def parse_iw_freq_mhz(iw_output: str) -> int | None:
    """Extract `channel <N> (freq <MHZ>)` -> MHZ from `iw dev wlan0
    info` output. Returns None if the channel/freq line is absent
    (no association, or `iw` returned an error string).

    Canonical line shape:
        channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz

    Pure function; testable on every host without `iw`.
    """
    # Look for `(freq MHz)` shape with a captured integer.
    match = re.search(r"\(\s*(\d{4})\s*MHz\s*\)", iw_output)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_iw_list_valid_combinations(iw_list_output: str) -> str | None:
    """P1.3 (2026-06-27) extract the `valid interface combinations:`
    block from `iw list` output. Spec §Diagnostics:
    "should show `#{ managed } <= 1, #{ AP } <= 1` (combined). If a
    firmware update changed this, everything above is moot."

    Canonical block (one or more indented lines beginning with `* `):

        valid interface combinations:
                 * #{ managed } <= 1, #{ AP } <= 1, ...,
                   total <= 4, #channels <= 1

    Returns the joined block (stripped of trailing whitespace per
    line, joined with spaces so the journal line is one-row) or
    None if the section is absent.

    Pure function; testable without `iw`.
    """
    lines = iw_list_output.splitlines()
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if "valid interface combinations:" in line:
            header_idx = i
            break
    if header_idx is None:
        return None
    # Capture indented continuation lines until we hit a line whose
    # leading indent is shallower than the first continuation line.
    body: list[str] = []
    first_indent: int | None = None
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            # Blank lines inside the block are tolerated by iw's
            # formatter but rare; treat them as terminators if we
            # haven't captured anything yet, otherwise end the block.
            if body:
                break
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if first_indent is None:
            first_indent = indent
        elif indent < first_indent:
            break
        body.append(stripped)
    if not body:
        return None
    return " ".join(body)


async def poll_iw_list_combos() -> str | None:
    """Best-effort: shell out to `iw list`, parse the valid-
    combinations block. Cross-platform: FileNotFoundError on Mac
    dev returns None.

    Note: `iw list` is heavy (PHY enumeration on every call) but
    we only run it ONCE at supervisor-loop startup so the cost is
    fine.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw",
            "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except TimeoutError:
            log.debug("network-supervisor: iw list timed out")
            with contextlib.suppress(Exception):
                proc.kill()
            return None
        if proc.returncode != 0:
            return None
        return parse_iw_list_valid_combinations(stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return None
    except Exception:
        log.debug("network-supervisor: iw list failed", exc_info=True)
        return None


async def poll_sta_freq_mhz() -> int | None:
    """Best-effort: shell out to `iw dev wlan0 info`, parse the freq
    field, return MHz or None. Cross-platform: FileNotFoundError on
    Mac dev returns None; the supervisor's apply_sta_freq is
    skipped that tick.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "iw",
            "dev",
            "wlan0",
            "info",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except TimeoutError:
            log.debug("network-supervisor: iw poll timed out")
            with contextlib.suppress(Exception):
                proc.kill()
            return None
        if proc.returncode != 0:
            return None
        return parse_iw_freq_mhz(stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        # No `iw` binary -- Mac dev. Logged once by the outer loop
        # on first occurrence.
        return None
    except Exception:
        log.debug("network-supervisor: iw poll failed", exc_info=True)
        return None


async def supervisor_observe_loop(
    supervisor: NetworkSupervisor,
    *,
    wpa_poll_interval_s: float = WPA_POLL_INTERVAL_S,
    sta_freq_poll_interval_s: float = STA_FREQ_POLL_INTERVAL_S,
) -> None:
    """Long-running asyncio task. Wires wpa_supplicant events +
    periodic STA-freq polls into the supervisor.

    Loop body:
      * Tries to maintain a `WpaSupplicantSocketClient` connection.
        Connect failures (Mac dev / wpa_supplicant not running)
        retry every WPA_CONNECT_RETRY_INTERVAL_S; the loop keeps
        polling STA freq + sleeping the wpa-poll interval.
      * On each tick: receive_event() (non-blocking with 500ms
        timeout inside the socket), parse, drive supervisor.
      * Every STA_FREQ_POLL_INTERVAL_S ticks: poll iw freq + drive
        supervisor.apply_sta_freq.

    Cancellation: caller cancels the asyncio.Task at lifespan
    shutdown. The loop catches CancelledError, closes the socket
    client, and exits.
    """
    client: WpaSupplicantSocketClient | None = None
    next_connect_attempt: float = 0.0
    next_sta_poll: float = 0.0
    loop = asyncio.get_event_loop()
    has_logged_missing_socket = False
    has_logged_missing_iw = False

    log.info(
        "network-supervisor: observe-only loop starting (wpa_poll=%.2fs sta_poll=%.2fs)",
        wpa_poll_interval_s,
        sta_freq_poll_interval_s,
    )

    # P1.3 (2026-06-27) one-shot boot-time diagnostic: dump the
    # firmware's valid interface combinations so a regression in
    # `#{ managed } <= 1, #{ AP } <= 1` is visible in the journal
    # the moment the supervisor wakes up. Spec §Diagnostics: "If
    # a firmware update changed this, everything above is moot."
    combos = await poll_iw_list_combos()
    if combos is not None:
        supervisor.diagnostics.push("hostapd", "info", f"iw_list_combos={combos}")
        log.info(
            "[network-supervisor] source=hostapd severity=info message=iw_list_combos %s",
            combos,
        )
    else:
        supervisor.diagnostics.push(
            "hostapd",
            "info",
            "iw_list_combos=unavailable (iw missing or returned no combos)",
        )

    try:
        while True:
            now = loop.time()
            # 1. Maintain wpa_supplicant socket.
            if client is None and now >= next_connect_attempt:
                try:
                    candidate = WpaSupplicantSocketClient(
                        ctrl_path=supervisor.config.wpa_ctrl_path,
                    )
                    candidate.connect()
                    client = candidate
                    supervisor.diagnostics.push(
                        "wpa_supplicant",
                        "info",
                        f"connected to {supervisor.config.wpa_ctrl_path}",
                    )
                    log.info(
                        "network-supervisor: connected to wpa_supplicant ctrl at %s",
                        supervisor.config.wpa_ctrl_path,
                    )
                except FileNotFoundError:
                    next_connect_attempt = now + WPA_CONNECT_RETRY_INTERVAL_S
                    if not has_logged_missing_socket:
                        log.warning(
                            "network-supervisor: wpa_supplicant ctrl socket "
                            "%s missing; running degraded (state machine + "
                            "persistence only). Retrying every %.0fs.",
                            supervisor.config.wpa_ctrl_path,
                            WPA_CONNECT_RETRY_INTERVAL_S,
                        )
                        has_logged_missing_socket = True
                except OSError as e:
                    next_connect_attempt = now + WPA_CONNECT_RETRY_INTERVAL_S
                    log.warning(
                        "network-supervisor: wpa_supplicant connect failed "
                        "(%s); retrying every %.0fs",
                        e,
                        WPA_CONNECT_RETRY_INTERVAL_S,
                    )
                    supervisor.diagnostics.push("wpa_supplicant", "warn", f"connect failed: {e}")

            # 2. Drain any pending wpa events.
            if client is not None:
                try:
                    raw = client.receive_event()
                except OSError as e:
                    log.warning(
                        "network-supervisor: wpa receive_event failed (%s); "
                        "reconnecting on next tick",
                        e,
                    )
                    supervisor.diagnostics.push(
                        "wpa_supplicant", "warn", f"recv failed: {e}; reconnecting"
                    )
                    client.close()
                    client = None
                    raw = None
                if raw:
                    # Single event per tick is fine — DGRAM is
                    # 1-message-per-recv. Future kernel versions
                    # may batch but the current behavior is one
                    # event per receive_event() call.
                    event, fields = parse_wpa_event(raw)
                    supervisor.diagnostics.push(
                        "wpa_supplicant",
                        "info",
                        f"raw={raw.strip()!r} parsed={event.value if event else 'None'} "
                        f"fields={fields}",
                    )
                    if event is not None:
                        supervisor.apply_event(event)

            # 3. Periodic STA-freq poll (drives channel-follow).
            if now >= next_sta_poll:
                next_sta_poll = now + sta_freq_poll_interval_s
                freq = await poll_sta_freq_mhz()
                if freq is not None:
                    supervisor.apply_sta_freq(freq)
                # Soft log once if `iw` binary is missing (Mac dev).
                # On Linux/Pi this still hits if wlan0 isn't
                # associated yet — but `iw` is present, so the exec
                # succeeds + returns non-zero and we just keep polling.
                elif not has_logged_missing_iw and not _iw_binary_present():
                    log.warning(
                        "network-supervisor: `iw` binary missing; "
                        "STA freq polling disabled (running on dev host?)."
                    )
                    has_logged_missing_iw = True

            # 4. P2 (2026-06-27) LINGER timer poll. The supervisor's
            # transition-effect dispatch arms `_linger_entered_at` on
            # entry into LINGER; we ask each tick whether the grace
            # window has elapsed and fire LINGER_TIMER_EXPIRED if so.
            # check_linger_timeout is idempotent: it returns False
            # once the state has transitioned out of LINGER, so we
            # can't double-fire.
            from openmarquee.network_supervisor import SupervisorEvent as _SE

            if supervisor.check_linger_timeout():
                supervisor.apply_event(_SE.LINGER_TIMER_EXPIRED)

            await asyncio.sleep(wpa_poll_interval_s)
    except asyncio.CancelledError:
        log.info("network-supervisor: observe-only loop cancelled; shutting down")
        if client is not None:
            client.close()
        raise


def _iw_binary_present() -> bool:
    """True if `iw` is on PATH."""
    return shutil.which("iw") is not None
