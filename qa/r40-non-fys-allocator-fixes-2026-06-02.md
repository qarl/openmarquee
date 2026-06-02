# r40 — Non-FYS allocator defense-in-depth fixes

**Author lane:** code1 (renderer-perf — the r38b deep-read +
r39 doc hygiene continuation).

**Scope:** apply the 2 latent allocator-leak fixes the r38b
sacred subagent surfaced (audit doc §7.3 there) but parked as
out-of-scope for r38b because both code paths are gated out of
FYS. These are real bugs for any non-FYS deployment running
stream slides or DMABUF mode.

**Scope expansion mid-review:** the sacred subagent for r40
caught an additional CRITICAL twin of Fix 1 in the V4L2 video
paint hot path (`bake_video_slide_to_current_fbo` MMAP, hdmi.rs:
6923) — identical shape, identical fix. Adding as **Fix 3**
before commit per the canonical scope of "allocator cleanup
fixes". Two lower-severity sites flagged in §F.new for r41+.

**Origin/main HEAD at fix time:** `af6001e` (my r39).

---

## §1 — Fix 1: `bake_external_nv12_to_current_fbo` (hdmi.rs:6607)

### Location

`renderer/src/hdmi.rs:6607-6609` (pre-fix) — the `uv_tex` glGenTextures
call inside the `dims_changed` branch of `bake_external_nv12_to_current_fbo`.

### Failure mode

```
y_tex = gl.create_texture()?         // line 6601 — succeeds
gl.bind_texture(...); ...tex_image_2d(...)  // y_tex now ~2 MB GLES storage
uv_tex = gl.create_texture()?        // line 6609 — FAILS, ?-bubble
// Function returns Err. y_tex is NEVER deleted.
// *nv12_tex was Some(old_y, old_uv, ...) at entry; old pair was
// already deleted at line 6594; the new y_tex was never assigned
// to *nv12_tex (assignment at line 6629 only fires after both
// succeed). y_tex leaks ~2 MB per failure.
//
// Next call: dims_changed=true (nv12_tex is still None from
// take() at 6594); allocates ANOTHER y_tex. Leak accumulates
// across transient GL_OUT_OF_MEMORY events.
```

### Trigger surface

- VLC NV12 HW-decode stream path (StreamSlide via
  ffmpeg `-c:v h264_v4l2m2m`, raw NV12 out).
- Specifically: first frame after sidecar boot OR a source-
  resolution switch (anything that flips `dims_changed=true`).
- Under CMA pressure, `gl.create_texture` can transiently fail
  with GL_OUT_OF_MEMORY — exactly the scenario this dispatch is
  hardening against.

### FYS-relevance

**None.** FYS reel has zero streams. Real bug for non-FYS
deployments running StreamSlide content.

### Fix shape

Match-arm replacement mirroring `:3724-3731` (canonical scanout
commit-fail) and `:4283-4302` (r38b transition-closure pattern):

```rust
let uv_tex = match gl.create_texture() {
    Ok(t) => t,
    Err(e) => {
        gl.delete_texture(y_tex);
        return Err(anyhow!("glGenTextures(external NV12 UV): {e}"));
    }
};
```

7-line LOC diff. No behavior change on the success path. The
delete_texture call on the failure path is the minimum cleanup
required to prevent the orphan.

---

## §2 — Fix 2: `run_nv12_dmabuf_blit_pass` (hdmi.rs:10349)

### Location

`renderer/src/hdmi.rs:10349-10350` (pre-fix) — the `tex`
glGenTextures call inside `run_nv12_dmabuf_blit_pass`, after
`eglCreateImageKHR` has succeeded.

### Failure mode

