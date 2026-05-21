#!/usr/bin/env bash
# scripts/install.sh — on-device provisioning for an openMarquee Pi.
#
# Idempotent. Runs once on first boot (invoked by cloud-init's runcmd in
# B.2) and again on every developer-mode redeploy (invoked by
# scripts/deploy.sh after rsync). Each step checks before mutating, so
# re-runs are safe and cheap.
#
# Steps (in order):
#   1. State directories (/var/openmarquee/, /var/lib/openmarquee/)
#   2. Python venv at /opt/openmarquee/venv + pip install -e .
#   3. Systemd unit files (openmarquee-backend, openmarquee-ap0,
#      openmarquee-tailscale)
#   3b. Rust IPC sidecar binary (Phase 7 slice 3) -- if staged at
#      /opt/openmarquee/bin/openmarquee-render by deploy.sh, install
#      to /usr/local/bin/ with chmod +x. Opt-in via OPENMARQUEE_
#      RENDERER=rust-sidecar; absence is fine (sidecar isn't used).
#   4. hostapd.conf staging (B.4 first-boot oneshot rotates the password)
#   5. dnsmasq.conf for captive-portal DNS intercept
#   6. iptables rules to redirect ap0 → port 80
#   7. First-boot oneshot trigger (B.4) if /var/openmarquee/.bootstrapped
#      doesn't exist yet
#   8. systemctl daemon-reload + enable units
#
# Usage:
#     sudo bash /opt/openmarquee/scripts/install.sh             # do it
#     bash /opt/openmarquee/scripts/install.sh --dry-run        # print actions
#     bash /opt/openmarquee/scripts/install.sh --root /tmp/test --dry-run
#                                                                # test off-device
#
# Dry-run prints each high-level action prefixed with `DRYRUN:`. The
# --root flag lets the test suite redirect destination paths into a
# tmpdir; in dry-run those paths are reported, not touched.

set -euxo pipefail

# Redirect xtrace output to a dedicated persistent file so the full
# execution trace survives even if cloud-init-output.log is truncated
# or journald state is lost on reboot. Real-device install only —
# guarded so dry-run / --root test invocations don't try to open
# /var/log on the build host. The default-init below lets the gate
# below evaluate before argv parsing has run; argv parsing then
# reassigns DRY_RUN / ROOT_PREFIX, which is fine: the xtrace FD is
# already open and stays redirected for the rest of the script.
: "${DRY_RUN:=0}"
: "${ROOT_PREFIX:=}"
if [ "$DRY_RUN" -eq 0 ] && [ -z "$ROOT_PREFIX" ]; then
    exec {XTRACE_FD}>>/var/log/openmarquee-install-xtrace.log 2>/dev/null || XTRACE_FD=
    if [ -n "${XTRACE_FD:-}" ]; then
        # 0600 for consistency with wifi.json — xtrace echoes any
        # PASSPHRASE-equivalent secrets via expansion.
        chmod 600 /var/log/openmarquee-install-xtrace.log 2>/dev/null || true
        BASH_XTRACEFD=$XTRACE_FD
        export BASH_XTRACEFD
    fi
fi

# --- Defaults / arg parsing -------------------------------------------------

DRY_RUN=0
ROOT_PREFIX=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)  DRY_RUN=1; shift ;;
        --root)     ROOT_PREFIX="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)  echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# --root is for off-device test harness only. Without --dry-run it would
# still issue real iptables / systemctl commands against the LIVE host
# while filesystem paths point at a tmpdir -- worst-of-both-worlds.
# Refuse the combination loudly.
if [ -n "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    echo "error: --root requires --dry-run (host-state ops would still run on real host)" >&2
    exit 2
fi

OPT_DIR="${ROOT_PREFIX}/opt/openmarquee"
VAR_DIR="${ROOT_PREFIX}/var/openmarquee"
LIB_DIR="${ROOT_PREFIX}/var/lib/openmarquee"
SYSTEMD_DIR="${ROOT_PREFIX}/etc/systemd/system"
HOSTAPD_DST="${ROOT_PREFIX}/etc/hostapd/hostapd.conf"
DNSMASQ_DST="${ROOT_PREFIX}/etc/dnsmasq.d/openmarquee.conf"
NM_UNMANAGED_DST="${ROOT_PREFIX}/etc/NetworkManager/conf.d/openmarquee-unmanaged.conf"
BOOTSTRAP_MARKER="${VAR_DIR}/.bootstrapped"

# --- Helpers ----------------------------------------------------------------

# Echo a step header (always shown, in any mode).
say() { printf '==> %s\n' "$*"; }

# Run a command for real, OR print it in dry-run mode.
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRYRUN: %s\n' "$*"
    else
        "$@"
    fi
}

# `run` for shell-built-ins / pipelines that don't compose with run().
run_sh() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf 'DRYRUN: %s\n' "$*"
    else
        bash -c "$*"
    fi
}

# Idempotency: return 0 if the step has already been done.
already_done() {
    if [ "$DRY_RUN" -eq 1 ]; then
        # In dry-run, ALWAYS print what the step would do, even if it's
        # already done in the real filesystem — so the test can assert
        # the full action set.
        return 1
    fi
    test "$@"
}

# Start-of-install pristine-state dump — captures the system BEFORE
# install.sh changes anything. Helps separate "Pi shipped with a weird
# state" from "install.sh broke something" in post-mortem. Inline (not
# via snapshot_state since the function isn't defined yet); appends to
# the same /var/log/openmarquee-debug.log the snapshot_state checkpoints
# will use.
if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    {
        printf '\n=== install.sh START: %s ===\n' "$(date -Iseconds 2>/dev/null || date)"
        printf '\n--- /proc/cmdline ---\n'
        cat /proc/cmdline 2>&1 || true
        printf '\n--- uname -a ---\n'
        uname -a 2>&1 || true
        printf '\n--- /etc/os-release ---\n'
        cat /etc/os-release 2>&1 || true
        printf '\n--- df -h ---\n'
        df -h 2>&1 || true
        printf '\n--- free -h ---\n'
        free -h 2>&1 || true
        printf '\n--- mount ---\n'
        mount 2>&1 || true
        printf '\n--- systemctl list-units --state=failed ---\n'
        systemctl list-units --state=failed --no-pager 2>&1 || true
        printf '\n--- ip link show ---\n'
        ip link show 2>&1 || true
        printf '\n=== end install.sh START dump ===\n'
    } >> /var/log/openmarquee-debug.log 2>&1
    chmod 600 /var/log/openmarquee-debug.log 2>/dev/null || true
