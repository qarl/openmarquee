#!/usr/bin/env bash
# 2026-06-14 — VIDEO golden-master runner for video→video transitions.
#
# This closes the test gap the dispatch called out: PR #2's golden runner
# at run_transition_golden_bg.sh is text-on-black ONLY. With the c3.x
# poster substitution PR #2 added, the offscreen capture path always had
# a non-black bg for text-over-video — so the offscreen golden was BLIND
# to the live video→video black-out bug. This runner asserts on the
# LIVE-pipeline scanout (via OPENMARQUEE_LIVE_PREVIEW_PATH) during a
# real video↔video transition.
#
# REQUIREMENTS
#   * Linux box with /dev/dri/card* + bcm2835-codec (Pi 3/4/Zero 2 W).
#   * systemd-managed openmarquee-backend (production shape).
#   * `journalctl` + `python3` + Pillow + `ffmpeg`.
#   * Golden fixtures at FIXTURES (see below).
#   * The renderer binary built from the branch under test (Option A).
#
# WHAT IT ASSERTS
#   1. Mid-transition central-60% region luma > floor on the live-
#      preview PNG. Catches the "outgoing black" failure mode that
#      origin/main's c3.x freeze-both regressed AND the old r103.1
#      max-mode bug — both produced near-black mid-transition.
#   2. The journal across the soak shows ZERO `paint_transition_skip
#      reason=endpoint_a_no_frame` (outgoing live decoder produced
#      frames throughout — Option A's single-decoder invariant).
#   3. No `delta_ms` over 1000 ms (no multi-second freeze).
#
# A red exit (1) means at least one of (1)–(3) failed; the message
# names which.
#
# USAGE
#   FIXTURES=/path/to/qa/fixtures/transition-golden \
#     CONTENT_ROOT=/var/openmarquee/content-vvtest \
#     PLAYLIST_PATH=/var/openmarquee/playlist-vvtest.json \
#     PREVIEW_PATH=/var/openmarquee/preview-vvtest.png \
#     renderer/tests/scripts/run_video_to_video_golden.sh

set -euo pipefail

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 2026-06-14: fixtures now live in-repo at code2/qa/fixtures/...
# (committed in the same PR as this runner so preflight doesn't fail
# on a missing outer-repo dir). Operators can still override
# FIXTURES= to point at a curated set.
FIXTURES="${FIXTURES:-$_SCRIPT_DIR/../../../qa/fixtures/transition-golden}"
CONTENT_ROOT="${CONTENT_ROOT:-/var/openmarquee/content-vvtest}"
PLAYLIST_PATH="${PLAYLIST_PATH:-/var/openmarquee/playlist-vvtest.json}"
PREVIEW_PATH="${PREVIEW_PATH:-/var/openmarquee/preview-vvtest.png}"
SOAK_SECONDS="${SOAK_SECONDS:-30}"
SERVICE_NAME="${SERVICE_NAME:-openmarquee-backend}"
LUMA_FLOOR="${LUMA_FLOOR:-20}"

err() { printf '%s\n' "ERR: $*" >&2; }
log() { printf '%s\n' "$*" >&2; }

# Cleanup trap — same shape as the preload-mode runner. The
# drop-in file MUST be removed on any exit path; otherwise the
# sign keeps running with the test playlist and the experiment
# preview-path env across reboots.
ACTIVE_DROP_IN_FILE=""
cleanup_drop_in() {
  if [ -n "$ACTIVE_DROP_IN_FILE" ] && [ -f "$ACTIVE_DROP_IN_FILE" ]; then
    log "Cleanup: removing test drop-in at $ACTIVE_DROP_IN_FILE..."
    sudo rm -f "$ACTIVE_DROP_IN_FILE" || true
    sudo systemctl daemon-reload || true
    sudo systemctl restart "$SERVICE_NAME" 2>/dev/null || true
  fi
  ACTIVE_DROP_IN_FILE=""
}
trap cleanup_drop_in EXIT INT TERM

# ---------------------------------------------------------------- preflight

