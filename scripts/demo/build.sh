#!/usr/bin/env bash
# Assemble the demo bundle into $OPENMARQUEE_BUILD_DIR/demo/.
#
# Usage:
#   scripts/demo/build.sh                           # uses $OPENMARQUEE_BUILD_DIR/ui
#   scripts/demo/build.sh /path/to/built-ui/        # explicit src
#
# What lands in the build dir:
#   index.html, mock-backend.js, sw.js   ← static/  (this repo)
#   styles.css                           ← built UI
#   dist/                                ← built UI (esbuild output)
#   fonts/                               ← built UI (@font-face TTFs)
#
# seed.json + assets/ are produced by generate-seed.py against a running
# backend; this script doesn't touch them. www's deploy.sh rsyncs the
# whole $OPENMARQUEE_BUILD_DIR/demo/ tree to openmarquee.com/demo/.
set -euo pipefail

cd "$(dirname "$0")"

SRC_UI="${1:-${OPENMARQUEE_BUILD_DIR:-$HOME/tmp/openmarquee-build}/ui}"
BUILD_DIR="${OPENMARQUEE_BUILD_DIR:-$HOME/tmp/openmarquee-build}"
DEST="$BUILD_DIR/demo"

if [ ! -f "$SRC_UI/dist/main.js" ]; then
    echo "error: $SRC_UI/dist/main.js not found — run 'npm run build' in the UI dir first" >&2
    exit 2
fi

mkdir -p "$DEST/dist" "$DEST/fonts"

# Static demo shell — index.html with reset button, mock-backend.js
# (in-browser API), sw.js (image rewrites).
cp static/index.html "$DEST/index.html"
cp static/mock-backend.js "$DEST/mock-backend.js"
cp static/sw.js "$DEST/sw.js"

# Bundled UI. Skip vendor/ffmpeg-core (~30 MB of video-transcoding wasm)
# because the demo disables uploads → ffmpeg never runs. Ships faster.
# Also exclude all dotfiles — covers esbuild's atomic-write tempfiles
# (`.<name>.<random>` left after an interrupted build) AND macOS
# AppleDouble metadata (`._<name>`) AND any other dotfile cruft. Both
# would otherwise propagate to the live demo as hidden-but-discoverable
# URLs (flagged by www-Jimmy + QA 2026-05-03; 7 stale tempfiles dated
# Apr 28 + 4 ._ files in $DEST/dist/).
rsync -a --delete --delete-excluded \
    --exclude='vendor/ffmpeg-core/' \
    --exclude='ffmpeg-worker.js' \
    --exclude='.*' \
    "$SRC_UI/dist/" "$DEST/dist/"
# Belt-and-suspenders: --delete-excluded above should remove dotfiles
# that USED to exist in $DEST/dist/ but are now excluded. Run an
# explicit `find -delete` afterwards in case a future rsync flag
# change loses --delete-excluded — same shape sweep used in refresh.sh.
find "$DEST/dist" -maxdepth 1 -type f -name '.*' -delete 2>/dev/null || true

# Batch 12.x followup (www-Jimmy 2026-05-11): drop orphan hashed chunks
# that npm run build left behind after a content-hash flip. Same shape
# as the dotfile leak above: rsync mirrors what's there; if a prior
# build wrote `sortable.esm-RLL2D47F.js` and the current build wrote
# `sortable.esm-PUWFMERW.js`, BOTH end up in dist/ and both deploy.
# main.js only loads the current one; the old one is dead weight on
# the live demo. Sweep removes hashed files not referenced by any
# non-hashed entry file (main.js / login.js / set-password.js / etc.).
bash "$(dirname "$0")/sweep_orphan_chunks.sh" "$DEST/dist"

# .htaccess for the live demo on DreamHost (www-Jimmy 2026-05-05;
# restored 2026-05-11 after a refactor dropped it). Sets the response
# Cache-Control on dist/* to 5 minutes (was 30 days by host default,
# so a sortable/main.js update wouldn't reach operators' browsers for
# a month). Must be written AFTER both the dotfile sweep (find
# -delete '.*') and the orphan-chunk sweep -- either would wipe it
# if it landed earlier. deploy.sh's rsync --delete only excludes
# .DS_Store + ._*, so the .htaccess MUST exist in BUILD_DIR before
# each deploy or the server copy gets wiped.
cat > "$DEST/dist/.htaccess" <<'HTACCESS'
<IfModule mod_expires.c>
  ExpiresActive On
  ExpiresDefault "access plus 5 minutes"
</IfModule>
<IfModule mod_headers.c>
  Header set Cache-Control "max-age=300, must-revalidate"
</IfModule>
HTACCESS

# Same stylesheet path (./styles.css) as the device UI so demo's
# index.html doesn't need URL rewrites.
cp "$SRC_UI/styles.css" "$DEST/styles.css"

# @font-face TTFs that styles.css references via url("fonts/<name>.ttf").
# Without these, slides that pick a bundled face fall back to system.
# Same dotfile-exclusion shape as the dist/ rsync above so any macOS
# AppleDouble metadata that landed in fonts/ via SMB / Finder doesn't
# propagate to the live demo.
rsync -a --delete --delete-excluded \
    --exclude='LICENSES.md' \
    --exclude='.*' \
    "$SRC_UI/fonts/" "$DEST/fonts/"

# 14.1: regression guard against stale main.js shipping eager-loaded
# dependencies that Batch 7 moved behind dynamic imports. The cold-load
# bundle MUST NOT contain "Sortable" (sortablejs is now lazy-loaded on
# track interaction; if it shows up here, the wrong main.js was copied
# in -- 219KB pre-Batch-7 build vs 178KB post). Sweep #6 caught a
# 41KB stale bundle on the live demo this way (2026-05-11).
if grep -q "Sortable" "$DEST/dist/main.js"; then
    echo "error: $DEST/dist/main.js contains 'Sortable' -- stale pre-Batch-7 bundle?" >&2
    echo "       (sortablejs is lazy-loaded; main.js should not reference it)" >&2
    exit 3
fi

echo "demo bundle ready at $DEST/"
du -sh "$DEST" | awk '{print "total size:", $1}'
