#!/usr/bin/env bash
# Network mitigation option 3 (2026-05-19): periodic preemptive
# brcmfmac module reload.
#
# Lives at /usr/local/bin/wifi-preemptive-reload.sh on the Pi, fired
# every 24h at 03:00 local by /etc/cron.d/openmarquee-wifi-
# preemptive-reload. Drops + reloads the brcmfmac kernel module to
# clear any latent firmware state before the ~2.5-day wedge horizon
# observed empirically on FYS Pi.
#
# Downtime per fire: ~5-10 sec (rmmod → modprobe → WPA re-associate →
# DHCP re-lease). 03:00 chosen as a low-viewing-window for the FYS
# production sign; qarl-approved 2026-05-19.
#
# Watchdog (option 1) coexists with this — if the reload happens
# while the watchdog is mid-cycle, the watchdog's ping fails for the
# reload-duration but counter resets on the next minute's successful
# ping (the threshold is 3 consecutive fails).
#
# Log: /var/log/wifi-preemptive-reload.log. Append-only.
#
# Idempotent + safe to run manually:
#   sudo /usr/local/bin/wifi-preemptive-reload.sh

set -euo pipefail

# Cron's PATH is minimal; rmmod/modprobe/ip live in /usr/sbin or
# /sbin which aren't always there by default. Belt-and-suspenders.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

LOG=/var/log/wifi-preemptive-reload.log

ts() { date -Iseconds; }

echo "$(ts): start preemptive brcmfmac reload" >> "$LOG"

# Three-step unload sequence:
#   (1) ip link set wlan0 down — releases NetworkManager's hold on
#       the interface so the kernel module's refcount drops.
#   (2) modprobe -r brcmfmac — recursively unloads brcmfmac plus
#       its dependent brcmfmac_wcc (worldwide regulatory compliance,
#       pins brcmfmac on modern Pi OS kernels).
#   (3) modprobe brcmfmac — reload; NM picks up the fresh wlan0 +
#       re-associates per its saved connection profile.
#
# No -f on modprobe -r; forced unload of a stuck firmware can hang
# the kernel on vc4. If the unload fails, exit non-zero so cron
# logs it; watchdog (option 1) will catch any resulting
# connectivity loss within ~3 min.

if ip link set wlan0 down 2>>"$LOG"; then
    echo "$(ts): ip link set wlan0 down OK" >> "$LOG"
else
    echo "$(ts): ip link set wlan0 down FAILED" >> "$LOG"
    exit 1
fi

# Brief pause for NetworkManager to release the wlan0 ref. NM's
# inotify-driven state machine usually sees the link-down within
# a few hundred ms.
sleep 1

if modprobe -r brcmfmac 2>>"$LOG"; then
    echo "$(ts): modprobe -r brcmfmac OK" >> "$LOG"
else
    echo "$(ts): modprobe -r brcmfmac FAILED; bringing wlan0 back up + exiting" >> "$LOG"
    ip link set wlan0 up 2>>"$LOG" || true
    exit 1
fi

# Brief pause for the kernel to settle. modprobe immediately after
# rmmod sometimes races with the SDIO bus.
sleep 2

if modprobe brcmfmac 2>>"$LOG"; then
    echo "$(ts): modprobe brcmfmac OK" >> "$LOG"
else
    echo "$(ts): modprobe brcmfmac FAILED" >> "$LOG"
    exit 1
fi

# Wait for the WiFi to come back online. NetworkManager auto-
# associates within ~5 sec on healthy firmware. Poll once a second
# for up to 15 sec.
for i in $(seq 1 15); do
    if ip -4 addr show wlan0 2>/dev/null | grep -q "inet "; then
        echo "$(ts): wlan0 has IPv4 after ${i}s: $(ip -4 addr show wlan0 | awk '/inet /{print $2}')" >> "$LOG"
        exit 0
    fi
    sleep 1
done

echo "$(ts): WARN: wlan0 did not acquire IPv4 within 15s post-reload" >> "$LOG"
# Non-zero exit so cron logs the failure. Watchdog (option 1) will
# detect + restart NetworkManager on the next minute if needed.
exit 2
