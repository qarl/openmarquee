#!/usr/bin/env bash
# Pi-side soak gate for the Rust renderer.
#
# Drives the canonical FYS reel in --reel-loop mode for SOAK_DURATION_
# SECS (default 3600 = 1h), captures the [mem] lines emitted at
# session=open / pass=N / session=close on stderr, and asserts via
# linear-regression slope test that VmRSS / VmData / CmaUsed show no
# monotonic growth per spec §8.2. Per-pass values from the standalone
# reel are the canonical sample point.
#
# Usage:
#   scripts/renderer_pi_soak.sh [TARGET]
#
# Env:
#   SOAK_DURATION_SECS   default 3600 (1h). v1 §8.2 starting point is
#                        ≥6h ideally overnight; CI gates use shorter.
#   SOAK_MAX_SLOPE_MBH   default 5.0   (MB/hour ceiling for slope test)
#   SOAK_MAX_CMA_MB      default 200.0 (CMA hard ceiling per budget §4)
#   SOAK_FORCE_MODE      unset by default; e.g. "1920x1080@30" runs
#                        the soak under --force-mode (CEA-861 Mode
#                        synthesis per slice 17(c)). Use for §8.2
#                        acceptance runs that need to measure at
#                        the spec'd 1080p target on a Pi with EDID
#                        issues.
#
# TARGET defaults to openmarquee@openMarqueeDev. The cross-built binary
# is expected at the same host path the smoke uses.

set -euo pipefail

TARGET="${1:-openmarquee@openMarqueeDev}"
DURATION="${SOAK_DURATION_SECS:-3600}"
MAX_SLOPE="${SOAK_MAX_SLOPE_MBH:-5.0}"
MAX_CMA="${SOAK_MAX_CMA_MB:-200.0}"
MAX_RSS="${SOAK_MAX_RSS_MB:-100.0}"
# Operator-tunable hold compression for noise/sample-count tradeoff:
# default leaves canonical FYS timings (per slide.duration_ms) for
# §8.2 acceptance runs; smoke embedding overrides to 1s for more
# passes per minute on short runs.
SOAK_HOLD_OVERRIDE="${SOAK_HOLD_SECS:-}"
WARMUP_PASSES="${SOAK_WARMUP_PASSES:-0}"
# v1-spec-delta #17 followup: optional --force-mode override so the
# §8.2 6h soak can run at the spec'd 1080p target instead of the
# dev Pi's native panel mode (1024x768 absent EDID). Format
# matches the renderer's --force-mode CLI: WxH@Hz, e.g.
# 1920x1080@30. Unset = use connector's reported modes.
FORCE_MODE_OVERRIDE="${SOAK_FORCE_MODE:-}"
BIN_HOST="renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
BIN_PI="/tmp/openmarquee-render"
LOG_DIR="/tmp/renderer-soak"
SOAK_LOG="$LOG_DIR/soak.log"

if [ ! -x "$BIN_HOST" ]; then
    echo "FAIL: missing host binary at $BIN_HOST"
    echo "      run cargo zigbuild --target aarch64-unknown-linux-gnu --release first"
    exit 1
fi

mkdir -p "$LOG_DIR"

# Restore the backend on any exit path -- includes Ctrl-C mid-soak,
# parser failure, ssh timeout, etc. Otherwise an orphan ssh holds
# DRM master and the backend systemd unit stays stopped.
restore_backend() {
    ssh "$TARGET" "sudo systemctl start openmarquee-backend" >/dev/null 2>&1 || true
}
trap restore_backend EXIT

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"
ssh "$TARGET" "test -x $BIN_PI" || { echo "FAIL: binary not executable on Pi"; exit 1; }
echo "    ok"

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh "$TARGET" "sudo systemctl stop openmarquee-backend"
sleep 2

# canonical FYS reel ≈ 30s/pass on dev Pi (hold + transitions). 1h
# default = ~120 passes, well above the slope-test 3-sample floor.
# §8.2 "≥6h overnight" uses SOAK_DURATION_SECS=21600.
HOLD_FLAG=""
if [ -n "$SOAK_HOLD_OVERRIDE" ]; then
    HOLD_FLAG="--hold-secs $SOAK_HOLD_OVERRIDE"
fi
FORCE_MODE_FLAG=""
if [ -n "$FORCE_MODE_OVERRIDE" ]; then
    FORCE_MODE_FLAG="--force-mode $FORCE_MODE_OVERRIDE"
fi
echo "==> running soak: ${DURATION}s @ canonical FYS reel --reel-loop ${HOLD_FLAG:-(canonical timings)} ${FORCE_MODE_FLAG:-(EDID-driven mode)}"
set +e
ssh "$TARGET" "timeout ${DURATION} $BIN_PI --output hdmi --play-reel --reel-loop --fps 30 $HOLD_FLAG $FORCE_MODE_FLAG 2>&1" \
    > "$SOAK_LOG" 2>&1
SOAK_EXIT=$?
set -e

# `timeout` returns 124 when it kills the child process on schedule;
# that's the canonical success path here. ssh returns 255 on connection
# error and propagates the remote command's exit code otherwise.
case "$SOAK_EXIT" in
    0|124)
        ;;
    *)
        echo "FAIL: soak ssh exit $SOAK_EXIT (non-timeout error)"
        tail -40 "$SOAK_LOG"
        exit 1
        ;;
esac

echo "==> parsing [mem] lines + asserting slope"
PARSER="$(dirname "$0")/renderer_soak_parse.py"
# Slice 12(c) followup: per-signal slope ceiling env vars
# (SOAK_MAX_RSS_SLOPE_MBH / DATA / SWAP / CMA). When unset,
# the default --max-slope-mb-per-hour applies to all signals.
PER_SIGNAL_ARGS=()
[ -n "${SOAK_MAX_RSS_SLOPE_MBH:-}" ] && PER_SIGNAL_ARGS+=(--max-rss-slope-mbh "$SOAK_MAX_RSS_SLOPE_MBH")
[ -n "${SOAK_MAX_DATA_SLOPE_MBH:-}" ] && PER_SIGNAL_ARGS+=(--max-data-slope-mbh "$SOAK_MAX_DATA_SLOPE_MBH")
[ -n "${SOAK_MAX_SWAP_SLOPE_MBH:-}" ] && PER_SIGNAL_ARGS+=(--max-swap-slope-mbh "$SOAK_MAX_SWAP_SLOPE_MBH")
[ -n "${SOAK_MAX_CMA_SLOPE_MBH:-}" ] && PER_SIGNAL_ARGS+=(--max-cma-slope-mbh "$SOAK_MAX_CMA_SLOPE_MBH")
if ! python3 "$PARSER" "$SOAK_LOG" \
    --max-slope-mb-per-hour "$MAX_SLOPE" \
    --max-cma-mb "$MAX_CMA" \
    --max-rss-mb "$MAX_RSS" \
    --warmup-passes "$WARMUP_PASSES" \
    "${PER_SIGNAL_ARGS[@]}"; then
    echo "FAIL: soak slope/budget gate"
    exit 1
fi

echo
echo "PASS: renderer Pi soak green"
echo "  log: $SOAK_LOG"
