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

# ── strip_cmdline_token (mitigation #5, 2026-05-23) ─────────────────
# Strips a single named kernel param token from cmdline.txt. Used to
# remove the base Pi OS `cgroup_disable=memory` flag so kernel PSI /
# cgroup memory accounting becomes available.

# Token in MIDDLE of line — stripped + surrounding tokens preserved +
# single line.
printf 'foo bar cgroup_disable=memory baz quux\n' > "$TMP/s1.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s1.txt" >/dev/null
check "s1: one line after strip" "1" "$(wc -l < "$TMP/s1.txt" | tr -d ' ')"
check "s1: target token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s1.txt")"
grep -qw foo  "$TMP/s1.txt" && ok "s1: foo preserved"  || bad "s1: foo lost"
grep -qw bar  "$TMP/s1.txt" && ok "s1: bar preserved"  || bad "s1: bar lost"
grep -qw baz  "$TMP/s1.txt" && ok "s1: baz preserved"  || bad "s1: baz lost"
grep -qw quux "$TMP/s1.txt" && ok "s1: quux preserved" || bad "s1: quux lost"

# Token at LINE-START — stripped + rest preserved.
printf 'cgroup_disable=memory foo bar baz\n' > "$TMP/s2.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s2.txt" >/dev/null
check "s2: target token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s2.txt")"
# After stripping a leading token, the result must NOT have a leading
# space (the awk re-join handles this; this fence catches a refactor
# that broke that property).
grep -q '^cgroup_disable=memory' "$TMP/s2.txt" && bad "s2: leading token still present" \
    || ok "s2: leading token removed"
grep -q '^ ' "$TMP/s2.txt" && bad "s2: leading space remained" || ok "s2: no leading space"
grep -qw foo "$TMP/s2.txt" && ok "s2: foo preserved" || bad "s2: foo lost"

# Token at LINE-END — stripped + rest preserved + no trailing space.
printf 'foo bar baz cgroup_disable=memory\n' > "$TMP/s3.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s3.txt" >/dev/null
check "s3: target token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s3.txt")"
# Trailing space before the newline would be a refactor smell — the
# printf '%s\n' "$stripped" path should never leave one.
grep -q ' $' "$TMP/s3.txt" && bad "s3: trailing space remained" || ok "s3: no trailing space"
grep -qw baz "$TMP/s3.txt" && ok "s3: baz preserved" || bad "s3: baz lost"

# Token ABSENT — no-op, file unchanged, exit 0.
printf 'foo bar baz\n' > "$TMP/s4.txt"
before_s4="$(cat "$TMP/s4.txt")"
if strip_cmdline_token "cgroup_disable=memory" "$TMP/s4.txt" >/dev/null; then
    ok "s4: absent-token exits 0"
else
    bad "s4: absent-token did not exit 0"
fi
after_s4="$(cat "$TMP/s4.txt")"
check "s4: file unchanged" "$before_s4" "$after_s4"

# Token appears TWICE (paranoid — real cmdline.txt won't, but the
# helper must be safe) — both occurrences stripped.
printf 'a cgroup_disable=memory b cgroup_disable=memory c\n' > "$TMP/s5.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s5.txt" >/dev/null
check "s5: both occurrences stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s5.txt")"
grep -qw a "$TMP/s5.txt" && ok "s5: a preserved" || bad "s5: a lost"
grep -qw b "$TMP/s5.txt" && ok "s5: b preserved" || bad "s5: b lost"
grep -qw c "$TMP/s5.txt" && ok "s5: c preserved" || bad "s5: c lost"

# Substring-prefix collision: `cgroup_disable=memory_zone` MUST NOT
# be stripped when the target is `cgroup_disable=memory`. awk field
# comparison is the safety; a regex-/sed-based refactor would burn
# here.
printf 'foo cgroup_disable=memory_zone bar cgroup_disable=memory baz\n' > "$TMP/s6.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s6.txt" >/dev/null
grep -qw 'cgroup_disable=memory_zone' "$TMP/s6.txt" \
    && ok "s6: substring-prefix collision preserved" \
    || bad "s6: substring-prefix collision wrongly stripped"
