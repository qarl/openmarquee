#!/usr/bin/env bash
# scripts/deploy.sh — push local code to a Raspberry Pi running openMarquee.
#
# Usage:
#     bash scripts/deploy.sh <ssh-target>
#     bash scripts/deploy.sh pi@openmarquee.local
#     bash scripts/deploy.sh pi@192.168.1.42
#
# What it does:
#   1. Mirrors source → $OPENMARQUEE_BUILD_DIR (fast, incremental).
#   2. Rebuilds the UI bundle in BUILD_DIR (esbuild; ~1s) so the Pi runs
#      current JS, not a stale dist/.
#   3. Rsyncs BUILD_DIR/backend/ (excluding tests, __pycache__, caches) to
#      /opt/openmarquee/backend/ on the target.
#   4. Rsyncs the UI static files (index.html, welcome.html, styles.css,
#      dist/) to /opt/openmarquee/ui/. Source .js, tests, and node_modules
#      are excluded — the device serves the built bundle only.
#   5. Installs or updates the backend's Python deps into
#      /opt/openmarquee/venv/ via pip -e .
#   6. Restarts the openmarquee-backend systemd unit.
#
# Assumes:
#   - The target has /opt/openmarquee/ writable by the ssh user.
#   - The target has a Python 3.11+ venv at /opt/openmarquee/venv.
#   - systemctl is available and an `openmarquee-backend` service is
#     installed (see system/README.md + system/openmarquee-backend.service).
#
# Not handled here: first-time provisioning (OS image flash, hostapd /
# dnsmasq configs, systemd unit install, service user creation). That's a
# one-off and lives in system/README.md; Phase 9's pi-gen recipe automates
# it into an SD card image.
set -euo pipefail

# r31 (2026-05-31): suppress macOS AppleDouble sidecars in the
# wheels cache that the new pip-download step writes to
# $OPENMARQUEE_BUILD_DIR/wheels/. Mirrors build_sd_bundle.sh:47
# which sets the same flag before its wheel download for the same
# reason (boot-5 forensics 2026-05-21 found 41 ._* sidecars
# accreting in the SD bundle's wheels dir). The rsync to remote
# already --excludes ._* so they never reach the Pi; this just
# keeps the local cache clean.
export COPYFILE_DISABLE=1

source "$(dirname "$0")/_lib.sh"

