#!/usr/bin/env bash
# scripts/stage_sd_card.sh -- drop the openMarquee bundle + cloud-init
# files onto a freshly-flashed Pi OS Lite arm64 SD card's bootfs
# partition, so the Pi auto-bootstraps into AP mode on first power-on.
#
# Usage:
#     bash scripts/stage_sd_card.sh /Volumes/bootfs
#     bash scripts/stage_sd_card.sh /run/media/$USER/bootfs   # Linux
#
# Prerequisites:
#   1. Flash Pi OS Lite arm64 to the SD with Raspberry Pi Imager.
#   2. Eject + re-insert the card; the bootfs partition automounts on
#      most desktops.
#   3. Run `bash scripts/build_sd_bundle.sh` first to produce
#      dist/openmarquee-sd-bundle.tar.zst.
#
# What this drops onto bootfs:
#   - openmarquee-bundle.tar.zst  (the bundle from build_sd_bundle.sh)
#   - user-data                   (cloud-init: enable ssh, extract bundle,
#                                  run install.sh, leave failure marker
#                                  if anything goes wrong)
#   - meta-data                   (unique instance-id; required for
#                                  cloud-init even when empty)
#   - network-config              (explicit: NO wifi pre-configuration;
#                                  AP mode handles wifi from here)
#
# Does NOT drop wpa_supplicant.conf (legacy + would conflict with AP mode).
#
# Recovery path: if install.sh fails on the Pi, /var/openmarquee-install-
# failed is created so an operator can ssh in (ssh is enabled before
# install runs) and diagnose. The Pi is reachable on its DHCP-assigned
# ethernet IP if cabled, or wifi-AP is up if firstboot.sh ran far enough.

set -euo pipefail

# --- Resolve paths ----------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUNDLE="${OPENMARQUEE_BUNDLE:-$REPO_ROOT/dist/openmarquee-sd-bundle.tar.zst}"

# --- Arg validation ---------------------------------------------------------

