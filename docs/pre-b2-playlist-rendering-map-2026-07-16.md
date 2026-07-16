# Pre-#B2 Playlist-Rendering Map (2026-07-16)

**Author:** Jimmy-openmarquee-code · **For:** admin + code2 (Colorlight-side owner)
**Type:** DISCOVERY (structural map, NOT a design proposal or #B2 decision)
**Base:** `origin/main @ 8714c55` — **includes PR #95** ("Option S" Colorlight
pump + real AF_PACKET `PacketSink` + IPC wiring), which merged *after* this
dispatch was written. The dispatch's premise (a pump streams `produce_frame()`
bytes) is **now real on main** — all file:line refs below are against `8714c55`.
(The initial discovery pass ran against `3e8de34` = pre-#95; refs are re-verified
against `8714c55` throughout.)

---

## 0. TL;DR — the one structural fact that frames #B2

**#95 forks TWO independent paths at the IPC Open gate, and they share no paint
code today.** `ipc_main.rs:1959`:

```
if params.output != "hdmi" && params.output != "colorlight" { <reject> }
...
if params.output == "colorlight" { return run_colorlight_stream_pump(&params); }  // ipc_main.rs:1973-1974
```

- **`output=hdmi`** → `run_open_and_inner_loop_linux` (`ipc_main.rs:2504`): the
  real playlist paint loop — long-lived `EglSession`, driven Advance-by-Advance
  by the Python `PlaybackLoop`, painting text/image/video slides + transitions to
  DRM scanout.
- **`output=colorlight`** → `run_colorlight_stream_pump` (`ipc_main.rs:2256`): a
  self-paced loop that calls `Compositor::produce_frame()` → `encode_to_sink` →
  `PacketSink` (AF_PACKET) to the card, at ~20 Hz, until SIGTERM. Its `Compositor`
  produces **TEST PATTERNS only** (`CpuCompositor`/`HeadlessGpuCompositor`), and
  it is **completely disconnected from the playlist state machine** — no
  `BeginSlide`, no `Advance`, no per-slide content.

**#B2 is the bridge:** make the pump's `Compositor` paint the *current playlist
slide* instead of a test pattern. Everything below maps the two sides of that
bridge + the coupling that makes it non-trivial.

The good news, established by the discovery: **the paint code is already
substantially decoupled from scanout** — `paint_slide`/image/video bake into a
*generic bound FBO* with zero DRM references; only the ~40-line "present tail" of
each wrapper is HDMI-specific. So #B2 is a *plumbing + context-lifecycle* problem,
not a "rewrite the renderer" problem.

---

## 1. Q1 — Where playlist content renders today

**Sequencing is 100% Python; the Rust renderer paints one slide at a time on
command.**

- **Python `PlaybackLoop`** (`backend/openmarquee/playback.py:211`, loop at `:659`)
  owns *which* slide is current, *which* is next, and *when* to advance. It
  resolves the schedule→playlist (`playback.py:2111`), holds the ordered item
  list, and drives the sidecar frame-by-frame.
- **Content handoff = ID + timing, renderer PULLS from disk.** Python sends
  `begin_slide(slide_id, t0_ms, duration_ms)` (`rendering/rust_renderer.py:642`);
  the sidecar loads the slide's assets from the `content_root` given once at Open
  (`rust_renderer.py:624`). "slide/transition frame bytes never cross the process
  boundary" (`rust_renderer.py:30-33`). **Exceptions:** StreamSlide + live takeover
  push RGB888/NV12 bytes over a binary pipe (`render_frame`, `rust_renderer.py:817`;
  loop `playback.py:1781`); web slides render to a PNG on disk then play as image.
- **The Rust paint chain (per Advance):** `handle_inner_request` →
  `IpcRequest::Advance` arm (`ipc_main.rs:4562`) → pure state machine
  `state.advance(t_ms)` (`playback.rs:204`) → returns `AdvanceCommand`
  (PaintSlide / PaintTransition / SlideComplete / Idle, `playback.rs:103`) →
  `run_paint_hook` (`ipc_main.rs:3105`) turns it into GL draws by dispatching to
  the per-kind `hdmi::paint_and_present_one_*` helpers (slide `ipc_main.rs:3412`,
  video `:3503`, transition `:3962`).
