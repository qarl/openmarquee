#!/usr/bin/env bash
# scripts/tests/test_check_netctl_compat.sh
#
# deploy-hygiene 2026-07-16 (Phase 2 / P2-b). Exercises the exit-code contract
# of scripts/check_netctl_compat.py — the gate that catches the JasonsSign1
# failure class (a backend calling a netctl subcommand the deployed daemon's
# allowlist doesn't contain; PR #89, 1ac5811).
#
# Exit contract:
#   0  compatible          2  usage/parse/unknown-transport          3  incompatible
#
# Run:
#     bash scripts/tests/test_check_netctl_compat.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
GATE="$REPO/scripts/check_netctl_compat.py"
DAEMON="$REPO/system/openmarquee-netctl-daemon"
BACKEND="$REPO/backend"
PASS=0
FAIL=0

ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1" >&2; }

[ -f "$GATE" ]   || { echo "FAIL: gate not found at $GATE" >&2; exit 1; }
[ -f "$DAEMON" ] || { echo "FAIL: daemon not found at $DAEMON" >&2; exit 1; }

run_gate() {  # -> echoes exit code; swallows output
    python3 "$GATE" "$@" >/dev/null 2>&1
    echo $?
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- 1. repo self-check: compatible, exit 0 ---------------------------------
rc="$(run_gate --daemon "$DAEMON" --backend "$BACKEND")"
[ "$rc" = 0 ] && ok "repo backend ⊆ repo allowlist (exit 0)" \
             || bad "repo self-check expected exit 0, got $rc"

# --- 2. synthetic skew: a called subcommand missing from allowlist, exit 3 --
# Drop `avahi-write-and-restart` (called by name_actuator.py) from the daemon.
grep -v 'avahi-write-and-restart' "$DAEMON" > "$TMP/daemon-stale"
rc="$(run_gate --daemon "$TMP/daemon-stale" --backend "$BACKEND")"
[ "$rc" = 3 ] && ok "backend call missing from allowlist -> INCOMPATIBLE (exit 3)" \
             || bad "skew expected exit 3, got $rc"

# --- 2b. skew via KEYWORD-form subcommand must also be caught (exit 3) ------
# netctl_send(subcommand="x") has no positional args; a positional-only gate
# would false-PASS it. Guards the keyword-arg hole (sacred review 2026-07-16).
mkdir -p "$TMP/kwbackend/openmarquee"
cp "$BACKEND"/openmarquee/netctl_client.py "$TMP/kwbackend/openmarquee/" 2>/dev/null
cat > "$TMP/kwbackend/openmarquee/kw_caller.py" <<'PY'
from openmarquee.netctl_client import netctl_send
def do():
    netctl_send(subcommand="totally-new-subcmd", payload=b"", timeout_s=5.0)
PY
rc="$(run_gate --daemon "$DAEMON" --backend "$TMP/kwbackend")"
[ "$rc" = 3 ] && ok "keyword-form subcommand skew -> INCOMPATIBLE (exit 3)" \
             || bad "keyword-form skew expected exit 3, got $rc"

# --- 3. self-defense: a rogue transport not in KNOWN_SENDERS, exit 2 --------
# Copy the backend and add a new function that opens the netctl socket but is
# not a known sender — the gate must refuse (can't guarantee completeness).
mkdir -p "$TMP/backend/openmarquee"
cp "$BACKEND"/openmarquee/*.py "$TMP/backend/openmarquee/" 2>/dev/null
cat > "$TMP/backend/openmarquee/rogue_netctl.py" <<'PY'
NETCTL_SOCKET_PATH = "/run/openmarquee/netctl.sock"
def _rogue_send(subcommand, payload):
    import socket
    s = socket.socket(socket.AF_UNIX)
    s.connect(NETCTL_SOCKET_PATH)
    s.sendall(subcommand.encode("ascii") + b"\n")
PY
rc="$(run_gate --daemon "$DAEMON" --backend "$TMP/backend")"
[ "$rc" = 2 ] && ok "unknown netctl transport wrapper -> gate error (exit 2)" \
             || bad "rogue transport expected exit 2, got $rc"

# --- 4. usage errors: missing daemon / backend, exit 2 ----------------------
rc="$(run_gate --daemon "$TMP/does-not-exist" --backend "$BACKEND")"
[ "$rc" = 2 ] && ok "missing daemon file -> exit 2" \
             || bad "missing daemon expected exit 2, got $rc"

rc="$(run_gate --daemon "$DAEMON" --backend "$TMP/no-such-dir")"
[ "$rc" = 2 ] && ok "missing backend dir -> exit 2" \
             || bad "missing backend expected exit 2, got $rc"

# --- 5. quiet mode still fails loud on skew ---------------------------------
rc="$(run_gate --quiet --daemon "$TMP/daemon-stale" --backend "$BACKEND")"
[ "$rc" = 3 ] && ok "--quiet still returns exit 3 on skew" \
             || bad "--quiet skew expected exit 3, got $rc"

echo
echo "== test_check_netctl_compat: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
