#!/usr/bin/env bash
# refresh.sh — rebuild the demo bundle into $OPENMARQUEE_BUILD_DIR/demo/
# and (optionally) re-snapshot the seed from a running backend.
#
# Usage:
#   scripts/demo/refresh.sh                       # UI rebuild only
#   scripts/demo/refresh.sh --seed                # also resnapshot seed
#                                                 #   (auto-detects 9886 / 8000)
#   scripts/demo/refresh.sh --seed http://host    # snapshot from that URL
#
# What it does, in order:
#   1. rsync source UI → $OPENMARQUEE_BUILD_DIR/ui (mirror to a writable spot)
#   2. npm run build (esbuild bundle)
#   3. build.sh    (assemble bundle + static shell into BUILD_DIR/demo/)
#   4. generate-seed.py (only with --seed; needs a backend running)
#   5. check-mock-drift.py (warns if real backend has endpoints the mock
#      doesn't handle; non-fatal — just a heads-up)
#
# Run this after editing UI source, the mock backend, or the device's
# seed content. Then www's deploy.sh (dry-run first) to push live.
set -euo pipefail

cd "$(dirname "$0")"

CODE_ROOT="$(cd ../.. && pwd)"
BUILD_DIR="${OPENMARQUEE_BUILD_DIR:-$HOME/tmp/openmarquee-build}"
SEED_BACKEND=""
DO_SEED=0

# Arg parse — keep simple: --seed [URL]
while [ $# -gt 0 ]; do
    case "$1" in
        --seed)
            DO_SEED=1
            shift
            if [ $# -gt 0 ] && [[ "$1" =~ ^https?:// ]]; then
                SEED_BACKEND="$1"
                shift
            fi
            ;;
        -h|--help)
            sed -n '2,/^set -/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
done

# Parse-check the demo's hand-maintained JS shims BEFORE any rebuild.
# vitest doesn't import mock-backend.js (it's loaded by index.html in the
# browser), so a syntax error there sails past `npm run test` and bricks
# the live demo on first /api/* call (qarl + QA caught one on 2026-05-01:
# `font_*/auto_*` inside a /** */ docblock closed the comment early).
echo "==> parse-check demo static JS"
for f in static/mock-backend.js static/sw.js; do
    if [ -f "$f" ]; then
        node --check "$f"
    fi
done

echo "==> mirror UI source to BUILD_DIR ($BUILD_DIR/ui)"
mkdir -p "$BUILD_DIR/ui"
rsync -a --delete \
    --exclude=node_modules \
    --exclude=dist \
    --exclude=.Jimmy \
    --exclude='._*' \
    --exclude='.DS_Store' \
    "$CODE_ROOT/ui/" "$BUILD_DIR/ui/"

if [ ! -d "$BUILD_DIR/ui/node_modules" ]; then
    echo "==> npm install (first run)"
    (cd "$BUILD_DIR/ui" && npm install --silent)
fi

echo "==> esbuild bundle"
# Sweep stale esbuild atomic-write tempfiles from any prior interrupted
# build. esbuild writes `dist/.<name>.<random>` then renames to
# `dist/<name>` for atomicity; killed mid-write, the tempfiles linger
# (mode 600, dated days back). Without this sweep, build.sh's rsync
# would carry them into BUILD_DIR/demo/dist/ and deploy.sh would push
# them to the live /demo/ as hidden-but-discoverable URLs — flagged
# by www-Jimmy + QA 2026-05-03 (7 stale tempfiles dated Apr 28; QA
# manually scrubbed before each deploy for 2 days).
#
# `find -name '.ffmpeg-worker.js.*'` matches the `.ffmpeg-worker.js.
# <random>` shape — the random suffix means the tempfile DOESN'T end
# in `.js`, so a glob like `.*.js` wouldn't catch it (caught my own
# first-pass mistake). `-maxdepth 1` keeps us inside dist/ proper.
if [ -d "$BUILD_DIR/ui/dist" ]; then
    find "$BUILD_DIR/ui/dist" -maxdepth 1 -type f -name '.ffmpeg-worker.js.*' -delete 2>/dev/null || true
fi
(cd "$BUILD_DIR/ui" && npm run build --silent | tail -3)

echo "==> assemble demo bundle into $BUILD_DIR/demo/"
./build.sh "$BUILD_DIR/ui"

if [ "$DO_SEED" = "1" ]; then
    if [ -z "$SEED_BACKEND" ]; then
        # Auto-detect: try the demo-peer port first, then the dev port.
        for port in 9886 8000; do
            if curl -s -o /dev/null -w "%{http_code}" \
                "http://127.0.0.1:$port/healthz" | grep -q "^200$"; then
                SEED_BACKEND="http://127.0.0.1:$port"
                break
            fi
        done
    fi
    if [ -z "$SEED_BACKEND" ]; then
        echo "WARNING: --seed requested but no backend is reachable"
        echo "         (tried 127.0.0.1:9886 and 127.0.0.1:8000)."
        echo "         Skipping seed regen — content + assets are unchanged."
    else
        echo "==> snapshot seed from $SEED_BACKEND"
        ./generate-seed.py "$SEED_BACKEND" | tail -5
    fi
else
    echo "==> skipping seed snapshot (pass --seed to refresh)"
fi

echo "==> drift check (mock-backend ↔ real backend routes)"
if [ -x ./check-mock-drift.py ]; then
    ./check-mock-drift.py || true
fi

echo
echo "demo bundle ready at $BUILD_DIR/demo/"
du -sh "$BUILD_DIR/demo/" | awk '{print "total size:", $1}'
echo "preview locally:"
echo "    (cd $BUILD_DIR && python3 -m http.server 8765 && open http://127.0.0.1:8765/demo/)"
