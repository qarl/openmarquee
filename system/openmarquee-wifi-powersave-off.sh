#!/usr/bin/env bash
# Disable brcmfmac WiFi power-save on the openMarquee Pi's wlan0 + ap0
# interfaces. P1.0 deliverable for the onboarding rework
# (docs/onboarding-rework-plan.md §A contributor #2 + §D item #4).
#
# Why:
#   - The BCM43438 driver enables power-save by default; under light
#     load this causes dropped beacons + stalled associations.
#     Symptoms read as "intermittent / unspecified" failures —
#     exactly the surface the spec is trying to fix.
#   - Pre-P1.0, scripts/wifi-watchdog.sh logged a WARN if power_save
#     was ON but did nothing to enforce OFF. This script enforces.
#
# What:
#   - `iw dev <iface> set power_save off` on each interface that
#     exists. Per-vif, so we touch both wlan0 (STA) and ap0 (AP).
#   - Best-effort: missing-interface (e.g. ap0 doesn't exist yet at
#     boot) is logged but doesn't fail the script.
#   - Idempotent: re-running on an already-OFF interface is fine.
#
# When (re-fire triggers, not handled here yet):
#   - Boot: this is enabled WantedBy=multi-user.target, fires once.
#   - Reassociation: P1.1's supervisor will call this script (or its
#     core logic) on every wpa_supplicant CTRL-EVENT-CONNECTED so
#     the setting survives the driver's reassociation-time reset.
#
# Diagnostics: every action is logged via `logger -t
# openmarquee-powersave` so the journal shows what was actually set,
# regardless of brcmfmac's eventual decision on the value.

set -euo pipefail

LOG_TAG="openmarquee-powersave"

# Comma- or space-separated env override for testing; default to the
# canonical wlan0 + ap0 pair. P1.0 is intentionally narrow.
INTERFACES="${OPENMARQUEE_PS_INTERFACES:-wlan0 ap0}"

set_power_save_off() {
    local iface="$1"
    # 1. Interface exists at link level?
    if ! ip link show "$iface" >/dev/null 2>&1; then
        logger -t "$LOG_TAG" \
            "skip iface=$iface reason=missing_link (will retry on next reassociation)"
        return 0
    fi
    # 2. Read current setting (best-effort; some drivers don't expose
    # it via `iw get`, in which case we still attempt the set).
    local before
    before=$(iw dev "$iface" get power_save 2>/dev/null \
        | awk -F': ' '/Power save/ {print $2}' \
        || echo "unknown")
    # 3. Set off.
    if iw dev "$iface" set power_save off 2>/dev/null; then
        local after
        after=$(iw dev "$iface" get power_save 2>/dev/null \
            | awk -F': ' '/Power save/ {print $2}' \
            || echo "unknown")
        logger -t "$LOG_TAG" \
            "set iface=$iface before=$before after=$after result=ok"
    else
        # The `iw set` failure is usually because the iface is in a
        # state that doesn't accept the call (e.g. AP-mode before
        # hostapd has fully come up). Log and continue.
        logger -t "$LOG_TAG" \
            "set iface=$iface before=$before result=iw_set_failed (non-fatal; will retry)"
    fi
}

logger -t "$LOG_TAG" "openmarquee-wifi-powersave-off start (interfaces=$INTERFACES)"
for iface in $INTERFACES; do
    set_power_save_off "$iface"
done
logger -t "$LOG_TAG" "openmarquee-wifi-powersave-off done"
