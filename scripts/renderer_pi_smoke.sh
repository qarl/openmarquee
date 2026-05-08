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
grep -qE 'panicked at|RUST_BACKTRACE' "$PROBE_LOG" && \
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

# Phase 4 entry: --play-slide. Asks the Pi to pick the first item_id
# from its live playlist.json so the test isn't tied to a specific
# UUID (seed regenerates them).
echo "==> Phase 4 -- --play-slide (first text_slide from live playlist)"
SLIDE_LOG="$LOG_DIR/play-slide.log"
SLIDE_EXIT=0
SLIDE_ID=$(ssh "$TARGET" "python3 -c \"
import json, pathlib
pl = json.loads(pathlib.Path('/var/openmarquee/playlist.json').read_text())
content_root = pathlib.Path('/var/openmarquee/content')
for playlist in pl.get('playlists', []):
    for item in playlist.get('items', []):
        item_id = item.get('item_id')
        ip = content_root / item_id / 'item.json'
        if not ip.exists(): continue
        env = json.loads(ip.read_text())
        if env.get('item', {}).get('type') == 'text_slide':
            print(item_id)
            raise SystemExit
\"")

# Phase 4.2a: pick a text_slide that has at least one non-empty
# text_layer (FYS slide #01 = "FREE" qualifies). Exercises the layout
# → atlas-upload → glyph-shader → composite path on real DRM scanout.
echo "==> Phase 4.2a -- --play-slide-text (first text_slide with text_layers)"
TEXT_LOG="$LOG_DIR/play-slide-text.log"
TEXT_EXIT=0
TEXT_ID=$(ssh "$TARGET" "python3 -c \"
import json, pathlib
pl = json.loads(pathlib.Path('/var/openmarquee/playlist.json').read_text())
content_root = pathlib.Path('/var/openmarquee/content')
for playlist in pl.get('playlists', []):
    for item in playlist.get('items', []):
        item_id = item.get('item_id')
        ip = content_root / item_id / 'item.json'
        if not ip.exists(): continue
        env = json.loads(ip.read_text())
        it = env.get('item', {})
        if it.get('type') != 'text_slide': continue
        layers = it.get('text_layers') or []
        if any(l.get('text') for l in layers):
            print(item_id)
            raise SystemExit
\"" || true)

# Phase 4.1b: same but specifically picks a slide whose
# background_pattern is "gradient" so we exercise the fragment-shader
# path, not just the clear_color path. FYS has 2 such slides.
echo "==> Phase 4.1b -- --play-slide (first GRADIENT-pattern slide)"
GRAD_LOG="$LOG_DIR/play-slide-gradient.log"
GRAD_EXIT=0
GRAD_ID=$(ssh "$TARGET" "python3 -c \"
import json, pathlib
pl = json.loads(pathlib.Path('/var/openmarquee/playlist.json').read_text())
content_root = pathlib.Path('/var/openmarquee/content')
for playlist in pl.get('playlists', []):
    for item in playlist.get('items', []):
        item_id = item.get('item_id')
        ip = content_root / item_id / 'item.json'
        if not ip.exists(): continue
        env = json.loads(ip.read_text())
        it = env.get('item', {})
        if it.get('type') != 'text_slide': continue
        bp = it.get('background_pattern')
        if bp and bp.get('pattern') == 'gradient':
            print(item_id)
            raise SystemExit
\"" || true)
if [ -z "${SLIDE_ID:-}" ]; then
    echo "FAIL: couldn't find a text_slide in /var/openmarquee/playlist.json"
    exit 1
fi
ssh "$TARGET" "$BIN_PI --output hdmi --play-slide $SLIDE_ID --hold-secs 3" \
    > "$SLIDE_LOG" 2>&1 || SLIDE_EXIT=$?

# Gradient-pattern slide is optional — if the seed doesn't include
# one, skip the assertion rather than failing. FYS does include
# them (slides "06 · Uncage!!" and "10 · Scream").
if [ -n "${GRAD_ID:-}" ]; then
    ssh "$TARGET" "$BIN_PI --output hdmi --play-slide $GRAD_ID --hold-secs 3" \
        > "$GRAD_LOG" 2>&1 || GRAD_EXIT=$?
fi

# Text-layer render is optional only on the off chance the seed has
# no text_layers anywhere — every FYS slide does, so on the dev Pi
# this is always exercised.
if [ -n "${TEXT_ID:-}" ]; then
    ssh "$TARGET" "$BIN_PI --output hdmi --play-slide-text $TEXT_ID --hold-secs 3" \
        > "$TEXT_LOG" 2>&1 || TEXT_EXIT=$?
fi

# Phase 5-a: FBO render-to-texture parity. Same slide as the text
# path but routed through the offscreen-FBO blit rather than direct
# render. Output should be visually identical; the smoke just
# asserts the FBO path completes cleanly + still rasterizes text
# (proving paint_slide is being called inside the FBO bind).
echo "==> Phase 5-a -- --play-slide-via-fbo (FBO path parity)"
FBO_LOG="$LOG_DIR/play-slide-via-fbo.log"
FBO_EXIT=0
if [ -n "${TEXT_ID:-}" ]; then
    ssh "$TARGET" "$BIN_PI --output hdmi --play-slide-via-fbo $TEXT_ID --hold-secs 3" \
        > "$FBO_LOG" 2>&1 || FBO_EXIT=$?
fi

# Phase 5-b-1: dual-FBO + FS_FADE single-frame composite at t=0.5.
# Picks the first two text slides from the playlist as a/b. The
# fade composite renders BOTH slides into FBOs once and blends them
# at the given t — at t=0.5 we expect both to rasterize text.
echo "==> Phase 5-b-1 -- --fade-from/to (single-frame fade @ t=0.5)"
FADE_LOG="$LOG_DIR/fade-composite.log"
FADE_EXIT=0
FADE_PAIR=$(ssh "$TARGET" "python3 -c \"
import json, pathlib
pl = json.loads(pathlib.Path('/var/openmarquee/playlist.json').read_text())
content_root = pathlib.Path('/var/openmarquee/content')
ids = []
for playlist in pl.get('playlists', []):
    for item in playlist.get('items', []):
        item_id = item.get('item_id')
        ip = content_root / item_id / 'item.json'
        if not ip.exists(): continue
        env = json.loads(ip.read_text())
        it = env.get('item', {})
        if it.get('type') != 'text_slide': continue
        ids.append(item_id)
        if len(ids) == 2:
            print(' '.join(ids))
            raise SystemExit
\"" || true)
FADE_FROM=$(echo "$FADE_PAIR" | awk '{print $1}')
FADE_TO=$(echo "$FADE_PAIR" | awk '{print $2}')
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --fade-t 0.5 --hold-secs 3" \
        > "$FADE_LOG" 2>&1 || FADE_EXIT=$?
fi

# Phase 5-b-2 / 5-c: animated transitions. Same slide pair driven
# over transition_ms by a per-frame loop. We exercise the three
# transition kinds 5-c-1 ships: cut + fade + wipe. Each is its own
# smoke run since `--transition` selects which shader runs.
# Wall-clock timing per run so the assertion can catch GPU
# saturation: if the loop emits the expected frame count but takes
# WAY longer than transition_ms (because per-frame work missed its
# 33ms budget), the gate must fail loudly instead of going
# silent-slow. This bites hardest at 1080p.
echo "==> Phase 5-c-1 -- --animate-fade --transition cut (per-frame @ 500ms / 30fps)"
ANCUT_LOG="$LOG_DIR/animate-cut.log"
ANCUT_EXIT=0
ANCUT_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANCUT_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition cut --transition-ms 500 --fps 30" \
        > "$ANCUT_LOG" 2>&1 || ANCUT_EXIT=$?
    ANCUT_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANCUT_WALL_MS=$((ANCUT_END - ANCUT_START))
fi

echo "==> Phase 5-b-2 -- --animate-fade --transition fade (per-frame @ 800ms / 30fps)"
ANFADE_LOG="$LOG_DIR/animate-fade.log"
ANFADE_EXIT=0
ANFADE_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANFADE_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition fade --transition-ms 800 --fps 30" \
        > "$ANFADE_LOG" 2>&1 || ANFADE_EXIT=$?
    ANFADE_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANFADE_WALL_MS=$((ANFADE_END - ANFADE_START))
fi

