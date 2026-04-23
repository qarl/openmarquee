#!/usr/bin/env bash
# scripts/test.sh — run the full openMarquee test suite (backend + UI).
# Hardware-tagged tests are skipped by default; pass `-m hardware` to pytest
# directly to run them on a real Pi.
#
# Mirrors source → $OPENMARQUEE_BUILD_DIR first, then runs everything from
# there (see scripts/_lib.sh for the why).
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

sync_to_build_dir
cd "$OPENMARQUEE_BUILD_DIR"

VENV="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"
PYTEST="$VENV/bin/pytest"
if [ ! -x "$PYTEST" ]; then
    PYTEST="pytest"
fi

echo "==> backend tests (pytest)"
(cd backend && "$PYTEST")

echo
echo "==> UI tests (vitest)"
(cd ui && npm test --silent)

echo
echo "all tests passed."
