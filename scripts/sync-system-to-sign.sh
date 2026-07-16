#!/usr/bin/env bash
# scripts/sync-system-to-sign.sh — low-impact resync of system/ config files to a
# LIVE sign, over the quiet link, without the heavy full-deploy.
#
# WHY THIS EXISTS (deploy-hygiene 2026-07-16, see
# docs/deploy-hygiene-audit-2026-07-16.md):
#   The routine live-sign deploy path (qa/deploy-to-sign.sh) is binary-ONLY —
#   it swaps /usr/local/bin/openmarquee-render and touches nothing under
#   system/. Only a full scripts/deploy.sh (or a fresh image burn) re-syncs
#   systemd units / the netctl daemon / hostapd / NM / sudoers / avahi / boot
#   config. On a fielded sign those are infrequent, so system/ drifts —
#   exactly the JasonsSign1 stale-netctl-daemon failure (PR #89, 1ac5811).
#
#   This wrapper makes a system/-only resync a first-class, LOW-IMPACT op:
#   it mirrors deploy-to-sign.sh's quiet-link discipline (stop the renderer
#   first so the transfer doesn't starve the WiFi during playback) but syncs
#   system/ + scripts/ + images/ and runs `install.sh --system-only` — the
#   OS-config sections only, skipping the venv/pip/backend app layer.
#
#   It is the PRIMITIVE, not the policy: QA can point her deploy-to-sign.sh at
#   this, run it directly, or wrap it — her orchestration, our lane provides
#   the tool. It does NOT hand-edit anything on the sign; everything flows
#   through the repo → rsync → install.sh pipeline.
#
# Usage:
#   scripts/sync-system-to-sign.sh <ssh-target>
#   scripts/sync-system-to-sign.sh openmarquee@openMarqueeDev
#
# Discipline baked in (matches the FYS-6-step + fail-loud-clean posture):
#   - PRE-FLIGHT compat gate (repo): refuse to ship if the repo's backend calls
#     a netctl subcommand the repo's daemon allowlist lacks (don't ship skew).
#   - FYS backup sidecar: tar the live system files (per-file granular
#     rollback) + md5 + tar-list-verify BEFORE any mutation.
#   - stop the renderer first (quiet link), trap-restart on ANY early exit so a
#     failed/aborted sync never leaves the sign dark.
#   - resilient rsync (retry over a flaky link).
#   - POST-SYNC compat gate ON THE SIGN (deployed daemon vs deployed backend) —
#     the direct JasonsSign1 detector — fail loud.
#   - assert no NEW failed openmarquee-netctl@ units + the socket is active.
#   - /healthz gate on restart.
set -euo pipefail

HOST="${1:?usage: sync-system-to-sign.sh <ssh-target>}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE_ROOT="${OPENMARQUEE_REMOTE_ROOT:-/opt/openmarquee}"
HEALTH_HOST="${HOST#*@}"

# --- PRE-FLIGHT: repo-internal netctl compat (don't ship known skew) --------
# Fail on the dev host BEFORE touching the sign if the repo's backend calls a
# subcommand the repo's daemon allowlist doesn't contain (exit 3), or the gate
# itself can't run / found an unknown transport (exit 2).
echo "== PRE-FLIGHT: netctl subcommand ↔ allowlist compat (repo) =="
if ! python3 "$REPO/scripts/check_netctl_compat.py"; then
    echo "ABORT: repo-internal netctl skew (or gate error) — fix before syncing." >&2
    exit 1
fi

# --- Safety net: restart the renderer if we exit after stopping it ----------
# (mirrors qa/deploy-to-sign.sh: a failed/aborted sync, an outer `timeout`
# SIGTERM, or a ctrl-C after STEP 3 would otherwise leave the sign DARK.)
STOPPED=0; DONE=0
cleanup() {
    if [ "$STOPPED" = 1 ] && [ "$DONE" = 0 ]; then
        echo "== ABORTED after stopping the backend — restarting so the sign is not left down ==" >&2
        ssh "$HOST" 'sudo systemctl reset-failed openmarquee-backend 2>/dev/null; sudo systemctl start openmarquee-backend' || true
    fi
}
trap cleanup EXIT INT TERM

