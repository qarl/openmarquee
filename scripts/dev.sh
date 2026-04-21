#!/usr/bin/env bash
# scripts/dev.sh — start the OpenMarquee backend and UI in dev mode, together.
#
# Backend: http://localhost:8000 (FastAPI, --reload)
# UI:      esbuild watch builds to ui/dist/; once the backend serves static
#          files (Phase 2+), it will be reachable at the same backend URL.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"
UVICORN="$VENV/bin/uvicorn"
if [ ! -x "$UVICORN" ]; then
    UVICORN="uvicorn"  # fall back to PATH if no openmarquee venv
fi

cleanup() {
    echo
    echo "stopping dev servers..."
    kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "starting backend on http://localhost:8000 (uvicorn --reload)"
(cd backend && "$UVICORN" openmarquee.app:app --reload --port 8000) &

echo "starting UI bundler (esbuild watch)"
(cd ui && npm run dev) &

wait
