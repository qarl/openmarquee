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
FIXTURE_DIR="$REPO/renderer/tests/fixtures"
# Synth-content-root on Pi: hosts the synthesized fixture slides
# that don't live in /var/openmarquee/content (overlay-route, etc.).
# Populated each run via scp of the fixtures/ subtree.
PI_FIXTURE_ROOT="/tmp/render-test-content"
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

# Fixture spec: NAME|TYPE|UUID|CONTENT_ROOT
# TYPE=slide          -> --capture-slide UUID --content-root CONTENT_ROOT
# TYPE=transition_mid -> --capture-sb-mid + --fade-from + --fade-to +
#                        --transition + --capture-sb-t; "UUID" field is
#                        encoded as "FROM_UUID,TO_UUID,KIND,T" (no spaces).
#                        CONTENT_ROOT must contain both UUIDs' item.json.
# TYPE=animated_slide -> --capture-slide UUID --capture-slide-at-tick T;
#                        "UUID" field is encoded as "UUID,T" so motion
#                        is sampled at a deterministic phase. Requires
#                        renderer with the 17.2 flag.
# Add more shapes in follow-up commits.
FIXTURES=(
    # 01 · FREE: solid bg, single-line single-layer text. Simplest
    # smoke fixture. Catches regressions in glyph rasterizer +
    # layout + bg fill + atlas geometry.
    #
    # 17.3 / sweep #9 #4: pinned via renderer/tests/fixtures/<UUID>/
    # item.json (snapshot of the on-Pi production seed). The FYS
    # reel is queued for re-authoring per
    # `project_demo_reel_rebuild_changes`; the previous /var/...
    # pointer would have silently invalidated this golden when the
    # rebuild lands. Now the harness scp's the checked-in snapshot
    # to $PI_FIXTURE_ROOT each run and points the fixture there.
    "fys_01_free|slide|3964c302-311f-44f2-a6c9-efd24a16cfc0|$PI_FIXTURE_ROOT"
    # 08 · Tile Chaos: heavy 5L pattern slide. Catches multi-layer
    # composite + per-layer color binding + multi-slide bg cache
    # invalidation. The slide that the FYS heavy bench used.
    # 17.3 snapshot-pinned -- see #01 note.
    "fys_08_tile_chaos|slide|99c11690-415b-40f6-8e3c-6491f3bdf60e|$PI_FIXTURE_ROOT"
    # 09 · Chant Wall: 5L ticker-heavy slide. Pairs with #08 for
    # transition midpoint fixtures.
    # 17.3 snapshot-pinned -- see #01 note.
    "fys_09_chant_wall|slide|2c858968-ae0a-4592-8083-85257de50bcd|$PI_FIXTURE_ROOT"
    # P2-G.fix overlay-route fixture: 2-layer slide where layer 2
    # has blend=overlay, forcing paint_layers_via_overlay_route
    # (legacy 3-pass with run_blit_pass + run_overlay_blend_pass
    # on the hot path). Catches regressions in P2-G's caches that
    # the Normal-blend FYS fixtures wouldn't surface. Synth slide
    # checked in at renderer/tests/fixtures/<UUID>/item.json;
    # pushed to Pi at $PI_FIXTURE_ROOT each run.
    "p2g_overlay_route|slide|f0000000-0000-4000-8000-000000000001|$PI_FIXTURE_ROOT"
    # --- 17.1 / sweep #9 #1: transition-midpoint goldens ---
    # Capture one frame at t=0.5 of a transition between two FYS
    # slides through the atlas-SB pipeline. Covers all 5 SP-portable
    # transitions (one per migration batch + the cut baseline) so
    # shader / blend regressions surface at bit-identical resolution
    # instead of via post-soak eyeball.
    #
    # FROM = fys_01_free (3964c302-...), TO = fys_09_chant_wall
    # (2c858968-...). Same FROM/TO pair across all 5 so the only
    # variable is the transition kind itself.
    "transition_mid_cut|transition_mid|3964c302-311f-44f2-a6c9-efd24a16cfc0,2c858968-ae0a-4592-8083-85257de50bcd,cut,0.5|$PI_FIXTURE_ROOT"
    "transition_mid_fade|transition_mid|3964c302-311f-44f2-a6c9-efd24a16cfc0,2c858968-ae0a-4592-8083-85257de50bcd,fade,0.5|$PI_FIXTURE_ROOT"
    "transition_mid_wipe|transition_mid|3964c302-311f-44f2-a6c9-efd24a16cfc0,2c858968-ae0a-4592-8083-85257de50bcd,wipe,0.5|$PI_FIXTURE_ROOT"
    "transition_mid_slide|transition_mid|3964c302-311f-44f2-a6c9-efd24a16cfc0,2c858968-ae0a-4592-8083-85257de50bcd,slide,0.5|$PI_FIXTURE_ROOT"
    "transition_mid_pixelate|transition_mid|3964c302-311f-44f2-a6c9-efd24a16cfc0,2c858968-ae0a-4592-8083-85257de50bcd,pixelate,0.5|$PI_FIXTURE_ROOT"
    # --- 17.2 / sweep #9 #2: animated-slide tick-pin goldens ---
    # Pin tick_seconds so the per-frame motion compositor evaluates
    # at a deterministic phase. Goldens reproduce bit-identically
    # across runs. Requires --capture-slide-at-tick (Batch 17.2).
    #
    # Coverage chosen from existing snapshot fixtures:
    #   - Chant Wall (09): single-motion ticker. Tick 1.75 picks
    #     mid-cycle (the ticker freq table puts intensity=50 at
    #     ~3s/cycle, so t=1.75 lands ~60% through the first cycle
    #     -- well off the wrap-around edge).
    #   - Tile Chaos (08): 4-layer slide with blink + shake + pulse
    #     + bounce on different layers. ONE capture exercises four
    #     motions at once. Tick 0.5 puts each motion mid-phase.
    #
    # Future motion-coverage additions land here as synthetic
    # single-motion fixtures (or as additional FYS slides if the
    # reel rebuild adds them).
    "animated_ticker_chant|animated_slide|2c858968-ae0a-4592-8083-85257de50bcd,1.75|$PI_FIXTURE_ROOT"
    "animated_multi_chaos|animated_slide|99c11690-415b-40f6-8e3c-6491f3bdf60e,0.5|$PI_FIXTURE_ROOT"
)