- **Text/image/video = three bake sources, ONE mechanism.** Each `paint_and_present_*`
  bakes its source into the currently-bound FBO then runs the shared scanout tail.
  Text: `paint_slide` (`hdmi.rs:15678`); image: `blit_cached_image_slide_to_current_fbo`
  (`hdmi.rs:9797`); video: `bake_video_slide_to_current_fbo` (`hdmi.rs:10162`).

## 2. Q2 — What compositor abstraction the paint assumes

**The paint primitives are framebuffer-agnostic; the scanout context is a
separate, HDMI-shaped object.**

- **Paint = GL-into-bound-FBO.** `paint_slide(gl, mode_w, mode_h, bg, text_layers…)`
  (`hdmi.rs:15678` → `paint_slide_with_viewport` `:15706`) references **no DRM,
  GBM, EGL, or Card** — it sets a viewport and draws into whatever FBO is bound.
  Image/video bakes are the same shape.
- **The scanout context = `EglSession`** (`hdmi.rs:429`): borrows the EGL/GBM
  handles (`EglHandles`, `egl_bringup.rs:108`) and *fuses* them with (a) DRM
  identity — `crtc_handle`/`connector_handle`/`mode` (`hdmi.rs:435-437`), (b)
  per-session GPU **caches** — `image_bg_cache`/`poster_cache`/`image_slide_tex_cache`
  (`hdmi.rs:457-467`), and (c) triple-buffered scanout state + EGL fences
  (`scanout_prev/current/prev2_*`, `hdmi.rs:478-525`). Created by
  `run_in_egl_session`/`with_egl_session` (`hdmi.rs:868`) via
  `egl_bringup::bring_up_egl` with the `for_drm_scanout` spec (`egl_bringup.rs:73`).
