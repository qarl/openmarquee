# Renderer Rewrite — Requirements

**Status:** draft, awaiting qarl review.
**Audience:** the agent that will design and implement the new renderer
from a clean slate.
**Goal:** capture *what* the renderer must do. Not *how*. The
implementing agent owns architecture, data structures, IPC, scheduling,
and buffer management. They are free to ignore every line of code under
`backend/openmarquee/rendering/` and start over.

---

## 1. Why we're rewriting

The current renderer is a 6,000-line two-path stack (multi-plane DRM/KMS
compositor + EGL/GLES2 shader compositor) that has accumulated three
architectural attempts and still leaks memory under shader-transition
load on the canonical hardware target. Three rounds of bounded-cache
fixes brought OOM cadence from "every 75s" to "every 2-3 min" but never
to zero. Beyond the leaks, the surface has grown organically rather
than to a fixed contract: simple `render_frame(bytes)` for some paths,
nine-method `MultiPlaneRenderer` for others, with playback poking at
both. We want a renderer that is **leak-free by construction** and has
**one well-defined contract** that playback can rely on.

## 2. Goals

1. **Leak-free under all observable load.** No monotonic memory growth
   over indefinite runtime. Bounded by playlist content, not playlist
   length or runtime.
2. **One clean public contract.** Playback should call into a single
   well-defined surface. No `hasattr(renderer, ...)` branching.
3. **Fits the canonical hardware.** Pi Zero 2 W (416 MB RAM, ~256 MB
   CMA carveout, vc4 V3D 2.1) at 1080p is the target the design must
   close on. Bigger Pis are bonus.
4. **Designed for the full feature set up front.** The current pipeline
   was bolted together feature by feature (motion → auto-mode →
   transitions → blend modes). The rewrite should know all the inputs
   on day one and architect for them.

## 3. Non-goals

- Not redesigning the slide editor, content model, playlist model, or
  scheduling. Treat those as fixed inputs.
- Not redesigning the LED-panel paths (HUB75, WS2812B). They are stubs
  today. See §6 for what to do with them.
- Not optimizing for a hardware target we don't ship to (no Vulkan, no
  Pi 5 RT extensions).
- Not redesigning the `/api/playback/current-frame` HTTP surface, only
  the renderer affordance that backs it.

---

## 4. Hardware targets

| Target | Status | Constraint |
|---|---|---|
| Pi Zero 2 W (vc4 V3D 2.1, 416 MB RAM, 256 MB CMA) | **Primary — must fit** | The design closes on this or it doesn't ship. |
| Pi 4 / 4B (vc4 V3D 4, 1-8 GB) | Secondary — must work | More headroom; same software path. |
| Mac (M1/M2 dev box) | Tertiary — dev only | Mock output, no DRM/EGL. |
| NTSC/PAL composite via Pi | Out of scope for v1 | Vestigial CompositeRenderer can be deleted. |

The primary target's binding constraint is **kernel CMA**, not
userspace heap. CMA is currently pinned at ~247 / 256 MB before the
first transition by the GBM dumb buffer pool. The new design must
account for CMA explicitly: it's the actual GPU memory budget, and it
includes scanout framebuffers, EGL backbuffers, and any GL textures
that go through dmabuf.

## 5. Output targets in scope

For v1 of the rewrite, all of these must be reachable through the new
single contract:

- **HDMI 1080p** (1920×1080). The primary target. Must close on the
  Pi Zero 2 W (see §4 and §8.1).
- **Mock / dev preview** — a path that produces a PNG-on-disk per frame
  so the live-preview UI in `scripts/dev.sh` keeps working without
  hardware. Must run on a Mac.
- **HUB75 LED matrix** — panel-write code is a stub today (raises
  NotImplementedError) but the gamma+brightness LUT, config validation,
  and pixel-format encoding are real. Keep this path reachable through
  the new contract; the panel-write stub stays a stub until Phase 8
  bring-up.
