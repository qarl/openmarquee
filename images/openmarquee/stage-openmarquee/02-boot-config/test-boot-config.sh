#!/bin/bash
# test-boot-config.sh — unit tests for boot-config-lib.sh.
#
# cmdline.txt is the boot-bricking-risk file: a single line of kernel
# params, and a stray newline drops every param after it. These tests
# lock the patch's safety properties — runnable on any host, no
# pi-gen / no Pi needed:  bash test-boot-config.sh
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
source "${DIR}/boot-config-lib.sh"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

ok()   { echo "ok:   $1"; }
bad()  { echo "FAIL: $1"; fail=1; }
check() { # name expected actual
    if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 — expected [$2] got [$3]"; fi
}

# ── cmdline.txt: append keeps ONE line + preserves every param ──────
printf 'console=serial0,115200 console=tty1 root=PARTUUID=53c1b4e3-02 rootwait\n' \
    > "$TMP/cmdline.txt"
patch_cmdline_txt "$TMP/cmdline.txt" >/dev/null
# exactly one newline in the file == one physical line of params.
check "cmdline is exactly one line" "1" "$(wc -l < "$TMP/cmdline.txt" | tr -d ' ')"
grep -q 'console=tty1'              "$TMP/cmdline.txt" && ok "console= preserved"   || bad "console= lost"
grep -q 'root=PARTUUID=53c1b4e3-02' "$TMP/cmdline.txt" && ok "root= preserved"      || bad "root= lost"
grep -qw quiet                      "$TMP/cmdline.txt" && ok "quiet appended"       || bad "quiet missing"
grep -qw splash                     "$TMP/cmdline.txt" && ok "splash appended"      || bad "splash missing"
grep -q 'plymouth.ignore-serial-consoles' "$TMP/cmdline.txt" && ok "plymouth param appended" || bad "plymouth param missing"
# the appended params must sit on the SAME physical line as root=.
check "params on the root= line" "1" \
    "$(grep -c 'root=PARTUUID=53c1b4e3-02 .*splash' "$TMP/cmdline.txt")"

# ── idempotency: a re-run must NOT double-append ────────────────────
patch_cmdline_txt "$TMP/cmdline.txt" >/dev/null
check "no double splash on re-run" "1" "$(grep -ow splash "$TMP/cmdline.txt" | wc -l | tr -d ' ')"
check "still one line after re-run" "1" "$(wc -l < "$TMP/cmdline.txt" | tr -d ' ')"

# ── edge: input with NO trailing newline ───────────────────────────
printf 'console=tty1 root=PARTUUID=abc-02' > "$TMP/c2.txt"
patch_cmdline_txt "$TMP/c2.txt" >/dev/null
check "no-newline input -> one line out" "1" "$(wc -l < "$TMP/c2.txt" | tr -d ' ')"
grep -q 'console=tty1' "$TMP/c2.txt" && ok "c2: console= preserved" || bad "c2: console= lost"
grep -qw splash        "$TMP/c2.txt" && ok "c2: splash appended"    || bad "c2: splash missing"

# ── edge: input that already spans two lines is RE-JOINED to one ───
printf 'console=tty1\nroot=PARTUUID=z\n' > "$TMP/c3.txt"
patch_cmdline_txt "$TMP/c3.txt" >/dev/null
check "two-line input collapsed to one" "1" "$(wc -l < "$TMP/c3.txt" | tr -d ' ')"
grep -q 'console=tty1 root=PARTUUID=z' "$TMP/c3.txt" && ok "c3: params re-joined" || bad "c3: params not joined"

# ── edge: empty cmdline.txt is REFUSED (never write a bare splash) ──
: > "$TMP/c4.txt"
if patch_cmdline_txt "$TMP/c4.txt" >/dev/null 2>&1; then
    bad "empty cmdline.txt should be refused"
else
    ok "empty cmdline.txt refused"
fi

# ── config.txt: append + idempotent + existing lines preserved ─────
printf '[all]\nhdmi_force_hotplug=1\n' > "$TMP/config.txt"
patch_config_txt "$TMP/config.txt" >/dev/null
grep -qE '^disable_splash=1' "$TMP/config.txt" && ok "disable_splash appended" || bad "disable_splash missing"
patch_config_txt "$TMP/config.txt" >/dev/null
check "no double disable_splash" "1" "$(grep -c '^disable_splash=1' "$TMP/config.txt")"
grep -q 'hdmi_force_hotplug=1' "$TMP/config.txt" && ok "config.txt existing line preserved" || bad "config.txt line lost"

# ── config.txt: already-present value is respected (no-op) ──────────
printf 'disable_splash=1\n' > "$TMP/c5.txt"
patch_config_txt "$TMP/c5.txt" >/dev/null
check "pre-set disable_splash untouched" "1" "$(grep -c 'disable_splash=1' "$TMP/c5.txt")"

if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
