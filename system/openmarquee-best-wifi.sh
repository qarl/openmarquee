#!/usr/bin/env bash
# system/openmarquee-best-wifi.sh — scan visible WiFi networks, pick
# the strongest known SSID, and connect to it. Driven by both a
# boot-time oneshot service and a recurring timer; idempotent so the
# timer is safe to fire as often as the operator likes.
#
# r60 (2026-06-04): qarl spec: "create a system where it scans all
# the network/passwords it knows and connects to the best." The Pi
# was auto-picking NEBULA @ -70 dBm / 0.5 Mbit when an alternative
# known network (`qarl`) might have a stronger signal. This script
# closes that gap.
#
# Topology: openMarquee runs on Pi Zero 2 W with the onboard wlan0
# in STA mode. The previous dual-mode (AP on ap0 + STA on wlan0)
# brcmfmac stack was unstable per r43; r60 ships AP off by default
# and treats wlan0 as the single radio.
#
# Safety contract (per dispatch):
#   * NEVER leave the Pi disconnected. If activating a new SSID
#     fails for any reason, fall back to whatever was active before
#     OR no-op.
#   * Hysteresis: don't switch unless the candidate's signal is
#     strictly better than the current's by HYSTERESIS_DBM (default
#     8 dB). Prevents oscillation between two SSIDs of similar
#     strength.
#   * In-band ssh safety: a `nmcli connection up` swap drops the
#     current connection. If we ssh in via the same wlan0 we may
#     time out mid-operation. The fallback below catches this --
#     post-switch we verify reachability + revert on failure.
#   * Idempotent: re-running on the already-best SSID is a no-op.
set -euo pipefail

LOG_TAG="openmarquee-best-wifi"
# r60 subagent (WARN): nmcli's SIGNAL column is a 0-100 NORMALIZED
# percentage (NM scales the kernel's RSSI dBm through a quality
# curve), not raw dBm. The threshold is 8 percentage-points by
# default -- conservative enough to suppress oscillation between
# APs within typical 5-10 point of one another. Keep the env-var
# name aligned with the units it actually represents.
HYSTERESIS_SIGNAL="${HYSTERESIS_SIGNAL:-${HYSTERESIS_DBM:-8}}"
# Skip switching if the new connection doesn't ping the gateway
# within this budget after activation. Generous default so flaky
# DHCP doesn't trigger a revert.
POST_SWITCH_VERIFY_TIMEOUT="${POST_SWITCH_VERIFY_TIMEOUT:-30}"
# nmcli rescan can take 5-15s on bcm43438; cap so a hung rescan
# doesn't pin the timer fire.
RESCAN_TIMEOUT="${RESCAN_TIMEOUT:-20}"

log() {
    # logger -> journald with the tag we register, plus echo to
    # stderr so the boot oneshot's journal carries the same lines
    # the timer recurrences emit.
    local msg="$*"
    logger -t "$LOG_TAG" "$msg" 2>/dev/null || true
    echo "$LOG_TAG: $msg" >&2
}

# r60 subagent (WARN): nmcli -t defaults to escaping literal colons
# in field values as `\:`, which our awk -F: splits then breaks the
# comparison. `-e no` disables the escape (nmcli 1.36+, shipping on
# Pi OS Trixie). Without this, SSIDs containing ':' (hotel guest
# networks like 'Marriott:Guest') would never be matched.
NMCLI_TERSE="nmcli -t -e no"

# Return space-separated list of SSIDs we have credentials for in
# NetworkManager. Filters to wifi-typed connections only; lowercase
# the type so a mixed-case stock NM doesn't drop a profile.
#
# r60 subagent (WARN): drop `exit` from terminating awk blocks to
# avoid SIGPIPE-141 under `set -o pipefail`. nmcli output can be
# longer than the pipe buffer when many wifi profiles exist; an
# awk that exits early closes the pipe and nmcli exits 141, which
# pipefail then propagates. Using `END { print best }` instead of
# `{ print; exit }` keeps the pipe drained.
known_ssids() {
    # nmcli -t with field selection puts each connection on its own
    # `name:type:ssid` line. We want the ssid; some profiles store
    # an empty SSID and reuse the connection name -- fall back to
    # NAME when 802-11-wireless.ssid is empty.
    $NMCLI_TERSE -f NAME,TYPE,802-11-wireless.ssid connection show 2>/dev/null \
        | awk -F: '
            tolower($2) == "802-11-wireless" {
                ssid = ($3 != "") ? $3 : $1
                print ssid
            }' \
        | sort -u
}

# Currently-active SSID on any interface. Returns empty string if
# nothing is connected.
current_ssid() {
    $NMCLI_TERSE -f IN-USE,SSID,DEVICE device wifi list 2>/dev/null \
        | awk -F: '
            $1 == "*" && first == "" { first = $2 }
            END { if (first != "") print first }'
}

# Signal strength (0-100, nmcli's normalized scale) for an SSID in
# the most recent scan. Empty if not visible. We take the MAX
# across BSSIDs so an SSID with two APs reports the strongest.
ssid_signal() {
    local ssid="$1"
    $NMCLI_TERSE -f SSID,SIGNAL device wifi list 2>/dev/null \
        | awk -F: -v want="$ssid" '
            $1 == want {
                if ($2+0 > best) best = $2+0
            }
            END { if (best > 0) print best }'
}

