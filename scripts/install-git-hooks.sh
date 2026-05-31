#!/usr/bin/env bash
# scripts/install-git-hooks.sh — opt-in installer for the repo-versioned
# pre-push hook at .githooks/pre-push.
#
# Run once after cloning. Idempotent. Points `git config core.hooksPath`
# at the in-tree .githooks/ directory so the pre-push gate runs on every
# `git push` against this clone.
#
# Why opt-in: git doesn't auto-install repo-versioned hooks (security
# precaution -- a malicious commit could otherwise add a hook that
# runs arbitrary code on the next pull). Contributors run this once,
# acknowledging they've reviewed what the hook does.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [ ! -f .githooks/pre-push ]; then
    echo "error: .githooks/pre-push not found; run from the openmarquee repo root" >&2
    exit 1
fi

git config core.hooksPath .githooks
chmod +x .githooks/pre-push

current_path="$(git config --get core.hooksPath)"

echo "✓ Pre-push hook installed."
echo "  core.hooksPath = $current_path"
echo
echo "What it does (fast subset of CI):"
echo "  - backend: ruff check + ruff format --check + pytest  (~30s)"
echo "  - ui: vitest                                           (~90s)"
echo "  - renderer: cargo test                                 (~5-30s)"
echo
echo "Modified-paths gated: a push touching only backend/ skips the ui"
echo "+ renderer steps (and inverse). Any root-level change runs all"
echo "(defensive default; total ~3 min for full-tree changes)."
echo
echo "To bypass in emergencies: git push --no-verify"
