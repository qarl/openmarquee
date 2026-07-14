#!/bin/bash -e
# 04-run.sh — substage runner for the openMarquee SSH-user hardening.
#
# Runs on the HOST during pi-gen's stage walk (per pi-gen convention):
#   * ${ROOTFS_DIR} points at the mounted image rootfs.
#   * the `on_chroot` helper runs commands INSIDE the image chroot.
#
# Bakes the image-level SSH lockdown + sudo grant for the `openmarquee`
# user — which is BOTH the systemd service user AND the sole key-only SSH
# login identity on a shipped device — so they are present on a fresh card
# INDEPENDENT of cloud-init. Idempotent: install/copy overwrite, and the
# .ssh setup is create-if-missing.
#
# What lands:
#   /etc/ssh/sshd_config.d/openmarquee.conf  (0644 root) — key-only, no root
#   /etc/sudoers.d/openmarquee               (0440 root) — full NOPASSWD, visudo-c'd
#   ~openmarquee/.ssh/                        (0700 openmarquee)
#   ~openmarquee/.ssh/authorized_keys         (0600 openmarquee) placeholder —
#     cloud-init / the flash tooling injects the operator's key here.

FILES="${BASH_SOURCE[0]%/*}/files"

# --- sshd hardening drop-in (root:root 0644; -D makes the parent dir) ---
install -d -m 0755 "${ROOTFS_DIR}/etc/ssh/sshd_config.d"
install -m 0644 "${FILES}/etc/ssh/sshd_config.d/openmarquee.conf" \
    "${ROOTFS_DIR}/etc/ssh/sshd_config.d/openmarquee.conf"

# --- sudoers drop-in (root:root 0440 — sudoers REQUIRES exactly 0440) ---
install -d -m 0750 "${ROOTFS_DIR}/etc/sudoers.d"
install -m 0440 "${FILES}/etc/sudoers.d/openmarquee" \
    "${ROOTFS_DIR}/etc/sudoers.d/openmarquee"

# Validate the sudoers drop-in INSIDE the chroot (target-arch visudo) so a
# malformed file fails the BUILD, never bricks a booted device's sudo.
on_chroot << 'EOF'
visudo -cf /etc/sudoers.d/openmarquee
EOF

# --- openmarquee ~/.ssh with correct perms (create-if-missing) ---
# The openmarquee user + /home/openmarquee already exist here: pi-gen's
# FIRST_USER_NAME creates them in stage2, which runs before stage-openmarquee.
# Set .ssh up now so perms are right the instant cloud-init / the flash
# tooling drops the operator key into authorized_keys.
on_chroot << 'EOF'
install -d -m 0700 -o openmarquee -g openmarquee /home/openmarquee/.ssh
[ -f /home/openmarquee/.ssh/authorized_keys ] || \
  install -m 0600 -o openmarquee -g openmarquee /dev/null \
    /home/openmarquee/.ssh/authorized_keys
EOF