echo "==> Phase 5-c-1 -- --animate-fade --transition wipe (per-frame @ 800ms / 30fps)"
ANWIPE_LOG="$LOG_DIR/animate-wipe.log"
ANWIPE_EXIT=0
ANWIPE_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANWIPE_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition wipe --transition-ms 800 --fps 30" \
        > "$ANWIPE_LOG" 2>&1 || ANWIPE_EXIT=$?
    ANWIPE_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANWIPE_WALL_MS=$((ANWIPE_END - ANWIPE_START))
fi

echo "==> Phase 5-c-2 -- --animate-fade --transition iris (per-frame @ 800ms / 30fps)"
ANIRIS_LOG="$LOG_DIR/animate-iris.log"
ANIRIS_EXIT=0
ANIRIS_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANIRIS_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition iris --transition-ms 800 --fps 30" \
        > "$ANIRIS_LOG" 2>&1 || ANIRIS_EXIT=$?
    ANIRIS_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANIRIS_WALL_MS=$((ANIRIS_END - ANIRIS_START))
fi

echo "==> Phase 5-c-2 -- --animate-fade --transition dissolve (per-frame @ 800ms / 30fps)"
ANDIS_LOG="$LOG_DIR/animate-dissolve.log"
ANDIS_EXIT=0
ANDIS_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANDIS_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition dissolve --transition-ms 800 --fps 30" \
        > "$ANDIS_LOG" 2>&1 || ANDIS_EXIT=$?
    ANDIS_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANDIS_WALL_MS=$((ANDIS_END - ANDIS_START))
fi

# Phase 5-c-3: pixelate, scanline, halftone.
echo "==> Phase 5-c-3 -- --animate-fade --transition pixelate (per-frame @ 800ms / 30fps)"
ANPIX_LOG="$LOG_DIR/animate-pixelate.log"
ANPIX_EXIT=0
ANPIX_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANPIX_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition pixelate --transition-ms 800 --fps 30" \
        > "$ANPIX_LOG" 2>&1 || ANPIX_EXIT=$?
    ANPIX_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANPIX_WALL_MS=$((ANPIX_END - ANPIX_START))
fi

echo "==> Phase 5-c-3 -- --animate-fade --transition scanline (per-frame @ 800ms / 30fps)"
ANSCAN_LOG="$LOG_DIR/animate-scanline.log"
ANSCAN_EXIT=0
ANSCAN_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANSCAN_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition scanline --transition-ms 800 --fps 30" \
        > "$ANSCAN_LOG" 2>&1 || ANSCAN_EXIT=$?
    ANSCAN_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANSCAN_WALL_MS=$((ANSCAN_END - ANSCAN_START))
fi

echo "==> Phase 5-c-3 -- --animate-fade --transition halftone (per-frame @ 800ms / 30fps)"
ANHALF_LOG="$LOG_DIR/animate-halftone.log"
ANHALF_EXIT=0
ANHALF_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANHALF_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition halftone --transition-ms 800 --fps 30" \
        > "$ANHALF_LOG" 2>&1 || ANHALF_EXIT=$?
    ANHALF_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANHALF_WALL_MS=$((ANHALF_END - ANHALF_START))
fi

# Phase 5-c-4: glitch + slide + push + scroll + blinds + flip +
# marquee + shutter. Each at 500ms (half the cut/fade/wipe duration)
# to keep the smoke wall-clock under ~30s for the whole script —
# 8 × ~1.5s = ~12s. Frame floor adjusted to 12 frames at 500ms/30fps.
run_anim_smoke() {
    local kind="$1"
    local out_var="$2"
    local exit_var="$3"
    local wall_var="$4"
    local log_path="$LOG_DIR/animate-$kind.log"
    local _exit=0
    if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
        local _start
        _start=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
        ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition $kind --transition-ms 500 --fps 30" \
            > "$log_path" 2>&1 || _exit=$?
        local _end
        _end=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
        local _wall=$((_end - _start))
        eval "$out_var=$log_path"
        eval "$exit_var=$_exit"
        eval "$wall_var=$_wall"
    else
        eval "$out_var=$log_path"
        eval "$exit_var=0"
        eval "$wall_var=0"
    fi
}
echo "==> Phase 5-c-4 -- 8 remaining transitions @ 500ms each"
run_anim_smoke "glitch"  ANGLI_LOG ANGLI_EXIT ANGLI_WALL_MS
run_anim_smoke "slide"   ANSLD_LOG ANSLD_EXIT ANSLD_WALL_MS
run_anim_smoke "push"    ANPSH_LOG ANPSH_EXIT ANPSH_WALL_MS
run_anim_smoke "scroll"  ANSCR_LOG ANSCR_EXIT ANSCR_WALL_MS
run_anim_smoke "blinds"  ANBLN_LOG ANBLN_EXIT ANBLN_WALL_MS
run_anim_smoke "flip"    ANFLP_LOG ANFLP_EXIT ANFLP_WALL_MS
run_anim_smoke "marquee" ANMRQ_LOG ANMRQ_EXIT ANMRQ_WALL_MS
run_anim_smoke "shutter" ANSHT_LOG ANSHT_EXIT ANSHT_WALL_MS

# v1-spec-delta #2 (slice d-smoke): exercise each motion kind on
# real DRM scanout. FYS today has zero animated layers, so this
# is the only on-Pi exercise of render_animated_slide. Each kind
# is rendered as a synthesized in-memory test slide for 1 second;
# we assert that no kind panics and that animated kinds (every
# value except `static`) report a frame count >= 20 (the
# per-frame loop must not stall).
echo "==> Phase d-smoke -- --play-motion-test for all 7 kinds"
MOTION_LOG="$LOG_DIR/motion-test.log"
MOTION_EXIT=0
ssh "$TARGET" "for k in static ticker breathe pulse bounce shake blink; do echo \"=== motion-kind=\$k ===\"; $BIN_PI --output hdmi --play-motion-test \$k --hold-secs 1 2>&1 || echo \"FAIL: kind=\$k exit=\$?\"; done" \
    > "$MOTION_LOG" 2>&1 || MOTION_EXIT=$?

# v1-spec-delta #2 (slice d-smoke): exercise motion through
# transitions. Both source and destination slides have animated
# layers so the per-frame FBO rebake path runs for both. Asserts
# no panic and that the animated transition completes with a
# frame count >= 20 (catches a stalled per-frame rebake).
echo "==> Phase d-smoke -- --play-motion-transition (motion through transitions)"
MOTION_TRANS_LOG="$LOG_DIR/motion-transition.log"
MOTION_TRANS_EXIT=0
ssh "$TARGET" "for pair in ticker,breathe shake,blink pulse,bounce; do echo \"=== motion-transition pair=\$pair via fade ===\"; $BIN_PI --output hdmi --play-motion-transition \$pair --transition fade --transition-ms 800 --fps 30 2>&1 || echo \"FAIL: pair=\$pair exit=\$?\"; done" \
    > "$MOTION_TRANS_LOG" 2>&1 || MOTION_TRANS_EXIT=$?

# Phase 6: full playlist-driven reel. Single pass through the
# live FYS playlist with --hold-secs 1 (compress hold to keep
# smoke tractable; production would use slide.duration_ms).
echo "==> Phase 6 -- --play-reel (single pass FYS playlist)"
REEL_LOG="$LOG_DIR/play-reel.log"
REEL_EXIT=0
ssh "$TARGET" "$BIN_PI --output hdmi --play-reel --hold-secs 1 --fps 30" \
    > "$REEL_LOG" 2>&1 || REEL_EXIT=$?

# Unknown-kind fallback: pick a name that's NOT in the deck.
echo "==> Phase 5-c -- --animate-fade --transition nonexistent (unknown→cut fallback)"
ANUNK_LOG="$LOG_DIR/animate-unknown.log"
ANUNK_EXIT=0
ANUNK_WALL_MS=0
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    ANUNK_START=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ssh "$TARGET" "$BIN_PI --output hdmi --fade-from $FADE_FROM --fade-to $FADE_TO --animate-fade --transition nonexistent --transition-ms 500 --fps 30" \
        > "$ANUNK_LOG" 2>&1 || ANUNK_EXIT=$?
    ANUNK_END=$(python3 -c 'import time; print(int(time.monotonic()*1000))')
    ANUNK_WALL_MS=$((ANUNK_END - ANUNK_START))
fi

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
grep -qE 'panicked at|RUST_BACKTRACE' "$COLOR_LOG" && \
    { echo "FAIL: panic in --solid-color output"; exit 1; }
echo "    --solid-color ok"

