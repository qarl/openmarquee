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

echo "==> backend lint (ruff)"
# 16.1 / sweep #8 B1: keep test.sh and CI in lockstep -- CI runs
# `ruff check` + `ruff format --check`, so devs running test.sh hit
# the same gate before push. Skip if ruff isn't installed locally
# (CI always has it via the [dev] extras install).
RUFF="$VENV/bin/ruff"
if [ ! -x "$RUFF" ] && ! command -v ruff > /dev/null; then
    echo "    (skipped: ruff not installed; run \`pip install -e backend[dev]\`)"
else
    if [ ! -x "$RUFF" ]; then
        RUFF="ruff"
    fi
    (cd backend && "$RUFF" check . && "$RUFF" format --check .)
fi

echo
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
echo "==> backend lock drift (pyproject.toml vs requirements.lock)"
# 16.1 / sweep #8 B8: ensure the committed lock matches what
# pip-compile would produce from the current pyproject.toml. Catches
# the "edited pyproject but forgot to re-lock" footgun before it
# ships -- a stale lock is what bit us in Batch 11.1's first attempt
# (Pillow CVEs unlocked because the dep bump never reached the lock).
# Skip if pip-compile isn't installed locally.
PIP_COMPILE="$VENV/bin/pip-compile"
if [ ! -x "$PIP_COMPILE" ] && ! command -v pip-compile > /dev/null; then
    echo "    (skipped: pip-tools not installed; run \`pip install -e backend[dev]\`)"
else
    if [ ! -x "$PIP_COMPILE" ]; then
        PIP_COMPILE="pip-compile"
    fi
    (cd backend && \
        "$PIP_COMPILE" --quiet --strip-extras pyproject.toml -o - 2>/dev/null \
        | diff -u requirements.lock - \
        || { echo "    lock drift -- regen with:"; \
             echo "    cd backend && pip-compile --strip-extras pyproject.toml -o - > requirements.lock"; \
             exit 1; })
fi

echo
echo "all tests passed."
