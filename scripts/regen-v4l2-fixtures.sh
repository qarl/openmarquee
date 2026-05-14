#!/usr/bin/env bash
# Regenerate the H.264 test fixtures used by renderer/src/mp4_demux.rs
# and renderer/src/v4l2.rs. The fixtures are committed to the repo so
# `cargo test` doesn't need ffmpeg at test time; rerun this script if
# they're ever lost or need updating.
#
# Outputs:
#   renderer/tests/fixtures/test_320x240.mp4    -- MP4-wrapped H.264
#   renderer/tests/fixtures/test_320x240.h264   -- raw Annex-B H.264
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
FIX_DIR="$REPO_ROOT/renderer/tests/fixtures"

mkdir -p "$FIX_DIR"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found; install ffmpeg first (brew install ffmpeg / apt install ffmpeg)" >&2
  exit 1
fi

# 2 seconds @ 30fps, baseline-profile H.264, 320x240. Baseline keeps
# the avcC simple (no B-frames, single SPS+PPS).
ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "testsrc=duration=2:size=320x240:rate=30" \
  -c:v libx264 -profile:v baseline -level 3.0 -pix_fmt yuv420p \
  -f mp4 "$FIX_DIR/test_320x240.mp4"

# Re-emit the raw Annex-B stream from the MP4 we just made so they
# stay aligned (the v4l2 piece 2b fixture used the same source).
ffmpeg -y -hide_banner -loglevel error \
  -i "$FIX_DIR/test_320x240.mp4" \
  -c:v copy -bsf:v h264_mp4toannexb \
  -f h264 "$FIX_DIR/test_320x240.h264"

echo "regenerated:"
ls -la "$FIX_DIR"/test_320x240.*