fi

# snapshot_state TAG — append a comprehensive system-state snapshot to
# /var/log/openmarquee-debug.log under a section header. Called at multiple
# checkpoints during install (BEFORE_DEBS_INSTALL / AFTER_DEBS_INSTALL /
# AFTER_FIRSTBOOT / END_OF_INSTALL) to capture how state evolves. Pure
# read-only diagnostics; no service restarts, no config changes. Gated to
# real-device install (silent no-op on VM-test / dry-run).
snapshot_state() {
    local tag="$1"
    [ -n "$ROOT_PREFIX" ] && return 0
    [ "$DRY_RUN" -eq 1 ] && return 0
    local debug_log="/var/log/openmarquee-debug.log"
    {
        printf '\n\n================================================================\n'
        printf '=== snapshot: %s at %s ===\n' "$tag" "$(date -Iseconds 2>/dev/null || date)"
        printf '================================================================\n'
        printf '\n--- systemctl status (relevant units) ---\n'
        systemctl status hostapd dnsmasq NetworkManager openmarquee-backend openmarquee-firstboot openmarquee-ap0 2>&1 || true
        printf '\n--- systemctl is-enabled / is-active / is-failed ---\n'
        for u in hostapd dnsmasq NetworkManager openmarquee-backend openmarquee-firstboot openmarquee-ap0; do
            printf '  %s: enabled=%s active=%s failed=%s\n' \
                "$u" \
                "$(systemctl is-enabled "$u" 2>&1 || echo unknown)" \
                "$(systemctl is-active "$u" 2>&1 || echo unknown)" \
                "$(systemctl is-failed "$u" 2>&1 || echo unknown)"
        done
        printf '\n--- ip link show ---\n'
        ip link show wlan0 2>&1 || true
        ip link show ap0 2>&1 || true
        printf '\n--- ip addr show ---\n'
        ip addr show wlan0 2>&1 || true
        ip addr show ap0 2>&1 || true
        printf '\n--- iw dev ---\n'
        iw dev 2>&1 || true
        printf '\n--- hostapd.conf (passphrase redacted) ---\n'
        if [ -f /etc/hostapd/hostapd.conf ]; then
            sed -E 's|^(wpa_passphrase=).*|\1REDACTED|' /etc/hostapd/hostapd.conf 2>&1 || true
        else
            printf '  (file not present)\n'
        fi
        printf '\n--- /etc/default/hostapd ---\n'
        if [ -f /etc/default/hostapd ]; then
            cat /etc/default/hostapd 2>&1 || true
        else
            printf '  (not present)\n'
        fi
        printf '\n--- stat /etc/hostapd/hostapd.conf ---\n'
        if [ -e /etc/hostapd/hostapd.conf ]; then
            stat /etc/hostapd/hostapd.conf 2>&1 || true
        else
            printf '  (not present)\n'
        fi
        printf '\n--- pgrep -af hostapd ---\n'
        pgrep -af hostapd 2>&1 || echo "  (no hostapd process)"
        printf '\n--- pgrep -af dnsmasq ---\n'
        pgrep -af dnsmasq 2>&1 || echo "  (no dnsmasq process)"
        printf '\n--- pgrep -af NetworkManager ---\n'
        pgrep -af NetworkManager 2>&1 || echo "  (no NM process)"
        printf '\n--- journalctl -b -u hostapd (tail 200) ---\n'
        journalctl -b -u hostapd --no-pager 2>&1 | tail -200 || true
        printf '\n--- journalctl -b -u openmarquee-ap0 (tail 100) ---\n'
        journalctl -b -u openmarquee-ap0 --no-pager 2>&1 | tail -100 || true
        printf '\n--- journalctl -b -u openmarquee-firstboot (tail 300) ---\n'
        journalctl -b -u openmarquee-firstboot --no-pager 2>&1 | tail -300 || true
        printf '\n--- nmcli device status ---\n'
        nmcli device status 2>&1 || true
        printf '\n--- nmcli connection show ---\n'
        nmcli connection show 2>&1 || true
        printf '\n--- nmcli connection show --active ---\n'
        nmcli connection show --active 2>&1 || true
        printf '\n--- nft list ruleset ---\n'
        nft list ruleset 2>&1 || true
        printf '\n--- iptables-save (if present) ---\n'
        iptables-save 2>&1 || echo "  (iptables-save not available)"
        printf '\n--- ss -tlnp (listening TCP sockets) ---\n'
        ss -tlnp 2>&1 || true
        printf '\n--- ss -ulnp (listening UDP sockets) ---\n'
        ss -ulnp 2>&1 || true
        printf '\n--- /proc/cmdline ---\n'
        cat /proc/cmdline 2>&1 || true
        printf '\n--- uname -a ---\n'
        uname -a 2>&1 || true
        printf '\n--- /etc/os-release ---\n'
        cat /etc/os-release 2>&1 || true
    } >> "$debug_log" 2>&1
    chmod 600 "$debug_log" 2>/dev/null || true
}

# EXIT trap: snapshot final state on non-zero exit. Happy-path snapshots
# happen at named checkpoints (BEFORE_DEBS / AFTER_DEBS / AFTER_FIRSTBOOT /
# END_OF_INSTALL); this catches catastrophic mid-step failures that fire
# set -e before reaching the next named checkpoint. BASH_LINENO[0] is the
# line of the caller (set -e exit unwinds the stack so this reports the
# line that triggered the failure).
_install_on_exit() {
    local rc=$?
    if [ "$rc" -ne 0 ] && [ -z "${ROOT_PREFIX:-}" ] && [ "${DRY_RUN:-0}" -eq 0 ]; then
        snapshot_state "EXIT_FAILED_RC_${rc}_LINE_${BASH_LINENO[0]:-unknown}"
    fi
    return $rc
}
trap _install_on_exit EXIT

# --- 1. State directories ---------------------------------------------------

say "Ensure state directories"
run mkdir -p "$VAR_DIR" "$LIB_DIR"
# 0750 so openmarquee can read+write but other system users can't peek at
# settings.json (carries the AP/station passwords + Tailscale auth key).
run chmod 0750 "$VAR_DIR"
run chmod 0755 "$LIB_DIR"
# Best-effort ownership; skip if running outside a real system (--root for
# tests, no `openmarquee` user in tmpdir).
if [ -z "$ROOT_PREFIX" ]; then
    run chown openmarquee:openmarquee "$VAR_DIR" "$LIB_DIR"