- **WS2812B LED strip/chain** — GRB encoder works; real DMA write via
  `rpi_ws281x` is Phase 10. Same shape as HUB75: reach it through the
  new contract, panel-write stays Phase-gated.

Out of scope for v1:

- Composite NTSC/PAL — vestigial, propose deleting outright. The
  implementing agent may remove `CompositeRenderer` without replacement.

## 6. Inputs the renderer must accept

Treat these as fixed. They come from elsewhere in the codebase and the
renderer must consume them as-is.

### 6.1 Slides (from `backend/openmarquee/content/__init__.py`)

- **TextSlide** — N text layers + a background. Renderer must support:
  - Background: solid color (#RRGGBB), one of 12 procedural patterns
    (solid, gradient, dots, halftone, stripes, scanlines, checker,
    grid, rings, rays, confetti, bricks), an image asset, or a video
    asset (last two reference another slide's asset by UUID).
  - Per-text-layer: position+size box (slide-relative fractions),
    text content, font family/size/weight, color, alignment, opacity,
    visibility flag, vertical anchor, optional outline stroke.
  - Per-layer **motion**: one of `static`, `ticker`, `breathe`,
    `pulse`, `bounce`, `shake`, `blink`, with intensity (0–100),
    phase (0–1), and speed (0–2) parameters. Motion is computed
    against a global tick clock so layers stay in sync across slide
    re-entries.
  - Per-layer **auto-mode**: `time` (HH:MM or HH:MM:SS), `date`
    (YYYY-MM-DD or "Apr 21" or "April 21, 2026"), `day` (full or
    short). Auto-mode text changes every second; the renderer is
    responsible for re-rasterizing affected layers at the right
    cadence.
  - Per-layer **blend modes**: `normal`, `screen`, `multiply`,
    `overlay`. All four must produce visually correct composites.
    These compose against the layer stack below (in painter's order),
    not against the slide background only.
- **ImageSlide** — a pre-scaled PNG/JPEG that fills the canvas. No
  layers. Has a transition.
- **VideoSlide** — H.264 MP4 capped at 1080p. The renderer must
  decode real video frames in v1 (no thumbnail stub). Looping
  behavior: play once and hold the last frame for the remainder of
  `duration_ms` if the file is shorter; clip at `duration_ms` if
  longer. Audio is out of scope. Video frames must also be usable as
  a TextSlide background (TextSlide.background_video_slide_id), so
  the decoder must be reachable from the text-slide compose path,
  not only when a VideoSlide is the active slide.

### 6.2 Transitions (from `backend/openmarquee/playlist.py`)

PlaylistItem.transition is one of 16 string values. The renderer must
support all 16. Their visual semantics match the existing fragment
shaders under `backend/openmarquee/rendering/shader_compositor.py`
(treat that as a reference for what each one looks like, not as a
contract to keep verbatim).

  `cut`, `fade`, `wipe`, `iris`, `dissolve`, `pixelate`, `scanline`,
  `halftone`, `glitch`, `slide`, `push`, `scroll`, `blinds`, `flip`,
  `marquee`, `shutter`.

Transition duration is a per-item integer 0–5,000 ms. `cut` is
duration-agnostic (instant). All others are time-parameterized.

### 6.3 Settings (from `backend/openmarquee/settings.py`)

The renderer must respect, and react to runtime changes of:

- `display_width`, `display_height` — canvas dimensions. Today defaults
  1920×1080. Tests use 128×96.
- `display_rotation` — 0 / 90 / 180 / 270. Applied at scanout (the
  *display* rotates; the canvas dims are always landscape in storage).
- `brightness` (0–100) and `gamma` (0.1–3.0). Applied to output.
- `output_mode` — selects which renderer implementation runs (`hdmi` is
  in-scope for v1).

Settings can change at runtime (operator hits "save" in the UI). The
renderer must handle a re-config without restarting the playback loop.

## 7. Functional requirements

The renderer is responsible for, at any wall-clock time:

1. **Composing the current slide** with all visible layers, applying
   motion offsets, auto-mode text content, opacity, blend, anchor, and
   the slide's background, at the canvas resolution.
2. **Performing the configured transition** between two slides over
   the configured duration. During the transition the renderer must
   continue advancing motion + auto-mode for both the outgoing and
   incoming slide — motion does not freeze through transitions. The
   strong preference is to do this **on the GPU side, inside the
   shader path**, so motion and transition share the same per-pixel
   pass. If that proves infeasible on the primary target's bandwidth /
   ALU envelope, a split fallback is acceptable: motion advances on
   one path while the transition runs on another, recombined at
   scanout. The rewrite must attempt the unified shader-side approach
   first; document the result.
3. **Producing a PNG snapshot** of the current composite on demand
   (backs the `/api/playback/current-frame` endpoint). Snapshot must
   reflect motion + auto-mode at the captured instant. Snapshot
   frequency is bounded above by the playback loop, not by the
   endpoint (today: at most 1 capture per 5 minutes per playlist, plus
   on playlist change). The renderer must not block playback to do it.
4. **Honoring brightness, gamma, and display rotation** at scanout.
5. **Surviving an asset error** (missing/corrupt image, missing font,
   etc.) by skipping the offending layer or slide and continuing the
   loop.

The renderer is **not** responsible for:

- Choosing which slide to play (playback owns scheduling).
- Loading content from disk (storage owns that; renderer takes already-
  parsed models or already-loaded asset bytes — implementer's choice).
- Audio playback (video is rendered without sound).

## 8. Non-functional requirements

1. **Memory bounded — must fit a Pi Zero 2 W.** Total renderer
   footprint (userspace heap + kernel CMA + GPU buffers) must stay
   within a fixed budget regardless of playlist length or runtime
   duration. The hard ceiling is the Pi Zero 2 W's 416 MB physical RAM
   and 256 MB CMA carveout, with headroom for the rest of the backend
   process (FastAPI, asyncio loop, content/playlist storage, glyph
   caches, video decoder). The implementing agent must produce a
   defensible budget breakdown (heap / CMA / GBM / GL textures / video
   decoder ring buffers / etc.) before writing the first line of
   compositor code, and must verify on the dev Pi that the budget
   holds under the canonical welcome reel.
2. **No leaks.** Across an extended soak on the canonical playlist
   (duration to be set by the implementing agent at a length that
   would surface a real leak — start point: ≥6 hours, ideally
   overnight), `VmData`, `VmRSS`, `Swap`, and `CmaUsed` must show no
   monotonic growth.
3. **Frame rate.** Smooth, judder-free playback at 1080p on the
   primary target. Concretely: motion ticks must not drop frames at
   steady state, and transitions must not show visible stutter on a
   60 Hz HDMI display. Specific fps targets (e.g. 30 fps steady, some
   floor through transitions) are for the implementing agent to set
   based on what's achievable on the canonical hardware; surface the
   chosen targets to qarl in the design doc.
4. **Cold start.** First frame on screen ≤ 4 seconds from process
   start. (Spec-author starting point; tighten or loosen if the chosen
   architecture demands.)
5. **Reconfig.** A settings change (rotation, brightness, dims) takes
   effect within one playback iteration, ≤ 2 seconds.
6. **Lifecycle.** Closing the renderer releases all GPU/kernel
   resources so a subsequent open succeeds. A systemd `restart` (or
   any other re-instantiation in the same process) must not leak
   buffers, fds, or kernel objects across the gap.
7. **Testable on a Mac.** All non-hardware logic (compositing math,
   motion math, auto-mode formatting, blend modes, transition
   parameter math) must be unit-testable without DRM/EGL.

## 9. Failure modes the renderer must handle

- **Renderer init fails** on the canonical hardware → fall back to
  Mock + log clearly. Today this is gated by an env-var; the rewrite
  should keep the fallback but pick a cleaner trigger.
- **Asset missing or corrupt** → skip the affected slide/layer, log
  once, continue.
- **Plane budget / shader budget exceeded** (more layers than the
  hardware can simultaneously handle) → degrade by software-compositing
  the surplus, OR drop to a single-plane software path for that
  slide. Implementer's choice; document the chosen behavior.
- **Settings change to unsupported dims/rotation** → reject at the
  API layer (not the renderer's job), but the renderer must not crash
  on a value it can't satisfy.
- **DRM EBUSY at scanout** (compositor lost the master) → log, retry
  next tick, surface in `/api/health` if persistent.

## 10. Public API expected by the playback loop

The playback loop in `backend/openmarquee/playback.py` is the only
in-tree caller of the renderer. The implementing agent owns the actual
method signatures, but the *operations* the playback loop needs are:

1. **Construct + open** a renderer for the configured output mode and
   dimensions, with a clean lifecycle (open → use → close, with
   guaranteed resource release on close — see §8.6).
2. **Begin a slide presentation** at wall-clock time T0 — give the
   renderer the slide's content model and any pre-loaded asset bytes,
   tell it the duration.
3. **Advance** — drive the renderer forward to a given wall-clock
   instant. The renderer is responsible for resolving motion,
   auto-mode, and what to put on screen at that instant. The
   playback loop does not implement compositing math or push raw
   pixel buffers; whether the renderer pulls timing or accepts
   pushed events is for the implementing agent to choose.
4. **Begin a transition** from the current slide to the next, with the
   transition kind and duration. Continue calling Advance during the
   transition; both slides remain logically active until it completes.
5. **Capture** — return a PNG of the current screen contents.
6. **Reconfigure** — apply new settings (dims, rotation, brightness,
   gamma) without losing playback state.
7. **Close** — release everything.

Internally the renderer may run its own thread, its own process, its
own I/O loop, or none of those. Visible to playback is the operation
list above and nothing else. No `prepare_primary_buffer` /
`restage_primary_fb` / `commit` / `attach_animated_layer` /
`drm_fd` leaking out.

## 11. Decisions made by qarl, 2026-05-06

These were open questions in the first draft of this doc. Answered:

- **Resolution:** 1080p. No retreat to 720p.
- **Transitions:** all 16. No curated subset.
- **Blend modes:** all four (`normal`, `screen`, `multiply`, `overlay`).
  First-class render requirement, not schema-only.
- **Video:** real H.264 decode in v1. Not a stub.
- **HUB75 + WS2812B:** keep them in scope. Reach them through the new
  contract; panel-write paths stay stubs until their respective
  phases.
- **Motion through transitions:** mandatory. Strong preference is to
  do motion *inside the shader transition pass* so it's all one
  per-pixel program. If the bandwidth / ALU envelope on the Pi Zero 2
  W can't carry it, a split fallback is acceptable: motion advances
  on one path while the transition runs on another, recombined at
  scanout. The rewrite must attempt the unified shader-side approach
  first; document the result.
- **Memory budget:** must fit Pi Zero 2 W. Implementing agent
  produces a defensible breakdown before writing compositor code.

The combination of "1080p + all 16 transitions + all 4 blend modes +
real video decode + motion through shader transitions, all fitting on
a Pi Zero 2 W" is ambitious. The implementing agent should not
silently drop any of these requirements; if they prove physically
incompatible on the canonical hardware, escalate to qarl with
specific numbers and a proposed scope cut, rather than ship a
half-implemented version.

## 12. What the implementing agent gets, in addition to this doc

- The full content model under `backend/openmarquee/content/`.
- The playback loop under `backend/openmarquee/playback.py` (treat
  its renderer-call sites as a list of *requirements*, not a contract
  to preserve verbatim — they will be rewritten to match the new
  surface).
- The seed playlist under `backend/openmarquee/seed.py` as a working
  test case.
- The dev Pi at `openmarquee@openMarqueeDev` (Tailscale magic-DNS) for
  on-hardware verification. systemd unit at
  `system/openmarquee-backend.service`. Redeploy via
  `bash scripts/deploy.sh openmarquee@openMarqueeDev`.
- Permission to delete every file under `backend/openmarquee/rendering/`
  except `__init__.py`'s public surface, which they redesign.