if [ "$ANIM_EXIT" -ne 0 ]; then
    echo "FAIL: --animate exit $ANIM_EXIT"
    cat "$ANIM_LOG"
    exit 1
fi
grep -q 'animated atomic render complete' "$ANIM_LOG" || \
    { echo "FAIL: --animate didn't print completion line"; cat "$ANIM_LOG"; exit 1; }
grep -qE 'panicked at|RUST_BACKTRACE' "$ANIM_LOG" && \
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

if [ "$SLIDE_EXIT" -ne 0 ]; then
    echo "FAIL: --play-slide exit $SLIDE_EXIT"
    cat "$SLIDE_LOG"
    exit 1
fi
# Phase 4.2b: --play-slide now renders bg + first text layer in
# ONE frame. The unified completion line is "slide render complete";
# FYS slide #01 has text_layers so "rasterized text" must also fire.
grep -q 'slide render complete' "$SLIDE_LOG" || \
    { echo "FAIL: --play-slide didn't complete the unified render"; cat "$SLIDE_LOG"; exit 1; }
grep -qE 'panicked at|RUST_BACKTRACE' "$SLIDE_LOG" && \
    { echo "FAIL: panic in --play-slide output"; exit 1; }
grep -q "rendering slide $SLIDE_ID" "$SLIDE_LOG" || \
    { echo "FAIL: --play-slide didn't log the slide id we requested"; cat "$SLIDE_LOG"; exit 1; }
grep -q 'rasterized text' "$SLIDE_LOG" || \
    { echo "FAIL: --play-slide didn't rasterize a text layer (FYS #01 has text — fold regression?)"; cat "$SLIDE_LOG"; exit 1; }
echo "    --play-slide ok ($SLIDE_ID, bg + text composited)"

# Gradient assertion is conditional on the seed having a gradient
# slide. When present, the unified render must log
# pattern=gradient AND rasterize text (FYS gradient slides do have
# text_layers — that's the whole point of 4.2b).
if [ -n "${GRAD_ID:-}" ]; then
    if [ "$GRAD_EXIT" -ne 0 ]; then
        echo "FAIL: --play-slide gradient exit $GRAD_EXIT"
        cat "$GRAD_LOG"
        exit 1
    fi
    grep -q 'slide render complete' "$GRAD_LOG" || \
        { echo "FAIL: gradient slide didn't complete via unified render"; cat "$GRAD_LOG"; exit 1; }
    grep -qE 'panicked at|RUST_BACKTRACE' "$GRAD_LOG" && \
        { echo "FAIL: panic in gradient slide output"; exit 1; }
    grep -q "pattern=gradient" "$GRAD_LOG" || \
        { echo "FAIL: gradient slide didn't log pattern=gradient (might've fallen back?)"; cat "$GRAD_LOG"; exit 1; }
    grep -q 'rasterized text' "$GRAD_LOG" || \
        { echo "FAIL: gradient slide didn't rasterize text — 4.2b's whole point is gradient + text in ONE frame"; cat "$GRAD_LOG"; exit 1; }
    echo "    --play-slide gradient ok ($GRAD_ID, gradient bg + text composited)"
else
    echo "    --play-slide gradient skipped (no gradient slide in seed)"
fi

# Phase 4.2a text-layer assertion (kept post-4.2b as the
# playlist-bypass path's smoke): completion line + no panics + the
# layer log line ("rasterized text") is present so we know the
# layout pass actually ran (and didn't fall through to a None path).
if [ -n "${TEXT_ID:-}" ]; then
    if [ "$TEXT_EXIT" -ne 0 ]; then
        echo "FAIL: --play-slide-text exit $TEXT_EXIT"
        cat "$TEXT_LOG"
        exit 1
    fi
    grep -q 'slide render complete' "$TEXT_LOG" || \
        { echo "FAIL: --play-slide-text didn't complete"; cat "$TEXT_LOG"; exit 1; }
    grep -qE 'panicked at|RUST_BACKTRACE' "$TEXT_LOG" && \
        { echo "FAIL: panic in --play-slide-text output"; exit 1; }
    grep -q 'rasterized text' "$TEXT_LOG" || \
        { echo "FAIL: --play-slide-text didn't log rasterization (layout fell through?)"; cat "$TEXT_LOG"; exit 1; }
    echo "    --play-slide-text ok ($TEXT_ID)"
else
    echo "    --play-slide-text skipped (no text-layer slide in seed)"
fi

# Phase 5-a FBO-path assertion: completion line + no panics +
# rasterized text fired (proves paint_slide ran inside the FBO
# bind, not just a black blit).
if [ -n "${TEXT_ID:-}" ]; then
    if [ "$FBO_EXIT" -ne 0 ]; then
        echo "FAIL: --play-slide-via-fbo exit $FBO_EXIT"
        cat "$FBO_LOG"
        exit 1
    fi
    grep -q 'slide render complete (via FBO)' "$FBO_LOG" || \
        { echo "FAIL: --play-slide-via-fbo didn't complete via FBO path"; cat "$FBO_LOG"; exit 1; }
    grep -qE 'panicked at|RUST_BACKTRACE' "$FBO_LOG" && \
        { echo "FAIL: panic in --play-slide-via-fbo output"; exit 1; }
    grep -q 'rasterized text' "$FBO_LOG" || \
        { echo "FAIL: --play-slide-via-fbo didn't paint text inside the FBO"; cat "$FBO_LOG"; exit 1; }
    echo "    --play-slide-via-fbo ok ($TEXT_ID)"
else
    echo "    --play-slide-via-fbo skipped (no text-layer slide in seed)"
fi

# Phase 5-b-1 fade-composite assertion: completion + both slides
# log a "rasterized text" line (proves both make_slide_fbo calls
# ran and paint_slide fired inside each FBO bind).
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    if [ "$FADE_EXIT" -ne 0 ]; then
        echo "FAIL: --fade-from/to exit $FADE_EXIT"
        cat "$FADE_LOG"
        exit 1
    fi
    grep -q 'fade composite render complete' "$FADE_LOG" || \
        { echo "FAIL: fade composite didn't complete"; cat "$FADE_LOG"; exit 1; }
    grep -qE 'panicked at|RUST_BACKTRACE' "$FADE_LOG" && \
        { echo "FAIL: panic in fade composite output"; exit 1; }
    # Two FBOs → at minimum 1 rasterized-text line per slide that
    # has any. FYS slides always have text, so at least 1 should
    # appear. (If both slides have text, expect 2 lines.)
    RAST_COUNT=$(grep -c 'rasterized text' "$FADE_LOG" || true)
    if [ "${RAST_COUNT:-0}" -lt 1 ]; then
        echo "FAIL: fade composite didn't paint any text (got $RAST_COUNT lines)"
        cat "$FADE_LOG"
        exit 1
    fi
    echo "    --fade-from/to ok ($FADE_FROM → $FADE_TO @ t=0.5, $RAST_COUNT text rasterizations)"
else
    echo "    --fade-from/to skipped (couldn't find 2 text slides in seed)"
fi