fi

# --- 2. Python venv ---------------------------------------------------------

VENV_DIR="${OPT_DIR}/venv"
say "Ensure Python venv at ${VENV_DIR}"
if already_done -f "${VENV_DIR}/pyvenv.cfg"; then
    say "  venv already present; skip create"
else
    run python3 -m venv "$VENV_DIR"
fi
say "Install backend package into venv (pip install -e .)"
# requirements.lock first (CVE-pinned dep tree), then editable install of
# the backend package itself for the openmarquee module + entry points.
# Both are idempotent — pip checks sha + skips no-op installs.
#
# Phase 4a 2026-05-15: prefer vendored wheels (factory-fresh offline-
# install promise). build_sd_bundle.sh ships aarch64 wheels at
# ${OPT_DIR}/wheels when bundled with --no-wheels=0 (the default).
# When the directory is present, install fully offline:
#   --no-index            don't consult PyPI
#   --find-links=$WHEELS  resolve from the bundled directory only
#   --no-build-isolation  reuse the venv's pre-installed setuptools/pip
#                         so `pip install -e .` doesn't trigger a PEP-517
#                         build-deps fetch from PyPI.
# When wheels/ is absent (e.g. dev redeploys, --no-wheels bundles), fall
# back to the previous online behavior. The cloud-init code path on
# fresh-flashed SD cards always ships wheels/ — so first-boot has no
# network dependency. A bundle that DOES ship wheels/ but is missing a
# required package fails LOUDLY here with pip's "No matching distribution"
# message, naming the missing package -- exactly what the operator needs.
PIP_OFFLINE_FLAGS=()
WHEELS_DIR="${OPT_DIR}/wheels"
if [ -d "$WHEELS_DIR" ] && [ -n "$(ls -A "$WHEELS_DIR" 2>/dev/null)" ]; then
    say "  found vendored wheels at $WHEELS_DIR — installing offline"
    PIP_OFFLINE_FLAGS=(--no-index "--find-links=$WHEELS_DIR" --no-build-isolation)
else
    say "  no vendored wheels at $WHEELS_DIR — falling back to online pip"
fi
# Bootstrap PEP-517 build backend into the venv. Python 3.13's
# `python3 -m venv` no longer seeds setuptools+wheel; the editable
# backend install below uses --no-build-isolation, so setuptools.build_meta
# must already exist in the venv (pip will not fetch build deps offline).
# setuptools+wheel are pure-Python py3-none-any wheels — they install via
# zip-extract without needing a build backend themselves (no chicken-and-
# egg). Unconditional: the online-fallback branch hits the same Python-
# 3.13-venv-lacks-setuptools issue. pip is deliberately excluded —
# upgrading pip in-place has known edge cases (rewriting its own console
# script), and the ensurepip-bundled pip is sufficient for our use.
run "${VENV_DIR}/bin/pip" install --upgrade ${PIP_OFFLINE_FLAGS[@]+"${PIP_OFFLINE_FLAGS[@]}"} setuptools wheel
if [ -f "${OPT_DIR}/backend/requirements.lock" ]; then
    run "${VENV_DIR}/bin/pip" install --upgrade ${PIP_OFFLINE_FLAGS[@]+"${PIP_OFFLINE_FLAGS[@]}"} -r "${OPT_DIR}/backend/requirements.lock"
fi
run "${VENV_DIR}/bin/pip" install --upgrade ${PIP_OFFLINE_FLAGS[@]+"${PIP_OFFLINE_FLAGS[@]}"} -e "${OPT_DIR}/backend"

# --- 3. Systemd unit files --------------------------------------------------

say "Install systemd units"
run mkdir -p "$SYSTEMD_DIR"
for unit in openmarquee-backend.service openmarquee-ap0.service openmarquee-tailscale.service; do
    SRC="${OPT_DIR}/system/${unit}"
    DST="${SYSTEMD_DIR}/${unit}"
    if already_done -f "$DST" && already_done "$SRC" -nt "$DST"; then
        # Source is newer than installed unit; update.
        run cp "$SRC" "$DST"
    elif already_done -f "$DST"; then
        say "  ${unit} up to date; skip"
    else
        run cp "$SRC" "$DST"
    fi
done

# --- 3a. Ensure +x on system/*.sh helpers -----------------------------------
#
# Task #99 investigation (2026-05-14): the deployed Pi had system/*.sh
# files at -rw-r--r-- despite Mac source being -rwxr-xr-x. `rsync -avz`
# SHOULD preserve perms (--perms is part of -a) and the tar-cf/-xf bundle
# path should too, but the dev Pi shows otherwise. Belt-and-suspenders:
# explicitly chmod +x the .sh ExecStart= targets so systemd can invoke
# them. Idempotent: chmod +x on an already-executable file is a no-op.
say "Ensure +x on system/*.sh helpers"
for sh_helper in openmarquee-ap0-setup.sh openmarquee-firstboot.sh openmarquee-tailscale.sh; do
    SH_PATH="${OPT_DIR}/system/${sh_helper}"
    if [ "$DRY_RUN" -eq 1 ] || [ -f "$SH_PATH" ]; then
        run chmod +x "$SH_PATH"
    fi
done

# --- 3b. Rust IPC sidecar binary (Phase 7 slice 3) --------------------------

# Install the openmarquee-render binary to /usr/local/bin/ if deploy.sh
# staged it under /opt/openmarquee/bin/. This binary is opt-in via
# OPENMARQUEE_RENDERER=rust-sidecar (see backend/openmarquee/
# dependencies.py); a missing staged binary just means the operator
# hasn't enabled the sidecar yet -- not an error.
RUST_BIN_STAGED="${OPT_DIR}/bin/openmarquee-render"
RUST_BIN_INSTALLED="${ROOT_PREFIX}/usr/local/bin/openmarquee-render"
say "Install Rust IPC sidecar binary (opt-in via OPENMARQUEE_RENDERER=rust-sidecar)"
if [ "$DRY_RUN" -eq 1 ] || [ -f "$RUST_BIN_STAGED" ]; then
    run mkdir -p "$(dirname "$RUST_BIN_INSTALLED")"
    run cp "$RUST_BIN_STAGED" "$RUST_BIN_INSTALLED"
    run chmod +x "$RUST_BIN_INSTALLED"
