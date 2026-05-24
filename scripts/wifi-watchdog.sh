#!/usr/bin/env bash
# Network mitigation option 1 (2026-05-19, hardened 2026-05-23): WiFi watchdog.
#
# Lives at /usr/local/bin/wifi-watchdog.sh on the Pi, fired every
# 30s by /etc/cron.d/openmarquee-wifi-watchdog (two cron lines, one
# offset by `sleep 30`, since cron's smallest native unit is 1 min).
# Pings the default gateway; on 2 consecutive failures, restarts
# NetworkManager to recover from brcmfmac firmware wedges (the
# failure pattern observed on FYS Pi 2026-05-18: brcmf_proto_bcdc_msg
# failed w/status -110 every 6s; Linux network stack frozen until
# reboot).
#
# 2026-05-23 hardening (postmortem mitigation #2): the previous
# THRESHOLD=3 + 1-min cadence gave a 3-min minimum-detection floor
# — longer than NM's typical self-recovery window, so the watchdog
# was structurally too slow to act. THRESHOLD=2 + 30s cadence
# gives a ~60s detection floor. The no-default-gateway branch
# previously incremented `fails` but never escalated to an NM
# restart — only the explicit-ping-fail branch did. Today's actual
# wedge took the no-gateway path, so the watchdog observed three
# strikes and did nothing. Both branches now escalate identically.
# All log messages also go to the systemd journal via `logger -t
# wifi-watchdog` so post-mortem cross-correlation with NM /
# wpa_supplicant entries doesn't require BST↔UTC reconciliation.
#
# State file: /var/run/wifi-watchdog.fails — integer count of
# consecutive failures. Reset to 0 on a successful ping OR after
# an NM-restart escalation. Persists across cron fires but cleared
# by reboot (/var/run is tmpfs).
#
# 2026-05-23 escalation (postmortem mitigation #4): the THRESHOLD-
# gated NM-restart above recovers from NM-level wedges, but NOT
# from the documented ~2.5-day brcmfmac firmware wedge (the chip
# itself is stuck — NM restart alone CAN'T recover). When 3 NM-
# restarts accumulate within a 10-minute window, the script issues
# `systemctl reboot` — kernel-level firmware re-init via a clean
# boot. Restart timestamps live in /var/run/wifi-watchdog.restarts
# (tmpfs — auto-wiped on boot, the structural anti-reboot-loop).
# The ledger is ALSO wiped explicitly before the reboot call, so
# the post-reboot Pi always starts with a clean counter regardless
# of what tmpfs would do.
#
# Log: /var/log/wifi-watchdog.log (file) AND systemd journal
# (`journalctl -t wifi-watchdog`). Append-only file; logrotate
# handles size. ISO-8601 timestamps.
#
# Idempotent + safe to run manually:
#   sudo /usr/local/bin/wifi-watchdog.sh

# Note: NO `set -e` / `set -o pipefail` — cron is a context where
# silent script abort is the worst-case outcome. `ip route show`
# returning non-zero (rare but possible on a half-up interface)
# would otherwise kill the watchdog before it could act. Every
# command's exit status is checked explicitly below.
set -u

# Cron's PATH is minimal; systemctl / ip / logger live in /usr/sbin
# or /sbin which aren't always there by default. Belt-and-suspenders
# (the cron entry sets PATH too).
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

STATE_FILE=/var/run/wifi-watchdog.fails
RESTARTS_FILE=/var/run/wifi-watchdog.restarts
LOG=/var/log/wifi-watchdog.log
THRESHOLD=2
# Burst-ping check (2026-05-24): instead of a single ping per
# invocation, send 5 pings spaced 0.2s apart and consider the link
# OK if at least 3 of 5 land. Rationale: live measurement on FYS
# during the 11:00 wedge investigation showed a SAME-WIFI rate-
# dependent loss pattern — sustained 1.0s-interval pings averaged
# ~55% loss while 0.2s-interval burst pings averaged 0% loss against
# the same gateway. A single-ping-per-30s check (the prior shape)
# matched the worst-case loss-prone cadence and false-positive-ed
# heavily, driving NM restarts and ultimately auto-reboot cycles on
# a link that was actually fine for short-burst traffic. The burst
# check costs ~1s of script wallclock per invocation (negligible)
# and turns the watchdog into a coherent "is the link working" probe
# rather than "did one random packet land".
PING_BURST_COUNT=5
PING_BURST_OK_MIN=3
# Postmortem mitigation #4 (2026-05-23): when this many NM-restarts
# accumulate inside REBOOT_WINDOW_SECONDS, the chip is presumed
# firmware-wedged and a clean reboot is the only path forward.
#
# 2026-05-24 widen-envelope tune (Path 1 of the wifi-wedge dispatch):
# the original 3-in-600s envelope was tight enough to trigger reboots
# every ~7-10 minutes when the link genuinely degrades for sustained
# 60-120s windows (measured live on FYS during the 11:00-11:50 wedge
# investigation: real catastrophic 0/5-received bursts oscillating
# with brief 4/5 recoveries). With qarl chasing the physical/RF root
# cause separately, the watchdog's job here is to recover via NM-
# restart cycles WITHOUT prematurely escalating to a full reboot
# the operator sees as "the sign blanked again". 5-in-1800s spans
# ~30 minutes — enough room for 5 NM-restart attempts (each takes
# ~15s + the 30s cron cadence + transient recovery windows) to ride
# out a degraded RF window before triggering a kernel-level reset.
REBOOT_AFTER_N_RESTARTS=5
REBOOT_WINDOW_SECONDS=1800

ts() { date -u -Iseconds; }

