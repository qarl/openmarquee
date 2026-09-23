#!/usr/bin/env bash
# scripts/tests/test_cma_watchdog.sh -- unit-style assertions for
# system/openmarquee-cma-watchdog.sh.
#
# Mocks /proc/meminfo via the script's MEMINFO_PATH env override
# (defaults to /proc/meminfo on real Pi) and mocks systemctl via
# a temp PATH shim. Asserts:
#
#   1. Below threshold => no restart fires.
#   2. Above threshold + no prior restart => restart fires.
#   3. Above threshold + within cooldown => no restart fires.
#   4. Above threshold + past cooldown => restart fires.
#   5. /proc/meminfo unreadable => cma_used=0, no restart.
#   6. CmaTotal=0 (kernel without CMA) => cma_used=0, no restart.
#   7. Override file injects CMA_USED_OVERRIDE_MB => uses that.
#   8. State file corrupted => treated as no prior restart, restart fires.
#   9.  Pool-relative (THRESHOLD_MB unset): fires at 90% of the live pool.
#   10. Pool-relative: below 90% of the live pool => no restart.
#   11. Bug 5 regression: the OLD absolute THRESHOLD_MB=300 on a 256M pool
#       CANNOT fire even at 250MB used (proves the watchdog it replaced was
#       dead) — the pool-relative default (tests 9/10) is what fixes it.
#   12. Pool-relative auto-adapts: same 90% rule fires on a 320M pool too.
#   13. Pool-relative + CmaTotal=0 (pool unknown) => no action, no restart.
#   14. Dead-watchdog hardening: an UNREACHABLE threshold (>= pool ceiling)
#       emits a loud WARN instead of sitting silently dead — the exact
#       misconfig class Bug 5 exists to catch.
#
# Run:
#     bash scripts/tests/test_cma_watchdog.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_SCRIPT="$SCRIPT_DIR/../../system/openmarquee-cma-watchdog.sh"

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

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s' "$haystack" | grep -qF "$needle"; then
        FAIL=$((FAIL+1))
        printf '  FAIL  %s (expected NOT to contain %q)\n' "$label" "$needle"
        printf '         haystack: %s\n' "${haystack:0:300}"
    else
        PASS=$((PASS+1))
        printf '  PASS  %s\n' "$label"
    fi
}

# Build a tempdir holding:
#   - meminfo: fake /proc/meminfo fixture
#   - state: state file (last_restart_epoch=...)
#   - systemctl-calls.log: every systemctl invocation appended here
#   - bin/systemctl: shim that appends args to systemctl-calls.log
mk_sandbox() {
    local d
    d="$(mktemp -d "${TMPDIR:-/tmp}/cma-watchdog-test.XXXXXX")"
    mkdir -p "$d/bin"
    cat > "$d/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$SANDBOX/systemctl-calls.log"
exit 0
EOF
    chmod +x "$d/bin/systemctl"
    : > "$d/systemctl-calls.log"
    printf '%s\n' "$d"
}

# Write a /proc/meminfo fixture with given CmaTotal + CmaFree in kB.
write_meminfo() {
    local path="$1" total_kb="$2" free_kb="$3"
    cat > "$path" <<EOF
MemTotal:         512000 kB
MemFree:          120000 kB
CmaTotal:         ${total_kb} kB
CmaFree:          ${free_kb} kB
EOF
}

run_watchdog() {
    # Pass through all env overrides for a single invocation.
    # Returns the script's exit code in $? and stdout+stderr in $OUT.
    local sandbox="$1"
    OUT=$(
        SANDBOX="$sandbox" \
        PATH="$sandbox/bin:$PATH" \
        MEMINFO_PATH="${MEMINFO_PATH:-$sandbox/meminfo}" \
        STATE_FILE="${STATE_FILE:-$sandbox/state}" \
        OVERRIDE_PATH="${OVERRIDE_PATH:-$sandbox/override}" \
        DEFAULTS="${DEFAULTS:-/nonexistent}" \
        SYSTEMCTL="${SYSTEMCTL:-$sandbox/bin/systemctl}" \
        THRESHOLD_MB="${THRESHOLD_MB-220}" \
        THRESHOLD_PCT="${THRESHOLD_PCT-90}" \
        COOLDOWN_SEC="${COOLDOWN_SEC:-1800}" \
        RESTART_TARGET="${RESTART_TARGET:-openmarquee-backend.service}" \
        bash "$TARGET_SCRIPT" 2>&1
    )
    EXIT=$?
}

# -- Test 1: below threshold => no restart ------------------------------------
echo "Test 1: cma_used=187MB < 220MB threshold => no restart"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 70000  # 192144 kB used = 187 MB
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cma_used" "cma_used=187MB" "$OUT"
assert_not_contains "no restart fired" "triggering" "$OUT"
assert_eq "systemctl-calls.log is empty" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 2: above threshold + no prior restart => restart fires -------------
echo "Test 2: cma_used=240MB >= 220MB, no prior restart => restart fires"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 16384  # 245760 kB used = 240 MB
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cma_used" "cma_used=240MB" "$OUT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
assert_contains "state file written" "last_restart_epoch=" "$(cat "$SANDBOX/state" 2>/dev/null || echo MISSING)"
rm -rf "$SANDBOX"

