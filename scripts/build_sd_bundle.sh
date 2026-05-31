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
#   images/                   -- pi-gen substage tree (Plymouth boot-splash
#                                theme + boot-config-lib.sh + handoff drop-in;
#                                install.sh's redeploy path reads these)
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

# Suppress macOS AppleDouble (._*) sidecar files for any cp/rsync/tar
# the script invokes downstream. Boot-5 forensics found 41 ._* metadata
# files in the bundle's wheels/ dir. pip tolerated them but they're a
# latent footgun for any downstream tool that doesn't.
export COPYFILE_DISABLE=1

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

# --- 0a. r37a (2026-05-31): ensure gitignored runtime assets present ------
#
# build_sd_bundle.sh has historically assumed scripts/setup.sh ran first
# (which downloads the COLRv1 emoji font). A fresh /tmp clone build skips
# setup.sh and ships the bundle without ui/fonts/noto-color-emoji-colrv1.ttf.
# Customers fresh-burning that bundle see emoji tofu. Same regression class
# that bit FYS prod 2026-05-31 r31 -> r32, surfaced for the bundle path by
# code2's r36 audit §B.5 + §C.1.
#
# Mirrors r32's deploy.sh fix shape. The download script is SHA-pinned +
# idempotent (skips if file present at the expected stripped size).
say "Ensuring gitignored runtime assets are present"
bash "$REPO_ROOT/scripts/download-emoji-font-colrv1.sh"

# --- 0b. r37a (2026-05-31): fail-loud asset preconditions -----------------
#
# Sanity-check required artifacts BEFORE the expensive staging + tar
# steps. Better to fail in ~50ms than after 30s of wheel downloads when
# a truncated font or missing binary would have silently shipped.
#
# Per code2's r36 audit §F.7. Covers the asset classes that cause
# customer-visible regressions if shipped silently:
#   - emoji font absent OR truncated (tofu on every emoji slide)
#   - aarch64 renderer binary absent (Pi falls back to Python+DRM,
#     much slower) OR wrong architecture (won't even exec)

# Emoji font: must exist + be larger than 3 MB (the stripped colrv1
# binary is ~4.8 MB; values < 3 MB indicate truncation, partial
# download, or accidental zero-byte file).
EMOJI_FONT="$REPO_ROOT/ui/fonts/noto-color-emoji-colrv1.ttf"
if [ ! -f "$EMOJI_FONT" ]; then
    echo "error: COLRv1 emoji font missing at $EMOJI_FONT" >&2
    echo "       (the download in section 0a should have placed it; re-run" >&2
    echo "        \`bash scripts/download-emoji-font-colrv1.sh\` manually if" >&2
    echo "        that section was edited out)" >&2
    exit 8
fi
# stat -f%z is BSD (macOS); stat -c%s is GNU (Linux). Try both per
# the same fallback pattern at the SHA256 sidecar + manifest size
# sites later in this script.
FONT_SIZE=$(stat -f%z "$EMOJI_FONT" 2>/dev/null || stat -c%s "$EMOJI_FONT" 2>/dev/null || echo 0)
if [ "$FONT_SIZE" -lt 3000000 ]; then
    echo "error: COLRv1 emoji font at $EMOJI_FONT is too small ($FONT_SIZE bytes; expected > 3 MB)" >&2
    echo "       truncated download? delete + re-run \`bash scripts/download-emoji-font-colrv1.sh\`" >&2
    exit 8
fi
say "  emoji font OK ($FONT_SIZE bytes at $EMOJI_FONT)"

# aarch64 renderer binary: pre-flight ARCH sanity ONLY (binary
# present-or-absent is handled by section 2 below, which prints
# WARNING + continues; we don't pre-flight that case because
# duplicating the warn adds no information). The arch check IS
# pre-flight-worthy: if the binary exists but is wrong-arch (e.g.
# host-build leaked to the cross-build path), better to fail in
# 50ms than after 30s of wheel downloads + tar/zstd.
RUST_BIN_PRE_LOCAL="$REPO_ROOT/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
RUST_BIN_PRE_BUILD="$HOME/tmp/openmarquee-build/renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render"
if [ -f "$RUST_BIN_PRE_LOCAL" ]; then
    RUST_BIN_PRE="$RUST_BIN_PRE_LOCAL"
elif [ -f "$RUST_BIN_PRE_BUILD" ]; then
    RUST_BIN_PRE="$RUST_BIN_PRE_BUILD"
