# Renderer Rewrite — Rust Implementation Plan

**Status:** core rewrite SHIPPED (DELETE-PIL purge, 2026-05-17,
commits 67cea75..adea339). The Python rendering subsystem is gone;
the Rust IPC sidecar at `renderer/` is the only production renderer.
Forward-looking sections of this doc (Step 8 HUB75/WS2812B reach,
Step 10 multi-renderer dispatch) describe work for a follow-up arc.
**Companion to:** `renderer-rewrite-requirements.md` (spec). Prior
Python plan is at `historical/renderer-rewrite-plan.md`.
**Grounded in:** `historical/renderer-rewrite-spike-data.md`
(measured, 2026-05-06).

> **Plan vs reality.** This doc is forward-looking. The empirical
> state of Phase 7 as actually shipped lives at
> [`docs/phase-7-as-built-2026-05-14.md`](phase-7-as-built-2026-05-14.md)
> (last update 2026-05-14). Read that for current architecture,
> measured perf characteristics, the wire-format drift between this
> plan's §7 and the in-tree IPC contract, and what's shipped vs
> qarl-pending. Where this plan and the as-built disagree, the
> as-built reflects code at HEAD.

---

## 1. Architecture overview

**Shape.** A standalone Rust binary, `openmarquee-render`, that owns DRM/KMS, GBM, EGL, GLES2, V4L2 video decode, glyph rasterization, and shader compositing. It runs as a **separate OS process** from the Python FastAPI backend. Process death = guaranteed CMA reclaim. That is the structural defense against the leak class — the seam between Python's GC and ctypes-managed kernel/GPU resources is gone because the renderer no longer lives in Python's process.

**Two run modes, one binary.**

- **Standalone mode** (the dev path, the smoke test, the milestone we close on first):
  ```
  openmarquee-render \
      --playlist /var/openmarquee/playlist.json \
      --content-root /var/openmarquee/content \
      --settings /var/openmarquee/settings.json \
      --output hdmi
  ```
  Parses the canonical playlist + content directory directly, walks the FREE YOUR SIGN reel, renders to HDMI. Re-reads `settings.json` on inotify event (file watch). No FastAPI in the loop. This is what qarl runs first to validate the renderer end-to-end.

- **Sidecar mode** (the production path, after parity):
  ```
  openmarquee-render --ipc /run/openmarquee/render.sock
  ```
  Listens on a Unix domain socket. Python backend (playback loop) connects, sends commands (`begin_slide`, `advance`, `begin_transition`, `capture_png`, `reconfigure`, `close`). Same renderer core; only the source of timing and slide IDs changes.

**Threading inside the Rust process.**

- **Main thread / control thread.** Tokio runtime. Owns the IPC socket (sidecar mode) or the playlist iterator + file watcher (standalone mode). Parses messages, dispatches commands to the render thread.
- **Render thread.** Pinned, dedicated. Owns the EGL context, GBM device, DRM master fd, GL programs, texture pool, and the V4L2 decoder. **All GL/DRM/V4L2 calls happen on this thread, period.** Receives `RenderCommand` over a bounded `crossbeam_channel` (depth 4; back-pressure on overrun).
- **Decoder thread.** Optional, only spun up when a VideoSlide enters the pipeline. Wraps V4L2 M2M (`/dev/video10`, `bcm2835-codec`). Produces YUV dmabuf frames into a 3-deep ring; render thread consumes via `EGL_EXT_image_dma_buf_import`.

**Where compositing happens.** GPU only on HDMI. Glyph rasterization is CPU (cosmic-text or fontdue) but cached per-slide. The shader does motion warp, transition, blend, brightness/gamma in one pass — *or* the split fallback (see §5.1). LED-panel paths skip GLES entirely and use a pure CPU compositor.

---

## 2. Rust crate selection

For each subsystem, one crate, one reason, one version pin.

| Subsystem | Crate | Pin | Why |
|---|---|---|---|
| DRM/KMS bindings | `drm-rs` | `=0.12` | Maintained; supports atomic commits, plane properties, page-flip events, `EBUSY` error surface. Used in production by smithay (compositor stack), so the Pi vc4 path is exercised. |
| GBM | `gbm` (smithay) | `=0.15` | Pairs with `drm-rs`; safe surface/buffer wrapping. Provides `gbm_bo_get_fd`/modifier export needed for dmabuf-to-EGL handoff. |
| EGL bindings | `khronos-egl` | `=6.0`, feature `dynamic` | Loads `libEGL.so` at runtime (not link-time) — avoids embedding a Mesa version into the binary; the device's `libEGL.so.1` is what we want. |
| GLES2 | `glow` | `=0.14`, GLES2 feature | Cleanly typed, supports GLES2 explicitly (not just desktop GL). The `gl` crate is also fine but glow is the more common choice in WGPU-adjacent stacks. |
| V4L2 H.264 decode | `v4l2` (`v4l` crate) + a custom thin wrapper | `v4l = 0.14` | Direct M2M ioctl access; the higher-level `ffmpeg-next` (libav) does work but adds a 30 MB libav dep we don't need on Pi Zero. We do M2M ourselves: queue input buffers, dequeue dmabuf-export output frames, hand fds to EGL. **Caveat:** `v4l` has aarch64 wheels but its M2M support is thin — expect to write our own ioctl wrappers using `nix` for ~200 lines. |
| dmabuf fd handling | `nix` | `=0.29` | `BorrowedFd`/`OwnedFd`, `mmap`, ioctls. Needed for V4L2 ioctls and CMA-aware fd lifetimes. |
| JSON (playlist, content, settings) | `serde` + `serde_json` | `serde=1.0`, `serde_json=1.0` | The model is Pydantic on the Python side; we'll write Rust structs that mirror only what the renderer needs (subset of TextSlide/ImageSlide/VideoSlide/PlaylistItem/Settings). Tolerant of unknown fields via `#[serde(default)]` + `deny_unknown_fields = false`. |
| File watch (settings reactivity, playlist reload) | `notify` | `=7.0` | Cross-platform inotify wrapper. We watch `settings.json` and `playlist.json`. |
| Async runtime | `tokio` | `=1.40`, multi-thread feature **off**, `rt` + `net` + `signal` + `time` features only | Single-threaded executor on the control thread. The render thread is a plain `std::thread`; we do NOT want tokio scheduling GL calls. |
| IPC (sidecar mode) | `tokio` Unix sockets + `bincode` | `bincode=2.0` | Length-prefixed bincode frames. Compact, fast, schema-evolution via versioned envelope. Not gRPC: gRPC adds a 5 MB protoc dep and TLS we don't need on a UDS. |
| Glyph rasterization | `cosmic-text` + `fontdue` (or just `fontdue`) | `cosmic-text=0.12`, `fontdue=0.9` | cosmic-text handles shaping/wrapping/bidi; fontdue rasterizes. Both are pure Rust, no FreeType dep. **Caveat:** binary size — cosmic-text pulls in ICU data. If size matters, just fontdue + naive layout. Decision: start with cosmic-text for correctness, profile binary size, drop to fontdue if >20 MB. |
| Image decode (PNG/JPEG bgs) | `image` | `=0.25`, default features | Standard. PNG and JPEG decoders are pure Rust. |
| CPU compositor (LED panels, Mock, snapshot fallback) | `tiny-skia` + hand-rolled blend | `tiny-skia=0.11` | Pure-Rust SIMD raster. For LED panels (≤256×128) we don't need it; a `Vec<u8>` and a hand-written compose loop is faster and smaller. tiny-skia is for the snapshot/Mock path at 1080p. |
| CLI parsing | `clap` | `=4.5`, derive feature | Standard. |
| Logging | `tracing` + `tracing-subscriber` | `tracing=0.1` | Structured logs; we need per-frame timing spans. journald-friendly format. |
| Error handling | `anyhow` (top-level) + `thiserror` (library boundaries) | latest | Standard split. |

