# openMarquee Pi OS image (B.1)

A pi-gen recipe that produces a flashable SD-card image for the
openMarquee captive-portal-first-boot device flow.

## What this directory contains

| Path | Role |
| --- | --- |
| `pi-gen.config` | Environment vars sourced by pi-gen's `build.sh` (rename to `config` when applying). |
| `stage-openmarquee/` | Custom pi-gen stage that runs after the standard `lite` stages. |
| `stage-openmarquee/00-install-packages/00-packages` | apt packages needed by the openMarquee runtime. |
| `stage-openmarquee/EXPORT_IMAGE` | Marker file (pi-gen sources as shell) — tells pi-gen to emit the .img at the end of this stage; also sets `IMG_SUFFIX`. |
| `cloud-init/user-data` | NoCloud first-boot config (B.2). Contains `{{SSH_AUTHORIZED_KEYS}}` placeholder to substitute. |
| `cloud-init/meta-data` | Minimal cloud-init metadata (instance-id + seed hostname). |
| `stage-openmarquee/prerun.sh` | Boilerplate: copy previous stage's rootfs into this stage. |

## Two operator onboarding flows

The Phase B image supports two onboarding paths. Phase C closure ensures
both actually work on a freshly-flashed device.

### Captive-portal flow (no pre-flash wifi creds)

Operator powers on a Pi without setting wifi at flash time:

1. `system/openmarquee-firstboot.service` fires on first boot. Generates
   a per-device WPA2 passphrase + MAC-derived SSID
   (`openMarquee-<HEX>`), writes `/var/openmarquee/wifi.json` (0600),
   templates `/etc/hostapd/hostapd.conf`, templates SSID/password into
   `/opt/openmarquee/ui/welcome.html` (with QR code if `qrencode` is
   installed).
2. AP comes up; phone joins via the QR scan or by typing the displayed
   passphrase.
3. Phone hits `http://192.168.4.1/` → welcome.html → `set-password.html`
   (Phase A) → operator types admin password.
4. Operator types their home wifi creds via the UI; backend writes
   them into `settings.json`.

### Pre-flash flow (operator has wifi creds at build time)

Operator either uses Pi Imager's "set wifi" interface, or sets
`WPA_ESSID` + `WPA_PASSWORD` in `pi-gen.config` before running
`scripts/build-image.sh`:

1. Pi-gen bakes `/etc/wpa_supplicant/wpa_supplicant.conf` into the
   image (with operator-chosen SSID + PSK).
2. On first boot, `openmarquee-firstboot.service` runs `chmod 644
   /etc/wpa_supplicant/wpa_supplicant.conf` so the openmarquee service
   user can read it (Phase C closure -- pi-gen ships the file 600
   root:root by default).
