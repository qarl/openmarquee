#!/bin/bash -e
# 05-run.sh — substage runner for openMarquee USB-gadget networking.
#
# Runs on the HOST during pi-gen's stage walk (per pi-gen convention):
#   * ${ROOTFS_DIR} points at the mounted image rootfs.
#
# Bakes the NetworkManager profile that brings up the CDC-ether gadget
# interface `usb0` into the BASE image, so the Pi is reachable as
# <sign-name>.local over a USB cable the instant it boots — the wired
# recovery path the Pi Zero 2 W lacks (no onboard ethernet), and qarl's
# tether for the "fireplacesign" dev device. The kernel side (dwc2 +
# g_ether) is enabled by the 02-boot-config substage; avahi advertises on
# usb0 via system/avahi (installed by install.sh). This substage owns
# only the usb0 IP bring-up.
#
# Baked into the base image (not just install.sh) on purpose: recovery
# must work BEFORE the app install runs. Coexists with wlan0 (the profile
# is bound to interface-name=usb0).
#
# What lands:
#   /etc/NetworkManager/system-connections/usb0.nmconnection  (0600 root)
#
# NM SILENTLY IGNORES a system-connection keyfile that is group- or
# world-readable ("ignoring insecure connection"), so the 0600 root:root
# mode is load-bearing, not hygiene. Idempotent: install overwrites.

FILES="${BASH_SOURCE[0]%/*}/files"

install -d -m 0755 "${ROOTFS_DIR}/etc/NetworkManager/system-connections"
# 0600 + root:root is REQUIRED for NM to accept the profile. pi-gen runs
# as root, so numeric 0:0 ownership takes regardless of the host user.
install -m 0600 -o 0 -g 0 \
    "${FILES}/etc/NetworkManager/system-connections/usb0.nmconnection" \
    "${ROOTFS_DIR}/etc/NetworkManager/system-connections/usb0.nmconnection"

echo "05-run.sh: staged usb0.nmconnection (link-local CDC-ether gadget) at 0600 root"
