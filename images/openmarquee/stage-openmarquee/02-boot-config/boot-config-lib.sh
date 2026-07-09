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

# Set `gpu_mem=128` in a Pi config.txt, IN PLACE. Idempotent.
#
# r110 c3.3.2-followup (2026-06-11): the stock gpu_mem=64 on the
# Pi Zero 2 W cannot create a `ril.video_decode` MMAL component —
# vchiq returns ETIME with reloc heap starved (~17M/44M free at
# idle). gpu_mem=128 restores enough firmware reloc heap for the
# decoder to allocate (paired patch_cmdline_txt_cma below).
#
# Handover reconcile 2026-07-09 (GAP2): cma pinned at 320M — the
# validated LIVE-sign value (supersedes the earlier 256M pi-gen
# guess). CMA pool drops from 384M (old default) to 320M, freeing
# 64M back to ARM; gpu_mem grows 64M (64->128) and takes it back,
# so ARM headroom net is unchanged vs the old split — within
# budget on a 512MB Zero 2 W. 320M is also less aggressive than
# the old 384M pool, which starved kernel+userspace+tailscale
# (see the cma-aggressive-on-pi-zero-2w note).
#
# Behavior:
#   - exactly ONE `gpu_mem=128` line (uncommented) present →
#     no-op
#   - any other state (zero gpu_mem= lines, one with trailing
#     junk, multiple gpu_mem= lines across [section] headers,
#     etc.) → STRIP all gpu_mem= lines + APPEND a fresh
#     `[all]` / `gpu_mem=128` block at EOF
#
# The strip-and-append shape (rather than in-place sed
# substitution) handles three failure modes a sed-substitute
# would silently miss:
#   (1) sacred subagent BLOCKER-1 — `gpu_mem=64 # comment`
#       trailing junk: the entry pattern matched
#       (`gpu_mem[[:space:]]*=`) but the strict substitution
#       pattern with end-anchor (`[^[:space:]]*$`) did NOT,
#       so sed no-op'd while the log claimed success.
#   (2) sacred subagent BLOCKER-2(a) — multiple `gpu_mem=` lines
#       across `[section]` selectors (e.g. `[pi4]
#       gpu_mem=128` + `[all] gpu_mem=64`): a plain grep for
#       `gpu_mem=128` would short-circuit the idempotency
#       check while the `[all]` line at 64 keeps the Pi Zero
#       2 W booting at 64M.
#   (3) sacred subagent BLOCKER-2(b) — appending a bare
#       `gpu_mem=128` at EOF inherits whatever `[section]`
#       header was last opened. Explicit `[all]` header
#       before the value pins scope regardless of EOF
#       context (and stock pi-gen Trixie ends in `[all]`
#       today, so this also matches the current empirical
#       layout).
#
# Idempotent: a re-run with exactly one uncommented
# `gpu_mem=128` line is a no-op.
patch_config_txt_gpu_mem() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "patch_config_txt_gpu_mem: $file not found" >&2
        return 1
    fi
    # Count uncommented `gpu_mem=` lines. Pattern requires
    # line-start + optional whitespace + literal `gpu_mem`
    # + `=` — so `# gpu_mem=64` is correctly excluded.
    local count
    count="$(grep -cE '^[[:space:]]*gpu_mem[[:space:]]*=' "$file" || true)"
    # Strict idempotency: exactly ONE gpu_mem= line AND it
    # equals gpu_mem=128 with no trailing junk → no-op.
    if [ "$count" = "1" ] && \
       grep -qE '^[[:space:]]*gpu_mem[[:space:]]*=[[:space:]]*128[[:space:]]*$' "$file"; then
        echo "config.txt: gpu_mem=128 already set — no-op"
        return 0
    fi
    # Strip ALL existing gpu_mem= lines (idempotent if none
    # present). Use same-dir mktemp so the mv is intra-fs
    # atomic on boot partitions that span filesystems from
    # /tmp.
    local tmp
    tmp="$(mktemp "${file}.gpu_mem.XXXXXX")"
    sed -E '/^[[:space:]]*gpu_mem[[:space:]]*=/d' "$file" > "$tmp"
    mv "$tmp" "$file"
    # Append fresh [all]/gpu_mem=128 block at EOF — explicit
    # section header pins scope regardless of any prior
    # section selectors above.
    printf '\n# openMarquee r110 c3.3.2-followup (2026-06-11): bump GPU\n# memory split so the ril.video_decode component can allocate\n# (paired with cma=320M in cmdline.txt).\n# Explicit [all] header pins scope across all model variants.\n[all]\ngpu_mem=128\n' \
        >> "$file"
    if [ "$count" -gt "0" ]; then
        echo "config.txt: stripped $count existing gpu_mem= line(s) + appended [all]/gpu_mem=128"
    else
        echo "config.txt: appended [all]/gpu_mem=128"
    fi
}

