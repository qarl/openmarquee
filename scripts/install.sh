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
#     sudo bash /opt/openmarquee/scripts/install.sh --system-only
#                                                                # system/ files only
#
# --system-only (deploy-hygiene 2026-07-16): install ONLY the system-config
# sections (systemd units, netctl helpers, hostapd/dnsmasq/NM/udev/sudoers/
# tmpfiles/avahi/boot-splash/cron + daemon-reload + enable) and SKIP the §2
# Python venv + pip install (the backend app layer). This is the primitive
# scripts/sync-system-to-sign.sh uses so the routine low-impact deploy path
# can refresh system/ files without the heavy venv work — closing the
# binary-only-deploy drift documented in docs/deploy-hygiene-audit-2026-07-16.md.
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
SYSTEM_ONLY=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)      DRY_RUN=1; shift ;;
        --root)         ROOT_PREFIX="$2"; shift 2 ;;
        --system-only)  SYSTEM_ONLY=1; shift ;;
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
# r34 (2026-05-31): predictable name for the USB-WiFi mgmt dongle.
# Rule pins rt2800usb-family dongles to NAME=wlan-dongle so the
# burn-time mgmt-WiFi NM keyfile can match on interface-name=
# reliably. See system/99-openmarquee-usb-wlan.rules for scope +
# qa/r31-dongle-topology-recommendation-2026-05-31.md §B.1 for rationale.
UDEV_USB_WLAN_DST="${ROOT_PREFIX}/etc/udev/rules.d/99-openmarquee-usb-wlan.rules"
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
        printf '\n=== install.sh START: %s ===\n' "$(date -u -Iseconds 2>/dev/null || date -u)"
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
        printf '=== snapshot: %s at %s ===\n' "$tag" "$(date -u -Iseconds 2>/dev/null || date -u)"
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

# r29 (2026-05-31) -- SECTION REORDER:
#
# Sections 3 (systemd units), 3a (chmod helpers), and 3b (Rust IPC
# sidecar binary) deliberately run BEFORE section 2 (Python venv +
# pip install) so a transient pip failure doesn't take down the
# deploy pipeline. Before r29, install.sh ran §2 → §3 → §3a → §3b
# → ... → §8 (end-of-script daemon-reload + restart). r28's FYS
# v1.0.0 deploy (2026-05-31) hit a pip resolver failure at §2; the
# EXIT trap fired; §3b's renderer binary cp + §8's backend restart
# never ran. Manual `scp + stop/cp/start` was the recovery — r29
# eliminates the need for that bypass.
#
# Section numbers (3, 3a, 3b) are preserved as historical IDs so
# downstream "the systemd units staged in §3" references stay
# accurate; only their position in the file moves.
#
# Key invariant per the r29 dispatch contract: NO systemctl start/
# restart fires between sections 3 and 7. The end-of-script
# daemon-reload + restart at section 8 (line ~939) is the single
# atomic transition. Section 3b uses atomic rename(2) for the
# renderer binary so it never needs to bounce the backend
# mid-install -- see the §3b body below for the mechanism.

# --- 3. Systemd unit files --------------------------------------------------

