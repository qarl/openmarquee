#!/bin/bash
# Smallest fresh renderer: H264 HW-decode -> KMS, looped.
# Stack: gstreamer (v4l2h264dec + kmssink). Zero shared code with
# openmarquee-render. Step 1 of fresh-stack rebuild (2026-06-17).
#
# No rotation: CPU videoflip crushed 1080p to 2-4fps on Pi Zero 2 W.
# Native/landscape only for now. Portrait will return as a proper
# feature: HW-plane rotation via kmssink DRM plane property, honoring
# settings.json display_rotation. Not in this step.
#
# Usage:
#   sudo systemctl stop openmarquee-render   # free the HW decoder + DRM master
#   ./play.sh                                # default video (asset A)
#   ./play.sh /path/to/other.mp4             # any H264 mp4

set -eu
VIDEO="${1:-/var/openmarquee/content/029c4d68-744c-4d30-9adc-0f37c55514f1/asset.mp4}"

die() { echo "[fresh] $*" >&2; exit 1; }

test -f "$VIDEO" || die "missing video: $VIDEO"
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

echo "[fresh] looping $VIDEO (ctrl-c to stop)"
while true; do
  gst-launch-1.0 -q \
    filesrc location="$VIDEO" ! \
    qtdemux name=d d. ! queue ! \
    h264parse ! v4l2h264dec ! \
    kmssink sync=true \
    || { echo "[fresh] pipeline exited non-zero; sleeping 1s + retrying"; sleep 1; }
done
