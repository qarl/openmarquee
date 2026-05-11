# Renderer Rewrite — Implementation Plan

**Status:** SUPERSEDED by [`renderer-rewrite-plan-rust.md`](renderer-rewrite-plan-rust.md) (2026-05-06 pivot to Rust binary). Kept as historical record.
**Companion to:** [`renderer-rewrite-requirements.md`](renderer-rewrite-requirements.md).
**Author:** Plan subagent, 2026-05-06.
**Audience:** the agent that will write the code. The plan describes
how it would be built; it does not implement.

---

## 1. Architecture overview

**Shape.** One process, one event loop, one renderer object. The
renderer is built around a single `Compositor` interface with
output-specific implementations (HDMI, Mock, HUB75, WS2812B) that all
consume the same canonical "scene description" produced by a shared,
output-independent `SceneBuilder`. Playback never sees compositor
internals; it only calls operations from spec §10.

The HDMI implementation is a **single-pass GLES2 shader compositor on
the vc4 V3D 2.1, with HVS scanout**. There is no multi-plane DRM
compositor and no PIL software per-frame path on the hot loop.
Everything that moves per-frame — slide A's bg/statics/animated text,
slide B's bg/statics/animated text, the 16 transitions, the 4 blend
modes, brightness, gamma — is one fragment shader that samples up to
four 1080p RGBA textures and writes one BGRA framebuffer per vsync.
The HVS handles the final scale/rotate at scanout (no software
rotation).

**Process boundaries.** All inside the FastAPI process. Two threads
only:
- The asyncio main thread (FastAPI, playback, scene math).
- A dedicated **Render thread** that owns EGL/GBM/DRM context and the
  H.264 decoder. It pulls work from a bounded `RenderCommand` queue
  produced by playback. This isolation gives us a single owner for
  every GL/DRM/V4L2 fd so close() is deterministic.

A third short-lived **Decoder thread** wraps `v4l2-request` (or PyAV
with the kernel-side `bcm2835-codec` v4l2_m2m driver) for H.264. It
produces YUV420 frames into a tiny ring buffer (3 frames). The render
thread consumes them.

**Where compositing happens.** GPU only. The only software composite
step is `SceneBuilder` rasterizing per-layer text glyphs once per
slide (cached) into RGBA tiles. The fragment shader does: per-layer
blend (4 modes), per-layer motion sampling (UV warp), full-slide
compose for slide A, same for slide B, transition lerp,
brightness/gamma LUT, output. One pass. One sample-and-write of the
framebuffer.

**Output differences.** HDMI is the only per-frame GPU path. Mock
writes one PNG per second (debounced) using PIL — it shares
`SceneBuilder` and a CPU compositor that mirrors the shader math
(source-of-truth: `composite_with_blend` + Pillow alpha; only used by
Mock and snapshot). HUB75 / WS2812B share the *same* CPU compositor
as Mock — they need ≤256×128 frames, software composite of a 256×128
RGBA buffer is sub-millisecond, no GPU needed. Brightness/gamma is a
per-pixel LUT applied at the end for all paths.

## 2. Module breakdown