3. The device joins the operator's wifi via wpa_supplicant.
4. On first GET `/api/settings`, `backend/openmarquee/wifi_prefill.py`
   shells out to `iwgetid -r` (from `wireless-tools`, also Phase C in
   pi-gen's 00-packages) to confirm the active SSID, then parses the
   wpa_supplicant.conf to extract the PSK, and folds both into
   `settings.json`. UI shows them pre-filled -- operator just
   confirms instead of re-typing.

Both flows converge on the same end-state: wifi creds in settings.json,
admin password set, device on operator's home wifi + AP up for
re-onboarding if needed.

## How to build a handover SD card (the 3-step flow)

⚠️ **`build-image.sh` produces a BASE image, not a flashable-standalone one.**
The pi-gen image bakes the OS + packages + boot-config (`cma=320M`,
`gpu_mem=128`) + the plymouth splash — but **NOT the app code**. The
openMarquee code (`/opt/openmarquee`) is delivered by a bundle that
`stage_sd_card.sh` overlays onto the card, along with the
extract-then-install `user-data`. Flashing the base image *standalone*
boots to a device with no app (it will say so on the console + drop a
`/var/lib/openmarquee/.provision-error` marker — see `cloud-init/user-data`).

The complete handover flow is three steps, **in order**:

```bash
# 1. Base image (OS + packages + boot-config + splash), pi-gen in Docker.
bash scripts/build-image.sh --ssh-key ~/.ssh/id_ed25519.pub
#    → /tmp/openmarquee-pi-image-<date>.img.xz

# 2. Fresh code bundle. REBUILD ui + renderer FIRST or build_sd_bundle
#    fails loud (it refuses to ship a stale ui/dist or renderer binary):
(cd ui && npm run build)
bash scripts/renderer_cross_build.sh
bash scripts/build_sd_bundle.sh
#    → dist/openmarquee-sd-bundle.tar.zst

# 3. Flash the base image to the SD, then overlay the bundle + cloud-init:
bash scripts/stage_sd_card.sh /Volumes/bootfs      # (or the card's bootfs mount)
```

Only after step 3 is the card a complete, self-provisioning openMarquee
sign. (First boot extracts the bundle to `/opt/openmarquee` and runs
`install.sh`.)

Phase B legs landed:
- B.1: pi-gen config (this directory)
- B.2: cloud-init user-data (cloud-init/)
- B.3: scripts/install.sh on-device provisioning
- B.4: system/openmarquee-firstboot.{service,sh} per-device AP password
- B.5: install.sh re-runs firstboot.sh on redeploy (welcome.html re-template)
- B.6: scripts/build-image.sh + scripts/flash-sd.sh wrappers (this leg)

## How to build an image manually (without scripts/build-image.sh)

You can drive pi-gen directly if needed:

```bash
# 1. Clone pi-gen alongside the openMarquee repo
git clone --branch arm64 https://github.com/RPi-Distro/pi-gen.git /tmp/pi-gen
cd /tmp/pi-gen

# 2. Copy our config + custom stage into the checkout
cp $OPENMARQUEE_ROOT/images/openmarquee/pi-gen.config ./config
cp -r $OPENMARQUEE_ROOT/images/openmarquee/stage-openmarquee ./

# 3. Skip the desktop stages (X11 / LXDE / Recommended Software)
touch ./stage3/SKIP ./stage4/SKIP ./stage5/SKIP
touch ./stage3/SKIP_IMAGES ./stage4/SKIP_IMAGES ./stage5/SKIP_IMAGES

# 4. Run the build (Docker required; pi-gen handles its own deps).
sudo ./build-docker.sh

# 5. Image lands in ./deploy/ as <date>-openmarquee-trixie-arm64-lite.img.xz
```

## Why these packages

- **hostapd + dnsmasq + iptables**: captive-portal AP + DHCP +
  redirect-to-welcome.html. See `system/README.md` for the ap0
  topology.
- **python3 + python3-pip + python3-venv**: backend runtime. `install.sh`
  (B.3) creates `/opt/openmarquee/venv/` with `pip install -e .`.
- **ffmpeg**: video transcode pipeline (UI uploads MP4 → backend
  re-encodes to a renderer-friendly format).
- **fonts-dejavu**: text-slide font fallback when the operator hasn't
  uploaded a custom TTF.
- **qrencode**: B.4 first-boot oneshot uses this to generate the AP
  password QR code. (Alternative: `qrcode` Python lib; we use the CLI
  here so the oneshot can run before the venv is ready.)
- **git + rsync**: developer-mode redeploy from a workstation (per
  `scripts/deploy.sh`).
- **cloud-init**: drives the first-boot config from a user-data template
  (B.2). Pi OS Lite trixie does NOT install cloud-init by default; the
  package must be present here for B.2's runcmd to fire.
- **wireless-regdb + iw**: WiFi region/regulatory + interface management.
  Required for `iw dev wlan0 interface add ap0` (the ap0 bring-up). The
  old `crda` userspace daemon is folded into the kernel on trixie, so
  it's intentionally omitted.
- **wpasupplicant**: WiFi association supplicant. Pi OS Lite trixie's
  base image ships NetworkManager as the network stack on this
  release; NM uses wpa_supplicant as its association backend under
  the hood. openmarquee does NOT manage wpa_supplicant directly --
  station-mode WiFi runs through nmcli via `backend/openmarquee/wifi_station.py`
  (post-AP-configure flow) and via the NM keyfile drop in Phase 4e-b
  (`scripts/burn_sd_card.sh --wifi-ssid` pre-config flow). The ap0
  interface used for hostapd's AP mode is created BEFORE NM starts
  (`system/openmarquee-ap0.service` ordering); NM treats ap0 as
  unmanaged and leaves it to hostapd.
- **zstd**: bundle decompression at first boot (cloud-init runcmd
  pipes `zstd -d -c openmarquee-bundle.tar.zst | tar -xf -`).
- **v4l-utils**: H.264 decoder out-of-band debugging (`v4l2-ctl`).
  Not load-bearing for the rendering path (Rust uses ioctls directly)
  but field-debugging VideoSlide problems on a Pi without v4l2-ctl
  is much harder.

## Why these specific RELEASE / TARGET_ARCH choices

- **RELEASE='trixie'**: Debian 13. Pi argon2id measurements (project
  memo) are pinned to trixie's argon2-cffi build. Earlier bookworm-line
  works but ships a different argon2 lib that we haven't tuned for.
- **TARGET_ARCH='arm64'**: Pi Zero 2 W (BCM2710A1) is ARMv8. Pi 4 / 5
  also 64-bit. The original Pi Zero (ARMv6) is unsupported — single
  HDMI 1080p target rules it out anyway.

## Validation

`backend/tests/test_pigen_config.py` parses the config + package list
and asserts structural invariants. Run as part of pytest.