say "Install systemd units"
run mkdir -p "$SYSTEMD_DIR"
for unit in openmarquee-backend.service openmarquee-ap0.service openmarquee-tailscale.service \
            openmarquee-mini.service \
            openmarquee-cma-watchdog.service openmarquee-cma-watchdog.timer \
            openmarquee-best-wifi.service openmarquee-best-wifi.timer \
            openmarquee-wifi-powersave-off.service \
            openmarquee-boot-gesture.service \
            openmarquee-boot-gesture-clear.service openmarquee-boot-gesture-clear.timer \
            openmarquee-backend-failure-handler.service \
            openmarquee-stability-promoter.service; do
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
for sh_helper in openmarquee-ap0-setup.sh openmarquee-firstboot.sh openmarquee-tailscale.sh \
                 openmarquee-cma-watchdog.sh openmarquee-best-wifi.sh \
                 openmarquee-wifi-powersave-off.sh \
                 openmarquee-boot-gesture.sh \
                 stability-promoter.sh; do
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
#
# r29 (2026-05-31) -- atomic-rename install pattern.
#
# The running sidecar holds $RUST_BIN_INSTALLED open via exec mmap, so
# `cp $SRC $DST` (in-place write) fails with ETXTBSY ("Text file busy").
# Three historical workarounds, in order of increasing quality:
#
#   1. (pre-r21) Plain cp + swallow the failure → silent stale binary
#      across redeploys. Cost: manual stop/cp/start workaround burned
#      twice during perf-night r5+r6 (2026-05-26+28).
#   2. (r21 2026-05-30) Stop backend, cp, then rely on §8's restart.
#      Works for the happy path, but couples binary install to a
#      backend bounce AND if a LATER section (e.g. pip) trips the EXIT
#      trap before §8 fires, the box is left with the backend stopped.
#      Cost: bit FYS-prod r28 v1.0.0 deploy 2026-05-31.
#   3. (r29 -- this commit) cp to sibling `.new` path, then `mv -f`
#      atomic rename. rename(2) on the same filesystem operates on
#      the directory entry, NOT the file content -- the running
#      backend's open fd stays bound to the OLD inode (kernel
#      preserves the inode until the last fd closes, even though
#      the dirent now points to the NEW inode). No ETXTBSY, no
#      backend bounce, no partial-rollback risk if a later section
#      trips. §8's end-of-script restart picks up the new binary
#      on the happy path; if pip fails, the backend keeps running
#      with the old binary (graceful) and the operator can re-run
#      install.sh after fixing pip to get the new binary live.
RUST_BIN_STAGED="${OPT_DIR}/bin/openmarquee-render"
RUST_BIN_INSTALLED="${ROOT_PREFIX}/usr/local/bin/openmarquee-render"
RUST_BIN_TMP="${RUST_BIN_INSTALLED}.new"
say "Install Rust IPC sidecar binary (opt-in via OPENMARQUEE_RENDERER=rust-sidecar)"
if [ "$DRY_RUN" -eq 1 ] || [ -f "$RUST_BIN_STAGED" ]; then
    # Staging/live REVERT GUARD (deploy-hygiene 2026-07-16). The binary-only
    # sign-deploy path (qa/deploy-to-sign.sh) writes the LIVE binary
    # (${RUST_BIN_INSTALLED}) directly and does NOT refresh this staging copy
    # (${RUST_BIN_STAGED}). Blindly promoting staging→live here would REVERT a
    # freshly hand-deployed binary to the older staged one — the "different md5
    # in each path" hazard from docs/deploy-hygiene-audit-2026-07-16.md §4.
    # Promote ONLY when staging is newer than the installed binary (or the
    # installed binary is absent). Mirrors the §3 unit-loop's `-nt` freshness
    # check. --dry-run always proceeds so it still prints the intended actions.
    # Caveat (same as §3): `-nt` compares mtimes, and staging carries the
    # build-HOST mtime (rsync -a preserves it) while live carries the Pi-side
    # promote time. A build host whose clock lags the sign by more than the
    # build interval could make a genuinely-new staged binary look older and be
    # skipped (a `say` is logged; deploy.sh does not error). Keep dev host + sign
    # NTP-synced. The proper belt is deploy-to-sign.sh refreshing staging on a
    # hand-deploy; this guard is the durable suspenders.
    if [ "$DRY_RUN" -eq 0 ] && [ -f "$RUST_BIN_INSTALLED" ] && [ ! "$RUST_BIN_STAGED" -nt "$RUST_BIN_INSTALLED" ]; then
        say "  staged binary ($RUST_BIN_STAGED) is NOT newer than installed"
        say "  ($RUST_BIN_INSTALLED); skip promote (revert guard)."
        say "  If unexpected, the /opt staging copy is stale vs a directly-deployed"
        say "  live binary — run a full deploy.sh (re-cross-build first) to re-stage."
    else
        run mkdir -p "$(dirname "$RUST_BIN_INSTALLED")"
        # cp to sibling temp path first (no ETXTBSY -- nothing holds the
        # .new path open). Defensive cleanup of any leftover .new from a
        # prior failed install run -- `cp` would overwrite it anyway, but
        # explicit rm makes the intent obvious.
        run rm -f "$RUST_BIN_TMP" || true
        if ! run cp "$RUST_BIN_STAGED" "$RUST_BIN_TMP"; then
            say "ERROR: cp $RUST_BIN_STAGED -> $RUST_BIN_TMP failed."
            say "  (likely disk-full or permission issue; investigate with:"
            say "   df -h $(dirname "$RUST_BIN_INSTALLED"); ls -ld $(dirname "$RUST_BIN_INSTALLED"))"
            run rm -f "$RUST_BIN_TMP" || true
            exit 1
        fi
        run chmod +x "$RUST_BIN_TMP"
        # Atomic swap. rename(2) on a same-filesystem dest doesn't touch
        # the destination's content -- it just re-points the directory
        # entry. The running backend's exec-mmap stays bound to the old
        # inode; the old inode is reaped when the backend exits OR is
        # restarted at §8. No ETXTBSY possible here.
        if ! run mv -f "$RUST_BIN_TMP" "$RUST_BIN_INSTALLED"; then
            say "ERROR: mv $RUST_BIN_TMP -> $RUST_BIN_INSTALLED failed."
            say "  (cross-filesystem rename? both paths must be on the same fs;"
            say "   check with: df -h $RUST_BIN_TMP $(dirname "$RUST_BIN_INSTALLED"))"
            run rm -f "$RUST_BIN_TMP" || true
            exit 1
        fi
    fi
else
    say "  no staged binary at ${RUST_BIN_STAGED}; skip (sidecar opt-in unused)"
fi

# --- 3c. CMA-pressure daily-restart cron (r38c stopgap) ---------------------
#
# Belt-and-braces companion to openmarquee-cma-watchdog.timer (the 60s
# threshold-driven watchdog). The cron fires unconditionally at 03:00
# local time -- catches slow CMA drift that stays below the watchdog
# threshold but would still wedge the renderer over multi-day uptime.
#
# Lives at /etc/cron.d/openmarquee-daily-restart. crond on Debian
# trixie scans /etc/cron.d for owner-root, mode <= 0644 drop-ins;
# we set 0644 explicitly. crond picks up the new drop-in on the
# next cron-daemon-scan tick (no daemon-reload needed).
#
# Bridges until r38b's actual leak fix lands. See
# qa/r38c-cma-pressure-watchdog-2026-06-02.md.
CRON_SRC="${OPT_DIR}/system/openmarquee-daily-restart.cron"
CRON_DST="${ROOT_PREFIX}/etc/cron.d/openmarquee-daily-restart"
say "Stage openmarquee-daily-restart cron drop-in"
if [ "$DRY_RUN" -eq 1 ] || [ -f "$CRON_SRC" ]; then
    run mkdir -p "$(dirname "$CRON_DST")"
    run cp "$CRON_SRC" "$CRON_DST"
    run chmod 0644 "$CRON_DST"
    run chown root:root "$CRON_DST"
else
    say "  no cron source at ${CRON_SRC}; skip (r38c not yet shipped on this branch)"
fi

# --- 3d. Ensure cron daemon is enabled (defense for §3c) -------------------
#
# The §3c cron drop-in only fires if cron.service is enabled + running.
# Belt-and-braces guard for the r38c daily-restart safety net. See
# qa/cron-enable-guard-followup-2026-06-02.md.
#
# Three states handled:
#   enabled / static / etc -> no-op
#   disabled               -> systemctl enable cron.service
#   masked / not-found     -> loud warn, no action (operator-deliberate)
#
# Cron package not installed at all (`cron` on Debian / `crond` on
# RHEL-family) -> loud warn + continue. Watchdog still functional;
# only the daily safety net is lost.
say "Ensure cron daemon enabled (for §3c daily-restart drop-in)"
if command -v cron >/dev/null 2>&1 || command -v crond >/dev/null 2>&1; then
    cron_state="$(systemctl is-enabled cron.service 2>&1 || echo unknown)"
    case "$cron_state" in
        enabled|enabled-runtime|static|indirect|alias|linked|linked-runtime|generated)
            say "  cron.service is ${cron_state}; no action needed"
            ;;
        disabled)
            say "  cron.service is disabled; enabling for /etc/cron.d/openmarquee-daily-restart"
            run systemctl enable cron.service
            ;;
        masked|masked-runtime)
            say "  WARN: cron.service is ${cron_state} (operator-deliberate?)."
            say "       /etc/cron.d/openmarquee-daily-restart will not fire."
            say "       Watchdog (openmarquee-cma-watchdog.timer) still functional."
            ;;
        *)
            # Catches: transient, bad, unknown (our fallback), or any
            # future systemd state we don't know about.
            say "  WARN: cron.service in unexpected state '${cron_state}'."
            say "       /etc/cron.d/openmarquee-daily-restart fire status unverified."
            ;;
    esac