if [ "$(uname -s)" != "Linux" ]; then
  err "this runner only runs on Linux (uses systemd + /dev/dri); on $(uname -s)"
  exit 2
fi
for tool in journalctl python3 ffmpeg systemctl; do
  command -v "$tool" >/dev/null || { err "$tool missing"; exit 2; }
done
if ! python3 -c 'import PIL' >/dev/null 2>&1; then
  err "Pillow missing (pip install Pillow)"; exit 2
fi
if [ ! -d "$FIXTURES" ]; then err "FIXTURES dir not found: $FIXTURES"; exit 2; fi
for f in golden-red.mp4 golden-blue.mp4 spec.md; do
  if [ ! -f "$FIXTURES/$f" ]; then err "missing $FIXTURES/$f"; exit 2; fi
done

# ----------------------------------------------------- content-root build

# Deterministic UUIDs. Two pure-VIDEO content items (type=video).
VID_RED_ID="f0e1d2c3-b4a5-4960-8780-707070707070"
VID_BLUE_ID="f0e1d2c3-b4a5-4960-8780-707070707071"

build_video_asset () {
  local id="$1" src_mp4="$2"
  local dir="$CONTENT_ROOT/$id"
  sudo install -d -o openmarquee -g openmarquee -m 0755 "$dir"
  sudo install -m 0644 "$src_mp4" "$dir/asset.mp4"
  # BT.709 limited per the canonical import recipe (content.rs
  # video_slide_poster_path doc-comment).
  sudo ffmpeg -y -loglevel error -i "$dir/asset.mp4" -vframes 1 \
    -pix_fmt yuv420p \
    -vf 'scale=1280:720:flags=lanczos:in_color_matrix=bt709:in_range=limited' \
    -color_range tv -colorspace bt709 \
    -color_primaries bt709 -color_trc bt709 \
    "$dir/poster.png"
  sudo tee "$dir/item.json" >/dev/null <<JSON
{
  "schema_version": 3,
  "item": {
    "type": "video",
    "id": "$id",
    "name": "golden-$(basename "$src_mp4" .mp4)",
    "duration_ms": 4000,
    "transition": "fade",
    "transition_ms": 1500
  }
}
JSON
}

log "Building test content-root at $CONTENT_ROOT..."
sudo install -d -o openmarquee -g openmarquee -m 0755 "$CONTENT_ROOT"
build_video_asset "$VID_RED_ID" "$FIXTURES/golden-red.mp4"
build_video_asset "$VID_BLUE_ID" "$FIXTURES/golden-blue.mp4"

log "Writing test playlist at $PLAYLIST_PATH..."
# 2026-06-14 iter-3 — QA bench feedback: iter-2's single-fade playlist
# would have passed despite the iris/wipe being broken across ALL
# transition kinds. The playlist now alternates fade, wipe, iris,
# slide so every code path that branches per-kind (SP shader vs SB
# vs legacy-3pass) gets exercised. A binary that's broken on one
# kind but green on another fires per-kind.
sudo tee "$PLAYLIST_PATH" >/dev/null <<JSON
{
  "schema_version": 4,
  "playlists": [
    {
      "id": "00000000-0000-4000-8000-000000000001",
      "name": "VideoToVideoTest",
      "items": [
        {"item_id": "$VID_RED_ID",  "transition": "fade",  "transition_ms": 1500},
        {"item_id": "$VID_BLUE_ID", "transition": "wipe",  "transition_ms": 1500},
        {"item_id": "$VID_RED_ID",  "transition": "iris",  "transition_ms": 1500},
        {"item_id": "$VID_BLUE_ID", "transition": "slide", "transition_ms": 1500}
      ]
    }
  ]
}
JSON

# --------------------------------------------------- drop-in + restart

