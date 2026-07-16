#!/usr/bin/env bash
# scripts/tests/test_install_sh_wifi_watchdog_pair.sh
#
# deploy-hygiene 2026-07-16 (P2-d). Guards install.sh's §3e install of the WiFi
# AP-deauth-recovery watchdog COUPLED PAIR:
#   scripts/wifi-watchdog.sh          -> /usr/local/bin/wifi-watchdog.sh   (0755)
#   system/openmarquee-wifi-watchdog  -> /etc/cron.d/openmarquee-wifi-watchdog (0644 root)
#
# QA's 2026-07-16 08:13 JasonsSign1 probe confirmed the watchdog is live infra
# hand-placed once (2026-05-23 wedge) with NO repo install path — so a fresh
# burn would silently lack it. §3e is the durable path. This test asserts BOTH
# install actions fire (a commented-out half would be a version-skewed pair), in
# BOTH the full and --system-only paths (it must not be gated off).
#
# STATIC parse + --dry-run only. Run:
#     bash scripts/tests/test_install_sh_wifi_watchdog_pair.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="${SCRIPT_DIR}/../install.sh"
PASS=0
FAIL=0
ok()  { PASS=$((PASS+1)); printf 'ok   %s\n' "$1"; }
bad() { FAIL=$((FAIL+1)); printf 'FAIL %s\n' "$1" >&2; }

[ -f "$INSTALL_SH" ] || { echo "FAIL: install.sh not found at $INSTALL_SH" >&2; exit 1; }

# --- 1. static: §3e section + both DST paths + the BOTH-or-NEITHER guard -----
grep -qE '^# --- 3e\. WiFi AP-deauth-recovery watchdog' "$INSTALL_SH" \
    && ok "§3e section header present" || bad "§3e section header missing"

grep -qE 'WIFI_WATCHDOG_SCRIPT_DST=.*usr/local/bin/wifi-watchdog\.sh' "$INSTALL_SH" \
    && ok "script DST = /usr/local/bin/wifi-watchdog.sh" || bad "script DST wrong/missing"

grep -qE 'WIFI_WATCHDOG_CRON_DST=.*etc/cron\.d/openmarquee-wifi-watchdog' "$INSTALL_SH" \
    && ok "cron DST = /etc/cron.d/openmarquee-wifi-watchdog" || bad "cron DST wrong/missing"

# BOTH-or-NEITHER coupling guard (don't install a half-pair).
grep -qF '[ -f "$WIFI_WATCHDOG_SCRIPT_SRC" ] && [ -f "$WIFI_WATCHDOG_CRON_SRC" ]' "$INSTALL_SH" \
    && ok "coupled BOTH-or-NEITHER source guard present" || bad "coupled source guard missing"

# Correct modes: script 0755, cron 0644 root.
grep -qE 'install -m 0755 "\$WIFI_WATCHDOG_SCRIPT_SRC" "\$WIFI_WATCHDOG_SCRIPT_DST"' "$INSTALL_SH" \
    && ok "script installed 0755" || bad "script 0755 install line missing"
grep -qE 'install -m 0644 -o root -g root "\$WIFI_WATCHDOG_CRON_SRC" "\$WIFI_WATCHDOG_CRON_DST"' "$INSTALL_SH" \
    && ok "cron installed 0644 root:root" || bad "cron 0644 root install line missing"

# --- 2. §3e must run BEFORE §2 (pre-venv failure-tolerant zone) --------------
E3=$(grep -n '^# --- 3e\.' "$INSTALL_SH" | head -1 | cut -d: -f1)
S2=$(grep -n '^# --- 2\. Python venv' "$INSTALL_SH" | head -1 | cut -d: -f1)
if [ -n "$E3" ] && [ -n "$S2" ] && [ "$E3" -lt "$S2" ]; then
    ok "§3e precedes §2 (installs before the failure-prone pip)"
else
    bad "§3e should precede §2 (got 3e=$E3 vs 2=$S2)"
fi

# --- 3. dynamic: BOTH install actions fire in FULL and --system-only --------
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
FULL_OUT="$(bash "$INSTALL_SH" --dry-run --root "$TMP/full" 2>&1 || true)"
SYS_OUT="$(bash "$INSTALL_SH" --system-only --dry-run --root "$TMP/sys" 2>&1 || true)"

for label in FULL SYS; do
    if [ "$label" = FULL ]; then out="$FULL_OUT"; else out="$SYS_OUT"; fi
    grep -qE 'install -m 0755 .*/scripts/wifi-watchdog\.sh .*/usr/local/bin/wifi-watchdog\.sh' <<<"$out" \
        && ok "$label: script install action fires" \
        || bad "$label: script install action MISSING"
    grep -qE 'install -m 0644 -o root -g root .*/system/openmarquee-wifi-watchdog .*/etc/cron\.d/openmarquee-wifi-watchdog' <<<"$out" \
        && ok "$label: cron.d install action fires" \
        || bad "$label: cron.d install action MISSING"
done

echo
echo "== test_install_sh_wifi_watchdog_pair: $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
