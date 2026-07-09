#!/usr/bin/env bash
# render-pwa-icons.sh — DEPRECATED (2026-07-08).
#
# Superseded by scripts/render-app-icons.py. The icons are now the square
# portrait LED-dot tile with a PER-SIZE column count (4 columns at
# 180/192/512, 2 at favicon sizes so the dots stay distinct) — a single
# scaled SVG through rsvg can't produce that, so a Pillow step generates
# each PNG at its own density. Running the old rsvg-from-monogram.svg path
# would OVERWRITE the adaptive PNGs with non-adaptive ones, so this script
# now just points you at the replacement.

set -euo pipefail
echo "render-pwa-icons.sh is deprecated. Use the Pillow generator instead:" >&2
echo "    python3 ui/scripts/render-app-icons.py" >&2
exit 1
