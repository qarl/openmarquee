# SD-burn: flashing a fresh openMarquee Pi

End-to-end "burn a card, boot Pi, connect to the AP" flow. Targets Pi
Zero 2 W on Pi OS Lite arm64 (Bookworm). Scope: AP-mode only — the Pi
serves its captive-portal welcome UI over its own wifi. Joining a home
network is a separate flow (deliberately out of scope here).

## TL;DR (Mac, single command)

```
bash scripts/build_sd_bundle.sh
bash scripts/burn_sd_card.sh /dev/diskN
```

`burn_sd_card.sh` validates the target is a removable disk, prompts
you to type `diskN` to confirm, fetches + caches the latest Pi OS
Lite arm64 image, flashes via `dd` against the raw `/dev/rdiskN`
device (~5x faster), waits for `bootfs` auto-mount, calls
`stage_sd_card.sh` to drop the bundle + cloud-init, ejects. About
5-8 minutes wall time end-to-end on USB 3 (mostly the dd phase).

Find the right `/dev/diskN`: `diskutil list external removable`. The
script refuses any target flagged Internal by `diskutil info`, so
you can't accidentally wipe your Mac's SSD even with a typo.

See "Two-step flow (GUI flash)" below if you prefer Raspberry Pi
Imager's GUI for the flash step.

## Prerequisites

On the Mac (or Linux laptop) you're staging from:
- `zstd` — `brew install zstd` on macOS, `apt-get install zstd` on Debian
- `xz` — `brew install xz` on macOS (only needed for the one-command
  `burn_sd_card.sh` flow; the GUI flow doesn't use it)
- `rsync` — usually preinstalled
- `pip` (Python 3.13+) — only if you're vendoring wheels (`--wheels`)
- The cross-built Rust renderer binary, if you want the sidecar opt-in.
  Run `bash scripts/renderer_cross_build.sh` ahead of time; the bundle
  picks it up from `renderer/target/aarch64-unknown-linux-gnu/release/`.
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/) — only
  needed for the GUI fallback flow below.

On the Pi side, nothing — that's the point.

## Two-step flow (GUI flash)

For operators who prefer the Raspberry Pi Imager GUI for the flash
step. The single-command path above is the recommended default.

1. **Flash Pi OS Lite arm64 to the SD card** using Raspberry Pi Imager.
   - Choose `Raspberry Pi OS Lite (64-bit)`.
   - **Settings (gear icon) — skip wifi entirely.** The cloud-init
     `network-config` this flow drops onto bootfs will replace whatever
     Imager pre-configures. Do not pre-set a hostname either; the Pi
     generates its own `MySignXXX` on first boot.
   - Imager's user setup (username/password) IS used — it creates the
     login account.
   - **Paste your SSH public key** in Imager's advanced settings.
     `ssh_pwauth: false` in the cloud-init we drop disables ssh password
     auth, so a public-key entry is the ONLY way to recover the Pi
     remotely if `install.sh` fails. Without it, recovery requires an
     HDMI monitor + USB keyboard physically attached.

2. **Re-insert the SD card** so the `bootfs` partition automounts.
   - macOS: `/Volumes/bootfs`
   - Linux: `/run/media/$USER/bootfs` (or wherever your DE mounts it)

3. **Build the bundle** (once per code change you want on the device):
   ```
   bash scripts/build_sd_bundle.sh
   ```
   Output: `dist/openmarquee-sd-bundle.tar.zst` (~75 MiB without
   wheels, ~200 MiB with). The script refuses to build if it finds
   `.env`, `.pem`, `id_rsa`, or other credential-shaped files in the
   source tree — see the script's secret-scanner section if it fires.

   To vendor Python wheels for offline install on the Pi (slower
   build, no internet needed at first boot):
   ```
   bash scripts/build_sd_bundle.sh  # wheels by default
   ```
   To skip wheels (faster; Pi will pip-install on first boot — needs
   ethernet cable plugged in):
   ```
   bash scripts/build_sd_bundle.sh --no-wheels
   ```

4. **Stage the SD card**:
   ```
   bash scripts/stage_sd_card.sh /Volumes/bootfs
   ```
   This drops:
   - `openmarquee-bundle.tar.zst` (the tarball)
   - `user-data` (cloud-init runcmd)
   - `meta-data` (instance-id; required even when empty)
   - `network-config` (eth DHCP only, no wifi pre-config)

   The script refuses to write if the mount path doesn't look like a
   Pi bootfs (no `cmdline.txt` / `config.txt`), so you won't
   accidentally trash `/Volumes/Untitled` if you point at the wrong
   disk.

5. **Eject + boot**:
   - macOS: `diskutil eject /Volumes/bootfs`
   - Linux: `umount /run/media/$USER/bootfs`
   - Insert SD into the Pi, power on.

6. **Wait ~2 minutes** for cloud-init to run. The flow:
   - cloud-init runs `runcmd`: extracts the bundle, runs `install.sh`
   - `install.sh` runs `openmarquee-firstboot.service`, which
     generates the per-device `MySignXXX` identifier + AP password,
     templates `hostapd.conf`, brings up `ap0`
   - `openmarquee-backend.service` starts, serving on `:80`