```
backend/openmarquee/rendering/
  __init__.py            -- public surface: Renderer ABC + factory + Frame
                            dataclass; this is the ONLY thing playback.py imports
  scene.py               -- SceneBuilder: slide -> Scene (output-independent)
                            owns glyph rasterization + per-slide cache
  scene_cache.py         -- bounded LRU keyed by (slide.id, updated_at)
                            holds RGBA tiles + premultiplied Per-layer atlas
  motion_eval.py         -- pure functions: (layer, elapsed) -> motion params
                            (offset, scale, alpha, jitter); tested on Mac
  blend_math.py          -- pure numpy reference for blend modes; used by
                            CPU compositor + unit tests; mirrors GLSL exactly
  compositor_cpu.py      -- CPUCompositor: Scene -> RGB888 frame
                            used by Mock, HUB75, WS2812B, snapshot endpoint
  compositor_gpu.py      -- GLESCompositor: Scene -> framebuffer via shader
                            Owns: EGL display, GBM device, GL programs,
                            texture pool, brightness/gamma LUT uniform
                            Single-pass shader compiled once per (kind,blend) combo
                            Texture pool is fixed-size: 4 slots, ring-allocated
  shader_program.py      -- GLSL source generator; fixes the per-layer blend
                            into the fragment shader at compile time
                            Produces 16 transition variants; "cut" is a no-op path
  drm_present.py         -- DRMPresenter: thin atomic-commit shim
                            Handles rotation, vsync wait via PageFlip event
                            Owns 1 primary plane only; never overlays; vc4 LBM
                            invariant: only 1 active plane at 1080p ARGB8888
  video_decode.py        -- H264Decoder: PyAV over v4l2_m2m bcm2835-codec
                            Produces (RGBA tex_id, pts, last_frame_flag)
                            Two clients: VideoSlide as primary; TextSlide bg
  output_hdmi.py         -- HDMIRenderer: orchestrates GLESCompositor +
                            DRMPresenter + H264Decoder; runs on render thread
  output_mock.py         -- MockRenderer: writes preview.png at ~1 Hz (worker)
                            via CPUCompositor; used by scripts/dev.sh
  output_hub75.py        -- HUB75Renderer: CPUCompositor + LUT + panel-write
                            stub (NotImplementedError, Phase 8)
  output_ws2812b.py      -- WS2812BRenderer: CPUCompositor + GRB encode +
                            DMA stub (Phase 10)
  budget.py              -- compile-time + runtime memory budget assertions
  metrics.py             -- frame timing histograms, CMA poll, RSS poll;
                            written to /api/health and a rotating in-memory
                            ringbuffer for the soak test
```

What each module exposes:

- `__init__.py` — `Renderer` ABC with the seven §10 operations;
  `make_renderer(settings) -> Renderer`. This is the single import
  surface for `playback.py`.
- `scene.py` — `Scene` (dataclass: list of `LayerTile`s + bg ref +
  auto-mode tags), `SceneBuilder.build(slide, asset_bytes) -> Scene`.
  Pure. Mac-testable.
- `compositor_gpu.py` — `GLESCompositor.render(scene_a, scene_b,
  transition, t, settings) -> None` (writes into the bound DRM
  scanout buffer). Owns nothing playback-visible.
- `output_hdmi.py` — implements `Renderer`. Threads commands across
  the render thread via a small queue.

## 3. Data flow for one slide (HDMI 1080p)

Time T0: playback decides to show slide S with duration 5s,
transition kind=`iris`, transition_ms=500.

1. **Playback** calls `renderer.begin_slide(S, asset_loader)` on the
   asyncio thread. The call enqueues a `BeginSlide` command on the
   render thread's queue and returns immediately. `asset_loader` is a
   callable `(uuid) -> bytes` that the renderer can call from the
   render thread.
2. **Render thread** dequeues `BeginSlide(S)`. It calls
   `SceneBuilder.build(S, asset_loader)`:
   - Asks `scene_cache` for `(S.id, S.updated_at)`. If hit, return
     cached `Scene` (pre-rasterized text RGBA tiles + bg RGBA tile +
     auto-mode tags). Done.
   - On miss: rasterize bg (procedural pattern via PIL, OR load image
     asset bytes, OR mark "video bg, decoder will provide texture").
     For each `TextLayer`, rasterize the text into a *minimal-bbox*
     RGBA tile (premultiplied). Stash all tiles in the cache.
3. Render thread uploads each tile to a GL texture via
   `glTexSubImage2D` into the **fixed texture pool** (slots 0–7,
   ring-allocated, never resized). If a slot's existing texture is
   the right dims, reuse — this is the leak-fix lesson from the prior
   shader compositor (the 247 MB CMA pin came from `glTexImage2D`
   reallocating). Bg goes to slot 0; layers go to 1..N.