# Phase 5-b-2 / 5-c-1 animated-transition assertions. Each kind:
# completion line ("animated transition complete: kind=KIND ..."),
# no panics, frame-count floor. cut runs at 500ms = 15 frames @
# 30fps (floor 12); fade/wipe at 800ms = 24 frames (floor 20).
assert_anim_transition() {
    local kind="$1"
    local log="$2"
    local exit_code="$3"
    local floor="$4"
    local transition_ms="$5"
    local wall_ms="$6"
    if [ "$exit_code" -ne 0 ]; then
        echo "FAIL: --animate-fade --transition $kind exit $exit_code"
        cat "$log"
        exit 1
    fi
    grep -q "animated transition complete: kind=\"$kind\"" "$log" || \
        { echo "FAIL: animated $kind didn't print expected completion line"; cat "$log"; exit 1; }
    grep -qE 'panicked at|RUST_BACKTRACE' "$log" && \
        { echo "FAIL: panic in animated $kind output"; exit 1; }
    local frames
    frames=$(grep -oE 'rendered [0-9]+ frames' "$log" | grep -oE '[0-9]+' | head -1)
    if [ -z "${frames:-}" ] || [ "$frames" -lt "$floor" ]; then
        echo "FAIL: --animate-fade --transition $kind frame count too low (got '${frames:-none}', want >=$floor)"
        cat "$log"
        exit 1
    fi
    # Wall-clock upper bound: transition_ms + 2000ms slack for ssh
    # roundtrip + EGL bring-up + cleanup. Catches GPU saturation
    # (loop emits the expected frame count but each frame missed
    # its 33ms budget, so wall-clock blows past transition_ms).
    # Bumped from 1500ms after 5-c-1 verify saw 27ms margin on the
    # 800ms wipe (2273ms vs 2300ms cap) — ssh latency wobble alone
    # could false-fail. Tightens to a per-frame budget assertion
    # once 1080p bring-up happens (where GPU saturation becomes
    # the real risk vs ssh wobble).
    local cap_ms=$((transition_ms + 2000))
    if [ "$wall_ms" -gt "$cap_ms" ]; then
        echo "FAIL: --animate-fade --transition $kind wall-clock too high (got ${wall_ms}ms, cap ${cap_ms}ms)"
        echo "      possible GPU saturation — frames missed their per-frame budget"
        cat "$log"
        exit 1
    fi
    echo "    --animate-fade --transition $kind ok ($frames frames in ${wall_ms}ms wall-clock)"
}
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    assert_anim_transition "cut"      "$ANCUT_LOG"  "$ANCUT_EXIT"  12 500 "$ANCUT_WALL_MS"
    assert_anim_transition "fade"     "$ANFADE_LOG" "$ANFADE_EXIT" 20 800 "$ANFADE_WALL_MS"
    assert_anim_transition "wipe"     "$ANWIPE_LOG" "$ANWIPE_EXIT" 20 800 "$ANWIPE_WALL_MS"
    assert_anim_transition "iris"     "$ANIRIS_LOG" "$ANIRIS_EXIT" 20 800 "$ANIRIS_WALL_MS"
    assert_anim_transition "dissolve" "$ANDIS_LOG"  "$ANDIS_EXIT"  20 800 "$ANDIS_WALL_MS"
    assert_anim_transition "pixelate" "$ANPIX_LOG"  "$ANPIX_EXIT"  20 800 "$ANPIX_WALL_MS"
    assert_anim_transition "scanline" "$ANSCAN_LOG" "$ANSCAN_EXIT" 20 800 "$ANSCAN_WALL_MS"
    assert_anim_transition "halftone" "$ANHALF_LOG" "$ANHALF_EXIT" 20 800 "$ANHALF_WALL_MS"
    # 5-c-4 batch — all at 500ms / 12-frame floor.
    assert_anim_transition "glitch"   "$ANGLI_LOG" "$ANGLI_EXIT" 12 500 "$ANGLI_WALL_MS"
    assert_anim_transition "slide"    "$ANSLD_LOG" "$ANSLD_EXIT" 12 500 "$ANSLD_WALL_MS"
    assert_anim_transition "push"     "$ANPSH_LOG" "$ANPSH_EXIT" 12 500 "$ANPSH_WALL_MS"
    assert_anim_transition "scroll"   "$ANSCR_LOG" "$ANSCR_EXIT" 12 500 "$ANSCR_WALL_MS"
    assert_anim_transition "blinds"   "$ANBLN_LOG" "$ANBLN_EXIT" 12 500 "$ANBLN_WALL_MS"
    assert_anim_transition "flip"     "$ANFLP_LOG" "$ANFLP_EXIT" 12 500 "$ANFLP_WALL_MS"
    assert_anim_transition "marquee"  "$ANMRQ_LOG" "$ANMRQ_EXIT" 12 500 "$ANMRQ_WALL_MS"
    assert_anim_transition "shutter"  "$ANSHT_LOG" "$ANSHT_EXIT" 12 500 "$ANSHT_WALL_MS"
    # Unknown-kind fallback: the renderer keeps the REQUESTED kind
    # in its log line ("kind=\"pixelate\"") so operators can
    # correlate logs with what they asked for; the warn line above
    # is what proves the FS fallback to FS_CUT actually ran. Both
    # must be present.
    if [ "$ANUNK_EXIT" -ne 0 ]; then
        echo "FAIL: --transition pixelate (unknown) exit $ANUNK_EXIT"
        cat "$ANUNK_LOG"
        exit 1
    fi
    # The full deck is implemented in 5-c-4; pick a name that
    # genuinely isn't in the dispatch.
    UNKNOWN_KIND="nonexistent"
    grep -q "warn: transition kind \"$UNKNOWN_KIND\" not yet implemented" "$ANUNK_LOG" || \
        { echo "FAIL: unknown-kind warn didn't fire"; cat "$ANUNK_LOG"; exit 1; }
    # The completion line keeps the REQUESTED kind so the operator
    # can correlate logs with what they asked for; the warn above
    # is what proves the FS fallback to FS_CUT actually ran. Both
    # must be present.
    grep -q "animated transition complete: kind=\"$UNKNOWN_KIND\"" "$ANUNK_LOG" || \
        { echo "FAIL: unknown-kind completion line missing"; cat "$ANUNK_LOG"; exit 1; }
    echo "    --animate-fade --transition $UNKNOWN_KIND ok (unknown → cut fallback fired)"
else
    echo "    --animate-fade skipped (couldn't find 2 text slides in seed)"
fi

# v1-spec-delta #2 (slice d-smoke) motion-kind sweep assertions.
# Static must complete via the one-shot harness ("slide render
# complete" without an "animated slide complete" line); the other
# six animated kinds must complete via the per-frame loop and
# render >= 20 frames in 1 s wall-clock (catches a stalled per-
# frame loop that times out without rendering).
if [ "$MOTION_EXIT" -ne 0 ]; then
    echo "FAIL: --play-motion-test sweep exit $MOTION_EXIT"
    cat "$MOTION_LOG"
    exit 1
fi
grep -qE 'panicked at|RUST_BACKTRACE' "$MOTION_LOG" && \
    { echo "FAIL: panic in --play-motion-test output"; cat "$MOTION_LOG"; exit 1; }
for kind in static ticker breathe pulse bounce shake blink; do
    grep -q "=== motion-kind=$kind ===" "$MOTION_LOG" || \
        { echo "FAIL: motion-kind=$kind didn't run"; exit 1; }
    grep -q "FAIL: kind=$kind" "$MOTION_LOG" && \
        { echo "FAIL: motion-kind=$kind reported nonzero exit"; cat "$MOTION_LOG"; exit 1; }
done
# Static -> one-shot path; only "slide render complete" line.
# Per-frame loop is gated on any_animated, so static MUST NOT
# emit "animated slide complete".
ANIM_LINES=$(grep -c 'animated slide complete' "$MOTION_LOG" || true)
if [ "$ANIM_LINES" -lt 6 ]; then
    echo "FAIL: --play-motion-test expected >=6 'animated slide complete' lines (one per non-static kind), got $ANIM_LINES"
    cat "$MOTION_LOG"
    exit 1
fi
# Frame-count floor: each animated kind held for 1 s should hit
# the spec §11 30 fps target (=30 frames). v1-spec-delta #3 (b)
# glyph cache (commit 9368e0e) eliminated the per-frame fontdue
# rasterization on motion paths; a regression to per-frame raster
# would land at ~15 fps. Floor at 25 catches the regression class
# (anything below 25 indicates either the cache stopped working
# OR a new fontdue path was introduced) while allowing ±5 frame
# jitter from the 30 fps target. QA F1 bump 2026-05-08.
LOWEST_FRAMES=$(grep -oE 'animated slide complete: [0-9]+ frames' "$MOTION_LOG" | grep -oE '[0-9]+' | sort -n | head -1)
if [ -z "${LOWEST_FRAMES:-}" ] || [ "$LOWEST_FRAMES" -lt 25 ]; then
    echo "FAIL: --play-motion-test min frame count $LOWEST_FRAMES < 25 floor (cache regression -- expected ~30 fps)"
    cat "$MOTION_LOG"
    exit 1
fi
echo "    --play-motion-test ok (7 kinds, $ANIM_LINES animated, min $LOWEST_FRAMES frames/sec)"

# v1-spec-delta #2 (slice d-smoke) motion-transition assertions.
if [ "$MOTION_TRANS_EXIT" -ne 0 ]; then
    echo "FAIL: --play-motion-transition sweep exit $MOTION_TRANS_EXIT"
    cat "$MOTION_TRANS_LOG"
    exit 1
fi
grep -qE 'panicked at|RUST_BACKTRACE' "$MOTION_TRANS_LOG" && \
    { echo "FAIL: panic in --play-motion-transition output"; cat "$MOTION_TRANS_LOG"; exit 1; }
TRANS_LINES=$(grep -c 'animated transition complete' "$MOTION_TRANS_LOG" || true)
if [ "$TRANS_LINES" -lt 3 ]; then
    echo "FAIL: --play-motion-transition expected >=3 'animated transition complete' lines, got $TRANS_LINES"
    cat "$MOTION_TRANS_LOG"
    exit 1