# Probe gateway reachability. Returns 0 if any of the listed test
# targets responds. Used post-switch to verify we didn't lock
# ourselves out.
verify_reachability() {
    local deadline=$(( $(date +%s) + POST_SWITCH_VERIFY_TIMEOUT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        # Default gateway -- the canonical "we have a network" test.
        local gw
        gw=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
        if [ -n "$gw" ]; then
            if ping -c 1 -W 2 -- "$gw" >/dev/null 2>&1; then
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

main() {
    # Trigger a fresh scan. NM's cached scan can be stale by minutes;
    # for the boot path we want a fresh look, and for the timer path
    # a fresh look is cheap insurance against stale signal numbers.
    # --rescan auto is idempotent: NM dedupes back-to-back rescan
    # requests.
    log "starting; rescanning wifi"
    timeout "$RESCAN_TIMEOUT" nmcli device wifi rescan 2>/dev/null || true
    # NM rescan is async -- the scan results land in the list output
    # over the next 1-5 seconds. Give it a beat before reading.
    sleep 3

    local known
    known=$(known_ssids)
    if [ -z "$known" ]; then
        log "no known wifi profiles configured; nothing to do"
        return 0
    fi
    log "known wifi profiles: $(echo "$known" | tr '\n' ' ')"

    # Find the strongest visible known SSID + its signal.
    local best_ssid=""
    local best_signal=0
    while IFS= read -r ssid; do
        [ -z "$ssid" ] && continue
        local signal
        signal=$(ssid_signal "$ssid")
        if [ -n "$signal" ]; then
            log "  visible: $ssid signal=$signal"
            if [ "$signal" -gt "$best_signal" ]; then
                best_signal=$signal
                best_ssid=$ssid
            fi
        fi
    done <<< "$known"

    if [ -z "$best_ssid" ]; then
        log "no known SSID visible in scan; leaving network state untouched"
        return 0
    fi

    local current
    current=$(current_ssid)
    log "current=$current best_visible=$best_ssid (signal=$best_signal)"

    # No-op: already on the best.
    if [ "$current" = "$best_ssid" ]; then
        log "already on best known SSID; no switch"
        return 0
    fi

    # Hysteresis: only switch if the candidate beats current by the
    # threshold. If we're not currently connected (current empty),
    # the threshold check is skipped -- any visible known SSID is
    # better than nothing.
    if [ -n "$current" ]; then
        local current_signal
        current_signal=$(ssid_signal "$current")
        # If we can't read the current signal (interface state
        # weird, current SSID not in scan), assume it's poor and
        # proceed with the switch. Better to move than stay stuck.
        if [ -n "$current_signal" ]; then
            local delta=$(( best_signal - current_signal ))
            if [ "$delta" -lt "$HYSTERESIS_SIGNAL" ]; then
                log "candidate $best_ssid only $delta pts better than $current; below hysteresis ($HYSTERESIS_SIGNAL); staying put"
                return 0
            fi
            log "candidate $best_ssid is $delta pts better than $current; switching"
        else
            log "current SSID signal unreadable; switching to $best_ssid"
        fi
    fi

    # Switch. nmcli connection up takes the NAME from the connection
    # profile, not the SSID -- they're usually identical but may
    # differ. Look up the connection NAME for this SSID; fall back
    # to the SSID string itself (NM matches case-sensitively).
    local conn_name
    conn_name=$($NMCLI_TERSE -f NAME,802-11-wireless.ssid,TYPE connection show 2>/dev/null \
        | awk -F: -v want="$best_ssid" '
            tolower($3) == "802-11-wireless" {
                ssid = ($2 != "") ? $2 : $1
                if (ssid == want && first == "") { first = $1 }
            }
            END { if (first != "") print first }')
    if [ -z "$conn_name" ]; then
        # Fallback: assume connection NAME == SSID.
        conn_name=$best_ssid
    fi

    log "activating connection: $conn_name (ssid=$best_ssid)"
    if ! nmcli connection up id "$conn_name" 2>&1 | sed "s|^|$LOG_TAG:   nmcli: |" >&2; then
        log "ERROR: nmcli connection up failed for $conn_name; leaving prior state"
        # Lockout-safety: nmcli's failed `up` may have torn down the
        # prior connection mid-switch. Try to restore the previous
        # SSID if we knew it.
        if [ -n "$current" ]; then
            log "  attempting to restore prior connection: $current"
            nmcli connection up id "$current" 2>/dev/null || true
        fi
        return 1
    fi

    # Verify we have network reachability via the new connection.
    # If not, fall back.
    if verify_reachability; then
        log "switched to $best_ssid; reachability verified"
        return 0
    fi
    log "ERROR: switched to $best_ssid but reachability check failed within ${POST_SWITCH_VERIFY_TIMEOUT}s"
    if [ -n "$current" ]; then
        log "  reverting to prior connection: $current"
        nmcli connection up id "$current" 2>/dev/null || true
    fi
    return 1
}

main "$@"
