#!/usr/bin/env bash
# scripts/dev.sh — start the openMarquee backend and UI in dev mode, together.
#
# Backend: http://localhost:8000 (FastAPI, --reload)
# UI:      esbuild watch builds to ui/dist/; the backend serves it.
#
# Runs out of $OPENMARQUEE_BUILD_DIR and keeps it in sync with source via
# fswatch (install with `brew install fswatch`) — or, if fswatch isn't
# available, a 2-second rsync poll loop. Edits in source propagate to
# BUILD_DIR so uvicorn's --reload + esbuild's --watch pick them up.
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

# Pull in the developer's personal secrets file, if present, BEFORE
# uvicorn starts — gives features like /api/backgrounds/generate access
# to OPENAI_API_KEY without having to export it in every shell. The
# file lives outside the project tree (default: ~/Jimmy/.env) so it
# never gets committed; override via OPENMARQUEE_DEV_ENV_FILE.
DEV_ENV_FILE="${OPENMARQUEE_DEV_ENV_FILE:-$HOME/Jimmy/.env}"
if [ -f "$DEV_ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$DEV_ENV_FILE"
    set +a
    echo "sourced $DEV_ENV_FILE (OPENAI_API_KEY: ${OPENAI_API_KEY:+set})"
fi

sync_to_build_dir
cd "$OPENMARQUEE_BUILD_DIR"

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

# Keep BUILD_DIR in sync with source while dev servers run. fswatch is
# event-driven; the fallback poll is fine but introduces a ~2s lag.
if command -v fswatch >/dev/null 2>&1; then
    echo "starting fswatch → rsync loop (event-driven)"
    (fswatch -o "$OPENMARQUEE_SRC" | while read -r _; do sync_to_build_dir >/dev/null 2>&1; done) &
else
    echo "fswatch not found (brew install fswatch for event-driven sync) — polling every 2s"
    (while true; do sleep 2; sync_to_build_dir >/dev/null 2>&1; done) &
fi

echo "starting backend on http://localhost:8000 (uvicorn --reload)"
(cd backend && "$UVICORN" openmarquee.app:app --reload --port 8000) &

echo "starting UI bundler (esbuild watch)"
(cd ui && npm run dev) &

wait
