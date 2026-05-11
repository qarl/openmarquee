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
echo "==> backend CVE audit (pip-audit vs requirements.lock)"
# Batch 11.1 / sweep #5: fail the suite on known CVEs in the
# locked deps. Lock is committed; regen via
# `pip-compile --strip-extras pyproject.toml -o - > requirements.lock`
# after intentional dep bumps. Skip if pip-audit isn't installed
# locally (devs running test.sh on a barebones env still get
# pytest + vitest; CI / pre-deploy hits the gate).
PIP_AUDIT="$VENV/bin/pip-audit"
if [ ! -x "$PIP_AUDIT" ] && ! command -v pip-audit > /dev/null; then
    echo "    (skipped: pip-audit not installed; run \`pip install -e backend[dev]\`)"
else
    if [ ! -x "$PIP_AUDIT" ]; then
        PIP_AUDIT="pip-audit"
    fi
    (cd backend && "$PIP_AUDIT" -r requirements.lock)
fi

echo
echo "all tests passed."
