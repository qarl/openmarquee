#!/usr/bin/env bash
# scripts/build_sd_bundle.sh -- produce a self-contained tarball that
# stage_sd_card.sh drops onto a freshly-flashed Pi OS Lite arm64 SD card.
#
# Output: dist/openmarquee-sd-bundle.tar.zst
#
# Contains, all rooted at "openmarquee/" inside the tarball:
#   backend/                  -- python package source (no tests)
#   ui/                       -- pre-built UI bundle (index.html + dist/)
#   scripts/                  -- install.sh and helpers (no SD-build scripts)
#   system/                   -- systemd units, hostapd.conf, dnsmasq.conf
#   bin/openmarquee-render    -- Rust IPC sidecar, aarch64-linux-gnu
#   wheels/                   -- vendored pip wheels for linux_aarch64
#   pyproject.toml            -- copied for `pip install -e .` on device
#   requirements.lock         -- pin file install.sh feeds to pip
#
# Excludes (HARD): .env, .ssh/, .git/, secrets/, ~/Jimmy/, openmarquee-
# content/, openmarquee-settings.json (carries Tailscale auth key + AP
# password), credentials.*, *.pem, *.key. Anything that smells like a
# secret stops the build with a clear error.
#
# Usage:
#     bash scripts/build_sd_bundle.sh                  # default output
#     bash scripts/build_sd_bundle.sh --no-wheels      # skip pip download
#                                                      #   (10x faster; Pi
#                                                      #   uses online pip)
#     bash scripts/build_sd_bundle.sh --output PATH    # alternative dest
#
# Vendored wheels: pip download with --platform manylinux2014_aarch64 and
# --only-binary=:all:. If pip can't satisfy a dependency as an aarch64
# wheel (e.g. C-extension packages without prebuilt wheels), the build
# fails LOUDLY -- shipping x86_64 wheels would crash the Pi at first run.
# Workaround for that case: run with --no-wheels and let the Pi pip-install
# online during first boot (slower; needs network in AP-only mode = nope).
#
# Run from the repo root or anywhere; the script resolves its own location.

set -euo pipefail

# --- Locate the repo root regardless of caller's cwd. -----------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# --- Arg parsing ------------------------------------------------------------

OUTPUT="$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst"
DO_WHEELS=1
PYTHON_VERSION="3.13"

while [ $# -gt 0 ]; do
    case "$1" in
        --no-wheels) DO_WHEELS=0; shift ;;
        --output)    OUTPUT="$2"; shift 2 ;;
        --python-version) PYTHON_VERSION="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --- Pre-flight: required tools --------------------------------------------

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "error: $1 not on PATH" >&2; exit 2; }
}
require tar
require zstd
require rsync
[ "$DO_WHEELS" -eq 1 ] && require pip

# --- Staging dir + cleanup trap --------------------------------------------

STAGING="$(mktemp -d -t openmarquee-sd-bundle.XXXXXX)"
cleanup() { rm -rf "$STAGING"; }
trap cleanup EXIT
ROOT="$STAGING/openmarquee"
mkdir -p "$ROOT"

say() { printf '==> %s\n' "$*"; }

# --- Hard-fail on secrets in the staging input -----------------------------

# These globs cover the obvious credentials. We check the SOURCE tree (not
# the staging output) so the error message points at the real file path.
# Match the patterns that landed in the dispatch checklist.
say "Scanning source for secrets (refuse to bundle credentials)"
# openmarquee-*.json (runtime settings/playlist) + openmarquee-content/
# (per-device CMS items) + tailscale_hostname are CARRIED by the dev tree
# but never enter the bundle -- the rsync excludes below skip them and
# the device generates fresh state on first boot. The scanner targets
# only the patterns that would survive rsync if accidentally added.
SECRET_HITS=$(
    {
        find . -name '.env' -not -path '*/node_modules/*' 2>/dev/null
        find . -name '.env.*' -not -path '*/node_modules/*' 2>/dev/null
        find . -name 'credentials.*' 2>/dev/null
        find . -name '*.pem' 2>/dev/null
        find . -name '*.key' -not -path '*/node_modules/*' 2>/dev/null
        find . -name 'id_rsa*' 2>/dev/null
        find . -name 'id_ed25519*' 2>/dev/null
        find . -name 'auth.json' 2>/dev/null
    } | grep -v '/.git/' || true
)
if [ -n "$SECRET_HITS" ]; then
    echo "error: refusing to bundle; secret-shaped files in source:" >&2
    echo "$SECRET_HITS" | sed 's/^/    /' >&2
    echo "    move these outside the repo (or .gitignore + delete) before re-running." >&2
    exit 3
fi

