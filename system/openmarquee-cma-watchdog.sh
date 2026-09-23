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
#   THRESHOLD_PCT=90    # fire at 90% of the LIVE CMA pool (default)
#   COOLDOWN_SEC=1800
#   THRESHOLD_MB=254    # OPTIONAL absolute override (wins over PCT)
#
# Bug 5 (2026-09-22): the threshold is POOL-RELATIVE by default
# (THRESHOLD_PCT% of the live CmaTotal), NOT a hardcoded MB. History:
# the absolute value was 254 (r59) on the 256M pool, bumped to 300 when
# cma went 256M->320M (GAP2, 2026-07-09) — but when the first-light fix
# dropped cma back to 256M, THAT 300 became a watchdog that can NEVER
# fire (CmaUsed caps at ~256 < 300) = dead safety machinery. A
# pool-relative default auto-tracks the pool: 90% of 256M = ~230MB, of
# 320M = ~288MB — always fires BEFORE exhaustion, whatever cma= ships.
# An explicit THRESHOLD_MB still wins as an absolute override.
# See qa/r59-cma-watchdog-default-decision-2026-06-04.md for the
# original empirical-peak reasoning.
#
# Override CmaUsed for testing via /run/openmarquee-cma-watchdog-test:
#   CMA_USED_OVERRIDE_MB=250
#
# This script is intentionally short + dependency-free (bash + awk +
# coreutils only). No python, no jq, no curl. Runs under the oneshot
# unit's restricted sandbox.

set -euo pipefail

# Bug 5 (2026-09-22): the threshold is POOL-RELATIVE by default — fire at
# THRESHOLD_PCT% of the LIVE CmaTotal — so it auto-adapts to whatever cma=
# the image ships (256M, 320M, ...) and can NEVER go stale/dead when the
# pool size changes. A hardcoded THRESHOLD_MB=300 on a 256M pool can never
# fire (CmaUsed caps at ~256 < 300) = dead safety machinery. An explicit
# THRESHOLD_MB (non-empty) still wins as an absolute operator override;
# empty/unset -> pool-relative via THRESHOLD_PCT.
THRESHOLD_MB="${THRESHOLD_MB:-}"
THRESHOLD_PCT="${THRESHOLD_PCT:-90}"
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

read_cma_total_mb() {
    # CmaTotal (MB) from MEMINFO_PATH — the size of the CMA pool the
    # kernel reserved (i.e. the cma= boot value). Missing/unreadable/0
    # -> 0, which the caller treats as "pool unknown" (no pool-relative
    # action). Bug 5 (2026-09-22): drives the pool-relative threshold so
    # the watchdog auto-adapts to whatever cma= the image ships.
    if [ ! -r "$MEMINFO_PATH" ]; then
        printf '0\n'
        return 0
    fi
    local total_kb
    total_kb=$(awk '/^CmaTotal:/ {print $2; exit}' "$MEMINFO_PATH")
    total_kb="${total_kb:-0}"
    printf '%s\n' "$((total_kb / 1024))"
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
    local cma_used_mb cma_total_mb effective_mb thr_src last_restart_epoch now elapsed
    cma_used_mb=$(read_cma_used_mb)
    cma_total_mb=$(read_cma_total_mb)

    # Effective threshold (Bug 5, 2026-09-22): an explicit THRESHOLD_MB is
    # an absolute operator override; otherwise POOL-RELATIVE — fire at
    # THRESHOLD_PCT% of the LIVE CmaTotal. Pool-relative auto-adapts to
    # the shipped cma= and can't go dead (a hardcoded 300 on a 256M pool
    # never fires). If the pool size is unknown (CmaTotal=0), we can't
    # compute a pool-relative threshold, so take no action.
    if [ -n "$THRESHOLD_MB" ]; then
        effective_mb="$THRESHOLD_MB"
        thr_src="absolute"
    elif [ "$cma_total_mb" -le 0 ]; then
        log "CmaTotal=${cma_total_mb}MB (pool unknown); cannot compute pool-relative threshold; no action"
        return 0
    else
        effective_mb=$((cma_total_mb * THRESHOLD_PCT / 100))
        thr_src="${THRESHOLD_PCT}%-of-${cma_total_mb}MB-pool"
    fi

    # Bug 5 hardening: a threshold at/above the pool ceiling can NEVER be
    # crossed (CmaUsed caps at CmaTotal) = a silently-dead watchdog. That
    # is the EXACT failure this bug fixes (absolute 300 on a 256M pool).
    # Make it LOUD in the journal instead of silent, so any future
    # misconfig (absolute THRESHOLD_MB > pool, or THRESHOLD_PCT >= 100) is
    # caught rather than sitting dead like the original did.
    if [ "$cma_total_mb" -gt 0 ] && [ "$effective_mb" -ge "$cma_total_mb" ]; then
        log "WARN: threshold ${effective_mb}MB >= CmaTotal ${cma_total_mb}MB — UNREACHABLE; watchdog can never fire (check THRESHOLD_MB/THRESHOLD_PCT)"
    fi

    last_restart_epoch=$(read_last_restart_epoch)
    now=$(date +%s)
    elapsed=$((now - last_restart_epoch))

    log "cma_used=${cma_used_mb}MB threshold=${effective_mb}MB (${thr_src}) last_restart=${elapsed}s ago"

    if [ "$cma_used_mb" -lt "$effective_mb" ]; then
        # below threshold; no action.
        return 0
    fi

    if [ "$last_restart_epoch" -gt 0 ] && [ "$elapsed" -lt "$COOLDOWN_SEC" ]; then
        log "above threshold but within cooldown (${elapsed}s < ${COOLDOWN_SEC}s); skip"
        return 0
    fi

    log "triggered restart (cma_used=${cma_used_mb}MB >= ${effective_mb}MB, ${thr_src})"
    trigger_restart
}

main "$@"
