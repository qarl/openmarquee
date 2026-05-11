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

set -euo pipefail

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
if [ -f "${OPT_DIR}/backend/requirements.lock" ]; then
    run "${VENV_DIR}/bin/pip" install --upgrade -r "${OPT_DIR}/backend/requirements.lock"
fi
run "${VENV_DIR}/bin/pip" install --upgrade -e "${OPT_DIR}/backend"

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

if [ ! -f "$BOOTSTRAP_MARKER" ] && [ "$DRY_RUN" -eq 0 ]; then
    # First boot. `enable --now` is synchronous for Type=oneshot units --
    # blocks until firstboot.sh exits, so hostapd.conf + welcome.html
    # are templated by the time we move on to backend restart.
    say "First boot detected; running openmarquee-firstboot.service"
    run systemctl enable --now openmarquee-firstboot.service
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
    fi
fi

# --- 8. systemctl reload + enable -------------------------------------------

say "Reload systemd + enable units"
run systemctl daemon-reload
run systemctl enable openmarquee-backend.service \
                    openmarquee-ap0.service

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

say "Done."