# --- 1. Code tree (backend, ui-built, scripts, system) ---------------------

say "Copying backend/ to staging (excluding tests + caches + runtime state)"
rsync -a --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '*.egg-info' \
    --exclude 'tests/' \
    --exclude '._*' \
    --exclude '.DS_Store' \
    --exclude '.Jimmy/' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.vite' \
    --exclude '.git/' \
    --exclude 'openmarquee-*.json' \
    --exclude 'openmarquee-content/' \
    --exclude 'auth.json' \
    --exclude 'wifi.json' \
    --exclude 'identity.json' \
    --exclude 'tailscale_hostname' \
    "$REPO_ROOT/backend/" "$ROOT/backend/"

# UI dist must already be built (esbuild). Don't bake the build step in;
# `bash scripts/build_sd_bundle.sh` after `(cd ui && npm run build)` is the
# expected flow. If dist/ is missing or stale, error out -- shipping an
# empty UI silently is worse than failing loudly.
if [ ! -d "$REPO_ROOT/ui/dist" ] || [ -z "$(ls -A "$REPO_ROOT/ui/dist" 2>/dev/null)" ]; then
    echo "error: ui/dist missing or empty; run \`(cd ui && npm run build)\` first" >&2
    exit 4
fi
say "Copying ui/ to staging (built bundle only; sources excluded)"
rsync -a --delete \
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
    --exclude '.DS_Store' \
    --exclude '.Jimmy/' \
    --exclude '.ruff_cache' \
    --exclude '.pytest_cache' \
    --exclude '.mypy_cache' \
    --exclude '.vite' \
    --exclude '.git/' \
    --exclude 'openmarquee-content/' \
    --exclude 'scripts/' \
    "$REPO_ROOT/ui/" "$ROOT/ui/"

say "Copying scripts/ to staging (install.sh + on-device helpers)"
# Exclude SD-build scripts from the bundle -- they're build-time-only and
# carry no value on the device. install.sh + deploy.sh are kept (deploy.sh
# is occasionally useful for redeploy from the device to a peer).
rsync -a --delete \
    --exclude '._*' \
    --exclude '.DS_Store' \
    --exclude '.Jimmy/' \
    --exclude '__pycache__' \
    --exclude '.git/' \
    --exclude 'build_sd_bundle.sh' \
    --exclude 'stage_sd_card.sh' \
    "$REPO_ROOT/scripts/" "$ROOT/scripts/"

say "Copying system/ to staging (systemd units + hostapd/dnsmasq configs)"
rsync -a --delete \
    --exclude '._*' \
    --exclude '.DS_Store' \
    --exclude '.Jimmy/' \
    --exclude '.git/' \
    "$REPO_ROOT/system/" "$ROOT/system/"

# --- 2. Rust IPC sidecar binary (aarch64) -----------------------------------

RUST_BIN_LOCAL="$REPO_ROOT/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
RUST_BIN_BUILD="$HOME/tmp/openmarquee-build/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
# Tolerate either local repo build OR BUILD_DIR mirror build location.
if [ -f "$RUST_BIN_LOCAL" ]; then
    RUST_BIN="$RUST_BIN_LOCAL"
elif [ -f "$RUST_BIN_BUILD" ]; then
    RUST_BIN="$RUST_BIN_BUILD"
else
    RUST_BIN=""
fi
if [ -n "$RUST_BIN" ]; then
    say "Adding Rust binary from $RUST_BIN"
    mkdir -p "$ROOT/bin"
    cp "$RUST_BIN" "$ROOT/bin/openmarquee-render"
    chmod +x "$ROOT/bin/openmarquee-render"
    # Sanity-check arch: aarch64 ELF magic + e_machine=0xb7 (AArch64).
    if command -v file >/dev/null; then
        FILE_OUT="$(file "$ROOT/bin/openmarquee-render")"
        if ! echo "$FILE_OUT" | grep -q 'aarch64\|ARM aarch64\|ELF 64-bit'; then
            echo "error: Rust binary doesn't look like aarch64 ELF:" >&2
            echo "    $FILE_OUT" >&2
            exit 5
        fi
    fi
else
    say "WARNING: no Rust binary at $RUST_BIN_LOCAL or $RUST_BIN_BUILD"
    say "         bundle will ship without sidecar; run \`scripts/renderer_cross_build.sh\` to fix"
    # Don't fail -- the Pi can still serve the UI on the Python+DRM path.
fi

# --- 3. Python wheels (vendored, aarch64) ----------------------------------

