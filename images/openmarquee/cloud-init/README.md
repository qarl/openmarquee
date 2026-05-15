# cloud-init for openMarquee

NoCloud datasource template for first-boot config. Pi-gen drops these
files into `/boot/firmware/` on the SD image; on first boot cloud-init
reads them and runs the directives.

| File | Role |
| --- | --- |
| `user-data` | Main directive list (users, ssh, bootcmd, runcmd). Contains `{{SSH_AUTHORIZED_KEYS}}` placeholder — substitute before flashing. |
| `meta-data` | Required-but-trivial instance metadata. cloud-init refuses to read user-data without it. |

## Placeholder substitution

`{{SSH_AUTHORIZED_KEYS}}` must be replaced with the operator's SSH
public key before flashing. The B.6 `scripts/build-image.sh` wrapper
will accept `--ssh-key <path>` and do this substitution; until then,
substitute by hand:

```bash
SSH_KEY=$(cat ~/.ssh/id_ed25519.pub)
sed "s|{{SSH_AUTHORIZED_KEYS}}|${SSH_KEY}|" \
    images/openmarquee/cloud-init/user-data > /tmp/user-data
# then copy /tmp/user-data to /boot/firmware/user-data on the SD card
```

Leaving the placeholder unsubstituted will fail cloud-init noisily —
`{{SSH_AUTHORIZED_KEYS}}` isn't a valid OpenSSH key string — so a
forgotten substitution doesn't silently brick the device with no SSH
access. (It does still boot, but you'd need to drop in via the local
serial console to fix it.)

## Why bootcmd vs runcmd

- `bootcmd` runs very early — before networking, SSH, or any
  user-reachable target. Right place to set the hostname so mDNS
  advertises the correct name before any SSH client tries to connect.
- `runcmd` runs after networking. Right place for `systemctl enable
  ssh` (the Pi OS Lite trixie gotcha — SSH is NOT enabled by default
  even when cloud-init is present) and for `install.sh` which expects
  the network stack to be up so it can pip-install from the local
  venv requirements.

## What's NOT here

- The actual `install.sh` referenced in `runcmd`. Lives in
  `scripts/install.sh` (B.3 leg, not yet committed).
- The first-boot oneshot that generates AP password + QR code.
  Lives in `system/openmarquee-firstboot.service` (B.4 leg, not yet
  committed). `install.sh` will be responsible for triggering it.
- Network config (`network-config` file). Default cloud-init network
  config is fine for our setup (DHCP on whatever interfaces are up).
  Station-mode WiFi join is NOT supported via cloud-init's `wifis:`
  block on this image -- cloud-init writes eni format which NM
  silently ignores (`qa/captures/cloud-init-wifis-investigation-
  2026-05-15.md`). Use `scripts/burn_sd_card.sh --wifi-ssid`
  (Phase 4e-b NM keyfile drop, commit `9c7ae78`) or the post-AP
  welcome-UI flow (`backend/openmarquee/wifi_station.py`).