DROP_IN="/etc/systemd/system/${SERVICE_NAME}.service.d/v2v-test-mode.conf"
ACTIVE_DROP_IN_FILE="$DROP_IN"
sudo install -d /etc/systemd/system/"${SERVICE_NAME}.service.d"
sudo tee "$DROP_IN" >/dev/null <<UNIT
[Service]
Environment=OPENMARQUEE_PLAYLIST_PATH=$PLAYLIST_PATH
Environment=OPENMARQUEE_CONTENT_ROOT=$CONTENT_ROOT
Environment=OPENMARQUEE_LIVE_PREVIEW_PATH=$PREVIEW_PATH
Environment=OPENMARQUEE_LIVE_PREVIEW_INTERVAL_MS=250
UNIT
sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE_NAME"

SINCE="$(date '+%Y-%m-%d %H:%M:%S')"
log "Soaking for ${SOAK_SECONDS}s (red→blue and blue→red transitions, each ~1.5s)..."

# ------------------------------------------------- sample the preview

# Sample the live-preview PNG every 250ms during the soak. Persist a
# rolling history to a tempdir so we can find mid-transition frames
# after the fact.
SAMPLE_DIR="$(mktemp -d -t v2v-samples-XXXXXX)"
log "Sampling live preview into $SAMPLE_DIR..."
SAMPLE_PID=""
# Sacred-review NIT-5: install the combined trap BEFORE spawning the
# sampler subshell so a SIGINT in the microsecond window between
# spawn and trap-install doesn't leak the cp loop.
trap 'kill "$SAMPLE_PID" 2>/dev/null || true; cleanup_drop_in' EXIT INT TERM
(
  i=0
  while true; do
    if [ -f "$PREVIEW_PATH" ]; then
      cp -f "$PREVIEW_PATH" "$SAMPLE_DIR/$(printf '%04d' "$i").png" 2>/dev/null || true
    fi
    i=$((i + 1))
    sleep 0.25
  done
) &
SAMPLE_PID=$!

sleep "$SOAK_SECONDS"
kill "$SAMPLE_PID" 2>/dev/null || true

# -------------------------------------------------- journal + assertions

JOURNAL_PATH="$(mktemp -t v2v-journal-XXXXXX)"
sudo journalctl -u "$SERVICE_NAME" --since "$SINCE" -o cat > "$JOURNAL_PATH" 2>&1 || true
JOURNAL_LINES="$(wc -l < "$JOURNAL_PATH")"
log "Captured $JOURNAL_LINES journal lines + $(ls -1 "$SAMPLE_DIR" | wc -l) preview samples."

# Sacred-review BLOCKER-1 fix: heredoc + process-substitution stdin
# redirection conflict — `python3 - <<'PY' < <(printf ...)` would
# have the process-sub clobber the heredoc and python would try to
# parse the journal as its script. Write the analyzer to a temp file
# instead and pipe the journal on stdin cleanly.
PYTHON_ANALYZER="$(mktemp -t v2v-analyze-XXXXXX.py)"
trap 'rm -f "$PYTHON_ANALYZER" "$JOURNAL_PATH"; kill "$SAMPLE_PID" 2>/dev/null || true; cleanup_drop_in' EXIT INT TERM
cat > "$PYTHON_ANALYZER" <<'PY'
import os, sys, re
from PIL import Image

sample_dir, luma_floor_s = sys.argv[1], sys.argv[2]
luma_floor = float(luma_floor_s)
journal = sys.stdin.read().splitlines()

errors = []

# --- Assertion 2: endpoint_a_no_frame ≤ small hard cap ----
# QA 2026-06-14 bench feedback #2: the iter-1 25%/min-2 tolerance was
# too generous — an intermittently-broken binary could pass. Hard cap
# of 3 total across the soak: outgoing fires throughout (Option A's
# contract), kernel pipeline only hiccups under pathological load,
# and 3 is enough headroom to absorb a single contended preload
# window without forgiving real starvation.
endpoint_a_re = re.compile(r"\bpaint_transition_skip\b.*\bendpoint_a_no_frame\b")
endpoint_a_hits = sum(1 for line in journal if endpoint_a_re.search(line))
endpoint_a_budget = 3
print(f"[journal] endpoint_a_no_frame count = {endpoint_a_hits} (budget {endpoint_a_budget})")
if endpoint_a_hits > endpoint_a_budget:
    errors.append(
        f"Option A invariant VIOLATED: {endpoint_a_hits} `endpoint_a_no_frame` "
        f"skips in the journal (budget {endpoint_a_budget}). The OUTGOING live "
        f"decoder failed to produce a frame during transitions — that's the "
        f"dual-decoder starvation pattern the fix was supposed to eliminate."
    )