check "s6: exact-match token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s6.txt")"

# Multiple spaces between tokens — collapsed to single space (mirrors
# patch_cmdline_txt's normalization).
printf 'foo   bar    cgroup_disable=memory    baz\n' > "$TMP/s7.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s7.txt" >/dev/null
check "s7: target token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s7.txt")"
grep -q '  ' "$TMP/s7.txt" && bad "s7: double-space remained" || ok "s7: spaces collapsed"

# Idempotency — second run is a no-op, file identical to first-run
# output, exit 0.
printf 'foo cgroup_disable=memory bar\n' > "$TMP/s8.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s8.txt" >/dev/null
after_first="$(cat "$TMP/s8.txt")"
if strip_cmdline_token "cgroup_disable=memory" "$TMP/s8.txt" >/dev/null; then
    ok "s8: second run exits 0 (idempotent)"
else
    bad "s8: second run did not exit 0"
fi
after_second="$(cat "$TMP/s8.txt")"
check "s8: second run yields identical file" "$after_first" "$after_second"

# Empty file refused — same safety as patch_cmdline_txt.
: > "$TMP/s9.txt"
if strip_cmdline_token "cgroup_disable=memory" "$TMP/s9.txt" >/dev/null 2>&1; then
    bad "s9: empty file should be refused"
else
    ok "s9: empty file refused"
fi

# CR/LF input is collapsed to a single line (mirrors patch_cmdline_txt's
# c3 test — symmetry guard so the two helpers don't diverge on the
# tr '\r\n' '  ' normalization in future refactors).
printf 'foo\r\ncgroup_disable=memory\r\nbar\r\n' > "$TMP/s11.txt"
strip_cmdline_token "cgroup_disable=memory" "$TMP/s11.txt" >/dev/null
check "s11: CR/LF input -> one line" "1" "$(wc -l < "$TMP/s11.txt" | tr -d ' ')"
check "s11: target token stripped" "0" "$(grep -cw 'cgroup_disable=memory' "$TMP/s11.txt")"
grep -q 'foo bar' "$TMP/s11.txt" && ok "s11: surrounding tokens re-joined" \
    || bad "s11: surrounding tokens not re-joined"

# Single-token file: stripping the only token would empty the file.
# Refused (a kernel without `root=` etc. bricks boot).
printf 'cgroup_disable=memory\n' > "$TMP/s10.txt"
before_s10="$(cat "$TMP/s10.txt")"
if strip_cmdline_token "cgroup_disable=memory" "$TMP/s10.txt" >/dev/null 2>&1; then
    bad "s10: would-be-empty result should be refused"
else
    ok "s10: would-be-empty result refused"
fi
after_s10="$(cat "$TMP/s10.txt")"
check "s10: file untouched on refusal" "$before_s10" "$after_s10"

# ── patch_config_txt_gpu_mem (r110 c3.3.2-followup, 2026-06-11) ─────
# Bumps GPU memory split to 128M so 1080p ril.video_decode can
# allocate. Idempotent: replace existing gpu_mem=<N> in place,
# or append if absent.

# Append when no gpu_mem= line present.
printf '[all]\nhdmi_force_hotplug=1\n' > "$TMP/g1.txt"
patch_config_txt_gpu_mem "$TMP/g1.txt" >/dev/null
check "g1: exactly one gpu_mem=128 line" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g1.txt")"
grep -q 'hdmi_force_hotplug=1' "$TMP/g1.txt" && ok "g1: existing line preserved" || bad "g1: existing line lost"

# Idempotent re-run does NOT double-append.
patch_config_txt_gpu_mem "$TMP/g1.txt" >/dev/null
check "g1: no double gpu_mem on re-run" "1" "$(grep -cE '^gpu_mem=128' "$TMP/g1.txt")"

