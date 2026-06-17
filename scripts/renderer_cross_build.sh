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
# 2026-06-15: per-worktree BUILD_DIR. Pre-fix this was hardcoded to
# /tmp/renderer-build, which two parallel worktrees (code + code2)
# would clobber each other's cargo incremental state in. Visible
# symptom: a build from worktree B would re-cp the LAST binary cargo
# linked in worktree A even though cargo "succeeded" — because rsync
# --exclude target preserved A's compiled artifacts and cargo
# decided nothing in the freshly-rsync'd source needed recompiling
# against those artifacts. QA caught it via byte-identical md5 of
# two unrelated commits' binaries.
#
# Fix: scope BUILD_DIR to the calling worktree's basename so each
# worktree has its own cargo state. `${BUILD_DIR:-...}` keeps the
# var operator-overridable for the rare case someone wants to pin
# a shared dir on purpose.
BUILD_DIR="${BUILD_DIR:-/tmp/renderer-build-$(basename "$REPO")}"
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

# SDF arc slice A: build.rs walks ../ui/fonts/*.ttf relative to the
# renderer crate root. The cross-build BUILD_DIR is /tmp/renderer-
# build-<worktree> (per-worktree post-2026-06-15) so we also need
# $(dirname BUILD_DIR)/ui/fonts to exist as a sibling of BUILD_DIR
# (same layout as the repo). Without this the atlas bake fails
# with cryptic include_bytes! errors at link time.
# NOTE: the rsync target $(dirname BUILD_DIR)/ui = /tmp/ui is STILL
# shared across worktrees. Fonts are identical across worktrees so
# the only hazard is a race on the rsync if two cross-builds run
# simultaneously. Acceptable today; revisit if parallel builds
# become routine.
echo "==> rsync ui/fonts -> $(dirname "$BUILD_DIR")/ui/fonts"
mkdir -p "$(dirname "$BUILD_DIR")/ui"
rsync -a --delete "$REPO/ui/fonts/" "$(dirname "$BUILD_DIR")/ui/fonts/"

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

# 2026-06-17 QA M0 dispatch: copy the optional m0-park-resume-probe
# binary out alongside openmarquee-render. Conditional so older
# trees without the M0 bin still cross-build cleanly.
M0_SRC_BIN="$BUILD_DIR/target/$TARGET/$PROFILE_DIR/m0-park-resume-probe"
M0_DST_BIN="$REPO/renderer/target/$TARGET/$PROFILE_DIR/m0-park-resume-probe"
if [ -x "$M0_SRC_BIN" ]; then
    cp "$M0_SRC_BIN" "$M0_DST_BIN"
    ls -la "$M0_DST_BIN"
fi

echo
echo "PASS: cross-build green. Binary at:"
echo "  $DST_BIN"
echo
echo "Deploy via:"
echo "  scripts/renderer_pi_smoke.sh openmarquee@openMarqueeDev"
echo "  scripts/renderer_pi_soak.sh  openmarquee@openMarqueeDev"
