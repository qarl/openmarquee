#!/usr/bin/env bash
# openmarquee-boot-gesture.sh — the "3× power-cycle → setup mode" recovery
# gesture (recovery cluster A1, qarl handover 2026-07-08).
#
# The problem: a sign whose home wifi credentials went stale (router
# replaced, password changed) can become unreachable — no network path to
# the backend, and Setup Mode only auto-arms on a *detected* wifi loss,
# which a mis-configured-but-"successful" association won't trigger. The
# operator needs a no-console, no-cable way to force the sign back into
# Setup Mode. The universal gesture is: power-cycle it a few times fast.
#
# Mechanism (the classic "reset by power-cycling N times"):
#   * A DISK counter at $COUNT_FILE survives power cuts (a hard power pull
#     wipes tmpfs, so the counter CANNOT live in /run).
#   * `increment` runs as a boot oneshot BEFORE the backend: count++, and
#     if the count reaches $THRESHOLD it writes the tmpfs boot-hint
#     ($HINT_FILE = "setup") + resets the counter.
#   * `clear` runs ~20s after boot (openmarquee-boot-gesture-clear.timer):
#     it zeroes the counter AND removes the hint. So a boot that stays up
#     past the clear window counts as "stable" and does NOT accumulate;
#     only boots cut short (power pulled < 20s) stack toward the threshold.
#   * The backend reads the hint at supervisor startup and fires
#     OPERATOR_REQUESTED_SETUP_MODE (openmarquee/boot_hint.py).
#
# So THREE rapid power cycles (each interrupted before the ~20s stable
# mark) land the sign in Setup Mode on the third boot; normal reboots,
# however many, never do (each one clears at +20s).
#
# Paths are env-overridable so the logic is unit-testable off-device.

set -euo pipefail

COUNT_FILE="${OPENMARQUEE_BOOT_CYCLE_COUNT_FILE:-/var/openmarquee/boot-cycle-count}"
HINT_FILE="${OPENMARQUEE_BOOT_HINT_FILE:-/run/openmarquee/boot-hint}"
THRESHOLD="${OPENMARQUEE_BOOT_GESTURE_THRESHOLD:-3}"

log() {
    # logger → journal on-device; also echo so unit tests / dry runs can
    # see it. `|| true` so a logger-less test host doesn't trip set -e.
    logger -t openmarquee-boot-gesture "$1" 2>/dev/null || true
    echo "openmarquee-boot-gesture: $1" >&2
}

read_count() {
    local c
    c=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
    # Guard against a corrupt/blank file — treat anything non-numeric as 0.
    if [[ "$c" =~ ^[0-9]+$ ]]; then
        echo "$c"
    else
        echo 0
    fi
}

write_atomic() {
    # write_atomic <path> <content> — same-dir tempfile + mv so a power
    # cut mid-write can't leave a torn counter/hint file.
    local path="$1" content="$2" dir tmp
    dir=$(dirname "$path")
    mkdir -p "$dir"
    tmp=$(mktemp -p "$dir" ".openmarquee-boot-gesture.XXXXXX")
    printf '%s\n' "$content" >"$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$path"
    # Flush to stable storage NOW. Without this the counter only becomes
    # power-safe up to ext4's ~5s commit interval later — but the whole
    # point of the disk counter is to survive an IMMEDIATE power pull, so
    # a deferred commit would silently drop the cycle we just counted.
    # Whole-fs sync (no file arg) for portability (BSD/macOS test hosts);
    # cheap enough for a once-per-boot op. The hint is on tmpfs where this
    # is a near no-op — harmless.
    sync 2>/dev/null || true
}

case "${1:-}" in
    increment)
        count=$(read_count)
        count=$((count + 1))
        if [ "$count" -ge "$THRESHOLD" ]; then
            write_atomic "$HINT_FILE" "setup"
            write_atomic "$COUNT_FILE" "0"
            log "power-cycle gesture FIRED (>= ${THRESHOLD} rapid cycles): wrote setup hint at ${HINT_FILE}, reset counter"
        else
            write_atomic "$COUNT_FILE" "$count"
            log "boot-cycle count = ${count} (threshold ${THRESHOLD}); pull power again before the sign settles to escalate"
        fi
        ;;
    clear)
        # Stable boot: the sign made it past the clear window. Zero the
        # counter so ordinary reboots never accumulate, and drop any hint
        # (a hint already consumed by the backend at startup; removing it
        # here prevents a later backend restart from re-reading it).
        write_atomic "$COUNT_FILE" "0"
        rm -f "$HINT_FILE"
        log "stable boot: cleared boot-cycle counter + removed any setup hint"
        ;;
    *)
        echo "usage: openmarquee-boot-gesture {increment|clear}" >&2
        exit 64  # EX_USAGE
        ;;
esac