4. Playback calls `renderer.advance(now)` on every tick (~33 ms at
   30 fps). The render thread, on each vblank-aligned tick:
   - Computes `elapsed = now - T0`.
   - Re-evaluates auto-mode for any layer flagged `auto_mode != None`
     if the displayed string would change (it polls cached strings
     keyed on minute/second/day; on a change, it re-rasterizes that
     ONE layer's tile and `glTexSubImage2D`s into its slot — same
     dims, no reallocation).
   - Calls `motion_eval` per layer to get `(offset, scale, alpha,
     jitter)` for `elapsed`. This becomes a 4-vec uniform per layer.
   - Calls `GLESCompositor.render(scene_a=S, scene_b=None,
     transition=None, t=0)`.
     - The shader samples up to 8 layer textures (steady state has
       only one Scene), applies per-layer blend mode, motion warp,
       opacity. Outputs RGBA into the GBM-backed surface.
   - `DRMPresenter.commit()` does an atomic page flip with rotation +
     brightness/gamma LUT applied as final ALU in the same fragment
     shader (no second pass).
5. Time T0+4500: playback calls `renderer.begin_transition(S→T,
   kind=iris, dur=500)`. Render thread starts building Scene T (cache
   hit if T was preloaded). The shader compositor binds slide A's
   textures *and* slide B's textures simultaneously; the shader's
   transition kernel ($u_t$) interpolates. **Motion continues**:
   per-layer motion uniforms keep advancing for both A and B, sampled
   inside the same fragment shader.
6. Time T0+5000: transition complete. Render thread frees nothing
   (textures are pool-resident). Slot allocations rotate so T's
   textures take A's recently-used slots first.
7. **Asset bytes**: loaded by `asset_loader` (storage layer) on first
   cache miss only. Image bytes are decoded by Pillow on the render
   thread (one-time cost). Video bytes never move through Python —
   `H264Decoder` opens the MP4 path directly via PyAV and pushes
   decoded YUV frames into a YUV→RGB GL texture using the v4l2_m2m
   sink directly when possible.
8. **Snapshot**: a separate `renderer.capture_png()` call enqueues a
   `Capture` command. On the render thread, the most recent rendered
   RGB frame (kept in a 1-deep ringbuffer in CPU memory after each
   commit — copied via `glReadPixels` ONCE every 60s, gated) is
   encoded to PNG. If the cache hit is fresh enough, we serve it
   without a readback. Never blocks playback.

## 4. The seven trickiest design problems

### 4.1 Pi Zero 2 W memory budget

**Budget table (proposed, must validate on hardware before writing
the compositor):**

| Region | Budget (MB) | Notes |
|---|---|---|
| Backend Python heap (FastAPI, asyncio, content/playlist) | 80 | measured today |
| `SceneCache` RGBA tiles: bounded LRU at 64 MB | 64 | hard cap; eviction by LRU not size |
| H.264 decoder ring (3× YUV420 1080p) | 9 | 3.1 MB/frame YUV420 |
| GLES texture pool (8 slots × 1080p RGBA, in CMA) | 64 | fixed pool, never reallocated |
| GBM scanout chain (2 buffers × 1080p XRGB) | 16 | double-buffer, no triple |
| Other DRM/CMA overhead | 16 | atomic-commit blobs, framebuffer metadata |
| Glyph atlas | 8 | shared FreeType + cached glyph tiles |
| Headroom for FastAPI burst, snapshot encode | 30 | |
| **Total userspace + CMA** | **287** | of 416 RAM (256 CMA cap is the binding wall) |
| **CMA share of total** | **~96** | well under 256 MB |

The dead prior architecture pinned 247/256 MB of CMA before the first
transition because `gpu_compositor.py` used a GBM dumb-buffer pool
that allocated per slide attach rather than a fixed pool. Lesson:
**all GBM/CMA allocations are made at `open()` and never grow at
runtime**. We *prove* this by exposing `metrics.cma_used()` and
asserting it is monotonic-equal across the soak test (not
monotonic-increasing).

How we prove it: a `metrics.budget_assert()` runs every 30s during
the soak, reads `/proc/meminfo` `CmaUsed`, `/proc/self/status`
`VmRSS`/`VmData`/`VmSwap`, and writes them to a ringbuffer. The
assertion fails the soak if any of those values increases by more
than 4 MB above its 30-minute baseline.

### 4.2 Motion-through-shader-transitions

**Approach:** unify everything in one fragment shader. The shader's
signature is:

```glsl
uniform sampler2D u_a_bg;            // slide A background
uniform sampler2D u_a_layers[6];     // up to 6 text layer tiles (premultiplied)
uniform vec4      u_a_motion[6];     // (dx, dy, scale, alpha) per layer
uniform vec4      u_a_geom[6];       // layer's UV box in slide-space
uniform int       u_a_blend[6];      // blend mode enum per layer
uniform sampler2D u_b_bg, u_b_layers[6];   // same for slide B
uniform vec4      u_b_motion[6], u_b_geom[6];
uniform int       u_b_blend[6];
uniform float     u_t;               // transition progress 0..1
uniform int       u_kind;            // transition enum
uniform vec3      u_lut;             // brightness, gamma_inv, exposure
```

The fragment shader does:
1. `compose_one(slide, uv) -> vec4`: starts with bg, then loops the 6
   layers in order: warp uv by motion offset, sample tile, branch by
   blend mode (4-way switch). This is **one function inlined twice**
   in the shader source — once for A, once for B.
2. `transition(a_color, b_color, kind, t) -> vec4`: switch over 16
   transition kinds. Most transitions are `mix(a, b, f(uv, t))` for
   some kind-specific `f`. The pattern compiles small.
3. Apply brightness * pow(channel, gamma_inv).

**Cost.** vc4 V3D 2.1 has ~16 GFLOPS. The 8-input blend ladder in the
prior shader compositor was GPU-bound at 8.6 fps because it did 8
layer samples + 8 blend kernels. We have at most 6 layers per side ×
2 sides = 12 samples, plus 2 bg = 14 samples per fragment. That is
**4× more than the prior 2-input transition shader** (~600 MOps/sec).
Linear extrapolation: ~2.4 GOps/sec at 1080p × 30 fps. That's 15% of
the ALU budget; samples are the binding constraint.

**Bandwidth budget**: 14 samples × 4 bytes × 1920×1080 × 30 fps =
3.5 GB/s — exceeds the ~1.5 GB/s effective DDR bandwidth. We must do
one of two things:

1. **Conditional sampling**: in the shader, use `u_a_geom` to test
   whether the current fragment is even inside the layer's box, and
   short-circuit the texture sample for the ~70% of pixels that
   aren't. With realistic layouts (2-3 active layers each side),
   effective sample rate drops to ~6 samples/fragment = 1.5 GB/s.
   Tight but feasible at 30 fps.
2. **Tile the fragment shader's work**: compose A and B in two
   intermediate FBO writes at half-rez or via `glScissor`. This is
   the unhappy path that doubles bandwidth.

**Plan**: attempt approach 1 first. The honest answer is this **may
not hit 30 fps with all 6 animated layers per side at 1080p with
conditional sampling alone**. If it doesn't, fallback is the
spec-authorized split: motion runs on a background CPU-prerendered
"static + motion-baked-this-frame" layer, transition shader takes
only 2 inputs (slide-A-now, slide-B-now). This is exactly the prior
architecture. The unifying shader is a stretch goal; the split is the
failsafe.

We **must** measure on real hardware before committing further. Build
a 200-line GLSL prototype in week 1 of the build order, run it on
`openMarqueeDev`, and observe.

### 4.3 H.264 video decode

**Decoder**: PyAV (libav bindings). The Pi Zero 2 W kernel ships
`bcm2835-codec` as a v4l2_m2m device at `/dev/video10`. PyAV wraps
libav, which can use V4L2 M2M decoders via `h264_v4l2m2m`. This
decoder produces YUV420 frames in **CMA buffers via dmabuf** that we
import directly as a GL texture using
`EGL_EXT_image_dma_buf_import`.

**Position in pipeline**: `H264Decoder` is owned by the render thread
but feeds a 3-frame ring. When a `VideoSlide` is the active slide,
`SceneBuilder` puts a `VideoTextureSlot` in slot 0 (the bg slot); the
GLESCompositor binds the latest decoded frame's GL texture id to
`u_a_bg` each frame. When a `TextSlide` references a video as its bg
(`background_video_slide_id`), the same path is used — the bg slot is
video-fed instead of static-image-fed.

**Frame production**: decoder runs at the source MP4's framerate
(capped at 30 fps), pushing into the ring. The render thread reads
the most-recent frame (drop late frames). When MP4 ends and
`duration_ms` hasn't expired, the ring's last frame holds; subsequent
reads keep returning that texture id (no decoder activity).