fi
# Frame-count floor: render_transition_animated parametrizes
# total_frames = round(transition_ms/1000 * fps). At
# transition_ms=800 / fps=30 that's 24. Anything <20 indicates
# either total_frames was changed silently OR the loop stalled.
# Wall-clock perf to actually hit the 30 fps target is bound by
# legacy drmModeSetCrtc per frame -- tracked separately under
# v1-spec-delta #5 (atomic + persistent context). QA F1 bump
# 2026-05-08: floor was 10, now 20.
LOWEST_TRANS_FRAMES=$(grep -oE 'animated transition complete: kind="[a-z]+" rendered [0-9]+ frames' "$MOTION_TRANS_LOG" | grep -oE '[0-9]+ frames' | grep -oE '[0-9]+' | sort -n | head -1)
if [ -z "${LOWEST_TRANS_FRAMES:-}" ] || [ "$LOWEST_TRANS_FRAMES" -lt 20 ]; then
    echo "FAIL: --play-motion-transition min frame count $LOWEST_TRANS_FRAMES < 20 floor"
    cat "$MOTION_TRANS_LOG"
    exit 1
fi
echo "    --play-motion-transition ok ($TRANS_LINES transitions, min $LOWEST_TRANS_FRAMES frames per transition)"

# v1-spec-delta #3 (slice d) auto-mode smoke. Synthesized auto_mode
# slides (time / date / day) over a 4s hold; the spec says auto-
# mode text ticks every second, so a `time` slide must rasterize
# AT LEAST 3 distinct values during a 4s hold. Catches: stalled
# per-frame loop, format breakage, clock-source wedge.
echo "==> Phase d-smoke -- --play-auto-mode-test (time / date / day)"
AUTO_LOG="$LOG_DIR/auto-mode-test.log"
AUTO_EXIT=0
ssh "$TARGET" "for k in time date day; do echo \"=== auto-mode-test \$k ===\"; $BIN_PI --output hdmi --play-auto-mode-test \$k --hold-secs 4 2>&1 || echo \"FAIL: kind=\$k exit=\$?\"; done" \
    > "$AUTO_LOG" 2>&1 || AUTO_EXIT=$?
if [ "$AUTO_EXIT" -ne 0 ]; then
    echo "FAIL: --play-auto-mode-test sweep exit $AUTO_EXIT"
    cat "$AUTO_LOG"
    exit 1
fi
grep -qE 'panicked at|RUST_BACKTRACE' "$AUTO_LOG" && \
    { echo "FAIL: panic in --play-auto-mode-test"; cat "$AUTO_LOG"; exit 1; }
for kind in time date day; do
    grep -q "=== auto-mode-test $kind ===" "$AUTO_LOG" || \
        { echo "FAIL: auto-mode-test $kind didn't run"; exit 1; }
done
# Time slide must show >=3 distinct text values across the hold
# (the seconds digit ticks). awk extracts the rasterized text
# value, sort -u counts uniques. Date and day are stable across a
# 4s window so they show 1 each.
DISTINCT_TIMES=$(awk '/=== auto-mode-test time ===/{capture=1;next} /=== auto-mode-test date ===/{capture=0} capture && /rasterized text "[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"/{print $0}' "$AUTO_LOG" | grep -oE '"[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"' | sort -u | wc -l | tr -d ' ')
if [ "${DISTINCT_TIMES:-0}" -lt 3 ]; then
    echo "FAIL: --play-auto-mode-test time showed only $DISTINCT_TIMES distinct values across 4s (clock not ticking)"
    cat "$AUTO_LOG"
    exit 1
fi
echo "    --play-auto-mode-test ok (3 kinds, time ticked $DISTINCT_TIMES distinct seconds across 4s)"

# v1-spec-delta #4 (slice b/d) outline smoke. Synthesizes a slide
# with layer.outline=true; asserts no panic + no GLSL link error
# from FS_GLYPH_OUTLINE. Visual verification (1-px black ring) is
# manual; smoke gates pipeline correctness.
echo "==> Phase d-smoke -- --play-outline-test"
OUTLINE_LOG="$LOG_DIR/outline-test.log"
OUTLINE_EXIT=0
ssh "$TARGET" "$BIN_PI --output hdmi --play-outline-test --hold-secs 2" \
    > "$OUTLINE_LOG" 2>&1 || OUTLINE_EXIT=$?
if [ "$OUTLINE_EXIT" -ne 0 ]; then
    echo "FAIL: --play-outline-test exit $OUTLINE_EXIT"
    cat "$OUTLINE_LOG"
    exit 1
fi
grep -qE 'panicked at|RUST_BACKTRACE' "$OUTLINE_LOG" && \
    { echo "FAIL: panic in --play-outline-test"; cat "$OUTLINE_LOG"; exit 1; }
grep -q 'slide render complete' "$OUTLINE_LOG" || \
    { echo "FAIL: --play-outline-test didn't reach slide render complete"; cat "$OUTLINE_LOG"; exit 1; }
grep -q 'rasterized text "OUTLINE TEST"' "$OUTLINE_LOG" || \
    { echo "FAIL: --play-outline-test didn't rasterize the test text"; cat "$OUTLINE_LOG"; exit 1; }
echo "    --play-outline-test ok (FS_GLYPH_OUTLINE linked + drew on hw)"

# v1-spec-delta #6 (slice b+) procedural pattern smoke. Renders a
# synthesized slide for each pattern whose shader has landed and
# asserts no panic + slide render complete + cyan/orange test
# colors composited (color_a #00BFFF / color_b #FF6B00 hex).
# Patterns whose shader hasn't landed warn-and-fall to color_a;
# they're left out of this list until they ship. The list grows
# slice-by-slice (b: stripes/checker/dots; c: ...; d: ...).
PATTERN_KINDS_IMPL="stripes checker dots halftone scanlines grid rings rays bricks confetti"
# v1-spec-delta #7 -- blend modes shipped by slice. (b) lands
# screen + multiply via blend func tweaks; (c) lands overlay via
# FBO sample. normal is shipped from day one.
BLEND_KINDS_IMPL="normal screen multiply overlay"
for kind in $PATTERN_KINDS_IMPL; do
    echo "==> Phase d-smoke -- --play-pattern-test $kind"
    PT_LOG="$LOG_DIR/pattern-test-$kind.log"
    PT_EXIT=0
    ssh "$TARGET" "$BIN_PI --output hdmi --play-pattern-test $kind --hold-secs 2" \
        > "$PT_LOG" 2>&1 || PT_EXIT=$?
    if [ "$PT_EXIT" -ne 0 ]; then
        echo "FAIL: --play-pattern-test $kind exit $PT_EXIT"
        cat "$PT_LOG"
        exit 1
    fi
    grep -qE 'panicked at|RUST_BACKTRACE' "$PT_LOG" && \
        { echo "FAIL: panic in --play-pattern-test $kind"; cat "$PT_LOG"; exit 1; }
    grep -q 'slide render complete' "$PT_LOG" || \
        { echo "FAIL: --play-pattern-test $kind didn't reach slide render complete"; cat "$PT_LOG"; exit 1; }
    # The dispatch must NOT have fallen back to the warn-clear
    # path for an implemented shader. The unimplemented warn line
    # has the form `warn: pattern=X shader not yet implemented`;
    # if it shows up for a pattern in PATTERN_KINDS_IMPL, the
    # dispatch arm regressed.
    if grep -q "warn: pattern=$kind shader not yet implemented" "$PT_LOG"; then
        echo "FAIL: --play-pattern-test $kind hit unimplemented-fallback warn (dispatch arm regressed?)"
        cat "$PT_LOG"
        exit 1
    fi
    KIND_UPPER=$(echo "$kind" | tr '[:lower:]' '[:upper:]')
    echo "    --play-pattern-test $kind ok (FS_PATTERN_${KIND_UPPER} linked + drew on hw)"
done

