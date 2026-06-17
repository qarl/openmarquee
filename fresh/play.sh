#!/bin/bash
# Smallest fresh renderer: H264 HW-decode -> KMS, looped over a sequence.
# Stack: gstreamer (v4l2h264dec + kmssink). Zero shared code with
# openmarquee-render. Step 2a of fresh-stack rebuild (2026-06-17).
#
# Plays A -> B -> A -> B ... forever. Cold pipeline restart at each seam
# (visible gap/black) is expected + accepted for step 2a; smoothing the
# seam comes later in the transition step.
#
# No rotation: CPU videoflip crushed 1080p to 2-4fps on Pi Zero 2 W.
# Native/landscape only for now. Portrait will return as a proper
# feature: HW-plane rotation via kmssink DRM plane property, honoring
# settings.json display_rotation. Not in this step.
#
# Usage:
#   sudo systemctl stop openmarquee-render   # free HW decoder + DRM master
#   ./play.sh

set -eu

VIDEOS=(
  "/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4"
  "/var/openmarquee/content/3f54a4d2-a120-4c0c-aa80-5b99aaf7c9ff/asset.mp4"
)

die() { echo "[fresh] $*" >&2; exit 1; }

for v in "${VIDEOS[@]}"; do
  test -f "$v" || die "missing video: $v"
done
command -v gst-launch-1.0 >/dev/null \
  || die "install: sudo apt install -y gstreamer1.0-tools gstreamer1.0-plugins-{good,bad}"
gst-inspect-1.0 v4l2h264dec >/dev/null 2>&1 \
  || die "install: sudo apt install -y gstreamer1.0-plugins-bad (need v4l2h264dec)"
gst-inspect-1.0 kmssink >/dev/null 2>&1 \
  || die "install: sudo apt install -y gstreamer1.0-plugins-bad (need kmssink)"

# Single HW decoder + single DRM master — refuse to double-start.
if pgrep -af 'gst-launch.*v4l2h264dec' | grep -v $$ >/dev/null; then
  die "another gst-launch HW-decode pipeline is already running (pkill it first)"
fi

echo "[fresh] looping ${#VIDEOS[@]} videos in sequence (ctrl-c to stop)"
while true; do
  for VIDEO in "${VIDEOS[@]}"; do
    gst-launch-1.0 -q \
      filesrc location="$VIDEO" ! \
      qtdemux name=d d. ! queue ! \
      h264parse ! v4l2h264dec ! \
      kmssink sync=true \
      || { echo "[fresh] pipeline exited non-zero on $VIDEO; sleeping 1s"; sleep 1; }
  done
done