**Memory**: the dmabuf import means YUV frames live in CMA, not
Python heap. GLES2 doesn't natively sample YUV — we need either
`EGL_EXT_yuv_surface` (vc4 supports `DRM_FORMAT_YUV420`) or a small
YUV→RGB conversion shader run as a single-pass FBO write. The FBO
write is a one-time cost per decoded frame (~3 ms at 1080p) and is
the simplest correct path. The RGB output FBO is allocated ONCE at
decoder open, reused for every frame.

**Fallback if PyAV+v4l2m2m proves flaky**: shell out to
`ffmpeg -c:v h264_v4l2m2m -pix_fmt rgba -f rawvideo - | python_pipe`.
Slower (extra copy through user pipe) but proven. We won't ship this;
it's a debug fallback.

### 4.4 Four blend modes in single-pass GLES2

The math is well-known (see `blend.py`). In the fragment shader's
per-layer compose loop:

```glsl
vec4 base = composed_so_far;          // RGB premultiplied, A in [0,1]
vec4 top  = texture2D(layer_tex, uv); // RGBA premultiplied
int  mode = u_blend[i];               // 0..3

vec3 blended;
if      (mode == 0) blended = top.rgb;                                     // normal (premul source-over below)
else if (mode == 1) blended = base.rgb * top.rgb;                          // multiply
else if (mode == 2) blended = base.rgb + top.rgb - base.rgb * top.rgb;     // screen
else /* overlay */  blended = mix(2.0*base.rgb*top.rgb,
                                   1.0 - 2.0*(1.0 - base.rgb)*(1.0 - top.rgb),
                                   step(0.5, base.rgb));
// Source-over with the blended RGB, gated by top's alpha:
vec3 out_rgb = mix(base.rgb, blended, top.a);
float out_a  = top.a + base.a * (1.0 - top.a);
composed_so_far = vec4(out_rgb, out_a);
```

**Numerical points:**
- All ops in [0,1] linear-ish space; we don't go to true sRGB linear
  because the cost of the gamma trip per layer is 8 fma's × 6 layers
  (prohibitive). Operators won't see the difference at signage scale.
- We use `step(0.5, base.rgb)` (not branching) for overlay because
  `if` in GLES2 fragment shaders doesn't always reduce to predicated
  execution on vc4.
- Premultiplied input means transparent regions of layers don't
  bleed black; this matches how the CPU path's `composite_with_blend`
  is being asked to behave.
- The 4-way `if` chain produces 4 ALU paths in the compiled shader;
  we accept the divergence cost. For better performance later we
  could **specialize the shader per-slide** (compile a variant with
  only the blend modes that slide actually uses) but that's a
  Phase-2 optimization.

**Cost**: 4 fma's per blend × 6 layers × 2 sides = 48 ALU ops per
fragment. Within budget.

### 4.5 Settings reactivity (rotation/dims/brightness/gamma)

Settings flows through the renderer as an immutable `RenderSettings`
snapshot. `Renderer.reconfigure(new_settings)` is called by playback
when settings change. The render thread:

- **Brightness/gamma**: trivial. They're a 3-float uniform. Updated
  next commit. No reallocation.
- **Rotation**: `DRMPresenter` manages CRTC orientation property.
  Rotation changes are applied via atomic property set; no buffer
  reshape. The shader's UV transform also updates. Cost: ~2 ms.
- **display_width/display_height**: a real change of canvas dims is
  the heavy case — every cached scene tile is invalidated, the GBM
  scanout chain must be reallocated, and the texture pool must be
  reallocated to the new size. We do this on the render thread with
  a hard `pause` from playback: drain command queue, fully `close()`
  the GLESCompositor and DRMPresenter, re-`open()` with new dims,
  `scene_cache.clear()`. The whole flip is bounded by the re-init
  cost (~1.5s on the prior compositor; we target ≤2s per spec §8.5).
  Playback notices via the queue draining and resumes its loop on
  the next iteration.

