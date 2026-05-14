#!/usr/bin/env bash
# scripts/download-emoji-font.sh — fetch Noto Color Emoji at build time.
#
# Idempotent. Bundling the ~10 MB color-emoji TTF in git would inflate
# every clone forever, but operators and the editor preview both need
# the file present at ui/fonts/noto-color-emoji.ttf:
#
#   - device-side renderer (backend/openmarquee/seed.py:_load_emoji_font)
#     loads it via Pillow when emoji codepoints appear in slide text
#   - editor canvas paints emoji via the same TTF through an @font-face
#     declaration so editor preview matches what the device renders
#
# Pinned to a specific googlefonts/noto-emoji release tag + verified
# against a known sha256 so a hijacked CDN can't swap in a different
# (or weaponized) font during install.
#
# Run from the repo root, or via scripts/setup.sh which calls it.

set -euo pipefail

# Pin the release tag + sha256 here. Bump deliberately when refreshing.
NOTO_EMOJI_TAG="v2.047"
NOTO_EMOJI_URL="https://github.com/googlefonts/noto-emoji/raw/${NOTO_EMOJI_TAG}/fonts/NotoColorEmoji.ttf"
NOTO_EMOJI_SHA256="39ee3c587e10e89669b9ff32703261d10d5f9c4dd5ad147b6b5a1c5200591817"

# Resolve repo root from the script's path so this works whether the
# caller is in the repo root, scripts/, or some BUILD_DIR mirror.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="$REPO_ROOT/ui/fonts"
DEST_FILE="$DEST_DIR/noto-color-emoji.ttf"

mkdir -p "$DEST_DIR"

# Idempotency: if a file already exists at DEST_FILE and matches the
# pinned sha, skip the download. Lets `setup.sh` re-run cheap.
if [ -f "$DEST_FILE" ]; then
    actual_sha=$(shasum -a 256 "$DEST_FILE" | awk '{print $1}')
    if [ "$actual_sha" = "$NOTO_EMOJI_SHA256" ]; then
        echo "==> Noto Color Emoji already at $DEST_FILE (sha matches; skipping)"
        exit 0
    else
        echo "==> Noto Color Emoji at $DEST_FILE has unexpected sha; re-downloading"
        rm -f "$DEST_FILE"
    fi
fi

echo "==> downloading Noto Color Emoji ${NOTO_EMOJI_TAG} (~10 MB)"
echo "    from: $NOTO_EMOJI_URL"

# --retry handles transient flakes; --fail surfaces non-200 as exit code.
# Stage to a .tmp file so an interrupted download doesn't leave a half-
# valid TTF that the next run sees and trusts.
TMP_FILE="${DEST_FILE}.tmp"
trap 'rm -f "$TMP_FILE"' EXIT
curl --fail --silent --show-error --location --retry 3 \
    --output "$TMP_FILE" "$NOTO_EMOJI_URL"

actual_sha=$(shasum -a 256 "$TMP_FILE" | awk '{print $1}')
if [ "$actual_sha" != "$NOTO_EMOJI_SHA256" ]; then
    echo "ERR: Noto Color Emoji sha mismatch" >&2
    echo "  expected: $NOTO_EMOJI_SHA256" >&2
    echo "  actual:   $actual_sha" >&2
    echo "  url:      $NOTO_EMOJI_URL" >&2
    echo "  Bump NOTO_EMOJI_SHA256 in $0 if you intentionally moved the tag." >&2
    exit 1
fi

mv "$TMP_FILE" "$DEST_FILE"
trap - EXIT

echo "==> Noto Color Emoji installed at $DEST_FILE"
