#!/usr/bin/env bash
# Phase 9 Step 9b (2026-05-16): IPC sidecar soak harness for §11 acceptance.
#
# Tails `journalctl -fu openmarquee-backend` on the target Pi for a
# configurable duration, captures `ipc.soak` lines (added in commit
# ffbb437, Phase 9 Step 9a) plus OOM / crash signals, prints a
# heartbeat every N minutes, and runs the companion parser
# (renderer_pi_soak_ipc_parse.py) for the §11 verdict.
#
# Spec ref: docs/renderer-rewrite-requirements.md §11 (V1-GA acceptance):
# 30 fps sustained on FREE YOUR SIGN with shader transitions, no OOM
# kills, over ≥6h soak.
#
# Per `feedback_no_soak_during_dev`: this script is built tonight but
# the actual 6h soak is release-candidate-gated. Use --dry-run to
# verify wiring without firing a real journalctl tail.
#
# Usage:
#   scripts/renderer_pi_soak_ipc.sh [--target TGT] [--duration DUR]
#                                   [--heartbeat HB] [--min-fps FPS]
#                                   [--rolling-window MIN]
#                                   [--dry-run]
#
# Defaults:
#   --target openmarquee@openMarqueeDev   prod dev Pi
#   --duration 6h                          §11 acceptance window
#   --heartbeat 10m                        periodic stats line
#   --min-fps 30.0                         §11 fps floor
#   --rolling-window 10                    min-fps rolling window (minutes)
#
# Companion: scripts/renderer_pi_soak_ipc_parse.py reads the log and
# emits the PASS/FAIL verdict. This script orchestrates the capture +
# heartbeat; the parser owns the §11 gate logic.

set -euo pipefail

TARGET="openmarquee@openMarqueeDev"
DURATION="6h"
HEARTBEAT="10m"
MIN_FPS="30.0"
ROLLING_WINDOW_MIN="10"
DRY_RUN=0

usage() {
    sed -n '1,/^set -euo pipefail/p' "$0" | sed -n '2,$p' | sed 's/^# \?//'
    exit "${1:-1}"
}

# Convert a human duration ("6h", "300s", "15m", "1h30m") to seconds.
# Supports h/m/s suffixes, accepts compound (e.g. "1h30m"), bare digits
# are seconds.
duration_to_secs() {
    local in="$1" total=0 num
    if [[ "$in" =~ ^[0-9]+$ ]]; then
        echo "$in"
        return
    fi
    while [[ -n "$in" ]]; do
        if [[ "$in" =~ ^([0-9]+)h(.*)$ ]]; then
            num="${BASH_REMATCH[1]}"; in="${BASH_REMATCH[2]}"
            total=$((total + num * 3600))
        elif [[ "$in" =~ ^([0-9]+)m(.*)$ ]]; then
            num="${BASH_REMATCH[1]}"; in="${BASH_REMATCH[2]}"
            total=$((total + num * 60))
        elif [[ "$in" =~ ^([0-9]+)s?(.*)$ ]]; then
            num="${BASH_REMATCH[1]}"; in="${BASH_REMATCH[2]}"
            total=$((total + num))
        else
            echo "ERR: cannot parse duration token: $in" >&2
            return 1
        fi
    done
    echo "$total"
}

# Arg parse.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --target) TARGET="$2"; shift 2;;
        --duration) DURATION="$2"; shift 2;;
        --heartbeat) HEARTBEAT="$2"; shift 2;;
        --min-fps) MIN_FPS="$2"; shift 2;;
        --rolling-window) ROLLING_WINDOW_MIN="$2"; shift 2;;
        --dry-run) DRY_RUN=1; shift;;
        -h|--help) usage 0;;
        *) echo "unknown arg: $1" >&2; usage 1;;
    esac
done

DURATION_S=$(duration_to_secs "$DURATION")
HEARTBEAT_S=$(duration_to_secs "$HEARTBEAT")

LOG_DIR="/tmp/renderer-soak-ipc"
TS="$(date +%Y%m%d-%H%M%S)"
LOG="$LOG_DIR/journalctl-$TS.log"
REPORT_JSON="$LOG_DIR/report-$TS.json"
mkdir -p "$LOG_DIR"

PARSER="$(dirname "$0")/renderer_pi_soak_ipc_parse.py"

cat <<HEADER
==> Phase 9 Step 9b IPC soak harness
    target:           $TARGET
    duration:         $DURATION ($DURATION_S s)
    heartbeat:        $HEARTBEAT ($HEARTBEAT_S s)
    min-fps:          $MIN_FPS
    rolling window:   ${ROLLING_WINDOW_MIN}min
    log:              $LOG
    report:           $REPORT_JSON
    parser:           $PARSER
