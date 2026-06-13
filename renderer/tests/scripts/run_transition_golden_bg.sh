#!/usr/bin/env bash
# 2026-06-13 — Drive --capture-sb-mid against the QA-deterministic golden
# fixtures + assert the background region is a non-black red/blue blend in
# the fade transition (the bug that shipped to fireplacesign), and that the
# golden-quad transitions preserve quadrant orientation through wipe + iris.
#
# Pre-fix the bg region was solid black (0,0,0) because the offscreen
# capture path lacked any background_video_slide_id handling. Post-fix it
# substitutes the referenced video's poster.png as the bake bg.
#
# REQUIREMENTS:
#   * Linux box (any — /dev/dri/card0 + EGL/GLES2 capable). FYS Pi works.
#   * Cross-built `openmarquee-render` binary on PATH (see scripts/renderer
#     _cross_build.sh) — or pass via OPENMARQUEE_RENDER_BIN=<path>.
#   * ffmpeg + ffprobe on PATH (for poster.png extraction in the BT.709
#     limited recipe per content/asset/import contract).
#   * python3 + Pillow (PIL) for PNG luma + quadrant-color assertions.
#
# USAGE:
#   FIXTURES=/path/to/qa/fixtures/transition-golden \
#     OPENMARQUEE_RENDER_BIN=/usr/local/bin/openmarquee-render \
#     renderer/tests/scripts/run_transition_golden_bg.sh
#
# Defaults FIXTURES to <repo>/../qa/fixtures/transition-golden if unset.
#
# OUTPUT: PNGs under $WORK_DIR/captures/ + a one-line PASS/FAIL summary.
# Exit code: 0 = all assertions pass, 1 = any assertion fails.

set -euo pipefail

FIXTURES="${FIXTURES:-/Users/qarl/project/openmarquee/qa/fixtures/transition-golden}"
RENDER_BIN="${OPENMARQUEE_RENDER_BIN:-openmarquee-render}"
WORK_DIR="${WORK_DIR:-$(mktemp -d -t omq-trans-golden-XXXXXX)}"

err() { printf '%s\n' "ERR: $*" >&2; }
log() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------- preflight

if ! command -v ffmpeg >/dev/null; then err "ffmpeg missing"; exit 2; fi
if ! command -v ffprobe >/dev/null; then err "ffprobe missing"; exit 2; fi
if ! command -v python3 >/dev/null; then err "python3 missing"; exit 2; fi
if ! python3 -c 'import PIL' >/dev/null 2>&1; then
  err "Pillow missing (pip install Pillow)"; exit 2
fi
if [ ! -d "$FIXTURES" ]; then err "FIXTURES dir not found: $FIXTURES"; exit 2; fi
for f in golden-red.mp4 golden-blue.mp4 golden-quad.mp4 spec.md; do
  if [ ! -f "$FIXTURES/$f" ]; then err "missing $FIXTURES/$f"; exit 2; fi
done
if ! command -v "$RENDER_BIN" >/dev/null && [ ! -x "$RENDER_BIN" ]; then
  err "openmarquee-render not found at: $RENDER_BIN"
  err "  Cross-build per scripts/renderer_cross_build.sh, then set"
  err "  OPENMARQUEE_RENDER_BIN=/path/to/openmarquee-render-aarch64"
  exit 2
fi

log "WORK_DIR: $WORK_DIR"
mkdir -p "$WORK_DIR/content" "$WORK_DIR/captures"

# ------------------------------------------------------- content-root build

# Deterministic UUIDs so re-runs are byte-identical.
RED_ID="f0e1d2c3-b4a5-4960-8780-707070707070"
BLUE_ID="f0e1d2c3-b4a5-4960-8780-707070707071"
QUAD_ID="f0e1d2c3-b4a5-4960-8780-707070707072"

