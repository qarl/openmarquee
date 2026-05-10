#!/usr/bin/env bash
# Golden-master render tests for the Rust renderer.
#
# qarl-direct 2026-05-10: "you have tests to cover everything,
# right? can you do some render tests? so we can compare actual
# pixels?" -- the 418 host tests in hdmi_logic.rs cover pure
# logic but can't catch wrong shader uniforms / blend modes /
# texture upload formats / actual rendered output. This harness
# closes that gap by capturing PNGs on real Pi hardware and
# diffing against checked-in goldens.
#
# Captures are deterministic for static slides (motion=Static
# layers + no auto_mode) at the same forced mode + tick=0.
# Animated slides + transition midpoints would need extra
# determinism plumbing (fixed wall_clock_unix for time-based
# motion); those land in a follow-up.
#
# Usage:
#   scripts/render_tests.sh            # capture + diff vs goldens
#   scripts/render_tests.sh --bless    # capture + accept as new
#                                      # goldens (use after a
#                                      # known-correct renderer
#                                      # change)
#
# Env:
#   RENDER_TARGET   default openmarquee@openMarqueeDev
#   RENDER_FORCE_MODE   default 1920x1080@60 (matches on-glass-
#                       validated mode; @30 fails panel timing)

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${RENDER_TARGET:-openmarquee@openMarqueeDev}"
FORCE_MODE="${RENDER_FORCE_MODE:-1920x1080@60}"
BIN_HOST="$REPO/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
BIN_PI="/tmp/openmarquee-render-rendertests"
GOLDEN_DIR="$REPO/renderer/tests/golden"
CAPTURE_DIR="$REPO/renderer/tests/captures"
DIFF_PY="$REPO/scripts/render_diff.py"

BLESS=0
if [ "${1:-}" = "--bless" ]; then
    BLESS=1
fi

# Restore systemd backend on any exit path.
restore_backend() {
    ssh -q "$TARGET" "sudo systemctl start openmarquee-backend" >/dev/null 2>&1 || true
}
trap restore_backend EXIT

mkdir -p "$GOLDEN_DIR" "$CAPTURE_DIR"

if [ ! -x "$BIN_HOST" ]; then
    echo "FAIL: missing host binary at $BIN_HOST"
    echo "      run scripts/renderer_cross_build.sh first"
    exit 1
fi

# Fixture spec: NAME|TYPE|UUID(s)
# TYPE=slide  -> --capture-slide UUID
# Add more shapes (transition_mid etc) in follow-up commits.
FIXTURES=(
    # 01 · FREE: solid bg, single-line single-layer text. Simplest
    # smoke fixture. Catches regressions in glyph rasterizer +
    # layout + bg fill + atlas geometry.
    "fys_01_free|slide|3964c302-311f-44f2-a6c9-efd24a16cfc0"
    # 08 · Tile Chaos: heavy 5L pattern slide. Catches multi-layer
    # composite + per-layer color binding + multi-slide bg cache
    # invalidation. The slide that the FYS heavy bench used.
    "fys_08_tile_chaos|slide|99c11690-415b-40f6-8e3c-6491f3bdf60e"
    # 09 · Chant Wall: 5L ticker-heavy slide. Pairs with #08 for
    # transition midpoint fixtures in a follow-up.
    "fys_09_chant_wall|slide|2c858968-ae0a-4592-8083-85257de50bcd"
)

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"
ssh -q "$TARGET" "test -x $BIN_PI" || { echo "FAIL: binary not exec on Pi"; exit 1; }

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh -q "$TARGET" "sudo systemctl stop openmarquee-backend"

PASS_COUNT=0
FAIL_COUNT=0
for fixture in "${FIXTURES[@]}"; do
    IFS='|' read -r NAME TYPE UUID <<<"$fixture"
    PI_PATH="/tmp/render-test-$NAME.png"
    LOCAL_PATH="$CAPTURE_DIR/$NAME.png"
    GOLDEN_PATH="$GOLDEN_DIR/$NAME.png"
    case "$TYPE" in
        slide)
            echo
            echo "==> $NAME (capture-slide $UUID)"
            CAP_LOG="$CAPTURE_DIR/$NAME.log"
            if ! ssh -q "$TARGET" "$BIN_PI --output hdmi --capture-slide $UUID --content-root /var/openmarquee/content --capture-path $PI_PATH --force-mode $FORCE_MODE" > "$CAP_LOG" 2>&1; then
                echo "    FAIL: capture exited non-zero (see $CAP_LOG)"
                FAIL_COUNT=$((FAIL_COUNT + 1))
                continue
            fi
            scp -q "$TARGET:$PI_PATH" "$LOCAL_PATH"
            ;;
        *)
            echo "    FAIL: unknown fixture type $TYPE for $NAME"
            FAIL_COUNT=$((FAIL_COUNT + 1))
            continue
            ;;
    esac
    if [ "$BLESS" = "1" ]; then
        cp "$LOCAL_PATH" "$GOLDEN_PATH"
        echo "    BLESSED: $LOCAL_PATH -> $GOLDEN_PATH"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        if python3 "$DIFF_PY" "$LOCAL_PATH" "$GOLDEN_PATH"; then
            PASS_COUNT=$((PASS_COUNT + 1))
        else
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
    fi
done

echo
echo "================================================================"
if [ "$BLESS" = "1" ]; then
    echo "BLESS: $PASS_COUNT fixtures saved to $GOLDEN_DIR"
elif [ "$FAIL_COUNT" = "0" ]; then
    echo "PASS: $PASS_COUNT/$((PASS_COUNT + FAIL_COUNT)) fixtures match goldens"
else
    echo "FAIL: $FAIL_COUNT/$((PASS_COUNT + FAIL_COUNT)) fixtures differ from goldens"
    echo "      review captures at $CAPTURE_DIR"
    echo "      if the renderer change is intended, re-run with --bless"
fi
echo "================================================================"

exit $FAIL_COUNT
