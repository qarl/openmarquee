#!/usr/bin/env bash
# scripts/deploy.sh — push local code to a Raspberry Pi running openMarquee.
#
# Usage:
#     bash scripts/deploy.sh <ssh-target>
#     bash scripts/deploy.sh pi@openmarquee.local
#     bash scripts/deploy.sh pi@192.168.1.42
#
# What it does:
#   1. Rebuilds the UI bundle locally (esbuild; ~1s) so the Pi runs the
#      current JS, not a stale dist/.
#   2. Rsyncs backend/ (excluding tests, __pycache__, caches) to
#      /opt/openmarquee/backend/ on the target.
#   3. Rsyncs the UI static files (index.html, welcome.html, styles.css,
#      dist/) to /opt/openmarquee/ui/. Source .js, tests, and node_modules
#      are excluded — the device serves the built bundle only.
#   4. Installs or updates the backend's Python deps into
#      /opt/openmarquee/venv/ via pip -e .
#   5. Restarts the openmarquee-backend systemd unit.
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

if [ $# -ne 1 ]; then
    echo "usage: $0 <ssh-target>" >&2
    echo "example: $0 pi@openmarquee.local" >&2
    exit 1
fi

TARGET="$1"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT="${OPENMARQUEE_REMOTE_ROOT:-/opt/openmarquee}"

echo "==> rebuilding UI bundle"
(cd "$PROJECT_ROOT/ui" && npm run build --silent)

echo "==> rsync backend to $TARGET:$REMOTE_ROOT/backend/"
rsync -avz --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude 'tests/' \
    "$PROJECT_ROOT/backend/" "$TARGET:$REMOTE_ROOT/backend/"

echo "==> rsync UI to $TARGET:$REMOTE_ROOT/ui/"
rsync -avz --delete \
    --exclude 'src/' \
    --exclude 'e2e/' \
    --exclude 'node_modules' \
    --exclude '*.test.js' \
    --exclude 'vitest.config.js' \
    --exclude 'playwright.config.js' \
    --exclude 'playwright-report/' \
    --exclude 'test-results/' \
    --exclude 'package-lock.json' \
    "$PROJECT_ROOT/ui/" "$TARGET:$REMOTE_ROOT/ui/"

echo "==> installing / updating backend deps in remote venv"
# -e install picks up any new pyproject.toml deps without reinstalling the
# world. If this fails the first time, run system/README.md § first-time
# install on the target.
ssh "$TARGET" "$REMOTE_ROOT/venv/bin/pip install --quiet --upgrade -e $REMOTE_ROOT/backend"

echo "==> restarting openmarquee-backend"
ssh "$TARGET" "sudo systemctl restart openmarquee-backend"

cat <<EOF

deployed to $TARGET.

check status:
    ssh $TARGET sudo systemctl status openmarquee-backend

tail logs:
    ssh $TARGET sudo journalctl -u openmarquee-backend -f

health check:
    curl http://$(echo "$TARGET" | sed 's/.*@//')/healthz
EOF
