#!/usr/bin/env bash
# scripts/e2e.sh — run the Playwright end-to-end suite.
#
# Auto-starts uvicorn on port 8765 with isolated content paths in /tmp,
# runs the specs in ui/e2e/, then tears down the backend.
#
# First run: bash scripts/e2e.sh install   (downloads Chromium)
set -euo pipefail

cd "$(dirname "$0")/../ui"

if [ "${1:-}" = "install" ]; then
    npm run e2e:install
    exit 0
fi

# Rebuild the bundle so Playwright tests against the current source, not a
# stale `dist/main.js` from an earlier dev session. Cheap (<1s via esbuild).
npm run build --silent

npm run e2e