**Pre-emptive aarch64 / Pi caveats:**
- `v4l` crate aarch64 cross-compile is fine (no C deps). The hard part is that the M2M ioctls hit kernel headers; we vendor the `linux/videodev2.h` constants we need rather than depending on `libv4l2`.
- `khronos-egl` with `dynamic` feature avoids a build-time link to `libEGL`. Critical for cross-compile from Mac.
- `drm-rs` and `gbm` use ioctl numbers from `linux/drm.h`. They're correct on aarch64 Pi (verified by smithay's CI).
- `cosmic-text` 0.12 requires Rust 1.74+. Pi's stock Rust is too old; we install via `rustup` on the Pi, not apt.

---

## 3. Module breakdown / crate layout

Top-level: `renderer/` at the repo root (sibling of `backend/` and `ui/`). Single crate, multiple binary targets if useful.

```
renderer/
  Cargo.toml
  Cargo.lock
  rust-toolchain.toml         -- pin 1.79.0 stable
  build.rs                    -- if we vendor V4L2 ioctl constants
  src/
    main.rs                   -- CLI entry, picks standalone vs sidecar mode
    lib.rs                    -- re-exports for unit tests on Mac
    config/
      cli.rs                  -- clap structs for CLI args
      settings.rs             -- Settings struct mirroring settings.json
      content.rs              -- TextSlide, ImageSlide, VideoSlide, TextLayer mirrors
      playlist.rs             -- PlaylistItem, Playlist mirrors
      watcher.rs              -- inotify-based file watch on settings + playlist
    scene/
      mod.rs                  -- Scene dataclass + SceneBuilder trait
      builder.rs              -- (Slide, AssetLoader) -> Scene; cache lookup
      cache.rs                -- bounded LRU keyed by (slide_id, updated_at)
      glyph.rs                -- cosmic-text wrapper; rasterize text -> RGBA tile
      motion.rs               -- pure motion math: (layer, elapsed) -> uniforms
      blend.rs                -- pure blend reference (CPU); mirrors GLSL
      auto_mode.rs            -- time/date/day formatters; cadence detector
    compositor/
      mod.rs                  -- Compositor trait + factory
      cpu.rs                  -- CpuCompositor: Scene -> RGB888; Mock/HUB75/WS2812B/snapshot
      gpu/
        mod.rs                -- GpuCompositor: Scene -> framebuffer
        program.rs            -- shader source generator + program-binary cache
        textures.rs           -- fixed texture pool (8 slots, ring-allocated)
        passes.rs             -- if we go split-shader: motion-bake pass + transition pass
    drm/
      mod.rs                  -- DrmPresenter
      modeset.rs              -- force 1080p mode-set without EDID
      atomic.rs               -- atomic commit, page-flip wait, rotation property
      planes.rs               -- enumerate planes, find primary, verify rotation property
    video/
      mod.rs                  -- H264Decoder
      v4l2_m2m.rs             -- raw M2M ioctl loop; output dmabufs
      egl_import.rs           -- dmabuf -> EGLImage -> GL texture
      yuv_to_rgb.rs           -- one-pass FBO conversion shader
    output/
      mod.rs                  -- Renderer trait (the §10 contract, in Rust)
      hdmi.rs                 -- HdmiRenderer: orchestrates GpuCompositor + DrmPresenter + H264Decoder
      mock.rs                 -- MockRenderer: writes preview.png at 1 Hz
      hub75.rs                -- HUB75Renderer: CpuCompositor + LUT + panel-write stub
      ws2812b.rs              -- WS2812BRenderer: CpuCompositor + GRB encode + DMA stub
    ipc/
      mod.rs                  -- sidecar protocol entry
      protocol.rs             -- bincode message types
      server.rs               -- accept, dispatch, back-pressure
    standalone/
      mod.rs                  -- standalone-mode driver (playlist iterator + clock)
    metrics/
      mod.rs                  -- frame timing histogram + CMA/RSS poller
      proc.rs                 -- /proc/meminfo + /proc/self/status readers
    util/
      raii.rs                 -- ScopeGuard helpers; explicit-close patterns
      bytes.rs                -- aligned buffer helpers
  tests/
    blend_math.rs             -- pure CPU; no hardware
    motion.rs                 -- pure CPU; phase math
    auto_mode.rs              -- date/time formatting
    scene_cache.rs            -- LRU eviction
    lifecycle_no_leak.rs      -- open/close 100x; checks fd count + RSS delta (Linux only)
    snapshot_golden/          -- PNG goldens for CPU compositor
  benches/
    bandwidth.rs              -- replicates the spike's 1080p sample-count benchmark
    cold_start.rs             -- process-start to first-frame
```

What the modules own:

- `output/mod.rs::Renderer` is the trait the `RenderCommand` dispatcher calls. Seven methods aligned with spec §10. The dispatcher doesn't know whether it's HDMI, Mock, or HUB75.
- `compositor/cpu.rs` is the source of truth for blend math and motion math. The GPU path's shader output is validated against it via `glReadPixels` + golden tolerance ±2 LSB.
- `drm/atomic.rs` is the only place that calls `drmModeAtomicCommit`. Owns the property-id resolution table.
- `video/v4l2_m2m.rs` is the only place that touches `/dev/video10`.
- `metrics/proc.rs` is the only place that reads `/proc/*` — used by both the in-process soak guard and exposed via IPC for the backend's `/api/health`.

---

## 4. Data flow for one slide on the standalone-test path

`openmarquee-render --playlist ... --content-root ... --settings ... --output hdmi` invocation. Walk a single TextSlide from process-start to HDMI pixels.

1. **Process start.** `main.rs` parses CLI. Loads `settings.json`. Loads `playlist.json` and resolves the active playlist (`DEFAULT_PLAYLIST_ID` if none specified). Spawns the file watcher on `settings.json` + `playlist.json`. Initializes the `tracing` subscriber.

2. **Renderer construction.** Calls `output::make_renderer(&settings)` → `HdmiRenderer::open(...)`:
   - Opens `/dev/dri/card1` (or whichever DRM node has a connected HDMI). Acquires DRM master.
   - Force-modesets HDMI-A-1 to 1920×1080@30 via atomic commit (see §5.8 for EDID-less strategy).
   - Creates GBM device on the DRM fd; allocates 2-buffer scanout chain (XRGB8888, 1080p) — see §5.2 for whether 2 is enough.
   - Initializes EGL: `eglGetDisplay(GBM_DEVICE)`, `eglInitialize`, picks a GLES2 config matching XRGB8888.
   - Creates `EGLContext`, `EGLSurface` bound to the GBM surface.
   - Compiles shaders: **pre-warms by compiling a trivial program first** (eats the 2.9s cold-start cost), then compiles the real program(s). If `GL_OES_get_program_binary` is present and we have a cached `.bin` from a prior boot, load instead of compile.
   - Allocates the 8-slot texture pool (each 2048×2048 RGBA — sized to the MAX_TEXTURE_SIZE, but used at 1920×1080). Each `glTexImage2D` happens here, exactly once. After this, only `glTexSubImage2D`.
   - Allocates the snapshot PBO (`GL_PIXEL_PACK_BUFFER`, 1920×1080×4).
   - Spawns the render thread; main thread retains a `Sender<RenderCommand>`.

