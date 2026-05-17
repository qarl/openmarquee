# openMarquee

**Escape vendor lock-in. Run your sign yourself.**

Every commercial LED sign ships locked to its vendor's software —
usually Windows-only, often abandonware, sometimes a $10–30/month
cloud subscription with your content held hostage. openMarquee is an
open-source, self-contained controller you flash onto a Raspberry Pi
that lets you drive the same hardware from any phone, with no app, no
account, no internet, no subscription.

The device boots into its own WiFi network. You connect your phone, a
captive portal opens in your browser, and you upload videos, images,
and text slides. Playback runs on the device; your content stays on
your SD card.

## Status

**v0.5.0-beta** (commit 7457068, 2026-05-17). Release-candidate work
in progress; first tagged GitHub release lands after the §11 soak
gate fires and the operator quickstart docs land (sub-slices 4b/c/d
+ slice 5 per
[`qa/captures/phase-e-release-prep-recon-2026-05-17.md`](qa/captures/phase-e-release-prep-recon-2026-05-17.md)).

What works at HEAD:

- Raspberry Pi Zero 2 W primary target with **HDMI output** to any
  TV / monitor / HDMI-input sign.
- **First-boot captive portal**: device creates its own WiFi
  network on flash; operator connects from a phone, sets a password,
  configures playlist. Per-device AP password generated at firstboot
  (no shared default across flashed images).
- **Optional WiFi-during-flash prefill**: ship the operator's home
  WiFi credentials into the image at flash time and the device boots
  straight onto their network, skipping the captive-portal step.
- **Strict-30 fps shipping gate locked**: the Rust shader renderer's
  per-frame budget is monitored as `paint_us_p99` and gated at ≤ 33 ms
  over a rolling 10-minute window for the §11 acceptance soak.
- **WebRTC phone-camera takeover**: hold a phone-to-sign live stream
  from the device's Stream tab.
- **Live editor** with text-over-video and text-over-image compositing,
  motion effects (breathe, pulse, bounce, shake, blink, ticker), and
  per-layer blend modes (normal, screen, multiply, overlay).

## Architecture

A Raspberry Pi Zero 2 W runs **Python FastAPI** for the auth /
playlist / IPC orchestration backend, a **Rust sidecar binary** for
the HDMI render path (EGL + GLES2 + dmabuf single-pass shader
compositor over DRM/KMS atomic, with V4L2 H.264 decode for video
slides), and a **vanilla-JS browser dashboard** served from the
device for the captive-portal editor (no framework, no bundler magic
— just `esbuild` + `vitest`). Content lives as JSON + asset files on
the SD card; there is no database. Heavy lifting like video decode
and scaling happens client-side via `ffmpeg.wasm` so the device
itself stays cheap. For owners who want remote access, installing
[Tailscale](https://tailscale.com) on the device provides secure
access from anywhere at zero ongoing cost.

## Quick links

- **Install / first boot:** flashable image + on-Pi install paths
  ship in the next doc slice (TODO — quickstart doc lands next).
- **Spec-of-record:**
  [`docs/renderer-rewrite-requirements.md`](docs/renderer-rewrite-requirements.md)
  (read this before changing render contracts).
- **Changelog:** [`CHANGELOG.md`](CHANGELOG.md).
- **Contributor docs:** [`docs/`](docs/).
- **Public site:** [openmarquee.com](https://openmarquee.com).

## What's NOT in v0.5.0-beta

Honest list of gaps so nobody is surprised:

- **HUB75 LED matrix output** — scaffolding present
  (`backend/openmarquee/rendering/hub75.py`, 288 LOC), hardware-wire
  path stubbed per spec §11. Functional on HDMI mode only at this
  release.
- **WS2812B LED strip output** — same shape as HUB75: scaffolding at
  `backend/openmarquee/rendering/ws2812b.py` (211 LOC), hardware-wire
  path stubbed.
- **Composite (NTSC/PAL) video output** — not implemented at this
  release. Post-v1 per the project plan.
- **AI background generation** — runtime / on-demand generation
  deferred. Pre-generated background images ship in the seed
  content; CivitAI tooling for offline regeneration lives in
  `www/scripts/civitai-bg-gen.py`.
- **Flock management UI** — multi-device cross-sync is scoped and
  partially implemented (peer discovery, manifest exchange, pull
  worker exist; operator-facing flock-onboarding UI does not). Per
  `docs/phase-b-flock-scope.md`.
- **Hardware compatibility matrix** — Pi Zero 2 W is the verified
  primary target; Pi 4 / Pi 5 cross-hardware validation is Phase F
  (deferred).
- **First tagged GitHub release artifact** — the SD-card image build
  (`scripts/build-image.sh`) is functional but a tagged release
  with sha256-verified flashable image ships in Phase E slice 5.

## Repository layout

- [`backend/`](backend/) — FastAPI app that runs on the device (auth,
  playlist, IPC orchestration of the renderer sidecar).
- [`renderer/`](renderer/) — Rust sidecar binary for the HDMI render
  path. Cross-compiles to aarch64-linux for the Pi.
- [`ui/`](ui/) — browser-based dashboard (captive-portal editor +
  playlist + settings + Stream).
- [`system/`](system/) — device OS config (hostapd, dnsmasq, systemd
  units, captive-portal glue, firstboot oneshot).
- [`scripts/`](scripts/) — build / deploy / soak harnesses.
- [`docs/`](docs/) — contributor-facing design + spec docs.
- [`qa/captures/`](qa/captures/) — phase-level audit notes and recon
  documents.

## License

GPLv3 — see [`LICENSE`](LICENSE).

---

> The project was briefly called "OpenSign" early on, before the name
> was locked in as **openMarquee** (domain:
> [openmarquee.com](https://openmarquee.com)). "OpenSign" collided
> with the e-signature platform OpenSignLabs and with the digital
> signage product at opensign.us.
