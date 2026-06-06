#!/usr/bin/env bash
# scripts/check_runtime_assets.sh — verify required runtime assets are
# present on a deployed openMarquee target Pi.
#
# Usage:
#   bash scripts/check_runtime_assets.sh <ssh-target>
#
# What it does:
#   sshes into TARGET and verifies that /opt/openmarquee/ui/fonts/
#   noto-color-emoji-colrv1.ttf (a) exists and (b) is larger than 1 MB.
#   Exits non-zero with a specific, actionable error if either check
#   fails.
#
# Why this exists (r67, 2026-06-05):
#   At least three times in the 2026-06-05 session, the COLRv1 emoji
#   font has gone missing on FYS prod and the renderer silently
#   substituted MSDF tofu glyphs on emoji slides. The shape:
#       ui/fonts/noto-color-emoji-colrv1.ttf is gitignored (~4.8 MB
#       binary downloaded by scripts/download-emoji-font-colrv1.sh).
#       A partial-rsync / --delete / manual rm on the target deletes
#       it. The runtime COLR dispatch at hdmi_logic.rs:733 then can't
#       load the font → rasterize_colr_cell returns Err → the worker
#       direct-inserts FontMissing → emoji renders as Tofu via the
#       MSDF fallback. No error log, no health check.
#
#   r32 added the prep-side download to deploy.sh; r67 closes the
#   loop with a post-rsync TARGET-side verification gate so a
#   tofu-shipping deploy fails loud at the dev host instead of
#   silently corrupting production.
#
# Called from scripts/deploy.sh AFTER all rsync steps but BEFORE the
# install.sh restart. Standalone-callable too (operators can run it
# to verify an already-deployed Pi without redeploying).
#
# Env overrides:
#   OPENMARQUEE_REMOTE_ROOT  defaults to /opt/openmarquee

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "usage: $0 <ssh-target>" >&2
    exit 64
fi

TARGET="$1"
REMOTE_ROOT="${OPENMARQUEE_REMOTE_ROOT:-/opt/openmarquee}"
FONT_PATH="$REMOTE_ROOT/ui/fonts/noto-color-emoji-colrv1.ttf"

# 1 MB minimum. The real COLRv1 font is ~4.8 MB after SVG-table
# strip (scripts/download-emoji-font-colrv1.sh:86), so 1 MB is a
# comfortable floor that catches 0-byte stubs / truncated downloads
# without false-positiving on a future smaller variant.
MIN_SIZE=1048576

# Single ssh round trip. `-f` first so a missing file returns the
# literal "missing" sentinel instead of wc's "No such file" stderr
# leaking through. The grep at the end is r67 subagent NIT-2
# defense: if the target's login shell rc-files write anything to
# stdout (a banner, a `cd` chatter, etc.), `wc -c <` output would
# be polluted, `tr -d` would leave non-numeric text, and the
# `-lt` integer compare below would abort under set -e with an
# unhelpful "integer expression expected". Keep only lines that
# match the sentinel grammar.
size=$(ssh "$TARGET" "if [ -f '$FONT_PATH' ]; then wc -c < '$FONT_PATH'; else echo missing; fi" \
    | tr -d ' ' \
    | grep -E '^[0-9]+$|^missing$' \
    | head -1)
if [ -z "${size:-}" ]; then
    echo "ERROR: could not determine size of $FONT_PATH on $TARGET" >&2
    echo "       (ssh returned no recognizable size/sentinel output)" >&2
    exit 1
fi

if [ "$size" = "missing" ]; then
    cat >&2 <<EOF
ERROR: COLRv1 emoji font missing at $FONT_PATH
       emoji slides will render as tofu (FontMissing -> MSDF fallback).
       Fix: run scripts/download-emoji-font-colrv1.sh on the dev host,
            then re-run: bash scripts/deploy.sh $TARGET
EOF
    exit 1
fi

if [ "$size" -lt "$MIN_SIZE" ]; then
    cat >&2 <<EOF
ERROR: COLRv1 emoji font at $FONT_PATH is only $size bytes (< $MIN_SIZE).
       Likely a 0-byte stub or truncated download.
       Fix: run scripts/download-emoji-font-colrv1.sh on the dev host,
            then re-run: bash scripts/deploy.sh $TARGET
EOF
    exit 1
fi

echo "==> runtime asset check OK (noto-color-emoji-colrv1.ttf: $size bytes)"