3. **First slide begins.** Standalone driver computes `T0 = now()`. The first playlist item is a TextSlide with bg pattern + 3 text layers. Driver sends `RenderCommand::BeginSlide { item, duration_ms, transition_in_kind, transition_in_ms }`.

4. **Render thread receives BeginSlide.** Calls `SceneBuilder::build(&slide)`:
   - Cache lookup on `(slide.id, slide.updated_at)`. Miss.
   - Rasterize bg: procedural pattern via `tiny-skia` into a 1920×1080 RGBA tile. (For image bg, decode via `image` crate. For video bg, mark the bg slot as "video-fed" and ensure the H264Decoder is warmed.)
   - For each text layer: cosmic-text shapes the string into glyphs; fontdue rasterizes into a *minimal-bbox* RGBA tile (premultiplied). Cache the tile.
   - Auto-mode layers: rasterize the *current* string at this tick; remember the cadence (per-second / per-minute / per-day) so the render loop knows when to re-rasterize.

5. **Texture upload.** Each tile is uploaded into a pool slot via `glTexSubImage2D` (no `glTexImage2D` — the pool was sized once at open). Bg → slot 0. Layers → slots 1..N.

6. **Per-frame loop.** Standalone driver ticks at vsync (page-flip event from DRM). On each tick, sends `RenderCommand::Advance { now }`. Render thread:
   - Computes `elapsed = now - T0`.
   - For auto-mode layers whose cadence has crossed: re-rasterize, `glTexSubImage2D` into the same slot.
   - Computes per-layer motion uniforms via `scene::motion::eval(layer, elapsed)`.
   - Uploads uniforms to the active program.
   - Binds the GBM scanout buffer's EGLImage as the framebuffer.
   - `glDrawArrays` — single full-screen quad, one fragment shader pass.
   - `eglSwapBuffers` — ties to the GBM surface; produces a new BO.
   - DRM atomic commit: front buffer ← new BO; flag `DRM_MODE_PAGE_FLIP_EVENT`.
   - Wait for page-flip event on the next iteration (the wait IS the vsync gate).

7. **Transition begins.** At T0+4500ms (last 500 ms of the 5s slide), driver sends `BeginTransition { next_slide, kind, duration_ms }`. Render thread starts building Scene B in parallel with rendering Scene A. On the transition's first frame, both scenes' textures are bound; the shader's `u_t` drives interpolation. Motion continues for both A and B inside the same shader.

8. **Snapshot endpoint.** In standalone mode, snapshots are triggered by a SIGUSR1 (or a simple stdin command for dev). On request, render thread issues a non-blocking `glReadPixels` into the snapshot PBO at the *next slide boundary* (queued; see §5.9). When the PBO is ready (n+1 frame), `glMapBufferRange` reads bytes, encodes PNG via `image` crate, writes to disk.

9. **Slide end / loop.** Driver advances to the next playlist item. Loops the playlist forever (or until SIGTERM). On SIGTERM, dispatches `RenderCommand::Close` → render thread tears down in §5.7's order → process exits → CMA reclaimed.

**How production-IPC mode differs:** steps 1-2 are the same except CLI args choose `--ipc /run/openmarquee/render.sock`. Steps 3-7 are driven by frames received from the Python backend over the UDS instead of the playlist iterator. Step 8's snapshot is triggered by an IPC `Capture` message (carrying a request-id), and the PNG bytes are returned over the socket. Step 9's loop is whatever Python's playback decides; renderer is purely reactive.

---

## 5. The trickiest design problems

### 5.1 The bandwidth ceiling at 1080p (8 samples/fragment)

**The measured ceiling** (spike §5): 8 samples at 1080p = 22.8 ms = 43 fps. 14 samples = 39 ms = 25 fps. The unified 14-sampler shader misses 30 fps.

**Decision: split-shader baseline. Unified is the stretch.**

The split looks like:

- **Pass 1 (per slide, only when content changes):** rasterize a slide into a single 1080p RGBA "slide texture" via the GpuCompositor. Inputs: bg tile + up to 6 layer tiles. Sample count = 7 max (1 bg + 6 layers). Cost ~20 ms. **Run only when the slide's bake state is invalidated** — when motion changes the visible composition (every frame for a moving layer) OR when an auto-mode layer's text changes. For static slides, cached.
- **Pass 2 (per frame, the hot pass):** transition shader takes 2 inputs (slide-A-baked, slide-B-baked) + bg ramp/mask textures = 3-4 samples max. Cost ~15 ms. Always runs.

For a **steady-state non-transitioning static slide**: Pass 1 ran once (20 ms cold), Pass 2 runs every frame at 3 samples = 15 ms = ~60 fps. Easily 30 fps.

For a **steady-state slide with motion** (one layer is a ticker): Pass 1 runs every frame. Pass 1 = 7 samples = 20 ms. Pass 2 = 3 samples = 15 ms. **Total = 35 ms = 28 fps.** Just below 30 fps. Mitigation: skip Pass 1 for layers whose motion offset is integer-pixel-stable this frame (ticker often is at low speeds); when nothing changed, reuse the baked texture. Realistically 30 fps holds.

For a **transition with motion on both sides**: Pass 1 runs twice (A and B, both moving), 2×20 = 40 ms. Pass 2 runs once = 15 ms. **Total = 55 ms = 18 fps.** Misses target during transitions. **Mitigations, in order:**
  1. **Tile Pass 1** to half-resolution (960×540) during transitions. Shader bandwidth scales linearly; 7 samples × 960×540 = 5 ms each, two passes = 10 ms. Pass 2 still on the final 1080p target = 15 ms. Total 25 ms = 40 fps. Visually: half-rez during a 500 ms transition is invisible to the operator.
  2. **Spec-authorized fallback:** freeze motion during transitions. Spec §11 explicitly accepts this as a fallback.