```
egl_image = (eps.create_image)(...)         // line 10277-10283
if egl_image.is_null() { return Err(...) }  // pre-create-tex guard
// egl_image holds kernel-side dma_buf ref (~3 MB CMA per NV12 frame
// at 1080p).
tex = gl.create_texture()?                  // line 10349 — FAILS, ?-bubble
// Function returns Err. egl_image is NEVER destroyed via
// (eps.destroy_image)(display, egl_image). Kernel keeps the
// dma_buf alive until renderer exit.
//
// The teardown block at lines 10381-10388 (delete_texture +
// destroy_image) is past the ?-bubble — never reached.
//
// Frame::Drop (the V4L2 buffer wrapper) re-QBUFs the buffer slot
// for re-decode but doesn't release the EGLImage's separate
// dma_buf ref. Per-leak: one NV12 frame's CMA buffer until
// process exit.
```

### Trigger surface

- V4L2 DMABUF VideoSlide playback path.
- Gated by `OPENMARQUEE_RENDERER_DMABUF=1` env var AND VideoSlide
  presence in the playlist.
- Under CMA pressure, `gl.create_texture` can transiently fail
  with GL_OUT_OF_MEMORY → leak accelerates exactly when CMA
  is already tight (positive-feedback shape).

### FYS-relevance

**None.** FYS has neither DMABUF env var nor VideoSlides. Real
bug on any deployment where both gates are met.

### Fix shape

Match-arm replacement mirroring the same canonical pattern,
with destroy_image instead of delete_texture:

```rust
let tex = match gl.create_texture() {
    Ok(t) => t,
    Err(e) => {
        let destroyed = (eps.destroy_image)(display.as_ptr(), egl_image);
        if destroyed == 0 {
            eprintln!(
                "warn: eglDestroyImageKHR returned EGL_FALSE for fd={} during create_texture-fail cleanup",
                fd
            );
        }
        return Err(anyhow!("glGenTextures(external-OES): {e}"));
    }
};
```

12-line LOC diff. The EGL_FALSE warn-and-continue shape mirrors
the success-path teardown at lines 10381-10387 (warn on EGL_FALSE,
keep going). The cleanup CAN still fail — if eglDestroyImageKHR
returns EGL_FALSE, the buffer remains leaked — but the warning
surfaces it for diagnosis and the function returns Err anyway.

---

## §3 — Pattern alignment

Both fixes mirror the same canonical pattern established by:

1. **The scanout commit-fail at hdmi.rs:3724-3731** (verified
   reference in r37 audit; quoted as canonical-correct in r38b
   §2 of `qa/r38b-hdmi-cma-deep-read-2026-06-02.md`).
2. **The r38b transition-closure cleanup at hdmi.rs:4283-4302**
   (shipped at SHA `5ac3ca2`; mirrors the same shape across 3
   `?`-bubble sites).
3. **The pre-existing match-arms at hdmi.rs:4252-4258 +
   4262-4269** (link_program + create_buffer; the in-repo
   reference for this pattern).

The "match-arm replacement of `?`-bubble" pattern is the
canonical fix shape for mid-sequence `?`-bubble leaks across
hdmi.rs. r40 extends it to the 2 surviving non-FYS sites.

---

## §3.5 — Fix 3 (scope-expansion mid-review)

### Location

`renderer/src/hdmi.rs:6923-6925` (pre-fix) — the `uv_tex`
glGenTextures call in `bake_video_slide_to_current_fbo`'s
MMAP path. **The V4L2 video paint hot path.**

### Failure mode

```
y_tex = gl.create_texture()?         // line 6900 — succeeds
gl.bind_texture(...); ...tex_image_2d(...)  // y_tex now ~2 MB GLES
uv_tex = gl.create_texture()?        // line 6923 — FAILS, ?-bubble
// y_tex is NEVER deleted. The matching delete_texture calls at
// lines 6959-6960 are past the ?-bubble.
//
// Unlike Fix 1, y_tex here is PER-CALL (not session-cached),
// so the leak rate is one orphan per failure. Under GL_OUT_OF_
// MEMORY pressure on the V4L2 video paint hot path, this can
// fire per video frame -- accelerating exactly when CMA is
// already tight (positive-feedback shape).
```

### Trigger surface

- V4L2 H.264 VideoSlide playback (MMAP path; the default when
  `OPENMARQUEE_RENDERER_DMABUF` is NOT set).
- Per-video-frame (every paint cycle through this function).
- Same GL_OUT_OF_MEMORY transient trigger as Fix 1 + Fix 2.