else
    RUST_BIN_PRE=""
fi
if [ -n "$RUST_BIN_PRE" ] && command -v file >/dev/null; then
    PRE_FILE_OUT="$(file "$RUST_BIN_PRE")"
    # `aarch64` / `ARM aarch64` are the discriminating substrings.
    # Do NOT add `ELF 64-bit` to the alternation — it matches x86_64
    # ELF too ("ELF 64-bit LSB executable, x86-64, ...") and would
    # silently pass a wrong-arch binary through. (Pre-r37a section 2
    # had this same loose alternation; r37a tightens both sites.)
    if ! echo "$PRE_FILE_OUT" | grep -q 'aarch64\|ARM aarch64'; then
        echo "error: Rust binary at $RUST_BIN_PRE does not look like aarch64 ELF:" >&2
        echo "    $PRE_FILE_OUT" >&2
        echo "    re-run \`bash scripts/renderer_cross_build.sh\` from a fresh state" >&2
        exit 8
    fi
    say "  aarch64 renderer binary OK ($RUST_BIN_PRE)"
fi
# (Binary-missing case intentionally not pre-flighted here — section
# 2 below handles it with a WARNING + ships without sidecar. Pi can
# still serve the UI on the Python+DRM path.)

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

# images/ carries the pi-gen substage tree — small (~the boot-splash
# theme PNG + recipe text). install.sh's redeploy path (section 7d)
# reads the Plymouth theme files, boot-config-lib.sh, and the
# plymouth-quit handoff drop-in from /opt/openmarquee/images/..., so
# the same boot splash a factory-flashed card gets from the image
# build also reaches an already-provisioned Pi on redeploy.
say "Copying images/ to staging (pi-gen substage tree — boot-splash assets)"
rsync -a --delete \
    --exclude '._*' \
    --exclude '.DS_Store' \
    --exclude '.Jimmy/' \
    --exclude '.git/' \
    "$REPO_ROOT/images/" "$ROOT/images/"

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
        if ! echo "$FILE_OUT" | grep -q 'aarch64\|ARM aarch64'; then
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
    # Pi OS Lite trixie ships glibc 2.36, so any manylinux tag with
    # glibc <= 2.36 is compatible. List the modern manylinux variants
    # explicitly: pip's platform matching is strict (no implicit
    # backcompat), so packages that only ship newer-tagged wheels
    # (e.g., argon2-cffi-bindings 25.1.0 ships manylinux_2_26/_2_28
    # but NOT manylinux2014) would otherwise fail with "no matching
    # distribution found." manylinux2014 stays in the list to cover
    # older packages that haven't moved off the legacy tag yet.
    pip download \
        --dest "$ROOT/wheels" \
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
        --requirement "$REPO_ROOT/backend/requirements.lock" \
        2>&1 | tail -40
    # Vendor setuptools + wheel + pip so that install.sh's
    # `pip install -e backend --no-index --no-build-isolation` finds the
    # PEP-517 build backend offline. Python 3.13's venv no longer installs
    # setuptools by default, so without these, an offline `pip install -e .`
    # would fail importing setuptools.build_meta. These are pure-Python
    # (py3-none-any wheels), arch-independent. Top-level versions pinned
    # for reproducibility; transitive deps (notably wheel's `packaging`
    # requirement) are resolved by pip — DO NOT add --no-deps here, that
    # causes the bootstrap install.sh step to fail with "No matching
    # distribution found for packaging>=24.0" because pip with --no-index
    # can't go to PyPI for the missing transitive.
    pip download \
        --dest "$ROOT/wheels" \
        --only-binary=:all: \
        "setuptools==82.0.1" "wheel==0.47.0" "pip==26.1.1" \
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