else
    say "  no staged binary at ${RUST_BIN_STAGED}; skip (sidecar opt-in unused)"
fi

# --- 4. hostapd.conf --------------------------------------------------------

say "Stage hostapd.conf"
run mkdir -p "$(dirname "$HOSTAPD_DST")"
# Always overwrite from source -- B.4's first-boot oneshot templates the
# wpa_passphrase line in /etc/hostapd/hostapd.conf AFTER this lays the
# base file. On subsequent install.sh re-runs the templated value gets
# replaced back to the placeholder; the oneshot re-runs unconditionally
# only on first boot, so without a guard a redeploy would silently
# revert the AP password. Guard: skip the copy if /etc/hostapd/hostapd.conf
# already exists AND /var/openmarquee/wifi.json exists (meaning the
# oneshot has already templated; we mustn't overwrite).
if already_done -f "$HOSTAPD_DST" && already_done -f "${VAR_DIR}/wifi.json"; then
    say "  AP password already templated; skip overwrite"
else
    run cp "${OPT_DIR}/system/hostapd.conf" "$HOSTAPD_DST"
fi

# --- 5. dnsmasq.conf --------------------------------------------------------

say "Stage dnsmasq.conf"
run mkdir -p "$(dirname "$DNSMASQ_DST")"
# dnsmasq.conf is fully static -- no per-device templating -- so overwriting
# on every install.sh run is fine and even desirable (picks up changes).
run cp "${OPT_DIR}/system/dnsmasq.conf" "$DNSMASQ_DST"

# --- 5a. NetworkManager unmanaged-devices for ap0 ---------------------------
#
# Phase 4u: keep ap0 out of NetworkManager's hands. The boot-9 forensic
# logs (2026-05-16) showed NM discovering ap0 as a wifi device and trying
# to activate the user's pre-configured WiFi keyfile on it (the keyfile
# has no `interface-name=` pin to wlan0, so it matches the first wifi
# iface NM sees). brcmfmac then panics with `setting AP mode failed -52`
# while NM tries to flip ap0 from AP to STA mode; regulatory domain
# thrashes to WORLD; both AP and STA collapse.
#
# The earlier in-script `nmcli dev set ap0 managed no` in
# openmarquee-ap0-setup.sh fails silently when ap0-setup runs Before=
# NetworkManager — NM is not yet up to receive the command. This conf.d
# drop-in is read by NM at startup, so the directive is in place before
# NM ever sees ap0.
say "Stage NetworkManager unmanaged-devices drop-in"
run mkdir -p "$(dirname "$NM_UNMANAGED_DST")"
run cp "${OPT_DIR}/system/NetworkManager-openmarquee-unmanaged.conf" "$NM_UNMANAGED_DST"

