#!/usr/bin/env bash
# scripts/download-emoji-font-colrv1.sh — fetch + strip Noto Color Emoji
# (COLRv1 variant) at build time.
#
# Bug 3 Slice 3 runtime COLRv1 vector emoji rasterizer (renderer/src/
# glyph_cache_colr.rs) reads this TTF via skrifa's ColorPainter trait +
# tiny-skia. Pinned to a specific google/fonts revision + verified
# against a known sha256 so a hijacked CDN can't swap in a different
# (or weaponized) font during install.
#
# Upstream tarball is 24.3 MB because it bundles an SVG table for browser
# fallback (80% of the file). skrifa reads COLR/CPAL/glyf only, so we
# strip SVG via Python+fontTools post-download, dropping the file to
# 4.8 MB. fontTools is already a Python dev-dep (used elsewhere in
# scripts/). The strip step is bit-stable for a fixed source file.
#
# Idempotent. Run from the repo root, or via scripts/setup.sh which
# calls it.

set -euo pipefail

# Pin the source revision + sha256 here. Bump deliberately when refreshing.
# Source revision is captured from `gh api .../contents/...` at fetch time.
NOTO_EMOJI_COLRV1_URL="https://github.com/google/fonts/raw/main/ofl/notocoloremoji/NotoColorEmoji-Regular.ttf"
NOTO_EMOJI_COLRV1_SRC_SHA256="be73479ba4fa277c89b85cd6c71717df30d9d0eff6da8c1e1a201e5b95459299"

# Resolve repo root from the script's path so this works whether the
# caller is in the repo root, scripts/, or some BUILD_DIR mirror.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$REPO_ROOT/ui/fonts"
DEST_FILE="$DEST_DIR/noto-color-emoji-colrv1.ttf"

mkdir -p "$DEST_DIR"

# Idempotency: if a stripped file already exists at DEST_FILE, trust it
# (matching shape: 4.8 MB, COLR present, SVG absent). We don't pin a
# stripped sha here because the strip step's bit-stability depends on
# fontTools version; the SOURCE sha pin is what protects the supply chain.
if [ -f "$DEST_FILE" ]; then
    size=$(wc -c < "$DEST_FILE" | tr -d ' ')
    if [ "$size" -gt 3000000 ] && [ "$size" -lt 6000000 ]; then
        echo "==> Noto Color Emoji COLRv1 already at $DEST_FILE (size $size; skipping)"
        exit 0
    else
        echo "==> Noto Color Emoji COLRv1 at $DEST_FILE has unexpected size ($size); re-downloading"
        rm -f "$DEST_FILE"
    fi
fi

echo "==> downloading Noto Color Emoji COLRv1 source (~24 MB)"
echo "    from: $NOTO_EMOJI_COLRV1_URL"

# Stage to a .tmp file so an interrupted download doesn't leave a half-
# valid TTF that the next run sees and trusts.
SRC_TMP="${DEST_FILE}.src.tmp"
trap 'rm -f "$SRC_TMP" "${DEST_FILE}.tmp"' EXIT
curl --fail --silent --show-error --location --retry 3 \
    --output "$SRC_TMP" "$NOTO_EMOJI_COLRV1_URL"

actual_sha=$(shasum -a 256 "$SRC_TMP" | awk '{print $1}')
if [ "$actual_sha" != "$NOTO_EMOJI_COLRV1_SRC_SHA256" ]; then
    echo "ERR: Noto Color Emoji COLRv1 source sha mismatch" >&2
    echo "  expected: $NOTO_EMOJI_COLRV1_SRC_SHA256" >&2
    echo "  actual:   $actual_sha" >&2
    echo "  url:      $NOTO_EMOJI_COLRV1_URL" >&2
    echo "  Bump NOTO_EMOJI_COLRV1_SRC_SHA256 in $0 if you intentionally moved the revision." >&2
    exit 1
fi

echo "==> stripping SVG table (saves ~19 MB; swash uses COLR/CPAL/glyf only)"
python3 - "$SRC_TMP" "${DEST_FILE}.tmp" <<'PY'
import sys
from fontTools.ttLib import TTFont
src, dst = sys.argv[1], sys.argv[2]
f = TTFont(src)
if 'SVG ' in f:
    del f['SVG ']
f.save(dst)
PY

mv "${DEST_FILE}.tmp" "$DEST_FILE"
rm -f "$SRC_TMP"
trap - EXIT

stripped_size=$(wc -c < "$DEST_FILE" | tr -d ' ')
echo "==> Noto Color Emoji COLRv1 installed at $DEST_FILE ($stripped_size bytes)"
