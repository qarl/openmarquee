#!/usr/bin/env bash
# scripts/setup.sh — set up the local dev environment for openMarquee.
#
# Idempotent. Re-run after changes to backend/pyproject.toml or ui/package.json.
#
# What it does:
#   - Creates a Python venv and installs the backend in editable mode
#     with dev extras (pytest, ruff, etc.).
#   - Installs UI Node deps outside the project tree so binaries keep
#     their POSIX exec bits (the rclone mount this project lives on
#     strips them). Symlinks node_modules back into ui/.
#
# Override paths if you need to:
#   OPENMARQUEE_VENV       (default: ~/tmp/venv/openmarquee)
#   OPENMARQUEE_DEPS_DIR   (default: ~/tmp/openmarquee-deps)
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"
DEPS_DIR="${OPENMARQUEE_DEPS_DIR:-$HOME/tmp/openmarquee-deps}"
UI_DEPS="$DEPS_DIR/ui"

# --- Python backend ---

if [ ! -x "$VENV/bin/python" ]; then
    echo "==> creating Python venv at $VENV"
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
fi

echo "==> installing backend with dev extras"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$PROJECT_ROOT/backend[dev]"

# --- UI ---

echo "==> preparing UI deps at $UI_DEPS"
mkdir -p "$UI_DEPS"
# `cat` instead of `cp` because the rclone mount rejects xattr copies.
cat "$PROJECT_ROOT/ui/package.json" > "$UI_DEPS/package.json"
if [ -f "$PROJECT_ROOT/ui/package-lock.json" ]; then
    cat "$PROJECT_ROOT/ui/package-lock.json" > "$UI_DEPS/package-lock.json"
fi

cd "$UI_DEPS"
if [ -f package-lock.json ]; then
    npm ci --silent
else
    npm install --silent
fi

# Always sync the lock back — npm install creates one, npm ci normally
# doesn't change it, but covering both ensures any lock change ends up
# in git.
cat "$UI_DEPS/package-lock.json" > "$PROJECT_ROOT/ui/package-lock.json"

# (Re)link node_modules into the project so vitest/esbuild find it via
# normal Node module resolution. Use a *relative* target — Mountain Duck
# silently rewrites symlinks with absolute targets into a broken relative
# form, but a target you write as relative passes through unchanged
# (well, with a no-op prefix MD adds, which still resolves).
rm -rf "$PROJECT_ROOT/ui/node_modules"
REL_TARGET="$(python3 -c "import os; print(os.path.relpath('$UI_DEPS/node_modules', '$PROJECT_ROOT/ui'))")"
ln -s "$REL_TARGET" "$PROJECT_ROOT/ui/node_modules"

cat <<EOF

ready.

  python venv:  $VENV
  node deps:    $UI_DEPS

run tests:        bash $PROJECT_ROOT/scripts/test.sh
start dev server: bash $PROJECT_ROOT/scripts/dev.sh
EOF
