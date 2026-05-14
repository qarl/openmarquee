#!/usr/bin/env bash
# Cross-build wrapper for the Rust renderer.
#
# The virtiofs cargo wedge (project memory 2026-05-07) means cargo
# hangs in U-state when built directly from /Users/qarl/project on
# this Mac. The workaround:
#   1. rsync renderer/ to /tmp/renderer-build (local APFS)
#   2. cargo zigbuild there (with sysroot env vars)
#   3. cp the resulting aarch64 binary back to the virtiofs target
#      path so renderer_pi_smoke.sh + renderer_pi_soak.sh find it
#
# Doing those by hand every iteration burns 30 seconds and is easy
# to forget the cp-back step. This wrapper bakes them all in.
#
# Usage:
#   scripts/renderer_cross_build.sh [--debug]
#
# Options:
#   --debug    Build the dev profile instead of release. Default
#              release (optimizes; what production runs).
#
# Env requirements (resolved automatically):
#   - cargo / cargo-zigbuild on $PATH (or under $HOME/.cargo/bin)
#   - $HOME/pi-sysroot/ with libdrm-dev / libgbm / libEGL / libGLES
#     mirrored from a Pi (per phase-1 commit d8fa439)
#
# Output:
#   - $REPO/renderer/target/aarch64-unknown-linux-gnu/release|debug/
#     openmarquee-render (the binary the smoke script deploys)

set -euo pipefail

PROFILE="release"
if [ "${1:-}" = "--debug" ]; then
    PROFILE="debug"
fi
PROFILE_DIR="$PROFILE"
[ "$PROFILE" = "debug" ] && PROFILE_DIR="debug" || PROFILE_DIR="release"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="/tmp/renderer-build"
SYSROOT="$HOME/pi-sysroot"
TARGET="aarch64-unknown-linux-gnu"
BIN_NAME="openmarquee-render"
SRC_BIN="$BUILD_DIR/target/$TARGET/$PROFILE_DIR/$BIN_NAME"
DST_BIN="$REPO/renderer/target/$TARGET/$PROFILE_DIR/$BIN_NAME"

if [ ! -d "$SYSROOT/usr/lib/aarch64-linux-gnu" ]; then
    echo "FAIL: pi sysroot not found at $SYSROOT — see d8fa439 commit msg" >&2
    exit 1
fi

export PATH="$HOME/.cargo/bin:$PATH"
if ! command -v cargo >/dev/null 2>&1; then
    echo "FAIL: cargo not on PATH (checked \$HOME/.cargo/bin)" >&2
    exit 1
fi

# Sysroot env: matches phase-1 build path (d8fa439).
export PKG_CONFIG_PATH_aarch64_unknown_linux_gnu="$SYSROOT/usr/lib/aarch64-linux-gnu/pkgconfig"
export PKG_CONFIG_SYSROOT_DIR_aarch64_unknown_linux_gnu="$SYSROOT"
export PKG_CONFIG_ALLOW_CROSS=1
export BINDGEN_EXTRA_CLANG_ARGS_aarch64_unknown_linux_gnu="--sysroot=$SYSROOT -I$SYSROOT/usr/include -I$SYSROOT/usr/include/aarch64-linux-gnu"
export RUSTFLAGS="-L $SYSROOT/usr/lib/aarch64-linux-gnu -L $SYSROOT/lib/aarch64-linux-gnu"

echo "==> rsync renderer/ -> $BUILD_DIR"
mkdir -p "$BUILD_DIR"
rsync -a --delete --exclude target "$REPO/renderer/" "$BUILD_DIR/"

echo "==> cargo zigbuild --target $TARGET --$PROFILE"
BUILD_FLAGS=""
[ "$PROFILE" = "release" ] && BUILD_FLAGS="--release"
cd "$BUILD_DIR"
cargo zigbuild $BUILD_FLAGS --target "$TARGET"

if [ ! -x "$SRC_BIN" ]; then
    echo "FAIL: build succeeded but binary not at $SRC_BIN" >&2
    exit 1
fi

echo "==> cp binary back to virtiofs target dir"
mkdir -p "$(dirname "$DST_BIN")"
cp "$SRC_BIN" "$DST_BIN"
ls -la "$DST_BIN"

echo
echo "PASS: cross-build green. Binary at:"
echo "  $DST_BIN"
echo
echo "Deploy via:"
echo "  scripts/renderer_pi_smoke.sh openmarquee@openMarqueeDev"
echo "  scripts/renderer_pi_soak.sh  openmarquee@openMarqueeDev"
