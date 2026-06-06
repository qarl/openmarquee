#!/usr/bin/env bash
# scripts/tests/test_check_runtime_assets.sh
#
# r67 (2026-06-05) regression guard for scripts/check_runtime_assets.sh.
#
# Cases pinned:
#   1. Font file is ABSENT on the target -> exits 1 + message names
#      "missing" and points at the canonical font path.
#   2. Font file is present but 0 BYTES (truncated download / stub)
#      -> exits 1 + message names the actual size.
#   3. Font file present + over the 1 MB floor -> exits 0 + "OK"
#      message.
#   4. Helper called with no target -> exits 64 (usage error).
#
# Adversarial validation (perform before commit): comment out the
# `[ -f ]` branch in check_runtime_assets.sh; case 1 must FAIL
# (because the check is no longer firing). That is the failure
# mode this test is built to catch.
#
# Fake-ssh transport: a $TMPDIR/bin/ssh shim drops the target arg
# and runs the rest as a local shell command, so the helper hits
# a tmp directory tree instead of a real Pi. Safe in any CI; runs
# in <1 s, no network, no sudo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK="${SCRIPT_DIR}/../check_runtime_assets.sh"

if [ ! -x "$CHECK" ]; then
    echo "FAIL: $CHECK not found or not executable" >&2
    exit 1
fi

TMP="$(mktemp -d)"
# shellcheck disable=SC2064 -- want immediate expansion of $TMP
trap "rm -rf '$TMP'" EXIT

mkdir -p "$TMP/bin"
mkdir -p "$TMP/fake-remote/opt/openmarquee/ui/fonts"

# Fake ssh: drop the target arg, run the rest as a shell command.
# Quoting the rest as "$*" preserves the single-quoted shell snippet
# the helper passes (the `if [ -f ... ]; then wc -c < ...` block).
cat > "$TMP/bin/ssh" <<'EOF'
#!/usr/bin/env bash
shift
bash -c "$*"
EOF
chmod +x "$TMP/bin/ssh"

export PATH="$TMP/bin:$PATH"
export OPENMARQUEE_REMOTE_ROOT="$TMP/fake-remote/opt/openmarquee"

FONT_DIR="$TMP/fake-remote/opt/openmarquee/ui/fonts"
FONT_PATH="$FONT_DIR/noto-color-emoji-colrv1.ttf"

failed=0

# -----------------------------------------------------------------
# Case 1: font absent on target -> exit 1 + "missing" diagnostic.
# This is the EXACT failure mode r67 was built to catch.
# -----------------------------------------------------------------
if [ -e "$FONT_PATH" ]; then rm -f "$FONT_PATH"; fi
set +e
out=$("$CHECK" fake@host 2>&1)
rc=$?
set -e
if [ "$rc" -ne 1 ]; then
    echo "FAIL case 1 (missing): expected exit 1, got $rc" >&2
    echo "$out" >&2
    failed=1
elif ! echo "$out" | grep -q "emoji font missing"; then
    echo "FAIL case 1 (missing): output missing 'emoji font missing' diagnostic" >&2
    echo "$out" >&2
    failed=1
elif ! echo "$out" | grep -q "noto-color-emoji-colrv1.ttf"; then
    echo "FAIL case 1 (missing): output missing font filename in diagnostic" >&2
    echo "$out" >&2
    failed=1
else
    echo "OK case 1: missing font detected with actionable error"
fi

# -----------------------------------------------------------------
# Case 2: font present but 0 bytes -> exit 1 + size in diagnostic.
# -----------------------------------------------------------------
: > "$FONT_PATH"
set +e
out=$("$CHECK" fake@host 2>&1)
rc=$?
set -e
if [ "$rc" -ne 1 ]; then
    echo "FAIL case 2 (0-byte): expected exit 1, got $rc" >&2
    echo "$out" >&2
    failed=1
elif ! echo "$out" | grep -q "only 0 bytes"; then
    echo "FAIL case 2 (0-byte): output missing 'only 0 bytes' diagnostic" >&2
    echo "$out" >&2
    failed=1
else
    echo "OK case 2: 0-byte stub detected with size in error"
fi

# -----------------------------------------------------------------
# Case 3: font present + >= 1 MB -> exit 0 + "OK" message.
# 2 MB of zeros suffices for the size gate; we're not validating
# the COLR table contents at this layer (that's Phase C scope).
# -----------------------------------------------------------------
dd if=/dev/zero of="$FONT_PATH" bs=1024 count=2048 2>/dev/null
set +e
out=$("$CHECK" fake@host 2>&1)
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    echo "FAIL case 3 (ok): expected exit 0, got $rc" >&2
    echo "$out" >&2
    failed=1
elif ! echo "$out" | grep -q "runtime asset check OK"; then
    echo "FAIL case 3 (ok): output missing 'runtime asset check OK' message" >&2
    echo "$out" >&2
    failed=1
else
    echo "OK case 3: valid font passes the check"
fi

# -----------------------------------------------------------------
# Case 4: no target arg -> exit 64 (usage).
# -----------------------------------------------------------------
set +e
out=$("$CHECK" 2>&1)
rc=$?
set -e
if [ "$rc" -ne 64 ]; then
    echo "FAIL case 4 (usage): expected exit 64, got $rc" >&2
    echo "$out" >&2
    failed=1
else
    echo "OK case 4: missing target arg fails with usage exit code"
fi

# -----------------------------------------------------------------
# Case 5: deploy.sh integration — the helper must be invoked from
# deploy.sh BEFORE the install.sh ssh call. A static-parse check
# guards against a future refactor moving the invocation after
# the backend restart (which would defeat the gate).
# -----------------------------------------------------------------
DEPLOY_SH="${SCRIPT_DIR}/../deploy.sh"
# r67 subagent NIT-5 tighten: require the matched line to look like
# an actual shell INVOCATION, not a banner / echo / docstring
# reference. The integration test must bind to the real call site
# or the gate-ordering invariant it pins is meaningless.
CHECK_INVOKE_LINE=$( (grep -nE '^[[:space:]]*bash[[:space:]].*check_runtime_assets\.sh' "$DEPLOY_SH" | head -1 | cut -d: -f1) || true )
INSTALL_SH_LINE=$( (grep -nE '^[[:space:]]*ssh[[:space:]].*scripts/install\.sh' "$DEPLOY_SH" | head -1 | cut -d: -f1) || true )

if [ -z "$CHECK_INVOKE_LINE" ]; then
    echo "FAIL case 5 (integration): deploy.sh does NOT invoke check_runtime_assets.sh" >&2
    failed=1
elif [ -z "$INSTALL_SH_LINE" ]; then
    echo "FAIL case 5 (integration): could not locate install.sh invocation in deploy.sh" >&2
    failed=1
elif [ "$CHECK_INVOKE_LINE" -ge "$INSTALL_SH_LINE" ]; then
    echo "FAIL case 5 (integration): check_runtime_assets.sh invoked at line $CHECK_INVOKE_LINE, AFTER install.sh at line $INSTALL_SH_LINE" >&2
    echo "  the gate must fire BEFORE the backend restart or it doesn't gate." >&2
    failed=1
else
    echo "OK case 5: deploy.sh invokes check_runtime_assets.sh at line $CHECK_INVOKE_LINE, before install.sh at line $INSTALL_SH_LINE"
fi

if [ "$failed" -eq 1 ]; then
    echo
    echo "FAIL: scripts/tests/test_check_runtime_assets.sh" >&2
    exit 1
fi

echo
echo "PASS: scripts/tests/test_check_runtime_assets.sh"
