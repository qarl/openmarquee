# Shader Compositor — Design

Status: **landed, glass-validated through #209 (motion code-path) +
#210 visual still pending**. Drafted 2026-05-04 after the night-2
session that closed #197 (all 14 transition kinds), #198 (Photoshop
blend modes), #205 (snapshot cache), #206 (motion through transitions),
#207 (motion phase passthrough), #200 (PageFlip event drain).

Reference memory: `project_shader_compositor_decision.md` (qarl
2026-05-03 decision to go shaders all the way; subsequently pivoted
to a hybrid model the same evening). `project_phase7_shader_progress.md`
tracks the commit-by-commit arc.

---

## Motivation

Two surfaces couldn't be done by the multi-plane DRM compositor:

1. **Slide-to-slide transitions.** vc4 HVS's plane-property animation
   covers fade (alpha) and wipe (SRC_W + CRTC_W) cleanly, but the
   other 12 kinds in qarl's transition palette (iris / dissolve /
   glitch / halftone / scanline / pixelate / marquee / blinds /
   shutter / slide / push / scroll / flip) need per-pixel math the
   HVS can't express. Software (PIL) at 1080p was 140-180 ms/frame
   = ~5-7 fps stutter through the transition window.

2. **Photoshop blend modes per layer.** The vc4 HVS implements only
   PREMULTI alpha-blend at scanout (COVERAGE is broken, see
   `multi-plane-gpu-compositor.md` gotchas). multiply / screen /
   overlay / darken / lighten / etc. need per-fragment math.

The shader compositor handles BOTH surfaces via GLES2. Steady-state
within-slide compositing **stays on the multi-plane DRM path** —
it's already fast, the HVS does it for free at scanout, and the
shader path's 8-layer blend ladder was GPU-bound on vc4 V3D 2.1
at 1080p (8.6 fps measured in commit 9b2ea0c, abandoned 9ae33f6).

Net architecture: **hybrid**. Multi-plane DRM owns steady state;
shader compositor owns transitions and blend modes; they cooperate
via a shared DRM fd + atomic primary-plane handoff at the
transition boundary.

## Hardware target

**Pi Zero 2 W** (vc4, V3D 2.1, kernel 6.12, Mesa 25.0.7-2+rpt4) is
the canonical target. The bandwidth + ALU envelope shapes every
choice in the design:

- 1.2-1.6 GB/s effective DDR bandwidth (Phoronix). Single-pass
  shader is the architectural rule; multi-pass FBO ping-pong would
  double bandwidth and blow the budget at 1080p.
- ~16 GFLOPS ALU. The 8-input layer blend ladder demanded ~38
  GOps/sec at 1080p × 30 fps; that's why the hybrid pivot
  happened. The 2-input transition shader runs ~600 MOps/sec —
  trivially within budget at 30.0 fps for 13 of 14 kinds (glitch
  at 23 fps for the corruption look, intentional).
- GLES 2.0 only (no GLES 3, no Vulkan ever on this part).
  `precision mediump float` is the default; `highp` is emulated
  by Mesa for fragment shaders that need 24-bit mantissa (the
  `fract(sin(*))` dissolve / glitch hash).
- **Pi Zero 2 W is a CLEANER target than Pi 4** for this
  architecture. Pi 4's UIF/T_TILED format-modifier mismatch forces
  copies through linear; vc4 V3D 2.1 + KMS planes natively accept
  the same `DRM_FORMAT_MOD_BROADCOM_VC4_T_TILED` modifier on both
  sides.

## Module layout

- `backend/openmarquee/rendering/shader_compositor.py` — owns the
  EGL/GBM/GL stack and the DRM-side commit. Long-lived; constructed
  once at PlaybackLoop startup, reused for every transition.
