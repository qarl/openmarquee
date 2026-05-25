#!/usr/bin/env bash
# Captive-portal bundle-size CI gate.
#
# Implements Recommendation #1 from
# qa/reports/2026-05-25/bundle-size-baseline-2026-05-25.md.
# Builds the 3 captive-portal entrypoints fresh (NO trust of any
# pre-existing dist/ artifact), measures gzip-compressed bytes per
# file, prints every measurement, and exits non-zero if any file
# exceeded its watchlist threshold.
#
# All measurements print regardless of pass/fail so PR authors see
# every violation in one CI run, not whack-a-mole. The first-failure
# exit code at the end is what GitHub Actions uses for the red mark.
#
# Threshold breach is a FLAG, not an auto-revert. PR author response:
# either trim the bundle, OR open a follow-up commit to update the
# threshold in this script with justification (per the baseline
# report's threshold policy).
#
# Run locally:
#   cd ui && bash scripts/check-bundle-sizes.sh
#
# Bash 3.2-portable (parallel-array, no associative arrays) so it
# runs on macOS dev machines without bash 4+ install.

set -euo pipefail

# Resolve ui/ working directory from script's own location so the
# check works whether invoked from repo root or from ui/.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UI_DIR="$(dirname "$SCRIPT_DIR")"
cd "$UI_DIR"

# Locate esbuild. CI's `npm ci` populates node_modules/.bin/ with the
# wrapper symlink + the platform-specific binary in
# node_modules/@esbuild/<arch>/bin/. Local dev under macOS-virtiofs
# can leave node_modules/.bin/ broken (storm postmortem 2026-05-24)
# but the @esbuild/<arch>/ binary stays intact -- try both.
ESBUILD=""
if [ -x node_modules/.bin/esbuild ]; then
    ESBUILD="node_modules/.bin/esbuild"
else
    for arch_bin in node_modules/@esbuild/*/bin/esbuild; do
        if [ -x "$arch_bin" ]; then
            ESBUILD="$arch_bin"
            break
        fi
    done
fi
if [ -z "$ESBUILD" ]; then
    echo "Error: esbuild binary not found (tried node_modules/.bin + node_modules/@esbuild/*/bin/)" >&2
    exit 2
fi

# Fresh build of the 3 captive-portal entries + the dashboard
# entrypoint into tmp dirs. Matches the 2026-05-25 baseline
# measurement commands exactly so thresholds stay calibrated to the
# same shape.
#
# The dashboard build (main.js) requires renderer-wasm/pkg/ to be
# present so esbuild can resolve src/wasm-renderer.js's import. In
# CI that's downloaded as an artifact from the build-wasm job before
# this script runs; locally it's produced by
# scripts/build_wasm_renderer.sh.
CAPTIVE_DIR="$(mktemp -d -t omq-bundle-captive-XXXXXX)"
DASHBOARD_DIR="$(mktemp -d -t omq-bundle-dashboard-XXXXXX)"
trap 'rm -rf "$CAPTIVE_DIR" "$DASHBOARD_DIR"' EXIT

"$ESBUILD" \
    src/welcome.js src/set-password.js src/login.js \
    --format=esm --bundle --splitting --minify \
    --loader:.wasm=binary \
    --outdir="$CAPTIVE_DIR"

if [ -d ../renderer-wasm/pkg ]; then
    "$ESBUILD" \
        src/main.js ffmpeg-worker=node_modules/@ffmpeg/ffmpeg/dist/esm/worker.js \
        --format=esm --bundle --splitting --minify \
        --loader:.wasm=binary \
        --outdir="$DASHBOARD_DIR"
    DASHBOARD_BUILT=1
else
    echo "WARN: ../renderer-wasm/pkg/ not present; skipping dashboard gate." >&2
    echo "      Run scripts/build_wasm_renderer.sh first (local), OR ensure" >&2
    echo "      the build-wasm CI job ran before this step (CI)." >&2
    DASHBOARD_BUILT=0
fi

# Watchlist thresholds (gzip-compressed bytes). Sources:
# - qa/reports/2026-05-25/bundle-size-baseline-2026-05-25.md (captive)
# - qa/reports/2026-05-25/dashboard-bundle-baseline-2026-05-25.md (dashboard)
# Both at ~15% above 2026-05-25 baseline. SI bytes (KB = 1000)
# per the reports' units convention.
#
# Parallel arrays for bash 3.2 portability (no associative arrays).
FILES=(
    "$CAPTIVE_DIR/welcome.js"
    "$CAPTIVE_DIR/login.js"
    "$CAPTIVE_DIR/set-password.js"
    "welcome.html"
    "login.html"
    "set-password.html"
)
THRESHOLDS=(
    11800
    1130
    1370
    5160
    2620
    3020
)
# Friendly labels for the log so PR authors see relative paths
# rather than the tmp-dir absolute paths.
LABELS=(
    "dist-captive/welcome.js"
    "dist-captive/login.js"
    "dist-captive/set-password.js"
    "ui/welcome.html"
    "ui/login.html"
    "ui/set-password.html"
)

