#!/usr/bin/env bash
# Network mitigation option 3 (2026-05-19): periodic preemptive
# NetworkManager cycle.
#
# Lives at /usr/local/bin/wifi-preemptive-reload.sh on the Pi, fired
# every 24h at 03:00 local by /etc/cron.d/openmarquee-wifi-
# preemptive-reload. Restarts NetworkManager to clear any
# accumulated state before the ~2.5-day brcmfmac wedge horizon
# observed empirically on FYS Pi.
#
# WHY NOT kernel module reload (the original spec): this Pi runs
# DUAL-MODE — wlan0 STA (home WiFi via NM + wpa_supplicant) AND
# ap0 AP (captive portal via hostapd + openmarquee-ap0.service).
# Both interfaces are tied to the same brcmfmac module. Unloading
# brcmfmac would require tearing down hostapd + ap0 + waiting for
# clean re-init — risking the captive portal not coming back. The
# dual-mode brcmfmac data-plane constraint is documented in ops
# memory (reference_pi_zero_2w_brcmfmac_dual_mode_data_plane).
#
# `systemctl restart NetworkManager` does NOT touch hostapd/ap0
# and is the same action Option 1's watchdog uses on cumulative
# failure. As a daily proactive cycle it:
#   - clears NM-side accumulated connection-profile state
#   - forces wlan0 re-association via wpa_supplicant
#   - re-leases DHCP
#   - leaves brcmfmac kernel module + ap0 captive portal untouched
#
# Trade-off: doesn't reset kernel-side brcmfmac firmware state.
# Firmware-wedge recovery still relies on Option 1's watchdog
# detecting the wedge + restarting NM (which is the same action
# this script does proactively, but reactive). This script just
# kicks the cycle daily so NM-side drift doesn't accumulate to
# the wedge horizon.
#
# Downtime per fire: ~5 sec (NM restart + wpa_supplicant re-assoc).
# qarl chose 03:00 as a low-viewing-window for the FYS sign.
#
# Log: /var/log/wifi-preemptive-reload.log. Append-only.
# Idempotent + safe to run manually: sudo /usr/local/bin/wifi-preemptive-reload.sh

set -euo pipefail

# Cron's PATH is minimal; systemctl/nmcli/ip live in /usr/sbin or
# /sbin which aren't always there by default. Belt-and-suspenders.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

LOG=/var/log/wifi-preemptive-reload.log

ts() { date -u -Iseconds; }

echo "$(ts): start preemptive NetworkManager cycle" >> "$LOG"

if systemctl restart NetworkManager 2>>"$LOG"; then
    echo "$(ts): systemctl restart NetworkManager OK" >> "$LOG"
else
    echo "$(ts): systemctl restart NetworkManager FAILED" >> "$LOG"
    exit 1
fi

# Wait for the WiFi to come back online. NM auto-associates within
# ~5 sec on healthy firmware. Poll once a second for up to 15 sec.
for i in $(seq 1 15); do
    if ip -4 addr show wlan0 2>/dev/null | grep -q "inet "; then
        echo "$(ts): wlan0 has IPv4 after ${i}s: $(ip -4 addr show wlan0 | awk '/inet /{print $2}')" >> "$LOG"
        exit 0
    fi
    sleep 1
done

echo "$(ts): WARN: wlan0 did not acquire IPv4 within 15s post-NM-restart" >> "$LOG"
# Non-zero exit so cron logs the failure. Watchdog (option 1) will
# detect + restart NetworkManager on the next minute if needed.
exit 2