build_video_item () {
  local id="$1" src_mp4="$2" name="$3"
  local dir="$WORK_DIR/content/$id"
  mkdir -p "$dir"
  cp "$src_mp4" "$dir/asset.mp4"
  # BT.709 limited per renderer/src/content.rs:video_slide_poster_path
  # docstring (canonical import recipe).
  ffmpeg -y -loglevel error -i "$dir/asset.mp4" -vframes 1 \
    -pix_fmt yuv420p \
    -vf 'scale=1280:720:flags=lanczos:in_color_matrix=bt709:in_range=limited' \
    -color_range tv -colorspace bt709 \
    -color_primaries bt709 -color_trc bt709 \
    "$dir/poster.png"
  # Minimal item.json envelope (schema_version 3 + type=video).
  python3 - "$id" "$name" "$dir/item.json" <<'PY'
import json, sys
item_id, name, out = sys.argv[1], sys.argv[2], sys.argv[3]
envelope = {
    "schema_version": 3,
    "item": {
        "type": "video",
        "id": item_id,
        "name": name,
        "duration_ms": 5000,
        "transition": "cut",
        "transition_ms": 500,
    },
}
with open(out, "w") as f:
    json.dump(envelope, f, indent=2)
PY
}

log "Building content items..."
build_video_item "$RED_ID" "$FIXTURES/golden-red.mp4" "golden-red"
build_video_item "$BLUE_ID" "$FIXTURES/golden-blue.mp4" "golden-blue"
build_video_item "$QUAD_ID" "$FIXTURES/golden-quad.mp4" "golden-quad"

# ------------------------------------------------------ run capture-sb-mid

run_capture () {
  local kind="$1" from="$2" to="$3" t="$4" png="$5"
  "$RENDER_BIN" --output hdmi \
    --capture-sb-mid \
    --fade-from "$from" --fade-to "$to" \
    --transition "$kind" \
    --capture-sb-t "$t" \
    --capture-path "$png" \
    --content-root "$WORK_DIR/content"
}

log "Capturing fade red->blue at t=0.25/0.5/0.75..."
run_capture fade  "$RED_ID"  "$BLUE_ID" 0.25 "$WORK_DIR/captures/fade-rb-t025.png"
run_capture fade  "$RED_ID"  "$BLUE_ID" 0.5  "$WORK_DIR/captures/fade-rb-t050.png"
run_capture fade  "$RED_ID"  "$BLUE_ID" 0.75 "$WORK_DIR/captures/fade-rb-t075.png"

log "Capturing quad through wipe + iris at t=0.5..."
run_capture wipe  "$QUAD_ID" "$RED_ID" 0.5  "$WORK_DIR/captures/wipe-quad-t050.png"
run_capture iris  "$QUAD_ID" "$RED_ID" 0.5  "$WORK_DIR/captures/iris-quad-t050.png"

# ------------------------------------------------------------- assertions

python3 - "$WORK_DIR/captures" <<'PY'
import sys, os
from PIL import Image
captures = sys.argv[1]
errors = []

def mean_luma(img, box=None):
    g = img.convert("L")
    if box is not None:
        g = g.crop(box)
    px = list(g.getdata())
    return sum(px) / len(px) if px else 0.0

def mean_rgb(img, box):
    rgb = img.convert("RGB").crop(box)
    r = g = b = n = 0
    for (rr, gg, bb) in rgb.getdata():
        r += rr; g += gg; b += bb; n += 1
    return (r/n, g/n, b/n)