else
    say "  WARN: cron daemon not installed (neither 'cron' nor 'crond' on PATH)."
    say "       /etc/cron.d/openmarquee-daily-restart will not fire."
    say "       Watchdog (openmarquee-cma-watchdog.timer) still functional."
    say "       To wire up: apt install cron && re-run install.sh"
fi

# --- 3e. WiFi AP-deauth-recovery watchdog (cron.d + script PAIR) -------------
#
# deploy-hygiene 2026-07-16 (P2-d). QA's 2026-07-16 08:13 JasonsSign1 probe
# confirmed the AP-deauth-recovery watchdog (30s cadence, THRESHOLD=2, dates to
# the 2026-05-23 wedge) is PRESENT + actively firing on the fielded sign — but
# NOTHING in the repo installed it: it was hand-placed once during the wedge
# remediation. pi-gen doesn't stage it and no install.sh path promoted it, so a
# fresh SD burn (JasonsSign2) would SILENTLY lack it. This makes install.sh the
# durable install path — same reproducibility gap the mini-play.sh install below
# closed for the boot-default renderer, and the live one of the two orphaned
# system/ files flagged in docs/deploy-hygiene-audit-2026-07-16.md §5.
#
# COUPLED PAIR (like the netctl helper+daemon): the cron.d drop-in fires
# `/usr/local/bin/wifi-watchdog.sh` via flock, so the two must install together
# and stay in lockstep — a version-skewed pair (new cadence + old script, or
# vice versa) must not be possible. Guard installs BOTH-or-NEITHER. Installed
# UNCONDITIONALLY (not gated by --system-only): OS-level config, so a full
# deploy.sh AND a targeted `install.sh --system-only` (via sync-system-to-sign.sh)
# both land it. cron.service is already enabled by §3d above; cron.d requires
# owner-root mode <=0644 (same as the §3c daily-restart drop-in).
WIFI_WATCHDOG_SCRIPT_SRC="${OPT_DIR}/scripts/wifi-watchdog.sh"
WIFI_WATCHDOG_SCRIPT_DST="${ROOT_PREFIX}/usr/local/bin/wifi-watchdog.sh"
WIFI_WATCHDOG_CRON_SRC="${OPT_DIR}/system/openmarquee-wifi-watchdog"
WIFI_WATCHDOG_CRON_DST="${ROOT_PREFIX}/etc/cron.d/openmarquee-wifi-watchdog"
say "Install WiFi AP-deauth-recovery watchdog (cron.d + script pair)"
if [ "$DRY_RUN" -eq 1 ] || { [ -f "$WIFI_WATCHDOG_SCRIPT_SRC" ] && [ -f "$WIFI_WATCHDOG_CRON_SRC" ]; }; then
    # Script first, then the cron.d that invokes it — so the target exists
    # before crond can fire the drop-in on its next scan.
    run mkdir -p "$(dirname "$WIFI_WATCHDOG_SCRIPT_DST")"
    run install -m 0755 "$WIFI_WATCHDOG_SCRIPT_SRC" "$WIFI_WATCHDOG_SCRIPT_DST"
    run mkdir -p "$(dirname "$WIFI_WATCHDOG_CRON_DST")"
    run install -m 0644 -o root -g root "$WIFI_WATCHDOG_CRON_SRC" "$WIFI_WATCHDOG_CRON_DST"
else
    # Coupled: if EITHER source is missing, install NEITHER (never leave a
    # cron.d drop-in pointing at an absent script, or a script with no trigger).
    say "  wifi-watchdog pair source(s) missing; skip both (coupled install)"
    say "    (script: ${WIFI_WATCHDOG_SCRIPT_SRC}; cron: ${WIFI_WATCHDOG_CRON_SRC})"
fi

# --- 2. Python venv ---------------------------------------------------------
#
# r29 (2026-05-31): runs AFTER sections 3, 3a, 3b — see the meta-comment
# above section 3 for the rationale.
#
# --system-only (deploy-hygiene 2026-07-16): this whole section — the ONLY
# app-layer (backend Python) work in install.sh — is skipped for a targeted
# system/-file resync. Everything else in this script is OS-level config and
# still runs. See the --system-only note in the header + sync-system-to-sign.sh.
if [ "$SYSTEM_ONLY" -eq 1 ]; then
    say "Skip §2 Python venv + pip install (--system-only: system files only)"
else

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
fi  # end §2 (skipped under --system-only)

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

# 2026-05-30 QA: re-run ap0 setup before dnsmasq starts so ap0's IP
# self-heals if it was flushed after the original boot-time setup
# (NetworkManager hiccup, brcmfmac wedge recovery, manual restart
# churn during perf rounds). Without this, dnsmasq fails with
# "unknown interface ap0" and stays in failed-state until the
# operator either reboots or manually re-runs openmarquee-ap0-setup.sh.
# `openmarquee-ap0.service` is `Type=oneshot RemainAfterExit=yes` so
# it doesn't re-execute on dnsmasq restart -- this drop-in's
# ExecStartPre is what closes that gap. Section 8's daemon-reload
# below picks up this drop-in; no separate reload needed here.
say "Stage dnsmasq ap0-heal drop-in"
DNSMASQ_DROPIN_SRC="${OPT_DIR}/system/dnsmasq.service.d/openmarquee-ap0-heal.conf"
DNSMASQ_DROPIN_DST_DIR="${SYSTEMD_DIR}/dnsmasq.service.d"
DNSMASQ_DROPIN_DST="${DNSMASQ_DROPIN_DST_DIR}/openmarquee-ap0-heal.conf"
if [ "$DRY_RUN" -eq 1 ] || [ -f "$DNSMASQ_DROPIN_SRC" ]; then
    run install -d -m 0755 "$DNSMASQ_DROPIN_DST_DIR"
    run install -m 0644 "$DNSMASQ_DROPIN_SRC" "$DNSMASQ_DROPIN_DST"