# Existing gpu_mem=64 is REPLACED in place (not appended below).
printf '[all]\ngpu_mem=64\nhdmi_force_hotplug=1\n' > "$TMP/g2.txt"
patch_config_txt_gpu_mem "$TMP/g2.txt" >/dev/null
check "g2: gpu_mem=128 present" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g2.txt")"
check "g2: old gpu_mem=64 gone" "0" "$(grep -cE '^gpu_mem=64$' "$TMP/g2.txt")"
grep -q 'hdmi_force_hotplug=1' "$TMP/g2.txt" && ok "g2: post-line preserved" || bad "g2: post-line lost"

# A different existing value (e.g., 256) is also replaced.
printf 'gpu_mem=256\n' > "$TMP/g3.txt"
patch_config_txt_gpu_mem "$TMP/g3.txt" >/dev/null
check "g3: gpu_mem=128 present" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g3.txt")"
check "g3: old gpu_mem=256 gone" "0" "$(grep -cE '^gpu_mem=256$' "$TMP/g3.txt")"

# Commented-out gpu_mem MUST NOT match (so it's NOT replaced).
printf '# gpu_mem=64 (commented out)\n' > "$TMP/g4.txt"
patch_config_txt_gpu_mem "$TMP/g4.txt" >/dev/null
grep -q '^# gpu_mem=64' "$TMP/g4.txt" && ok "g4: commented line preserved" || bad "g4: commented line lost"
check "g4: gpu_mem=128 appended" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g4.txt")"

# BLOCKER-1 pin (sacred subagent): gpu_mem=64 with trailing
# `# comment` junk MUST result in an effective gpu_mem=128
# (the pre-fix lenient-entry / strict-substitution sed would
# silently no-op and the log lied about success).
printf 'gpu_mem=64 # default\n' > "$TMP/g5.txt"
patch_config_txt_gpu_mem "$TMP/g5.txt" >/dev/null
# The old gpu_mem=64 line (with trailing junk) MUST be gone.
check "g5: old gpu_mem=64 line gone" "0" "$(grep -cE '^gpu_mem=64' "$TMP/g5.txt")"
# Exactly one effective gpu_mem= line, and it's 128.
check "g5: exactly one gpu_mem= line" "1" "$(grep -cE '^[[:space:]]*gpu_mem[[:space:]]*=' "$TMP/g5.txt")"
check "g5: gpu_mem=128 present" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g5.txt")"

# BLOCKER-2(a) pin: multiple gpu_mem= lines across [section]
# selectors. The pre-fix idempotency check would find
# gpu_mem=128 in [pi4] and short-circuit while the [all]
# line at 64 keeps the Pi booting at 64M.
printf '[pi4]\ngpu_mem=128\n[all]\ngpu_mem=64\n' > "$TMP/g6.txt"
patch_config_txt_gpu_mem "$TMP/g6.txt" >/dev/null
# Both pre-existing gpu_mem= lines (one 128, one 64) MUST be
# stripped — the appended [all]/gpu_mem=128 at EOF is the
# only one left.
check "g6: exactly one gpu_mem= line" "1" "$(grep -cE '^[[:space:]]*gpu_mem[[:space:]]*=' "$TMP/g6.txt")"
check "g6: gpu_mem=128 present" "1" "$(grep -cE '^gpu_mem=128$' "$TMP/g6.txt")"
check "g6: gpu_mem=64 gone" "0" "$(grep -cE '^gpu_mem=64$' "$TMP/g6.txt")"
# The [pi4] section header from before is preserved (we only
# strip gpu_mem= lines, not section headers).
grep -q '^\[pi4\]' "$TMP/g6.txt" && ok "g6: [pi4] header preserved" || bad "g6: [pi4] header lost"

