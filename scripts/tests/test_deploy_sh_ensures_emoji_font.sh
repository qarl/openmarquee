#!/usr/bin/env bash
# scripts/tests/test_deploy_sh_ensures_emoji_font.sh
#
# r32 (2026-05-31) regression guard: deploy.sh must invoke
# download-emoji-font-colrv1.sh BEFORE sync_to_build_dir, so the
# COLRv1 emoji font lands in $OPENMARQUEE_SRC/ui/fonts/ before the
# BUILD_DIR mirror + the ui/ rsync to the target.
#
# Why: the font is gitignored (~5 MB binary; downloaded once at
# clone time by scripts/setup.sh). The /tmp clone workflow that
# deploy.sh frequently runs from never invokes setup.sh, so the
# font is absent from a fresh clone. Without the download-script
# call, deploy.sh's ui/ rsync --delete WIPES the existing emoji
# font from the target. The runtime COLR dispatch then can't load
# the font -> rasterize_colr_cell returns Err -> emoji renders as
# Tofu via the MSDF FontMissing fallback.
#
# r32 origin: my own r31 deploy hit this on FYS prod 2026-05-31;
# QA mis-attributed the regression to r25's prewarm gate but the
# actual cause was the deploy pipeline silently shipping a pruned
# ui/fonts/ that --delete propagated to the target.
#
# This test is a STATIC PARSE: no `bash deploy.sh` execution, no
# network, no SSH. It asserts ordering of two string markers in
# deploy.sh. Safe to run in any CI; runs in <100 ms.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="${SCRIPT_DIR}/../deploy.sh"

if [ ! -f "$DEPLOY_SH" ]; then
    echo "FAIL: deploy.sh not found at $DEPLOY_SH" >&2
    exit 1
fi

get_line() {
    # `|| true` on the pipeline: when grep finds no match the pipeline
    # exits non-zero, and `set -o pipefail` would otherwise abort the
    # script BEFORE the explicit `[ -z "$val" ]` check below. The empty
    # return then flows into the FAIL path with the proper diagnostic
    # message instead of a bare exit-1.
    (grep -n "$1" "$DEPLOY_SH" | head -1 | cut -d: -f1) || true
}

DOWNLOAD_LINE=$(get_line 'download-emoji-font-colrv1.sh')
SYNC_LINE=$(get_line '^sync_to_build_dir$')
RSYNC_UI_LINE=$(get_line '"\$OPENMARQUEE_BUILD_DIR/ui/" "\$TARGET:\$REMOTE_ROOT/ui/"')

for var in DOWNLOAD_LINE SYNC_LINE RSYNC_UI_LINE; do
    val="${!var}"
    if [ -z "$val" ]; then
        echo "FAIL: $var marker not found in deploy.sh" >&2
        echo "  (deploy.sh refactor may have moved the call; if intentional," >&2
        echo "   update the get_line regex; otherwise re-add the call)." >&2
        exit 1
    fi
done

fail=0
if [ "$DOWNLOAD_LINE" -ge "$SYNC_LINE" ]; then
    echo "FAIL: download-emoji-font-colrv1.sh (line $DOWNLOAD_LINE) called AFTER sync_to_build_dir (line $SYNC_LINE)" >&2
    fail=1
fi
if [ "$DOWNLOAD_LINE" -ge "$RSYNC_UI_LINE" ]; then
    echo "FAIL: download-emoji-font-colrv1.sh (line $DOWNLOAD_LINE) called AFTER ui rsync (line $RSYNC_UI_LINE)" >&2
    fail=1
fi

if [ "$fail" -eq 1 ]; then
    echo >&2
    echo "  r32 invariant: deploy.sh MUST call download-emoji-font-colrv1.sh" >&2
    echo "  BEFORE sync_to_build_dir + ui rsync, so the BUILD_DIR mirror has" >&2
    echo "  the font and the --delete rsync doesn't WIPE it from the target." >&2
    echo "  See deploy.sh meta-comment + feedback_deploy_sh_emoji_font_wipe.md." >&2
    exit 1
fi

echo "OK: deploy.sh ensures emoji font present before sync_to_build_dir + ui rsync"
echo "  download-emoji-font-colrv1.sh call: line $DOWNLOAD_LINE"
echo "  sync_to_build_dir:                  line $SYNC_LINE"
echo "  ui rsync:                           line $RSYNC_UI_LINE"