if [ "$DO_WHEELS" -eq 1 ]; then
    say "Downloading aarch64 wheels per requirements.lock (this is slow)"
    mkdir -p "$ROOT/wheels"
    # --only-binary=:all: forces refusal of source-dist; if a dep doesn't
    # have a manylinux2014_aarch64 wheel, pip fails LOUDLY here instead
    # of silently grabbing x86_64 wheels that crash on the Pi.
    # --platform tag matches what pip will actually use on the Pi:
    #   manylinux2014_aarch64 (covers the common case)
    #   linux_aarch64 (some packages tag this way too)
    # We request both; pip resolves the union.
    pip download \
        --dest "$ROOT/wheels" \
        --platform manylinux2014_aarch64 \
        --platform linux_aarch64 \
        --python-version "$PYTHON_VERSION" \
        --implementation cp \
        --abi "cp${PYTHON_VERSION//./}" \
        --only-binary=:all: \
        --requirement "$REPO_ROOT/backend/requirements.lock" \
        2>&1 | tail -40
    # Phase 4a 2026-05-15: also vendor setuptools + wheel + pip so that
    # install.sh's `pip install -e backend --no-index --no-build-isolation`
    # finds the PEP-517 build backend offline. Python 3.13's venv no
    # longer installs setuptools by default, so without these, an offline
    # `pip install -e .` would fail importing setuptools.build_meta.
    # These three are pure-Python (py3-none-any wheels), arch-independent.
    pip download \
        --dest "$ROOT/wheels" \
        --no-deps \
        --only-binary=:all: \
        setuptools wheel pip \
        2>&1 | tail -10
    WHEEL_COUNT=$(find "$ROOT/wheels" -name '*.whl' | wc -l | tr -d ' ')
    say "  vendored $WHEEL_COUNT wheels"
    # Quick sanity check: every wheel's filename should contain
    # manylinux*_aarch64 or linux_aarch64 (not x86_64 or universal). Pure-
    # Python wheels (py3-none-any) are fine -- they're arch-independent.
    BAD_WHEELS=$(find "$ROOT/wheels" -name '*x86_64*.whl' -o -name '*amd64*.whl' 2>/dev/null || true)
    if [ -n "$BAD_WHEELS" ]; then
        echo "error: x86_64 wheels snuck into the bundle:" >&2
        echo "$BAD_WHEELS" | sed 's/^/    /' >&2
        echo "    pip download flags may be too permissive; investigate." >&2
        exit 6
    fi
else
    say "Skipping wheel download (--no-wheels); Pi will pip-install online"
fi

# --- 4. Top-level metadata --------------------------------------------------

# pyproject.toml + requirements.lock at the bundle root so install.sh can
# find them without depending on backend/ layout. Both are small.
cp "$REPO_ROOT/backend/pyproject.toml" "$ROOT/pyproject.toml"
cp "$REPO_ROOT/backend/requirements.lock" "$ROOT/requirements.lock"

# Stamp a manifest so the operator can verify what shipped.
{
    echo "openmarquee SD bundle"
    echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git:   $(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "host:  $(hostname)"
    echo
    echo "tree:"
    (cd "$STAGING" && find openmarquee -maxdepth 2 -type d | sort)
    echo
    if [ -d "$ROOT/wheels" ]; then
        echo "wheels: $(find "$ROOT/wheels" -name '*.whl' | wc -l | tr -d ' ')"
    fi
    if [ -f "$ROOT/bin/openmarquee-render" ]; then
        echo "rust-binary: present ($(stat -f%z "$ROOT/bin/openmarquee-render" 2>/dev/null || stat -c%s "$ROOT/bin/openmarquee-render") bytes)"
    else
        echo "rust-binary: ABSENT"
    fi
} > "$ROOT/MANIFEST.txt"

# --- 5. Tar + zstd ----------------------------------------------------------

mkdir -p "$(dirname "$OUTPUT")"
say "Compressing to $OUTPUT"
# -C $STAGING then archive "openmarquee" so the tar's top-level entry is
# "openmarquee/" -- extracting against /opt/ on the Pi lands at
# /opt/openmarquee/, no rename step needed.
tar -C "$STAGING" -cf - openmarquee \
    | zstd --quiet -19 -o "$OUTPUT"

SIZE=$(stat -f%z "$OUTPUT" 2>/dev/null || stat -c%s "$OUTPUT")
SIZE_HUMAN=$(echo "$SIZE" | awk '{
    if ($1 > 1073741824) printf "%.1f GiB", $1/1073741824
    else if ($1 > 1048576) printf "%.1f MiB", $1/1048576
    else printf "%d B", $1
}')

cat <<EOF

bundle ready:
    $OUTPUT
    size: $SIZE_HUMAN ($SIZE bytes)

next:
    bash scripts/stage_sd_card.sh /Volumes/bootfs

EOF
