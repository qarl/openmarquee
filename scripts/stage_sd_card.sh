#!/usr/bin/env bash
# scripts/stage_sd_card.sh -- drop the openMarquee bundle + cloud-init
# files onto a freshly-flashed Pi OS Lite arm64 SD card's bootfs
# partition, so the Pi auto-bootstraps into AP mode on first power-on.
#
# Usage:
#     bash scripts/stage_sd_card.sh /Volumes/bootfs
#     bash scripts/stage_sd_card.sh /run/media/$USER/bootfs   # Linux
#
#     # Optional: stage operator-provided NetworkManager keyfile
#     # connection profiles so the Pi auto-joins a home wifi on
#     # FIRST boot (otherwise the device only has AP mode).
#     bash scripts/stage_sd_card.sh \
#         --wifi-profiles ~/Desktop/jasons-sign-wifi/ \
#         /Volumes/bootfs
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
#   - openmarquee-wifi/*.nmconnection  (OPTIONAL — only when
#                                  --wifi-profiles DIR is passed)
#
# Does NOT drop wpa_supplicant.conf (legacy + would conflict with AP mode).
#
# Optional --wifi-profiles DIR (JasonsSign1 first-boot fix, 2026-06-11):
#   Pre-stage NetworkManager keyfile connection profiles so the Pi has
#   home-wifi connectivity on FIRST boot. The files in DIR matching
#   *.nmconnection are copied to bootfs/openmarquee-wifi/ and a
#   cloud-init `bootcmd` block is added to user-data that installs them
#   into /etc/NetworkManager/system-connections/ with mode 0600 BEFORE
#   NetworkManager.service starts. Critical ordering: `runcmd` runs in
#   cloud-final (after NM is up); `bootcmd` runs in cloud-init-local
#   (before NM is up, in the very first systemd milestone). The runcmd
#   path was the QA-hand-spliced fix attempt that failed on the Jason
#   gift device tonight — profiles loaded only on the SECOND boot.
#
#   The default path (no --wifi-profiles) is UNCHANGED: no
#   openmarquee-wifi/ dir, no bootcmd block in user-data, AP-only
#   wifi as before. The flag is purely additive.
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

# --- Arg parsing ------------------------------------------------------------

WIFI_PROFILES_DIR=""
BOOTFS=""
SSH_KEY_PATH=""

usage() {
    cat >&2 <<EOF
usage: $0 [--wifi-profiles DIR] [--ssh-key PATH] <bootfs-mount-path>

  --wifi-profiles DIR   Pre-stage NetworkManager keyfile profiles
                        (*.nmconnection in DIR) so the Pi auto-joins
                        a home wifi on FIRST boot. Optional. If
                        omitted, the device is AP-only.

  --ssh-key PATH        Public key file to seed as openmarquee's SSH
                        authorized key on the shipped card (single pubkey,
                        e.g. ~/.ssh/id_ed25519.pub). Default: the first of
                        ~/.ssh/id_ed25519.pub, ~/.ssh/id_rsa.pub if present.
                        Without a key the card boots but SSH-in is
                        impossible (key-only auth) — a loud warning fires.

examples:
    $0 /Volumes/bootfs                                 # macOS, AP-only
    $0 --wifi-profiles ~/wifi-profiles /Volumes/bootfs  # with home-wifi
    $0 --ssh-key ~/.ssh/id_ed25519.pub /Volumes/bootfs  # seed SSH key
    $0 /run/media/\$USER/bootfs                         # Linux
EOF
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --wifi-profiles)
            [ $# -ge 2 ] || { echo "error: --wifi-profiles requires a value" >&2; exit 1; }
            WIFI_PROFILES_DIR="$2"
            shift 2
            ;;
        --wifi-profiles=*)
            WIFI_PROFILES_DIR="${1#--wifi-profiles=}"
            shift
            ;;
        --ssh-key)
            [ $# -ge 2 ] || { echo "error: --ssh-key requires a value" >&2; exit 1; }
            SSH_KEY_PATH="$2"
            shift 2
            ;;
        --ssh-key=*)
            SSH_KEY_PATH="${1#--ssh-key=}"
            shift
            ;;
        --help|-h)
            usage
            ;;
        -*)
            echo "error: unknown flag: $1" >&2
            usage
            ;;
        *)
            if [ -n "$BOOTFS" ]; then
                echo "error: extra positional arg: $1 (already have $BOOTFS)" >&2
                usage
            fi
            BOOTFS="$1"
            shift
            ;;
    esac
