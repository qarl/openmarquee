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
rsync -avz --delete --delete-excluded \
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
RUST_BIN_HOST="$OPENMARQUEE_BUILD_DIR/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
if [ -f "$RUST_BIN_HOST" ]; then
    echo "==> rsync Rust IPC sidecar binary to $TARGET:$REMOTE_ROOT/bin/"
    ssh "$TARGET" "mkdir -p $REMOTE_ROOT/bin"
    rsync -avz "$RUST_BIN_HOST" "$TARGET:$REMOTE_ROOT/bin/openmarquee-render"
else
    echo "==> no Rust sidecar binary at $RUST_BIN_HOST; skipping (run scripts/renderer_cross_build.sh first to enable opt-in)"
fi

echo "==> rsync UI to $TARGET:$REMOTE_ROOT/ui/"
rsync -avz --delete --delete-excluded \
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
