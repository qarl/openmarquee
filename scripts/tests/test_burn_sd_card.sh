#!/usr/bin/env bash
# scripts/tests/test_burn_sd_card.sh -- unit-style assertions for
# burn_sd_card.sh's validation logic. Mocks diskutil so the test
# can run on a laptop without an actual SD card / external disk.
#
# We don't unit-test the dd/xz/curl flow -- that needs real hardware
# + network. We DO unit-test the arg-parser, the disk-shape rejection
# (path must be /dev/diskN whole-disk), and the diskutil-info-driven
# internal-vs-external classification.
#
# Run:
#     bash scripts/tests/test_burn_sd_card.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../burn_sd_card.sh"

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
        printf '         haystack: %s\n' "${haystack:0:300}"
    fi
}

run_with_mock() {
    # Spawn burn_sd_card.sh under a temp PATH where `diskutil` +
    # `plutil` are shims that return fixture text. Capture stdout +
    # stderr + exit code. Args 1..N are passed to the script.
    local mock_dir="$1"; shift
    local out exit_code
    set +e
    out="$(PATH="$mock_dir:$PATH" "$TARGET_SCRIPT" "$@" 2>&1 </dev/null)"
    exit_code=$?
    set -e
    printf '%s\n%s' "$exit_code" "$out"
}

make_mock_dir() {
    # Builds a temp dir holding shim scripts. The shims read a fixture
    # plist file at $MOCK_FIXTURE_PATH for diskutil info -plist output,
    # so each test can configure the disk shape independently.
    local d
    d="$(mktemp -d -t burn-sd-test.XXXXXX)"
    cat >"$d/diskutil" <<'EOF'
#!/usr/bin/env bash
case "$1" in
    info)
        if [ "$2" = "-plist" ]; then
            cat "$MOCK_FIXTURE_PATH"
        else
            cat "$MOCK_FIXTURE_PATH"
        fi
        ;;
    list)
        echo "mock: diskutil list (no disks)"
        ;;
    *)
        # unmountDisk / mountDisk / eject: silently succeed
        exit 0
        ;;
esac
EOF
    chmod +x "$d/diskutil"
    # plutil shim that reads the same fixture (which is a real plist)
    # is unnecessary -- we want REAL plutil to parse the fixture. So
    # we don't shim plutil; the system one works fine.
    printf '%s' "$d"
}

# ============================================================
# Fixtures: minimal plutil-parseable plists.
# ============================================================

INTERNAL_PLIST='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Internal</key><true/>
    <key>RemovableMediaOrExternalDevice</key><false/>
    <key>Ejectable</key><false/>
    <key>TotalSize</key><integer>500000000000</integer>
    <key>MediaName</key><string>APPLE SSD</string>
</dict>
</plist>'

EXTERNAL_REMOVABLE_PLIST='<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Internal</key><false/>
    <key>RemovableMediaOrExternalDevice</key><true/>
    <key>Ejectable</key><true/>
    <key>TotalSize</key><integer>32000000000</integer>
    <key>MediaName</key><string>SanDisk SD Reader</string>
</dict>
</plist>'

# ============================================================
# Tests.
# ============================================================

echo "burn_sd_card.sh -- disk validation gauntlet"
echo ""

MOCK_DIR="$(make_mock_dir)"
# Single-quote so $MOCK_DIR is resolved at signal time, not at trap-set time
# (SC2064). Today MOCK_DIR isn't reassigned, but the late-expansion form is
# the safer pattern + keeps shellcheck quiet.
trap 'rm -rf "$MOCK_DIR"' EXIT

# --- Test 1: missing target arg ---
echo "test 1: missing target"
RESULT="$(run_with_mock "$MOCK_DIR")"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "exits non-zero" 1 "$EXIT"
assert_contains "error message names the issue" "missing target" "$OUT"

# --- Test 2: --help works ---
echo ""
echo "test 2: --help"
RESULT="$(run_with_mock "$MOCK_DIR" --help)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "--help exits zero" 0 "$EXIT"
assert_contains "--help prints usage" "burn_sd_card.sh" "$OUT"