**Unified shader as stretch.** In Step 4 of the build order (after the split is working), prototype the unified path on the conditional-sampling trick from the prior plan (gate sampling on whether the fragment is inside the layer's UV box). If realistic FREE YOUR SIGN slides have ≤3 active layers per side, this might close at 1080p. Result either way is documented per spec §11. **Default ship is the split.** Sampler array dynamic indexing is not needed for the split; we declare samplers individually (`u_a_layers_0`, `u_a_layers_1`, ...) per the spike's portability note.

### 5.2 Pi Zero memory budget (Rust accounting)

Now that the renderer is its own process, the budget split becomes:

| Region | Process | Budget (MB) | Notes |
|---|---|---|---|
| Python backend (FastAPI, asyncio, content/playlist) | `openmarquee-backend` | 100 | measured ~80 today; 100 leaves 20 MB headroom |
| Rust renderer userspace heap | `openmarquee-render` | 30 | conservative; bincode buffers, scene cache metadata, Tokio buffers |
| Glyph cache (cosmic-text + fontdue glyph atlas) | `openmarquee-render` | 8 | bounded |
| Scene cache RGBA tiles (LRU 32 MB cap) | `openmarquee-render` heap | 32 | hard cap |
| GLES texture pool (8 slots × 1080p RGBA) | CMA | 64 | fixed, allocated at open |
| GBM scanout chain (2 buffers × 1080p XRGB) | CMA | 16 | see §5.2 follow-up |
| YUV decoder ring (3× 1080p YUV420) | CMA (dmabuf) | 9 | only allocated when a video plays |
| Snapshot PBO (1× 1080p RGBA) | CMA | 8 | one buffer, ring-mapped |
| DRM/CMA overhead | CMA | 16 | atomic blobs, fb metadata |
| FreeType / fontdue / shaping work | `openmarquee-render` heap | 8 | shared shaping buffers |
| Headroom (FastAPI burst, decoder transients) | both | 30 | |
| **Total** | | **321** | of 426 RAM, with CMA component ~113 of 256 MB |

**The hard ceiling check:**
- Python process (backend): ~100 MB.
- Rust process (renderer): heap ~78 MB + CMA ~113 MB = 191 MB.
- Total: ~291 MB. Comfortably below the 256 MB CMA carveout (CMA is 113 of 256 MB) and below the 416 MB physical RAM ceiling.

**Why this is honest where the prior plan wasn't.** The prior plan's "ALL inside the FastAPI process" lumped GBM into Python's RSS accounting and pretended `bounded LRU 64 MB` was a tight ceiling. The Rust split breaks that: GBM is now allocated by the Rust process, accounted in CMA explicitly. The leak class (Python GC + ctypes-managed GBM dumb buffers re-allocating per-slide) **cannot recur** because Rust owns those buffer lifetimes via `Drop` and the pool is sized once at open.

**The 2-buffer GBM scanout chain.** Spike §1's outstanding question. **Build Step -1** prototypes this on hardware before we commit. If 2 buffers cause page-flip stalls (race between scanout-active and render-pending), we go to 3 buffers, eating an additional 8 MB of CMA. The budget tolerates 3.

**Verification.** `metrics/proc.rs` polls `/proc/meminfo:CmaUsed` and `/proc/$PID/status:VmRSS` for both processes every 30 s. Soak asserts both are flat (delta ≤4 MB over any rolling 30-min window). The metric is exposed over the IPC socket; Python backend can surface it on `/api/health`.

### 5.3 H.264 video decode via V4L2 M2M + dmabuf import

**The crate choice.** `v4l = 0.14` for the device-open + buffer-queue ioctls, plus a hand-written wrapper using `nix` for the M2M-specific bits (CAPTURE plane buffer-export-as-dmabuf is poorly covered in the high-level v4l API). **Roughly 250 lines of unsafe Rust** — small, auditable, all in `video/v4l2_m2m.rs`.

**The FFI shape:**
1. Open `/dev/video10`. Verify `V4L2_CAP_VIDEO_M2M_MPLANE` capability.
2. Set OUTPUT format to `V4L2_PIX_FMT_H264`. Set CAPTURE format to `V4L2_PIX_FMT_NV12` (or `YUV420` if the driver prefers; we negotiate).
3. Request 4 OUTPUT buffers (mmap, we copy compressed frames into them) + 4 CAPTURE buffers (`V4L2_MEMORY_MMAP` initially, then export each as dmabuf via `VIDIOC_EXPBUF`).
4. Each CAPTURE buffer's dmabuf fd → `eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT, attribs)` → `glEGLImageTargetTexture2DOES` → bind to a GL texture in our pool.
5. The decoded frame's sampler is YUV. We do a **one-pass FBO convert** to RGBA (3 ms at 1080p per the prior plan's measurement; cheap). The conversion shader is the only non-trivial second pass we keep.

**Modifier negotiation.** The spike confirmed `EGL_EXT_image_dma_buf_import_modifiers` is present, but didn't verify bcm2835-codec actually produces a modifier the EGL importer accepts. **Step -1 of the build** does a smoke test: decode 30 frames of a known H.264 clip, import each as EGLImage, log success/fail. **Fallback if modifier negotiation fails:** memcpy the YUV out of the V4L2 buffer (mmap) into a GL texture via `glTexSubImage2D`. Costs an extra ~12 ms per frame at 1080p YUV. Drops video playback to ~25 fps, but it works. Not the ship target; just the failsafe.

**Reachability from text-slide bg.** The decoder is a singleton inside `HdmiRenderer`. When a `TextSlide.background_video_slide_id` is set, `SceneBuilder` doesn't put a tile in slot 0; it tags the scene with "bg-from-decoder, slide-id X". The render thread queries the decoder for the latest frame's GL texture id and binds that to `u_a_bg`. Same path as VideoSlide; just the trigger is different.

### 5.4 4 blend modes + 16 transitions in the shader

The math is unchanged from the prior plan (§4.4 there). What changes in Rust:

- **Generation:** `compositor/gpu/program.rs` produces GLSL strings via a `format!` template. The 4 blend modes compile into a `step()`-driven branchless compose function inlined per layer (no `if` on hot fragment paths). The 16 transitions compile into a single shader variant via a `u_kind` switch, OR — if compile time matters — 16 separate program variants each specialized to one transition kind. **Default: one big shader with a switch.** The spike's 66 ms compile time for the unified 8-sampler/4-blend shader is fine; 16 variants at 66 ms = 1 s warm-up which is acceptable if cached.

- **Program-binary cache.** `GL_OES_get_program_binary` is present. After the first run on the Pi, save each program's binary blob to `/var/openmarquee/render-cache/programs/`. On subsequent boots, load via `glProgramBinaryOES` (~5-10 ms each). This drops cold start from "first-shader 2.9 s + 16 variants × 66 ms = ~4 s" to "~16 × 8 ms = 128 ms." Target §8.4 (≤4 s cold start) is comfortably hit.

- **Specialization at the transition level.** When `begin_transition(kind=X)` is called, we bind the variant for kind X. Inside the shader, the blend modes are still per-layer dynamic — operators can mix blend modes within one slide.

### 5.5 Settings reactivity (file watch + IPC + both)

**Standalone mode:** `notify` watches `/var/openmarquee/settings.json` and `/var/openmarquee/playlist.json`. On a settings-write event, the control thread parses, diffs against the cached `Settings`, classifies the change:

- **Trivial** (brightness, gamma): one shader uniform update; <10 ms; queued as `RenderCommand::SetUniforms`. No reallocation.
- **Rotation:** atomic property update on the primary plane (if rotation is supported on vc4 — see plane-rotation follow-up below). If not, a fragment-shader UV transform. ~20 ms.
- **Dim change:** heavy. Drain command queue, fully tear down `GpuCompositor` and `DrmPresenter`, re-construct with new dims. Cost ~1.5 s. Within the §8.5 ≤2 s budget. The Rust render thread does this without a process restart — the leak-free guarantee from RAII on Rust types means the teardown actually releases everything.

**Sidecar mode:** the Python backend writes `settings.json` and *also* sends `RenderCommand::Reconfigure { settings }` over the IPC socket. **Both** paths are wired, but the socket path is authoritative when the renderer is running in sidecar mode. The file watch is a defense-in-depth so standalone-mode dev iteration feels normal.

**Plane rotation property follow-up.** Spike §8 noted the rotation property lives on plane, not connector, and wasn't enumerated. Step -1 of the build runs `modetest -p` on the dev Pi and records whether the primary plane on vc4 advertises a `rotation` property. If yes: free GPU rotation at scanout. If no: shader-side UV transform (also free, just a 2x2 matrix multiply on `texCoord`).

### 5.6 HUB75 / WS2812B path sharing

**The shape.** `Renderer` trait in `output/mod.rs` is what `RenderCommand` dispatcher targets. Three implementations:

- `HdmiRenderer` (in `output/hdmi.rs`): owns `GpuCompositor` + `DrmPresenter` + `H264Decoder`.
- `Hub75Renderer` (in `output/hub75.rs`): owns `CpuCompositor` + LUT + a panel-write stub.
- `Ws2812bRenderer` (in `output/ws2812b.rs`): owns `CpuCompositor` + GRB encoder + DMA stub.

