#!/usr/bin/env bash
# scripts/tests/test_install_sh_section_order.sh
#
# r29 (2026-05-31) regression guard: install.sh sections must appear
# in the deploy-resilient order where the failure-tolerant sections
# (renderer binary install §3b + systemd unit install §3) run BEFORE
# the failure-prone section (Python venv + pip install §2).
#
# Before r29, install.sh ordered §2 → §3 → §3a → §3b → ... → §8;
# r28's FYS v1.0.0 deploy (2026-05-31) hit a pip resolver failure at
# §2, the EXIT trap fired, and §3b's renderer binary cp + §8's
# backend restart never ran. Manual scp + stop/cp/start was the
# recovery. r29 reordered so §3 + §3a + §3b run before §2 — a
# transient pip flake now leaves the renderer binary updated and
# the systemd unit updated, with only the end-of-script restart
# deferred to the next install.sh run.
#
# This test catches an accidental reorder back to the broken state
# (or a refactor that moves the section markers without preserving
# the invariant). It is a STATIC PARSE — no `bash install.sh`
# execution; no privileged operations; safe to run in any CI.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="${SCRIPT_DIR}/../install.sh"

if [ ! -f "$INSTALL_SH" ]; then
    echo "FAIL: install.sh not found at $INSTALL_SH" >&2
    exit 1
fi

# Extract first-line-number of each section marker.
get_line() {
    grep -n "$1" "$INSTALL_SH" | head -1 | cut -d: -f1
}

PIP_LINE=$(get_line '^# --- 2\. Python venv')
SYSTEMD_LINE=$(get_line '^# --- 3\. Systemd unit files')
CHMOD_LINE=$(get_line '^# --- 3a\. Ensure +x on system/\*\.sh helpers')
RUST_LINE=$(get_line '^# --- 3b\. Rust IPC sidecar binary')

# All four markers must exist.
for var in PIP_LINE SYSTEMD_LINE CHMOD_LINE RUST_LINE; do
    val="${!var}"
    if [ -z "$val" ]; then
        echo "FAIL: $var section marker not found in install.sh" >&2
        echo "  (the test or install.sh has drifted; if the section was renamed," >&2
        echo "   update the get_line regex; otherwise re-add the section)." >&2
        exit 1
    fi
done

# r29 invariant: §3, §3a, §3b all appear BEFORE §2.
fail=0
if [ "$SYSTEMD_LINE" -ge "$PIP_LINE" ]; then
    echo "FAIL: §3 'Systemd unit files' (line $SYSTEMD_LINE) appears AFTER §2 'Python venv' (line $PIP_LINE)" >&2
    fail=1
fi
if [ "$CHMOD_LINE" -ge "$PIP_LINE" ]; then
    echo "FAIL: §3a 'Ensure +x' (line $CHMOD_LINE) appears AFTER §2 'Python venv' (line $PIP_LINE)" >&2
    fail=1
fi
if [ "$RUST_LINE" -ge "$PIP_LINE" ]; then
    echo "FAIL: §3b 'Rust IPC sidecar binary' (line $RUST_LINE) appears AFTER §2 'Python venv' (line $PIP_LINE)" >&2
    fail=1
fi

if [ "$fail" -eq 1 ]; then
    echo >&2
    echo "  r29 invariant: §3 + §3a + §3b must precede §2 so a pip" >&2
    echo "  failure doesn't kill the renderer-binary rollover. See" >&2
    echo "  install.sh:259 meta-comment + feedback_install_sh_pip_blocks_renderer.md." >&2
    exit 1
fi

echo "OK: install.sh section order respects r29 invariant"
echo "  §3  Systemd unit files:       line $SYSTEMD_LINE"
echo "  §3a Ensure +x:                line $CHMOD_LINE"
echo "  §3b Rust IPC sidecar binary:  line $RUST_LINE"
echo "  §2  Python venv (pip):        line $PIP_LINE"
