#!/usr/bin/env bash
# scripts/tests/test_best_wifi.sh -- unit-style assertions for
# system/openmarquee-best-wifi.sh's helper functions.
#
# r63 (2026-06-05) -- regression test for the known_ssids bug
# (https://github.com/qarl/openmarquee — r60 deploy). The pre-r63
# implementation combined connection-level fields (NAME, TYPE)
# with setting-level fields (802-11-wireless.ssid) in a single
# `nmcli connection show -f ...` call, which nmcli rejects with
# exit 2 ("invalid field '802-11-wireless.ssid'"). Under
# `set -euo pipefail` the whole service failed every 5-min timer
# tick. r63 splits into a two-pass query: list wifi-typed names,
# then `nmcli -g 802-11-wireless.ssid connection show <NAME>` per
# name.
#
# Mocks nmcli via a temp PATH shim. Asserts:
#
#   1. known_ssids returns the expected SSIDs given a fixture.
#   2. known_ssids does NOT exit with status 2 (the regression).
#   3. Empty-SSID profile falls back to connection NAME.
#   4. Non-wifi profile types (ethernet, bridge) are filtered out.
#   5. Per-NAME lookup uses `nmcli -g 802-11-wireless.ssid`, NOT
#      a combined -f field list.
#
# Run:
#     bash scripts/tests/test_best_wifi.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../../system/openmarquee-best-wifi.sh"

if [ ! -f "$TARGET_SCRIPT" ]; then
    printf 'FAIL: target script not found at %s\n' "$TARGET_SCRIPT"
    exit 1
fi

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

assert_exit_code_not() {
    local label="$1" forbidden="$2" actual="$3"
    if [ "$forbidden" != "$actual" ]; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (got forbidden exit code %s)\n' "$label" "$forbidden"
    fi
}

# ---------------------------------------------------------------
# Mock nmcli builder.
#
# Generates a `nmcli` script in $tmpdir/bin that responds to the
# 3 query shapes the helper invokes:
#
#   1. `nmcli -t -e no -f NAME,TYPE connection show`
#      -> list of "NAME:TYPE" lines for ALL connections
#
#   2. `nmcli -t -e no -g 802-11-wireless.ssid connection show <NAME>`
#      -> the raw SSID for that connection's wifi setting (or empty)
#
#   3. `nmcli -t -e no -f IN-USE,SSID,DEVICE device wifi list`
#      `nmcli -t -e no -f SSID,SIGNAL device wifi list`
#      -> empty (no scan needed for known_ssids unit test).
#
# REJECTS the pre-r63 combined-field call shape -- if the helper
# ever regresses, the mock exits 2 and the script aborts under
# set -euo pipefail, surfacing the bug.
# ---------------------------------------------------------------

setup_mock_nmcli() {
    local tmpdir="$1"
    local fixture_file="$2"
    mkdir -p "$tmpdir/bin"
    cat > "$tmpdir/bin/nmcli" <<EOF
#!/usr/bin/env bash
set -uo pipefail
FIXTURE_FILE='$fixture_file'

# Parse the args we care about. The helper ALWAYS prefixes with
# '-t -e no' so we skip those.
ARGS=("\$@")
i=0
# Skip terse / no-escape flags.
while [ "\$i" -lt "\${#ARGS[@]}" ]; do
    case "\${ARGS[\$i]}" in
        -t|--terse) i=\$((i+1)) ;;
        -e) i=\$((i+2)) ;;  # skip -e no
        --escape) i=\$((i+2)) ;;
        *) break ;;
    esac
done

# Reslice from \$i forward.
ARGS=("\${ARGS[@]:\$i}")

# Pattern 1: -f NAME,TYPE connection show
if [ "\${ARGS[0]:-}" = "-f" ] && [ "\${ARGS[1]:-}" = "NAME,TYPE" ] && \
   [ "\${ARGS[2]:-}" = "connection" ] && [ "\${ARGS[3]:-}" = "show" ]; then
    # Emit NAME:TYPE from the fixture (filter to first 2 fields).
    awk -F':' '{print \$1 ":" \$2}' "\$FIXTURE_FILE"
    exit 0
fi

# Pattern 2: -g 802-11-wireless.ssid connection show NAME
# (target_name, NOT local_name -- the bare `local` would be a bash
# builtin and read as ambiguous; subagent NIT.)
if [ "\${ARGS[0]:-}" = "-g" ] && [ "\${ARGS[1]:-}" = "802-11-wireless.ssid" ] && \
   [ "\${ARGS[2]:-}" = "connection" ] && [ "\${ARGS[3]:-}" = "show" ]; then
    target_name=\${ARGS[4]:-}
    awk -F':' -v want="\$target_name" '\$1 == want {print \$3}' "\$FIXTURE_FILE"
    exit 0
fi

# Pattern 3: device wifi list (mocked empty).
if [ "\${ARGS[*]}" = "-f IN-USE,SSID,DEVICE device wifi list" ] || \
   [ "\${ARGS[*]}" = "-f SSID,SIGNAL device wifi list" ]; then
    exit 0
fi

# Pattern 4: device wifi rescan (mocked no-op).
if [ "\${ARGS[*]}" = "device wifi rescan" ]; then
    exit 0
fi

# Pattern 5: connection up id ... (mocked success).
if [ "\${ARGS[0]:-}" = "connection" ] && [ "\${ARGS[1]:-}" = "up" ]; then
    exit 0
fi