done

[ -n "$BOOTFS" ] || usage

# Resolve the SSH key early (before the slow bundle copy): explicit
# --ssh-key wins, else the first default that exists. It gets substituted
# into user-data's {{SSH_AUTHORIZED_KEYS}} below so the operator can SSH in
# as openmarquee (key-only auth — without a key the card is unreachable).
if [ -z "$SSH_KEY_PATH" ]; then
    for _cand in "$HOME/.ssh/id_ed25519.pub" "$HOME/.ssh/id_rsa.pub"; do
        if [ -f "$_cand" ]; then SSH_KEY_PATH="$_cand"; break; fi
    done
fi
if [ -n "$SSH_KEY_PATH" ] && [ ! -f "$SSH_KEY_PATH" ]; then
    echo "error: --ssh-key file not found: $SSH_KEY_PATH" >&2
    exit 1
fi

# Validate --wifi-profiles arg shape early so a typo doesn't get caught
# only after the bundle copy (the slow step).
if [ -n "$WIFI_PROFILES_DIR" ]; then
    if [ ! -d "$WIFI_PROFILES_DIR" ]; then
        echo "error: --wifi-profiles dir doesn't exist: $WIFI_PROFILES_DIR" >&2
        exit 1
    fi
    # Must contain at least one *.nmconnection file or the flag is a no-op.
    NMCONN_COUNT=$(find "$WIFI_PROFILES_DIR" -maxdepth 1 -name '*.nmconnection' -type f | wc -l | tr -d ' ')
    if [ "$NMCONN_COUNT" -eq 0 ]; then
        echo "error: no *.nmconnection files in $WIFI_PROFILES_DIR" >&2
        echo "       (export from a working machine via:" >&2
        echo "        sudo cp /etc/NetworkManager/system-connections/<name>.nmconnection ~/wifi-profiles/)" >&2
        exit 1
    fi
fi

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
# --- Optional: stage wifi keyfile profiles ----------------------------------

