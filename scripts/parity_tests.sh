#!/usr/bin/env bash
# Cross-renderer parity test entrypoint.
#
# Captures the browser preview side via Playwright + diffs against
# the checked-in Rust goldens (renderer/tests/golden/*.png). See
# qa/cross-renderer-parity-design.md for the design + threshold
# rationale.
#
# Usage:
#   scripts/parity_tests.sh            # capture + diff, report PASS/FAIL
#   scripts/parity_tests.sh --bless    # save browser captures as baseline
#
# Dependencies (one-time setup):
#   pip3 install Pillow scikit-image playwright
#   playwright install chromium

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${OPENMARQUEE_PYTHON:-python3}"

# Resolve a venv'd python if present so the harness picks up the
# project's pinned Pillow / scikit-image / playwright. Mirrors the
# `OPENMARQUEE_VENV` convention from playwright.config.js + test.sh.
VENV_BASE="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"
VENV_PY="$VENV_BASE/bin/python3"
if [ -x "$VENV_PY" ]; then
    PYTHON="$VENV_PY"
fi

exec "$PYTHON" "$REPO/scripts/parity/run.py" "$@"