# BLOCKER-2(b) pin: append emits an EXPLICIT [all] header
# before gpu_mem=128 so scope is pinned regardless of which
# section header was last opened at EOF. Verify the appended
# value's IMMEDIATE preceding section context is [all], not
# any earlier section.
printf '[pi4]\nsome_pi4_only_setting=1\n' > "$TMP/g7.txt"
patch_config_txt_gpu_mem "$TMP/g7.txt" >/dev/null
# The new gpu_mem=128 line must be preceded by an [all] header
# (not just inherit the [pi4] from above). awk: find the last
# [section] header BEFORE the gpu_mem=128 line.
last_section="$(awk '/^\[/{sec=$0} /^gpu_mem=128$/{print sec; exit}' "$TMP/g7.txt")"
check "g7: [all] scope pinned at gpu_mem=128" "[all]" "$last_section"
grep -q '^\[pi4\]' "$TMP/g7.txt" && ok "g7: original [pi4] preserved" || bad "g7: original [pi4] lost"

# BLOCKER-1+2(a) idempotency under the new contract: a
# correctly-already-applied file (single uncommented
# gpu_mem=128 line, last seen [all] header) is a true no-op.
printf '[all]\ngpu_mem=128\n' > "$TMP/g8.txt"
md5_before="$(md5sum < "$TMP/g8.txt" 2>/dev/null || md5 < "$TMP/g8.txt" 2>/dev/null | awk '{print $NF}')"
patch_config_txt_gpu_mem "$TMP/g8.txt" >/dev/null
md5_after="$(md5sum < "$TMP/g8.txt" 2>/dev/null || md5 < "$TMP/g8.txt" 2>/dev/null | awk '{print $NF}')"
check "g8: true no-op (file unchanged byte-for-byte)" "$md5_before" "$md5_after"

# ── patch_cmdline_txt_cma (r110 c3.3.2-followup, 2026-06-11) ────────
# Sets cma=320M, replacing any prior cma= token, preserving the
# single-line cmdline.txt invariant. Idempotent.

# Append when no cma= present.
printf 'console=tty1 root=PARTUUID=abc-02 rootwait\n' > "$TMP/cma1.txt"
patch_cmdline_txt_cma "$TMP/cma1.txt" >/dev/null
check "cma1: one line" "1" "$(wc -l < "$TMP/cma1.txt" | tr -d ' ')"
grep -qw 'cma=320M' "$TMP/cma1.txt" && ok "cma1: cma=320M appended" || bad "cma1: cma=320M missing"
grep -qw 'root=PARTUUID=abc-02' "$TMP/cma1.txt" && ok "cma1: root= preserved" || bad "cma1: root= lost"
grep -qw 'rootwait' "$TMP/cma1.txt" && ok "cma1: rootwait preserved" || bad "cma1: rootwait lost"

# Idempotency: re-run is a no-op.
patch_cmdline_txt_cma "$TMP/cma1.txt" >/dev/null
check "cma1: re-run no double cma=" "1" "$(grep -ow 'cma=320M' "$TMP/cma1.txt" | wc -l | tr -d ' ')"
check "cma1: still one line after re-run" "1" "$(wc -l < "$TMP/cma1.txt" | tr -d ' ')"

# Existing cma=384M is REPLACED with cma=320M.
printf 'console=tty1 cma=384M root=PARTUUID=x rootwait\n' > "$TMP/cma2.txt"
patch_cmdline_txt_cma "$TMP/cma2.txt" >/dev/null
check "cma2: one line" "1" "$(wc -l < "$TMP/cma2.txt" | tr -d ' ')"
check "cma2: cma=320M present" "1" "$(grep -ow 'cma=320M' "$TMP/cma2.txt" | wc -l | tr -d ' ')"
check "cma2: old cma=384M gone" "0" "$(grep -ow 'cma=384M' "$TMP/cma2.txt" | wc -l | tr -d ' ')"
grep -qw 'root=PARTUUID=x' "$TMP/cma2.txt" && ok "cma2: root= preserved" || bad "cma2: root= lost"

