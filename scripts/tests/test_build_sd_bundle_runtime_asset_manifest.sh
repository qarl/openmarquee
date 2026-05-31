#!/usr/bin/env bash
# scripts/tests/test_build_sd_bundle_runtime_asset_manifest.sh
#
# r37a (2026-05-31) regression guard: build_sd_bundle.sh must invoke
# the download script for every gitignored runtime-required asset
# BEFORE the rsync(s) that would otherwise ship a stale/missing copy.
#
# Mirrors scripts/tests/test_deploy_sh_runtime_asset_manifest.sh
# (commit 3cee501, r33) — same bug class for the customer-shipping
# bundle artifact instead of the dev redeploy path.
#
# Bug class: a /tmp clone build environment never runs setup.sh, so
# the COLRv1 emoji font (~5 MB, gitignored) is absent. The bundle's
# ui/ rsync silently copies whatever ui/fonts/ has, and customers
# fresh-burning the bundle see emoji tofu. Same regression mode that
# bit FYS prod 2026-05-31 r31 -> r32, surfaced for the bundle path
# by code2's r36 audit §B.5 + §C.1.
#
# This test is a STATIC PARSE: no `bash build_sd_bundle.sh` execution,
# no network, no shasum. It greps the bundle script for the asset
# protection markers per the MANIFEST below. Safe to run in any CI;
# <100 ms.
#
# Adding a new bundle-shipped gitignored asset:
#   1. Append (path, protection, marker) to the MANIFEST array
#      below.
#   2. Wire the protection into build_sd_bundle.sh.
#   3. Re-run this test.
#
# Adversarial validation (performed 2026-05-31 before commit):
#   - removed the download-emoji-font-colrv1.sh call from
#     build_sd_bundle.sh -> FAIL with the colrv1 download marker
#     diagnostic.
#   - moved the call AFTER the ui rsync (so the staging directory
#     would not have the font when rsync ran) -> FAIL with the
#     "download must run BEFORE ui rsync" diagnostic.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_SH="${SCRIPT_DIR}/../build_sd_bundle.sh"

if [ ! -f "$BUNDLE_SH" ]; then
    echo "FAIL: build_sd_bundle.sh not found at $BUNDLE_SH" >&2
    exit 1
fi

# Manifest of gitignored runtime-required (or operator-drop) assets
# that build_sd_bundle.sh must protect.
#
# Format: "relative-path|protection-type|marker"
#   protection-type:
#     download_before_rsync -- a download script invocation that
#                              MUST appear in build_sd_bundle.sh
#                              BEFORE the `ui/` rsync src/dst pair.
#                              Marker is the FULL bash invocation
#                              line (NOT just the script basename),
#                              so doc-comment references like
#                              `\`bash scripts/foo.sh\` manually` in
#                              error messages don't false-positive.
#
# Asset 1: ui/fonts/noto-color-emoji-colrv1.ttf -- COLRv1 vector
# emoji TTF (~5 MB stripped). Required at runtime by the device
# renderer's COLR dispatch path. Gitignored; downloaded by
# scripts/download-emoji-font-colrv1.sh.
#
# Intentionally NOT included (diverges from r33's deploy.sh
# MANIFEST):
#   - ui/fonts/noto-color-emoji.ttf (legacy CBDT, ~10 MB,
#     operator-drop). The deploy.sh manifest test protects this
#     via an --exclude on the ui rsync so an operator-dropped
#     copy on the TARGET isn't wiped by --delete. The bundle has
#     no analogous concern: it produces a fresh artifact for a
#     fresh SD card; there's no "existing on target" to preserve.
#     If a customer wants the legacy CBDT font on their device,
#     they drop it post-install per backend/openmarquee/seed.py:
#     1528-1535. Adding it to the bundle MANIFEST would require
#     a new `protection-type: explicit_include` which has no
#     current marker (no download script, no rsync include) — out
#     of scope for r37a.
MANIFEST=(
    "ui/fonts/noto-color-emoji-colrv1.ttf|download_before_rsync|bash \"\$REPO_ROOT/scripts/download-emoji-font-colrv1.sh\""
)

