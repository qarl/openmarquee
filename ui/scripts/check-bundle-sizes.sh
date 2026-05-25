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

# Fresh build of the 3 captive-portal entries into a tmp dir. Matches
# the 2026-05-25 baseline measurement command exactly so thresholds
# stay calibrated to the same shape. Excluding main.js here because
# main.js needs renderer-wasm/pkg/ (built by the separate build-wasm
# CI job) -- this gate runs against a captive-only subset.
BUILD_DIR="$(mktemp -d -t omq-bundle-XXXXXX)"
trap 'rm -rf "$BUILD_DIR"' EXIT

"$ESBUILD" \
    src/welcome.js src/set-password.js src/login.js \
    --format=esm --bundle --splitting --minify \
    --loader:.wasm=binary \
    --outdir="$BUILD_DIR"

# Watchlist thresholds (gzip-compressed bytes). Source:
# qa/reports/2026-05-25/bundle-size-baseline-2026-05-25.md, the
# corrected v2 watchlist table. ~15% above the 2026-05-25 baseline.
# SI bytes (KB = 1000) per the report's units convention.
#
# Parallel arrays for bash 3.2 portability (no associative arrays).
FILES=(
    "$BUILD_DIR/welcome.js"
    "$BUILD_DIR/login.js"
    "$BUILD_DIR/set-password.js"
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

violations=0
echo "Captive-portal bundle-size gate (gzip bytes vs threshold):"
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