echo "==> deploying binary to $TARGET:$BIN_PI"
scp -q "$BIN_HOST" "$TARGET:$BIN_PI"
ssh -q "$TARGET" "test -x $BIN_PI" || { echo "FAIL: binary not exec on Pi"; exit 1; }

echo "==> deploying synth fixtures to $TARGET:$PI_FIXTURE_ROOT"
ssh -q "$TARGET" "mkdir -p $PI_FIXTURE_ROOT"
scp -qr "$FIXTURE_DIR/." "$TARGET:$PI_FIXTURE_ROOT/"

echo "==> stopping openmarquee-backend (DRM master grab)"
ssh -q "$TARGET" "sudo systemctl stop openmarquee-backend"

# 17.5 / sweep #9 #5: capture provenance for bless / diff. When a
# future diff fails, the operator needs to know whether the GOLDEN
# was blessed under a different driver build (vc4) or a different
# Pi (model bump can produce subtly-different rendering).
#
# Gathered once per run so all per-fixture writes share the same
# values. Format mirrors the SystemInfo wire shape: git_sha is the
# code state, blessed_at is run-time UTC, pi_model + vc4_version
# come from the Pi.
GIT_SHA=$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "unknown")
PI_MODEL=$(ssh -q "$TARGET" "tr -d '\0' < /proc/device-tree/model 2>/dev/null" 2>/dev/null || echo "unknown")
# vc4 driver version comes from `modinfo vc4` -- field "version" if
# present, else fall back to the kernel version (vc4 ships in-tree).
VC4_VERSION=$(ssh -q "$TARGET" "modinfo vc4 2>/dev/null | awk '/^version:/{print \$2; exit}' || uname -r" 2>/dev/null || echo "unknown")
BLESSED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

