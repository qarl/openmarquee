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
grep -qi 'panic\|panicked' "$SLIDE_LOG" && \
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
    grep -qi 'panic\|panicked' "$GRAD_LOG" && \
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
    grep -qi 'panic\|panicked' "$TEXT_LOG" && \
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
    grep -qi 'panic\|panicked' "$FBO_LOG" && \
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
    grep -qi 'panic\|panicked' "$FADE_LOG" && \
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
    grep -qi 'panic\|panicked' "$log" && \
        { echo "FAIL: panic in animated $kind output"; exit 1; }
    local frames
    frames=$(grep -oE 'rendered [0-9]+ frames' "$log" | grep -oE '[0-9]+' | head -1)
    if [ -z "${frames:-}" ] || [ "$frames" -lt "$floor" ]; then
        echo "FAIL: --animate-fade --transition $kind frame count too low (got '${frames:-none}', want >=$floor)"
        cat "$log"
        exit 1
    fi
    # Wall-clock upper bound: transition_ms + 1500ms slack for ssh
    # roundtrip + EGL bring-up + cleanup. Catches GPU saturation
    # (loop emits the expected frame count but each frame missed
    # its 33ms budget, so wall-clock blows past transition_ms).
    # Generous slack so ssh latency wobble doesn't false-fail.
    local cap_ms=$((transition_ms + 1500))
    if [ "$wall_ms" -gt "$cap_ms" ]; then
        echo "FAIL: --animate-fade --transition $kind wall-clock too high (got ${wall_ms}ms, cap ${cap_ms}ms)"
        echo "      possible GPU saturation — frames missed their per-frame budget"
        cat "$log"
        exit 1
    fi
    echo "    --animate-fade --transition $kind ok ($frames frames in ${wall_ms}ms wall-clock)"
}
if [ -n "${FADE_FROM:-}" ] && [ -n "${FADE_TO:-}" ]; then
    assert_anim_transition "cut"  "$ANCUT_LOG"  "$ANCUT_EXIT"  12 500 "$ANCUT_WALL_MS"
    assert_anim_transition "fade" "$ANFADE_LOG" "$ANFADE_EXIT" 20 800 "$ANFADE_WALL_MS"
    assert_anim_transition "wipe" "$ANWIPE_LOG" "$ANWIPE_EXIT" 20 800 "$ANWIPE_WALL_MS"
else
    echo "    --animate-fade skipped (couldn't find 2 text slides in seed)"
fi

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
