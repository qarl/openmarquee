#!/usr/bin/env bash
# scripts/e2e.sh — run the Playwright end-to-end suite out of BUILD_DIR.
#
# Auto-starts uvicorn on port 8765 with isolated content paths in /tmp,
# runs the specs in ui/e2e/, then tears down the backend.
#
# First run: bash scripts/e2e.sh install   (downloads Chromium)
#
# Watching the tests run: set PW_SLOWMO=<ms> to pace every Playwright action.
# Example: PW_SLOWMO=200 bash scripts/e2e.sh  (slow enough to follow by eye)
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

sync_to_build_dir
cd "$OPENMARQUEE_BUILD_DIR/ui"

if [ "${1:-}" = "install" ]; then
    npm run e2e:install
    exit 0
fi

# Rebuild the bundle so Playwright tests against the current source, not a
# stale `dist/main.js` from an earlier dev session. Cheap (<1s via esbuild).
npm run build --silent

npm run e2e
