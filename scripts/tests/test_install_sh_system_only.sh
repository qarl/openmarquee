#!/usr/bin/env bash
# scripts/tests/test_install_sh_system_only.sh
#
# deploy-hygiene 2026-07-16 (Phase 2). Guards two install.sh additions that
# close the binary-only-deploy drift (docs/deploy-hygiene-audit-2026-07-16.md):
#
#   1. --system-only: install ONLY the OS-config sections, SKIP the §2 Python
#      venv + pip (backend app layer). scripts/sync-system-to-sign.sh relies on
#      this to refresh system/ over the quiet link without the heavy venv work.
#
#   2. §3b staging/live REVERT GUARD: promote the staged renderer binary to the
#      live path ONLY when staging is newer than live — so a later install.sh
#      can't revert a binary hand-deployed via qa/deploy-to-sign.sh (which
#      writes live directly, never refreshing staging).
#
# STATIC parse + --dry-run only (install.sh's privileged ops never run; --root
# forces the filesystem paths into a tmpdir and install.sh refuses --root
# without --dry-run). The guard's runtime skip branch is DRY_RUN-gated off, so
# its decision logic is unit-tested standalone here against fixture mtimes and
# tied back to install.sh by a static assertion that the guard condition exists.
#
# Run:
#     bash scripts/tests/test_install_sh_system_only.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="${SCRIPT_DIR}/../install.sh"
PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1" >&2; }

[ -f "$INSTALL_SH" ] || { echo "FAIL: install.sh not found at $INSTALL_SH" >&2; exit 1; }

# --- 1. static: the flag, the §2 guard, and the §3b revert guard exist ------
grep -qE -- '--system-only\)[[:space:]]+SYSTEM_ONLY=1' "$INSTALL_SH" \
    && ok "parser accepts --system-only" \
    || bad "parser does not accept --system-only"

grep -qE 'if \[ "\$SYSTEM_ONLY" -eq 1 \]; then' "$INSTALL_SH" \
    && ok "§2 venv block is gated on SYSTEM_ONLY" \
    || bad "§2 venv block is NOT gated on SYSTEM_ONLY"

# The revert guard: SKIP promote iff not-dry AND live exists AND staging is NOT
# newer than live. Match the FULL condition incl. the leading `!` and operand
# order — a dropped `!` (inverted guard) or swapped operands would still match a
# looser `-nt` substring but is a shipped-inverted-guard bug (sacred review
# 2026-07-16). The runtime branch is DRY_RUN-gated off, so this static assertion
# is the guard against inversion; §4 below unit-tests the decision logic.
grep -qF '[ "$DRY_RUN" -eq 0 ] && [ -f "$RUST_BIN_INSTALLED" ] && [ ! "$RUST_BIN_STAGED" -nt "$RUST_BIN_INSTALLED" ]' "$INSTALL_SH" \
    && ok "§3b revert guard has the exact SKIP condition (not inverted)" \
    || bad "§3b revert guard condition missing/altered (inverted negation or swapped operands?)"

# --- 2. dynamic: --system-only --dry-run SKIPS venv, KEEPS system sections --
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SYSONLY_OUT="$(bash "$INSTALL_SH" --system-only --dry-run --root "$TMP/sysonly" 2>&1 || true)"
FULL_OUT="$(bash "$INSTALL_SH" --dry-run --root "$TMP/full" 2>&1 || true)"

# NB: here-strings (grep <<<"$VAR"), NOT `echo "$VAR" | grep -q`. Under
# `set -o pipefail`, grep -q exits on first match and closes the pipe, so echo
# hits EPIPE (non-zero) and pipefail reports the whole pipeline as failed —
# a false negative. Here-strings have no pipe.
grep -q 'system files only' <<<"$SYSONLY_OUT" \
    && ok "--system-only announces the §2 skip" \
    || bad "--system-only did NOT announce the §2 skip"

grep -q 'Ensure Python venv at' <<<"$SYSONLY_OUT" \
    && bad "--system-only STILL ran the venv section" \
    || ok "--system-only skipped 'Ensure Python venv'"

grep -q 'Install backend package into venv' <<<"$SYSONLY_OUT" \
    && bad "--system-only STILL ran 'pip install -e .'" \
    || ok "--system-only skipped the backend pip install"

# System-config sections must STILL run under --system-only.
for marker in 'Install systemd units' 'Stage openmarquee-netctl-daemon' 'Stage hostapd.conf' 'Reload systemd'; do
    grep -q "$marker" <<<"$SYSONLY_OUT" \
        && ok "--system-only still runs: $marker" \
        || bad "--system-only DROPPED a system section: $marker"
done

# --- 3. control: a FULL --dry-run DOES run the venv section ------------------
grep -q 'Ensure Python venv at' <<<"$FULL_OUT" \
    && ok "full run still runs the venv section" \
    || bad "full run unexpectedly skipped the venv section"
grep -q 'system files only' <<<"$FULL_OUT" \
    && bad "full run wrongly announced the §2 skip" \
    || ok "full run does not announce the §2 skip"

# --- 4. unit: the §3b revert-guard decision (mirrors install.sh's condition) -
# SKIP promote iff: not-dry AND live exists AND staging NOT newer than live.
should_skip() {  # args: dry live_exists staged_newer  -> echoes SKIP|PROMOTE
    local dry="$1" live="$2/live" staged="$2/staged"
    : > "$staged"
    if [ "$3" = live-absent ]; then
        rm -f "$live"
    elif [ "$3" = staged-newer ]; then
        : > "$live"; sleep 1; : > "$staged"   # staged mtime > live
    else # staged-older
        : > "$staged"; sleep 1; : > "$live"    # live mtime > staged
    fi
    if [ "$dry" -eq 0 ] && [ -f "$live" ] && [ ! "$staged" -nt "$live" ]; then
        echo SKIP
    else
        echo PROMOTE
    fi
}

G="$TMP/guard"; mkdir -p "$G"
[ "$(should_skip 0 "$G" staged-older)" = SKIP ] \
    && ok "guard: staged older than live -> SKIP (no revert)" \
    || bad "guard: staged older than live should SKIP"
[ "$(should_skip 0 "$G" staged-newer)" = PROMOTE ] \
    && ok "guard: staged newer than live -> PROMOTE" \
    || bad "guard: staged newer than live should PROMOTE"
[ "$(should_skip 0 "$G" live-absent)" = PROMOTE ] \
    && ok "guard: live absent -> PROMOTE (first install)" \
    || bad "guard: live absent should PROMOTE"
[ "$(should_skip 1 "$G" staged-older)" = PROMOTE ] \
    && ok "guard: --dry-run always PROMOTE (prints intended action)" \
    || bad "guard: --dry-run should PROMOTE"

echo
echo "== test_install_sh_system_only: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
