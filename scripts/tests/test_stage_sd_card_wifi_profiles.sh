#!/usr/bin/env bash
# scripts/tests/test_stage_sd_card_wifi_profiles.sh -- unit-style
# assertions for the --wifi-profiles flag added to stage_sd_card.sh
# 2026-06-11 (JasonsSign1 Bug 1: NM keyfile first-boot ordering).
#
# Default path (no flag) MUST stay byte-identical to the pre-flag
# behavior — verified via:
#   - no openmarquee-wifi/ subdirectory on the bootfs
#   - no bootcmd: block in user-data
#
# Optional path (--wifi-profiles DIR) MUST:
#   - reject when DIR doesn't exist (exit non-zero, helpful message)
#   - reject when DIR has no *.nmconnection files
#   - copy each *.nmconnection to <bootfs>/openmarquee-wifi/
#   - splice a bootcmd: block BEFORE the runcmd: line in user-data
#   - the bootcmd block runs in cloud-init-local (BEFORE NM start)
#   - the bootcmd targets /etc/NetworkManager/system-connections/
#     with mode 0600 (the canonical NM keyfile requirement)
#
# Run:
#     bash scripts/tests/test_stage_sd_card_wifi_profiles.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../stage_sd_card.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (expected %q, got %q)\n' "$label" "$expected" "$actual"
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (expected to contain %q)\n' "$label" "$needle"
        printf '         haystack[:400]: %s\n' "${haystack:0:400}"
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if ! printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (expected NOT to contain %q)\n' "$label" "$needle"
    fi
}

make_mock_bootfs() {
    local d="$1"
    rm -rf "$d"
    mkdir -p "$d"
    # cmdline.txt + config.txt are the Pi-bootfs sanity-check
    # markers stage_sd_card.sh requires.
    touch "$d/cmdline.txt" "$d/config.txt"
}

make_mock_wifi_dir() {
    local d="$1"
    rm -rf "$d"
    mkdir -p "$d"
    cat > "$d/HomeWifi.nmconnection" <<'EOF'
[connection]
id=HomeWifi
type=wifi
[wifi]
mode=infrastructure
ssid=HomeNetwork
[wifi-security]
key-mgmt=wpa-psk
psk=hunter2
EOF
    cat > "$d/Garage.nmconnection" <<'EOF'
[connection]
id=Garage
type=wifi
[wifi]
mode=infrastructure
ssid=Garage
[wifi-security]
key-mgmt=wpa-psk
psk=garagepass
EOF
}

# Stub a tiny bundle so the bundle-must-exist check passes.
# The contents don't matter — stage_sd_card.sh just copies the file.
stub_bundle() {
    mkdir -p "$REPO_ROOT/dist"
    if [ ! -f "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst" ]; then
        printf 'stub-bundle' > "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst"
        return 1  # tells caller to clean up
    fi
    return 0
}

cleanup_bundle_stub() {
    rm -f "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst"
}

run_stage() {
    local out exit_code
    set +e
    out="$("$@" 2>&1 </dev/null)"
    exit_code=$?
    set -e
    printf '%s\n%s' "$exit_code" "$out"
}

# ============================================================
# Tests
# ============================================================

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

BUNDLE_PRE_EXISTING=true
stub_bundle || BUNDLE_PRE_EXISTING=false

echo "Test 1: default path (no --wifi-profiles) is byte-identical to pre-flag behavior"
make_mock_bootfs "$WORKDIR/bootfs1"
out_combined="$(run_stage bash "$TARGET_SCRIPT" "$WORKDIR/bootfs1")"
ec1="$(printf '%s' "$out_combined" | head -1)"
assert_eq "default path exits 0" "0" "$ec1"
# No openmarquee-wifi/ subdir.
if [ ! -d "$WORKDIR/bootfs1/openmarquee-wifi" ]; then
    PASS=$((PASS+1))
    echo "  PASS  default path: no openmarquee-wifi/ subdir"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  default path leaked openmarquee-wifi/ subdir"
fi
# No bootcmd: block in user-data.
user_data1="$(cat "$WORKDIR/bootfs1/user-data")"
assert_not_contains "default user-data has NO bootcmd: line" "bootcmd:" "$user_data1"
assert_contains "default user-data still has runcmd:" "runcmd:" "$user_data1"
assert_contains "default user-data still extracts the bundle" "openmarquee-bundle.tar.zst" "$user_data1"

echo ""
echo "Test 2: --wifi-profiles with bad dir fails fast"
make_mock_bootfs "$WORKDIR/bootfs2"
out_combined="$(run_stage bash "$TARGET_SCRIPT" --wifi-profiles "/nonexistent/dir" "$WORKDIR/bootfs2")"
ec2="$(printf '%s' "$out_combined" | head -1)"
out2_msg="$(printf '%s' "$out_combined" | tail -n +2)"
if [ "$ec2" -ne 0 ]; then
    PASS=$((PASS+1))
    echo "  PASS  bad --wifi-profiles dir exits non-zero ($ec2)"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  bad --wifi-profiles dir exited 0 (should be non-zero)"
fi
assert_contains "error message names the bad path" "/nonexistent/dir" "$out2_msg"

