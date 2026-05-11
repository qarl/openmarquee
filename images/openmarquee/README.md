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

What's NOT here (yet):
- **B.3 install.sh** — the real provisioning. Drops the systemd unit,
  builds the venv, lays down hostapd/dnsmasq configs from `wifi.json`.
- **B.4 first-boot oneshot** — generates the AP password + QR code on
  first boot, templates into welcome.html, closes sweep #5 #2.
- **B.5 welcome.html flow integration** — AP password + QR code shown
  on the captive-portal landing screen.
- **B.6 build artifact + flash script** — `scripts/build-image.sh` and
  `scripts/flash-sd.sh` wrappers.

## How to build an image (when B.6 lands)

```bash
bash scripts/build-image.sh
# → drops /tmp/openmarquee-pi-image-<date>.img
```

## How to build an image manually (until B.6 lands)

Until B.6 is in place, you can drive pi-gen directly:

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
- **wpasupplicant**: station-mode WiFi join (wlan0). NetworkManager is
  intentionally absent — we manage wpa_supplicant directly.

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
