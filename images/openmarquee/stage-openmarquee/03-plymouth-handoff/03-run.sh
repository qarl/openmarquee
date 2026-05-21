#!/bin/bash -e
# 03-run.sh — plymouth -> renderer handoff substage.
#
# Runs on the build HOST (pi-gen convention) with ${ROOTFS_DIR}
# pointing at the image rootfs. Installs a systemd drop-in that makes
# the stock plymouth-quit.service quit with `--retain-splash`, so the
# splash framebuffer stays on screen until the renderer paints its
# first frame — no black flash between the splash and the renderer.
# See files/plymouth-quit.service.d/retain-splash.conf for the why.
#
# Baseline handoff per the QA scope: the drop-in only. The optional
# backend-driven `plymouth quit` polish was explicitly deferred.

DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${ROOTFS_DIR}/etc/systemd/system/plymouth-quit.service.d"

install -d -m 755 "$DEST"
install -m 644 \
    "${DIR}/files/plymouth-quit.service.d/retain-splash.conf" \
    "${DEST}/retain-splash.conf"

echo "03-run.sh: installed plymouth-quit --retain-splash drop-in"