fi

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

# --- 5b. udev rule for predictable USB-WiFi dongle naming -------------------
#
# r34 (2026-05-31): pin rt2800usb-driver USB-WiFi dongles to
# NAME=wlan-dongle so the dual-radio mgmt-WiFi NM keyfile (dropped
# at burn time via burn_sd_card.sh --mgmt-wifi-ssid) can match
# on interface-name=wlan-dongle reliably. Without the rule, USB
# enumeration order decides the kernel name (wlan1 / wlan2 / wlan3)
# and the keyfile's interface-name= pin won't match. Section B.1
# of qa/r31-dongle-topology-recommendation-2026-05-31.md has the
# full design + chipset-matrix discussion.
#
# Timing: cloud-init runcmd (images/openmarquee/cloud-init/user-data:80)
# runs THIS install.sh AFTER NetworkManager is already up on first
# boot. So on a first flash with a dongle pre-attached, NM sees the
# dongle as wlan1 BEFORE the rule lands. The udevadm trigger below
# renames the existing interface at runtime; netlink notifies NM of
# the rename + NM picks up the mgmt-WiFi keyfile (which matches
# interface-name=wlan-dongle on the renamed iface).
#
# On SUBSEQUENT boots (and on redeploys), the rule is already in
# /etc/udev/rules.d/ at kernel init -- udev applies the name before
# NM even starts, and the renaming-after-NM race doesn't apply.

say "Stage USB-WiFi-dongle udev rule"
run mkdir -p "$(dirname "$UDEV_USB_WLAN_DST")"
run cp "${OPT_DIR}/system/99-openmarquee-usb-wlan.rules" "$UDEV_USB_WLAN_DST"

# Reload udev's compiled ruleset + trigger a re-eval on already-
# attached net devices. Idempotent: udev's per-device applied-rules
# cache makes a re-run a no-op when the rule body hasn't changed.
# Skip in dry-run mode AND when staging into a ROOT_PREFIX (tests).
#
# CAVEAT (subagent review 2026-05-31): the kernel rejects NAME=
# udev rename directives on an interface that is IFF_UP (EBUSY).
# By the time cloud-init runcmd reaches install.sh, NM is already
# up and has brought any wifi netdev to UP for scanning. So the
# trigger below WILL NOT rename a dongle that was attached at
# kernel-init time — on the first-flash-WITH-dongle-pre-attached
# path, the rename converges only after a reboot (when the rule
# is in /etc/udev/rules.d/ before kernel enumeration). The
# hot-plug path (dongle inserted after install.sh ran) works
# correctly via the udev `add` event, which fires BEFORE NM
# brings the new iface up.
#
# Mitigations considered + deferred:
#   - `ip link set wlanX down; ip link set wlanX name wlan-dongle;
#     ip link set wlan-dongle up` is the single-boot bring-up
#     surface; needs reliable dongle detection (parse `udevadm
#     info /sys/class/net/wlan*` for DRIVERS=rt2800usb). Adds
#     ~30 LOC + a small race window if NM tries to reassociate
#     during the down→rename→up. Punt to a future dispatch.
#   - Forcing a reboot at install.sh end fixes it but is
#     heavy-handed and breaks redeploy idempotency.
#
# docs/dual-radio-shipping-test.md step 3 explicitly verifies the
# reboot-with-dongle path; system/README.md hot-plug section
# notes the first-flash-with-dongle reboot requirement.
if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=net --action=add 2>/dev/null || true
fi

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

# P1.2-B.2 (2026-06-10): privileged network-control daemon. The
# openMarquee backend runs under NoNewPrivileges=true +
# ProtectSystem=strict (system/openmarquee-backend.service); sudo
# from inside that sandbox is BLOCKED by NNP. The privilege
# transition now happens via a socket-activated systemd template
# service: openmarquee-netctl.socket listens at
# /run/openmarquee/netctl.sock with SocketGroup=openmarquee so the
# backend can connect; openmarquee-netctl@.service spawns
# openmarquee-netctl-daemon as root per connection, with
# STDIN/STDOUT bound to the socket. The bash helper at
# /usr/local/sbin/openmarquee-netctl is the actual root code that
# does the file writes + systemctl calls; the daemon is a thin
# IPC adapter.
say "Stage openmarquee-netctl privilege-boundary helper"
NETCTL_DST="${ROOT_PREFIX}/usr/local/sbin/openmarquee-netctl"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-netctl" ]; then
    run mkdir -p "$(dirname "$NETCTL_DST")"
    run install -m 0755 -o root -g root "${OPT_DIR}/system/openmarquee-netctl" "$NETCTL_DST"
fi
say "Stage openmarquee-netctl-daemon (socket-activated root daemon)"
NETCTL_DAEMON_DST="${ROOT_PREFIX}/usr/local/sbin/openmarquee-netctl-daemon"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-netctl-daemon" ]; then
    run mkdir -p "$(dirname "$NETCTL_DAEMON_DST")"
    run install -m 0755 -o root -g root "${OPT_DIR}/system/openmarquee-netctl-daemon" "$NETCTL_DAEMON_DST"
fi
say "Stage openmarquee-netctl systemd socket + template unit"
NETCTL_SOCKET_DST="${SYSTEMD_DIR}/openmarquee-netctl.socket"
NETCTL_TEMPLATE_DST="${SYSTEMD_DIR}/openmarquee-netctl@.service"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-netctl.socket" ]; then
    run install -m 0644 "${OPT_DIR}/system/openmarquee-netctl.socket" "$NETCTL_SOCKET_DST"
fi
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-netctl@.service" ]; then
    run install -m 0644 "${OPT_DIR}/system/openmarquee-netctl@.service" "$NETCTL_TEMPLATE_DST"
