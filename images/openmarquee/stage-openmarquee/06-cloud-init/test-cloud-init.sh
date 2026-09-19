#!/bin/bash
# test-cloud-init.sh — structural tests for the 06-cloud-init substage.
#
# A loop-mount of the built image is out of scope for a unit test (that is
# the manual build-completeness check + admin's live serial-console
# validation). These tests guard the WIRING that the first-light failure
# revealed was missing: the units get enabled, the disable marker gets
# cleared, and the NoCloud datasource is pointed at the boot partition.
# Runnable on any host: bash test-cloud-init.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN="${DIR}/06-run.sh"
CFG="${DIR}/files/etc/cloud/cloud.cfg.d/99_openmarquee.cfg"
fail=0
ok()  { echo "ok:   $1"; }
bad() { echo "FAIL: $1"; fail=1; }
has() { # name file pattern
    if grep -qE "$3" "$2"; then ok "$1"; else bad "$1"; fi
}

# ── the runner exists + installs the datasource drop-in ──────────────
[ -f "$RUN" ] && ok "06-run.sh exists" || bad "06-run.sh missing"
has "06-run.sh installs 99_openmarquee.cfg into /etc/cloud/cloud.cfg.d" \
    "$RUN" 'install .*/etc/cloud/cloud\.cfg\.d/99_openmarquee\.cfg'

# ── the runner ENABLES the cloud-init units (both classic + renamed) ──
# The gap that caused first-light: units installed but never enabled.
for unit in cloud-init-local.service cloud-init.service \
            cloud-init-network.service cloud-config.service \
            cloud-final.service; do
    has "06-run.sh references unit ${unit}" "$RUN" "${unit}"
done
has "06-run.sh calls systemctl enable" "$RUN" 'systemctl enable'
has "06-run.sh removes /etc/cloud/cloud-init.disabled" \
    "$RUN" 'rm -f /etc/cloud/cloud-init\.disabled'
has "06-run.sh fails loud if no cloud-init units found" \
    "$RUN" 'no cloud-init units found'

# ── the NoCloud datasource drop-in is correct ───────────────────────
[ -f "$CFG" ] && ok "99_openmarquee.cfg exists" || bad "99_openmarquee.cfg missing"
has "cfg forces datasource_list: [ NoCloud ]" \
    "$CFG" '^datasource_list:[[:space:]]*\[[[:space:]]*NoCloud[[:space:]]*\]'
has "cfg declares a NoCloud datasource block" "$CFG" '^[[:space:]]*NoCloud:'
# The seed MUST point at the FAT boot partition (bootfs=/boot/firmware on
# trixie) with a file:// URL and a TRAILING SLASH (cloud-init appends
# user-data/meta-data/network-config directly to it).
has "cfg seedfrom = file:///boot/firmware/ (trailing slash)" \
    "$CFG" '^[[:space:]]*seedfrom:[[:space:]]*file:///boot/firmware/[[:space:]]*$'

if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