# v1-spec-delta #7 (slice b+) blend mode smoke. Renders a synth
# slide for each shipped blend mode and asserts no panic + slide
# render complete + the dispatch arm wasn't bypassed.
for kind in $BLEND_KINDS_IMPL; do
    echo "==> Phase d-smoke -- --play-blend-test $kind"
    BL_LOG="$LOG_DIR/blend-test-$kind.log"
    BL_EXIT=0
    ssh "$TARGET" "$BIN_PI --output hdmi --play-blend-test $kind --hold-secs 2" \
        > "$BL_LOG" 2>&1 || BL_EXIT=$?
    if [ "$BL_EXIT" -ne 0 ]; then
        echo "FAIL: --play-blend-test $kind exit $BL_EXIT"
        cat "$BL_LOG"
        exit 1
    fi
    grep -qE 'panicked at|RUST_BACKTRACE' "$BL_LOG" && \
        { echo "FAIL: panic in --play-blend-test $kind"; cat "$BL_LOG"; exit 1; }
    grep -q 'slide render complete' "$BL_LOG" || \
        { echo "FAIL: --play-blend-test $kind didn't reach slide render complete"; cat "$BL_LOG"; exit 1; }
    # Non-Normal modes must NOT have hit the warn-and-fall path
    # (which would mean the dispatch arm regressed). The "Normal"
    # mode is allowed to skip this check (it's the baseline).
    if [ "$kind" != "normal" ]; then
        if grep -q "warn: blend=$kind" "$BL_LOG"; then
            echo "FAIL: --play-blend-test $kind hit warn-and-fall (dispatch arm regressed?)"
            cat "$BL_LOG"
            exit 1
        fi
    fi
    echo "    --play-blend-test $kind ok (blend func dispatched correctly)"
done