fi

# P1.2-B.3 (2026-06-10): create /run/openmarquee/ as root:openmarquee
# 0750 via systemd-tmpfiles. The socket unit's DirectoryMode=0750
# alone created the dir root:root which the backend (openmarquee
# user) couldn't TRAVERSE — QA's verify caught this. tmpfiles
# definitions live in /etc/tmpfiles.d/ and are applied on boot via
# systemd-tmpfiles-setup.service; we also run --create at install
# time so the socket can bind without a reboot.
say "Stage openmarquee tmpfiles (/run/openmarquee/ perms)"
TMPFILES_DST="${ROOT_PREFIX}/etc/tmpfiles.d/openmarquee.conf"
if [ "$DRY_RUN" -eq 1 ] || [ -f "${OPT_DIR}/system/openmarquee-tmpfiles.conf" ]; then
    run mkdir -p "$(dirname "$TMPFILES_DST")"
    run install -m 0644 "${OPT_DIR}/system/openmarquee-tmpfiles.conf" "$TMPFILES_DST"
    # Apply now so the next `systemctl start openmarquee-netctl.socket`
    # finds /run/openmarquee/ with correct ownership without a reboot.
    if [ "$DRY_RUN" -eq 0 ]; then
        run systemd-tmpfiles --create "$TMPFILES_DST" || true
    fi
fi

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
    # Handover reconcile 2026-07-09 (GAP8): cap the persistent journal
    # so it can't grow unbounded and fill the SD card (which compounds
    # the swap pressure GAP7 mitigates). SystemMaxUse bounds the
    # on-disk journal at /var/log/journal; RuntimeMaxUse bounds the
    # tmpfs journal before persistence takes over. Folds the live
    # sign's 99-openmarquee-cap.conf values into this same drop-in.
    cat > "$JOURNALD_DROPIN" <<'JOURNALD_EOF'
[Journal]
Storage=persistent
SystemMaxUse=300M
RuntimeMaxUse=64M
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
    say "DRYRUN: would enable persistent journal at /var/log/journal (cap 300M/64M)"
fi

# --- 7c-2. Kernel + watchdog tuning drop-ins --------------------------------
#
# Handover reconcile 2026-07-09: fold two deploy-only tuning files into
# BOTH the pi-gen image and the redeploy path (DST carries $ROOT_PREFIX
# so the offline build bakes them too); only the live sysctl reload is
# gated to a real device.
#   GAP7  vm.swappiness=10                 -> /etc/sysctl.d/
#         (SD-swap-thrash mitigation for the text reel)
#   GAP9  cma-watchdog THRESHOLD/COOLDOWN  -> /etc/default/
#         (reconciled for the 320M pool: THRESHOLD_MB=300)

SWAPPINESS_DST="${ROOT_PREFIX}/etc/sysctl.d/99-openmarquee-swappiness.conf"
say "Install vm.swappiness=10 drop-in (GAP7)"
run mkdir -p "$(dirname "$SWAPPINESS_DST")"
run install -m 0644 "${OPT_DIR}/system/99-openmarquee-swappiness.conf" "$SWAPPINESS_DST"

CMA_WATCHDOG_DEFAULT_DST="${ROOT_PREFIX}/etc/default/openmarquee-cma-watchdog"
say "Install openmarquee-cma-watchdog default (GAP9: THRESHOLD_MB=300 for 320M pool)"
run mkdir -p "$(dirname "$CMA_WATCHDOG_DEFAULT_DST")"
run install -m 0644 "${OPT_DIR}/system/openmarquee-cma-watchdog.default" "$CMA_WATCHDOG_DEFAULT_DST"

# Apply swappiness now on a real device (the build host has no running
# kernel to sysctl into; the drop-in still applies on the target's next
# boot regardless). cma-watchdog.default needs no reload — the watchdog
# sources /etc/default on each timer fire.
if [ -z "$ROOT_PREFIX" ] && [ "$DRY_RUN" -eq 0 ]; then
    say "Apply sysctl (vm.swappiness=10)"
    sysctl --system >/dev/null 2>&1 \
        || say "  WARN: sysctl --system failed; vm.swappiness applies on next boot"
elif [ "$DRY_RUN" -eq 1 ]; then
    say "DRYRUN: would sysctl --system to apply vm.swappiness=10"
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
    # Clear any deferred marker — plymouth IS present now, splash
    # will be applied on this run.
    if [ -z "$ROOT_PREFIX" ] && [ -f /var/openmarquee-splash-deferred ]; then
        rm -f /var/openmarquee-splash-deferred
        say "  Cleared /var/openmarquee-splash-deferred from prior offline run."
    fi
