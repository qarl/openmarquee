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
LOG=/var/log/wifi-watchdog.log
THRESHOLD=2

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
    fi
elif ping -c 1 -W 2 "$gw" >/dev/null 2>&1; then
    if [ "$fails" -gt 0 ]; then
        note "ping to $gw OK; resetting fails=$fails -> 0"
    fi
    echo 0 > "$STATE_FILE"
else
    fails=$((fails + 1))
    note "ping to $gw failed (fails=$fails)"
    echo "$fails" > "$STATE_FILE"
    if [ "$fails" -ge "$THRESHOLD" ]; then
        note "restarting NetworkManager after $fails consecutive failures"
        systemctl restart NetworkManager
        echo 0 > "$STATE_FILE"
    fi
fi