PASS_COUNT=0
FAIL_COUNT=0
for fixture in "${FIXTURES[@]}"; do
    IFS='|' read -r NAME TYPE UUID CONTENT_ROOT <<<"$fixture"
    PI_PATH="/tmp/render-test-$NAME.png"
    LOCAL_PATH="$CAPTURE_DIR/$NAME.png"
    GOLDEN_PATH="$GOLDEN_DIR/$NAME.png"
    case "$TYPE" in
        slide)
            echo
            echo "==> $NAME (capture-slide $UUID, content-root=$CONTENT_ROOT)"
            CAP_LOG="$CAPTURE_DIR/$NAME.log"
            if ! ssh -q "$TARGET" "$BIN_PI --output hdmi --capture-slide $UUID --content-root $CONTENT_ROOT --capture-path $PI_PATH --force-mode $FORCE_MODE" > "$CAP_LOG" 2>&1; then
                echo "    FAIL: capture exited non-zero (see $CAP_LOG)"
                FAIL_COUNT=$((FAIL_COUNT + 1))
                continue
            fi
            scp -q "$TARGET:$PI_PATH" "$LOCAL_PATH"
            ;;
        transition_mid)
            # UUID field encodes "FROM_UUID,TO_UUID,KIND,T" -- split.
            IFS=',' read -r T_FROM T_TO T_KIND T_T <<<"$UUID"
            echo
            echo "==> $NAME (transition-mid $T_KIND@t=$T_T from $T_FROM -> $T_TO)"
            CAP_LOG="$CAPTURE_DIR/$NAME.log"
            if ! ssh -q "$TARGET" "$BIN_PI --output hdmi --capture-sb-mid --fade-from $T_FROM --fade-to $T_TO --transition $T_KIND --capture-sb-t $T_T --content-root $CONTENT_ROOT --capture-path $PI_PATH --force-mode $FORCE_MODE" > "$CAP_LOG" 2>&1; then
                echo "    FAIL: capture exited non-zero (see $CAP_LOG)"
                FAIL_COUNT=$((FAIL_COUNT + 1))
                continue
            fi
            scp -q "$TARGET:$PI_PATH" "$LOCAL_PATH"
            ;;
        animated_slide)
            # UUID field encodes "UUID,TICK" -- split.
            # 17.2: tick pins motion phase so the per-frame
            # animated compositor reproduces bit-identically.
            IFS=',' read -r A_UUID A_TICK <<<"$UUID"
            echo
            echo "==> $NAME (animated-slide $A_UUID @ tick=$A_TICK)"
            CAP_LOG="$CAPTURE_DIR/$NAME.log"
            if ! ssh -q "$TARGET" "$BIN_PI --output hdmi --capture-slide $A_UUID --capture-slide-at-tick $A_TICK --content-root $CONTENT_ROOT --capture-path $PI_PATH --force-mode $FORCE_MODE" > "$CAP_LOG" 2>&1; then
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
    PROVENANCE_PATH="$GOLDEN_DIR/$NAME.golden.json"
    if [ "$BLESS" = "1" ]; then
        cp "$LOCAL_PATH" "$GOLDEN_PATH"
        # 17.5: record provenance so a future diff failure can be
        # disambiguated (renderer regression vs. different driver
        # build vs. different Pi).
        cat > "$PROVENANCE_PATH" <<EOF
{
  "git_sha": "$GIT_SHA",
  "pi_model": "$PI_MODEL",
  "vc4_version": "$VC4_VERSION",
  "blessed_at": "$BLESSED_AT"
}
EOF
        echo "    BLESSED: $LOCAL_PATH -> $GOLDEN_PATH"
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        # 17.5: warn (don't gate) on driver/model mismatch in diff
        # mode. The diff still runs -- a real renderer regression
        # should fail on bit-equality regardless -- but operator
        # gets a heads-up that the failure might be a driver bump
        # rather than a renderer bug.
        if [ -f "$PROVENANCE_PATH" ]; then
            G_VC4=$(awk -F'"' '/vc4_version/{print $4}' "$PROVENANCE_PATH" 2>/dev/null || echo "")
            G_MODEL=$(awk -F'"' '/pi_model/{print $4}' "$PROVENANCE_PATH" 2>/dev/null || echo "")
            if [ -n "$G_VC4" ] && [ "$G_VC4" != "$VC4_VERSION" ]; then
                echo "    WARN: vc4 mismatch -- golden=$G_VC4 vs current=$VC4_VERSION"
            fi
            if [ -n "$G_MODEL" ] && [ "$G_MODEL" != "$PI_MODEL" ]; then
                echo "    WARN: pi_model mismatch -- golden=$G_MODEL vs current=$PI_MODEL"
            fi
        fi
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