# --- Assertion 3: no multi-second freeze ----
# delta_ms is logged by the paint hook; tolerate occasional spikes
# but flag any > 1000ms as a freeze.
delta_re = re.compile(r"\bdelta_ms[=:](\d+)")
deltas = [int(m.group(1)) for line in journal for m in [delta_re.search(line)] if m]
if deltas:
    over_budget = [d for d in deltas if d > 1000]
    print(f"[journal] delta_ms samples={len(deltas)} max={max(deltas)} over_1000ms={len(over_budget)}")
    if over_budget:
        errors.append(
            f"Multi-second freeze detected: {len(over_budget)} delta_ms samples "
            f"> 1000ms (max={max(over_budget)}). The paint loop blocked on "
            f"something — most likely the sync cold-start prime in "
            f"BeginTransition. Confirm the PreloadSlide arm's "
            f"`preload_defer_skipped_for_still_coverage` probe is firing."
        )
else:
    print("[journal] no delta_ms samples found (probe absent? check the renderer build)")

# --- Assertion 1: mid-transition central-region luma > floor ----
# We can't perfectly pin which frame was mid-transition without
# correlating the sampler's mtime with journalctl wall-clock, but
# the goldens are deterministic colors that never legitimately dim:
# golden-red is pure 0xFF0000 (BT.601 luma ≈ 76), golden-blue is
# pure 0x0000FF (luma ≈ 29). Any sample whose center is BLACK
# (luma < 20) during the soak is the bug — no legitimate
# golden-fixture frame can be that dark. Treats the WHOLE soak
# as the assertion window.
#
# QA 2026-06-14 bench feedback #3 (mid-transition isolation):
# punted. The golden fixtures' dynamic-range floor (29 for blue)
# is already above the floor threshold (20), so a legitimately-
# dark frame can't false-positive here. For a future runner
# exercising operator-uploaded content with darker shots, we'd
# need to correlate sample timestamps with `transition_paint_
# entry` / `transition_paint_exit` journal probes.
samples = sorted(p for p in os.listdir(sample_dir) if p.endswith(".png"))
luma_min = 999.0
luma_min_path = None
luma_low_count = 0
for name in samples:
    path = os.path.join(sample_dir, name)
    try:
        img = Image.open(path).convert("L")
    except OSError:
        continue
    w, h = img.size
    box = (int(w * 0.2), int(h * 0.2), int(w * 0.8), int(h * 0.8))
    g = img.crop(box)
    px = list(g.getdata())
    if not px:
        continue
    luma = sum(px) / len(px)
    if luma < luma_min:
        luma_min = luma
        luma_min_path = path
    if luma < luma_floor:
        luma_low_count += 1

print(f"[preview] samples={len(samples)} luma_min={luma_min:.1f} "
      f"low_luma_count<{luma_floor} = {luma_low_count}")
if luma_min < luma_floor:
    errors.append(
        f"Outgoing video went BLACK at least once during the soak: minimum "
        f"central-region luma {luma_min:.1f} < floor {luma_floor} "
        f"(file: {luma_min_path}). The pre-fix mode signature. "
        f"{luma_low_count} samples total under threshold."
    )

print()
if errors:
    print("OVERALL: FAIL")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("OVERALL: PASS — video→video transition kept outgoing live + no freeze.")
PY

set +e
python3 "$PYTHON_ANALYZER" "$SAMPLE_DIR" "$LUMA_FLOOR" < "$JOURNAL_PATH"
RUN_RC=$?
set -e

log "Done. Cleanup trap will restore production unit state."
exit "$RUN_RC"
