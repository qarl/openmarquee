# CI runner trade-offs

Living doc for runner-choice rationale + tracked alternatives. Append
when a CI job's runner choice is non-obvious.

## renderer job: macos-latest (Batch 16.4, 2026-05-11)

`.github/workflows/ci.yml`'s `renderer:` job runs on `macos-latest`,
not the conventional `ubuntu-latest`.

**Why:**
`renderer/Cargo.toml` declares the Linux-only hardware deps
(`drm`, `gbm`, `khronos-egl`, `libloading`, `glow`, `libc`, `drm-ffi`)
under `[target.'cfg(target_os = "linux")'.dependencies]`. On macOS
those deps cfg-out automatically -- `cargo test` builds and runs only
the host-portable surfaces (`parse_color`, `hdmi_logic`, `content.rs`,
font / motion / layout logic). No system-package install needed.

On `ubuntu-latest` the same `cargo test` invocation would attempt to
build the linux-targeted deps and fail at link time without an
`apt-get install libdrm-dev libgbm-dev libegl1-mesa-dev libudev-dev`
step. That apt-get step bit-rots: a Mesa / libdrm version bump
silently breaks CI, and the fix has nothing to do with the renderer
itself.

**Trade-off:**
macOS runners cover *correctness of host-portable Rust* (parsers,
layout, content I/O, transition logic) but NOT *Linux-only Rust paths*
(EGL/GBM bring-up, DRM atomic-mode-set, hub75 wiring). Those paths
exercise via on-Pi smoke and the golden-master baseline rather than
CI.

**Tracked alternative:**
*Consider a parallel `renderer-linux` job (ubuntu-latest + apt-get
install) if a Linux-only Rust regression ever slips through to Pi.*
The cost is one slow CI lane + a deps-install step; the benefit is
catching link-time / cfg-conditional regressions before the on-Pi
smoke. Not paying that cost today because the Pi smoke gate already
covers this lane and dev iteration speed is the sweep #8 theme.

## Future entries

Add a new `## <job-name>: <runner> (Batch X.Y, YYYY-MM-DD)` section
whenever a CI runner choice deviates from the convention or has a
non-obvious trade-off worth documenting.
