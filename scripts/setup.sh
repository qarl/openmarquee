#!/usr/bin/env bash
# scripts/setup.sh — set up the local dev environment for openMarquee.
#
# Idempotent. Re-run after changes to backend/pyproject.toml or ui/package.json.
#
# What it does:
#   - Mirrors the source tree into $OPENMARQUEE_BUILD_DIR (see _lib.sh for why).
#   - Creates a Python venv, installs the backend in editable mode against
#     BUILD_DIR/backend with dev extras.
#   - Installs UI Node deps via npm ci into BUILD_DIR/ui/node_modules.
#
# Override paths if you need to:
#   OPENMARQUEE_BUILD_DIR  (default: ~/tmp/openmarquee-build)
#   OPENMARQUEE_VENV       (default: ~/tmp/venv/openmarquee)
set -euo pipefail

source "$(dirname "$0")/_lib.sh"

VENV="${OPENMARQUEE_VENV:-$HOME/tmp/venv/openmarquee}"

echo "==> mirroring source to $OPENMARQUEE_BUILD_DIR"
sync_to_build_dir

cd "$OPENMARQUEE_BUILD_DIR"

# --- Python backend ---

if [ ! -x "$VENV/bin/python" ]; then
    echo "==> creating Python venv at $VENV"
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
fi

echo "==> installing backend with dev extras"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$OPENMARQUEE_BUILD_DIR/backend[dev]"

# --- UI ---

echo "==> installing UI deps"
cd "$OPENMARQUEE_BUILD_DIR/ui"
if [ -f package-lock.json ]; then
    npm ci --silent
else
    npm install --silent
fi

# If npm ci / npm install updated the lock, copy it back to source so git
# tracks the change. Normally a no-op.
if ! cmp -s "$OPENMARQUEE_BUILD_DIR/ui/package-lock.json" "$OPENMARQUEE_SRC/ui/package-lock.json"; then
    cp "$OPENMARQUEE_BUILD_DIR/ui/package-lock.json" "$OPENMARQUEE_SRC/ui/package-lock.json"
    echo "==> package-lock.json updated; synced back to source"
fi

# --- Noto Color Emoji (build-time download) ---

# Bundled color-emoji TTF used by the device renderer (Pillow side,
# backend/openmarquee/seed.py:_load_emoji_font) AND the editor preview
# (@font-face declaration in ui/styles.css), so emoji codepoints render
# identically in editor and on glass. The TTF is ~10 MB — too heavy
# for git — so we fetch it at setup time. Pinned + sha-verified inside
# the download script. Idempotent: re-running setup.sh is cheap.
echo "==> ensuring Noto Color Emoji is installed"
bash "$OPENMARQUEE_SRC/scripts/download-emoji-font.sh"
mkdir -p "$OPENMARQUEE_BUILD_DIR/ui/fonts"
# Atomic copy via .tmp + mv so a partial write on a network-mounted
# BUILD_DIR (NFS / SMB / rclone-fuse) doesn't leave a truncated TTF
# that the dev server would serve as garbage. Plain `cp` on the
# default local BUILD_DIR is fine, but the .tmp/mv cost is negligible
# and covers the edge case.
cp "$OPENMARQUEE_SRC/ui/fonts/noto-color-emoji.ttf" \
    "$OPENMARQUEE_BUILD_DIR/ui/fonts/noto-color-emoji.ttf.tmp"
mv "$OPENMARQUEE_BUILD_DIR/ui/fonts/noto-color-emoji.ttf.tmp" \
    "$OPENMARQUEE_BUILD_DIR/ui/fonts/noto-color-emoji.ttf"

cat <<EOF

ready.

  source:       $OPENMARQUEE_SRC
  build dir:    $OPENMARQUEE_BUILD_DIR
  python venv:  $VENV

run tests:        bash $OPENMARQUEE_SRC/scripts/test.sh
start dev server: bash $OPENMARQUEE_SRC/scripts/dev.sh
EOF