# --- 3b. Trixie .debs (vendored, arm64) ------------------------------------
#
# Stock Pi OS Lite arm64 trixie does NOT ship hostapd, iptables, or the
# `dnsmasq` package (it has dnsmasq-base but NOT the systemd-unit-shipping
# dnsmasq), but install.sh on first boot calls all three unconditionally
# (AP bring-up via hostapd + captive-portal NAT via iptables + captive-
# portal DHCP/DNS via dnsmasq). qarl has no ethernet at first boot →
# apt-get is off the table. We vendor the .debs + transitive dep closure
# here at build time so install.sh can install them offline.
#
# Closure computed by walking each apt Packages file for trixie/main
# arm64 + filtering against /var/lib/dpkg/status from a known-good
# Pi-OS-Lite-trixie rootfs. Five packages below are the ones missing
# from the base image (the rest of hostapd + iptables + dnsmasq's
# Depends are already installed). Total ~1.3 MB.
#
# URLs point at snapshot.debian.org with an immutable timestamp so
# future trixie point-releases don't 404 these URLs. The timestamp was
# chosen at commit time; regenerating against a fresher snapshot is a
# bundle-build operator decision (security update tracking).
#
# Pinned filename + sha256 for reproducibility (same approach as the
# setuptools/wheel/pip pinning above). Each .deb is fetched from
# snapshot.debian.org and SHA256-verified before staging.
#
# Skipped on --no-wheels (dev-redeploy on a Pi that already has the
# packages installed).
if [ "$DO_WHEELS" -eq 1 ]; then
    say "Vendoring trixie .debs (hostapd + iptables + dnsmasq closure)"
    mkdir -p "$ROOT/debs"
    # Format: package|version|size_bytes|sha256|url
    # Closure regeneration recipe (if Debian publishes security
    # updates): mount a current Pi-OS-Lite-arm64-trixie rootfs, walk
    # the trixie/main + trixie-security + trixie-updates apt Packages
    # files for hostapd + iptables + dnsmasq's transitive Depends/
    # Pre-Depends, filter against /var/lib/dpkg/status to drop already-
    # installed packages, replace the manifest below with the resulting
    # name|version|size|sha256|url tuples (snapshot URLs preferred).
    DEB_MANIFEST="hostapd|2:2.10-24|801636|0e36fa2d6fe79ae7fc9718ad5f6531eb4e710418fa337543f17315154dc8ab64|https://snapshot.debian.org/archive/debian/20260515T000000Z/pool/main/w/wpa/hostapd_2.10-24_arm64.deb
iptables|1.8.11-2|353592|441a3c4b202f83cb1c2609c9eff768229ce991c2f49bea37416168028bc23e95|https://snapshot.debian.org/archive/debian/20260515T000000Z/pool/main/i/iptables/iptables_1.8.11-2_arm64.deb
libip4tc2|1.8.11-2|19552|e2d86b0b1b5d06b529cf60f3876970b02c1ae1147555bfcf1c5b17bfdae992ca|https://snapshot.debian.org/archive/debian/20260515T000000Z/pool/main/i/iptables/libip4tc2_1.8.11-2_arm64.deb
libip6tc2|1.8.11-2|19800|3ab7581ae06d0b7109ec60907a063ef167446e492f30918dcb20faed810d172d|https://snapshot.debian.org/archive/debian/20260515T000000Z/pool/main/i/iptables/libip6tc2_1.8.11-2_arm64.deb
dnsmasq|2.91-1|69476|91c35d23bae8c121bc066194b5531a5d4ef61af92424feb2efa6044f74048575|https://snapshot.debian.org/archive/debian/20260515T000000Z/pool/main/d/dnsmasq/dnsmasq_2.91-1_all.deb"
    while IFS='|' read -r name version size sha256 url ; do
        [ -z "$name" ] && continue
        deb_filename="$(basename "$url")"
        deb_path="$ROOT/debs/$deb_filename"
        say "  downloading $name $version"
        # --fail = non-200 → curl exits non-zero → set -e aborts the build.
        # --silent --show-error = no progress bar, but errors print.
        # --location = follow redirects (snapshot.debian.org's 302 →
        # static.debian.org or CDN mirror; deb.debian.org → CDN too).
        curl --fail --silent --show-error --location --output "$deb_path" "$url"
        # SHA256 verify against the indexed hash. Mac uses shasum -a 256;
        # Linux build hosts use sha256sum — try both.
        actual_sha=$(shasum -a 256 "$deb_path" 2>/dev/null | awk '{print $1}')
        if [ -z "$actual_sha" ]; then
            actual_sha=$(sha256sum "$deb_path" 2>/dev/null | awk '{print $1}')
        fi
        if [ "$actual_sha" != "$sha256" ]; then
            echo "error: SHA256 mismatch for $deb_filename" >&2
            echo "    expected: $sha256" >&2
            echo "    actual:   $actual_sha" >&2
            exit 7
        fi
        # Also size-check as a redundant guard against truncated downloads.
        actual_size=$(stat -f%z "$deb_path" 2>/dev/null || stat -c%s "$deb_path")
        if [ "$actual_size" != "$size" ]; then
            echo "error: size mismatch for $deb_filename" >&2
            echo "    expected: $size bytes" >&2
            echo "    actual:   $actual_size bytes" >&2
            exit 7
        fi
    done <<< "$DEB_MANIFEST"
    DEB_COUNT=$(find "$ROOT/debs" -name '*.deb' | wc -l | tr -d ' ')
    DEB_SIZE=$(du -sh "$ROOT/debs" | awk '{print $1}')
    say "  vendored $DEB_COUNT .debs ($DEB_SIZE total)"
