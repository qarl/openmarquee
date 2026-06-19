#!/usr/bin/env bash
# Cross-build wrapper for blendr (macOS host -> aarch64 Linux target).
#
# Per project memory (virtiofs cargo wedge, 2026-05-07): cargo hangs
# in U-state when built directly from /Users/qarl/project on this Mac.
# Workaround:
#   1. rsync the crate to /tmp/blendr-build (local APFS, no virtiofs)
#   2. cargo zigbuild --release --target aarch64-unknown-linux-gnu.2.36
#   3. Print the final binary path so the caller can scp it.
#
# Pre-reqs on the host (one-time):
#   - rustup target add aarch64-unknown-linux-gnu
#   - cargo install cargo-zigbuild
#   - brew install zig
#
# Bookworm (Pi OS) ships glibc 2.36; targeting .2.36 ensures the
# binary loads on FYS without manual sysroot.
#
# Usage:
#   fresh/blendr/build.sh [--debug]
#
# Env override:
#   BLENDR_BUILD_DIR=/some/dir   (default /tmp/blendr-build)

set -euo pipefail

PROFILE="release"
if [ "${1:-}" = "--debug" ]; then
    PROFILE="debug"
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${BLENDR_BUILD_DIR:-/tmp/blendr-build}"
SYSROOT="${BLENDR_PI_SYSROOT:-$HOME/pi-sysroot}"

if [ ! -d "$SYSROOT/usr/lib/aarch64-linux-gnu" ]; then
    echo "[blendr-build] FAIL: pi sysroot not found at $SYSROOT" >&2
    echo "  Need libgbm / libdrm / libEGL / libGLESv2 mirrored from a Pi." >&2
    echo "  See scripts/renderer_cross_build.sh for the proven setup." >&2
    exit 1
fi

mkdir -p "$BUILD_DIR"
echo "[blendr-build] src=$SRC  build=$BUILD_DIR  profile=$PROFILE"
echo "[blendr-build] sysroot=$SYSROOT"
rsync -a --delete --exclude target --exclude .git \
    "$SRC"/ "$BUILD_DIR/blendr/"
cd "$BUILD_DIR/blendr"

# Point zigbuild at the Pi sysroot for libgbm.so, libdrm.so.2,
# libEGL.so.1, libGLESv2.so.2 at link time. RUSTFLAGS -L tells
# the linker where to find the .so files (we link against
# Bookworm's exact libs to match runtime ABI). Mirrors the OLD
# renderer's scripts/renderer_cross_build.sh setup.
export PKG_CONFIG_PATH_aarch64_unknown_linux_gnu="$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig"
# LIBDIR overrides the system-default search path entirely.
# Without it, pkg-config falls back to /opt/homebrew/*/pkgconfig
# on macOS and pulls in the host's glib (with -lintl from
# gettext) into the link line -- libintl.so does not exist on
# Debian aarch64 (glibc rolls gettext symbols into libc.so.6)
# so the link fails. Pinning LIBDIR makes pkg-config see ONLY
# the sysroot's .pc files.
export PKG_CONFIG_LIBDIR_aarch64_unknown_linux_gnu="$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig:$SYSROOT/usr/share/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR_aarch64_unknown_linux_gnu="$SYSROOT"
export PKG_CONFIG_ALLOW_CROSS=1
export RUSTFLAGS="-L $SYSROOT/usr/lib/aarch64-linux-gnu -L $SYSROOT/lib/aarch64-linux-gnu"

CARGO_FLAGS="--target aarch64-unknown-linux-gnu.2.36"
[ "$PROFILE" = "release" ] && CARGO_FLAGS="--release $CARGO_FLAGS"

cargo zigbuild $CARGO_FLAGS

BIN="$BUILD_DIR/blendr/target/aarch64-unknown-linux-gnu/$PROFILE/blendr"
if [ ! -f "$BIN" ]; then
    echo "[blendr-build] ERROR: binary not at $BIN"
    exit 1
fi

MD5="$(md5 -q "$BIN" 2>/dev/null || md5sum "$BIN" | awk '{print $1}')"
SIZE="$(stat -f '%z' "$BIN" 2>/dev/null || stat -c '%s' "$BIN")"
echo "[blendr-build] PASS: $BIN"
echo "[blendr-build]   md5=$MD5  size=$SIZE bytes"