if [ $# -ne 1 ]; then
    echo "usage: $0 <ssh-target>" >&2
    echo "example: $0 pi@openmarquee.local" >&2
    exit 1
fi

TARGET="$1"
REMOTE_ROOT="${OPENMARQUEE_REMOTE_ROOT:-/opt/openmarquee}"

sync_to_build_dir

echo "==> rebuilding UI bundle in $OPENMARQUEE_BUILD_DIR/ui"
(cd "$OPENMARQUEE_BUILD_DIR/ui" && npm run build --silent)

echo "==> rsync backend to $TARGET:$REMOTE_ROOT/backend/"
# Pre-clean any __pycache__ + *.egg-info that the running backend (which
# writes .pyc as root) and pip install -e . (which creates .egg-info as
# root via install.sh's sudo run) left behind. Without this, --delete-
# excluded in the rsync below tries to delete root-owned files as the
# openmarquee user and exits 23 ("partial transfer"). The 2>/dev/null
# || true guards against the pre-clean failing on a fresh Pi where
# none of these paths exist yet.
ssh "$TARGET" "sudo find $REMOTE_ROOT/backend -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; sudo find $REMOTE_ROOT/backend -name '*.egg-info' -type d -exec rm -rf {} + 2>/dev/null; true"
rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '*.egg-info' \
    --exclude 'tests/' \
    --exclude '._*' \
    "$OPENMARQUEE_BUILD_DIR/backend/" "$TARGET:$REMOTE_ROOT/backend/"

# Phase 7 slice 3 (2026-05-13): rsync the cross-built Rust IPC sidecar
# binary if it exists. The cross-build is heavyweight (cargo-zigbuild +
# sysroot per scripts/renderer_cross_build.sh) so we don't kick it off
# implicitly here -- operators run renderer_cross_build.sh ahead of
# deploy when they want the sidecar opt-in active. A missing binary
# just means install.sh will skip the binary install step (sidecar is
# opt-in via OPENMARQUEE_RENDERER=rust-sidecar).
#
# PERF-NIGHT R1 LESSON (2026-05-26): the .githooks/pre-push hook's
# aarch64 cross-COMPILE gate is a regression check, NOT a build step.
# It does cargo zigbuild but the link stage fails on Mac (no
# libgbm.so) so it never produces a deployable binary. After ANY
# renderer/ source change, run scripts/renderer_cross_build.sh
# BEFORE bash scripts/deploy.sh -- the cross-build uses the pi-sysroot
# to actually link, then cp's the binary back to the path deploy.sh
# rsyncs from. Skipping this step ships a stale binary silently
# (no error -- deploy.sh just rsyncs what's on disk).
#
# Staleness guard (2026-05-26): warn if the local binary is older
# than the most recent commit touching renderer/. Helps catch the
# "forgot to cross-build" footgun before it ships stale bits.
RUST_BIN_HOST="$OPENMARQUEE_BUILD_DIR/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
if [ -f "$RUST_BIN_HOST" ]; then
    LATEST_RENDERER_COMMIT_TS="$(git -C "$OPENMARQUEE_SRC" log -1 --format=%ct -- renderer/ 2>/dev/null || echo 0)"
    BIN_MTIME="$(stat -f %m "$RUST_BIN_HOST" 2>/dev/null || stat -c %Y "$RUST_BIN_HOST" 2>/dev/null || echo 0)"
    if [ "$LATEST_RENDERER_COMMIT_TS" -gt "$BIN_MTIME" ]; then
        echo "==> WARNING: aarch64 binary mtime is OLDER than the latest renderer/ commit." >&2
        echo "             binary: $(date -r "$BIN_MTIME")" >&2
        echo "             commit: $(date -r "$LATEST_RENDERER_COMMIT_TS")" >&2
        echo "             Run scripts/renderer_cross_build.sh to refresh before deploy." >&2
        echo "             Continuing in 5s with the stale binary; Ctrl-C to abort." >&2
        sleep 5
    fi
    echo "==> rsync Rust IPC sidecar binary to $TARGET:$REMOTE_ROOT/bin/"
    ssh "$TARGET" "mkdir -p $REMOTE_ROOT/bin"
    rsync -avz --rsync-path="sudo rsync" "$RUST_BIN_HOST" "$TARGET:$REMOTE_ROOT/bin/openmarquee-render"
else
    echo "==> no Rust sidecar binary at $RUST_BIN_HOST; skipping (run scripts/renderer_cross_build.sh first to enable opt-in)"
fi

echo "==> rsync UI to $TARGET:$REMOTE_ROOT/ui/"
rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
    --exclude 'src/' \
    --exclude 'e2e/' \
    --exclude 'node_modules' \
    --exclude '*.test.js' \
    --exclude 'vitest.config.js' \
    --exclude 'playwright.config.js' \
    --exclude 'playwright-report/' \
    --exclude 'test-results/' \
    --exclude 'package-lock.json' \
    --exclude '._*' \
    "$OPENMARQUEE_BUILD_DIR/ui/" "$TARGET:$REMOTE_ROOT/ui/"

# scripts/ and system/ both need to be on the Pi for install.sh to find
# itself + the systemd units, hostapd.conf, dnsmasq.conf it stages. Without
# these the next line ("ssh $TARGET sudo bash $REMOTE_ROOT/scripts/install.sh")
# fails with "No such file or directory" and the deploy aborts. Both
# directories are small (<200KB total) so unconditional rsync is fine.
echo "==> rsync scripts/ to $TARGET:$REMOTE_ROOT/scripts/"
rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
    --exclude '._*' \
    --exclude '__pycache__' \
    "$OPENMARQUEE_BUILD_DIR/scripts/" "$TARGET:$REMOTE_ROOT/scripts/"

echo "==> rsync system/ to $TARGET:$REMOTE_ROOT/system/"
rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
    --exclude '._*' \
    "$OPENMARQUEE_BUILD_DIR/system/" "$TARGET:$REMOTE_ROOT/system/"

# images/ carries the pi-gen substage assets install.sh's boot-splash
# section (7d) reads at redeploy time: the Plymouth theme, boot-config-
# lib.sh, and the plymouth-quit handoff drop-in. build_sd_bundle.sh
# bundles images/ for a factory SD image; the developer/redeploy path
# needs the same single canonical copies or install.sh logs "asset
# absent; skip" and the boot splash silently never installs. Small dir.
echo "==> rsync images/ to $TARGET:$REMOTE_ROOT/images/"
rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
    --exclude '._*' \
    --exclude '__pycache__' \
    "$OPENMARQUEE_BUILD_DIR/images/" "$TARGET:$REMOTE_ROOT/images/"

# r31 (2026-05-31) -- wheels refresh.
#
# FYS-prod v1.0.0 deploy (r28) hit a pip rc=1 because
# /opt/openmarquee/wheels/ was 15 days stale (pinned to v0.9.0's
# wheel set, which had certifi==2026.4.22 vs v1.0.0's pinned
# certifi==2026.5.20). pip with --no-index --find-links can't
# find the newer version → "No matching distribution found for
# certifi==2026.5.20" → install.sh EXIT trap → partial deploy.
#
# Fix: download a fresh aarch64 wheel set matching the CURRENT
# requirements.lock into a local cache, then rsync that to
# /opt/openmarquee/wheels/ on the Pi. pip download is idempotent
# (skips wheels already in the cache that match the requested
# spec), so cross-deploy reuse is fast — only the wheels for new
# / bumped pins get re-fetched.
#
# The cache lives at $OPENMARQUEE_BUILD_DIR/wheels/ so it
# survives across deploys but is local-disk-only (not in the
# source tree, not gitignored separately because BUILD_DIR is
# already outside the repo).
#
# Platform tags mirror build_sd_bundle.sh §3 (which builds the
# canonical SD-bundle wheels). Keep both in sync if either
# changes -- a divergence would mean factory-flashed Pis get
# different wheels than dev-redeployed Pis.
echo "==> refreshing aarch64 wheel cache for current requirements.lock"
WHEELS_CACHE="$OPENMARQUEE_BUILD_DIR/wheels"
mkdir -p "$WHEELS_CACHE"
# --only-binary=:all: refuses source-dist: if a pinned dep doesn't
# ship an aarch64 wheel at any of the listed manylinux tags, this
# fails LOUDLY on the dev host rather than silently shipping an
# incomplete cache that the Pi can't resolve.
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"
pip download \
    --dest "$WHEELS_CACHE" \
    --platform manylinux2014_aarch64 \
    --platform manylinux_2_17_aarch64 \
    --platform manylinux_2_28_aarch64 \
    --platform manylinux_2_26_aarch64 \
    --platform manylinux_2_31_aarch64 \
    --platform linux_aarch64 \
    --python-version "$PYTHON_VERSION" \
    --implementation cp \
    --abi "cp${PYTHON_VERSION//./}" \
    --only-binary=:all: \
    --requirement "$OPENMARQUEE_BUILD_DIR/backend/requirements.lock" \
    > /dev/null
# Bootstrap setuptools/wheel/pip per build_sd_bundle.sh §3 — pure-
# Python (py3-none-any) wheels needed for the editable install with
# --no-build-isolation. Versions pinned to match the SD bundle.
pip download \
    --dest "$WHEELS_CACHE" \
    --only-binary=:all: \
    "setuptools==82.0.1" "wheel==0.47.0" "pip==26.1.1" \
    > /dev/null
WHEEL_COUNT=$(find "$WHEELS_CACHE" -name '*.whl' | wc -l | tr -d ' ')
echo "==>   wheel cache: $WHEEL_COUNT wheels at $WHEELS_CACHE"

# Mirror build_sd_bundle.sh:305-311's BAD_WHEELS sanity check.
# Catches a permissive --platform tag (or a regression in pip's
# tag matching) that would otherwise quietly land x86_64 / amd64
# wheels in the cache + ship them to the Pi, where they crash on
# first import. Fast-fail at the dev host before the rsync.
BAD_WHEELS=$(find "$WHEELS_CACHE" -name '*x86_64*.whl' -o -name '*amd64*.whl' 2>/dev/null || true)
if [ -n "$BAD_WHEELS" ]; then
    echo "ERROR: non-aarch64 wheels in cache (would crash on the Pi):" >&2
    echo "$BAD_WHEELS" | sed 's/^/    /' >&2
    echo "    pip download flags may be too permissive; investigate $WHEELS_CACHE" >&2
    exit 1
fi

echo "==> rsync wheels/ to $TARGET:$REMOTE_ROOT/wheels/"
# --delete trims stale wheels (e.g. yanked package versions) so the
# Pi's /opt/openmarquee/wheels/ exactly matches the current cache.
# Important: a stale wheel left in place would happily satisfy a
# pin if the version happened to match, even if it's no longer in
# the requirements.lock -- which would mask a future regression
# where install.sh ought to fail-loud.
rsync -avz --rsync-path="sudo rsync" --delete \
    --exclude '._*' \
    "$WHEELS_CACHE/" "$TARGET:$REMOTE_ROOT/wheels/"

echo "==> running install.sh on remote (idempotent provisioning)"
# install.sh handles venv (Batch 11.1 / sweep #5 #7 requirements.lock
# pin), systemd unit install, hostapd/dnsmasq/iptables wiring, and
# kicks the backend restart. It's idempotent -- safe to re-run on
# every redeploy. Per Phase B.3 dispatch: this is the single
# entry point for both first-boot config and developer redeploy.
ssh "$TARGET" "sudo bash $REMOTE_ROOT/scripts/install.sh"

# 19.3 / sweep #10 #5: gate the deploy on /healthz returning 200.
# Mandatory (not advisory) -- a backend that crashes during startup
# (config error / asset missing / migration failure) silently
# remained "deployed" before. Now the deploy fails fast.
#
# --max-time 30 budget covers startup of: lifespan (seed, prune,
# renderer __enter__, playback start, pull worker), uvicorn bind.
# --retry 5 + --retry-delay 2 spans 10-30s; matches the StartLimit
# in backend.service (5 failures in 5 min).
echo "==> verifying backend health (/healthz must 200 within 30s)"
# Use Tailscale hostname (or whatever TARGET resolves to); deploy
# typically targets openmarquee@<host> so we strip the user@.
HEALTH_HOST="${TARGET#*@}"
if ! curl --max-time 30 --retry 5 --retry-delay 2 --fail -sS "http://$HEALTH_HOST/healthz" > /dev/null; then
    echo "ERROR: backend did not return 200 within budget"
    echo "       inspect: ssh $TARGET sudo journalctl -u openmarquee-backend -n 50"
    exit 1
fi
echo "==> /healthz OK"

cat <<EOF

deployed to $TARGET.

check status:
    ssh $TARGET sudo systemctl status openmarquee-backend

tail logs:
    ssh $TARGET sudo journalctl -u openmarquee-backend -f

health check:
    curl http://$(echo "$TARGET" | sed 's/.*@//')/healthz
EOF