get_line() {
    # `-F` matches as a fixed string so manifest entries with regex
    # metachars (`.`, `+`, `[`, etc.) don't over- or under-match.
    # `-e` disambiguates from grep's own flags so patterns that start
    # with `--` (e.g. `--exclude 'fonts/...'`) don't get parsed as
    # options.
    # `|| true` keeps `set -o pipefail` from aborting the script
    # when grep returns no match; the caller's empty-string check
    # produces a clear FAIL message instead of a bare exit-1.
    (grep -F -n -e "$1" "$BUNDLE_SH" | head -1 | cut -d: -f1) || true
}

# Anchor lines in build_sd_bundle.sh. The ui rsync block runs from
# UI_RSYNC_HEAD_LINE (the `Copying ui/ to staging` banner) to
# RSYNC_UI_LINE (the trailing src/dst pair).
UI_RSYNC_HEAD_LINE=$(get_line 'Copying ui/ to staging')
RSYNC_UI_LINE=$(get_line '"$REPO_ROOT/ui/" "$ROOT/ui/"')

for var in UI_RSYNC_HEAD_LINE RSYNC_UI_LINE; do
    val="${!var}"
    if [ -z "$val" ]; then
        echo "FAIL: anchor $var not found in build_sd_bundle.sh" >&2
        echo "       refactor moved the marker; update the anchor regex above." >&2
        exit 1
    fi
done
if [ "$UI_RSYNC_HEAD_LINE" -ge "$RSYNC_UI_LINE" ]; then
    echo "FAIL: ui rsync head ($UI_RSYNC_HEAD_LINE) is not above the src/dst pair ($RSYNC_UI_LINE)" >&2
    echo "       refactor reshuffled the block; update anchors." >&2
    exit 1
fi

fail=0
echo "anchors:  ui-rsync-head=line $UI_RSYNC_HEAD_LINE  ui-rsync-pair=line $RSYNC_UI_LINE"
echo

for entry in "${MANIFEST[@]}"; do
    IFS='|' read -r asset_path protection marker <<<"$entry"

    case "$protection" in
        download_before_rsync)
            line=$(get_line "$marker")
            if [ -z "$line" ]; then
                echo "FAIL  $asset_path" >&2
                echo "      expected download invocation '$marker' in build_sd_bundle.sh; not found" >&2
                echo "      -> asset would be ABSENT from the bundle's ui/fonts/; customer fresh-burns see tofu" >&2
                fail=1
                continue
            fi
            if [ "$line" -ge "$RSYNC_UI_LINE" ]; then
                echo "FAIL  $asset_path" >&2
                echo "      download '$marker' at line $line; ui rsync at line $RSYNC_UI_LINE" >&2
                echo "      -> download must run BEFORE ui rsync so the source tree has the asset to copy" >&2
                fail=1
                continue
            fi
            echo "OK    $asset_path  (download '$marker' at line $line)"
            ;;
        *)
            echo "FAIL  $asset_path" >&2
            echo "      unknown protection type '$protection' in manifest" >&2
            echo "      (valid: download_before_rsync)" >&2
            fail=1
            ;;
    esac
done

echo
if [ "$fail" -eq 1 ]; then
    echo "FAIL: build_sd_bundle.sh does not protect every runtime-required gitignored asset." >&2
    echo "      r37a invariant: every entry in MANIFEST above MUST be downloaded" >&2
    echo "      before the ui rsync. See qa/r36-sd-bundle-rebuild-audit-2026-05-31.md §C.1." >&2
    exit 1
fi

echo "OK: build_sd_bundle.sh protects all ${#MANIFEST[@]} gitignored runtime assets in MANIFEST."
