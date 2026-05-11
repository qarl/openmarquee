#!/bin/bash -e
# 00-run.sh — substage runner (runs OUTSIDE the chroot; the package
# list above is consumed by pi-gen before this script fires).
#
# We don't need to do anything here yet -- B.3 (install.sh) is where
# the venv + systemd unit drop-in + hostapd/dnsmasq templating happens.
# At pi-gen time we just lay the packages down; first-boot config is
# cloud-init's job (B.2) and runs `install.sh` from /opt/openmarquee/.
#
# Keeping this script as a no-op explicit placeholder so pi-gen's
# stage walker sees the substage and apt resolves the package list.
# (A substage with only a package list and no runner is valid; this
# empty runner is documentation for the next leg.)

true