# --- 5.5. Install vendored trixie packages ----------------------------------
#
# Stock Pi OS Lite arm64 trixie does NOT ship hostapd or iptables, but
# the iptables + hostapd sections below assume both are on PATH. We
# bundle .debs for both (+ transitive dep closure) at build time via
# build_sd_bundle.sh's section 3b, and dpkg-install them here on first
# boot. This preserves the factory-fresh-offline promise — no apt + no
# network needed at first boot.
#
# /opt/openmarquee/debs/ may be absent in dev-redeploy bundles (where
# the dev Pi already has the packages installed by a prior install.sh
# run); skip silently in that case.
say "Install vendored trixie packages"
DEBS_DIR="${OPT_DIR}/debs"
snapshot_state "BEFORE_DEBS_INSTALL"
if [ -d "$DEBS_DIR" ] && [ -n "$(ls -A "$DEBS_DIR"/*.deb 2>/dev/null)" ]; then
    say "  found vendored .debs at $DEBS_DIR — installing"
    # apt install accepts local .deb paths and computes install order
    # from each package's Depends: field — unlike `dpkg -i` which is
    # argv-order-sensitive and would have iptables fail its first
    # configure pass when libip4tc2 hasn't been configured yet (alphabetical
    # glob expansion puts iptables before its libs). apt also gracefully
    # accepts already-installed packages at same-or-newer version
    # (idempotent on re-run). --no-install-recommends keeps the closure
    # tight; no network needed when the closure is complete. `|| true`
    # is defense-in-depth: if apt returns non-zero for a non-fatal
    # reason (e.g. a warning surfaced as exit-1 by a future apt change),
    # we don't want set -e to fail-stop the install when the .debs are
    # already correctly applied.
    #
    # Phase 4u: mask hostapd + dnsmasq BEFORE the apt install so the .deb
    # postinst auto-start does NOT fire before ap0 exists and before
    # firstboot.service has templated their configs. Without this,
    # hostapd retries on a nonexistent ap0, hits Restart=on-failure →
    # StartLimitBurst → unit marked failed, then install.sh's
    # `systemctl enable` (no --now) at section 8 does NOT activate
    # ap0.service because multi-user.target is already reached during
    # cloud-init. Result on boot 9 (2026-05-16 forensic logs): hostapd
    # loops forever with `Could not read interface ap0 flags: No such
    # device`. Mask only stops FUTURE invocations; the explicit start
    # sequence in section 8 below resets the failed state and brings
    # the units up against the now-extant ap0 and now-templated configs.
    run systemctl mask hostapd.service dnsmasq.service || true
    run apt install -y --no-install-recommends "$DEBS_DIR"/*.deb || true
else
    say "  no vendored .debs at $DEBS_DIR — assuming hostapd + iptables + dnsmasq already installed (dev-redeploy)"
fi
snapshot_state "AFTER_DEBS_INSTALL"

# --- 5.6. ffmpeg availability check (STREAM/VLC feature) --------------------
#
# ffmpeg is a runtime dependency of the Stream feature — the stream
# takeover and the stream playlist slide both shell out to it
# (backend/openmarquee/stream_consumer.py). It ships in the pi-gen
# base-image package list (images/openmarquee/.../00-packages), so
# factory-flashed cards have it. But a Pi provisioned BEFORE ffmpeg
# landed in that list won't — and install.sh runs offline (no apt), so
# it cannot fix that here. Warn loudly + actionably instead. Absence is
# non-fatal: every non-VLC feature still works.
say "Check ffmpeg availability (STREAM/VLC feature)"
if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    if command -v ffmpeg >/dev/null 2>&1; then
        say "  ffmpeg present: $(command -v ffmpeg)"
    else
        say "  WARNING: ffmpeg not found on PATH — VLC stream slides and the"
        say "           RTSP takeover will not render. This Pi predates ffmpeg"
        say "           in the image package list; run 'sudo apt install ffmpeg'"
        say "           (needs network) to enable the STREAM/VLC feature."
    fi
else
    say "DRYRUN: would check ffmpeg availability (warn if absent)"
fi

# --- 6. iptables redirect rules ---------------------------------------------

say "Apply iptables captive-portal redirect"
# Redirect all TCP destined for port 80 on the ap0 interface to our local
# uvicorn. Idempotency via -C (check before insert).
IPT_RULE=(
    -t nat -A PREROUTING -i ap0 -p tcp --dport 80
    -j DNAT --to-destination 10.0.0.1:80
)
IPT_CHECK=(-t nat -C PREROUTING -i ap0 -p tcp --dport 80
           -j DNAT --to-destination 10.0.0.1:80)
if already_done iptables "${IPT_CHECK[@]}" 2>/dev/null; then
    say "  rule already present; skip"
else
    run iptables "${IPT_RULE[@]}"
fi

# Persist across reboot. iptables-save dumps the live ruleset to a file
# that iptables-restore reads at boot. We don't install iptables-persistent
# here -- iptables itself is in B.1's 00-packages, and the restore-on-boot
# wiring lives in /etc/network/if-pre-up.d/ courtesy of the package.
IPT_RULES_FILE="${ROOT_PREFIX}/etc/iptables/rules.v4"
if [ "$DRY_RUN" -eq 1 ]; then
    printf 'DRYRUN: mkdir -p %s\n' "$(dirname "$IPT_RULES_FILE")"
    printf 'DRYRUN: iptables-save > %s\n' "$IPT_RULES_FILE"
else
    mkdir -p "$(dirname "$IPT_RULES_FILE")"
    iptables-save > "$IPT_RULES_FILE"
fi

# --- 7. First-boot oneshot service + redeploy re-templating -----------------

# Install the .service file unconditionally so deploy.sh-rsync'd updates
# to the unit body (e.g. tightened hardening directives) take effect.
# In dry-run mode the source file isn't present in the tmpdir, so we
# unconditionally print the cp action.
say "Stage openmarquee-firstboot service file"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-firstboot.service" ]; then
    run cp "${OPT_DIR}/system/openmarquee-firstboot.service" \
           "${SYSTEMD_DIR}/openmarquee-firstboot.service"
fi

# Phase 4r drop-ins: append stdout+stderr of firstboot, openmarquee-ap0,
# and hostapd to dedicated persistent log files alongside journal capture.
# The hostapd drop-in additionally dumps the config DAEMON_CONF points
# at on every start (passphrase redacted) — so boot-8-style "config on
# disk is correct but hostapd broadcast empty SSID" mysteries are
# answerable by inspecting /var/log/hostapd.log directly.
say "Stage service-unit drop-in log redirects"
for unit in openmarquee-firstboot openmarquee-ap0 hostapd; do
    src="${OPT_DIR}/system/${unit}.service.d/log-to-file.conf"
    dst_dir="${SYSTEMD_DIR}/${unit}.service.d"
    dst="${dst_dir}/log-to-file.conf"
    if [ "$DRY_RUN" -eq 1 ] || [ -f "$src" ]; then
        run install -d -m 0755 "$dst_dir"
        run install -m 0644 "$src" "$dst"
    fi
done

# --- 7b. sudoers (wifi-station applier) -------------------------------------
#
# Drop the narrowly-scoped sudoers fragment that lets the openmarquee
# user restart/stop wpa_supplicant@wlan0 without a password. The
# backend's wifi_station.py shells out to those two systemctl calls
# when the operator updates home-WiFi creds via the Settings UI.
#
# /etc/sudoers.d/openmarquee MUST be mode 0440 (sudoers refuses to
# parse it otherwise). visudo -c validates the full /etc/sudoers tree
# including the .d/ fragments; abort the install if validation trips.
say "Stage openmarquee-sudoers"
SUDOERS_DST="${ROOT_PREFIX}/etc/sudoers.d/openmarquee"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-sudoers" ]; then
    run mkdir -p "$(dirname "$SUDOERS_DST")"
    run cp "${OPT_DIR}/system/openmarquee-sudoers" "$SUDOERS_DST"
    run chmod 0440 "$SUDOERS_DST"
    if [ "$DRY_RUN" -eq 0 ]; then
        if ! visudo -c -f "$SUDOERS_DST" >/dev/null; then
            echo "FAIL: visudo rejected $SUDOERS_DST" >&2
            exit 1
        fi
    else
        printf 'DRYRUN: visudo -c -f %s\n' "$SUDOERS_DST"
    fi
fi

if [ ! -f "$BOOTSTRAP_MARKER" ] && [ "$DRY_RUN" -eq 0 ]; then
    # First boot. `enable --now` is synchronous for Type=oneshot units --
    # blocks until firstboot.sh exits, so hostapd.conf + welcome.html
    # are templated by the time we move on to backend restart.
    say "First boot detected; running openmarquee-firstboot.service"
    run systemctl enable --now openmarquee-firstboot.service
    snapshot_state "AFTER_FIRSTBOOT"
elif [ "$DRY_RUN" -eq 1 ]; then
    # In real mode this block fires ONLY when marker is absent. Dry-run
    # prints unconditionally for test-coverage visibility -- the redeploy
    # block below is similarly always printed in dry-run.
    say "DRYRUN first-boot path (real mode: only when marker absent)"
    printf 'DRYRUN: systemctl enable --now openmarquee-firstboot.service\n'
fi

# Redeploy path (B.5): if .bootstrapped is present, the systemd unit
# won't re-fire (ConditionPathExists guard + ExecStartPost disabled
# the unit on first run). But scripts/deploy.sh rsyncs welcome.html
# back to its placeholders on every redeploy. Re-run firstboot.sh
# DIRECTLY to re-template welcome.html + hostapd.conf from the
# existing wifi.json. firstboot.sh's idempotency means this re-uses
# the same SSID + passphrase -- no churn.
if [ "$DRY_RUN" -eq 1 ] || [ -f "$BOOTSTRAP_MARKER" ]; then
    if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-firstboot.sh" ]; then
        # In real mode this fires ONLY when marker is present (i.e. it's
        # a redeploy). Dry-run prints unconditionally for test coverage.
        say "Re-running firstboot.sh for redeploy templating (real mode: only when marker present; idempotent)"
        run bash "${OPT_DIR}/system/openmarquee-firstboot.sh"
        snapshot_state "AFTER_FIRSTBOOT"
    fi
fi

# --- 7c. Enable persistent journal ------------------------------------------
#
# Pi OS Lite default journald config is Storage=auto, which persists only
# if /var/log/journal exists with correct perms. The directory ships empty
# on stock Pi OS Lite — and a runtime-only journal disappears at reboot,
# which is exactly the gap that masked boot 7's firstboot.service failure
# mode and forced this whole forensic arc. Force persistence via a drop-in
# so future failures of openmarquee-* services leave a recoverable trail
# in `journalctl --boot=-1`.
#
# Gated to real-device install (skip in ROOT_PREFIX build / DRY_RUN). On
# the build host: no systemd running, possibly no systemd-journal group.

if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    say "Enable persistent journal"
    JOURNALD_DROPIN="/etc/systemd/journald.conf.d/openmarquee-persistent.conf"
    mkdir -p "$(dirname "$JOURNALD_DROPIN")"
    cat > "$JOURNALD_DROPIN" <<'JOURNALD_EOF'
[Journal]
Storage=persistent
JOURNALD_EOF
    mkdir -p /var/log/journal
    # systemd-journal group exists on stock trixie. Resolve by NAME (not
    # a hardcoded numeric GID) so the chown picks up the right GID on
    # this image. If the group is somehow missing, log a warning and
    # leave the directory with its default ownership — journald will
    # fall back to runtime in that pathological case.
    if getent group systemd-journal >/dev/null; then
        chown root:systemd-journal /var/log/journal
        chmod 2755 /var/log/journal
    else
        say "  WARN: systemd-journal group missing; journal directory perms not set"
    fi
    # Reload journald to pick up the new storage mode immediately. Best-
    # effort; failure here is non-fatal (next reboot picks it up anyway).
    systemctl restart systemd-journald 2>/dev/null || true
elif [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: would enable persistent journal at /var/log/journal"
fi

# --- 7d. Boot splash --------------------------------------------------------
#
# Apply the Plymouth boot splash to an ALREADY-PROVISIONED Pi. A
# factory-flashed card gets the splash from the pi-gen image build
# (substages 01-plymouth-theme / 02-boot-config / 03-plymouth-handoff);
# this section replicates the SAME four effects on the redeploy path so
# a live sign picks up the splash without a reflash:
#   1. plymouth package installed
#   2. the openmarquee theme laid into /usr/share/plymouth/themes/
#   3. plymouth-set-default-theme -R openmarquee (rebuilds the initramfs)
#   4. config.txt + cmdline.txt patched (disable_splash + quiet/splash)
#   5. the plymouth-quit --retain-splash handoff drop-in installed
#
# The boot splash is NON-CRITICAL to device function. Every step here
# is best-effort: if plymouth can't be fetched (offline / apt failure)
# we log it via `say` and continue — we do NOT abort the install.
#
# Idempotent: re-running on an already-splashed Pi is a clean no-op
# (apt sees plymouth installed, the theme copy overwrites identically,
# plymouth-set-default-theme is idempotent, the boot-config-lib patch
# functions short-circuit when their marker is already present, and the
# drop-in copy overwrites identically).
#
# Slotted BEFORE section 8 deliberately: section 8 runs the single
# `systemctl daemon-reload`, which picks up the handoff drop-in this
# section installs — no separate daemon-reload needed here.
say "Apply boot splash (Plymouth)"

# All the boot-splash source assets ship in the bundle under
# /opt/openmarquee/images/ (build_sd_bundle.sh copies the pi-gen
# substage tree). SPLASH_STAGE is the stage-openmarquee/ root.
SPLASH_STAGE="${OPT_DIR}/images/openmarquee/stage-openmarquee"

# 1. Install the plymouth package. install.sh otherwise runs offline
#    (no apt) on most paths, so this may fail when there's no network
#    or no apt cache — that's tolerated: the apt call sits in an `if`
#    (which suspends set -e for its condition), so a failure just logs
#    a `say` warning and continues rather than fail-stopping the
#    install. The presence check makes a re-run quiet (apt is itself
#    idempotent, but skipping the call avoids needless apt chatter).
PLYMOUTH_PRESENT=0
if [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: would apt-get install -y plymouth (if not already present)"
    # Assume present in dry-run so the rest of the section prints its
    # full action set for test visibility.
    PLYMOUTH_PRESENT=1
elif command -v plymouth >/dev/null 2>&1; then
    say "  plymouth already installed; skip apt"
    PLYMOUTH_PRESENT=1
else
    say "  plymouth not present; attempting apt-get install"
    if apt-get install -y plymouth; then
        PLYMOUTH_PRESENT=1
        say "  plymouth installed"
    else
        say "  WARNING: could not install plymouth (offline / apt failure?)"
        say "           boot splash skipped — non-critical, device still works."
        say "           Run 'sudo apt install plymouth' (needs network) then"
        say "           re-run install.sh to enable the splash."
    fi
fi

# 2. Install the openmarquee Plymouth theme. The 4 theme files
#    (openmarquee.plymouth / .script / splash.png / spinner.png) live
#    under the 01-plymouth-theme substage's files/openmarquee/ dir.
#    generate_splash.py is the reproducible-artwork source and is
#    deliberately NOT copied into the rootfs (mirrors 01-run.sh).
if [ "$PLYMOUTH_PRESENT" -eq 1 ]; then
    THEME_SRC="${SPLASH_STAGE}/01-plymouth-theme/files/openmarquee"
    THEME_DST="${ROOT_PREFIX}/usr/share/plymouth/themes/openmarquee"
    say "Install Plymouth theme to ${THEME_DST}"
    if [ "$DRY_RUN" -eq 1 ] || [ -d "$THEME_SRC" ]; then
        run install -d -m 755 "$THEME_DST"
        for theme_file in openmarquee.plymouth openmarquee.script splash.png spinner.png; do
            run install -m 644 "${THEME_SRC}/${theme_file}" "${THEME_DST}/${theme_file}"
        done
    else
        say "  theme source ${THEME_SRC} absent; skip (bundle predates boot splash)"
    fi

    # 3. Select the openmarquee theme as the system default. -R rebuilds
    #    the initramfs so the splash is available from early boot.
    #    Idempotent (re-selecting the same theme is a no-op). Gated to a
    #    real-device install — plymouth-set-default-theme writes
    #    /etc/plymouth/plymouthd.conf + runs update-initramfs, neither of
    #    which makes sense against a tmpdir --root.
    if [ -z "$ROOT_PREFIX" ]; then
        say "Set openmarquee as the default Plymouth theme (-R rebuilds initramfs)"
        run plymouth-set-default-theme -R openmarquee
        # Confirm openmarquee actually became the default. Query the
        # current default directly -- `plymouth-set-default-theme` with
        # no args prints it -- rather than parsing the `-l` theme list:
        # the list query can transiently miss a just-installed theme
        # right after -R (a caching/timing quirk on a fresh plymouth
        # install -- observed false-failing an FYS rollout 2026-05-21).
        # Non-fatal: the boot splash is not device-critical, so a
        # mismatch is a WARNING, consistent with the rest of 7d.
        cur_theme="$(plymouth-set-default-theme 2>/dev/null || true)"
        if [ "$cur_theme" = openmarquee ]; then
            say "  default Plymouth theme = openmarquee"
        else
            say "  WARNING: default Plymouth theme is '${cur_theme}', not openmarquee — splash may not display"
        fi
    else
        say "DRYRUN: would run plymouth-set-default-theme -R openmarquee"
    fi
fi

# 4. Patch the Pi boot files (config.txt + cmdline.txt).
#
#    REUSE THE TESTED LIBRARY — DO NOT REIMPLEMENT. cmdline.txt is a
#    single-line file of space-separated kernel params: a stray newline
#    silently drops every param after it and can leave the kernel with
#    no root= — a boot-bricking edit recoverable only by a physical
#    reflash. images/.../02-boot-config/boot-config-lib.sh already has
#    `patch_config_txt` + `patch_cmdline_txt`, both idempotent and
#    unit-tested by test-boot-config.sh. We `source` that lib and CALL
#    those functions rather than writing a fresh, untested cmdline patch
#    here.
#
#    DRY_RUN safety: the lib's patch_* functions modify the boot files
#    DIRECTLY — they do not go through install.sh's `run` wrapper. So
#    under --dry-run we must NOT call them; we only `say` what would
#    happen. The `if [ "$DRY_RUN" -eq 0 ]` guard below enforces that.
BOOT_LIB="${SPLASH_STAGE}/02-boot-config/boot-config-lib.sh"
if [ -f "$BOOT_LIB" ]; then
    # shellcheck source=/dev/null
    source "$BOOT_LIB"
    # Resolve the boot dir the same way 02-run.sh does: trixie puts the
    # boot partition at /boot/firmware, older layouts at /boot.
    boot_dir=""
    for candidate in "${ROOT_PREFIX}/boot/firmware" "${ROOT_PREFIX}/boot"; do
        if [ -f "${candidate}/cmdline.txt" ] && [ -f "${candidate}/config.txt" ]; then
            boot_dir="$candidate"
            break
        fi
    done
    if [ -z "$boot_dir" ]; then
        # Non-fatal: a missing boot dir means we can't patch, but the
        # device still functions (the splash just won't show). Mirrors
        # the boot-splash-is-non-critical stance for the whole section.
        say "  WARNING: cmdline.txt + config.txt not found under ${ROOT_PREFIX}/boot[/firmware]"
        say "           skipping boot-config patch — splash may not display."
    else
        say "Patch boot config in ${boot_dir} (config.txt + cmdline.txt)"
        if [ "$DRY_RUN" -eq 0 ]; then
            # patch_* modify the files directly; both are idempotent and
            # short-circuit when the marker is already present. They
            # `return 1` on a missing / empty boot file — guard the
            # calls so that non-zero can't trip `set -e` and abort the
            # whole redeploy: the boot splash is non-critical, a bad
            # boot file must not break a production FYS redeploy.
            patch_config_txt  "${boot_dir}/config.txt" \
                || say "  WARNING: config.txt patch skipped (see above)"
            patch_cmdline_txt "${boot_dir}/cmdline.txt" \
                || say "  WARNING: cmdline.txt patch skipped (see above)"
        else
            say "  DRYRUN: would patch config.txt (disable_splash=1) +"
            say "          cmdline.txt (quiet splash plymouth.ignore-serial-consoles)"
        fi
    fi
else
    say "  boot-config-lib.sh absent at ${BOOT_LIB}; skip boot-config patch"
fi

# 5. Install the plymouth-quit --retain-splash handoff drop-in. This
#    keeps the splash framebuffer on screen until the renderer paints
#    its first frame (no black flash). The drop-in lands in a
#    plymouth-quit.service.d/ dir; section 8's daemon-reload picks it
#    up. The copy overwrites identically on re-run (idempotent).
HANDOFF_SRC="${SPLASH_STAGE}/03-plymouth-handoff/files/plymouth-quit.service.d/retain-splash.conf"
HANDOFF_DST_DIR="${SYSTEMD_DIR}/plymouth-quit.service.d"
HANDOFF_DST="${HANDOFF_DST_DIR}/retain-splash.conf"
say "Install plymouth-quit --retain-splash handoff drop-in"
if [ "$DRY_RUN" -eq 1 ] || [ -f "$HANDOFF_SRC" ]; then
    run install -d -m 755 "$HANDOFF_DST_DIR"
    run install -m 644 "$HANDOFF_SRC" "$HANDOFF_DST"
else
    say "  handoff drop-in source ${HANDOFF_SRC} absent; skip"
fi

# 6. No separate `systemctl daemon-reload` needed here: section 8 below
#    runs `systemctl daemon-reload` and that picks up the drop-in just
#    installed (this section is slotted before section 8 precisely so
#    the drop-in is in place when that reload runs).

# --- 8. systemctl reload + enable -------------------------------------------

say "Reload systemd + enable units"
run systemctl daemon-reload
# Pi OS Lite trixie stock image doesn't ship hostapd or iptables at
# all; we install them via vendored .debs at step 5.5 above. After
# dpkg-install, hostapd.service may land in a masked state (Debian's
# default for the dual-mode wifi-stack packages); unmask before enable
# so openmarquee-ap0.service's `Before=hostapd.service` ordering
# actually pulls hostapd up at boot. dnsmasq.service ships unmasked-
# but-disabled; enabling makes the captive-portal DHCP+DNS reach the
# boot critical path. Both unmasks are idempotent (no-op on already-
# unmasked units). `|| true` is defense-in-depth: if a future Debian
# image leaves these unmasked, the no-op unmask shouldn't fail-stop
# the install under set -e.
run systemctl unmask hostapd.service dnsmasq.service || true
run systemctl enable openmarquee-backend.service \
                    openmarquee-ap0.service \
                    hostapd.service \
                    dnsmasq.service

# Phase 4u: explicit start sequence. `systemctl enable` alone does NOT
# activate units when multi-user.target is already reached during
# cloud-init -- the units are wired into next-boot's dependency graph,
# but they don't get started this boot. Section 5.5's mask above stops
# FUTURE invocations of hostapd / dnsmasq from the .deb postinst, but
# any failed-state already queued before mask landed survives the
# mask; reset-failed clears it. Then bring up ap0 explicitly (needed
# for hostapd to find an interface to bind), then restart hostapd +
# dnsmasq so they pick up the now-templated configs and the now-extant
# ap0. `ip link set wlan0 up` is belt-and-braces (ap0-setup.sh handles
# its own interface up, but a wlan0-down state would mean ap0 can't
# create either).
run systemctl reset-failed hostapd.service dnsmasq.service || true
run ip link set wlan0 up || true
run systemctl start openmarquee-ap0.service
run systemctl restart hostapd.service
run systemctl restart dnsmasq.service

# Restart the backend so the new code takes effect on developer redeploy.
# On first boot the unit isn't running; --no-block queues the restart and
# returns immediately, letting systemd resolve the dependency chain
# (network-online -> ap0 -> hostapd -> dnsmasq -> backend) in the
# background. Without --no-block on first boot we'd block here for up to
# TimeoutStartSec waiting for hostapd, and any transient failure under
# `set -e` would abort install.sh before the "Done." line. `|| true`
# belts-and-braces against systemctl returning non-zero on edge cases
# (unit masked, machine ID issues during cloud-init, etc.).
run systemctl --no-block restart openmarquee-backend.service || true

# Probe backend health so the operator gets an obvious signal if uvicorn
# died on startup (import error, port-bind conflict, missing settings,
# etc.) — without this, install.sh would exit 0 even when the AP comes
# up but http://10.0.0.1/ hangs. /healthz is documented as the deploy
# health gate (auth_middleware.py:52) and is no-auth + side-effect-free.
# Loop is 30 × (curl --max-time 1 + sleep 1): ~30s when port 80 is
# unbound (curl returns immediately), up to ~60s if uvicorn is bound
# but hung. Failure does NOT fail-stop install.sh — the backend may
# legitimately come up later when cloud-init finishes pulling in
# deferred services. Failure leaves a sentinel file so next-boot
# diagnosis has an obvious anchor.
if [ "$DRY_RUN" -eq 0 ]; then
    say "Probing backend health (~30s budget; up to ~60s if uvicorn hangs)"
    backend_up=0
    for _ in $(seq 1 30); do
        if curl -fsS --max-time 1 http://127.0.0.1/healthz >/dev/null 2>&1; then
            backend_up=1
            break
        fi
        sleep 1
    done
    if [ "$backend_up" -eq 1 ]; then
        say "  backend /healthz responded OK"
        rm -f "${ROOT_PREFIX}/var/openmarquee-backend-startup-failed"
    else
        say "WARNING: backend did not respond to /healthz within 30s"
        touch "${ROOT_PREFIX}/var/openmarquee-backend-startup-failed"
    fi
fi

# --- 8a. End-of-install snapshot + kernel/prev-boot capture -----------------
#
# Final checkpoint: append the END_OF_INSTALL snapshot to the debug log
# alongside BEFORE_DEBS_INSTALL / AFTER_DEBS_INSTALL / AFTER_FIRSTBOOT.
# Also dump dmesg + the previous-boot journal so a Restart=on-failure
# cycle leaves a complete forensic trail. Pure read-only.

snapshot_state "END_OF_INSTALL"

if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    # dmesg: kernel ring buffer at end of install. Catches hostapd /
    # iptables / NM kernel-side errors that don't surface to journald.
    dmesg > /var/log/openmarquee-debug-dmesg.log 2>&1 || true
    chmod 600 /var/log/openmarquee-debug-dmesg.log 2>/dev/null || true
    # Previous-boot journal: empty on the very first boot, populated on
    # any Restart=on-failure-cycled later boot. Catches the scenario
    # where firstboot.service failed, was retried, qarl yanked the SD
    # mid-retry, and the next boot's journal has the prior-boot failure.
    journalctl --boot=-1 --no-pager > /var/log/openmarquee-debug-prevboot.log 2>&1 || true
    chmod 600 /var/log/openmarquee-debug-prevboot.log 2>/dev/null || true
    # systemd-analyze blame: which units took longest to start. Useful
    # for diagnosing Restart=on-failure loops + startup ordering issues
    # (a unit hitting StartLimitBurst will show as failed-fast here).
    systemd-analyze blame --no-pager > /var/log/openmarquee-debug-systemd-blame.log 2>&1 || true
    chmod 600 /var/log/openmarquee-debug-systemd-blame.log 2>/dev/null || true
    # critical-chain: dependency graph of why each unit started in the
    # order it did. Catches After=/Before= mis-orderings between ap0 /
    # hostapd / NetworkManager.
    systemd-analyze critical-chain --no-pager > /var/log/openmarquee-debug-systemd-critical.log 2>&1 || true
    chmod 600 /var/log/openmarquee-debug-systemd-critical.log 2>/dev/null || true
    # Full system journal for the current boot — catches anything not
    # captured by the unit-specific tails in snapshot_state (kernel
    # subsystem messages, udev events, dbus chatter that surfaces
    # service-startup interactions).
    journalctl --boot=0 --no-pager > /var/log/openmarquee-debug-fullboot.log 2>&1 || true
    chmod 600 /var/log/openmarquee-debug-fullboot.log 2>/dev/null || true
elif [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: would capture dmesg + previous-boot journal + systemd-analyze blame/critical + full-boot journal under /var/log/openmarquee-debug-*.log"
fi

say "Done."
