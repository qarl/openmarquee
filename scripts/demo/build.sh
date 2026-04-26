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
rsync -a --delete \
    --exclude='vendor/ffmpeg-core/' \
    --exclude='ffmpeg-worker.js' \
    "$SRC_UI/dist/" "$DEST/dist/"

# Same stylesheet path (./styles.css) as the device UI so demo's
# index.html doesn't need URL rewrites.
cp "$SRC_UI/styles.css" "$DEST/styles.css"

# @font-face TTFs that styles.css references via url("fonts/<name>.ttf").
# Without these, slides that pick a bundled face fall back to system.
rsync -a --delete --exclude='LICENSES.md' "$SRC_UI/fonts/" "$DEST/fonts/"

echo "demo bundle ready at $DEST/"
du -sh "$DEST" | awk '{print "total size:", $1}'