# Ensure `dtparam=audio=on` is set in a Pi config.txt, IN PLACE.
# Idempotent.
#
# HDMI audio 2026-07-01 (qarl decision, locked): any video with an
# audio track plays its sound out the sign's HDMI. The Pi's vc4hdmi
# ALSA card is exposed by the vc4-kms-v3d driver when
# `dtparam=audio=on` is present (Trixie ships it commented). The
# live sign already has this line; new SD-card images + redeploy
# reruns need the same shape for symmetry.
#
# Trixie stock config.txt has an existing `dtparam=audio=on` line
# commented out ("#dtparam=audio=on"); we want an UNCOMMENTED one.
# Behavior:
#   - exactly ONE uncommented `dtparam=audio=on` line present →
#     no-op
#   - any other state (only commented out, missing, misspelled) →
#     APPEND a fresh `[all]` / `dtparam=audio=on` block at EOF
#
# Uses the same append-with-explicit-header pattern as
# `patch_config_txt_gpu_mem` above so `[section]` context inherited
# from an earlier header can't leave the line scoped to only some
# Pi models.
patch_config_txt_audio() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "patch_config_txt_audio: $file not found" >&2
        return 1
    fi
    # Count UNCOMMENTED `dtparam=audio=on` lines. Line-start + any
    # whitespace + literal `dtparam=audio=on` — no leading `#`.
    if grep -qE '^[[:space:]]*dtparam[[:space:]]*=[[:space:]]*audio[[:space:]]*=[[:space:]]*on[[:space:]]*$' "$file"; then
        echo "config.txt: dtparam=audio=on already set — no-op"
        return 0
    fi
    printf '\n# openMarquee HDMI-audio 2026-07-01: enable the vc4hdmi\n# ALSA card so the Python playback loop can pipe VideoSlide\n# audio out HDMI via ffmpeg. Explicit [all] header pins scope\n# across all model variants.\n[all]\ndtparam=audio=on\n' \
        >> "$file"
    echo "config.txt: appended [all]/dtparam=audio=on"
}

# Set `cma=320M` in a Pi cmdline.txt, IN PLACE. Idempotent.
#
# r110 c3.3.2-followup (2026-06-11): paired with
# patch_config_txt_gpu_mem above. Shrinks the CMA pool from the
# older 384M default to leave headroom for the gpu_mem=128 reloc
# heap bump — together the split frees the firmware reloc heap
# enough for MMAL video-decode component creation.
#
# Handover reconcile 2026-07-09 (GAP2): the value is 320M — the
# VALIDATED live-sign setting (was 256M on the earlier pi-gen
# path). 320M is the reconciled pool size the running sign uses;
# the SD build must match so a fresh burn isn't a different
# memory split from the deployed system.
#
# Same DANGER as patch_cmdline_txt — cmdline.txt is a SINGLE
# line and a stray newline silently drops every param after it,
# bricking boot. This function therefore:
#   - reads the whole file and turns every CR/LF into a SPACE,
#   - drops any existing `cma=*` token via exact-prefix awk
#     field match (so `cma=512M`, `cma=128M`, etc. all get
#     replaced — NOT regex substring; a hypothetical token
#     `cma_zone=foo` would NOT match),
#   - appends `cma=320M` and exactly ONE trailing newline,
#   - is idempotent: a re-run finds `cma=320M` and re-runs
#     the strip+append, which is a no-op (same output).
patch_cmdline_txt_cma() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "patch_cmdline_txt_cma: $file not found" >&2
        return 1
    fi
    local current
    current="$(tr '\r\n' '  ' < "$file" | sed 's/[[:space:]]\{1,\}/ /g; s/^ //; s/ $//')"
    if [ -z "$current" ]; then
        echo "patch_cmdline_txt_cma: $file is empty — refusing to patch" >&2
        return 1
    fi
    # awk field match: drop any token whose substring up to '=' is
    # exactly `cma`. Substring-prefix collisions like `cmaX=Y` are
    # safe because the split-on-`=` test compares the FULL prefix.
    # Same-dir mktemp not strictly needed here (we don't atomic-
    # mv the cmdline.txt via tmp file; we rewrite it directly
    # with the printf below) — but the awk's stdout buffer is
    # the staging area + final write is one redirection.
    local stripped
    stripped="$(printf '%s\n' "$current" | awk '{
        out=""
        for (i=1; i<=NF; i++) {
            eq=index($i, "=")
            key=(eq>0) ? substr($i, 1, eq-1) : $i
            if (key != "cma") {
                if (out=="") out=$i; else out=out" "$i
            }
        }
        print out
    }')"
    if [ -z "$stripped" ]; then
        echo "patch_cmdline_txt_cma: stripping cma= would empty $file — refusing" >&2
        return 1
    fi
    # Idempotency check: if the BEFORE-strip current already has
    # cma=320M AND no other cma=*, the stripped+append result is
    # identical to current → log no-op + skip the write.
    local target_only_cma
    target_only_cma="$current"
    # Detect: exactly one cma= token AND it equals cma=320M.
    local cma_count
    cma_count="$(printf '%s\n' "$current" | awk '{ c=0; for (i=1;i<=NF;i++) { eq=index($i,"="); key=(eq>0)?substr($i,1,eq-1):$i; if (key=="cma") c++ } print c }')"
    if [ "$cma_count" = "1" ] && printf '%s\n' "$current" | grep -qw 'cma=320M'; then
        echo "cmdline.txt: cma=320M already set (exactly one cma= token) — no-op"
        return 0
    fi
    printf '%s %s\n' "$stripped" "cma=320M" > "$file"
    echo "cmdline.txt: set cma=320M (stripped any prior cma= + appended)"
}