# When --wifi-profiles is set, copy the .nmconnection files into a
# subdirectory of bootfs (which becomes /boot/firmware/ on the Pi).
# A bootcmd block (added below to user-data) installs them into
# /etc/NetworkManager/system-connections/ BEFORE NetworkManager
# starts. Without this pre-NM install, NM has no profiles to load on
# its first scan and the device boots with no network.
#
# The FAT bootfs partition loses Linux file permissions on copy, but
# that doesn't matter — the bootcmd's `install -m 0600` on the target
# side sets the canonical mode NM requires for keyfile profiles.
WIFI_BOOTCMD_BLOCK=""
if [ -n "$WIFI_PROFILES_DIR" ]; then
    WIFI_STAGE_DIR="$BOOTFS/openmarquee-wifi"
    rm -rf "$WIFI_STAGE_DIR"
    mkdir -p "$WIFI_STAGE_DIR"
    COPIED=0
    for src in "$WIFI_PROFILES_DIR"/*.nmconnection; do
        [ -f "$src" ] || continue
        base=$(basename "$src")
        cp "$src" "$WIFI_STAGE_DIR/$base"
        COPIED=$((COPIED + 1))
    done
    echo "==> staged $COPIED wifi profile(s) to $WIFI_STAGE_DIR"

    # bootcmd runs in cloud-init-local.service (Before=NetworkManager.
    # service per Debian Bookworm's systemd ordering) so the install
    # lands BEFORE NM scans its keyfile dir.
    #
    # Cadence: cloud-init's `bootcmd` is documented to run on EVERY
    # boot (NOT once-per-instance-id like `runcmd`). That's correct
    # for our case because the `install -m 0600 -o root -g root`
    # commands are idempotent — re-installing the same keyfile bytes
    # at the same path with the same mode is a no-op. Side effect
    # operators should know: a profile deleted on the running Pi
    # (e.g. `nmcli c delete HomeWifi`) WILL reappear on next reboot
    # unless the matching file is also removed from
    # /boot/firmware/openmarquee-wifi/. To permanently remove a
    # profile, delete it from BOTH locations.
    #
    # Path note: on Pi OS Bookworm bootfs is mounted at /boot/firmware/;
    # on older images it's /boot/. We check both — same fallback the
    # bundle-extract runcmd uses.
    # Marker file later sed-inserted before the `runcmd:` line so
    # the bootcmd block executes BEFORE the runcmd / NM startup
    # sequence. Kept as a here-doc-able multi-line string for the
    # test harness; YAML-validity verified by test_stage_sd_card_
    # wifi_profiles.sh.
    WIFI_BOOTCMD_BLOCK=$(cat <<'WIFI_BOOTCMD_EOF'
bootcmd:
  - [ sh, -c, '
      if [ -d /boot/firmware/openmarquee-wifi ]; then SRC=/boot/firmware/openmarquee-wifi;
      elif [ -d /boot/openmarquee-wifi ]; then SRC=/boot/openmarquee-wifi;
      else exit 0; fi;
      install -d -m 0700 -o root -g root /etc/NetworkManager/system-connections;
      for f in "$SRC"/*.nmconnection; do
          [ -f "$f" ] || continue;
          dst=/etc/NetworkManager/system-connections/$(basename "$f");
          install -m 0600 -o root -g root "$f" "$dst";
      done
    ' ]

WIFI_BOOTCMD_EOF
)
fi

cat > "$BOOTFS/user-data" <<'EOF'
#cloud-config

# Lock down SSH: key-only, no password auth, no root login. (The
# image-baked /etc/ssh/sshd_config.d/openmarquee.conf is the canonical
# enforcement; these cloud-init toggles are the belt to its suspenders.)
ssh_pwauth: false
disable_root: true

# `openmarquee` does double duty — it is the systemd service user AND the
# sole key-only SSH login identity. pi-gen's FIRST_USER_NAME already created
# it (login-capable /home/openmarquee, /bin/bash); we re-declare by name to
# ATTACH the operator's SSH key, full passwordless sudo (operational admin
# over the SSH login — the service itself runs under NoNewPrivileges and
# can't sudo), and the render/video groups. NOT a nologin/system account:
# an operator must be able to SSH in as openmarquee.
users:
  - name: openmarquee
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    # `video`: open /dev/video10 (bcm2835-codec H.264 decoder) for the Rust
    # IPC VideoSlide route (rooted `crw-rw---- root video`); without it the
    # sidecar fails codec open() with EACCES. `render`: dri/card perms for
    # the same path under trixie's tighter udev defaults.
    groups: [video, render]
    ssh_authorized_keys:
      # Substituted with the operator's public key by stage_sd_card.sh's
      # --ssh-key handling below (quoted so an UNsubstituted placeholder is
      # a literal string cloud-init rejects on the ssh validator — the rest
      # of user-data still runs, so the device is console-recoverable).
      - "{{SSH_AUTHORIZED_KEYS}}"

# Phase 4e-a 2026-05-15: NO apt at first boot -- factory-fresh promise
# means the Pi must boot offline. The 6 packages this used to install
# (zstd, python3-venv, hostapd, dnsmasq, iptables, v4l-utils) are ALL
# already pre-baked into the image via pi-gen's 00-packages list. The
# old `package_update: true + packages: [...]` triggered apt-update on
# first boot, which fails without network and blocks runcmd entirely
# (install.sh never runs, AP never comes up). See
# qa/captures/parity-phase4e-cloud-init-network-config-2026-05-15.md.
package_update: false

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

# When --wifi-profiles is set, splice the bootcmd block BEFORE the
# `runcmd:` line so the keyfile install runs in cloud-init-local
# (pre-NM) rather than cloud-final (post-NM, which is too late for
# the first NM scan). The block was assembled into WIFI_BOOTCMD_BLOCK
# earlier; this is the single insertion point.
#
# Portability: BSD awk (macOS default) rejects multi-line literal
# strings via `-v`, so we stage the block to a temp file and have
# awk `getline` it. Works on BSD + GNU awk.
if [ -n "$WIFI_BOOTCMD_BLOCK" ]; then
    WIFI_BLOCK_FILE="$BOOTFS/.wifi-bootcmd-block.tmp"
    printf '%s\n' "$WIFI_BOOTCMD_BLOCK" > "$WIFI_BLOCK_FILE"
    USER_DATA_TMP="$BOOTFS/user-data.tmp"
    awk -v block_file="$WIFI_BLOCK_FILE" '
        /^runcmd:/ && !inserted {
            while ((getline line < block_file) > 0) print line
            close(block_file)
            inserted = 1
        }
        { print }
    ' "$BOOTFS/user-data" > "$USER_DATA_TMP"
    mv "$USER_DATA_TMP" "$BOOTFS/user-data"
    rm -f "$WIFI_BLOCK_FILE"
fi

# Seed openmarquee's SSH authorized key into user-data. openmarquee is
# key-only (ssh_pwauth:false) so WITHOUT this the shipped card is
# SSH-unreachable. python (not sed) so the key's / + = chars can't collide
# with a substitution delimiter. Mirrors build-image.sh's substitution.
if [ -n "$SSH_KEY_PATH" ]; then
    SSH_KEY_CONTENT="$(cat "$SSH_KEY_PATH")"
    python3 - "$BOOTFS/user-data" "$SSH_KEY_CONTENT" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
with open(path) as f:
    body = f.read()
if "{{SSH_AUTHORIZED_KEYS}}" not in body:
    sys.exit("stage_sd_card: {{SSH_AUTHORIZED_KEYS}} placeholder missing from user-data")
body = body.replace("{{SSH_AUTHORIZED_KEYS}}", key.strip())
with open(path, "w") as f:
    f.write(body)
PY
    echo "==> seeded openmarquee SSH authorized key from $SSH_KEY_PATH"
else
    echo "WARNING: no --ssh-key given and no default key found." >&2
    echo "         The card boots, but SSH-in as openmarquee is IMPOSSIBLE" >&2
    echo "         (key-only auth). Re-run with --ssh-key ~/.ssh/id_ed25519.pub." >&2
    echo "         The literal {{SSH_AUTHORIZED_KEYS}} placeholder stays; cloud-init" >&2
    echo "         rejects it on the ssh validator but still runs the rest." >&2
fi

if [ -n "$WIFI_PROFILES_DIR" ]; then
    echo "==> wrote user-data (cloud-init bootcmd wifi-install + runcmd)"
else
    echo "==> wrote user-data (cloud-init runcmd)"
fi

# --- Done -------------------------------------------------------------------

cat <<EOF

SD card staged at $BOOTFS:
    openmarquee-bundle.tar.zst   ($BUNDLE_SIZE bytes)
    user-data                    (cloud-init runcmd${WIFI_PROFILES_DIR:+ + bootcmd wifi-install})
    meta-data                    (instance-id=$INSTANCE_ID)
    network-config               (eth DHCP, no wifi pre-config)$([ -n "$WIFI_PROFILES_DIR" ] && printf '\n    openmarquee-wifi/%s             (%s wifi profile(s) installed at boot)' "*.nmconnection" "$NMCONN_COUNT")

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