else
    say "  plymouth not present; attempting apt-get install"
    if apt-get install -y plymouth; then
        PLYMOUTH_PRESENT=1
        say "  plymouth installed"
        # Clear the deferred marker (if any) — a previous offline
        # install dropped it; the splash IS being applied now.
        if [ -z "$ROOT_PREFIX" ] && [ -f /var/openmarquee-splash-deferred ]; then
            rm -f /var/openmarquee-splash-deferred
            say "  Cleared /var/openmarquee-splash-deferred from prior offline run."
        fi
    else
        # JasonsSign1 follow-up (2026-06-11): first-boot install via
        # cloud-init runcmd has NO network on a freshly-flashed Pi OS
        # Lite — plymouth isn't in the base image and apt fails. The
        # install otherwise succeeds (factory-fresh promise per
        # phase-4e), but the openMarquee splash silently doesn't
        # appear. qarl noticed immediately on JasonsSign1: "this
        # build doesn't have the openMarquee boot image."
        #
        # Drop a marker file + write a clear journal message so the
        # operator can recover via a one-shot online step:
        #   sudo apt install plymouth && sudo bash /opt/openmarquee/scripts/install.sh
        say "  WARNING: could not install plymouth (offline / apt failure)"
        say "           Boot splash skipped — DEVICE STILL WORKS without it."
        say ""
        say "           Limitation: Pi OS Lite arm64 does NOT include plymouth"
        say "           in the base image, so the stage_sd_card.sh flow can't"
        say "           install the splash on a first-boot-offline device."
        say "           Recovery (needs network on the Pi):"
        say "               sudo apt install plymouth"
        say "               sudo bash /opt/openmarquee/scripts/install.sh"
        say "           After this, the openMarquee splash will appear on"
        say "           the NEXT boot."
        say ""
        if [ -z "$ROOT_PREFIX" ]; then
            run touch /var/openmarquee-splash-deferred
            say "  Marker dropped at /var/openmarquee-splash-deferred so the"
            say "  operator (and future install.sh runs) can detect the deferred state."
        fi
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
            # Handover reconcile 2026-07-09 (GAP2): apply the memory
            # split on the DEPLOY path too. The pi-gen 02-run.sh burn
            # path already invokes both (02-run.sh:53-54), but a
            # redeploy over a live sign was leaving cma/gpu_mem to
            # whatever the card was flashed with. Both are idempotent
            # (no-op when gpu_mem=128 / cma=320M already present, which
            # the live sign has) and guarded so non-zero can't trip
            # set -e and abort a production redeploy. Takes effect on
            # next reboot (kernel cmdline / firmware split).
            patch_config_txt_gpu_mem "${boot_dir}/config.txt" \
                || say "  WARNING: config.txt gpu_mem patch skipped (see above)"
            patch_cmdline_txt_cma "${boot_dir}/cmdline.txt" \
                || say "  WARNING: cmdline.txt cma patch skipped (see above)"
            # Postmortem mitigation #5 (2026-05-23): strip the base
            # Pi OS `cgroup_disable=memory` flag so PSI/cgroup
            # memory accounting + systemd-OOMD become available.
            strip_cmdline_token "cgroup_disable=memory" "${boot_dir}/cmdline.txt" \
                || say "  WARNING: cmdline.txt strip skipped (see above)"
            # HDMI audio 2026-07-01 (qarl decision, locked): ensure
            # `dtparam=audio=on` is present so the vc4hdmi ALSA card
            # exposes on boot. Idempotent — no-op when already set
            # (the live production sign already has it). Guarded so
            # non-zero doesn't trip set -e on a redeploy: a stale
            # boot file must not block a production update.
            patch_config_txt_audio "${boot_dir}/config.txt" \
                || say "  WARNING: config.txt audio patch skipped (see above)"
        else
            say "  DRYRUN: would patch config.txt (disable_splash=1) +"
            say "          cmdline.txt (quiet splash plymouth.ignore-serial-consoles)"
            say "          cmdline.txt strip (cgroup_disable=memory)"
            say "          config.txt gpu_mem=128 + cmdline.txt cma=320M"
            say "          config.txt audio (dtparam=audio=on)"
        fi
    fi
else
    say "  boot-config-lib.sh absent at ${BOOT_LIB}; skip boot-config patch"
fi

# --- 4b. HDMI audio: openmarquee user in 'audio' group --------------------
#
# HDMI audio 2026-07-01 (qarl decision, locked): the openmarquee-backend
# service runs as user `openmarquee` and needs to open /dev/snd/* to pipe
# VideoSlide audio out the vc4hdmi ALSA card. /dev/snd/ is owned root:audio
# with group-rw permissions, so the backend user MUST be in `audio` or
# every ffmpeg spawn ENOENTS on the ALSA device and the sign is silent.
#
# QA verified live (2026-07-01): a fresh SD-card image left the backend
# user OUT of `audio` and every audio spawn failed silently. usermod is
# idempotent — repeated runs with the user already in the group print an
# informational log and exit 0. We wrap it defensively so an unexpected
# failure (missing user, missing group) doesn't abort the install.
#
# Effect is permanent per subsequent process starts — a running backend
# needs a restart to pick the new group up. Section 8's daemon-reload +
# service restart below handles that automatically on install.sh redeploy.
if getent passwd openmarquee >/dev/null 2>&1 && getent group audio >/dev/null 2>&1; then
    say "Ensure openmarquee is in 'audio' group (HDMI audio)"
    if [ "$DRY_RUN" -eq 0 ]; then
        usermod -aG audio openmarquee \
            || say "  WARNING: usermod -aG audio openmarquee failed (HDMI audio may be silent)"
    else
        say "  DRYRUN: would run 'usermod -aG audio openmarquee'"
    fi
else
    say "  openmarquee user or audio group missing; skipping audio-group add"
fi