The key invariant: **`reconfigure` never partially mutates state**.
Either the new settings are fully applied or the renderer rolls back
to the prior state. We do this by constructing a new `GLESCompositor`
instance bound to a new GBM surface, swapping it in atomically, then
closing the old one. This costs us ~64 MB of CMA temporarily during
the swap — we have headroom.

### 4.6 HUB75 / WS2812B path sharing

The contract is: `Renderer` ABC with the §10 seven operations.
`output_hub75.py` and `output_ws2812b.py` are concrete classes that
inherit no GLES code — they share `SceneBuilder`, `motion_eval`, and
`compositor_cpu`, then add their pixel-format encoding. At LED-panel
sizes (typically 64×32 to 256×128) the CPU compositor runs in <2 ms
per frame even with 6 layers — no GPU needed.

The HDMI-specific machinery (GLES, GBM, DRM, V4L2 video decode, the
texture pool) is **only constructed inside `output_hdmi.py`**. The
HUB75/WS2812B paths can run on a Mac dev box with no DRM/EGL
whatsoever. The factory in `__init__.py:make_renderer(settings)`
picks the right concrete class from `settings.output_mode`.

For video on LED panels: the H264Decoder is HDMI-only;
`output_hub75` decodes via PyAV directly to RGB (no v4l2_m2m needed
at 64×32) or just drops video frames and shows a "video not
supported on this output" still. The spec explicitly defers
panel-write code, so this is fine.

### 4.7 No-leak lifecycle guarantee

The leak history is the elephant. Prior code held GBM dumb buffers,
GL textures, and DRM framebuffers in pools that grew with playlist
length. The fix is structural, not patches:

1. **Single ownership.** Every fd, GL handle, DRM blob, and CMA
   buffer is owned by exactly one Python object whose `__del__` is
   *never* relied on. Lifecycle is explicit `open()`/`close()`,
   never `with` alone.
2. **Bounded pools, never-grow.** All texture slots and GBM buffers
   are allocated at `open()` and explicitly destroyed at `close()`.
   The texture pool is exactly 8 slots; the LRU evicts contents
   (uploads new bytes via `glTexSubImage2D`) without changing the GL
   texture identity. **No `glTexImage2D` after open() — only
   `glTexSubImage2D`**.
3. **Explicit close() chain.** `Renderer.close()` calls, in order:
   stop render thread, drain command queue, close H264Decoder (V4L2
   fd + dmabufs), `glDeleteTextures` the pool,
   `glDeleteFramebuffers`, `eglDestroyContext`, `eglTerminate`,
   `gbm_surface_destroy`, `gbm_device_destroy`, `drmDropMaster`,
   `os.close(drm_fd)`. Each step has a try/except — a failing step
   logs but doesn't skip subsequent cleanups.
4. **Tested via test harness.** `test_lifecycle_no_leak` opens and
   closes the renderer 100 times in a row, asserts CMA, RSS, and
   open-fd count return to baseline within ±2 MB / ±5 fds. Runs on
   the Mac as a Mock test; runs on the Pi as an integration test.
5. **Process-level fallback for HDMI.** As a defense in depth, the
   HDMI path can be optionally configured to run in a *subprocess*
   with stdin command pipe. Subprocess death = guaranteed CMA
   reclaim. We DON'T ship with this enabled but we keep it as the
   structural safety net if Stripe-of-fds testing reveals a leak we
   can't pin down. Important: the subprocess path adds an IPC hop
   per command; we accept that only as a last resort.

## 5. Build order

Each step produces a working slice that runs end-to-end on something.
No half-skeletons.

**Step 0 — Strip and seed (1 day).** Delete every file under
`backend/openmarquee/rendering/` except `__init__.py`. Replace
`__init__.py` with the new `Renderer` ABC and `make_renderer`
factory. `make_renderer` returns a stub Mock that produces black
frames. Wire `playback.py` to the new ABC. CI green.

**Step 1 — Mock + SceneBuilder + CPU compositor (3 days).** Implement
`scene.py`, `motion_eval.py`, `blend_math.py`, `compositor_cpu.py`,
`output_mock.py`. Now the dev preview shows real slides with motion +
blend modes + auto-mode. The seed playlist's welcome reel renders to
PNG at 1 Hz on `scripts/dev.sh`. **Minimum viable demo point — first
checkpoint with qarl.** Confirms: scene model works, motion is
correct, blend modes look right, auto-mode reformats correctly,
output is PNG-on-disk so we can A/B compare the old renderer.