# Dashboard files (added only if the dashboard build succeeded).
# The sortable.esm-*.js name has a content-hash suffix esbuild assigns
# per build; resolve it via glob at check-time + bail if it doesn't
# match a single file (an upstream change splitting sortable across
# multiple chunks should be a manual triage, not a silent SUM).
if [ "$DASHBOARD_BUILT" -eq 1 ]; then
    # Glob for the sortable chunk (esbuild's content-hash filename).
    SORTABLE_GLOB=("$DASHBOARD_DIR"/sortable.esm-*.js)
    if [ ${#SORTABLE_GLOB[@]} -ne 1 ] || [ ! -f "${SORTABLE_GLOB[0]}" ]; then
        echo "ERROR: expected exactly 1 sortable.esm-*.js chunk in $DASHBOARD_DIR, found ${#SORTABLE_GLOB[@]}" >&2
        echo "       (esbuild splitting may have changed; thresholds need re-baselining)" >&2
        exit 2
    fi
    SORTABLE_CHUNK="${SORTABLE_GLOB[0]}"

    FILES+=(
        "$DASHBOARD_DIR/main.js"
        "$SORTABLE_CHUNK"
        "../renderer-wasm/pkg/renderer_wasm_bg.wasm"
        "../renderer-wasm/pkg/renderer_wasm.js"
        "index.html"
    )
    THRESHOLDS+=(
        72000
        18000
        60600
        3600
        1400
    )
    LABELS+=(
        "dist-dashboard/main.js"
        "dist-dashboard/sortable.esm-*.js"
        "renderer-wasm/pkg/renderer_wasm_bg.wasm"
        "renderer-wasm/pkg/renderer_wasm.js"
        "ui/index.html"
    )
fi

violations=0
if [ "$DASHBOARD_BUILT" -eq 1 ]; then
    echo "Bundle-size gate -- captive-portal + dashboard (gzip bytes vs threshold):"
else
    echo "Bundle-size gate -- captive-portal ONLY (dashboard skipped, see WARN above):"
fi
echo ""
printf "  %-32s %10s %10s %6s  %s\n" "FILE" "GZ BYTES" "THRESHOLD" "% USED" "STATUS"
printf "  %-32s %10s %10s %6s  %s\n" "----" "--------" "---------" "------" "------"
for i in "${!FILES[@]}"; do
    file="${FILES[$i]}"
    threshold="${THRESHOLDS[$i]}"
    label="${LABELS[$i]}"
    if [ ! -f "$file" ]; then
        printf "  %-32s %10s %10d %6s  %s\n" "$label" "MISSING" "$threshold" "n/a" "FAIL"
        violations=$((violations + 1))
        continue
    fi
    gz=$(gzip -c "$file" | wc -c | tr -d ' ')
    # Avoid division-by-zero on bad threshold input.
    if [ "$threshold" -eq 0 ]; then
        pct="--"
    else
        pct=$((100 * gz / threshold))
    fi
    if [ "$gz" -gt "$threshold" ]; then
        printf "  %-32s %10d %10d %5d%%  %s\n" "$label" "$gz" "$threshold" "$pct" "BREACH"
        violations=$((violations + 1))
    else
        printf "  %-32s %10d %10d %5d%%  %s\n" "$label" "$gz" "$threshold" "$pct" "OK"
    fi
done

echo ""
if [ "$violations" -gt 0 ]; then
    echo "FAIL: $violations file(s) over threshold."
    echo ""
    echo "Response options for the PR author:"
    echo "  1. Reduce the bundle (refactor, lazy-load, drop a transitive dep)."
    echo "  2. Update the threshold in ui/scripts/check-bundle-sizes.sh with"
    echo "     justification in the commit message (per the threshold policy"
    echo "     in qa/reports/2026-05-25/bundle-size-baseline-2026-05-25.md:"
    echo "     'If any threshold trips: surface as a finding, not auto-revert.')."
    exit 1
fi
echo "PASS: all ${#FILES[@]} files within threshold."
