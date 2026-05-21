#!/bin/bash -e
# 01-run.sh — substage runner for the openMarquee Plymouth boot splash.
#
# Runs on the HOST during pi-gen's stage walk (per pi-gen convention):
#   * ${ROOTFS_DIR} points at the mounted image rootfs.
#   * the `on_chroot` helper runs commands INSIDE the image chroot.
# The `plymouth` package itself is laid down by 00-install-packages.
#
# This script installs our custom "openmarquee" theme and makes it the
# system default so it shows during boot. It is safe to re-run: the
# install/copy steps overwrite, and plymouth-set-default-theme is
# idempotent.

# Theme files live alongside this script under files/openmarquee/.
# We ship the .plymouth config, the .script plugin theme, and the two
# rendered PNGs -- but NOT generate_splash.py, which stays in the repo
# as the reproducible artwork source and has no business in the rootfs.
THEME_SRC="${BASH_SOURCE[0]%/*}/files/openmarquee"
THEME_DST="${ROOTFS_DIR}/usr/share/plymouth/themes/openmarquee"

# Lay the theme directory into the image with sane perms:
# directory 755, files 644 (world-readable, not writable).
install -d -m 755 "${THEME_DST}"
install -m 644 "${THEME_SRC}/openmarquee.plymouth" "${THEME_DST}/"
install -m 644 "${THEME_SRC}/openmarquee.script"   "${THEME_DST}/"
install -m 644 "${THEME_SRC}/splash.png"           "${THEME_DST}/"
install -m 644 "${THEME_SRC}/spinner.png"          "${THEME_DST}/"

# Inside the chroot: select our theme as the default and rebuild the
# initramfs (-R) so the splash is available from early boot.
on_chroot << EOF
plymouth-set-default-theme -R openmarquee
EOF
