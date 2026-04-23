# scripts/_lib.sh — shared helpers for the BUILD_DIR mirror workflow.
#
# Source (don't exec) from other scripts:
#     source "$(dirname "$0")/_lib.sh"
#
# Exposes:
#   $OPENMARQUEE_SRC         — absolute path of the repo (where .git lives)
#   $OPENMARQUEE_BUILD_DIR   — local-disk mirror where tools actually run
#   sync_to_build_dir        — rsync source → BUILD_DIR, protecting build
#                              artifacts (node_modules, dist, .egg-info, ...)
#
# Why: the source tree lives on a Mountain Duck mount where symlinks,
# mmap, and tiny-file stat storms are flaky. Running builds/tests out of
# a plain local-disk mirror avoids all of it.
# shellcheck shell=bash

OPENMARQUEE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENMARQUEE_BUILD_DIR="${OPENMARQUEE_BUILD_DIR:-$HOME/tmp/openmarquee-build}"

sync_to_build_dir() {
    mkdir -p "$OPENMARQUEE_BUILD_DIR"
    rsync -a --delete \
        --exclude='.git' \
        --exclude='node_modules' \
        --exclude='dist' \
        --exclude='build' \
        --exclude='__pycache__' \
        --exclude='*.egg-info' \
        --exclude='.vite' \
        --exclude='.ruff_cache' \
        --exclude='.pytest_cache' \
        --exclude='.mypy_cache' \
        --exclude='test-results' \
        --exclude='playwright-report' \
        --exclude='.playwright' \
        --exclude='.Jimmy' \
        --exclude='.DS_Store' \
        --exclude='._*' \
        --exclude='.fuse_hidden*' \
        --exclude='*.partial' \
        --exclude='openmarquee-content' \
        --exclude='openmarquee-*.json' \
        --exclude='*.log' \
        "$OPENMARQUEE_SRC/" "$OPENMARQUEE_BUILD_DIR/"
}