HEADER

if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "==> DRY RUN -- would tail journalctl on $TARGET for $DURATION_S seconds"
    echo "    skipping ssh, heartbeat, and parser invocation."
    exit 0
fi

# Sanity check parser is present + executable.
if [[ ! -f "$PARSER" ]]; then
    echo "FAIL: parser missing at $PARSER" >&2
    exit 1
fi

# Sanity check target reachability.
echo "==> sanity check: ssh $TARGET hostname"
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$TARGET" 'hostname'; then
    echo "FAIL: cannot ssh to $TARGET (check Tailscale / SSH keys)" >&2
    exit 1
fi

# Sanity check backend running.
echo "==> sanity check: openmarquee-backend.service active on $TARGET"
if ! ssh -o BatchMode=yes "$TARGET" 'systemctl is-active openmarquee-backend' >/dev/null; then
    echo "WARN: openmarquee-backend is not active on $TARGET; soak may produce no ipc.soak samples." >&2
    echo "      consider 'ssh $TARGET sudo systemctl start openmarquee-backend' before retry." >&2
    # Don't hard-fail -- operator may intentionally test the no-paint
    # path (verify the parser handles zero samples gracefully).
fi

# Heartbeat: in a background loop, summarize the last N samples from
# the running log. Killed by EXIT trap.
heartbeat_loop() {
    # Stagger the first heartbeat so we don't print before any
    # ipc.soak line has had a chance to land (first one fires ~30s
    # after first paint).
    sleep "$HEARTBEAT_S"
    while true; do
        if [[ -f "$LOG" ]]; then
            local elapsed=$(( $(date +%s) - START_TS ))
            # Use the parser in inline mode against the partial log.
            # Tolerate parser non-zero exit (early failures shouldn't
            # kill the heartbeat -- the final parser run is what gates
            # PASS/FAIL).
            local quick
            quick="$(python3 "$PARSER" "$LOG" --min-fps-avg "$MIN_FPS" --rolling-window-min "$ROLLING_WINDOW_MIN" 2>/dev/null | head -5 | tr '\n' ' ' || true)"
            echo "[heartbeat t=${elapsed}s/${DURATION_S}s] $quick"
        fi
        sleep "$HEARTBEAT_S"
    done
}

# Start the soak. ssh + timeout on the remote so even if the local
# script dies, the remote journalctl process exits. journalctl -fu
# follows the service unit; --no-pager + --output=short-iso prints
# wall-clock prefixes that the parser can use.
START_TS=$(date +%s)
echo "==> starting capture at $(date -u -Iseconds)"
echo "    (background ssh + journalctl tail; will run $DURATION_S seconds)"
heartbeat_loop &
HEARTBEAT_PID=$!
trap 'kill $HEARTBEAT_PID 2>/dev/null || true' EXIT

# `timeout` on the remote kills the journalctl follower after the
# soak window elapses. Exit code 124 from `timeout` is the canonical
# success path here.
#
# ServerAliveInterval/CountMax keep the ssh tunnel alive across NAT
# idle-timeout / brief Tailscale blips during long captures. Without
# these, a 6h §11 run can silently drop and exit 255 mid-soak (caught
# in subagent review on the Phase 9b commit).
set +e
ssh -o BatchMode=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=10 \
    "$TARGET" \
    "timeout $DURATION_S journalctl -fu openmarquee-backend --no-pager --output=short-iso --since 'now'" \
    > "$LOG" 2>&1
SSH_EXIT=$?
set -e

# Stop heartbeat (the trap will fire anyway, but be explicit).
kill "$HEARTBEAT_PID" 2>/dev/null || true
trap - EXIT

case "$SSH_EXIT" in
    0|124)
        echo "==> capture window closed (ssh exit $SSH_EXIT)"
        ;;
    255)
        echo "FAIL: ssh transport error (255) -- check Tailscale / network" >&2
        echo "      partial log retained at $LOG"
        exit 1
        ;;
    *)
        echo "FAIL: capture exited non-canonically: $SSH_EXIT" >&2
        echo "      partial log retained at $LOG"
        exit 1
        ;;
esac

# Final parser run + verdict.
echo "==> parsing $LOG"
if ! python3 "$PARSER" "$LOG" \
        --min-fps-avg "$MIN_FPS" \
        --rolling-window-min "$ROLLING_WINDOW_MIN" \
        --json "$REPORT_JSON"; then
    echo "FAIL: §11 acceptance gate" >&2
    echo "      log:    $LOG"
    echo "      report: $REPORT_JSON"
    exit 1
fi

echo
echo "PASS: §11 IPC soak green"
echo "  log:    $LOG"
echo "  report: $REPORT_JSON"