# v1-spec-delta #8 (F-image-bg-smoke) image-bg-on-text smoke.
# Picks the first ImageSlide UUID from the content store and
# synthesizes a TextSlide whose background_image_slide_id
# points to it. Validates the BgKind::Image path through
# paint_slide -- decode + texture upload + FS_BLIT before glyph
# composite -- and exercises the F-image-bg-cache reuse path
# under render_slide_in_session. Visual verification (text
# layer composites correctly over the image bg) is qarl-
# eyeball; smoke gates pipeline correctness.
echo "==> Phase d-smoke -- --play-bg-image-test (TextSlide w/ image bg)"
BGI_UUID=$(ssh "$TARGET" '
for d in /var/openmarquee/content/*/; do
  if [ -f "$d/asset.png" ]; then
    if python3 -c "
import json, sys
d = json.load(open(\"$d/item.json\"))
sys.exit(0 if d.get(\"item\", {}).get(\"type\") == \"image\" else 1)
" 2>/dev/null; then
      basename "$d"
      exit 0
    fi
  fi
done
echo ""
' || true)
BGI_UUID=$(echo "$BGI_UUID" | tr -d '/' | head -1)
if [ -z "$BGI_UUID" ]; then
    echo "    --play-bg-image-test skipped (no ImageSlide on target)"
else
    BGI_LOG="$LOG_DIR/play-bg-image-test.log"
    BGI_EXIT=0
    ssh "$TARGET" "$BIN_PI --output hdmi --play-bg-image-test $BGI_UUID --content-root /var/openmarquee/content --hold-secs 2" \
        > "$BGI_LOG" 2>&1 || BGI_EXIT=$?
    if [ "$BGI_EXIT" -ne 0 ]; then
        echo "FAIL: --play-bg-image-test exit $BGI_EXIT (uuid=$BGI_UUID)"
        cat "$BGI_LOG"
        exit 1
    fi
    grep -qE 'panicked at|RUST_BACKTRACE' "$BGI_LOG" && \
        { echo "FAIL: panic in --play-bg-image-test"; cat "$BGI_LOG"; exit 1; }
    grep -q 'pattern=image asset=' "$BGI_LOG" || \
        { echo "FAIL: --play-bg-image-test didn't take the BgKind::Image path"; cat "$BGI_LOG"; exit 1; }
    grep -q 'slide render complete' "$BGI_LOG" || \
        { echo "FAIL: --play-bg-image-test didn't reach slide render complete"; cat "$BGI_LOG"; exit 1; }
    echo "    --play-bg-image-test ok (image-bg + text composite drew on hw)"
fi

# v1-spec-delta #8 (slice a) ImageSlide smoke. Picks the first
# asset.png from the live content store on the Pi and renders it
# via --play-image-slide. Asserts no panic + slide-render-complete
# + the PNG decode + texture-upload + FS_BLIT path linked. Visual
# verification (the PNG actually shows on screen) is qarl-eyeball.
# v1-spec-delta #11 (slice c) -- snapshot capture smoke. Picks
# the first text_slide UUID from the live FYS playlist and
# captures it to /tmp/openmarquee-capture-test.png. Asserts
# no panic + the PNG exists + has the canonical PNG signature
# (8-byte 89 50 4E 47 0D 0A 1A 0A header). Visual verification
# (the PNG looks like the slide) is qarl-eyeball.
# v1-spec-delta #9 (slice e pre-stamp gate) -- end-to-end IPC
# sidecar smoke. Pipes a JSON-line script (open + begin_slide
# + 3 advance ticks + capture + close) into --ipc-sidecar
# over stdin, captures stdout, asserts each expected response
# shape + verifies the captured PNG signature on disk.
#
# This exercises the full IPC stack on real hw:
#   - run_open_and_inner_loop_linux opening DRM via with_egl_session
#   - PlaybackState.begin_slide loading the text_slide
#   - 3 paint_and_present_one_frame_for_slide calls (real GL paint)
#   - capture_current_scene_to_png via slice 11 primitives
#   - clean Close + EglSession teardown
echo "==> Phase d-smoke -- --ipc-sidecar (open + 3 advance + capture + close)"
IPC_UUID=$(ssh "$TARGET" '
for d in /var/openmarquee/content/*/; do
  if python3 -c "
import json, sys
d = json.load(open(\"$d/item.json\"))
sys.exit(0 if d.get(\"item\", {}).get(\"type\") == \"text_slide\" else 1)
" 2>/dev/null; then
    basename "$d"
    exit 0
  fi
done
echo ""
' || true)
IPC_UUID=$(echo "$IPC_UUID" | tr -d '/' | head -1)
if [ -z "$IPC_UUID" ]; then
    echo "    --ipc-sidecar skipped (no text_slide on target)"
else
    IPC_LOG="$LOG_DIR/ipc-sidecar.log"
    # macOS mktemp doesn't accept a suffix after the X's; use
    # the default tempfile name and live with no `.json` suffix.
    IPC_SCRIPT=$(mktemp -t openmarquee-ipc-script)
    IPC_OUT="/tmp/openmarquee-ipc-capture.png"
    cat > "$IPC_SCRIPT" <<EOF
{"op":"open","params":{"output":"hdmi","content_root":"/var/openmarquee/content"}}
{"op":"begin_slide","params":{"slide_id":"$IPC_UUID","t0_ms":0,"duration_ms":5000}}
{"op":"advance","params":{"t_ms":100}}
{"op":"advance","params":{"t_ms":500}}
{"op":"advance","params":{"t_ms":1000}}
{"op":"capture","params":{"path":"$IPC_OUT"}}
{"op":"close"}
EOF
    IPC_EXIT=0
    ssh "$TARGET" "$BIN_PI --ipc-sidecar" < "$IPC_SCRIPT" > "$IPC_LOG" 2>&1 || IPC_EXIT=$?
    if [ "$IPC_EXIT" -ne 0 ]; then
        echo "FAIL: --ipc-sidecar exit $IPC_EXIT"
        cat "$IPC_LOG"
        exit 1
    fi
    # The renderer also emits eprintln stderr during DRM
    # bring-up + rasterization, which gets merged into the
    # log via 2>&1. Filter only the JSON response lines (start
    # with {"ok": or {"err":) for the count check.
    JSON_COUNT=$(grep -cE '^\{"(ok|err)":' "$IPC_LOG" || true)
    if [ "$JSON_COUNT" -ne 7 ]; then
        echo "FAIL: --ipc-sidecar emitted $JSON_COUNT JSON responses, expected 7"
        cat "$IPC_LOG"
        exit 1
    fi
    grep -q '"command":"open_ok"' "$IPC_LOG" || \
        { echo "FAIL: --ipc-sidecar missing open_ok"; cat "$IPC_LOG"; exit 1; }
    PAINT_COUNT=$(grep -c '"command":"paint_slide"' "$IPC_LOG" || true)
    if [ "$PAINT_COUNT" -ne 3 ]; then
        echo "FAIL: --ipc-sidecar paint_slide count $PAINT_COUNT, expected 3"
        cat "$IPC_LOG"
        exit 1
    fi
    grep -q '"command":"capture_ok"' "$IPC_LOG" || \
        { echo "FAIL: --ipc-sidecar missing capture_ok"; cat "$IPC_LOG"; exit 1; }
    EMPTY_COUNT=$(grep -c '"command":"empty"' "$IPC_LOG" || true)
    if [ "$EMPTY_COUNT" -lt 2 ]; then
        echo "FAIL: --ipc-sidecar empty count $EMPTY_COUNT (expected >= 2: begin_slide + close)"
        cat "$IPC_LOG"
        exit 1
    fi
    if grep -qE '^\{"err":' "$IPC_LOG"; then
        echo "FAIL: --ipc-sidecar emitted Err response"
        cat "$IPC_LOG"
        exit 1
    fi
    # Verify captured PNG sig on the Pi side.
    IPC_PNG_OK=$(ssh "$TARGET" "head -c 8 $IPC_OUT | od -An -t x1 | tr -d ' \n'" || true)
    if [ "$IPC_PNG_OK" != "89504e470d0a1a0a" ]; then
        echo "FAIL: --ipc-sidecar capture output $IPC_OUT not a PNG (sig=$IPC_PNG_OK)"
        exit 1
    fi
    rm -f "$IPC_SCRIPT"
    echo "    --ipc-sidecar ok (open + 3 paint + capture + close, PNG sig verified)"
fi

echo "==> Phase d-smoke -- --capture-slide (text_slide PNG snapshot)"
CAPTURE_UUID=$(ssh "$TARGET" '
for d in /var/openmarquee/content/*/; do
  if python3 -c "
import json, sys
d = json.load(open(\"$d/item.json\"))
sys.exit(0 if d.get(\"item\", {}).get(\"type\") == \"text_slide\" else 1)
" 2>/dev/null; then
    basename "$d"
    exit 0
  fi
done
echo ""
' || true)
CAPTURE_UUID=$(echo "$CAPTURE_UUID" | tr -d '/' | head -1)
if [ -z "$CAPTURE_UUID" ]; then
    echo "    --capture-slide skipped (no text_slide on target)"
else
    CAP_LOG="$LOG_DIR/capture-slide.log"
    CAP_OUT="/tmp/openmarquee-capture-test.png"
    CAP_EXIT=0
    ssh "$TARGET" "$BIN_PI --output hdmi --capture-slide $CAPTURE_UUID --content-root /var/openmarquee/content --capture-path $CAP_OUT" \
        > "$CAP_LOG" 2>&1 || CAP_EXIT=$?
    if [ "$CAP_EXIT" -ne 0 ]; then
        echo "FAIL: --capture-slide exit $CAP_EXIT (uuid=$CAPTURE_UUID)"
        cat "$CAP_LOG"
        exit 1
    fi
    grep -qE 'panicked at|RUST_BACKTRACE' "$CAP_LOG" && \
        { echo "FAIL: panic in --capture-slide"; cat "$CAP_LOG"; exit 1; }
    grep -q "captured slide" "$CAP_LOG" || \
        { echo "FAIL: --capture-slide didn't reach 'captured slide' log line"; cat "$CAP_LOG"; exit 1; }
    # PNG signature check on the Pi side. xxd isn't on the
    # canonical raspbian image; od is. Hex-strip via tr.
    PNG_OK=$(ssh "$TARGET" "head -c 8 $CAP_OUT | od -An -t x1 | tr -d ' \n'" || true)
    if [ "$PNG_OK" != "89504e470d0a1a0a" ]; then
        echo "FAIL: --capture-slide output $CAP_OUT not a PNG (sig=$PNG_OK)"
        exit 1
    fi
    echo "    --capture-slide ok (PNG signature verified on hw)"
fi

echo "==> Phase d-smoke -- --play-image-slide (first content/<uuid>/asset.png)"
IMAGE_ASSET=$(ssh "$TARGET" 'ls -1 /var/openmarquee/content/*/asset.png 2>/dev/null | head -1' || true)
if [ -z "$IMAGE_ASSET" ]; then
    echo "    --play-image-slide skipped (no asset.png on target)"
else
    IS_LOG="$LOG_DIR/play-image-slide.log"
    IS_EXIT=0
    ssh "$TARGET" "$BIN_PI --output hdmi --play-image-slide $IMAGE_ASSET --hold-secs 2" \
        > "$IS_LOG" 2>&1 || IS_EXIT=$?
    if [ "$IS_EXIT" -ne 0 ]; then
        echo "FAIL: --play-image-slide exit $IS_EXIT (asset=$IMAGE_ASSET)"
        cat "$IS_LOG"
        exit 1
    fi
    grep -qE 'panicked at|RUST_BACKTRACE' "$IS_LOG" && \
        { echo "FAIL: panic in --play-image-slide"; cat "$IS_LOG"; exit 1; }
    grep -q 'rendering image_slide from' "$IS_LOG" || \
        { echo "FAIL: --play-image-slide didn't reach renderer"; cat "$IS_LOG"; exit 1; }
    echo "    --play-image-slide ok (decoded + uploaded + drew on hw)"
fi

# v1-spec-delta #10 (slice d) -- settings reactivity end-to-end
# smoke. Captures via IPC sidecar at current settings, mutates
# settings.json (brightness 100 -> 20), captures again, asserts
# the two PNGs differ on disk (settings change took effect on
# the captured frame). Restores original settings before the
# next phase.
echo "==> Phase d-smoke -- settings reactivity (brightness change diff)"
SETTINGS_UUID=$(ssh "$TARGET" '
for d in /var/openmarquee/content/*/; do
  if python3 -c "
import json, sys
d = json.load(open(\"$d/item.json\"))
sys.exit(0 if d.get(\"item\", {}).get(\"type\") == \"text_slide\" else 1)
" 2>/dev/null; then
    basename "$d"
    exit 0
  fi
done
echo ""
' || true)
SETTINGS_UUID=$(echo "$SETTINGS_UUID" | tr -d '/' | head -1)
if [ -z "$SETTINGS_UUID" ]; then
    echo "    settings reactivity skipped (no text_slide on target)"
else
    SR_LOG="$LOG_DIR/settings-reactivity.log"
    SR_OUT_A="/tmp/openmarquee-settings-A.png"
    SR_OUT_B="/tmp/openmarquee-settings-B.png"
    SR_BACKUP="/tmp/openmarquee-settings-backup.json"
    # Backup current settings.
    ssh "$TARGET" "sudo cp /var/openmarquee/settings.json $SR_BACKUP" || true
    # Capture A: at whatever settings are live.
    SR_SCRIPT_A=$(mktemp -t openmarquee-settings-A)
    cat > "$SR_SCRIPT_A" <<EOF
{"op":"open","params":{"output":"hdmi","content_root":"/var/openmarquee/content"}}
{"op":"begin_slide","params":{"slide_id":"$SETTINGS_UUID","t0_ms":0,"duration_ms":5000}}
{"op":"advance","params":{"t_ms":100}}
{"op":"capture","params":{"path":"$SR_OUT_A"}}
{"op":"close"}
EOF
    ssh "$TARGET" "$BIN_PI --ipc-sidecar" < "$SR_SCRIPT_A" >> "$SR_LOG" 2>&1 || \
        { echo "FAIL: settings-reactivity capture A failed"; cat "$SR_LOG"; rm -f "$SR_SCRIPT_A"; exit 1; }
    rm -f "$SR_SCRIPT_A"
    # Mutate settings: brightness 100 -> 20 (drastic so the
    # tonemapping diff is visually obvious).
    ssh "$TARGET" '
sudo python3 -c "
import json
d = json.load(open(\"/var/openmarquee/settings.json\"))
d[\"brightness\"] = 20
json.dump(d, open(\"/var/openmarquee/settings.json\", \"w\"), indent=2)
"
sudo touch /var/openmarquee/settings.json
' || { echo "FAIL: settings mutate failed"; exit 1; }
    # Capture B: at brightness=20.
    SR_SCRIPT_B=$(mktemp -t openmarquee-settings-B)
    cat > "$SR_SCRIPT_B" <<EOF
{"op":"open","params":{"output":"hdmi","content_root":"/var/openmarquee/content"}}
{"op":"begin_slide","params":{"slide_id":"$SETTINGS_UUID","t0_ms":0,"duration_ms":5000}}
{"op":"advance","params":{"t_ms":100}}
{"op":"capture","params":{"path":"$SR_OUT_B"}}
{"op":"close"}
EOF
    ssh "$TARGET" "$BIN_PI --ipc-sidecar" < "$SR_SCRIPT_B" >> "$SR_LOG" 2>&1 || \
        { echo "FAIL: settings-reactivity capture B failed"; cat "$SR_LOG"; rm -f "$SR_SCRIPT_B"; exit 1; }
    rm -f "$SR_SCRIPT_B"
    # Diff the two captures. cmp returns 0 on identical, 1 on
    # different. We want different (settings change took
    # effect).
    DIFFERS=$(ssh "$TARGET" "cmp -s $SR_OUT_A $SR_OUT_B && echo same || echo differ" || true)
    # Restore settings BEFORE asserting so we don't leave a
    # mutated state if the assertion fails.
    ssh "$TARGET" "sudo cp $SR_BACKUP /var/openmarquee/settings.json && sudo systemctl restart openmarquee-backend" || true
    if [ "$DIFFERS" != "differ" ]; then
        echo "FAIL: settings reactivity didn't change captured PNG (cmp=$DIFFERS)"
        cat "$SR_LOG"
        exit 1
    fi
    echo "    settings reactivity ok (brightness change reflected in capture)"
fi

# v1-spec-delta #12 (slice c-2): live end-to-end soak gate. Runs
# the canonical FYS reel via --reel-loop for SMOKE_SOAK_DURATION_
# SECS (default 180s ≈ 5 passes at 34s/pass) and asserts the
# slope test + budget gates green. Defaults laxer than the
# production §8.2 6h gate: short runs are noisy, the smoke just
# proves the slope-test machinery works end-to-end. Operators
# bump SMOKE_SOAK_DURATION_SECS or call scripts/renderer_pi_soak.
# sh directly for the long-form §8.2 acceptance test.
echo "==> Phase d-smoke -- soak gate (short ~4min slope test)"
SMOKE_SOAK_DURATION_SECS="${SMOKE_SOAK_DURATION_SECS:-240}"
SOAK_SMOKE_LOG="$LOG_DIR/soak-smoke.log"
SOAK_DURATION_SECS="$SMOKE_SOAK_DURATION_SECS" \
SOAK_MAX_RSS_MB=120.0 \
SOAK_MAX_CMA_MB=220.0 \
SOAK_MAX_SLOPE_MBH=300.0 \
SOAK_HOLD_SECS=1 \
SOAK_WARMUP_PASSES=2 \
`# 300 MB/h slope ceiling tolerates system-wide CMA noise on short` \
`# runs (cma_used reads /proc/meminfo, includes peer processes).` \
`# Production §8.2 6h gate uses the soak script's tight 5 MB/h.` \
bash "$(dirname "$0")/renderer_pi_soak.sh" "$TARGET" > "$SOAK_SMOKE_LOG" 2>&1 || \
    { echo "FAIL: soak gate"; tail -30 "$SOAK_SMOKE_LOG"; exit 1; }
SOAK_SAMPLES=$(grep -oE 'samples=[0-9]+ passes=[0-9]+\.\.[0-9]+' "$SOAK_SMOKE_LOG" | head -1)
echo "    soak gate ok ($SOAK_SAMPLES)"

# Phase 6 reel assertion: completion + slide count + transition
# count + no panics. The reel logs "reel: resolved N items" once
# and "reel: transition into item I/N" for each transition.
# A 4-item playlist (the FYS seed) at single-pass should fire
# 3 transitions (no entry transition for the first item).
if [ "$REEL_EXIT" -ne 0 ]; then
    echo "FAIL: --play-reel exit $REEL_EXIT"
    cat "$REEL_LOG"
    exit 1
fi
grep -q 'reel: complete after' "$REEL_LOG" || \
    { echo "FAIL: --play-reel didn't print completion line"; cat "$REEL_LOG"; exit 1; }
grep -qE 'panicked at|RUST_BACKTRACE' "$REEL_LOG" && \
    { echo "FAIL: panic in --play-reel output"; exit 1; }
REEL_RESOLVED=$(grep -oE 'reel: resolved [0-9]+ playable' "$REEL_LOG" | grep -oE '[0-9]+' | head -1)
REEL_TRANSITIONS=$(grep -c 'reel: transition into item' "$REEL_LOG" || true)
REEL_HOLDS=$(grep -c 'reel: holding item' "$REEL_LOG" || true)
if [ -z "${REEL_RESOLVED:-}" ] || [ "$REEL_RESOLVED" -lt 2 ]; then
    echo "FAIL: --play-reel resolved too few playable items (got '${REEL_RESOLVED:-none}', want >=2)"
    cat "$REEL_LOG"
    exit 1
fi
# N items → N holds, N-1 transitions on a single pass.
EXPECTED_TRANSITIONS=$((REEL_RESOLVED - 1))
if [ "$REEL_TRANSITIONS" -ne "$EXPECTED_TRANSITIONS" ]; then
    echo "FAIL: --play-reel transitions count mismatch (got $REEL_TRANSITIONS, expected $EXPECTED_TRANSITIONS for $REEL_RESOLVED items)"
    cat "$REEL_LOG"
    exit 1
fi
if [ "$REEL_HOLDS" -ne "$REEL_RESOLVED" ]; then
    echo "FAIL: --play-reel holds count mismatch (got $REEL_HOLDS, expected $REEL_RESOLVED)"
    cat "$REEL_LOG"
    exit 1
fi
# v1-spec-delta #5 (slice e): cumulative wall-clock floor on the
# pass. With --hold-secs 1, each hold is 1000ms; transitions are
# variable-length (capped at clamp_transition_ms's bounds, 200-
# 2000ms). Floor: per-item budget = 1000ms hold + 2000ms transition
# upper bound + 500ms slack for slide setup; total = N items × 3500
# - 2000 (no transition into the first item). On a healthy slice
# (c)+(d) build with the FYS playlist (~19 items), ~64s. Pre-slice
# (c) pass time was much higher (~500ms EGL bring-up × per-slide
# accumulated). Pre-slice (d) had set_crtc per frame; perf delta
# was visual (BLACK gaps) more than wall-clock. The floor catches
# regressions that silently undo the architectural wins.
REEL_PASS_MS=$(grep -oE 'reel: pass #0 complete pass_ms=[0-9]+' "$REEL_LOG" | grep -oE '[0-9]+$' | head -1)
if [ -z "${REEL_PASS_MS:-}" ]; then
    echo "FAIL: --play-reel didn't emit per-pass wall-clock (pass_ms=...)"
    cat "$REEL_LOG"
    exit 1
fi
# Lower bound: each item costs 1000ms hold + ~50ms commit min;
# transitions add visible per-frame work. A reel that finishes
# in << expected has skipped most of its rendering work via
# error paths -- catches bugs where transitions or slides fail
# silently in the warn-and-continue path. v1-spec-delta #5
# slice e caught the slice d EBUSY regression this way.
REEL_FLOOR_MIN_MS=$(( REEL_RESOLVED * 1000 ))
if [ "$REEL_PASS_MS" -lt "$REEL_FLOOR_MIN_MS" ]; then
    echo "FAIL: --play-reel pass suspiciously fast (got ${REEL_PASS_MS}ms, expected >= ${REEL_FLOOR_MIN_MS}ms for $REEL_RESOLVED items)"
    echo "  -> probably warn-and-continue path swallowing real render errors; check 'reel: warn' lines"
    cat "$REEL_LOG"
    exit 1
fi
REEL_FLOOR_MS=$(( REEL_RESOLVED * 3500 - 2000 ))
if [ "$REEL_PASS_MS" -gt "$REEL_FLOOR_MS" ]; then
    echo "FAIL: --play-reel pass too slow (got ${REEL_PASS_MS}ms, floor ${REEL_FLOOR_MS}ms for $REEL_RESOLVED items)"
    echo "  -> probable regression on slice (c) single-EGL-session or slice (d) page_flip"
    cat "$REEL_LOG"
    exit 1
fi
# Any 'reel: warn' line means a per-slide/transition path failed.
# The reel driver intentionally warn-and-continues to avoid
# wedging on a single bad slide, but the smoke must NOT go green
# in that state. (slice d EBUSY across-call regression had this
# exact silent-failure shape.)
REEL_WARNS=$(grep -c 'reel: warn' "$REEL_LOG" || true)
if [ "$REEL_WARNS" -ne 0 ]; then
    echo "FAIL: --play-reel emitted $REEL_WARNS 'reel: warn' line(s)"
    grep 'reel: warn' "$REEL_LOG" | head -10
    exit 1
fi
echo "    --play-reel ok ($REEL_RESOLVED items, $REEL_TRANSITIONS transitions, $REEL_HOLDS holds — pass_ms=$REEL_PASS_MS, floor=$REEL_FLOOR_MS)"

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
