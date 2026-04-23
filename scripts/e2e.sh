#!/usr/bin/env bash
# scripts/e2e.sh — run the Playwright end-to-end suite.
#
# Auto-starts uvicorn on port 8765 with isolated content paths in /tmp,
# runs the specs in ui/e2e/, then tears down the backend.
#
# First run: bash scripts/e2e.sh install   (downloads Chromium)
#
# Watching the tests run: set PW_SLOWMO=<ms> to pace every Playwright action.
# Example: PW_SLOWMO=200 bash scripts/e2e.sh  (slow enough to follow by eye)
#
# Build + tests run from $OPENMARQUEE_DEPS_DIR/ui (the real node_modules
# location), with absolute paths back to the source tree. The ui/node_modules
# symlink can be rendered broken on rclone-style mounts (Mountain Duck), so
# we never invoke esbuild or Node from the rclone-side ui dir.
set -euo pipefail

SRC_UI="$(cd "$(dirname "$0")/../ui" && pwd)"
SRC_BACKEND="$(cd "$(dirname "$0")/../backend" && pwd)"
DEPS_UI="${OPENMARQUEE_DEPS_DIR:-$HOME/tmp/openmarquee-deps}/ui"

if [ ! -d "$DEPS_UI/node_modules" ]; then
    echo "deps not installed at $DEPS_UI — run scripts/setup.sh first" >&2
    exit 1
fi

if [ "${1:-}" = "install" ]; then
    cd "$DEPS_UI" && npm run e2e:install
    exit 0
fi

cd "$DEPS_UI"

# Build the ui bundle into source dist/ so the backend (OPENMARQUEE_UI_DIR
# = source) serves the freshly-built assets.
# NODE_PATH lets esbuild fall back to the deps dir when it can't resolve a
# bare specifier from the entry file's location (the rclone-side ui dir,
# whose own node_modules symlink is broken).
NODE_PATH="$DEPS_UI/node_modules" \
    node_modules/.bin/esbuild \
    "$SRC_UI/src/main.js" "$SRC_UI/src/welcome.js" "$SRC_UI/src/spike.js" \
    "ffmpeg-worker=$DEPS_UI/node_modules/@ffmpeg/ffmpeg/dist/esm/worker.js" \
    --format=esm --bundle --minify --outdir="$SRC_UI/dist" >/dev/null

# Vendor @ffmpeg/core (~31 MB wasm) — same payload scripts/copy-ffmpeg-core.mjs
# would have produced, just done inline so we don't have to symlink the script
# back into the deps dir.
mkdir -p "$SRC_UI/dist/vendor/ffmpeg-core"
cp -R "$DEPS_UI/node_modules/@ffmpeg/core/dist/esm/." "$SRC_UI/dist/vendor/ffmpeg-core/"

# Mirror e2e/ + a patched playwright.config.js into the deps dir so Node's
# ESM resolver finds @playwright/test natively. The patched config pins
# testDir / webServer.cwd / OPENMARQUEE_UI_DIR back to source paths.
rm -rf "$DEPS_UI/e2e" "$DEPS_UI/playwright.config.js"
cp -R "$SRC_UI/e2e" "$DEPS_UI/e2e"
sed \
    -e "s|testDir: \"./e2e\"|testDir: \"$DEPS_UI/e2e\"|" \
    -e "s|cwd: \"../backend\"|cwd: \"$SRC_BACKEND\"|" \
    -e "s|OPENMARQUEE_UI_DIR: __dirname|OPENMARQUEE_UI_DIR: \"$SRC_UI\"|" \
    "$SRC_UI/playwright.config.js" > "$DEPS_UI/playwright.config.js"

exec node_modules/.bin/playwright test "$@"
