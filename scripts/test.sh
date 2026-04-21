#!/usr/bin/env bash
# scripts/test.sh — run the full openMarquee test suite (backend + UI).
# Hardware-tagged tests are skipped by default; pass `-m hardware` to pytest
# directly to run them on a real Pi.
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"
PYTEST="$VENV/bin/pytest"
if [ ! -x "$PYTEST" ]; then
    PYTEST="pytest"  # fall back to PATH if no openmarquee venv
fi

echo "==> backend tests (pytest)"
(cd backend && "$PYTEST")

echo
echo "==> UI tests (vitest)"
(cd ui && npm test --silent)

echo
echo "all tests passed."
