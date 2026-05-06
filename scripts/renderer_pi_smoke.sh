#!/usr/bin/env bash
# Pi-side integration smoke for the Rust renderer.
#
# Asserts the cross-built binary deploys to the dev Pi, exercises both
# Phase 1 (--probe) and Phase 2 (--solid-color) against real DRM
# hardware, and that the openmarquee-backend systemd unit recovers
# after we grab DRM master from it.
#
# Per the QA test gate (2026-05-06): every renderer phase commit
# requires this script returning green.
#
# Usage:
#   scripts/renderer_pi_smoke.sh [TARGET]
#
# TARGET defaults to openmarquee@openMarqueeDev (Tailscale magic-DNS).
# The cross-built binary is expected at
# renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render
# — run `cargo zigbuild --target aarch64-unknown-linux-gnu --release`
# in the renderer/ dir first, with the sysroot env vars from the
# Phase 2 commit message.

set -euo pipefail

TARGET="${1:-openmarquee@openMarqueeDev}"
BIN_HOST="renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
BIN_PI="/tmp/openmarquee-render"
LOG_DIR="/tmp/renderer-smoke"

if [ ! -x "$BIN_HOST" ]; then
    echo "FAIL: missing host binary at $BIN_HOST"
    echo "      run cargo zigbuild --target aarch64-unknown-linux-gnu --release first"
    exit 1
fi

mkdir -p "$LOG_DIR"

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"
ssh "$TARGET" "test -x $BIN_PI" || { echo "FAIL: binary not executable on Pi"; exit 1; }
echo "    ok"

echo "==> Phase 1 -- --probe"
PROBE_LOG="$LOG_DIR/probe.log"
ssh "$TARGET" "$BIN_PI --output hdmi --probe" > "$PROBE_LOG" 2>&1 || \
    { echo "FAIL: --probe exit non-zero"; cat "$PROBE_LOG"; exit 1; }
grep -q '=== Connectors ===' "$PROBE_LOG" || \
    { echo "FAIL: --probe didn't print Connectors section"; exit 1; }
grep -q 'HDMIA' "$PROBE_LOG" || \
    { echo "FAIL: --probe didn't list an HDMI connector"; exit 1; }
grep -qi 'panic\|panicked' "$PROBE_LOG" && \
    { echo "FAIL: panic in --probe output"; exit 1; }
echo "    ok ($(grep -c 'connector::Handle' "$PROBE_LOG") connectors,"\
"$(grep -c 'plane::Handle' "$PROBE_LOG") planes)"

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh "$TARGET" "sudo systemctl stop openmarquee-backend"
sleep 2

echo "==> Phase 2 -- --solid-color 0,1,1 --hold-secs 3"
COLOR_LOG="$LOG_DIR/solid-color.log"
COLOR_EXIT=0
ssh "$TARGET" "$BIN_PI --output hdmi --solid-color 0,1,1 --hold-secs 3" \
    > "$COLOR_LOG" 2>&1 || COLOR_EXIT=$?

echo "==> Phase 2.1 -- --animate --hold-secs 3 --fps 30"
ANIM_LOG="$LOG_DIR/animate.log"
ANIM_EXIT=0
ssh "$TARGET" "$BIN_PI --output hdmi --animate --hold-secs 3 --fps 30" \
    > "$ANIM_LOG" 2>&1 || ANIM_EXIT=$?

# Always try to bring the backend back up before we assert anything.
echo "==> restarting openmarquee-backend"
ssh "$TARGET" "sudo systemctl start openmarquee-backend"
sleep 3

if [ "$COLOR_EXIT" -ne 0 ]; then
    echo "FAIL: --solid-color exit $COLOR_EXIT"
    cat "$COLOR_LOG"
    exit 1
fi
grep -q 'solid-color render complete' "$COLOR_LOG" || \
    { echo "FAIL: --solid-color didn't print completion line"; cat "$COLOR_LOG"; exit 1; }
grep -qi 'panic\|panicked' "$COLOR_LOG" && \
    { echo "FAIL: panic in --solid-color output"; exit 1; }
echo "    --solid-color ok"

if [ "$ANIM_EXIT" -ne 0 ]; then
    echo "FAIL: --animate exit $ANIM_EXIT"
    cat "$ANIM_LOG"
    exit 1
fi
grep -q 'animated atomic render complete' "$ANIM_LOG" || \
    { echo "FAIL: --animate didn't print completion line"; cat "$ANIM_LOG"; exit 1; }
grep -qi 'panic\|panicked' "$ANIM_LOG" && \
    { echo "FAIL: panic in --animate output"; exit 1; }
# Frame-count sanity: a 3-second animate run at any reasonable fps
# should land at least ~30 frames. The completion line includes a
# count we can grep for.
FRAMES=$(grep -oE 'rendered [0-9]+ frames' "$ANIM_LOG" | grep -oE '[0-9]+' | head -1)
if [ -z "${FRAMES:-}" ] || [ "$FRAMES" -lt 30 ]; then
    echo "FAIL: --animate rendered too few frames (got '${FRAMES:-none}', want >=30)"
    cat "$ANIM_LOG"
    exit 1
fi
echo "    --animate ok ($FRAMES frames in 3s)"

echo "==> backend recovery check (DRM master returned)"
BACKEND_STATE=$(ssh "$TARGET" "systemctl is-active openmarquee-backend" || true)
if [ "$BACKEND_STATE" != "active" ]; then
    echo "FAIL: openmarquee-backend not active after run (state=$BACKEND_STATE)"
    ssh "$TARGET" "sudo journalctl -u openmarquee-backend --since='1 minute ago' --no-pager | tail -30" || true
    exit 1
fi
echo "    ok"

echo
echo "PASS: renderer Pi smoke green"
echo "  logs: $LOG_DIR/{probe,solid-color}.log"