- `backend/openmarquee/rendering/snapshot.py` — composes a slide as
  RGBA bytes for the shader's u_from / u_to inputs. Two variants:
  full-composite (compose_slide_rgba) and bg+statics-only
  (compose_slide_bg_statics_rgba, used during #206
  motion-through-transitions). Plus `SlideSnapshotCache` (#205).
- `backend/openmarquee/rendering/blend.py` — Photoshop blend modes
  (multiply / screen / overlay) for the per-layer composite step.
  Numpy-vectorized; "normal" stays on the fast PIL alpha_composite
  path. PIL doesn't support these modes natively.
- `backend/openmarquee/playback.py` — PlaybackLoop integration:
  feature flag (`OPENMARQUEE_SHADER_TRANSITIONS=1`), dispatcher
  routing, motion-through-transitions orchestration.
- `backend/openmarquee/rendering/drm_kms.py` — multi-plane
  DRMRenderer (predecessor architecture, still load-bearing). The
  shader compositor borrows its `drm_fd` and `restage_primary_fb()`
  for the handoff dance.

## The transition pipeline

```
PlaybackLoop dispatcher
    │
    ├─ kind == "fade" or "wipe":
    │      → _fade_gpu / _wipe_gpu
    │      → multi-plane DRM plane-property animation (HVS)
    │      → _drain_outgoing_compositor BEFORE this fires
    │      → no shader involvement
    │
    └─ kind in _SHADER_TRANSITION_KINDS (12 others):
           → _run_shader_transition
              │
              ├─ outgoing slide was dynamic (#206):
              │     u_from = compose_slide_bg_statics_rgba(outgoing)
              │     outgoing compositor's overlays stay attached
              │     compositor.tick() each shader frame
              │     ramp overlay alpha 65535 → 0 across t
              │     drain at transition end
              │
              ├─ outgoing slide was static:
              │     u_from = current_image (already in memory)
              │     no overlays in play
              │
              └─ shared-fd ShaderRenderer:
                    set_kind(kind) — picks fragment program
                    set_from(u_from), set_to(u_to)
                    loop frames: set_transition_t(t), commit_frame()
                       (commit_frame: GL draw, eglSwapBuffers,
                        gbm_surface_lock_front_buffer, drmModeAddFB2,
                        drmModePageFlip + DRM_MODE_PAGE_FLIP_EVENT,
                        event drain on next frame)
                    primary-plane handoff back to multi-plane:
                       drm.render_frame(to_image_rgb)
                       drm.restage_primary_fb()
                       drm.commit() — atomic, displaces shader fb
                    ShaderRenderer doesn't close (long-lived)
```

## DRM master sharing (#204)

Only one DRM master per device. Multi-plane DRMRenderer owns it.
ShaderRenderer constructs with `drm_fd=DRMRenderer.drm_fd`; in
shared-fd mode it skips the `os.open` + master-take + SetCrtc-blank
on close. Both renderers issue ioctls on the same fd:

- DRMRenderer's atomic commits operate on overlay planes (it never
  GBM-creates anything; uses dumb buffers for the primary fb).
- ShaderRenderer's drmModeSetCrtc / drmModePageFlip operate on the
  primary plane during the transition window.

vc4 reroutes legacy SetCrtc through `drm_atomic_helper_set_config`
internally, so atomic and legacy commits coexist on the same CRTC.
Different planes; no kernel-side conflict.

The handoff dance at transition end is **load-bearing** (#204
pre-commit review caught it):

```python
drm.render_frame(to_image_rgb)    # paint dumb buffer
drm.restage_primary_fb()          # stage primary FB_ID + CRTC rects
drm.commit()                      # atomic-rebind to multi-plane fb
shader.close()                    # safe to RmFB shader's last fb now
```

`restage_primary_fb()` is necessary because legacy SetCrtc bypasses
the atomic property layer. Without it `_pending_props` is empty and
DRMRenderer.commit() is a no-op, leaving the kernel scanning
shader's last (RmFB'd-but-pinned) fb indefinitely.

## Motion through transitions (#206)

The 2-input shader composites only the **bg+statics** portion of
each slide. Animated text layers stay on multi-plane overlay planes
during the transition window:

- Outgoing slide: GPUSlideCompositor stays alive. Each shader
  frame, the loop calls compositor.tick() to advance motion phase
  + ramp every overlay's alpha 65535 → 0 over transition_t. HVS
  composites overlays over shader output at scanout.
- Incoming slide: NOT yet in scope — overlays attach AFTER the
  transition completes. Future symmetric work would add fade-in for
  incoming, but plane budget would need to be split (8 planes ÷ 2
  = 4 animated layers per side during transition).

