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

# --- openmarquee ~/.ssh with correct perms ---
# The openmarquee user + /home/openmarquee already exist here: pi-gen's
# FIRST_USER_NAME creates them in stage2, which runs before stage-openmarquee.
on_chroot << 'EOF'
install -d -m 0700 -o openmarquee -g openmarquee /home/openmarquee/.ssh
EOF

# First-light hardening 2026-09-19: BAKE the operator's SSH key into the IMAGE
# (not only cloud-init) so ssh-over-wifi / wired recovery works EVEN IF
# cloud-init ever hiccups. build-image.sh --ssh-key drops the key here as
# `files/operator-authorized-keys`; if it's absent (no --ssh-key, or a dev
# redeploy) we fall back to an empty 0600 placeholder. The key is a PUBLIC key
# + sshd is key-only/no-password/no-root (openmarquee.conf above), so baking it
# is the intended access path, not new exposure.
OPERATOR_KEYS="${FILES}/operator-authorized-keys"
if [ -f "$OPERATOR_KEYS" ]; then
    install -m 0600 "$OPERATOR_KEYS" \
        "${ROOTFS_DIR}/home/openmarquee/.ssh/authorized_keys"
    on_chroot << 'EOF'
chown openmarquee:openmarquee /home/openmarquee/.ssh/authorized_keys
EOF
    echo "04-run.sh: baked operator SSH key into /home/openmarquee/.ssh/authorized_keys"
else
    on_chroot << 'EOF'
[ -f /home/openmarquee/.ssh/authorized_keys ] || \
  install -m 0600 -o openmarquee -g openmarquee /dev/null \
    /home/openmarquee/.ssh/authorized_keys
EOF
    echo "04-run.sh: no operator key staged — empty authorized_keys placeholder"
fi

# --- enable the ssh SERVICE at image-build time ---
# First-light hardening 2026-09-19: a burned card must be reachable over ssh
# (home wifi or USB tether) EVEN IF cloud-init never runs. Pi OS Lite ships
# openssh-server (also pinned in 00-packages) with the service DISABLED by
# default, and until now ssh was enabled ONLY from cloud-init's runcmd — so a
# cloud-init hiccup meant no ssh at all. Enable it here. `unmask` guards a base
# image that ships ssh.service masked. Key-only auth is already enforced by
# sshd_config.d/openmarquee.conf, so no password window is opened.
on_chroot << 'EOF'
systemctl unmask ssh.service 2>/dev/null || true
systemctl enable ssh.service
EOF
echo "04-run.sh: enabled ssh.service at base"
