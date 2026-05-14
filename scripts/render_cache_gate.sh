#!/usr/bin/env bash
# Fast cache-regression gate for the IPC sidecar's raster cache.
#
# Locks in 9e776e7 (hold-path) + e6f914e (transition-path) cache
# wiring. Pre-cache, the heavy FYS slides had 22.8% over-budget
# frames (bdc7303 sustained smoke). Post-cache: 0.24% (post-
# transition-cache smoke). The actual wiring is just `Some(&mut
# cache.glyph)` vs `None` at 5 callsites in renderer/src/hdmi.rs --
# any future commit could revert without test failure.
#
# This gate runs in ~5-10 seconds and trips the moment any of those
# 5 callsites gets reverted. Companion to scripts/render_tests.sh
# (golden-master pixel diff) -- this one's the frame-budget axis.
#
# Why a separate script vs bundled into render_tests.sh:
#   - render_tests.sh stops the backend, captures 30+ PNGs, and
#     scp's the deltas back. That's a different lifecycle.
#   - The gate writes nothing to disk on the host -- it just
#     gets a pass/fail. Lighter weight.
#   - Operator can run them independently when iterating on one
#     axis (perf vs. pixels).
#
# Usage:
#   scripts/render_cache_gate.sh           # default: 50 frames of FYS-01
#   FRAMES=200 scripts/render_cache_gate.sh    # longer run
#   BUDGET_MS=20 scripts/render_cache_gate.sh  # tighter budget
#   VERBOSE=1 scripts/render_cache_gate.sh     # per-frame totals
#
# Env:
#   RENDER_TARGET     default openmarquee@openMarqueeDev
#   FRAMES            default 50
#   WARMUP            default 3 (frame[0] cache cold + mode-set,
#                     frame[1] post-init GBM, frame[2] occasional
#                     DRM-resched outlier; frame[3+] steady state)
#   BUDGET_MS         default 33 (the painted-frame-time gate; gate
#                     fails if any post-warmup frame exceeds)
#   SLIDE_ID          default 3964c302-... (FYS-01 FREE -- one of the
#                     4 pre-cache 100% over-budget slides, and the
#                     only one with a checked-in fixture)
#   FIXTURE_DIR       default renderer/tests/fixtures (matches
#                     scripts/render_tests.sh's PI_FIXTURE_ROOT
#                     pattern)
#   VERBOSE           1 to dump every frame's total_us
#
# Exit codes:
#   0   gate passed (max post-warmup frame_dt <= BUDGET_MS)
#   1   gate failed (regression -- cache likely bypassed)
#   2   driver-internal error

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${RENDER_TARGET:-openmarquee@openMarqueeDev}"
FRAMES="${FRAMES:-50}"
WARMUP="${WARMUP:-3}"
BUDGET_MS="${BUDGET_MS:-33}"
# FYS-01 FREE -- simplest fixture (solid bg + 1 text layer) yet
# pre-cache showed 100% over-budget per 381fa49's paint_slide profile.
# Pinned UUID matches scripts/render_tests.sh fixture #01.
SLIDE_ID="${SLIDE_ID:-3964c302-311f-44f2-a6c9-efd24a16cfc0}"
VERBOSE_FLAG=""
if [ "${VERBOSE:-}" = "1" ]; then
    VERBOSE_FLAG="--verbose"
fi

BIN_HOST="$REPO/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
BIN_PI="/tmp/openmarquee-render-cachegate"
DRIVER_HOST="$REPO/scripts/render_cache_gate_driver.py"
DRIVER_PI="/tmp/render_cache_gate_driver.py"
FIXTURE_DIR_HOST="$REPO/renderer/tests/fixtures"
# Reuse the same Pi fixture root as render_tests.sh so the checked-in
# snapshot for FYS-01 (item.json + assets) is available without
# depending on /var/openmarquee/content (which mutates).
PI_FIXTURE_ROOT="/tmp/render-test-content"

# Restore systemd backend on any exit path (we stop it to grab DRM master).
restore_backend() {
    ssh -q "$TARGET" "sudo systemctl start openmarquee-backend" >/dev/null 2>&1 || true
}
trap restore_backend EXIT

if [ ! -x "$BIN_HOST" ]; then
    echo "FAIL: missing host binary at $BIN_HOST"
    echo "      run scripts/renderer_cross_build.sh first"
    exit 1
fi

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"
ssh -q "$TARGET" "test -x $BIN_PI" || { echo "FAIL: binary not exec on Pi"; exit 1; }

echo "==> deploying driver to $TARGET:$DRIVER_PI"
scp -q "$DRIVER_HOST" "$TARGET:$DRIVER_PI"

echo "==> deploying fixture $SLIDE_ID to $TARGET:$PI_FIXTURE_ROOT"
ssh -q "$TARGET" "mkdir -p $PI_FIXTURE_ROOT"
# Only push the one fixture we need (faster than full subtree).
scp -qr "$FIXTURE_DIR_HOST/$SLIDE_ID" "$TARGET:$PI_FIXTURE_ROOT/"

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh -q "$TARGET" "sudo systemctl stop openmarquee-backend"

# Same defensive cleanup pattern as render_tests.sh -- a stale
# /tmp/openmarquee-render-* binary may still hold DRM master from a
# previous run.
#
# IMPORTANT: pkill -f with a pattern that appears in the remote
# bash's argv (e.g. an inline `ssh "...pkill -f /tmp/openmarquee-
# render..."`) will MATCH ITS OWN PARENT BASH and kill the ssh
# session (exit 255), even when no stale renderer exists. We use a
# heredoc-fed `bash -s` so the remote bash's argv is just `bash -s`
# (no pattern match), and only the sudo+pkill chain inside the
# script body sees the pattern.
echo "==> releasing DRM master from any stale renderer binary"
ssh -q "$TARGET" bash -s <<'REMOTE'
sudo pkill -f /tmp/openmarquee-render 2>/dev/null || true
sleep 1
REMOTE

echo "==> running gate: $FRAMES frames of $SLIDE_ID, warmup=$WARMUP, budget=${BUDGET_MS}ms"
echo

# The driver runs ON the Pi (DRM master + GBM only work there);
# ssh propagates its exit code. -t allocates a TTY for cleaner
# Ctrl-C handling if the operator aborts mid-run.
ssh -q "$TARGET" "python3 $DRIVER_PI \
    --binary $BIN_PI \
    --content-root $PI_FIXTURE_ROOT \
    --slide-id $SLIDE_ID \
    --frames $FRAMES \
    --warmup $WARMUP \
    --budget-ms $BUDGET_MS \
    $VERBOSE_FLAG"