# The live paths install.sh --system-only can (re)write — the FYS backup set.
# --ignore-failed-read: not every path exists on every sign / board variant.
# Kept in lockstep with the *_DST vars in scripts/install.sh.
BACKUP_PATHS=(
    /etc/systemd/system/openmarquee-\*
    /usr/local/sbin/openmarquee-netctl
    /usr/local/sbin/openmarquee-netctl-daemon
    /usr/local/bin/mini-play.sh
    /etc/hostapd/hostapd.conf
    /etc/dnsmasq.d/openmarquee.conf
    /etc/NetworkManager/conf.d/openmarquee-unmanaged.conf
    /etc/udev/rules.d/99-openmarquee-usb-wlan.rules
    /etc/cron.d/openmarquee-daily-restart
    /etc/tmpfiles.d/openmarquee.conf
    /etc/sudoers.d/openmarquee
    /etc/sysctl.d/99-openmarquee-swappiness.conf
    /etc/default/openmarquee-cma-watchdog
    /etc/avahi/avahi-daemon.conf
    /etc/avahi/services/openmarquee.service
    "${REMOTE_ROOT}/system"
)

# --- FYS backup: tar the live system files (per-file rollback) --------------
# Lives under $REMOTE_ROOT/backups (persistent — NOT /tmp, which is a small
# tmpfs on the sign). Steps: backup + md5 + tar-list-verify.
echo "== FYS backup: snapshot live system files on $HOST =="
BACKUP_DIR="${REMOTE_ROOT}/backups"
# shellcheck disable=SC2029  # intentional client-side expansion of the path glob list.
BACKUP_FILE="$(ssh "$HOST" "
    set -e
    sudo mkdir -p '$BACKUP_DIR'
    ts=\$(date +%Y%m%d-%H%M%S)
    f='$BACKUP_DIR'/system-presync-\$ts.tar.gz
    sudo tar czf \"\$f\" --ignore-failed-read ${BACKUP_PATHS[*]} 2>/dev/null || true
    [ -s \"\$f\" ] || { echo 'BACKUP EMPTY' >&2; exit 1; }
    sudo tar tzf \"\$f\" >/dev/null || { echo 'BACKUP UNREADABLE' >&2; exit 1; }
    m=\$(md5sum \"\$f\" | cut -d' ' -f1)
    n=\$(sudo tar tzf \"\$f\" | wc -l | tr -d ' ')
    echo \"  backup: \$f (md5 \$m, \$n entries, tar-list-verified)\" >&2
    echo \"\$f\"
")"
echo "== rollback (per-file): ssh $HOST 'sudo tar xzf $BACKUP_FILE -C / <path>' =="

# --- Record pre-existing failed netctl@ instances (baseline) ----------------
# The post-sync assertion allows a strict subset that PRE-existed; it only
# fails on NEW openmarquee-netctl failures introduced by this sync.
PRE_FAILED="$(ssh "$HOST" "systemctl --failed --no-legend 2>/dev/null | awk '{print \$1}' | grep -E '^openmarquee-netctl' | sort || true")"
[ -n "$PRE_FAILED" ] && echo "== note: pre-existing failed netctl units (baseline): $(echo "$PRE_FAILED" | tr '\n' ' ')"

# --- STEP 1: stop the renderer (quiet link) ---------------------------------
echo "== STEP 1: stop the renderer on $HOST (quiet link for the transfer) =="
ssh "$HOST" 'sudo systemctl stop openmarquee-backend && echo RENDERER_STOPPED'
STOPPED=1

# --- STEP 2: resilient rsync of system/ + scripts/ + images/ ----------------
# install.sh --system-only reads system/ + images/ from $REMOTE_ROOT and runs
# from $REMOTE_ROOT/scripts (which also carries check_netctl_compat.py for the
# post-sync gate). Excludes mirror scripts/deploy.sh's for these dirs.
echo "== STEP 2: rsync system/ + scripts/ + images/ over the quiet link =="
for dir in system scripts images; do
    tries=0
    until rsync -avz --rsync-path="sudo rsync" --delete --delete-excluded \
            --partial --timeout=30 \
            --exclude '._*' --exclude '__pycache__' \
            "$REPO/$dir/" "$HOST:$REMOTE_ROOT/$dir/"; do
        tries=$((tries+1)); echo "  rsync $dir retry $tries"; sleep 5
        [ $tries -ge 12 ] && { echo "ERROR: rsync $dir failed after $tries tries" >&2; exit 1; }
    done
done

# --- STEP 3: install ONLY the system-file sections --------------------------
echo "== STEP 3: install.sh --system-only (skips venv/pip/backend) =="
ssh "$HOST" "sudo bash $REMOTE_ROOT/scripts/install.sh --system-only"

# --- STEP 4: POST-SYNC compat gate ON THE SIGN ------------------------------
# The direct JasonsSign1 detector: does the DEPLOYED daemon's allowlist cover
# every subcommand the DEPLOYED backend calls? A partial sync (daemon or
# backend didn't land) fails here — loud.
echo "== STEP 4: netctl compat gate on the sign (deployed daemon vs deployed backend) =="
if ! ssh "$HOST" "python3 $REMOTE_ROOT/scripts/check_netctl_compat.py --daemon /usr/local/sbin/openmarquee-netctl-daemon --backend $REMOTE_ROOT/backend"; then
    echo "ERROR: post-sync netctl compat gate FAILED on the sign — the daemon" >&2
    echo "       allowlist does not cover the backend's calls. Sync incomplete." >&2
    echo "       Rollback: ssh $HOST 'sudo tar xzf $BACKUP_FILE -C / <path>'" >&2
    exit 1
fi

# --- STEP 5: restart the renderer + settle ----------------------------------
echo "== STEP 5: restart the renderer =="
ssh "$HOST" 'sudo systemctl start openmarquee-backend && sleep 10 && systemctl is-active openmarquee-backend'

# --- STEP 6: assert no NEW failed netctl@ units + socket active -------------
echo "== STEP 6: assert netctl@ family clean (no NEW failures vs baseline) =="
POST_FAILED="$(ssh "$HOST" "systemctl --failed --no-legend 2>/dev/null | awk '{print \$1}' | grep -E '^openmarquee-netctl' | sort || true")"
NEW_FAILED="$(comm -13 <(printf '%s\n' "$PRE_FAILED") <(printf '%s\n' "$POST_FAILED") | sed '/^$/d')"
if [ -n "$NEW_FAILED" ]; then
    echo "ERROR: NEW failed openmarquee-netctl unit(s) after sync: $(echo "$NEW_FAILED" | tr '\n' ' ')" >&2
    echo "       inspect: ssh $HOST sudo journalctl -u openmarquee-netctl@ -n 50" >&2
    echo "       Rollback: ssh $HOST 'sudo tar xzf $BACKUP_FILE -C / <path>'" >&2
    exit 1
fi
ssh "$HOST" 'systemctl is-active openmarquee-netctl.socket >/dev/null && echo "  netctl.socket active"' \
    || { echo "ERROR: openmarquee-netctl.socket is not active after sync" >&2; exit 1; }

# --- STEP 7: /healthz gate --------------------------------------------------
echo "== STEP 7: backend health (/healthz must 200 within 30s) =="
if ! curl --max-time 30 --retry 5 --retry-delay 2 --fail -sS "http://$HEALTH_HOST/healthz" > /dev/null; then
    echo "ERROR: backend did not return 200 within budget after sync" >&2
    echo "       inspect: ssh $HOST sudo journalctl -u openmarquee-backend -n 50" >&2
    exit 1
fi

DONE=1
cat <<EOF

== system/ resynced to $HOST ==
  backup (rollback):  $BACKUP_FILE
  restore one file:   ssh $HOST 'sudo tar xzf $BACKUP_FILE -C / <path>'
  status:             ssh $HOST sudo systemctl status openmarquee-backend
EOF