**Step 2 — DRMPresenter + GLESCompositor steady-state (4 days).**
Implement `drm_present.py` and a *minimal* `compositor_gpu.py` that
handles one slide (bg + 1 static text layer, no motion, no blend
modes, no transitions). Implement `output_hdmi.py`'s
`begin_slide`/`advance`/`close`. Deploy to `openMarqueeDev`.
Validate: slide appears, no leak across `systemctl restart` 10
times.

**Step 3 — Motion + blend modes in shader (3 days).** Extend the
fragment shader to N layers with per-layer motion uniforms and blend
mode switching. Run the bench. Verify 30 fps on the welcome reel's
animated slides.

**Step 4 — Transitions (4 days).** Add the 16-way transition switch
to the shader; add `begin_transition` to `output_hdmi.py`. Verify all
16 visually match the prior `shader_compositor.py` reference. Verify
motion continues across transitions.

**Step 5 — Video decode (4 days).** Implement `video_decode.py` with
PyAV + dmabuf import. Wire into the bg texture slot. Test with the
demo Blender video. Verify both VideoSlide-as-primary AND
TextSlide-with-video-bg paths.

**Step 6 — HUB75 + WS2812B contract reach (1 day).** Implement
`output_hub75.py` and `output_ws2812b.py` as `output_mock.py`
siblings. Panel-write paths stay stubs.

**Step 7 — Snapshot + reconfigure (2 days).** Implement
`capture_png()` and `reconfigure()`. Wire to
`/api/playback/current-frame`.

**Step 8 — Soak + polish (3 days).** 12-hour soak on
`openMarqueeDev`. Fix any leaks surfaced. Tune pool sizes if CMA
budget is tight.

Total: ~25 working days. Budget 30 to absorb the inevitable surprise.

## 6. Verification strategy

- **Soak test (§8.2).** A `scripts/soak_renderer.py` runs the welcome
  reel for ≥6 hours (target: 12). Every 30s it samples
  `/proc/meminfo:CmaUsed`, `/proc/self/status:VmRSS,VmData,VmSwap`,
  and the open-fd count from `/proc/self/fd`. The test asserts: each
  metric's max-over-the-last-30-min minus min is ≤ 4 MB (RAM) /
  ≤2 fds. The test ALSO asserts that the playback loop has advanced
  at least N slides per minute (no stall). Output is a CSV that gets
  committed alongside the test result for the qarl review.
- **CMA measurement.** `metrics.py` exposes a `/api/health/memory`
  endpoint with the values above. The soak script polls this; no
  separate ssh+cat needed.
- **Motion-not-dropping-frames.** Per-frame `glDraw` start/end
  timestamps recorded in a fixed ringbuffer (last 2000 frames). After
  a 10-minute run with a ticker slide, assert: 0 frames > 40 ms (=
  dropped at 30 fps), p99 ≤ 33 ms. Dump the histogram on test
  failure.
- **Clean close→reopen.** `test_lifecycle_no_leak` (described in
  §4.7). Run on every CI commit (Mac path) and on the deploy script
  (Pi path). 100 cycles, asserted residual leakage of <2 MB / <2
  fds.
- **Visual regression.** A small set of golden PNGs
  (`tests/render/goldens/`) for: each blend mode at 0/0.5/1 alpha,
  each transition at t=0/0.5/1, each motion effect at known phases.
  Generated by `compositor_cpu.py` (which is the source of truth for
  the math). The GLES path is separately validated against these by
  `glReadPixels` after each shader change; tolerance ±2 LSB per
  channel for the bilinear filtering difference.

## 7. Risks and proposed scope cuts

**Honest read on scope cut likelihood: ~50%.**

The single biggest risk is the unified shader pass (§4.2). My
calculation says we're at the edge of vc4's bandwidth budget with 14
samples/fragment at 1080p × 30 fps. Conditional sampling buys us back
enough headroom **if real slides have ≤3 active layers per side**,
which the welcome reel does, but operators *can* construct slides
with more. The prior shader compositor failed at 8 layers at 8.6 fps;
we'd be at 12 layers (6 each side × 2 sides counting bg) for a
worst-case during transition.