Without #206, the shader's u_from baked animated layers' positions
into the texture; motion froze for the transition window. With #206,
the shader's u_from is bg+statics-only (compose_slide_bg_statics_rgba)
so the live overlays don't double-paint.

Slide t0 stash (#207) keeps motion phase continuous across the
handoff: PlaybackLoop._outgoing_slide_t0 holds the asyncio loop
time when the slide started ticking. _tick_outgoing_during_transition
uses `monotonic.now() - outgoing_slide_t0` for elapsed_s. Without
this, ticker motion would snap back to far-right at transition
entry (phase=0 = scroll start position). Pulse / breathe / bounce /
shake / blink are cycle-symmetric so the seam is invisible there.

## Snapshot cache (#205)

`SlideSnapshotCache` keyed by (slide.id, slide.updated_at). Both
full-composite and bg+statics-only variants are cached lazily.
Without it, every transition pays ~600 ms compose at 1080p (PIL
bg load + alpha_composite per layer + blend math) for u_from AND
u_to = ~1.2 s freeze on the playback thread before each fade.

**Auto-mode slides skip the cache entirely.** Their text re-renders
from the current time (clock at 12:34 vs 12:35) so cached bytes
would either serve stale text or invalidate every second.
`_slide_has_auto_layer(slide)` is the predicate.

Memory cost: ~8 MB per slide (1080p RGBA) × N slides. Typical
playlist ~10 slides = ~80 MB held; well within Pi Zero 2 W's
512 MB budget.

## Blend modes (#198)

Four modes in the TextLayer schema (`content/__init__.py`): normal,
multiply, screen, overlay. Math in `rendering/blend.py`:

- multiply: `result = base * top` per channel (in [0,1] space)
- screen: `result = 1 - (1-base) * (1-top)`
- overlay: multiply where `base < 0.5`, screen where `base >= 0.5`

Followed by Porter-Duff source-over alpha-mix using top's alpha,
divided by result_a to un-premultiply. Non-premultiplied input
matches PIL's RGBA convention.

`composite_with_blend(base, top, mode)` is the entrypoint. "normal"
mode falls through to PIL `alpha_composite` (fast); other modes
go through the numpy path (~10 ms at 1080p).

**Animated blend-mode layers fall back to software** -- HVS
overlay planes can't do non-alpha blend on vc4. The
`_slide_has_animated_blend_mode_layer()` predicate vetoes the GPU
path; `compose_motion_frame` applies blend per-tick.

## PageFlip event drain (#200)

`drmModePageFlip` queues an event the kernel won't release the CRTC
slot until userspace reads. Without
`DRM_MODE_PAGE_FLIP_EVENT`, the SECOND flip on the same CRTC
returns -EBUSY and we'd fall to synchronous SetCrtc on every
frame. Implementation:

- `commit_frame` issues PageFlip + EVENT, sets
  `_pageflip_pending = True`.
- Next `commit_frame` calls `_drain_pageflip_events(timeout=0.020)`
  before the next flip; select() + os.read() on `self._fd` consumes
  the queued event.
- close() drains pending events before teardown so the shared fd
  doesn't carry queued state into the caller's next read.

20 ms timeout = ~one vblank at 60 fps. Above that, the SetCrtc
fallback path takes over (still works, just heavier).

## Transition kinds (#197)

All 14 kinds in qarl's transition palette have implementations:

- **HVS plane-property** (faster, no shader): fade, wipe.
- **Shader fragments** (`_TRANSITION_SHADERS` dict): iris,
  dissolve, pixelate, scanline, halftone, glitch, slide, push,
  scroll, blinds, flip, marquee, shutter.

Each shader is 10-50 lines of GLSL. Live-fire on dev Pi:

- 13/14 at **30.0 fps stable** at 1080p.
- glitch at **23.6 fps** (highp `fract(sin(*))` hash + 2 hash
  calls per fragment is heavy; the slight stutter reads as part
  of the corruption effect, intentional).

Adding a new kind:

1. Write the fragment shader (input: u_from, u_to, u_transition_t;
   output: gl_FragColor). Add to `_TRANSITION_SHADERS`.
2. Add the kind name to `_SHADER_TRANSITION_KINDS` in playback.py.
3. In the existing dispatcher (e.g. `_iris`), add
   `if await self._run_shader_transition(...): return` before the
   PIL fallback.

## Python ctypes gotchas

`project_python_gles_gotchas.md` captures six PyOpenGL-on-Pi traps
discovered during the spike phase:

1. `os.environ["PYOPENGL_PLATFORM"] = "egl"` BEFORE `import OpenGL`
   or any state-tracking GL call raises "Attempt to retrieve
   context."
2. PyOpenGL's `eglGetPlatformDisplay` + `eglCreateWindowSurface`
   byref() native_display/native_window. Bind them direct via
   `ctypes.CDLL("libEGL.so.1")`.
3. `glGetString` returns a GLubyteArray; cast to c_char_p, don't
   use `bytes(...)`.
4. `eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR)` only works on
   card nodes (card0), not render nodes (renderD128). Render
   nodes lack KMS.
5. `gbm_bo_get_handle` returns union by value; wrap as
   `Structure { c_uint64 }` and read low 32 bits.
6. `libdrm.so.2` needs `CDLL(..., use_errno=True)` for
   `ctypes.get_errno()` to work.

The patterns in `shader_compositor.py:_load_libs()` are the
canonical reference. New ctypes work in this area should copy
from there rather than rebuilding from web examples.

## Configuration

**Feature flag**: `OPENMARQUEE_SHADER_TRANSITIONS=1` in the
environment activates the shader path. Default is off; falls
through to existing PIL transitions. Set in
`scripts/phase6_welcome_loop.py` invocation when testing on the
dev Pi.

The flag is intentionally an env var (not settings.json) for now
so operators can A/B the path on a single device without surface
edits. Migration to settings.json is a future concern.

## Open work

- **#199 fence-based GL↔KMS sync**
  (`EGL_ANDROID_native_fence_sync`). Real value at high
  framerates: skip the implicit glFinish stall before commit; pass
  the GL render fence to DRM via IN_FENCE_FD plane property.
  Requires atomic-commit refactor of ShaderRenderer's DRM layer
  (currently legacy SetCrtc + PageFlip). ~250 lines of mechanical
  reshape. Benefits from glass verification mid-flight.

- **#208 measure shader-frame budget under #206 with marquee**.
  Pre-commit reviewer's concern: each shader frame does GL draw +
  SwapBuffers + AddFB2 + PageFlip + (compositor.tick atomic
  commit) + (alpha-ramp atomic commit). Two atomic commits on
  overlay side per shader frame. At 30 fps the 33 ms budget should
  fit; live-fire with marquee will tell us. Drops below 25 fps =
  batch the alpha-ramp into tick's atomic commit.

- **#210 visual confirmation of #198 + #206 on glass**. Offscreen
  PNG verification done; on-glass requires qarl's eyes.

- **Symmetric incoming-side fade-in** (no task # yet). #206
  handles outgoing slide's animated text staying alive through
  the transition. Incoming slide's animated layers don't appear
  until the transition ends. A symmetric fade-in would attach
  incoming overlays at alpha=0 with appropriate plane-budget
  handling. Self._outgoing_compositor's docstring spells out the
  collision risk.

## Architecture decisions log

- **2026-05-03 morning**: full-shader rewrite chosen over hybrid
  (project_shader_compositor_decision.md). Multi-plane DRM kept
  as fallback behind a flag.

- **2026-05-03 evening**: Milestone B's 8-slot blend ladder hit
  8.6 fps at 1080p (GPU-bound). qarl: "for the transition we
  should only need two planes...?" Pivot to the hybrid (commit
  9ae33f6). Multi-plane DRM stays canonical for steady state;
  shader narrows to 2-input transitions only.

- **2026-05-04 night**: phase 7 closes architecturally. All 14
  transitions, blend modes, motion-through-transitions, snapshot
  cache, motion phase passthrough, PageFlip event drain. Glass
  verification of motion continuity + blend mode rendering is
  the remaining gate before general deployment.
