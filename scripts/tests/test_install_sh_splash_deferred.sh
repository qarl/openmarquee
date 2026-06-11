#!/usr/bin/env bash
# scripts/tests/test_install_sh_splash_deferred.sh -- assertions
# for the boot-splash deferral marker introduced 2026-06-11
# (JasonsSign1 Bug 2: stage_sd_card flow never installs the splash
# on first-boot-offline devices; document the limitation +
# operator-visible marker instead of silently skipping).
#
# Scope: text-level assertions on install.sh § 7d. We do NOT run
# install.sh end-to-end (it needs root + a real Pi). We DO grep for:
#   - the deferred-marker drop on apt-failure path
#   - the deferred-marker clear on apt-success path
#   - the deferred-marker clear on plymouth-already-present path
#   - the explicit "limitation" + "recovery" messaging
#   - the marker location (/var/openmarquee-splash-deferred — under
#     ReadWritePaths= per openmarquee-backend.service)
#
# Run:
#     bash scripts/tests/test_install_sh_splash_deferred.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../install.sh"

PASS=0
FAIL=0

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (expected to contain %q)\n' "$label" "$needle"
    fi
}

assert_grep() {
    local label="$1" pattern="$2" file="$3"
    if grep -qE "$pattern" "$file"; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (no match for pattern %q in %s)\n' "$label" "$pattern" "$file"
    fi
}

# Extract just § 7d (Boot splash) so the assertions don't false-positive
# on unrelated lines elsewhere in install.sh.
SECTION_7D="$(awk '
    /^# --- 7d\. Boot splash/  { in7d = 1 }
    /^# --- 8\./ && in7d        { exit }
    in7d                         { print }
' "$TARGET_SCRIPT")"

if [ -z "$SECTION_7D" ]; then
    echo "FAIL: could not locate § 7d in $TARGET_SCRIPT — install.sh structure may have changed" >&2
    exit 1
fi

echo "Test 1: deferred-marker drop is wired on apt-failure path"
assert_contains \
    "marker touched on apt-get install -y plymouth failure" \
    "touch /var/openmarquee-splash-deferred" \
    "$SECTION_7D"

echo ""
echo "Test 2: deferred-marker clear is wired on apt-success path"
# After 'plymouth installed' there should be a guarded rm of the
# marker (cleared by a later online install).
assert_contains \
    "marker cleared after successful apt-get install -y plymouth" \
    "rm -f /var/openmarquee-splash-deferred" \
    "$SECTION_7D"

echo ""
echo "Test 3: deferred-marker clear is wired on plymouth-already-present path"
# Same rm should appear in the 'command -v plymouth' branch so that
# a subsequent install.sh run on a Pi where plymouth got fetched
# manually clears the marker.
rm_count=$(printf '%s' "$SECTION_7D" | grep -c 'rm -f /var/openmarquee-splash-deferred')
if [ "$rm_count" -ge 2 ]; then
    PASS=$((PASS+1))
    echo "  PASS  marker-clear appears in both already-present + apt-success branches ($rm_count occurrences)"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  expected marker-clear in TWO places (apt-success + already-present); got $rm_count"
fi

echo ""
echo "Test 4: explicit 'limitation' messaging is present"
assert_contains \
    "section explains Pi OS Lite arm64 doesn't include plymouth" \
    "Pi OS Lite arm64 does NOT include plymouth" \
    "$SECTION_7D"

echo ""
echo "Test 5: explicit operator recovery path is documented"
assert_contains \
    "recovery: apt install plymouth" \
    "sudo apt install plymouth" \
    "$SECTION_7D"
assert_contains \
    "recovery: re-run install.sh" \
    "sudo bash /opt/openmarquee/scripts/install.sh" \
    "$SECTION_7D"

echo ""
echo "Test 6: marker drop is guarded by an empty-ROOT_PREFIX check"
# Critical: under a chroot/dry-run target (ROOT_PREFIX != ""), the
# marker MUST NOT be dropped on the host filesystem. Verify the
# touch is conditional on -z "\$ROOT_PREFIX".
# We look for the conditional on the surrounding lines.
assert_grep \
    "marker drop is guarded by -z ROOT_PREFIX" \
    'if \[ -z "\$ROOT_PREFIX" \]; then' \
    "$TARGET_SCRIPT"

echo ""
echo "Test 7: marker location is /var/openmarquee-splash-deferred (one canonical path)"
# Confirm the marker is /var/openmarquee-splash-deferred — NOT
# /var/openmarquee/splash-deferred or similar. The path follows the
# /var/openmarquee-install-failed convention (sibling, not a child
# of /var/openmarquee/).
canonical_count=$(printf '%s' "$SECTION_7D" | grep -c '/var/openmarquee-splash-deferred')
if [ "$canonical_count" -ge 3 ]; then
    PASS=$((PASS+1))
    echo "  PASS  /var/openmarquee-splash-deferred appears in touch + 2 rm sites ($canonical_count refs)"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  expected /var/openmarquee-splash-deferred in touch + 2 rm sites; got $canonical_count refs"
fi

echo ""
echo "Test 8: deferred path follows the install-failed sibling convention"
# The install-failed marker at /var/openmarquee-install-failed is
# the documented recovery surface (set by stage_sd_card.sh's
# user-data on a runcmd failure). The splash-deferred marker
# follows the SAME convention — sibling under /var/, not a child
# of /var/openmarquee/ (which would be inside the openmarquee
# state tree and harder for an operator to grep for at recovery
# time). Verify via stage_sd_card.sh which owns the sibling
# convention.
STAGE_SD="$SCRIPT_DIR/../stage_sd_card.sh"
assert_grep \
    "stage_sd_card.sh defines the /var/openmarquee-install-failed sibling" \
    '/var/openmarquee-install-failed' \
    "$STAGE_SD"
# And confirm splash-deferred uses the same /var/ prefix (no
# /var/openmarquee/ subdir leak).
if grep -E 'touch /var/openmarquee/splash-deferred|rm.*\$/var/openmarquee/splash-deferred' "$TARGET_SCRIPT" >/dev/null; then
    FAIL=$((FAIL+1))
    echo "  FAIL  splash-deferred leaked into /var/openmarquee/ subdir (should be /var/openmarquee-splash-deferred sibling)"
else
    PASS=$((PASS+1))
    echo "  PASS  splash-deferred is a sibling under /var/ (not under /var/openmarquee/)"
fi

echo ""
echo "================================================================"
printf "RESULT: %d pass / %d fail\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
