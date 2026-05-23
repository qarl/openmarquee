# boot-config-lib.sh — sourceable patch functions for the Pi boot
# files. Kept separate from 02-run.sh so they can be unit-tested
# against temp files without a pi-gen build (see test-boot-config.sh).
#
# Both functions are IDEMPOTENT — safe to run on a fresh pi-gen
# rootfs, on a re-run of the build, and from install.sh's redeploy
# path on an already-provisioned Pi.

# Append `disable_splash=1` to a Pi config.txt.
#
# config.txt is a multi-line file, so a plain newline-append is safe.
# disable_splash=1 silences the firmware rainbow so the plymouth
# splash owns the screen from kernel hand-off onward. Appended at the
# end of the file — the stock layout ends inside the `[all]` section,
# so the setting applies to every model.
patch_config_txt() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "patch_config_txt: $file not found" >&2
        return 1
    fi
    if grep -qE '^[[:space:]]*disable_splash[[:space:]]*=' "$file"; then
        echo "config.txt: disable_splash already set — no-op"
        return 0
    fi
    printf '\n# openMarquee boot splash — silence the firmware rainbow\n# so the plymouth splash owns the screen.\ndisable_splash=1\n' \
        >> "$file"
    echo "config.txt: appended disable_splash=1"
}

# Append the plymouth kernel params to a Pi cmdline.txt, IN PLACE.
#
# DANGER: cmdline.txt is a SINGLE line of space-separated kernel
# params. Every param MUST stay on that one line — a stray newline
# silently drops every param after it, which can leave the kernel
# without `root=` and brick the boot (recoverable only by a physical
# reflash). This function therefore:
#   - reads the whole file and turns every CR/LF into a SPACE
#     (defensive: if the file somehow already spans lines, the tokens
#     are re-joined with a separator, not concatenated),
#   - collapses whitespace runs and trims leading/trailing space,
#   - appends our params and exactly ONE trailing newline,
#   - is idempotent: a re-run with `splash` already present is a
#     no-op, so it never double-appends.
patch_cmdline_txt() {
    local file="$1"
    local add="quiet splash plymouth.ignore-serial-consoles"
    if [ ! -f "$file" ]; then
        echo "patch_cmdline_txt: $file not found" >&2
        return 1
    fi
    if grep -qw splash "$file"; then
        echo "cmdline.txt: 'splash' already present — no-op"
        return 0
    fi
    local current
    current="$(tr '\r\n' '  ' < "$file" | sed 's/[[:space:]]\{1,\}/ /g; s/^ //; s/ $//')"
    if [ -z "$current" ]; then
        echo "patch_cmdline_txt: $file is empty — refusing to patch" >&2
        return 1
    fi
    printf '%s %s\n' "$current" "$add" > "$file"
    echo "cmdline.txt: appended '$add'"
}

# Strip a single named kernel param token from cmdline.txt, IN PLACE.
#
# Same DANGER as patch_cmdline_txt — cmdline.txt is a SINGLE line and
# a stray newline silently drops every param after it. This function:
#   - reads the whole file and re-joins any spanned lines to one,
#   - splits the line on whitespace + drops EXACT-MATCH fields (awk
#     field comparison, NOT regex/sed substring match — so a token
#     `cgroup_disable=memory` does NOT also strip `cgroup_disable=
#     memory_zone` or similar substring-prefix collisions),
#   - writes the result back with exactly ONE trailing newline,
#   - refuses to write an EMPTY file (a cmdline.txt without `root=`
#     bricks boot, recoverable only by physical reflash),
#   - is idempotent: a re-run when the token is already absent
#     short-circuits as a no-op with a clear log line.
#
# Used by postmortem mitigation #5 (2026-05-23) to remove the base
# Pi OS `cgroup_disable=memory` flag, which suppresses kernel PSI /
# cgroup memory accounting and blocks systemd-OOMD policies — the
# substrate for sustained-memory-pressure failure modes.
strip_cmdline_token() {
    local token="$1"
    local file="$2"
    if [ ! -f "$file" ]; then
        echo "strip_cmdline_token: $file not found" >&2
        return 1
    fi
    local current
    current="$(tr '\r\n' '  ' < "$file" | sed 's/[[:space:]]\{1,\}/ /g; s/^ //; s/ $//')"
    if [ -z "$current" ]; then
        echo "strip_cmdline_token: $file is empty — refusing to patch" >&2
        return 1
    fi
    local stripped
    stripped="$(printf '%s\n' "$current" | awk -v t="$token" '{
        out=""
        for (i=1; i<=NF; i++) {
            if ($i != t) {
                if (out=="") out=$i; else out=out" "$i
            }
        }
        print out
    }')"
    if [ "$stripped" = "$current" ]; then
        echo "cmdline.txt: '$token' not present — no-op"
        return 0
    fi
    if [ -z "$stripped" ]; then
        echo "strip_cmdline_token: stripping '$token' would empty $file — refusing" >&2
        return 1
    fi
    printf '%s\n' "$stripped" > "$file"
    echo "cmdline.txt: stripped '$token'"
}
