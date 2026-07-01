#!/usr/bin/env bash
# render-pwa-icons.sh — regenerate PWA PNG icons from
# ui/icons/monogram.svg.
#
# Run manually when the monogram design changes. The generated PNGs
# are checked in (committed alongside the SVG) so a fresh clone
# doesn't need rsvg-convert / ImageMagick to serve the icons.
#
# Requires: rsvg-convert (Homebrew: `brew install librsvg`) or an
# equivalent SVG-to-PNG renderer.

set -euo pipefail

SVG_SRC="ui/icons/monogram.svg"
ICONS_DIR="ui/icons"

if ! command -v rsvg-convert >/dev/null 2>&1; then
    echo "error: rsvg-convert not found. Install librsvg (Homebrew: brew install librsvg)" >&2
    exit 1
fi

if [ ! -f "$SVG_SRC" ]; then
    echo "error: $SVG_SRC not found (run from repo root)" >&2
    exit 1
fi

echo "render-pwa-icons: source=$SVG_SRC"
rsvg-convert -w 180 -h 180 "$SVG_SRC" -o "${ICONS_DIR}/apple-touch-icon.png"
echo "  -> ${ICONS_DIR}/apple-touch-icon.png (180x180)"
rsvg-convert -w 192 -h 192 "$SVG_SRC" -o "${ICONS_DIR}/icon-192.png"
echo "  -> ${ICONS_DIR}/icon-192.png (192x192)"
rsvg-convert -w 512 -h 512 "$SVG_SRC" -o "${ICONS_DIR}/icon-512-maskable.png"
echo "  -> ${ICONS_DIR}/icon-512-maskable.png (512x512 maskable)"
rsvg-convert -w 32 -h 32 "$SVG_SRC" -o "${ICONS_DIR}/favicon.png"
echo "  -> ${ICONS_DIR}/favicon.png (32x32)"
echo "render-pwa-icons: done"
