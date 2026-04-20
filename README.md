# openmarquee

**Free your sign from proprietary vendor lock-in.**

A self-contained, phone-controlled, no-subscription LED sign and digital-signage platform built on a Raspberry Pi Zero 2 W (or similar).

The device boots into its own WiFi network. You connect your phone, a captive portal opens in your browser, and you upload videos, images, and text slides. **No app to install. No account to create. No subscription. No internet required.**

## Two output modes, one codebase

- **HUB75 mode** — drives LED matrix panels directly via an Adafruit RGB Matrix Bonnet. For repurposing commercial LED signs that use standard HUB75 internally. (~$35 BOM.)
- **HDMI mode** — outputs video to any TV, monitor, or HDMI-input sign. (~$20 BOM.)

Same Linux image, same Python backend, same web UI. A config flag selects the output.

## How it works

The browser does the heavy lifting. Video files are decoded and scaled client-side using `ffmpeg.wasm`, so the device itself stays simple and cheap. The device runs FastAPI, stores content as files on an SD card, and plays them through either `ffmpeg` → framebuffer (HDMI) or the `hzeller/rpi-rgb-led-matrix` library (HUB75). Playlists and schedules are JSON. There is no database.

For owners who want remote access, installing Tailscale on the device provides secure access from anywhere at zero ongoing cost.

## Status

**Early.** No releases yet. Architecture is still settling and there's no working firmware image. See [`docs/`](docs/) for contributor-facing docs.

## Repository layout

- [`backend/`](backend/) — FastAPI app that runs on the device
- [`ui/`](ui/) — browser-based web UI (the captive-portal interface, with `ffmpeg.wasm`)
- [`system/`](system/) — device OS config (hostapd, dnsmasq, systemd units, captive-portal glue)
- [`docs/`](docs/) — contributor-facing docs (hardware, dev setup, architecture)

## License

GPLv3 — see [`LICENSE`](LICENSE).

---

> The project was briefly called "OpenSign" early on, before the name was locked in as **OpenMarquee** (domain: openmarquee.com). "OpenSign" collided with the e-signature platform OpenSignLabs and with the digital signage product at opensign.us.
