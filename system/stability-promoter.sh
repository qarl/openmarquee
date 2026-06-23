#!/bin/sh
# openMarquee stability arc Layer 3 promoter (2026-06-23).
#
# Runs once at boot (oneshot unit openmarquee-stability-promoter.service).
# Decides whether to promote backend (= stop mini + start backend) or
# stay on mini under a renderer/backend-failure reboot loop.
#
# State files:
#   /var/lib/openmarquee/backend-failure-reboot
#       Written by Layer 2's OnFailure handler BEFORE rebooting.
#       Distinguishes backend-failure reboots from other reboots
#       (wifi watchdog, operator, kernel panic). If present here,
#       this boot was caused by a backend failure → increment counter.
#       If absent (wifi reboot, operator), counter is NOT incremented
#       so wifi flakiness can't false-trip the cap.
#
#   /var/lib/openmarquee/reboot-counter
#       Newline-separated UNIX timestamps of recent backend-failure
#       reboots. Pruned to last hour on every run.
#
# Cap: if more than CAP_PER_HOUR backend-failure reboots in
# WINDOW_S seconds → STAY on mini, log [stability]
# reboot_loop_cap_reached. Operator drops the cap manually via
# `rm /var/lib/openmarquee/reboot-counter` then
# `systemctl start openmarquee-stability-promoter.service` to
# re-fire the decision.
#
# Backend bring-up sequence (when promoting):
#   1. systemctl stop openmarquee-mini.service (BLOCKING; mini
#      releases /dev/video10 before we proceed).
#   2. systemctl start openmarquee-backend.service (BLOCKING;
#      backend's own After= deps cascade — network-online + ap0
#      + hostapd + dnsmasq — so backend doesn't actually launch
#      until those satisfy. The promoter doesn't wait for the
#      network itself; systemd does the ordering when start is
#      issued).
#
# Hard ordering (per QA's L3 review constraint): mini STOPPED
# BEFORE backend STARTED. Never both holding /dev/video10
# concurrently → no codec fight wedge.
#
# Idempotency: re-running the script is safe. The marker is
# deleted on first read, so subsequent runs (e.g., operator
# `systemctl start openmarquee-stability-promoter`) read no
# marker → no counter increment → just re-decide based on
# whatever counter state already exists.

set -eu

STATE_DIR=/var/lib/openmarquee
MARKER="$STATE_DIR/backend-failure-reboot"
COUNTER="$STATE_DIR/reboot-counter"
CAP_PER_HOUR=3
WINDOW_S=3600

# Ensure state dir exists. Layer 2's failure handler also does
# mkdir -p, but defensive — handles first-boot before any
# failure has fired.
mkdir -p "$STATE_DIR"

now=$(date +%s)

# Step 1: marker → counter increment + marker delete.
if [ -f "$MARKER" ]; then
    # Read marker contents (ISO 8601 timestamp from Layer 2's
    # OnFailure handler) BEFORE deleting it so the log line
    # carries the actual marker_ts, not the post-delete
    # `missing` fallback.
    marker_ts=$(cat "$MARKER" 2>/dev/null || echo missing)
    echo "$now" >> "$COUNTER"
    rm -f "$MARKER"
    logger -t openmarquee-stability \
        "[stability] backend_failure_reboot_observed marker_ts=$marker_ts counter_incremented"
fi

# Step 2: prune counter to last hour. Atomic via tmp + mv.
if [ -f "$COUNTER" ]; then
    cutoff=$((now - WINDOW_S))
    # awk filters timestamps within window. Empty lines stripped
    # via sed because awk doesn't emit them on no-match but be
    # defensive (manual edits, partial writes).
    awk -v cutoff="$cutoff" 'NF && $1 > cutoff' "$COUNTER" > "$COUNTER.tmp"
    mv "$COUNTER.tmp" "$COUNTER"
    count=$(wc -l < "$COUNTER" | tr -d ' ')
else
    count=0
fi

# Step 3: cap check.
if [ "$count" -gt "$CAP_PER_HOUR" ]; then
    logger -t openmarquee-stability \
        "[stability] reboot_loop_cap_reached count=$count cap=$CAP_PER_HOUR window_s=$WINDOW_S; staying on mini"
    echo "stability-promoter: reboot-loop cap reached (count=$count cap=$CAP_PER_HOUR); staying on mini" >&2
    exit 0
fi

# Step 4: promote — stop mini FIRST (blocking), then start backend.
logger -t openmarquee-stability \
    "[stability] promote_backend count=$count cap=$CAP_PER_HOUR"
echo "stability-promoter: promoting backend (count=$count cap=$CAP_PER_HOUR)" >&2

# Stop mini. Blocking — systemctl returns after mini exits.
# If mini wasn't running (e.g., it failed at boot too), this
# is a no-op + exits 0.
if ! systemctl stop openmarquee-mini.service; then
    logger -t openmarquee-stability \
        "[stability] mini_stop_failed; continuing — backend start will fight for /dev/video10"
    echo "stability-promoter: WARNING mini stop failed; continuing" >&2
fi

# Start backend. systemd resolves backend's own After= deps
# (network-online.target etc.) at start time, so we don't need
# to wait for the network up here — systemd does the ordering.
# Blocking start: returns once backend is active (READY=1
# fires via sd_notify) or the start fails.
if systemctl start openmarquee-backend.service; then
    logger -t openmarquee-stability "[stability] backend_started"
    echo "stability-promoter: backend started" >&2
else
    # Backend's own restart logic kicks in via Restart=on-failure.
    # We log the failure but exit 0 — leaving the systemd unit
    # alive to retry. The next backend failure will trigger the
    # OnFailure handler + write a marker; counter will increment
    # on the NEXT boot's promoter run.
    logger -t openmarquee-stability \
        "[stability] backend_start_failed; systemd will retry per Restart="
    echo "stability-promoter: WARNING backend start failed; systemd will retry" >&2
fi