# ---- Assertion 1: red->blue fade bg is NEVER black at t=0.25/0.5/0.75 ----
# Pre-fix this prints luma=0 (solid #000000); post-fix the bg blends through
# the red→blue mp4 posters via the FS_FADE shader, mean luma ~70-100.
for t_str, name in [("025","fade-rb-t025"),("050","fade-rb-t050"),
                    ("075","fade-rb-t075")]:
    p = os.path.join(captures, f"{name}.png")
    img = Image.open(p)
    # Avoid the edges in case of letterboxing — measure central 60%.
    w, h = img.size
    box = (int(w*0.2), int(h*0.2), int(w*0.8), int(h*0.8))
    luma = mean_luma(img, box)
    floor = 20.0  # well above 0 but below any plausible blend luma.
    status = "PASS" if luma > floor else "FAIL"
    print(f"[fade t={t_str}] mean_luma={luma:.1f} floor={floor} {status}")
    if luma <= floor:
        errors.append(f"fade-rb t={t_str}: bg luma {luma:.1f} <= floor {floor} "
                      f"(the black-bg bug — see {p})")
    # Channel-balance sanity at t=0.5 (mid-fade should be ~half-red+half-blue):
    if t_str == "050":
        r, g, b = mean_rgb(img, box)
        # Allow wide tolerance — the SP-shader fade isn't a pure RGB lerp
        # because each side runs through its own scissored bake region
        # before the composite. We just check both channels are non-trivial.
        r_ok = r > 30
        b_ok = b > 30
        status = "PASS" if (r_ok and b_ok) else "FAIL"
        print(f"[fade t=050] mean_rgb=({r:.0f},{g:.0f},{b:.0f}) "
              f"r>30={r_ok} b>30={b_ok} {status}")
        if not (r_ok and b_ok):
            errors.append(f"fade-rb t=0.5: red channel {r:.0f}, blue {b:.0f} "
                          f"— expected both > 30 for a non-black blend")

# ---- Assertion 2: golden-quad orientation through wipe + iris ----
# TL=red, TR=green, BL=blue, BR=yellow. Sample a small patch from each
# corner and assert dominant channel matches. Tolerant to anti-aliasing
# at the partition seams and to whatever transition reveals at t=0.5.
def dominant_quadrant(img):
    w, h = img.size
    # Sample inside each quadrant, away from the edges (10% in from each
    # outer edge, and 30% in from the cross-axis center to avoid the seam).
    pad_x = int(w * 0.1); cen_x_l = int(w * 0.2); cen_x_r = int(w * 0.8)
    pad_y = int(h * 0.1); cen_y_t = int(h * 0.2); cen_y_b = int(h * 0.8)
    quads = {
        "TL": (pad_x, pad_y, cen_x_l, cen_y_t),
        "TR": (cen_x_r, pad_y, w - pad_x, cen_y_t),
        "BL": (pad_x, cen_y_b, cen_x_l, h - pad_y),
        "BR": (cen_x_r, cen_y_b, w - pad_x, h - pad_y),
    }
    out = {}
    for k, box in quads.items():
        out[k] = mean_rgb(img, box)
    return out

def classify(rgb):
    r, g, b = rgb
    # Heuristic: dominant channel(s) above midline, others well below.
    if r > 100 and g < 80 and b < 80: return "red"
    if r < 80 and g > 100 and b < 80: return "green"
    if r < 80 and g < 80 and b > 100: return "blue"
    if r > 100 and g > 100 and b < 80: return "yellow"
    return f"other(r={r:.0f},g={g:.0f},b={b:.0f})"

EXPECT = {"TL": "red", "TR": "green", "BL": "blue", "BR": "yellow"}
for kind, fname in [("wipe","wipe-quad-t050"), ("iris","iris-quad-t050")]:
    p = os.path.join(captures, f"{fname}.png")
    img = Image.open(p)
    classified = {q: classify(rgb) for q, rgb in dominant_quadrant(img).items()}
    matches_ok = 0
    for q, want in EXPECT.items():
        if classified[q] == want:
            matches_ok += 1
    # A wipe/iris at t=0.5 may have the OTHER side (golden-red) covering some
    # quadrants. For the orientation test we want to confirm: at LEAST 2 of
    # the 4 quadrant labels are still in their corners (no flip/rotation).
    # On a flipped image we'd see e.g. TL=blue / BL=red / TR=yellow / BR=green.
    floor = 2
    status = "PASS" if matches_ok >= floor else "FAIL"
    print(f"[{kind} t=050] quadrants_in_corner={matches_ok}/4 "
          f"({classified}) {status}")
    if matches_ok < floor:
        errors.append(f"{kind} t=0.5: only {matches_ok}/4 quadrants in their "
                      f"corner — orientation flipped/rotated. ({p})")

print()
if errors:
    print("OVERALL: FAIL")
    for e in errors: print(f"  - {e}")
    sys.exit(1)
else:
    print("OVERALL: PASS — non-black bg + quadrant orientation hold")
PY
log "Captures + assertions complete. See $WORK_DIR/captures/."