echo ""
echo "Test 3: --wifi-profiles with empty dir (no *.nmconnection) fails fast"
make_mock_bootfs "$WORKDIR/bootfs3"
mkdir -p "$WORKDIR/empty-wifi"
out_combined="$(run_stage bash "$TARGET_SCRIPT" --wifi-profiles "$WORKDIR/empty-wifi" "$WORKDIR/bootfs3")"
ec3="$(printf '%s' "$out_combined" | head -1)"
if [ "$ec3" -ne 0 ]; then
    PASS=$((PASS+1))
    echo "  PASS  empty --wifi-profiles dir exits non-zero ($ec3)"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  empty --wifi-profiles dir exited 0 (should be non-zero)"
fi

echo ""
echo "Test 4: --wifi-profiles with 2 keyfiles → both copied + bootcmd block present"
make_mock_bootfs "$WORKDIR/bootfs4"
make_mock_wifi_dir "$WORKDIR/wifi4"
out_combined="$(run_stage bash "$TARGET_SCRIPT" --wifi-profiles "$WORKDIR/wifi4" "$WORKDIR/bootfs4")"
ec4="$(printf '%s' "$out_combined" | head -1)"
out4_msg="$(printf '%s' "$out_combined" | tail -n +2)"
assert_eq "happy path exits 0" "0" "$ec4"
# Both keyfiles copied to bootfs/openmarquee-wifi/.
if [ -f "$WORKDIR/bootfs4/openmarquee-wifi/HomeWifi.nmconnection" ] \
        && [ -f "$WORKDIR/bootfs4/openmarquee-wifi/Garage.nmconnection" ]; then
    PASS=$((PASS+1))
    echo "  PASS  2 keyfiles copied to bootfs/openmarquee-wifi/"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  expected HomeWifi + Garage in bootfs/openmarquee-wifi/"
fi
assert_contains "stdout reports 2 staged" "staged 2 wifi profile(s)" "$out4_msg"

# user-data must have a bootcmd: block AND it must precede runcmd:.
user_data4="$(cat "$WORKDIR/bootfs4/user-data")"
assert_contains "happy-path user-data has bootcmd:" "bootcmd:" "$user_data4"
# Ordering: bootcmd: comes BEFORE runcmd: in the file.
bootcmd_line=$(grep -n "^bootcmd:" "$WORKDIR/bootfs4/user-data" | head -1 | cut -d: -f1)
runcmd_line=$(grep -n "^runcmd:" "$WORKDIR/bootfs4/user-data" | head -1 | cut -d: -f1)
if [ -n "$bootcmd_line" ] && [ -n "$runcmd_line" ] && [ "$bootcmd_line" -lt "$runcmd_line" ]; then
    PASS=$((PASS+1))
    echo "  PASS  bootcmd: precedes runcmd: in user-data (lines $bootcmd_line < $runcmd_line)"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  bootcmd: must precede runcmd: in user-data (got bootcmd=$bootcmd_line runcmd=$runcmd_line)"
fi

echo ""
echo "Test 5: bootcmd block targets canonical NM keyfile path + mode"
# Mode 0600 is what NM requires for keyfile profiles with secrets.
assert_contains "bootcmd installs to NM system-connections dir" \
    "/etc/NetworkManager/system-connections" "$user_data4"
assert_contains "bootcmd uses install -m 0600 (NM-required mode)" \
    "install -m 0600" "$user_data4"
assert_contains "bootcmd creates the system-connections dir (0700)" \
    "install -d -m 0700" "$user_data4"
# Both /boot/firmware/ AND /boot/ fallbacks present (Pi OS Bookworm
# vs older).
assert_contains "bootcmd checks /boot/firmware/openmarquee-wifi" \
    "/boot/firmware/openmarquee-wifi" "$user_data4"
assert_contains "bootcmd checks /boot/openmarquee-wifi fallback" \
    "/boot/openmarquee-wifi" "$user_data4"

echo ""
echo "Test 6: no leftover .tmp files in bootfs"
leftover="$(find "$WORKDIR/bootfs4" -name '*.tmp' 2>/dev/null | wc -l | tr -d ' ')"
assert_eq "no .tmp files left in bootfs" "0" "$leftover"

echo ""
echo "Test 7: --wifi-profiles=DIR form (single-arg equals form) works"
make_mock_bootfs "$WORKDIR/bootfs7"
out_combined="$(run_stage bash "$TARGET_SCRIPT" "--wifi-profiles=$WORKDIR/wifi4" "$WORKDIR/bootfs7")"
ec7="$(printf '%s' "$out_combined" | head -1)"
assert_eq "--wifi-profiles=DIR exits 0" "0" "$ec7"
if [ -f "$WORKDIR/bootfs7/openmarquee-wifi/HomeWifi.nmconnection" ]; then
    PASS=$((PASS+1))
    echo "  PASS  --wifi-profiles=DIR copies keyfiles same as --wifi-profiles DIR"
else
    FAIL=$((FAIL+1))
    echo "  FAIL  --wifi-profiles=DIR did not copy keyfiles"
fi

if [ "$BUNDLE_PRE_EXISTING" = false ]; then
    cleanup_bundle_stub
fi

echo ""
echo "================================================================"
printf "RESULT: %d pass / %d fail\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