# REGRESSION SENTINEL: pre-r63 combined-field call. nmcli on a
# real Pi rejects this with exit 2; our mock matches that
# behavior so the test catches the regression if introduced.
if [ "\${ARGS[0]:-}" = "-f" ] && \
   printf '%s' "\${ARGS[1]:-}" | grep -q '802-11-wireless.ssid'; then
    echo "Error: invalid field '802-11-wireless.ssid'; allowed fields:" >&2
    echo "NAME,UUID,TYPE,TIMESTAMP,TIMESTAMP-REAL,..." >&2
    exit 2
fi

# Unknown invocation -- surface for debugging.
echo "MOCK nmcli: unhandled args: \${ARGS[*]}" >&2
exit 1
EOF
    chmod +x "$tmpdir/bin/nmcli"
}

# Logger shim (helper script calls `logger`; not relevant here).
setup_mock_logger() {
    local tmpdir="$1"
    cat > "$tmpdir/bin/logger" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$tmpdir/bin/logger"
}

# ---------------------------------------------------------------
# Test 1: known_ssids returns the expected SSIDs from a fixture.
# ---------------------------------------------------------------

printf 'Test 1: known_ssids returns expected SSIDs from fixture\n'
TMPDIR_1=$(mktemp -d)
trap 'rm -rf "$TMPDIR_1"' EXIT

# Fixture: 2 wifi profiles + 1 ethernet + 1 empty-SSID wifi.
# Format: NAME:TYPE:SSID (the mock awk-splits this to mimic nmcli
# output). NOTE: colon-in-SSID profiles are NOT exercised here --
# the r60 `-t -e no` raw-output mode + the helper's `-g` per-name
# query DOES handle colons in real-world SSIDs correctly (the
# `-g` path returns the raw value with no field splitting), but
# this fixture's mock uses awk -F: internally so colons in test
# data would skew the mock's own parsing, not the helper's.
cat > "$TMPDIR_1/fixture.txt" <<EOF
NEBULA:802-11-wireless:NEBULA
qarl:802-11-wireless:qarl
Wired:802-3-ethernet:
EmptySsidConn:802-11-wireless:
EOF

setup_mock_nmcli "$TMPDIR_1" "$TMPDIR_1/fixture.txt"
setup_mock_logger "$TMPDIR_1"

# Source the helper script with the mocked PATH; assert known_ssids
# returns the expected SSIDs in sorted order. Capture stderr to a
# log so a future regression shows the nmcli mock's error message
# (subagent NIT: don't blackhole the regression breadcrumb).
ACTUAL=$(PATH="$TMPDIR_1/bin:$PATH" bash -c "source $TARGET_SCRIPT; known_ssids" 2>"$TMPDIR_1/stderr.log")
EXIT_CODE=$?
if [ "$EXIT_CODE" = "2" ]; then
    printf 'DEBUG  stderr from known_ssids:\n'
    sed 's/^/        /' "$TMPDIR_1/stderr.log"
fi

assert_exit_code_not "known_ssids does not exit 2 (regression sentinel)" 2 "$EXIT_CODE"

# Expected: NEBULA, qarl, EmptySsidConn (fallback to NAME). Sorted.
EXPECTED='EmptySsidConn
NEBULA
qarl'
assert_eq "known_ssids returns expected sorted SSIDs" "$EXPECTED" "$ACTUAL"

# ---------------------------------------------------------------
# Test 2: ethernet/non-wifi profiles are filtered out.
# ---------------------------------------------------------------

printf 'Test 2: non-wifi profiles filtered\n'
assert_contains_not() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (haystack contains %q)\n' "$label" "$needle"
    else
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    fi
}
assert_contains_not "ethernet profile 'Wired' filtered out of known_ssids" "Wired" "$ACTUAL"

# ---------------------------------------------------------------
# Test 3: empty-SSID profile falls back to NAME.
# ---------------------------------------------------------------

printf 'Test 3: empty-SSID profile falls back to NAME\n'
# 'EmptySsidConn' is in the fixture with empty SSID; should appear
# in the output as its name.
if printf '%s' "$ACTUAL" | grep -qFx "EmptySsidConn"; then
    PASS=$((PASS+1))
    printf '  PASS  empty-SSID profile uses NAME as fallback\n'
else
    FAIL=$((FAIL+1))
    printf '  FAIL  empty-SSID profile fallback did not produce "EmptySsidConn"\n'
fi

# ---------------------------------------------------------------
# Test 4: regression -- pre-r63 combined-field call would have
# tripped the mock's exit-2 sentinel.
# ---------------------------------------------------------------

printf 'Test 4: regression sentinel fires if combined-field call returns\n'

# Simulate the pre-r63 broken call. The mock's regression sentinel
# (last branch) emits stderr + exits 2.
PRE_R63_OUT=$(PATH="$TMPDIR_1/bin:$PATH" nmcli -t -e no -f NAME,TYPE,802-11-wireless.ssid connection show 2>&1)
PRE_R63_EXIT=$?
assert_eq "regression sentinel exit code" "2" "$PRE_R63_EXIT"
assert_eq_grep() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    else
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (haystack: %s)\n' "$label" "${haystack:0:200}"
    fi
}
assert_eq_grep "regression sentinel error message" "invalid field" "$PRE_R63_OUT"

# ---------------------------------------------------------------
# Summary
# ---------------------------------------------------------------

printf '\n'
printf 'r63 best-wifi test summary: %d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