- **The Colorlight target abstraction = the `Compositor` trait**
  (`colorlight_compositor.rs:130`): `produce_frame() -> Result<Vec<u8>>`
  (`:135`) returning **card-native RGB888, exactly `w*h*3` bytes** (128×96 default,
  `:52`). Two impls: `CpuCompositor` (`:154`, tiny_skia test patterns, cross-platform)
  and `HeadlessGpuCompositor` (`colorlight_gpu_compositor.rs:52`, Linux) — which
  brings up its **own SEPARATE headless EGL context** via
  `for_headless_compositor` (`egl_bringup.rs:87`; XRGB8888 / RENDERING-only /
  no-swap vs HDMI's ARGB8888 / SCANOUT / swap-0) and today only `glClear`s a solid
  color + `glReadPixels` (`colorlight_gpu_compositor.rs:137`), rejecting patterned
  input.

**Key:** `HeadlessGpuCompositor` owns only bare `EglHandles` — a `gl` context, no
caches, no scanout, no paint entry points. The playlist paint code needs the
caches; it does not need the scanout.

## 3. Q3 — The backend↔renderer IPC surface

- **`IpcRequest` enum** (`renderer/src/playback.rs:280`, 13 ops / 15 variants):
  Open, BeginSlide, Advance, BeginTransition, Capture, Reconfigure,
  BeginExternalFrames, Close, ProfileStart, ProfileDump, PreloadSlide,
  RenderSystemCard, ClearSystemCard.
- **Open gate** (`ipc_main.rs:1959`): now `hdmi` **or** `colorlight` (was hdmi-only
  pre-#95). `colorlight` short-circuits to the pump before any EglSession is made.
- **Cadence:** Python owns the frame clock — one `advance(t_ms)` per tick at ~30 Hz
  against a monotonic clock (`playback.py:1357,1470,1595`). The renderer inner loop
  **blocks on `read_line`** (`ipc_main.rs`, in `run_open_and_inner_loop_linux`) —
  message-driven, not self-paced. The **Colorlight pump self-paces** at
  `OPENMARQUEE_COLORLIGHT_TARGET_HZ` (default 20, `ipc_main.rs:2265-2270`) via
  sleep-to-cadence (`:2380`+) — an independent clock, disconnected from Advance.
- **`BeginExternalFrames`** (`ipc_main.rs:2789` → `run_external_frame_pump`
  `:2103`): the **existing "external producer" seam** — it bypasses the slide state
  machine and blits length-prefixed RGB888/NV12 frames from an inherited binary FD
  straight to the HDMI scanout (`hdmi::paint_and_present_external_frame`). This is
  how StreamSlide/live already inject pixels. It is the closest existing analog to
  "someone else produces frames," but note it targets *HDMI scanout*, not the
  Colorlight pump.
- **Python HDMI coupling to fork around:** `output="hdmi"` is effectively
  hard-coded (`settings.py:49` `OutputMode = Literal["hdmi"]`; `rust_renderer.py:472`
  default, never overridden by `dependencies.py:539`); 1920×1080 defaults "match
  HDMI" (`settings.py:264`); and the whole `HdmiAudioHelper` vc4hdmi-ALSA audio
  path is woven through the loop (`playback.py:400-410,751-789,1609-1629`). BUT the
  hot path negotiates `width`/`height` from the Open response
  (`rust_renderer.py:291-298`), so panel size already flows through.

## 4. Q4 — What "playlist rendering into the headless context" requires

The pump calls `compositor.produce_frame()` (`ipc_main.rs:2401`). To make that
produce a *painted playlist slide* rather than a test pattern, the compositor must
run the real bake code (`paint_slide`/image/video) into its headless FBO, then
read back. Five concrete requirements the code imposes:

- **(A) Decouple paint from `EglSession`'s scanout fields.** The bake helpers are
  already `gl`-only, but they are *reached* through `EglSession` methods that also
  carry the caches + scanout. Either (i) build a **headless `EglSession` variant**
  that has the caches but no CRTC/scanout and terminates in `glReadPixels` instead
  of the present tail, or (ii) refactor the bake helpers to take a `(gl, caches)`
  context abstraction independent of scanout. `render_one_frame_in_session`
  (`hdmi.rs:1879`, takes a `FnOnce(&gl, w, h)` draw closure) already shows the
  "draw into FBO through a closure" shape a headless variant would reuse.
- **(B) One-EGL-context-per-thread.** Thread-local + process-global GL caches
  (`MSDF_ATLAS_OWNED` `hdmi.rs:734`; program caches `:13102`; shared VBOs `:9151`)
  key GL handles to "the current context on this thread." A second concurrent EGL
  context *on the same thread* would hand context-A handles to context-B. →
  headless and HDMI must not paint concurrently on one thread (separate threads,
  or one-at-a-time), or the caches need per-context scoping.
- **(C) Video decoder is single-drain `&mut`.** A V4L2 decoder yields exactly one
  CAPTURE sample per call, and the code explicitly forbids two `&mut` to the same
  decoder (`ipc_main.rs:3615-3626`). → you cannot naively drive the same decoder
  twice (once for HDMI, once for headless) in the same tick. The shape must be
  **bake once, fan out** the resulting RGBA to both outputs.
- **(D) Y-flip.** `glReadPixels` is bottom-up; the encoder is top-down — invisible
  for solids but mandatory for real content (`colorlight_gpu_compositor.rs:192-203`).
- **(E) Dimension policy.** Headless is card-native 128×96; the paint pipeline lays
  out at `mode_w × mode_h` (1080p logical). Geometry is ratio-based so it *adapts*
  to any size with **no paint-code change** (`hdmi.rs:15741`) — but text tuned for
  1080p is unreadable at 128×96 (MSDF cells baked at fixed px, `hdmi.rs:1042`). #B2
  must decide: paint at card-native scale, or paint large + downscale. This is a
  content-design decision, not a coupling.
- **Concurrent-vs-replace + cadence:** two `EglSession`s (HDMI + headless) OR a
  headless-only session when `output=colorlight`. Cadences are independent today
  (HDMI ~30 Hz Advance-driven; pump 20 Hz self-paced) — coupling them, or driving
  the pump from playlist state, is a design choice #B2 must make.

## 5. Q5 — Natural insertion points (where the redirect cleanly attaches)

Both independent discovery passes converged on the **same seam**:

- **Split the DRM/GBM present tail from the FBO bake.** Keep the per-type
  bake-into-FBO helpers unchanged (`paint_slide` `hdmi.rs:15678`,
  `blit_cached_image_slide_to_current_fbo` `:9797`, `bake_video_slide_to_current_fbo`
  `:10162`); make the *present* a seam so **either** an HDMI scanout **or** a
  headless `glReadPixels` compositor consumes the same baked FBO. The present tail
  to extract is the ~40 lines at the end of each `paint_and_present_*` — canonical
  example `hdmi.rs:4316-4346`: `eglSwapBuffers` → `lock_front_buffer` →
  `add_framebuffer` → `commit_fb` (`hdmi.rs:1438`) → `rotate_scanout_3_deep`
  (`hdmi.rs:1651`).
- **At the Compositor:** `HeadlessGpuCompositor::produce_frame` (`colorlight_gpu_compositor.rs:137`)
  would run the bake closure into its FBO then `glReadPixels` — instead of the
  current `glClear`. Needs the bake code callable with `(gl, w, h, content-from-disk,
  caches)`.
- **At the pump (the biggest bridge):** `run_colorlight_stream_pump` (`ipc_main.rs:2256`)
  already calls `compositor.produce_frame()` every tick — but it has **no idea
  which slide is current or what time it is.** For real content, the pump needs
  playlist state: either it shares the `PlaybackState` (`playback.rs`) that the
  HDMI loop advances, or it receives Advance-like ticks. Today the two paths don't
  even share a process code path after the Open fork. **This — connecting the pump
  to playlist sequencing — is the largest architectural question #B2 must answer,
  and it is not addressed by the "just paint into a headless FBO" framing.**

## 6. Q6 — Coupling that complicates the redirect

- **DRM page-flip pacing is the HDMI loop's timing source.** `commit_fb` drains
  the prior frame's page-flip before issuing the next (`hdmi.rs:1447`), i.e. vsync
  paces the loop. Colorlight has **no vblank** — the pump self-paces on a timer
  instead. Not a blocker for a headless bake, but it means the two paths pace on
  fundamentally different clocks.
- **Triple-buffer + EGL fence lifecycle** (`rotate_scanout_3_deep` `hdmi.rs:1651`,
  fence waits `:1667`) is scanout-only; a headless path drops it (`glFinish` +
  `glReadPixels`).
- **`EglSession` is a monolith** fusing GL + caches + DRM scanout (`hdmi.rs:429-547`).
  The clean refactor splits it into a GL+cache half (reusable headlessly) and a
  DRM-scanout half. The recently-landed `Compositor` trait + `egl_bringup` split
  already mark where that seam is intended to go.
- **Thread-local GL caches** (Q4-B) — one-context-per-thread.
- **Video `&mut` single-drain decoder** (Q4-C) — bake-once-fan-out.
- **Sequencing lives in Python + drives HDMI via Advance** (Q1/Q3) — the Colorlight
  pump has no equivalent playlist driver; something must feed it "current slide at
  time T."

## 7. The #B2 design surface (structural facts only — NOT a proposal)

The facts support (at least) two candidate shapes; recording them for the design
conversation, endorsing neither:

- **(i) Headless-session compositor** — the pump's `Compositor` becomes a headless
  `EglSession` variant (caches, no scanout) that runs the same bake helpers +
  `glReadPixels`. Playlist state must reach the pump (share `PlaybackState`, or
  feed it Advance-like ticks). This maps to code2's noted insight ("run a SECOND
  paint pipeline in the headless GBM+EGL context"). Enables real content on
  Colorlight; the work is the `EglSession` split (Q4-A), the dim policy (Q4-E), the
  threading discipline (Q4-B), and — the hard part — wiring playlist sequencing to
  the pump (Q5).
- **(ii) Two concurrent sessions painting one playlist** (HDMI + headless
  simultaneously, e.g. a sign that is both HDMI and Colorlight) — requires
  bake-once-fan-out for video (Q4-C), cross-context cache discipline (Q4-B), and a
  shared frame clock. Heavier; only needed if simultaneous dual-output is a goal.

**Smallest first step the map suggests** (again, not a decision): because the
paint→FBO helpers are already scanout-free and `HeadlessGpuCompositor` already
brings up a headless context, the tractable seam is *(A) split the present tail
out of one `paint_and_present_*` + (B) give `HeadlessGpuCompositor` a bake-then-
readback `produce_frame` for a single static slide*, deferring the
pump↔sequencing wiring (Q5) to a follow-up. That isolates the graphics-context
work from the harder playlist-driver work.

## 8. Appendix — file:line index (verified against `8714c55`)

| Fact | Ref |
|---|---|
| Open gate (hdmi\|colorlight) | `ipc_main.rs:1959` |
| Colorlight fork → pump | `ipc_main.rs:1973-1974` |
| `run_colorlight_stream_pump` | `ipc_main.rs:2256` |
| pump: `produce_frame` / `encode_to_sink` per tick | `ipc_main.rs:2401` / `2409` |
| pump self-pace (20 Hz env) | `ipc_main.rs:2265-2270`, loop `2380` |
| HDMI inner loop | `ipc_main.rs:2504` |
| `run_paint_hook` (OpResult→draws) | `ipc_main.rs:3105` |
| Advance handler | `ipc_main.rs:4562` |
| per-kind paint dispatch (slide/video/transition) | `ipc_main.rs:3412 / 3503 / 3962` |
| same-decoder `&mut` guard | `ipc_main.rs:3615-3626` |
| `BeginExternalFrames` seam / pump | `ipc_main.rs:2789` / `run_external_frame_pump 2103` |
| `IpcRequest` enum | `playback.rs:280` |
| `state.advance` / `AdvanceCommand` | `playback.rs:204` / `103` |
| paint primitive (text) | `hdmi.rs:15678` (`paint_slide_with_viewport 15706`) |
| image bake helper (def) | `hdmi.rs:9797` (call site `5034`) |
| video bake | `hdmi.rs:10162` (dmabuf `10347`, mmap `10430`) |
| transition blend (one FBO) | `hdmi.rs:5999`, target `7294` |
| present tail (canonical) | `hdmi.rs:4316-4346` |
| `commit_fb` / page-flip drain | `hdmi.rs:1438` / `1447` |
| `rotate_scanout_3_deep` + fence | `hdmi.rs:1651` / `1667` |
| `EglSession` struct | `hdmi.rs:429-547` |
| thread-local GL caches | `hdmi.rs:734, 13102, 9151` |
| `EglHandles` / `bring_up_egl` | `egl_bringup.rs:108` / `131` |
| specs: scanout vs headless | `egl_bringup.rs:73` / `87` |
| `Compositor` trait / `produce_frame` | `colorlight_compositor.rs:130` / `135` |
| `CpuCompositor` | `colorlight_compositor.rs:154` |
| `HeadlessGpuCompositor` / `produce_frame` | `colorlight_gpu_compositor.rs:52` / `137` |
| headless Y-flip caveat | `colorlight_gpu_compositor.rs:192-203` |
| Python `PlaybackLoop` / loop | `playback.py:211` / `659` |
| `begin_slide` (id+timing, disk-pull) | `rust_renderer.py:642` / `624` |
| output hard-coded hdmi | `settings.py:49`, `rust_renderer.py:472`, `dependencies.py:539` |
| HDMI audio coupling | `playback.py:400-410, 751-789, 1609-1629` |

---

*Discovery only. No code/design/PR produced. The map is durable-capture-worthy
(like the #92 audit); it lives here as scratchpad notes pending admin's call on a
`code/docs/` home for code2.*