else
    say "Skipping .deb download (--no-wheels); Pi must have hostapd + iptables + dnsmasq already"
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
    if [ -d "$ROOT/debs" ]; then
        echo "debs: $(find "$ROOT/debs" -name '*.deb' | wc -l | tr -d ' ')"
    fi
    if [ -f "$ROOT/bin/openmarquee-render" ]; then
        echo "rust-binary: present ($(stat -f%z "$ROOT/bin/openmarquee-render" 2>/dev/null || stat -c%s "$ROOT/bin/openmarquee-render") bytes)"
    else
        echo "rust-binary: ABSENT"
    fi
} > "$ROOT/MANIFEST.txt"

# --- 5. Tar + zstd ----------------------------------------------------------

mkdir -p "$(dirname "$OUTPUT")"
# Belt-and-suspenders with COPYFILE_DISABLE=1 above: scrub any macOS
# AppleDouble sidecars that snuck into the staging tree before they
# enter the tar. find -delete is silent on no-match so this is a no-op
# when the export already prevented them.
find "$ROOT" -name '._*' -delete
say "Compressing to $OUTPUT"
# -C $STAGING then archive "openmarquee" so the tar's top-level entry is
# "openmarquee/" -- extracting against /opt/ on the Pi lands at
# /opt/openmarquee/, no rename step needed. -f overrides zstd's default
# refusal to overwrite — rebuilds are idempotent without a manual rm.
tar -C "$STAGING" -cf - openmarquee \
    | zstd --quiet -19 -f -o "$OUTPUT"

SIZE=$(stat -f%z "$OUTPUT" 2>/dev/null || stat -c%s "$OUTPUT")
SIZE_HUMAN=$(echo "$SIZE" | awk '{
    if ($1 > 1073741824) printf "%.1f GiB", $1/1073741824
    else if ($1 > 1048576) printf "%.1f MiB", $1/1048576
    else printf "%d B", $1
}')

# r37a (2026-05-31): SHA256 sidecar so operators can verify the artifact
# they pulled matches the artifact this build produced. Per code2 r36
# audit §F.6. Mirrors the v0.9.0 release pattern — that bundle shipped
# with SHA documented (sha256
# 9aec7cdaef771735d8085769d37b4d85b9f7e37c63c9db2281860742cdeaf987)
# but the stamp was published manually; this lands the sidecar at
# build time so it's never out-of-band.
#
# shasum is BSD-style (macOS preinstalled); falls back to sha256sum
# on Linux. Output format matches what `sha256sum -c` accepts so an
# operator can verify with: `sha256sum -c <bundle>.sha256`.
if command -v shasum >/dev/null 2>&1; then
    (cd "$(dirname "$OUTPUT")" && shasum -a 256 "$(basename "$OUTPUT")") > "$OUTPUT.sha256"
elif command -v sha256sum >/dev/null 2>&1; then
    (cd "$(dirname "$OUTPUT")" && sha256sum "$(basename "$OUTPUT")") > "$OUTPUT.sha256"
else
    # Remove any stale sidecar from a prior build so the "bundle ready"
    # report doesn't mis-stamp this artifact with the prior hash.
    rm -f "$OUTPUT.sha256"
    echo "warning: no shasum/sha256sum on PATH; skipping sidecar" >&2
fi
BUNDLE_SHA256=$(awk '{print $1}' "$OUTPUT.sha256" 2>/dev/null || echo "(unavailable)")

cat <<EOF

bundle ready:
    $OUTPUT
    size:   $SIZE_HUMAN ($SIZE bytes)
    sha256: $BUNDLE_SHA256
    stamp:  $OUTPUT.sha256

next:
    bash scripts/stage_sd_card.sh /Volumes/bootfs

EOF
