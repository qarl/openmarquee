#!/usr/bin/env bash
# scripts/tests/test_deploy_sh_runtime_asset_manifest.sh
#
# r33 (2026-05-31) regression guard: deploy.sh must protect every
# gitignored runtime-required asset, either by (a) invoking a
# download script BEFORE sync_to_build_dir so the asset lands in
# the BUILD_DIR mirror + survives the rsync, or (b) by an
# `--exclude` on the relevant rsync so an absent local file
# doesn't cause --delete to wipe an existing copy on the target.
#
# Bug class generalized from r32: my r31 /tmp-clone deploy wiped
# `noto-color-emoji-colrv1.ttf` from FYS prod via the ui/ rsync
# --delete, because the clone never ran setup.sh and the font is
# gitignored. r32 (621b4f3) fixed the specific case by adding a
# download-script call + a --exclude for the legacy CBDT variant.
# r33 codifies the class so future runtime assets are checked
# the same way at static-parse time.
#
# This test is a STATIC PARSE: no `bash deploy.sh` execution, no
# network, no SSH, no sudo. It greps deploy.sh for protection
# markers per the manifest below. Safe to run in any CI; <100 ms.
#
# Adding a new runtime asset:
#   1. Append (path, protection, marker) to the MANIFEST array
#      below.
#   2. Wire the protection into deploy.sh.
#   3. Re-run this test.
#
# Adversarial validation (performed 2026-05-31 before commit):
#   - removed the download-emoji-font-colrv1.sh call from
#     deploy.sh → FAIL with the colrv1 download marker
#     diagnostic.
#   - removed --exclude 'fonts/noto-color-emoji.ttf' from the
#     ui rsync → FAIL with the legacy CBDT marker diagnostic.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="${SCRIPT_DIR}/../deploy.sh"

if [ ! -f "$DEPLOY_SH" ]; then
    echo "FAIL: deploy.sh not found at $DEPLOY_SH" >&2
    exit 1
fi

# Manifest of gitignored runtime-required (or operator-drop)
# assets that deploy.sh must protect.
#
# Format: "relative-path|protection-type|marker"
#   protection-type:
#     download_before_sync  -- a download script invocation that
#                              MUST appear in deploy.sh BEFORE the
#                              `sync_to_build_dir` call. Marker is
#                              the script basename used to grep.
#     rsync_ui_exclude      -- an `--exclude '<pattern>'` that
#                              MUST appear in deploy.sh BEFORE the
#                              ui rsync's source/target pair, so
#                              --delete on the ui rsync doesn't
#                              wipe the existing-on-target file.
#                              Marker is the rsync --exclude
#                              pattern (single-quoted as written).
#
# Asset 1: ui/fonts/noto-color-emoji-colrv1.ttf -- COLRv1 vector
# emoji TTF (~5 MB). Read at runtime by renderer/src/glyph_cache_
# colr.rs:55 + glyph_cache.rs:791. Gitignored; downloaded by
# scripts/download-emoji-font-colrv1.sh (called from setup.sh at
# clone time + from deploy.sh prep at deploy time). The
# pre-existing scripts/tests/test_deploy_sh_ensures_emoji_font.sh
# also checks this specific case with stricter ordering vs ui
# rsync; this manifest entry intentionally duplicates the basic
# "download call present at all" check so a single test failure
# names the entire class.
#
# Asset 2: ui/fonts/noto-color-emoji.ttf -- legacy CBDT bitmap
# emoji TTF (~10 MB). Operator-drop (no auto-download); seed.py:
# 1539 reads it for emoji in baked seed-slide backgrounds when
# present, returns None otherwise (no crash). The --exclude
# protects an operator-dropped copy from being --deleted off the
# target by a /tmp-clone deploy that doesn't have the file
# locally. See feedback_deploy_sh_clone_workflow_gitignored_
# assets.md for the regression history.
MANIFEST=(
    "ui/fonts/noto-color-emoji-colrv1.ttf|download_before_sync|download-emoji-font-colrv1.sh"
    "ui/fonts/noto-color-emoji.ttf|rsync_ui_exclude|fonts/noto-color-emoji.ttf"
)

get_line() {
    # `-F` matches as a fixed string so manifest entries with
    # regex metachars (`.`, `+`, `[`, etc.) don't over- or
    # under-match. `-e` disambiguates from grep's own flags so
    # patterns that start with `--` (e.g. `--exclude 'fonts/...'`)
    # don't get parsed as options.
    # `|| true` keeps `set -o pipefail` from aborting the script
    # when grep returns no match; the caller's empty-string check
    # produces a clear FAIL message instead of a bare exit-1.
    (grep -F -n -e "$1" "$DEPLOY_SH" | head -1 | cut -d: -f1) || true
}

get_line_regex() {
    # Like get_line but uses basic regex (no -F) so callers can
    # anchor with ^ / $ — needed when a fixed-string substring
    # appears in both a comment and the line we actually want.
    (grep -n -e "$1" "$DEPLOY_SH" | head -1 | cut -d: -f1) || true
}

