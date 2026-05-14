#!/usr/bin/env bash
# scripts/download-demo-video.sh — fetch the CC-BY demo clip that first-boot
# seed optionally registers as a VideoSlide.
#
# Source: Blender Foundation's "Big Buck Bunny" trailer (CC-BY-3.0). Hosted
# on peach.blender.org. Replace the URL / filename with any short, legally
# redistributable H.264 MP4 the operator wants shipped.
#
# Target: backend/openmarquee/seed_assets/demo.mp4 (where the Python
# package's default dependency provider looks for it). Alternative: set
# OPENMARQUEE_DEMO_VIDEO_PATH to point the seed at a different location.
#
# Safe to re-run — exits quickly if the file is already in place.

set -euo pipefail

cd "$(dirname "$0")/.."

DEST_DIR="backend/openmarquee/seed_assets"
DEST="$DEST_DIR/demo.mp4"

# Big Buck Bunny trailer, 640x360 H.264 MP4, ~5 MB, CC-BY-3.0 (Blender
# Foundation, licensed via https://peach.blender.org/about/).
URL="https://download.blender.org/peach/trailer/trailer_480p.mov"

if [ -f "$DEST" ]; then
    echo "demo video already present at $DEST ($(wc -c < "$DEST") bytes)"
    exit 0
fi

mkdir -p "$DEST_DIR"

echo "downloading demo video from $URL ..."
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$DEST"
elif command -v wget >/dev/null 2>&1; then
    wget -q "$URL" -O "$DEST"
else
    echo "neither curl nor wget installed — can't fetch the clip"
    exit 1
fi

echo "demo video ready at $DEST ($(wc -c < "$DEST") bytes)"
echo "first boot will register it as a VideoSlide alongside the gradient backgrounds."