# Different existing value (cma=512M) also replaced.
printf 'cma=512M root=PARTUUID=y\n' > "$TMP/cma3.txt"
patch_cmdline_txt_cma "$TMP/cma3.txt" >/dev/null
check "cma3: cma=320M present" "1" "$(grep -ow 'cma=320M' "$TMP/cma3.txt" | wc -l | tr -d ' ')"
check "cma3: old cma=512M gone" "0" "$(grep -ow 'cma=512M' "$TMP/cma3.txt" | wc -l | tr -d ' ')"

# Substring-prefix collision: `cma_zone=foo` MUST NOT be stripped.
printf 'cma_zone=foo cma=384M root=PARTUUID=z\n' > "$TMP/cma4.txt"
patch_cmdline_txt_cma "$TMP/cma4.txt" >/dev/null
grep -qw 'cma_zone=foo' "$TMP/cma4.txt" && ok "cma4: cma_zone=foo preserved" || bad "cma4: cma_zone=foo wrongly stripped"
check "cma4: cma=320M present" "1" "$(grep -ow 'cma=320M' "$TMP/cma4.txt" | wc -l | tr -d ' ')"
check "cma4: old cma=384M gone" "0" "$(grep -ow 'cma=384M' "$TMP/cma4.txt" | wc -l | tr -d ' ')"

# Empty cmdline.txt is REFUSED (same safety as patch_cmdline_txt).
: > "$TMP/cma5.txt"
if patch_cmdline_txt_cma "$TMP/cma5.txt" >/dev/null 2>&1; then
    bad "cma5: empty cmdline.txt should be refused"
else
    ok "cma5: empty cmdline.txt refused"
fi

# Single-token file where the only token IS cma=*: stripping would
# empty → refused.
printf 'cma=384M\n' > "$TMP/cma6.txt"
before_cma6="$(cat "$TMP/cma6.txt")"
if patch_cmdline_txt_cma "$TMP/cma6.txt" >/dev/null 2>&1; then
    bad "cma6: would-be-empty result should be refused"
else
    ok "cma6: would-be-empty result refused"
fi
after_cma6="$(cat "$TMP/cma6.txt")"
check "cma6: file untouched on refusal" "$before_cma6" "$after_cma6"

# Multiple existing cma= tokens (paranoid; cmdline shouldn't have
# this but the function must be safe) — both stripped, single
# cma=320M appended.
printf 'foo cma=128M bar cma=384M baz\n' > "$TMP/cma7.txt"
patch_cmdline_txt_cma "$TMP/cma7.txt" >/dev/null
check "cma7: exactly one cma=320M" "1" "$(grep -ow 'cma=320M' "$TMP/cma7.txt" | wc -l | tr -d ' ')"
check "cma7: cma=128M gone" "0" "$(grep -ow 'cma=128M' "$TMP/cma7.txt" | wc -l | tr -d ' ')"
check "cma7: cma=384M gone" "0" "$(grep -ow 'cma=384M' "$TMP/cma7.txt" | wc -l | tr -d ' ')"
grep -qw foo "$TMP/cma7.txt" && ok "cma7: foo preserved" || bad "cma7: foo lost"
grep -qw bar "$TMP/cma7.txt" && ok "cma7: bar preserved" || bad "cma7: bar lost"
grep -qw baz "$TMP/cma7.txt" && ok "cma7: baz preserved" || bad "cma7: baz lost"

# CR/LF input collapsed to one line + cma=320M applied.
printf 'console=tty1\r\nroot=PARTUUID=q\r\n' > "$TMP/cma8.txt"
patch_cmdline_txt_cma "$TMP/cma8.txt" >/dev/null
check "cma8: CR/LF input -> one line" "1" "$(wc -l < "$TMP/cma8.txt" | tr -d ' ')"
grep -qw 'cma=320M' "$TMP/cma8.txt" && ok "cma8: cma=320M present" || bad "cma8: cma=320M missing"
grep -q 'console=tty1 root=PARTUUID=q' "$TMP/cma8.txt" && ok "cma8: params re-joined" || bad "cma8: params not joined"

if [ "$fail" -eq 0 ]; then
    echo "ALL PASS"
else
    echo "TESTS FAILED"
    exit 1
fi
