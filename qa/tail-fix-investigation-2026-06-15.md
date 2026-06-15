# Tail-fix investigation 2026-06-15

Investigation log for the F-1 post-deploy tail (8.8s+ blit stall during
v2v transitions, ~14% of transitions, all in_transition=true, all
path=dmabuf). Mechanically routed to GL/V3D lane by QA's read of code's
tail-diag-v1 bake_breakdown probe (fed928d).

## tail-diag v2 — sub-phase blit instrumentation (perf-gl)

**Owner:** code2 lane (hdmi.rs paint hot path + bake helpers)
**Commit:** stacked on combined-stack tip 1568e5b on
`task/perf-gl-2026-06-15`.
**Binary:** /tmp/openmarquee-render-tail-diag-v2-<sha>
**Fingerprint:** `strings <bin> | grep -E 'tail_diag_blit_(subphase|flush)|cache_path'` → counts 1/1/1.

### Hypothesis tree (admin)

Admin's two strong surfaces for the GL stall under 2-video transition
load:

- **GL2.1 — 2-dmabuf-blit-per-frame overload.** During transition the
  from + to endpoints both bake via the DMABUF arm. That's 2×
  `eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT)`, 2×
  `glEGLImageTargetTexture2DOES`, 2× `nv12_blit_pass` per frame, all
  into offscreen FBOs (per r106 cached `transition_fbo_a/b`). vc4 V3D
  pipeline may stall under 2-concurrent-video DMA pressure (CMA-
  allocated dmabufs from V4L2 + V3D tile-store on same memory).
- **GL2.2 — GL sync/fence under 2-video load (prior pick).** The iter-7
  `is_offscreen_bake && bake_offscreen_flush_enabled()` flush in
  `bake_video_slide_to_current_fbo` DMABUF branch (2026-06-14 from-side-
  black cure, documented DO-NOT) may serialize against the V3D backlog
  under 2-video pressure → multi-second wait for GPU drain. Perfect
  under 1-video steady-state; suspect under 2-video transition.

### What v2 instruments (pure field-add per cross-lane rule)

**Inside `run_nv12_dmabuf_blit_pass`** (`renderer/src/hdmi.rs:~12736`):

5 `Instant` bindings spanning fn entry → 4 phase boundaries → return.
Gated emit only when `total_us > 500_000` (500 ms; fast ticks pay 5
Instant reads + 1 compare ≈ 1 µs).

```
[perf] tail_diag_blit_subphase
       import_us=N        — fn entry → EGLImage acquired (cache OR per-frame create)
       sampler_us=N       — EGLImage acquired → image_target_texture_2d done
                            (create_texture V3D BO alloc + 4× tex_param_i32 + EGLImage→texture bind)
       draw_us=N          — closure exec (cached_nv12_dmabuf_program + uniforms + draw_arrays + cleanup)
       destroy_us=N       — closure end → fn return (texture delete + maybe EGLImage destroy)
       total_us=N
       cache_path=<bool>  — true when r101 EGLImage cache was enabled for this call
                            (does NOT disambiguate cache-hit vs fresh-insert on the cache_path=true arm;
                             future v3 can thread `_created` if QA needs the finer split)
```

**Around the iter-7 `gl.flush()`** (`renderer/src/hdmi.rs:~8848`):

`flush_us` via `t_flush_start.elapsed()`. Gated emit only when
`flush_us > 500_000`. Probe is INSIDE the existing
`if is_offscreen_bake && bake_offscreen_flush_enabled()` block — steady-
state pays zero.

```
[perf] tail_diag_blit_flush flush_us=N is_offscreen_bake=true
```

### Disambiguation tree post-data

When QA's FYS sample fires:

- **import_us > 1_000_000** → EGLImage acquire stall.
  - `cache_path=true` → Mutex contention in
    `Decoder::get_or_init_egl_image` OR slow per-decoder Mutex
    acquisition under concurrent transition bake. Fix candidate:
    lock-free per-buffer slot, or atomic-cache-per-buffer instead of
    per-decoder Mutex.
  - `cache_path=false` → per-frame `eglCreateImageKHR` is the slow
    path. Fix candidate: enforce cache ON in production
    (`OPENMARQUEE_EGL_IMAGE_CACHE=on`); investigate why kill-switch is
    active.
- **sampler_us > 1_000_000** → V3D BO alloc for the texture object
  stalling. Fix candidate: cache `glow::NativeTexture` per
  `(decoder, capture_buffer_index)` alongside the existing EGLImage
  cache (currently the texture is created + destroyed per frame even
  on cache_path=true).
- **draw_us > 1_000_000** → vc4 V3D pipeline genuinely stalled on
  shader+draw. Fix candidate: reduce per-tick work (e.g. skip the
  second endpoint's blit on alternating ticks for non-essential
  motion) OR investigate driver-side V3D scheduling.
- **destroy_us > 1_000_000** → texture delete / EGLImage destroy
  blocking. Fix candidate: defer teardown to end-of-frame instead of
  per-blit.
- **All sub-phases small + flush_us > 1_000_000** → GL2.2 confirmed.
  iter-7 flush serializes. Fix shape options (preserve from-side-black
  cure):
  - Scope flush tighter: from-side only, since to-side is bridged by
    other mechanisms during the transition window.
  - Replace `gl.flush()` with `glFenceSync` + targeted wait at a
    later phase boundary (release the render thread, sync at present
    time).
  - Conditionally skip flush when both endpoints are video (re-check
    the original bug — was the from-side-black specific to mixed
    text/video endpoints?).
- **All small AND flush_us small** → stall is OUTSIDE bake. v3 in
  `paint_and_present_one_transition_frame` composite/swap path.

### Probe overhead bound

- Fast tick (total_us ≤ 500 ms): 5 Instant reads + 1 subtract + 1
  compare ≈ **1 µs**. Imperceptible vs the 33 ms / 30 fps budget.
- Slow tick (total_us > 500 ms): +4 sub-phase subtracts + 1 eprintln
  format ≈ **5-50 µs**. Negligible vs the multi-second stall being
  measured.
- Source-pin regression-lock: `frame_pacing.rs` test
  `tail_diag_blit_subphase_field_name_pinned_in_hdmi_source` pins
  both literals against hdmi.rs source (matches code's
  `peak_triage_since_restart_ms_field_name_pinned_in_hdmi_source`
  shape).

### DO-NOT regress

- Iter-7 `gl.flush()` gate
  (`is_offscreen_bake && bake_offscreen_flush_enabled()`) — preserved
  unchanged. Probe wraps the flush, does not alter its execution.
- r106 feed/drain decouple — untouched.
- `transition_fbo_a/b_painted` flag pattern — untouched.
- M-1 slide_caches LruMap cap — untouched.
- W-2 thread_local env-cache helpers — untouched.
- Item-1 `bake_offscreen_flush_enabled` cached helper — untouched.

### Bench-routing

- code2 cross-builds in `/tmp/renderer-build-om-rebase` (per-worktree
  BUILD_DIR per 2026-06-15 script fix).
- Binary handed to QA with fingerprint count manifest.
- QA deploys + samples on FYS; classification per disambiguation tree
  above; routes data back for v3 instrumentation OR fix-candidate
  scoping.
