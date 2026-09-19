#!/bin/bash -e
# 06-run.sh — substage runner for openMarquee cloud-init enablement.
#
# Runs on the HOST during pi-gen's stage walk (per pi-gen convention):
#   * ${ROOTFS_DIR} points at the mounted image rootfs.
#   * the `on_chroot` helper runs commands INSIDE the image chroot
#     (target arch); `systemctl enable` in a chroot just creates the
#     .wants symlinks on the filesystem — no running systemd needed.
#
# FIRST-LIGHT FIX 2026-09-19. The cloud-init PACKAGE is installed (see
# 00-install-packages) but on the first burned card nothing made it RUN:
# /var/log/cloud-init.log was empty, /etc/hostname was stock `raspberrypi`,
# the staged bundle never extracted, install.sh never ran → black screen.
# Two gaps, both closed here:
#
#   (1) UNITS NEVER ENABLED. apt-installing cloud-init in the pi-gen chroot
#       does NOT leave its systemd units enabled on Debian/RPi-OS, so
#       nothing lands in multi-user.target.wants and cloud-init never
#       fires. The only `systemctl enable cloud-init*` calls in the tree
#       live INSIDE cloud-init's own runcmd + install.sh, which only run
#       IF cloud-init already ran — a chicken-and-egg that never breaks.
#       We enable the units here at build time. The unit SET was renamed
#       across cloud-init versions (24.x split cloud-init.service into
#       cloud-init-network.service + a cloud-init-main.service orchestrator),
#       and trixie ships 24.x — so we enable TOLERANTLY: try each known
#       name, skip absent, unmask any masked. Fail loudly only if NONE of
#       them exist (that would mean the package wasn't actually installed).
#
#   (2) NOCLOUD DATASOURCE NEVER POINTED AT THE BOOT PARTITION. Closed by
#       the drop-in in files/etc/cloud/cloud.cfg.d/99_openmarquee.cfg,
#       installed below.
#
# NOTE (2026-09-19): this commit is the static wiring + unit tests. Admin is
# validating this EXACT recipe live on the fireplacesign card over a USB
# serial console in parallel (verify-before-bake); live confirmation of the
# enabled unit names + seedfrom resolution is pending that run, and any delta
# reconciles as a follow-up. Do NOT treat this as live-validated yet.
#
# What lands:
#   /etc/cloud/cloud.cfg.d/99_openmarquee.cfg   (0644 root) — NoCloud seed cfg
#   cloud-init systemd units enabled in the chroot

FILES="${BASH_SOURCE[0]%/*}/files"

# --- NoCloud datasource drop-in (root:root 0644) ---
install -d -m 0755 "${ROOTFS_DIR}/etc/cloud/cloud.cfg.d"
install -m 0644 "${FILES}/etc/cloud/cloud.cfg.d/99_openmarquee.cfg" \
    "${ROOTFS_DIR}/etc/cloud/cloud.cfg.d/99_openmarquee.cfg"

# --- enable the cloud-init units + clear any disable marker (in chroot) ---
# Tolerant loop: unit names differ across cloud-init versions. Guard each
# `enable` with list-unit-files so an absent unit is skipped, not fatal.
on_chroot << 'EOF'
set -e
rm -f /etc/cloud/cloud-init.disabled
enabled_any=0
for unit in cloud-init-local.service \
            cloud-init.service \
            cloud-init-network.service \
            cloud-init-main.service \
            cloud-config.service \
            cloud-final.service; do
    if systemctl list-unit-files "$unit" 2>/dev/null | grep -q "^${unit}"; then
        systemctl unmask "$unit" 2>/dev/null || true
        if systemctl enable "$unit" 2>/dev/null; then
            echo "06-run.sh: enabled ${unit}"
            enabled_any=1
        fi
    fi
done
# Belt: the target that groups the cloud-init stage services.
if systemctl list-unit-files cloud-init.target 2>/dev/null | grep -q '^cloud-init.target'; then
    systemctl enable cloud-init.target 2>/dev/null || true
fi
if [ "$enabled_any" = "0" ]; then
    echo "06-run.sh: ERROR — no cloud-init units found to enable (is the package installed?)" >&2
    exit 1
fi
EOF

echo "06-run.sh: cloud-init units enabled + NoCloud-from-boot cfg staged"