if [ $# -ne 1 ]; then
    echo "usage: $0 <bootfs-mount-path>" >&2
    echo "example: $0 /Volumes/bootfs   # macOS" >&2
    echo "         $0 /run/media/\$USER/bootfs   # Linux" >&2
    exit 1
fi

BOOTFS="$1"

if [ ! -d "$BOOTFS" ]; then
    echo "error: $BOOTFS doesn't exist or isn't a directory" >&2
    echo "       Did you flash + insert the SD? Check Disk Utility / lsblk." >&2
    exit 2
fi

# --- Sanity-check that this looks like a real Pi bootfs --------------------

# cmdline.txt is Pi-specific kernel boot args; config.txt is Pi-specific
# firmware config. Both are present on every Pi OS Lite arm64 image since
# at least 2020. If neither exists, the operator's mount path is wrong --
# refuse to write a 200MB tarball to /Volumes/Untitled or similar.
if [ ! -f "$BOOTFS/cmdline.txt" ] && [ ! -f "$BOOTFS/config.txt" ]; then
    echo "error: $BOOTFS doesn't look like a Pi bootfs" >&2
    echo "       (no cmdline.txt or config.txt; check the mount path)" >&2
    exit 3
fi

# --- Bundle must exist ------------------------------------------------------

if [ ! -f "$BUNDLE" ]; then
    echo "error: bundle not found at $BUNDLE" >&2
    echo "       run \`bash scripts/build_sd_bundle.sh\` first." >&2
    exit 4
fi

# --- Drop the bundle --------------------------------------------------------

BUNDLE_DST="$BOOTFS/openmarquee-bundle.tar.zst"
BUNDLE_SIZE=$(stat -f%z "$BUNDLE" 2>/dev/null || stat -c%s "$BUNDLE")
echo "==> copying bundle to $BUNDLE_DST ($BUNDLE_SIZE bytes)"
cp "$BUNDLE" "$BUNDLE_DST"

# --- cloud-init: meta-data --------------------------------------------------

# Per-device unique instance-id so cloud-init treats each re-flash as a
# fresh provisioning run. Date + random suffix is plenty unique for the
# burn-a-card-a-day flow; if you re-flash the same physical SD multiple
# times you want each one to re-run the runcmd.
INSTANCE_ID="openmarquee-$(date +%Y%m%d-%H%M%S)-$RANDOM"
cat > "$BOOTFS/meta-data" <<EOF
instance-id: $INSTANCE_ID
local-hostname: mysign-init
EOF
echo "==> wrote meta-data (instance-id=$INSTANCE_ID)"

# --- cloud-init: network-config --------------------------------------------

# Explicit "no wifi pre-config" -- per qarl directive, AP mode is the only
# wifi path for this slice. The Pi boots, openmarquee-firstboot.service
# generates per-device AP password, hostapd brings up ap0 wifi. The Pi
# never connects to a home network (station mode is OUT OF SCOPE per
# dispatch). Ethernet is DHCP if cabled.
#
# version: 2 is netplan-style yaml that cloud-init understands. We
# explicitly list NO wifis: section so the Pi's wifi radio is available
# for hostapd to claim.
cat > "$BOOTFS/network-config" <<'EOF'
version: 2
ethernets:
  eth0:
    dhcp4: true
    optional: true
# Intentionally no `wifis:` section.
# AP mode is configured by openmarquee-firstboot.service after first boot;
# pre-configuring wifi here would conflict with hostapd's ap0 setup.
EOF
echo "==> wrote network-config (eth DHCP only, no wifi pre-config)"

# --- cloud-init: user-data --------------------------------------------------

# runcmd executes ONCE per instance-id. The flow:
#   1. enable + start ssh (per dev-pi memory: ssh.service is masked on
#      Pi OS Lite arm64 by default and needs explicit enable; without
#      this the operator can't recover from a failed install)
#   2. extract the bundle to /opt/openmarquee
#   3. fix ownership (cloud-init runs as root; backend wants openmarquee
#      user to own state)
#   4. ensure openmarquee system user exists (install.sh expects it)
#   5. run install.sh
#   6. on ANY non-zero exit, drop /var/openmarquee-install-failed so the
#      operator can find the journal log
#
# We DO NOT add wpa_supplicant.conf or any wifi config (per dispatch
# scope-out). The Pi-OS-Lite-arm64 cloud-init replaces legacy
# wpa_supplicant.conf anyway -- mixing the two is a known footgun
# (memory: project_phase_b_sd_card_automation).
cat > "$BOOTFS/user-data" <<'EOF'
#cloud-config

# Enable + start ssh first so the operator can recover if anything later
# fails. ssh.service is masked on Pi OS Lite arm64 by default; we have to
# explicitly `systemctl unmask` before `enable + start`.
ssh_pwauth: false

users:
  - default
  - name: openmarquee
    system: true
    shell: /usr/sbin/nologin
    home: /var/openmarquee
    homedir: /var/openmarquee

# packages cloud-init installs BEFORE runcmd. We need:
#   - zstd to decompress the bundle tarball
#   - python3-venv to bootstrap the venv (install.sh expects it)
#   - hostapd + dnsmasq for AP mode (install.sh stages configs)
#   - iptables for the captive-portal NAT rule
package_update: true
packages:
  - zstd
  - python3-venv
  - hostapd
  - dnsmasq
  - iptables

runcmd:
  - [ systemctl, unmask, ssh.service ]
  - [ systemctl, enable, --now, ssh.service ]
  - [ mkdir, -p, /opt/openmarquee ]
  # Bundle lives at /boot/firmware/openmarquee-bundle.tar.zst on Pi OS
  # Bookworm (where bootfs is mounted at /boot/firmware/) -- cloud-init's
  # default for the bootfs partition's user-data file moved here in 2024.
  # Older Pi OS images mount bootfs at /boot/; cloud-init understands both
  # but the file path differs. Try /boot/firmware first.
  - [ sh, -c, '
      if [ -f /boot/firmware/openmarquee-bundle.tar.zst ]; then
          BUNDLE=/boot/firmware/openmarquee-bundle.tar.zst;
      elif [ -f /boot/openmarquee-bundle.tar.zst ]; then
          BUNDLE=/boot/openmarquee-bundle.tar.zst;
      else
          echo "openmarquee-bundle.tar.zst not found in /boot or /boot/firmware" >&2;
          touch /var/openmarquee-install-failed;
          exit 1;
      fi;
      zstd -d -c "$BUNDLE" | tar -C /opt -xf -
    ' ]
  # The tarball lands as /opt/openmarquee/. Fix ownership so the
  # openmarquee user (created above) owns its tree.
  - [ chown, -R, 'openmarquee:openmarquee', /opt/openmarquee ]
  # Run install.sh. Capture exit code; drop a marker file on failure
  # so a recovery ssh sees something obvious in /var/.
  - [ sh, -c, '
      if ! bash /opt/openmarquee/scripts/install.sh; then
          echo "install.sh exited non-zero -- see journalctl" >&2;
          touch /var/openmarquee-install-failed;
          exit 1;
      fi
    ' ]

# After runcmd succeeds, openmarquee-firstboot.service runs (enabled by
# install.sh) which generates per-device wifi.json + identity.json +
# templates hostapd.conf with a fresh AP password. The backend service
# then starts and serves the welcome UI at 10.0.0.1 over the ap0 wifi.
EOF
echo "==> wrote user-data (cloud-init runcmd)"

# --- Done -------------------------------------------------------------------

cat <<EOF

SD card staged at $BOOTFS:
    openmarquee-bundle.tar.zst   ($BUNDLE_SIZE bytes)
    user-data                    (cloud-init runcmd)
    meta-data                    (instance-id=$INSTANCE_ID)
    network-config               (eth DHCP, no wifi pre-config)

next:
    1. Eject the SD: diskutil eject (macOS) or umount (Linux)
    2. Insert into Pi, power on
    3. Wait ~2 minutes for first-boot provisioning
    4. AP "MySign-XXXX" appears in wifi list; password is on the
       welcome UI at http://10.0.0.1/ once connected
    5. If AP doesn't come up: ssh openmarquee@mysign-init.local (ethernet)
       and check journalctl -u openmarquee-backend and
       /var/openmarquee-install-failed

EOF