# Anchor lines in deploy.sh. The ui rsync block runs from
# UI_RSYNC_HEAD_LINE (the `echo "==> rsync UI to ..."` banner)
# to RSYNC_UI_LINE (the trailing src/dst pair). Any --exclude
# that protects an asset from the ui rsync's --delete MUST sit
# strictly inside that window — an exclude before the banner
# belongs to an earlier rsync (backend); one after the pair
# belongs to scripts/system/images/wheels. Either misplacement
# leaves the ui rsync's --delete free to wipe the asset.
#
# SYNC_LINE uses `^sync_to_build_dir$` because the substring
# `sync_to_build_dir` also appears in deploy.sh's r32 comment
# (line ~56) — anchored line-match picks the actual function
# call at line ~78.
SYNC_LINE=$(get_line_regex '^sync_to_build_dir$')
UI_RSYNC_HEAD_LINE=$(get_line '==> rsync UI to')
RSYNC_UI_LINE=$(get_line '"$OPENMARQUEE_BUILD_DIR/ui/" "$TARGET:$REMOTE_ROOT/ui/"')
for var in SYNC_LINE UI_RSYNC_HEAD_LINE RSYNC_UI_LINE; do
    val="${!var}"
    if [ -z "$val" ]; then
        echo "FAIL: anchor $var not found in deploy.sh" >&2
        echo "       deploy.sh refactor; update the anchor regex above." >&2
        exit 1
    fi
done
if [ "$UI_RSYNC_HEAD_LINE" -ge "$RSYNC_UI_LINE" ]; then
    echo "FAIL: ui rsync head ($UI_RSYNC_HEAD_LINE) is not above the src/dst pair ($RSYNC_UI_LINE)" >&2
    echo "       deploy.sh refactor reshuffled the block; update anchors." >&2
    exit 1
fi

fail=0
echo "anchors:  sync_to_build_dir=line $SYNC_LINE  ui-rsync-head=line $UI_RSYNC_HEAD_LINE  ui-rsync-pair=line $RSYNC_UI_LINE"
echo

for entry in "${MANIFEST[@]}"; do
    IFS='|' read -r asset_path protection marker <<<"$entry"

    case "$protection" in
        download_before_sync)
            line=$(get_line "$marker")
            if [ -z "$line" ]; then
                echo "FAIL  $asset_path" >&2
                echo "      expected download invocation '$marker' in deploy.sh; not found" >&2
                echo "      → asset would be ABSENT from BUILD_DIR; ui rsync --delete would WIPE it from target" >&2
                fail=1
                continue
            fi
            if [ "$line" -ge "$SYNC_LINE" ]; then
                echo "FAIL  $asset_path" >&2
                echo "      download '$marker' at line $line; sync_to_build_dir at line $SYNC_LINE" >&2
                echo "      → download must run BEFORE sync_to_build_dir so the file lands in BUILD_DIR" >&2
                fail=1
                continue
            fi
            if [ "$line" -ge "$RSYNC_UI_LINE" ]; then
                echo "FAIL  $asset_path" >&2
                echo "      download '$marker' at line $line; ui rsync at line $RSYNC_UI_LINE" >&2
                echo "      → download must run BEFORE ui rsync so target receives the file" >&2
                fail=1
                continue
            fi
            echo "OK    $asset_path  (download '$marker' at line $line)"
            ;;
        rsync_ui_exclude)
            # The exclude must sit strictly inside the ui rsync block
            # — i.e. between the `==> rsync UI to` banner and the
            # src/dst pair. An exclude BEFORE the banner belongs to
            # the earlier backend rsync (also has --delete, but for a
            # different tree); one AFTER the pair belongs to the
            # scripts/system/images/wheels rsyncs that follow. In
            # either misplacement, the ui rsync's --delete is still
            # free to wipe this asset. We scan the rsync block with
            # awk so the FIRST in-window match (if any) is what we
            # check, not the first match anywhere in the file.
            exclude_line=$(awk -v lo="$UI_RSYNC_HEAD_LINE" -v hi="$RSYNC_UI_LINE" \
                -v needle="--exclude '$marker'" \
                'NR > lo && NR <= hi && index($0, needle) { print NR; exit }' \
                "$DEPLOY_SH")
            if [ -z "$exclude_line" ]; then
                # Was the marker present at all in deploy.sh? A friendlier
                # diagnostic when someone moved it into the wrong rsync.
                stray_line=$(get_line "--exclude '$marker'")
                echo "FAIL  $asset_path" >&2
                if [ -n "$stray_line" ]; then
                    echo "      \"--exclude '$marker'\" present at line $stray_line, but OUTSIDE the ui rsync block ($UI_RSYNC_HEAD_LINE..$RSYNC_UI_LINE)" >&2
                    echo "      → ui rsync --delete would still WIPE this file from the target" >&2
                else
                    echo "      expected \"--exclude '$marker'\" inside the ui rsync block ($UI_RSYNC_HEAD_LINE..$RSYNC_UI_LINE); not found anywhere" >&2
                    echo "      → an operator-dropped copy on the target would be WIPED by ui rsync --delete" >&2
                    echo "        whenever the local clone lacks this file (the usual /tmp clone case)" >&2
                fi
                fail=1
                continue
            fi
            echo "OK    $asset_path  (--exclude at line $exclude_line, inside ui rsync block)"
            ;;
        *)
            echo "FAIL  $asset_path" >&2
            echo "      unknown protection type '$protection' in manifest" >&2
            echo "      (valid: download_before_sync, rsync_ui_exclude)" >&2
            fail=1
            ;;
    esac
done

echo
if [ "$fail" -eq 1 ]; then
    echo "FAIL: deploy.sh does not protect every runtime-required gitignored asset." >&2
    echo "      r33 invariant: every entry in MANIFEST above MUST be either" >&2
    echo "      downloaded before sync_to_build_dir or excluded from the ui" >&2
    echo "      rsync. See feedback_deploy_sh_clone_workflow_gitignored_assets.md." >&2
    exit 1
fi

echo "OK: deploy.sh protects all ${#MANIFEST[@]} gitignored runtime assets in MANIFEST."