# Log a message to BOTH the watchdog file log and the systemd
# journal (tagged `wifi-watchdog`). The journal pairing fixes the
# TZ-drift forensics pain from 2026-05-23 — file log was BST,
# journal was UTC, reconciling timestamps cost ~20 min.
note() {
    local stamp
    stamp=$(ts)
    echo "$stamp: $*" >> "$LOG"
    logger -t wifi-watchdog -- "$*"
}

# Record the current NM-restart epoch in the ledger, prune entries
# older than REBOOT_WINDOW_SECONDS, and reboot if the remaining
# count meets REBOOT_AFTER_N_RESTARTS. Called AFTER `systemctl
# restart NetworkManager` from both the no-gateway and ping-fail
# escalation paths.
#
# Pruning uses the KEEP-criterion `ts >= cutoff` rather than
# delta math, so a future-dated ts (an NTP backward jump past a
# previously-recorded restart) is harmlessly kept rather than
# triggering a negative-delta surprise. A forward NTP jump (Pi
# without RTC — boots at 1970 then jumps to 2026 when NTP catches
# up) makes pre-jump timestamps look "very old" and prunes them,
# also harmless.
#
# Anti-reboot-loop: the ledger is wiped BEFORE the reboot call,
# and /var/run is tmpfs so a reboot also wipes it by side-effect.
# Minimum theoretical reboot interval under the 2026-05-24 widen-
# envelope (5 strikes × ~75s per strike) is ~6 min, enough for ops
# to intervene and for transient bad-RF pockets to clear.
record_nm_restart_and_maybe_reboot() {
    local now cutoff ts_line kept count
    now=$(date +%s)
    cutoff=$((now - REBOOT_WINDOW_SECONDS))
    kept=""
    count=0
    # Read existing timestamps, keep only those still inside the
    # window. Missing/empty file -> no prior restarts, count starts
    # at 0.
    if [ -f "$RESTARTS_FILE" ]; then
        while IFS= read -r ts_line; do
            # Skip non-numeric lines defensively (a corrupted file
            # mustn't crash the watchdog).
            case "$ts_line" in
                ''|*[!0-9]*) continue ;;
            esac
            if [ "$ts_line" -ge "$cutoff" ]; then
                kept="${kept}${ts_line}"$'\n'
                count=$((count + 1))
            fi
        done < "$RESTARTS_FILE"
    fi
    # Record this restart.
    kept="${kept}${now}"$'\n'
    count=$((count + 1))
    # Write the pruned + appended ledger back. printf is fine for a
    # few lines; no `set -e` risk since we've kept set -u only.
    printf '%s' "$kept" > "$RESTARTS_FILE"

    if [ "$count" -ge "$REBOOT_AFTER_N_RESTARTS" ]; then
        note "rebooting: $count NM-restarts within ${REBOOT_WINDOW_SECONDS}s window — kernel-level brcmfmac wedge suspected"
        # Wipe the ledger BEFORE the reboot so a post-reboot Pi
        # cannot inherit a "we already rebooted 3 times" state.
        # tmpfs would clear it on reboot anyway; the explicit
        # wipe is belt-and-suspenders for the (impossible-on-
        # tmpfs but defensive) case where /var/run were ever
        # relocated to a persistent fs.
        : > "$RESTARTS_FILE"
        systemctl reboot
    fi
}

# Send a burst of pings and return 0 iff at least PING_BURST_OK_MIN
# of PING_BURST_COUNT packets were received. Designed to coalesce
# within one ~1-second RF window so a rate-dependent-loss link (see
# constant block above) doesn't produce false alarms. PING_RECEIVED
# is set as a side-effect so the caller can log the X/N fraction
# for forensics.
ping_burst_ok() {
    local target=$1
    local out
    # ping stderr → $LOG (not /dev/null) per [[feedback_never_swallow_
    # stderr_in_ci]]: a missing /bin/ping, sudo policy change, or
    # netlink hiccup would otherwise silently produce "0 received"
    # and look like a real outage. Route it to the watchdog log so
    # spawn-side failures are visible in the same forensic stream as
    # the link-side ones.
    out=$(ping -c "$PING_BURST_COUNT" -W 1 -i 0.2 "$target" 2>>"$LOG")
    PING_RECEIVED=$(echo "$out" | awk '/packets transmitted/ { print $4 }')
    # Use := (assign), not :- (substitute), so the subsequent -ge
    # sees a real integer instead of an empty expansion that would
    # trip `set -u` indirectly via the comparison.
    : "${PING_RECEIVED:=0}"
    [ "$PING_RECEIVED" -ge "$PING_BURST_OK_MIN" ]
}

fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
gw=$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')

if [ -z "$gw" ]; then
    fails=$((fails + 1))
    note "no default gateway (fails=$fails)"
    echo "$fails" > "$STATE_FILE"
    if [ "$fails" -ge "$THRESHOLD" ]; then
        note "restarting NetworkManager after $fails consecutive no-gateway"
        systemctl restart NetworkManager
        echo 0 > "$STATE_FILE"
        record_nm_restart_and_maybe_reboot
    fi
elif ping_burst_ok "$gw"; then
    if [ "$fails" -gt 0 ]; then
        note "ping to $gw OK ($PING_RECEIVED/$PING_BURST_COUNT); resetting fails=$fails -> 0"
    fi
    echo 0 > "$STATE_FILE"
else
    fails=$((fails + 1))
    note "ping to $gw degraded ($PING_RECEIVED/$PING_BURST_COUNT received) (fails=$fails)"
    echo "$fails" > "$STATE_FILE"
    if [ "$fails" -ge "$THRESHOLD" ]; then
        note "restarting NetworkManager after $fails consecutive ping-burst failures"
        systemctl restart NetworkManager
        echo 0 > "$STATE_FILE"
        record_nm_restart_and_maybe_reboot
    fi
fi
