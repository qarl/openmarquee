#!/usr/bin/env bash
# Network mitigation option 1 (2026-05-19): WiFi watchdog.
#
# Lives at /usr/local/bin/wifi-watchdog.sh on the Pi, fired every
# minute by /etc/cron.d/openmarquee-wifi-watchdog. Pings the default
# gateway; on 3 consecutive failures, restarts NetworkManager to
# recover from brcmfmac firmware wedges (the failure pattern observed
# on FYS Pi 2026-05-18: brcmf_proto_bcdc_msg failed w/status -110
# every 6s; Linux network stack frozen until reboot).
#
# State file: /var/run/wifi-watchdog.fails — integer count of
# consecutive failures. Reset to 0 on a successful ping. Persists
# across cron fires (1 min apart) but cleared by reboot (/var/run
# is tmpfs).
#
# Log: /var/log/wifi-watchdog.log. Append-only; logrotate handles
# size. ISO-8601 timestamps so post-mortem cross-correlation with
# journalctl / dmesg is straightforward.
#
# Idempotent + safe to run manually:
#   sudo /usr/local/bin/wifi-watchdog.sh

set -euo pipefail

STATE_FILE=/var/run/wifi-watchdog.fails
LOG=/var/log/wifi-watchdog.log
THRESHOLD=3

ts() { date -Iseconds; }

fails=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
gw=$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')

if [ -z "$gw" ]; then
    fails=$((fails + 1))
    echo "$(ts): no default gateway (fails=$fails)" >> "$LOG"
    echo "$fails" > "$STATE_FILE"
elif ping -c 1 -W 2 "$gw" >/dev/null 2>&1; then
    if [ "$fails" -gt 0 ]; then
        echo "$(ts): ping to $gw OK; resetting fails=$fails -> 0" >> "$LOG"
    fi
    echo 0 > "$STATE_FILE"
else
    fails=$((fails + 1))
    echo "$(ts): ping to $gw failed (fails=$fails)" >> "$LOG"
    echo "$fails" > "$STATE_FILE"
    if [ "$fails" -ge "$THRESHOLD" ]; then
        echo "$(ts): restarting NetworkManager after $fails consecutive failures" >> "$LOG"
        systemctl restart NetworkManager
        echo 0 > "$STATE_FILE"
    fi
fi