### FYS-relevance

**None.** FYS has no VideoSlides. Real bug for any deployment
running VideoSlide content in MMAP mode (the more common mode —
DMABUF requires explicit opt-in).

### Fix shape

Same canonical match-arm replacement as Fix 1, with
`(video UV)` context tag instead of `(external NV12 UV)`:

```rust
let uv_tex = match gl.create_texture() {
    Ok(t) => t,
    Err(e) => {
        gl.delete_texture(y_tex);
        return Err(anyhow!("glGenTextures(video UV): {e}"));
    }
};
```

7-line LOC diff. Identical structure to Fix 1.

---

## §F — Adjacent sweep findings (§F.new)

The sacred subagent review scanned hdmi.rs + the surrounding
renderer source for other similar shapes: multi-step alloc/init
sequences with mid-sequence `?`-bubbles between two CMA-relevant
allocations where the prior alloc would leak.

### §F.1 — CRITICAL (shipped in this commit as Fix 3)

- **`hdmi.rs:6923` — `bake_video_slide_to_current_fbo` MMAP path.**
  V4L2 video paint hot path twin of Fix 1. **Shipped as Fix 3 in
  this commit.**

### §F.2 — DEFERRED for r41+ (lower severity; non-FYS)

- **`hdmi.rs:5179` — `capture_fullres_transition_mid_to_png`.**
  `cap_tex = gl.create_texture().map_err(...)?` leaks fbo_a +
  tex_a + fbo_b + tex_b (~8 MB FBO storage) on failure. The
  sibling legacy 3-pass capture path at `hdmi.rs:4952` already
  cleans them; the new path is asymmetric. This is a capture /
  debug path (not the production paint hot path), so leak rate
  is low. Defer to r41+ "capture-path cleanup audit".
- **`renderer/src/sdf_atlas_gl.rs:53` — `upload_all` loop.**
  Per-iteration `create_texture().map_err(...)?` leaks all
  previously-pushed atlas textures (Vec<MsdfAtlasGl>) on
  failure. One-shot at session startup, so blast radius is one
  failed bring-up = whole renderer dead. The failure-then-
  partial-leak shape is technically real but the failure is
  fatal anyway. Lowest severity; defer to a future "GLES
  startup cleanup audit" dispatch if appetite exists.

### §F.3 — Non-issues (verified clean)

- All `gl.create_framebuffer()?`, `gl.create_buffer()?`,
  `gl.create_program()?`, `gl.create_shader()?` callsites
  outside the scenarios above: verified by subagent as either
  in pure error paths (early-return before any prior alloc),
  or paired with explicit cleanup, or in patterns the r38b
  audit already verified.
- `eglCreateImageKHR` callsites OTHER than the Fix 2 site:
  none found; the DMABUF EGLImage import path is the only
  callsite in renderer/src.
- `add_framebuffer` / `lock_front_buffer` callsites: all 13
  verified clean per r38b deep-read (`qa/r38b-hdmi-cma-deep-read-2026-06-02.md`).

---

## §G — Open questions for qarl

**None expected.** This is defense-in-depth on already-flagged
candidates from the r38b audit. The fix shapes mirror existing
canonical patterns, and the failure mode (GL_OUT_OF_MEMORY
under CMA pressure) is the exact scenario the broader r38
arc is hardening against.

(Nice-to-have flagged: a synthetic unit test that mocks
glow::Context to inject create_texture failures + asserts
cleanup fires. The pure-glow mocking surface is non-trivial —
glow uses extern function pointers via the Context, not a
trait. Skipped per dispatch's "if cheap" qualifier. The
canonical-pattern-mirroring approach + subagent review provides
the same confidence the r38b transition-closure fix relied on.)

---

## Push posture

Single commit. Pre-push hook will run cargo test + cross-build;
both should pass cleanly (the fixes only ADD match-arm branches
on previously-unreachable error paths; no test surface change).

— jimmy:openmarquee-code1 (lane: r38b/r40 non-FYS defense-in-depth)
