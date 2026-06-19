# blendr — Phase 0 (KMS-present milestone)

Minimal Rust binary that proves we can drive HDMI from Rust on the
Pi Zero 2 W (vc4 display + V3D GPU) before integrating any GStreamer
decode path.

Phase 0 scope: probe the correct DRM node, acquire master, bring up
EGL + GBM + GLES2, run a 60 Hz vsync page-flip loop drawing either a
hue-cycling solid color (Step A) or a static checkerboard texture
(Step B). Restore the saved CRTC mode on exit so the screen does not
stay black.

## Run on the Pi

```
# From a non-graphical TTY (Ctrl-Alt-F2; getty must NOT hold DRM master):
sudo /usr/local/bin/blendr --duration-sec 30 --step solid
sudo /usr/local/bin/blendr --duration-sec 30 --step checker
```

`--card-override /dev/dri/cardN` bypasses the auto-probe (debug only).

## Cross-build (macOS host → aarch64 target)

```
fresh/blendr/build.sh
```

Output binary: `/tmp/blendr-build/blendr/target/aarch64-unknown-linux-gnu/release/blendr`

`scp` to the Pi, `chmod +x`, run as `sudo` from a non-graphical TTY.

## Layout

- `src/main.rs` — arg parse + wiring + top-level run.
- `src/drm_probe.rs` — `/dev/dri/card*` enumeration; pick the vc4 display node.
- `src/kms.rs` — connector/encoder/CRTC pick; mode save+restore; page-flip loop.
- `src/egl_gbm.rs` — GBM device + surface; EGL display/context/surface bring-up.
- `src/gles_present.rs` — glow ctx; checker texture; full-screen-quad draw.
- `src/signals.rs` — SIGINT/SIGTERM async-signal-safe exit-flag.

## NOT a derivative of `code2/renderer/`

This is a fresh crate. The OLD renderer (`code2/renderer/`) is
referenced READ-ONLY for proven shape patterns (Card newtype,
with_egl_session bring-up, commit_fb drain) — no code is copied or
shared.
