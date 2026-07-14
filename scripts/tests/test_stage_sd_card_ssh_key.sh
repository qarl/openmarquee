#!/usr/bin/env bash
# scripts/tests/test_stage_sd_card_ssh_key.sh -- runtime assertions for the
# --ssh-key flag added to stage_sd_card.sh 2026-07-13 (SSH-user arc: make
# openmarquee the key-only SSH login identity on shipped cards).
#
# The shipped card is key-only (ssh_pwauth:false), so the operator key MUST
# land in user-data or the device is SSH-unreachable. This guards the runtime
# substitution (the static template shape is guarded by
# backend/tests/test_cloud_init_userdata.py):
#   - --ssh-key KEYFILE: {{SSH_AUTHORIZED_KEYS}} replaced by the key, placeholder gone
#   - no key + empty HOME: loud warning + placeholder RETAINED (console-recoverable)
#   - combined --wifi-profiles + --ssh-key: BOTH substitutions land
#
# Run: bash scripts/tests/test_stage_sd_card_ssh_key.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../stage_sd_card.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PASS=0
FAIL=0

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1)); printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1)); printf '  FAIL  %s (expected to contain %q)\n' "$label" "$needle"
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if ! printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1)); printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1)); printf '  FAIL  %s (expected NOT to contain %q)\n' "$label" "$needle"
    fi
}

make_mock_bootfs() {
    local d="$1"
    rm -rf "$d"; mkdir -p "$d"
    touch "$d/cmdline.txt" "$d/config.txt"
}

# Stub a tiny bundle so stage_sd_card's bundle-must-exist check passes.
STUBBED=0
if [ ! -f "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst" ]; then
    mkdir -p "$REPO_ROOT/dist"
    printf 'stub-bundle' > "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst"
    STUBBED=1
fi

TMP="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP"
    [ "$STUBBED" = 1 ] && rm -f "$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst"
}
trap cleanup EXIT

# A clearly-fake throwaway key (never a real credential).
KEY="$TMP/test_key.pub"
printf 'ssh-ed25519 AAAATESTKEYNOTREAL test@example\n' > "$KEY"

echo "== --ssh-key KEYFILE: key lands, placeholder gone =="
BOOTFS1="$TMP/bootfs1"; make_mock_bootfs "$BOOTFS1"
bash "$TARGET_SCRIPT" --ssh-key "$KEY" "$BOOTFS1" >/dev/null 2>&1
UD1="$(cat "$BOOTFS1/user-data")"
assert_contains     "key content lands in user-data"       "ssh-ed25519 AAAATESTKEYNOTREAL" "$UD1"
assert_not_contains "placeholder is substituted away"      "{{SSH_AUTHORIZED_KEYS}}"         "$UD1"
assert_contains     "openmarquee stays login-capable"      "shell: /bin/bash"               "$UD1"

echo "== no key + empty HOME: warns loudly, placeholder retained =="
BOOTFS2="$TMP/bootfs2"; make_mock_bootfs "$BOOTFS2"
OUT2="$(HOME="$TMP/no-such-home" bash "$TARGET_SCRIPT" "$BOOTFS2" 2>&1 || true)"
UD2="$(cat "$BOOTFS2/user-data")"
assert_contains "no-key path warns SSH is impossible"  "SSH-in as openmarquee is IMPOSSIBLE" "$OUT2"
assert_contains "placeholder retained when no key"     "{{SSH_AUTHORIZED_KEYS}}"             "$UD2"

echo "== combined --wifi-profiles + --ssh-key: both land =="
WIFI="$TMP/wifi"; mkdir -p "$WIFI"
cat > "$WIFI/Home.nmconnection" <<'EOF'
[connection]
id=Home
type=wifi
[wifi]
ssid=Home
[wifi-security]
key-mgmt=wpa-psk
psk=hunter2
EOF
BOOTFS3="$TMP/bootfs3"; make_mock_bootfs "$BOOTFS3"
bash "$TARGET_SCRIPT" --wifi-profiles "$WIFI" --ssh-key "$KEY" "$BOOTFS3" >/dev/null 2>&1
UD3="$(cat "$BOOTFS3/user-data")"
assert_contains     "key lands with wifi too"          "ssh-ed25519 AAAATESTKEYNOTREAL" "$UD3"
assert_not_contains "placeholder gone with wifi too"   "{{SSH_AUTHORIZED_KEYS}}"        "$UD3"
assert_contains     "wifi bootcmd spliced alongside"   "system-connections"            "$UD3"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