**My priority order if we have to cut, first to last:**

1. **Cut the unified shader to a split (motion runs on a 2nd FBO,
   transition takes 2 fully-composed inputs).** Same architecture as
   the deleted shader compositor. We give up the elegance of "one
   shader pass" but keep all 16 transitions, all 4 blend modes, real
   video, and 1080p. The CMA budget actually gets *easier* under the
   split because the shader only ever has 4 textures bound
   (slide-A-now, slide-B-now, gradient/ramp, output FBO).
2. **Cut transition count.** Drop the 4 most-expensive (glitch,
   halftone, marquee, dissolve). Keep 12.
3. **Cut motion-during-transitions.** Freeze motion for the 500ms
   transition window. The spec calls this a "split fallback is
   acceptable."
4. **Cut to 720p**. Spec says no, but if 1, 2, 3 don't close the
   budget, this is the last lever. 720p halves bandwidth.

I'd present option 1 to qarl in week 1 if the prototype confirms the
shader is bandwidth-bound. The split fallback is a known-good
architecture; we have all the code to reference even if we delete
it.

Secondary risks:

- **PyAV + v4l2_m2m + dmabuf import is a thin path.** If any of
  those three doesn't compose cleanly (especially the
  `EGL_EXT_image_dma_buf_import` part on Mesa 25.0.7), we fall to
  the FFmpeg-pipe shell-out, and video decode adds 30+ ms latency.
  Acceptable but inelegant.
- **Atomic DRM commits with rotation may not actually be atomic on
  vc4.** vc4 reroutes legacy SetCrtc internally, but I haven't
  verified the rotation property is supported on the primary plane.
  Worst case we rotate inside the fragment shader (free ALU) and
  present unrotated.

## 8. Open questions for qarl

1. **`max_layers_per_slide` cap.** The shader compiles with a fixed N
   (proposing 6). Real slides with more layers fall back to CPU
   composite. Is 6 enough for the operator-facing UX, or should I
   push to 8 and accept the bandwidth tightening?
2. **Snapshot frequency.** Spec says "at most 1 capture per 5 minutes
   per playlist, plus on playlist change." Confirm: is sub-second
   snapshot freshness ever needed (e.g. for the live-preview UI in
   the editor)? If yes, the design accommodates by keeping a rolling
   1-deep readback ring; if no, we save the readback cost.
3. **What's the right behavior for a blend mode on a layer with
   motion=ticker?** The shader does support it (sample warped UV,
   apply blend), but visually it composites a wrapping ticker through
   "multiply" — which is intentional but unusual. Confirm operators
   can construct this and we render it without complaint.
4. **`output_mode` switch at runtime.** Spec §6.3 settings reactivity
   says rotation/dims/brightness/gamma must be reactive. It doesn't
   say `output_mode`. Today switching output_mode requires a process
   restart. Confirm we can keep it that way (cleaner lifecycle) or
   whether mid-playback `output_mode` switching is a v1 requirement.
5. **Snapshot for the LED-panel paths.** `/api/playback/current-frame`
   makes sense for HDMI but on a 64×32 HUB75 the current-frame is
   just the panel's tiny RGB. Same code path or HDMI-only?
6. **Acceptable CPU cost during a settings reconfigure.** The "≤2
   second" reconfig in §8.5 is fine for dims changes but trivial
   (≤50 ms) for brightness/gamma. Confirm we can fast-path the
   trivial cases and only do the full re-init on actual canvas dim
   changes.
7. **What's the cold-start budget actually counting?** "≤4s from
   process start to first frame" — does "first frame" include the
   seed slide rasterization (~200 ms) or only the first pixel
   hitting HDMI? Affects whether we can lazily build SceneCache or
   have to pre-warm.
8. **Memory budget verification path.** I'm proposing the soak runs
   on `openMarqueeDev` overnight. Is there a CI/automation hook for
   "this build was approved by an X-hour soak" that I should write
   to, or does qarl manually review the soak CSV?

---

Ready to implement when {Q1 layer cap, Q4 output_mode reactivity, Q7
cold-start scope} are answered.