7. **Connect to the AP**:
   - SSID: `MySignXXX` (look for it in your laptop's wifi list)
   - Password: printed on the welcome UI; for the first connection
     you'll need it ahead-of-time from a console attached to the Pi,
     OR from `/var/openmarquee/wifi.json` if you can ssh in over
     ethernet (see Recovery below)
   - Once connected, the captive-portal redirect should bring up the
     welcome UI; if not, manually navigate to `http://10.0.0.1/`

## How `burn_sd_card.sh` works

The one-command path (`scripts/burn_sd_card.sh /dev/diskN`):

1. **Validates target** via `diskutil info -plist`. Refuses anything
   that isn't shape `/dev/diskN` (whole disk; not `/dev/disk4s1`).
   Refuses anything flagged `Internal: true` in the plist. Refuses
   anything not flagged `RemovableMediaOrExternalDevice` or
   `Ejectable`. Bails loudly if the device isn't present.

2. **Confirmation prompt.** Operator must type the exact identifier
   (e.g. `disk7`) to proceed. No `--force` flag. This is the
   wipes-wrong-disk guard.

3. **Image cache.** Pi OS Lite arm64 (`raspios_lite_arm64_latest`)
   downloads to `$OPENMARQUEE_BUILD_DIR/cache/pi-os-lite-arm64.img.xz`
   (or `~/Library/Caches/openmarquee/` when `OPENMARQUEE_BUILD_DIR`
   isn't set). Cached for 30 days. SHA256 verified against the
   Raspberry Pi Foundation's published `.sha256` sibling URL.

4. **Sudo once.** `sudo -v` at the start primes the credential cache;
   the dd + `diskutil unmountDisk` / `mountDisk` / `eject` calls
   reuse it. The bundle stage step does NOT run as root (would dirty
   file ownership inside the bundle tar).

5. **Unmount whole disk** with `sudo diskutil unmountDisk /dev/diskN`
   (not `diskutil unmount` per-volume).

6. **Flash** via `xz -dc <cache> | sudo dd of=/dev/rdiskN bs=4m`. The
   raw `rdiskN` device is ~5x faster than the buffered `diskN` on
   macOS. A Finder "disk not recognized" popup may appear mid-dd;
   it's harmless.

7. **Wait for bootfs.** macOS typically auto-mounts the FAT bootfs
   partition within 5-20 seconds. If it doesn't, force with
   `sudo diskutil mountDisk /dev/diskN`. 60-second timeout.

8. **Stage bundle** by calling `stage_sd_card.sh /Volumes/bootfs`.
   If staging fails, the SD is left mounted so you can re-run or
   inspect.

9. **Eject** via `sudo diskutil eject /dev/diskN`. SIGINT mid-flash
   triggers a cleanup eject + warns that the card is in an
   undefined state.

`scripts/burn_sd_card.sh --dry-run /dev/diskN` runs everything
through step 1+8 without invoking dd / diskutil destructive ops; the
output prints what it WOULD do. Good for verifying your target
identifier before the real burn.

## Recovery: install.sh failed mid-boot

If the AP doesn't come up after ~3 minutes, the install probably
errored. Check for the failure marker:

- Plug the Pi into ethernet. cloud-init enables sshd before running
  install.sh, so you should be able to:
  ```
  ssh openmarquee@mysign-init.local
  ```
  (`mysign-init` is the hostname cloud-init sets BEFORE
  `openmarquee-firstboot.service` rotates it to `MySignXXX`.)

- On the Pi, check:
  ```
  ls /var/openmarquee-install-failed         # marker present => install errored
  sudo journalctl -u cloud-final --no-pager  # cloud-init's runcmd log
  sudo journalctl -u openmarquee-backend     # backend log if install got that far
  sudo journalctl -u openmarquee-firstboot   # firstboot log
  ```

- To re-run install manually:
  ```
  sudo bash /opt/openmarquee/scripts/install.sh
  ```

## Rough edges (qarl will probably hit these)

- **macOS sudo for SD eject sometimes**: Disk Utility's eject works
  without sudo, but `diskutil eject` from CLI occasionally asks. If
  it does, ignore (file system unmounts cleanly enough either way).
- **`brew install zstd`** is the one tool not preinstalled on most
  Macs. The script fails loudly with a `command not found` if you
  skip it.
- **Imager's "advanced settings" wifi field**: leave EMPTY. If you
  pre-configure wifi via Imager, the cloud-init `network-config`
  this flow drops won't always cleanly override Imager's
  `wpa_supplicant.conf`. Easier to just not.
- **First-boot wait is long**: ~2 minutes is the typical case (`apt`
  installs zstd + python3-venv + hostapd + dnsmasq + iptables on
  first boot, ~80 MB download). If your internet is slow or you're
  not on ethernet, add 1-3 min.
- **AP password discovery is awkward without console access**: the
  welcome UI displays the password, but you can't see the welcome UI
  until you're on the AP. Either attach an HDMI monitor + USB
  keyboard for first boot, ssh over ethernet, or check
  `/var/openmarquee/wifi.json` over ethernet ssh.

## Updating an already-deployed Pi

This flow targets fresh SD cards. For redeploying code to an
already-provisioned Pi, use `scripts/deploy.sh openmarquee@<host>`
instead — it rsyncs and re-runs `install.sh` in idempotent mode.
