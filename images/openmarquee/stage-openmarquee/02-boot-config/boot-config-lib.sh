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
