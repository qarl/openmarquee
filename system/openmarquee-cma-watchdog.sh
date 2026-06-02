#!/usr/bin/env bash
# system/openmarquee-cma-watchdog.sh -- CMA-pressure stopgap watchdog.
#
# Reads /proc/meminfo (CmaTotal - CmaFree = CmaUsed). If above
# THRESHOLD_MB and outside the cooldown window, runs
# `systemctl restart --no-block openmarquee-backend.service` to
# release the renderer subprocess's GBM/V4L2/EGLImage/GLES
# allocations on the CMA pool.
#
# Bridges until r38b's actual leak fix lands. See
# qa/r38c-cma-pressure-watchdog-2026-06-02.md.
#
# Configurable via /etc/default/openmarquee-cma-watchdog:
#   THRESHOLD_MB=220
#   COOLDOWN_SEC=1800
#
# Override CmaUsed for testing via /run/openmarquee-cma-watchdog-test:
#   CMA_USED_OVERRIDE_MB=250
#
# This script is intentionally short + dependency-free (bash + awk +
# coreutils only). No python, no jq, no curl. Runs under the oneshot
# unit's restricted sandbox.

set -euo pipefail

THRESHOLD_MB="${THRESHOLD_MB:-220}"
COOLDOWN_SEC="${COOLDOWN_SEC:-1800}"
STATE_FILE="${STATE_FILE:-/var/openmarquee/cma-watchdog-state}"
MEMINFO_PATH="${MEMINFO_PATH:-/proc/meminfo}"
OVERRIDE_PATH="${OVERRIDE_PATH:-/run/openmarquee-cma-watchdog-test}"
RESTART_TARGET="${RESTART_TARGET:-openmarquee-backend.service}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"

# /etc/default/openmarquee-cma-watchdog supplies operator overrides.
# Sourced unconditionally; missing file is a no-op.
DEFAULTS="${DEFAULTS:-/etc/default/openmarquee-cma-watchdog}"
if [ -r "$DEFAULTS" ]; then
    # shellcheck disable=SC1090
    . "$DEFAULTS"
fi

log() {
    # journald via stderr; the oneshot service captures stderr to the
    # journal by default.
    printf 'cma-watchdog: %s\n' "$*" >&2
}

read_cma_used_mb() {
    # Test override: if /run/openmarquee-cma-watchdog-test sets
    # CMA_USED_OVERRIDE_MB, return that instead of reading
    # /proc/meminfo. Operators (or scripts/tests/) write this file
    # to inject a high CmaUsed reading without modifying /proc.
    if [ -r "$OVERRIDE_PATH" ]; then
        # shellcheck disable=SC1090
        . "$OVERRIDE_PATH"
        if [ -n "${CMA_USED_OVERRIDE_MB:-}" ]; then
            log "using CMA_USED_OVERRIDE_MB=${CMA_USED_OVERRIDE_MB} from $OVERRIDE_PATH"
            printf '%s\n' "$CMA_USED_OVERRIDE_MB"
            return 0
        fi
    fi

    if [ ! -r "$MEMINFO_PATH" ]; then
        log "ERROR: $MEMINFO_PATH unreadable; reporting cma_used=0"
        printf '0\n'
        return 0
    fi

    # /proc/meminfo lines look like:
    #   CmaTotal:         262144 kB
    #   CmaFree:           70000 kB
    # Each field is a whole number of kB. Missing key → 0.
    local total_kb free_kb
    total_kb=$(awk '/^CmaTotal:/ {print $2; exit}' "$MEMINFO_PATH")
    free_kb=$(awk '/^CmaFree:/ {print $2; exit}' "$MEMINFO_PATH")
    total_kb="${total_kb:-0}"
    free_kb="${free_kb:-0}"

    if [ "$total_kb" -eq 0 ]; then
        log "WARN: CmaTotal=0 in $MEMINFO_PATH (kernel without CMA support?); reporting cma_used=0"
        printf '0\n'
        return 0
    fi

    # saturating subtraction: in the (impossible-but-defensive) case
    # free > total, clamp to 0 rather than wrap.
    local used_kb
    if [ "$free_kb" -ge "$total_kb" ]; then
        used_kb=0
    else
        used_kb=$((total_kb - free_kb))
    fi
    printf '%s\n' "$((used_kb / 1024))"
}

read_last_restart_epoch() {
    # State file is one line: "last_restart_epoch=NNNNNNNNN".
    # Unparseable / missing → 0 (treated as "no prior restart").
    if [ ! -r "$STATE_FILE" ]; then
        printf '0\n'
        return 0
    fi
    local val
    val=$(awk -F= '/^last_restart_epoch=/ {print $2; exit}' "$STATE_FILE")
    val="${val:-0}"
    # Numeric sanity: if not a number, treat as 0.
    case "$val" in
        ''|*[!0-9]*) printf '0\n' ;;
        *) printf '%s\n' "$val" ;;
    esac
}

write_last_restart_epoch() {
    local epoch="$1"
    mkdir -p "$(dirname "$STATE_FILE")"
    # Atomic write via tmp + mv. State file is 1 line; corruption
    # window is the rename, which is atomic on the same filesystem.
    printf 'last_restart_epoch=%s\n' "$epoch" > "${STATE_FILE}.tmp"
    mv "${STATE_FILE}.tmp" "$STATE_FILE"
}

trigger_restart() {
    local epoch
    epoch=$(date +%s)
    write_last_restart_epoch "$epoch"
    log "triggering: $SYSTEMCTL restart --no-block $RESTART_TARGET"
    if ! "$SYSTEMCTL" restart --no-block "$RESTART_TARGET"; then
        log "ERROR: systemctl restart failed; state file still updated to prevent immediate retry"
        return 1
    fi
    return 0
}

main() {
    local cma_used_mb last_restart_epoch now elapsed
    cma_used_mb=$(read_cma_used_mb)
    last_restart_epoch=$(read_last_restart_epoch)
    now=$(date +%s)
    elapsed=$((now - last_restart_epoch))

    log "cma_used=${cma_used_mb}MB threshold=${THRESHOLD_MB}MB last_restart=${elapsed}s ago"

    if [ "$cma_used_mb" -lt "$THRESHOLD_MB" ]; then
        # below threshold; no action.
        return 0
    fi

    if [ "$last_restart_epoch" -gt 0 ] && [ "$elapsed" -lt "$COOLDOWN_SEC" ]; then
        log "above threshold but within cooldown (${elapsed}s < ${COOLDOWN_SEC}s); skip"
        return 0
    fi

    log "triggered restart (cma_used=${cma_used_mb}MB >= ${THRESHOLD_MB}MB)"
    trigger_restart
}

main "$@"