The LED-panel renderers **never link against** `khronos-egl`, `gbm`, or `drm-rs`. They share `scene/`, `compositor/cpu.rs`, and `metrics/` only. We use Cargo features to make this enforceable: the `gpu` feature gates the EGL/GBM/DRM crates. `cargo build --no-default-features --features hub75` produces a smaller binary that has no GLES dependency (useful for cross-compile from Mac if we ever want to run HUB75 paths in dev).

**Snapshot endpoint for LED paths.** A 64×32 HUB75 snapshot is the panel's pixels, period. PNG-encoded. Same `Renderer::capture_png()` method, different implementation. The IPC protocol carries a `width`/`height` in the snapshot response so the Python backend doesn't need to know per-mode.

### 5.7 Lifecycle / no-leak guarantee — what RAII actually buys us

**What Rust gives for free:**
- `OwnedFd` for every fd (DRM, GBM, V4L2 M2M, dmabufs, EGL display fd) closes via `Drop`.
- Custom `Drop` on `GlTexture`, `GlProgram`, `GlBuffer` calls `glDeleteTextures` etc. **on the render thread** — but only if the `Drop` happens on the render thread.
- `EglContext`, `EglSurface`, `EglDisplay` similarly.
- `GbmDevice`, `GbmSurface` similarly.

**What Rust does NOT give for free, and how we patch:**

1. **Drop-on-wrong-thread risk.** GL handles MUST be deleted with the EGL context current on the calling thread. If a `GlTexture` is dropped from the control thread (because, say, a `BeginSlide` command struct was dropped before reaching the render thread), the `Drop` impl would call `glDeleteTextures` with no current context — undefined behavior, possibly leaking. **Mitigation:** GL handles are wrapped in `RenderThreadOnly<T>` which has a `Drop` that *panics* if not on the render thread. All GL handles flow through ownership chains that prove they're on the render thread by construction (typestate). Practically: the texture pool is owned by `GpuCompositor`, which is owned by the render thread's stack. Nothing else holds GL handles.

2. **Panicking thread holding a GL context.** If the render thread panics (out-of-memory in `tiny-skia`, an `unwrap` on a malformed font), the GL/EGL/DRM teardown might not run cleanly. **Mitigation:** the render thread runs inside a `catch_unwind` that, on panic, runs an emergency teardown (raw `eglTerminate`, `drmDropMaster`, `close(drm_fd)`) before re-raising. Even if THAT fails: the process exits, and the kernel reclaims the CMA buffers + V4L2 fds. **This is the structural safety net.** Process death is the leak fix.

3. **Systemd restart fd lifecycle.** `Restart=on-failure` in the systemd unit means after a panic, systemd respawns. There's a brief window between process death and the next process opening DRM master where another process could grab it. We don't have other processes that want it; spec accepts this. **The Python backend never opens DRM** — important. (The current Python renderer's drm-master ownership transferring across systemd restart was a leak risk; it's gone in the Rust split.)