# --- 4c. mDNS: avahi-daemon + openmarquee.local advertisement -------------
#
# HANDOVER-BLOCKER 2026-07-01 (QA audit): the LINGER card + post-
# onboarding redirect promise `openmarquee.local` resolves from the
# operator's phone after the setup AP tears down. That name only
# resolves if avahi-daemon is installed + advertising on wlan0. Before
# this section, avahi lived only in the spec — onboarding shipped a
# broken promise (Jason loses the sign after ONLINE).
#
# Two files land here + avahi picks them up on daemon restart:
#   1. /etc/avahi/avahi-daemon.conf — overrides host-name to
#      `openmarquee` (so `.local` announces the marketing name
#      regardless of the per-device randomized /etc/hostname) and
#      allow-interfaces=wlan0 (so the AP interface ap0 isn't
#      dual-advertised during the SETUP overlap window; wlan0
#      advertisement survives AP teardown because wlan0 stays up).
#   2. /etc/avahi/services/openmarquee.service — the _http._tcp
#      port-80 record so DNS-SD browsers surface the dashboard.
#
# Both files are staged as source-of-truth under system/avahi/ so the
# image build + install.sh redeploy both drop the same content.
AVAHI_SRC_DIR="${OPT_DIR}/system/avahi"
AVAHI_CONF_DST="${ROOT_PREFIX}/etc/avahi/avahi-daemon.conf"
AVAHI_SVC_DIR="${ROOT_PREFIX}/etc/avahi/services"
# Header logged unconditionally so test_install_sh's dry-run coverage
# can pin the section presence (matches the hostapd/dnsmasq pattern).
# The actual apt-install + file drop is guarded on AVAHI_SRC_DIR
# existence below so a tmpdir dry-run without the sources doesn't
# error.
say "Configure avahi (mDNS openmarquee.local advertisement)"
say "  avahi-daemon.conf -> ${AVAHI_CONF_DST}"
say "  openmarquee.service -> ${AVAHI_SVC_DIR}/openmarquee.service"
if [ -d "$AVAHI_SRC_DIR" ]; then
    if [ "$DRY_RUN" -eq 0 ]; then
        # Ensure avahi is installed. Idempotent — apt-get returns 0 +
        # says "already the newest version" when it's already there.
        # Redeploy path: on a Pi that already has avahi from the image
        # build this is a cheap short-circuit; on a fresh Pi that
        # skipped the image build it's the install.
        if ! command -v avahi-daemon >/dev/null 2>&1; then
            apt-get update -qq \
                || say "  WARNING: apt-get update failed (avahi may not install)"
            apt-get install -y --no-install-recommends avahi-daemon libnss-mdns \
                || say "  WARNING: avahi install failed; mDNS unavailable"
        fi
        # Copy config + service. Non-destructive on the redeploy path:
        # cp overwrites atomically per POSIX, so a concurrent avahi read
        # can't see a half-written file.
        run install -m 0644 "${AVAHI_SRC_DIR}/avahi-daemon.conf" "$AVAHI_CONF_DST" \
            || say "  WARNING: avahi-daemon.conf install failed (redeploy)"
        run mkdir -p "$AVAHI_SVC_DIR"
        run install -m 0644 "${AVAHI_SRC_DIR}/openmarquee.service" \
            "${AVAHI_SVC_DIR}/openmarquee.service" \
            || say "  WARNING: openmarquee.service install failed (redeploy)"
        # Enable + restart. `enable --now` covers both fresh installs
        # (start it) and redeploys where the unit is already running
        # (restart to pick up the new conf/service files). Non-fatal
        # on failure — mDNS is a nice-to-have; a wedged systemctl
        # mustn't block the rest of install.sh.
        run systemctl enable --now avahi-daemon.service \
            || say "  WARNING: systemctl enable --now avahi-daemon failed"
        run systemctl restart avahi-daemon.service \
            || say "  WARNING: systemctl restart avahi-daemon failed"
    else
        say "  DRYRUN: would apt-install avahi-daemon + libnss-mdns"
        say "  DRYRUN: would install ${AVAHI_CONF_DST} + ${AVAHI_SVC_DIR}/openmarquee.service"
        say "  DRYRUN: would systemctl enable --now + restart avahi-daemon"
    fi
else
    say "  ${AVAHI_SRC_DIR} absent; skipping avahi setup (mDNS unavailable)"
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
# r60 (2026-06-04): AP off by default. Per qarl spec, the brcmfmac
# dual-mode (AP on ap0 + STA on wlan0) crashes under load (r43
# investigation). Mask the AP-side units so they cannot fire on
# boot or via late-postinst nudges. The unit files stay installed
# under /etc/systemd/system/ so an operator can re-enable for
# field-provisioning via `systemctl unmask openmarquee-ap0.service
# hostapd.service dnsmasq.service && systemctl start ...`.
#
# Field-provisioning fallback design lives in
# qa/r60-ap-off-by-default-design.md §A: today the operator runs
# the manual unmask + start sequence at the console (Option 2 from
# the dispatch); a one-shot first-boot-AP wizard with auto-disable
# is a future r62+ candidate.
#
# The mask is idempotent (no-op on already-masked units) and uses
# `|| true` because an unmask-after-prior-mask race (e.g. operator
# unmasked manually + restarted install.sh) shouldn't fail-stop
# under set -e.
say "Mask AP-side units (r60: AP off by default; field-provisioning is unmask-then-start)"
run systemctl mask openmarquee-ap0.service hostapd.service dnsmasq.service || true

# Stability arc Layer 3 boot-wiring fix (2026-06-23 post-review):
# DO NOT enable openmarquee-backend.service at boot — the L3
# promoter is the SOLE thing that starts backend (after stopping
# mini). Pre-fix this line was `systemctl enable openmarquee-
# backend.service` which would have caused backend AND mini to
# both auto-start at boot → fight over /dev/video10 → exactly
# the wedge L3 exists to prevent.
#
# `systemctl disable` is idempotent on an already-disabled unit
# (no-op). On FYS (current state per QA: backend already
# disabled, mini enabled), this is a no-op. On a fresh-install
# or post-rollback box where backend somehow ended up enabled,
# this enforces the scenario-B boot config.
run systemctl disable openmarquee-backend.service || true

# Stability arc Layer 3 (2026-06-23) + handover reconcile 2026-07-09
# (GAP1): the mini boot-default chain now ships wholly in-tree. mini's
# unit (openmarquee-mini.service, copied in §3 above) runs
# scripts/mini-play.sh, which loops video via gstreamer so a fresh
# sign is never DARK — both the first-boot default AND the L3 cap
# "stay on stable mini" rest-state. Install the script + a bundled
# welcome clip, then enable UNCONDITIONALLY. (Pre-reconcile this only
# enabled "if already present" because mini lived only on FYS from a
# prior manual install; now the whole chain is in the repo, so a fresh
# Jason sign gets it too.)
MINI_PLAY_DST="${ROOT_PREFIX}/usr/local/bin/mini-play.sh"
say "Install mini-play.sh (GAP1 boot-default renderer)"
run mkdir -p "$(dirname "$MINI_PLAY_DST")"
run install -m 0755 "${OPT_DIR}/scripts/mini-play.sh" "$MINI_PLAY_DST"

# Bundled welcome clip: mini's last-resort fallback when the sign has
# no playlist/content yet. Sourced from the seed asset (H264 1080p,
# baked into the image + rsynced on deploy) and copied to a clean
# canonical path (no spaces) for the gst filesrc. Non-fatal if the
# source is somehow absent — mini still plays playlist/content when
# present; only a truly empty sign would then be dark.
MINI_WELCOME_SRC="${OPT_DIR}/backend/openmarquee/seed_assets/videos/Hearth Fire.mp4"
MINI_WELCOME_DST="${ROOT_PREFIX}/opt/openmarquee/assets/welcome.mp4"
if [ -f "$MINI_WELCOME_SRC" ]; then
    say "Install mini welcome clip -> ${MINI_WELCOME_DST}"
    run mkdir -p "$(dirname "$MINI_WELCOME_DST")"
    run install -m 0644 "$MINI_WELCOME_SRC" "$MINI_WELCOME_DST"