# -- Test 3: above threshold + within cooldown => no restart -----------------
echo "Test 3: cma_used=240MB, last restart 60s ago, cooldown=1800s => no restart"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 16384  # 240 MB
RECENT_EPOCH=$(( $(date +%s) - 60 ))
printf 'last_restart_epoch=%s\n' "$RECENT_EPOCH" > "$SANDBOX/state"
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cooldown" "within cooldown" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 4: above threshold + past cooldown => restart fires ----------------
echo "Test 4: cma_used=240MB, last restart 2000s ago, cooldown=1800s => restart fires"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 16384  # 240 MB
OLD_EPOCH=$(( $(date +%s) - 2000 ))
printf 'last_restart_epoch=%s\n' "$OLD_EPOCH" > "$SANDBOX/state"
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 5: /proc/meminfo unreadable => cma_used=0, no restart --------------
echo "Test 5: meminfo unreadable => cma_used=0, no restart"
SANDBOX="$(mk_sandbox)"
# No meminfo file written; default MEMINFO_PATH=$SANDBOX/meminfo doesn't exist.
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs error" "unreadable" "$OUT"
assert_contains "reports zero" "cma_used=0MB" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 6: CmaTotal=0 => kernel without CMA => no restart -------------------
echo "Test 6: CmaTotal=0 => no restart"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 0 0
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs warn" "CmaTotal=0" "$OUT"
assert_contains "reports zero" "cma_used=0MB" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 7: override file injects CMA_USED_OVERRIDE_MB ----------------------
echo "Test 7: override file => uses override"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 240000  # would compute ~21 MB
printf 'CMA_USED_OVERRIDE_MB=250\n' > "$SANDBOX/override"
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs override" "using CMA_USED_OVERRIDE_MB=250" "$OUT"
assert_contains "logs cma_used" "cma_used=250MB" "$OUT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 8: corrupted state file => treated as no prior restart -------------
echo "Test 8: state file corrupted => restart fires (above threshold)"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 16384  # 240 MB
printf 'garbage_not_an_epoch\n' > "$SANDBOX/state"
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB COOLDOWN_SEC RESTART_TARGET
run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 9: pool-relative fires at 90% of the live 256M pool ----------------
echo "Test 9: THRESHOLD_MB unset, 256M pool, cma_used=250MB >= 90% (230MB) => restart fires"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 6144  # 256000 kB used = 250 MB, pool 256 MB
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB="" run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cma_used" "cma_used=250MB" "$OUT"
assert_contains "logs pool-relative threshold" "threshold=230MB (90%-of-256MB-pool)" "$OUT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 10: pool-relative, below 90% of the live pool => no restart ---------
echo "Test 10: THRESHOLD_MB unset, 256M pool, cma_used=200MB < 90% (230MB) => no restart"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 57344  # 204800 kB used = 200 MB, pool 256 MB
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB="" run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs pool-relative threshold" "threshold=230MB (90%-of-256MB-pool)" "$OUT"
assert_not_contains "no restart fired" "triggering" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 11: Bug 5 regression -- the OLD absolute 300 is DEAD on a 256M pool -
echo "Test 11: absolute THRESHOLD_MB=300 on 256M pool, cma_used=250MB => NEVER fires (dead watchdog)"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 6144  # 250 MB used, pool 256 MB (CmaUsed caps at 256 < 300)
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB=300 run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cma_used at 250" "cma_used=250MB" "$OUT"
assert_contains "logs absolute threshold" "threshold=300MB (absolute)" "$OUT"
assert_not_contains "no restart fired" "triggering" "$OUT"
assert_eq "systemctl NOT called (proves the pre-Bug-5 watchdog was dead)" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 12: pool-relative auto-adapts to a 320M pool -----------------------
echo "Test 12: THRESHOLD_MB unset, 320M pool, cma_used=290MB >= 90% (288MB) => restart fires"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 327680 30720  # 296960 kB used = 290 MB, pool 320 MB
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB="" run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs cma_used" "cma_used=290MB" "$OUT"
assert_contains "logs pool-relative threshold auto-adapts" "threshold=288MB (90%-of-320MB-pool)" "$OUT"
assert_contains "logs trigger" "triggered restart" "$OUT"
assert_contains "systemctl restart called" "restart --no-block openmarquee-backend.service" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 13: pool-relative + CmaTotal=0 => pool unknown, no action ----------
echo "Test 13: THRESHOLD_MB unset, CmaTotal=0 => pool unknown, no restart"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 0 0
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB="" run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs pool unknown" "pool unknown" "$OUT"
assert_not_contains "no restart fired" "triggering" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Test 14: unreachable threshold => loud WARN (dead-watchdog hardening) ----
echo "Test 14: absolute THRESHOLD_MB=300 on 256M pool => UNREACHABLE warning logged"
SANDBOX="$(mk_sandbox)"
write_meminfo "$SANDBOX/meminfo" 262144 6144  # 250 MB used, pool 256 MB; 300 > 256 = unreachable
unset MEMINFO_PATH STATE_FILE OVERRIDE_PATH DEFAULTS SYSTEMCTL THRESHOLD_MB THRESHOLD_PCT COOLDOWN_SEC RESTART_TARGET
THRESHOLD_MB=300 run_watchdog "$SANDBOX"
assert_eq "exit code" "0" "$EXIT"
assert_contains "logs UNREACHABLE warn" "UNREACHABLE" "$OUT"
assert_contains "warn names ceiling" "threshold 300MB >= CmaTotal 256MB" "$OUT"
assert_not_contains "no restart fired" "triggering" "$OUT"
assert_eq "systemctl NOT called" "" "$(cat "$SANDBOX/systemctl-calls.log")"
rm -rf "$SANDBOX"

# -- Summary ------------------------------------------------------------------
printf '\n--- %d PASS / %d FAIL ---\n' "$PASS" "$FAIL"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
