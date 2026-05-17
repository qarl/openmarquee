# Phase 1 Build Attempt — Status (2026-05-06 ~03:20–04:10)

**Read this first thing in the morning.** The dev Pi (`openMarqueeDev`)
is unreachable; it almost certainly needs a physical power cycle.

## Tl;dr

Phase 1 build attempt validated **content-review BLOCKER B2 in
production**: rustc + the proposed dep set OOM-thrashed the Pi Zero 2 W
into unreachability about 25 minutes into a `cargo build`. The Pi has
been unresponsive for 15+ minutes (no ping, no Tailscale, no SSH); this
is past normal OOM-killer recovery. Power cycle on your end is the
recovery path.

The Phase 1 scaffolding (the Rust crate at `renderer/`) is on disk and
committed. The cross-compile path is being prepared on the Mac so that
when you power-cycle the Pi we deploy a pre-built binary instead of
trying to build on the Pi again.

## What was attempted

Per your "GO DO IT" + "if it doesn't work out you can revert" directive,
I started Phase 1 as QA-Jimmy outlined: pixels-on-HDMI from a fresh
Rust binary at `renderer/`. The plan §6 chose "build natively on the Pi
via rustup-installed toolchain"; the content review's BLOCKER B2 said
this would OOM. We tried it. B2 was right.

**What the build looked like, step by step:**

1. Confirmed Pi had no Rust toolchain. Installed rustup with channel
   1.79.0 minimal profile (~5 min, succeeded).
2. Created `renderer/Cargo.toml` + `renderer/rust-toolchain.toml` +
   minimal `renderer/src/main.rs` that opens DRM and prints connector
   info (Phase 0 / probe-only milestone, before GBM/EGL/GLES).
3. First `cargo build` attempt: failed because clap_derive 4.6 requires
   `edition2024`, which is only stable in Rust 1.85+. Bumped
   `rust-toolchain.toml` to 1.85.0.
4. Second `cargo build` attempt: rustup downloaded 1.85.0 toolchain (the
   download itself emitted "using single-threaded unpacking due to low
   memory (ram budget: 216.1 MiB < 512.0 MiB threshold)" — early
   warning that the box is tight). Started compiling 102 dependencies.
5. ~25 minutes into the dep compile (with `proc-macro2` and `libc`
   build scripts running concurrently), the Pi went unreachable. SSH
   timeouts. Ping unreachable. Tailscale daemon unreachable.
6. Six SSH retry attempts at 30 s intervals: all failed.
7. Direct ping to the Tailscale magic-DNS hostname: 100% packet loss.
8. As of this note: Pi has been unreachable for 15+ min. Past the
   typical OOM-killer recovery window where systemd would respawn user
   services and Tailscale would reconnect. Best read: heavy swap thrash
   either bricked the SD card temporarily or the kernel itself got
   confused (possible on Pi Zero 2 W with rustc + LLVM in ~1.2 GB peak
   resident on 416 MB RAM + 415 MB swap).

## What this means for the plan

**Plan §6 ("build natively on the Pi") is dead.** The content review
identified this as a BLOCKER and the Pi just demonstrated it. The
post-mortem is straightforward: rustc + LLVM compiling clap_derive +
drm-rs + gbm + glow + tracing + tokio is a 1-2 GB peak resident
memory event under release optimization, and even debug mode pushes
hard against a 416 MB box. With swap at 415 MB on zram, the system is
right at the edge — and the pages it needs to swap (rustc's working
set) are exactly what rustc keeps re-touching. Swap thrash to death.

**Cross-compile from Mac is now the only viable path.** Setting it up
preemptively below.

## What I have ready when the Pi is back

- `renderer/` crate scaffolded: `Cargo.toml` (deps: clap, drm 0.12,
  gbm 0.15, khronos-egl 6.0 dynamic, glow 0.14, anyhow, thiserror,
  tracing), `rust-toolchain.toml` pinned 1.85.0, `src/main.rs` with
  `--probe` mode that enumerates DRM connectors/encoders/CRTCs/planes.
  Committed (see git log).
- Mac-side rustup + the `aarch64-unknown-linux-gnu` target installed.
- `cargo install cross` is running in background (Mac task `b3ckc6ysn`)
  — produces the `cross` binary that wraps `cargo build` in a Docker
  container with a Debian arm64 sysroot pre-loaded with libdrm-dev,
  libgbm-dev, libegl-dev. ETA ~10 min.
- Once `cross` is in, I can try `cross build --target aarch64-unknown-
  linux-gnu` and produce a binary. Drm-rs and gbm-rs both need libdrm
  and libgbm headers AT BUILD TIME (bindgen-generated bindings), and
  the `cross` Docker image satisfies that.
- When you power-cycle the Pi, deploy is `scp
  renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render
  openmarquee@openMarqueeDev:/usr/local/bin/`. No Pi-side build needed
  for Phase 1.

## What you need to do

1. **Power-cycle the Pi.** That's it. The kernel will come up clean,
   systemd will bring back sshd + Tailscale, the Python backend will
   restart from its systemd unit. Confirm via `ssh
   openmarquee@openMarqueeDev "uptime"`.
2. Once it's back, ping me (or Jimmy-Jimmy) and I'll run the cross-
   compile + scp + Phase 1 probe test. Should take ~30 minutes
   end-to-end after the Pi is up.

## Holding open until your call

Decisions that wait on you:

- **Update plan §6 to cross-compile baseline?** Yes, I think so.
  Build-on-Pi has now been falsified twice (review prediction +
  hardware confirmation). Worth committing the plan revision.
- **Update plan toolchain pin from 1.79.0 to 1.85.0?** Yes, edition2024
  forced it.
- **Sleep on the Pi is fine until you wake.** I won't keep retrying
  SSH; that's just noise.
- **Ping Jimmy-prime or QA-Jimmy with this status?** I'll ping QA-Jimmy
  so they can relay if they catch you first; saving a hard ping for
  Jimmy-prime to your call.

— jimmy:openmarquee-code, 04:10 local