4. **DRM master contention on restart.** Risk register item from prior plan. If the Rust renderer crashes and the Python backend somehow holds DRM master (it shouldn't, but historical artifact): on restart, `drmSetMaster` returns `EBUSY`. Mitigation: explicit `drmDropMaster` in the Rust `Drop` chain, plus a `signal::SIGTERM` handler that runs the same teardown synchronously. If the Python backend is also running the *old* renderer transiently (during migration), we coordinate via a flock on `/run/openmarquee/drm.lock`.

5. **V4L2 dmabuf fd leak across decoder restart.** Each frame's dmabuf fd is owned by an `OwnedFd` in the decoder ring; when the ring buffer struct is dropped, all 3 fds close. The EGL images that imported them also drop, calling `eglDestroyImageKHR`. Tested explicitly in `tests/lifecycle_no_leak.rs` with V4L2 mocked + on-Pi integration.

**Test:** `tests/lifecycle_no_leak.rs` (Pi-only integration; the Mac CI runs a pure-CPU version) opens and closes `HdmiRenderer` 100 times. Asserts `/proc/$PID/status:VmRSS` and `/proc/$PID/fd | wc -l` are within ±2 MB / ±2 fds of baseline. CMA is harder because it's a system-wide counter, but we sample `CmaUsed` and assert no monotonic growth.

### 5.8 HDMI 1080p mode-set without EDID

The dev Pi reports no 1080p in modetest's mode list because EDID is empty. Two strategies, used together:

1. **Kernel cmdline override (boot-time):** add `video=HDMI-A-1:1920x1080@30,margin_left=0,...` to `/boot/firmware/cmdline.txt`. This is set by deploy or by a one-shot install script. Forces a CEA-1080p mode regardless of EDID. **Authoritative.**

2. **Atomic-commit override (runtime):** `drm/modeset.rs` constructs a `drmModeModeInfo` with the standard CEA 1920×1080@30 timings hard-coded (clock=74250, hdisplay=1920, hsync_start=2008, hsync_end=2052, htotal=2200, vdisplay=1080, vsync_start=1084, vsync_end=1089, vtotal=1125, flags=PHSYNC|PVSYNC), and uses `drmModeAtomicAddProperty` with the `MODE_ID` property on the CRTC, plus the `ACTIVE` property = 1. This bypasses the kernel's mode-list-from-EDID. **Defense in depth** in case the kernel cmdline isn't applied.

We do both. The Rust binary attempts the cmdline-supplied mode first; if that fails, falls back to atomic-commit override.

### 5.9 glReadPixels snapshot endpoint without dropping frames

**Decision: PBO async readback, scheduled at slide boundaries.** Both mitigations from spike §7, used together.

**Mechanism:**
- One PBO allocated at open (8 MB). Re-mapped, never reallocated.
- On `Capture` request: queue a flag `snapshot_pending = true`.
- On the next `Advance` whose frame is the *last* of a slide (just before transition starts), after the page-flip queue, issue `glBindBuffer(GL_PIXEL_PACK_BUFFER, pbo); glReadPixels(0,0,1920,1080,GL_RGBA,GL_UNSIGNED_BYTE,0);`. The GPU enqueues the readback; the CPU returns immediately.
- On the *next* `Advance` (one frame later), `glMapBufferRange(GL_PIXEL_PACK_BUFFER, ..., GL_MAP_READ_BIT)` — by now the readback is done, the map is fast. Encode PNG via `image` crate (~50 ms at 1080p RGBA — non-blocking from the GPU's point of view; happens between frames).
- Send PNG bytes back via IPC.

**Why slide-boundary scheduling:** the spec already gates snapshots to ≤1/5 min, and the Python backend triggers them on playlist change. The user-visible thing is "I want to see what's on screen right now" — the slide boundary is "right now-ish" and avoids the 265 ms `glReadPixels` blocking stall (which the spike confirmed). At slide boundaries we also have a brief window where Pass 1 (slide bake) doesn't run, so the readback fits.

**Edge case: snapshot during a long static slide.** If the operator hits "show me current frame" mid-slide, we can issue the PBO readback immediately (no pending bake), and pick up the result one frame later. The 33 ms latency is invisible.

---

## 6. Build + deploy story

### Cross-compile from Mac vs build on Pi: **build on Pi**, with caveats.

Cross-compiling to aarch64 is technically clean (rust+aarch64 targets are stable). The friction is the Mesa / EGL / V4L2 sysroot — we'd need a Pi sysroot mirrored to the Mac to satisfy `khronos-egl --features dynamic` (which loads at runtime, so this is mostly fine) and to satisfy `drm-rs` (which links against `libdrm`). Keeping that sysroot in sync is a pain.

**Decision:** build natively on the Pi. The Pi Zero 2 W is slow (cargo build takes ~3-4 minutes for a release build of this crate, given the small footprint), but it's correct, and `cargo build --release` is incremental — most edits rebuild in under 30 seconds.

**Concretely:**
- `scripts/deploy.sh` is extended with a new step. Pseudo-additions:
  ```
  # After the existing UI bundle build, before the rsync of backend:
  echo "==> rsync renderer Rust source to $TARGET:$REMOTE_ROOT/renderer/"
  rsync -avz --delete --exclude target --exclude '._*' \
      "$OPENMARQUEE_BUILD_DIR/renderer/" "$TARGET:$REMOTE_ROOT/renderer/"

  echo "==> cargo build --release on target"
  ssh "$TARGET" "cd $REMOTE_ROOT/renderer && cargo build --release"

  echo "==> install renderer binary"
  ssh "$TARGET" "sudo install -m 0755 $REMOTE_ROOT/renderer/target/release/openmarquee-render /usr/local/bin/openmarquee-render"
  ```
  This is additive: the existing Python deploy is unchanged. If the Rust step fails, Python still gets deployed and the live device runs Python (the migration path).

- **First-time Pi setup:** `scripts/setup.sh` is extended to install `rustup` + the stable toolchain on the Pi. One-time, ~5 minutes.

- **CI:** none beyond what's there. The repo doesn't run CI today. We add Mac-side `cargo test` (pure CPU tests) to `scripts/test.sh`. The on-Pi integration tests run only on `bash scripts/deploy.sh && ssh ... cargo test --features pi-integration` — manual until there's CI.

- **Deployable artifact:** a single dynamically-linked binary at `/usr/local/bin/openmarquee-render`. Dynamic linking against `libEGL.so.1`, `libGLESv2.so.2`, `libgbm.so.1`, `libdrm.so.2` — system libraries from Raspberry Pi OS, not vendored. Static linking would be possible (Mesa Lite) but adds complexity and 50+ MB to the binary; not worth it.

- **Systemd integration:** new unit `system/openmarquee-render.service`:
  ```
  [Unit]
  Description=openMarquee renderer
  After=openmarquee-backend.service
  PartOf=openmarquee-backend.service

  [Service]
  Type=simple
  User=openmarquee
  Group=video,render,openmarquee
  ExecStart=/usr/local/bin/openmarquee-render --ipc /run/openmarquee/render.sock
  Restart=on-failure
  RestartSec=2s

  # Sandbox
  PrivateTmp=true
  ProtectSystem=strict
  ReadWritePaths=/var/openmarquee /run/openmarquee
  ```
  `PartOf=openmarquee-backend.service` means `systemctl restart openmarquee-backend` also restarts the renderer. Needed during dev. In production, a renderer crash auto-restarts via `Restart=on-failure` without dragging the backend down.

---

## 7. Python ↔ Rust integration

**Implementation note (2026-05-13 update):** the actual sidecar wire format
that landed in `renderer/src/ipc_main.rs` is **stdin/stdout + JSON lines**,
not UDS + bincode as originally planned below. The simpler shape made the
Bug-1 verify path trivial (manual stdin scripting) and the Phase 7 slice 1
Python proxy at `backend/openmarquee/rendering/rust_renderer.py` matches the
code, not the original §7 plan. The doc-vs-code drift is left below for
historical context; treat the running code as the source of truth.

**Decision: subprocess + Unix domain socket + length-prefixed bincode frames.**

**Rejected alternatives:**
- **PyO3 (in-process FFI).** Doesn't give us the leak-class fix. Process boundary is the whole point.
- **gRPC.** Heavyweight (protoc dep on Pi build, TLS setup, http/2 framing). Over a UDS, raw bincode is faster and 100× smaller in dependency footprint.
- **Stdin/stdout.** Works but doesn't survive the Python process restarting independently of the Rust process. UDS lets either side reconnect.

**The protocol (sketch):**

Length-prefixed bincode v2 frames. Header: 4-byte little-endian length, then the frame body.

Message types (a subset; full schema lives in `ipc/protocol.rs`):

```
ClientToRenderer:
  Open { settings, output_mode }
  BeginSlide { slide, asset_paths, duration_ms, transition_kind, transition_ms }
  Advance { wall_clock_ns }
  BeginTransition { next_slide, asset_paths, kind, duration_ms }
  Capture { request_id, max_dim_hint }
  Reconfigure { settings }
  Close

RendererToClient:
  Ready                                        // sent on connect after Open succeeds
  AdvanceAck { presented_at_ns, fps }          // optional, for back-pressure
  CaptureResult { request_id, width, height, png_bytes }
  Error { code, message }
  Health { rss_bytes, cma_used_bytes, fd_count, frame_p99_ms }   // periodic
  Closed
```

**Notable:** `BeginSlide` carries `asset_paths` (file paths into `/var/openmarquee/content/`), not asset bytes. The Rust renderer reads files itself. Avoids serializing 1080p PNGs through the socket. **Asset access in production:** Rust reads from `OPENMARQUEE_CONTENT_ROOT` directly. Same path the Python backend writes to.

**Back-pressure:** the IPC server's command queue is bounded at depth 8. If full, the next `BeginSlide` blocks the IPC thread. The Python backend treats blocked send as "renderer is behind"; it backs off the playback tick rate. In practice: Python only ever has 1 slide in flight (the active one) plus 1 queued (the next), so the queue doesn't fill in normal operation.

**Health channel:** the Rust process sends a `Health` message every 5 s unprompted. Python's `/api/health` endpoint relays it.

**Failure detection and fallback:**

1. Python's IPC client wraps the connection in a watchdog. If the socket disconnects (Rust process died) or `Advance` blocks > 2 s without an `AdvanceAck`, Python:
   - Logs a warning.
   - Attempts to reconnect to the UDS up to 3 times with 500 ms backoff.
   - If still failed after 3 attempts, swaps in `MockRenderer` for the rest of the playback session (the existing Python `MockRenderer` stays in the tree precisely for this).
   - Surfaces the failure on `/api/health`.
   - systemd auto-restarts the Rust process; on next reconnect attempt, Python resumes the GPU path.

2. **Crashes during a transition:** the next slide's `BeginSlide` triggers the reconnect path; the operator sees a brief MockRenderer black-frame interlude. Acceptable.

3. **Renderer init fails on the Pi (no DRM master, etc.):** Rust process exits with a specific code; systemd respawns; if it fails 3× in 30 s, systemd backs off. Python sees persistent disconnection and falls back to MockRenderer for the rest of the session. Operator gets `/api/health` warning.

**Mac-dev mode.** On Mac, the Rust binary refuses to open `HdmiRenderer` (no DRM). The Python backend always uses MockRenderer on Mac (existing behavior). The Rust binary in Mac dev is exercised through `cargo test` only, not through the IPC path.

---

## 8. Build order

Each step produces a working slice. Identify the MVD (minimum viable demo) milestone: standalone-mode HDMI playback of FREE YOUR SIGN.

**Step -1 — Hardware spikes (3 days). Must complete before anything else.**

  Three separate, independent Rust prototypes, each <300 lines, each run on `openMarqueeDev`. **No production code; pure spike.**

  - **a. 2-buffer GBM scanout chain.** Open GBM, allocate 2 buffers, EGL context, GLES2, render a flat color, atomic page-flip in a tight loop for 60 s. Measure missed flips. If 2 buffers cause stalls, repeat with 3.
  - **b. V4L2 M2M dmabuf import.** Decode 30 frames of a 1080p H.264 clip via M2M, export each CAPTURE buffer as dmabuf, import to EGLImage, sample once. Measure success rate and modifier compatibility.
  - **c. Plane rotation property enumeration.** `modetest -p` programmatically; record whether vc4 primary plane has `rotation` property. If yes, atomic-rotate; if no, plan for shader UV rotation.

  Outcome: confirm/deny three open questions. If any spike fails, escalate to qarl with measurements before designing further.

**Step 0 — Crate skeleton + standalone-mode CLI (1 day).** Create `renderer/`. Set up Cargo workspace. Implement `config/`, `output/mod.rs::Renderer` trait, `output/mock.rs` (writes black PNG to `/var/openmarquee/preview.png`). The binary parses `--playlist`, walks items, ticks at 30 fps, calls Mock. Ships a working "playlist tick loop" with no rendering. CI green on Mac.

**Step 1 — CPU compositor + scene + glyph (3 days).** Implement `scene/`, `compositor/cpu.rs`. Mock now produces real PNGs of TextSlides with motion + blend modes + auto-mode. **Mac-dev parity check:** the same FREE YOUR SIGN reel rendered via Rust CPU compositor and via the existing Python compositor are visually compared. Goldens established.

**Step 2 — DRM/EGL/GBM bring-up + first GLES draw (4 days).** Implement `drm/`, `output/hdmi.rs` skeleton, `compositor/gpu/textures.rs`. The Rust binary on the Pi opens HDMI 1080p, draws a flat color full-screen via GLES2, atomic page-flips at 30 fps. Validate: `systemctl restart openmarquee-render` 10× without leaks.

**Step 3 — Slide-bake pass (Pass 1 of split shader) (4 days).** Implement `compositor/gpu/passes.rs::bake_slide`. Bg + 6 layers + motion + 4 blend modes + brightness/gamma → 1080p RGBA in an FBO. Drive from a fixed FREE YOUR SIGN slide. Verify visually. Bench: confirm 7-sample bake at 1080p is ~20 ms (matches spike).

**Step 4 — Transition pass (Pass 2 of split shader) + 16 transition kinds (3 days).** Implement transition shader. Wire `begin_transition` to drive both A and B bake passes plus the transition shader. Verify all 16 visually. Confirm 30 fps on motion-free transition; document fps on motion-on-both-sides transition (the §5.1 worst case).

**Step 5 — Video decode + dmabuf import (4 days).** Implement `video/`. Wire VideoSlide into the bg slot. Test the demo Blender video. Verify both VideoSlide-as-primary and TextSlide-with-video-bg paths.

**MVD milestone — Step 5 complete:** standalone Rust binary plays the FREE YOUR SIGN reel at 30 fps with shader transitions enabled, on the dev Pi, no FastAPI in the loop. **First qarl checkpoint with full hardware parity.** From here the work is integration + LED panels + soak.

**Step 6 — Snapshot endpoint + settings reactivity (2 days).** PBO snapshot, `notify` file watch on `settings.json`, `Reconfigure` plumbing.

**Step 7 — Sidecar IPC mode (3 days).** Implement `ipc/`, the Unix socket protocol. Add `openmarquee-render.service`. Modify Python's playback loop to optionally dispatch to a `RustRenderer` proxy that talks UDS. Behind a feature flag (`OPENMARQUEE_RENDERER=rust-sidecar`). Both Python-direct and Rust-sidecar paths work.

**Step 8 — HUB75 + WS2812B reach (1 day).** Implement `output/hub75.rs`, `output/ws2812b.rs` as thin wrappers around `CpuCompositor` + LUT/encoder. Panel-write paths stay stubs.

**Step 9 — Soak + parity acceptance (3 days).** ≥6 hour soak on `openMarqueeDev` with shader transitions on. Per spec §11 acceptance: 30 fps on FREE YOUR SIGN with shader transitions enabled and no OOM kills. If green, flip the live device's `OPENMARQUEE_RENDERER=rust-sidecar` and let it bake.

**Step 10 — Python renderer retire (1 day).** Once Rust has been live for 30 days without regression, delete `backend/openmarquee/rendering/` (except a thin shim that imports `RustRenderer` for backward import compat) and remove the env-var gate.

**Total:** ~30 working days. Budget 35 to absorb the V4L2 dmabuf-import surprise that's the most likely speedbump.

---

## 9. Verification strategy

**Memory and lifecycle (spec §8.1, §8.2, §8.6):**

- `metrics/proc.rs` polls `/proc/meminfo:CmaUsed`, `/proc/$PID/status:VmRSS`/`VmData`/`VmSwap`, and `/proc/$PID/fd | count` for both `openmarquee-render` and `openmarquee-backend` every 30 s. Logged to `/var/log/openmarquee/metrics.csv`.
- **Soak script** (`scripts/soak_renderer.sh`): kicks off a 6-hour run on `openMarqueeDev`, polls the IPC `Health` channel, asserts: (a) every metric's max−min over each rolling 30-min window ≤ 4 MB / ±2 fds; (b) zero process restarts (systemd journal); (c) playback advanced ≥ N slides per minute (no stall). Output is a CSV committed to `tests/soak-results/<date>.csv` for review.
- **Lifecycle test** (`tests/lifecycle_no_leak.rs`, Pi integration): opens and closes `HdmiRenderer` 100×, asserts residual leak <2 MB / <2 fds.

**Frame rate (spec §8.3):**

- `metrics/mod.rs` records per-frame `glDrawArrays` start → `eglSwapBuffers` complete timestamps in a 2000-deep ring. After the soak run, assert: 0 frames > 33 ms outside transitions (= no dropped frames at 30 fps), p99 ≤ 33 ms steady-state, p99 ≤ 50 ms during transitions. Histograms exposed via IPC `Health` and rendered to a small report image as part of the soak output.

**FREE YOUR SIGN acceptance test (spec §11):**

- `tests/integration/freeyoursign.sh`: runs `openmarquee-render --playlist seed.json --content-root /var/openmarquee/content --output hdmi` for 1 hour, asserts soak metrics + p99 frame time + zero `Error` IPC messages. Run nightly (manually triggered via `bash scripts/deploy.sh && ssh ... systemctl start openmarquee-soak.service`).

**Visual regression:**

- CPU compositor goldens (`tests/snapshot_golden/`): one PNG per (blend mode × alpha) and (transition × t). The CPU compositor is source of truth; the GLES path is validated against goldens via `glReadPixels` + ±2 LSB tolerance on tests run with `--features pi-integration`.

**Mac unit tests (spec §8.7):**

- `cargo test` on Mac runs all of `scene/`, `compositor/cpu.rs`, motion/blend/auto-mode math, scene cache LRU eviction. No DRM/EGL needed. CI-friendly even though we don't have CI yet.

---

## 10. Risks and proposed scope cuts

> **Empirical risk update (2026-05-14).** See
> [`docs/phase-7-as-built-2026-05-14.md`](phase-7-as-built-2026-05-14.md)
> §4 (perf characteristics) and §6 (gates) for the actual measured
> baseline: 41× over_33 improvement to a 0.24% floor with p99 under
> the 33 ms budget, sustained across 58k IPC ops with flat memory
> trajectory. The split-shader baseline is validated at 1024×768;
> 1080p re-test is still office-glass-gated (HDMI EDID stuck at 0
> bytes on dev Pi). The risks below reflect the original plan;
> consult the as-built for risks that have been retired or
> surfaced anew.

**Honest read on whether the full feature set fits at 1080p × 30 fps:**

Per the spike, the unified-shader stretch goal is unlikely to close at 1080p with 6 active layers per side during a transition. The split shader **does close** at 1080p × 30 fps for the steady state and for transitions where motion is on at most one side, with the half-rez-Pass-1 trick during full motion-on-both-sides transitions. **The plan ships the split as the baseline.** Unified is a Phase-2 optimization.

**Priority order if we have to cut, first to last:**

1. **Cut the unified-shader stretch goal entirely; ship the split.** Already the baseline plan; this isn't really a cut, more a confirmation.
2. **Cut Pass-1 to half-rez during all transitions.** Visually invisible; ~30% bandwidth savings during transitions.
3. **Freeze motion during transitions.** Spec §11 explicitly authorizes this fallback. Saves the second Pass-1 entirely; transition becomes ~3-sample bandwidth-free pass.
4. **Cut the 4 most-expensive transitions** (`glitch`, `halftone`, `marquee`, `dissolve`). Keeps 12 of 16. Spec §11 says no cuts to transition count, so escalate first.
5. **Drop to 720p.** Last resort. Spec says no.

**Secondary risks:**

**Status update (2026-05-13):** the IPC sidecar now supports TextSlide
(including auto_mode) + ImageSlide for PaintSlide and Capture (landed
d6b4f6a). VideoSlide remains the only TBD per the V4L2 line below.
Error paths are wire-format-pinned via cargo unit tests at 601820f and
matched by the Python proxy's `RustRendererOpError.message` (slice 1).

- **V4L2 M2M dmabuf modifier negotiation** is the highest-likelihood blocker. Step -1 spike mitigates. If it fails, the memcpy fallback drops video to ~25 fps; we ship that with a known-issue flag and prioritize a v4l investigation.
- **Plane rotation property absence.** Mitigated by shader-side UV transform (free).
- **The 2-buffer GBM scanout chain.** Step -1 spike confirms. If 3 needed, eat 8 MB CMA — within budget.
- **Cosmic-text binary size.** Could push the binary to >25 MB. Profile in Step 1; drop to fontdue + naive layout if a problem.
- **`rustup` install on a fresh Pi.** Rust toolchain on aarch64 is ~600 MB. The Pi Zero 2 W's 16 GB SD card has it; the install adds ~5 minutes to first-time provisioning. Documented in `system/README.md`.

---

## 11. Migration plan

> **Shipped vs pending status.** See
> [`docs/phase-7-as-built-2026-05-14.md`](phase-7-as-built-2026-05-14.md)
> §1 for the per-slice ledger as of 2026-05-14. Slices 1-3 (Python
> proxy, factory branch, systemd unit) are in tree but OFF by
> default. Slice 4 (`playback.py` IPC-op bypass) is blocked on
> qarl's VideoSlide handling design call (task #75). The
> robustness layer (reconnect + watchdog + AutoFallbackRenderer)
> landed after slices 1-3 and is wired into the slice-2 factory.

**Both renderers in tree, side by side, until parity.**

- The existing Python renderer at `backend/openmarquee/rendering/` stays unchanged. Production keeps running it. The systemd unit's `OPENMARQUEE_RENDERER=auto` continues to select the Python multi-plane path or shader path per the existing logic.
- The new Rust crate lives at `renderer/`. It builds independently. Its standalone binary is exercised by `bash scripts/deploy.sh && ssh ... openmarquee-render --playlist ... --output hdmi` — manual, alongside the running Python renderer. Because the Python renderer normally holds DRM master, the Rust standalone test requires `systemctl stop openmarquee-backend` for the test window. **A separate playlist directory** can be passed via `--content-root /var/openmarquee/content-test` if we want isolated test data.
- After Step 7 (sidecar IPC mode lands), the Python backend gains a `RustRenderer` proxy class. The env-var `OPENMARQUEE_RENDERER=rust-sidecar` switches playback to use it. **Default stays `auto` (Python).** Operators can opt in. *(Phase 7 slice 1 landed 2026-05-13: `backend/openmarquee/rendering/rust_renderer.py`. Slices 2-7 — factory wiring, systemd unit, playback.py hot-path bypass — remain pending qarl-direct greenlight.)*
- After Step 9 (acceptance soak passes on the dev Pi), the live device's systemd unit flips to `OPENMARQUEE_RENDERER=rust-sidecar`. The Python renderer code is untouched in the tree — just unselected.
- **Emergency rollback for 30 days.** The flip is one env-var. `ssh openMarqueeDev "sudo sed -i 's/rust-sidecar/auto/' /etc/systemd/system/openmarquee-backend.service.d/override.conf && sudo systemctl daemon-reload && sudo systemctl restart openmarquee-backend"`. ~5 seconds. The Python renderer is still on disk; no rebuild needed.
- After 30 days clean (no rollbacks invoked), Step 10 deletes `backend/openmarquee/rendering/` (except the import shim). The env-var gate goes away.

**During the dual-tree window, what's fragile:**
- The Python backend's `playback.py` has hard-coded imports from `rendering.gpu_compositor`. We add a feature flag at import time so the rust-sidecar path skips those imports. Otherwise, no changes to Python. **[DONE: the rust-sidecar flip + the post-soak Step 10 deletion of `rendering/gpu_compositor.py` + `shader_compositor.py` landed in commits b320dfd + 70a4865; this fragility no longer applies.]**
- The Rust standalone test path needs DRM master. It's incompatible with Python's renderer running. Document this; don't try to be clever with master-handoff.

---

## 12. Open questions for qarl

1. **MVD scope confirmation.** Is "standalone Rust binary plays FREE YOUR SIGN at 30 fps on dev Pi, with shader transitions, no Python in the loop" the right MVD milestone (Step 5 complete)? Or do you want to see it via the Python sidecar path (Step 7 complete) before counting it as "real"?

2. **Cargo workspace shape.** I'm proposing `renderer/` at the repo root as a single Rust crate with multiple binary targets if needed. Acceptable, or do you want it under `backend/renderer-rs/`? My preference: top-level. Easier to reason about.

3. **`rustup` on the Pi during first-time provisioning.** Adds ~5 min and ~600 MB to flash-day. Acceptable, or do we want a pi-gen recipe pre-baking Rust into the image?

4. **Health metric exposure.** The Python `/api/health` should surface the Rust renderer's frame-time p99 and CMA usage. New endpoint, or stuff into existing one?

5. **MockRenderer fallback when Rust dies.** Confirming: the operator-visible behavior is "screen shows the last good frame for ≤2 s, then black until Rust comes back." Acceptable, or do we need a software-rendered fallback (i.e. switch to *software HDMI* via `/dev/fb0`)? My read of spec §9 is "fall back to Mock + log clearly," which aligns with the black-frame behavior.

6. **HUB75/WS2812B snapshot at 64×32.** Different IPC payload size. Confirming the `/api/playback/current-frame` endpoint accepts variable-dim PNGs (the LED snapshot is just smaller). I think yes — the endpoint is dim-agnostic — but confirming.

7. **Step -1 spike kickoff.** Three small Rust prototypes against `openMarqueeDev` over 3 days. Want to greenlight that work before I plan further, or is the rest of the plan good-enough-to-commit even if a spike returns surprises?

8. **The 30-day rollback window.** Sized arbitrarily. Shorter is fine if we soak heavily before flipping; longer is fine if you want extra paranoia. Confirm 30.

---

Ready to implement when {Q1 MVD scope, Q2 crate location, Q7 Step-1 spike approval} are answered.