else
    say "  WARNING: mini welcome clip source missing (${MINI_WELCOME_SRC});"
    say "  mini falls back to playlist/content only (empty sign would be DARK)."
fi

say "Enable openmarquee-mini.service at boot (boot-default + L3 cap rest-state)"
run systemctl enable openmarquee-mini.service || true

# Stability arc Layer 3 (2026-06-23): enable the post-reboot
# promoter. Runs once at boot (Type=oneshot), reads the
# backend-failure marker + reboot counter, decides whether
# to stop mini + start backend (normal path) or stay on mini
# under the reboot-loop cap. NOT gated on network-online (the
# recovery target IS wifi flakiness, so the promoter must
# run regardless of wifi state — backend's own After= deps
# handle network ordering at backend-start time). Hard
# ordering of mini-stop-then-backend-start is enforced inside
# the script so the systemd dep graph stays simple.
run systemctl enable openmarquee-stability-promoter.service

# r60: best-known-WiFi scanner + roam timer. Boot oneshot picks the
# strongest visible known SSID; 5-min recurring timer roams to a
# better SSID when one appears (with 8 pts NM-signal hysteresis to
# prevent oscillation). qa/r60-ap-off-by-default-design.md §B
# details the roam semantics + lockout-safety contract.
#
# Only the .timer carries [Install]/WantedBy (r60 subagent NIT fix
# to avoid boot-time double-fire on the .service); enabling the
# timer wires the unit pair into the boot dependency graph.
run systemctl enable openmarquee-best-wifi.timer

# P1.0 (2026-06-10, onboarding rework): WiFi power-save off on wlan0 +
# ap0. brcmfmac defaults power_save=on which causes dropped beacons /
# stalled associations under light load — exactly the "intermittent /
# unspecified" failure shape the onboarding spec is fixing. Pre-P1.0
# the wifi-watchdog only WARNed; nothing enforced OFF. This oneshot
# enforces at every boot. ap0 may be masked (r60 ships AP off by
# default); the script logs missing-iface gracefully so it's safe to
# enable unconditionally. See docs/onboarding-rework-plan.md §A
# contributor #2 + §D item #4.
run systemctl enable openmarquee-wifi-powersave-off.service

# Recovery A1 (2026-07-08): the "3x power-cycle → Setup Mode" gesture.
# Enable the increment oneshot (WantedBy=multi-user.target — runs early
# every boot to count cycles + write the setup hint at threshold) and the
# clear timer (WantedBy=timers.target — zeroes the counter ~20s into a
# stable boot so ordinary reboots never accumulate). The clear .service
# is triggered by its timer, not enabled directly. See
# system/openmarquee-boot-gesture.sh for the full design.
run systemctl enable openmarquee-boot-gesture.service
run systemctl enable openmarquee-boot-gesture-clear.timer

# r38c CMA-pressure watchdog -- timer fires the oneshot every 60s,
# oneshot reads /proc/meminfo and restarts openmarquee-backend.service
# if CmaUsed crosses THRESHOLD_MB (default 254MB per r59; was 220 in
# r38c, raised for v1.0.1) outside the cooldown window (default 30
# min). enable + start the timer; the .service is triggered by the
# timer and shouldn't be enabled directly.
# See qa/r59-cma-watchdog-default-decision-2026-06-04.md.
run systemctl enable openmarquee-cma-watchdog.timer
run systemctl start openmarquee-cma-watchdog.timer || true

# P1.2-B.2 (2026-06-10): enable the netctl socket so the take-over
# orchestrator can hit /run/openmarquee/netctl.sock. The matching
# template unit (openmarquee-netctl@.service) is spawned per-
# connection by systemd; we don't enable it directly. Best-effort
# start: if the .socket fails to bind (e.g. dir-create race), the
# next service restart picks it up cleanly.
run systemctl enable openmarquee-netctl.socket
run systemctl start openmarquee-netctl.socket || true

# r60 subagent (BLOCKER fix): only fire the best-wifi oneshot mid-
# install when we're NOT running under an interactive ssh session.
# If install.sh was invoked over wifi-ssh (developer redeploy via
# `bash scripts/deploy.sh`), the helper script's `nmcli connection
# up id <other-ssid>` would drop wlan0 mid-session and the install
# would die before the §8.5+ backend restart + /healthz probe ran.
# In cloud-init / first-boot contexts SSH_CONNECTION is unset
# (init's environment), so the oneshot fires normally. In all
# other contexts the OnBootSec=1min timer picks it up at next
# boot -- one boot's grace period is fine; the system was already
# working with whatever NM picked anyway.
if [ -z "${SSH_CONNECTION:-}" ]; then
    say "Starting best-wifi oneshot (boot context detected; SSH_CONNECTION unset)"
    run systemctl start openmarquee-best-wifi.service || true
else
    say "Deferring best-wifi oneshot (SSH session detected; OnBootSec=1min timer will pick it up at next boot)"
fi

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
# Loop is 75 × (curl --max-time 1 + sleep 1): ~75s when port 80 is
# unbound (curl returns immediately), up to ~150s if uvicorn is bound
# but hung. Budget bumped from 30s to 75s on 2026-05-31 (r28) to
# accommodate the r25 glyph-prewarm gate -- sidecar boot now spends
# ~33s draining the prewarm queue before the FastAPI lifespan
# completes, so the prior 30s probe false-WARNed on every clean
# install. Failure does NOT fail-stop install.sh — the backend may
# legitimately come up later when cloud-init finishes pulling in
# deferred services. Failure leaves a sentinel file so next-boot
# diagnosis has an obvious anchor.
if [ "$DRY_RUN" -eq 0 ]; then
    say "Probing backend health (~75s budget; up to ~150s if uvicorn hangs)"
    backend_up=0
    for _ in $(seq 1 75); do
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
        say "WARNING: backend did not respond to /healthz within 75s"
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