# --- Test 3: invalid disk shape (partition, not whole disk) ---
echo ""
echo "test 3: rejects partition path /dev/disk4s1"
# Declare + assign separately so mktemp's exit code propagates (SC2155).
# Combined `export X="$(mktemp)"` would mask a mktemp failure.
MOCK_FIXTURE_PATH="$(mktemp)"
export MOCK_FIXTURE_PATH
echo "$EXTERNAL_REMOVABLE_PLIST" > "$MOCK_FIXTURE_PATH"
RESULT="$(run_with_mock "$MOCK_DIR" --dry-run /dev/disk4s1)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "rejects partition path" 1 "$EXIT"
assert_contains "names the constraint" "must be /dev/diskN" "$OUT"

# --- Test 4: invalid disk shape (random path) ---
echo ""
echo "test 4: rejects /tmp/some-file"
RESULT="$(run_with_mock "$MOCK_DIR" --dry-run /tmp/some-file)"
EXIT="${RESULT%%$'\n'*}"
assert_eq "rejects non-disk path" 1 "$EXIT"

# --- Test 5: internal disk refused (the wipes-mac-ssd guard) ---
echo ""
echo "test 5: refuses internal disk (CRITICAL safety check)"
echo "$INTERNAL_PLIST" > "$MOCK_FIXTURE_PATH"
RESULT="$(run_with_mock "$MOCK_DIR" --dry-run /dev/disk0)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "refuses internal disk" 1 "$EXIT"
assert_contains "names the issue" "INTERNAL disk" "$OUT"

# --- Test 6: external+removable disk accepted in dry-run ---
echo ""
echo "test 6: accepts external+removable in dry-run"
echo "$EXTERNAL_REMOVABLE_PLIST" > "$MOCK_FIXTURE_PATH"
# Create a fake bundle so the bundle-presence check passes.
FAKE_BUNDLE_DIR="$(mktemp -d)"
mkdir -p "$FAKE_BUNDLE_DIR/dist"
touch "$FAKE_BUNDLE_DIR/dist/openmarquee-sd-bundle.tar.zst"
# Override the bundle path explicitly + run.
RESULT="$(OPENMARQUEE_BUNDLE="$FAKE_BUNDLE_DIR/dist/openmarquee-sd-bundle.tar.zst" \
    run_with_mock "$MOCK_DIR" --dry-run /dev/disk7)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "accepts external in dry-run" 0 "$EXIT"
assert_contains "names the device" "/dev/disk7" "$OUT"
assert_contains "shows would-flash plan" "[DRY-RUN] would run: xz -dc" "$OUT"
assert_contains "uses raw rdisk" "/dev/rdisk7" "$OUT"
rm -rf "$FAKE_BUNDLE_DIR"

# --- Test 7: external disk, bundle missing -> bail ---
echo ""
echo "test 7: bails when bundle is missing"
RESULT="$(OPENMARQUEE_BUNDLE="/nonexistent/path/bundle.tar.zst" \
    run_with_mock "$MOCK_DIR" --dry-run /dev/disk7)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "bails on missing bundle" 1 "$EXIT"
assert_contains "names the bundle path" "missing bundle" "$OUT"

# --- Test 8: unknown flag ---
echo ""
echo "test 8: rejects unknown flag"
RESULT="$(run_with_mock "$MOCK_DIR" --force /dev/disk7)"
EXIT="${RESULT%%$'\n'*}"
OUT="${RESULT#*$'\n'}"
assert_eq "rejects unknown flag" 1 "$EXIT"
assert_contains "unknown flag" "unknown flag" "$OUT"

# ============================================================
# Summary.
# ============================================================

echo ""
echo "========================================"
echo "PASS: $PASS"
echo "FAIL: $FAIL"
echo "========================================"
[ "$FAIL" -eq 0 ] || exit 1
