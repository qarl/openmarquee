# r41 — Capture-path + GLES-startup allocator cleanup

**Author lane:** code1 (continuing the r38b→r40 allocator-
defense arc).

**Scope:** the 2 §F.2 sites my r40 subagent surfaced + parked
because they were lower-severity (capture/debug path + session-
startup fatal-on-fail). r41 ships both with the canonical
match-arm cleanup pattern.

**Origin/main HEAD at fix time:** `f14c3b1` (my r40).

---

## §1 — Fix 1: `capture_fullres_transition_mid_to_png` cap_tex cleanup

### Location

`renderer/src/hdmi.rs:5177-5195` (pre-fix) — the `cap_tex`
glGenTextures call in `capture_fullres_transition_mid_to_png`,
after `make_fullres_slide_fbo_with_motion` has allocated
`fbo_a`/`tex_a` (line 5155) and `fbo_b`/`tex_b` (line 5161).

### Failure mode

```
(fbo_a, tex_a) = make_fullres_slide_fbo_with_motion(...)?
(fbo_b, tex_b) = make_fullres_slide_fbo_with_motion(...) {
    Ok(p) => p,
    Err(e) => { delete fbo_a + tex_a; return Err(e) }
}
// fbo_a/tex_a/fbo_b/tex_b alive = ~16 MB FBO + RGBA texture
// storage at 1080p (2× 1920×1080×4 = 16 MB).
cap_tex = gl.create_texture()?  // line 5180 — FAILS, ?-bubble
// fbo_a/tex_a/fbo_b/tex_b LEAK. The deferred cleanup at the
// function's success path (lines ~5234+) is past the bubble.
```

### Trigger surface

- `capture_fullres_transition_mid_to_png` is called by
  capture / debug tooling (golden-test generation,
  qarl-direct `--capture-transition-mid` CLI invocations).
- Under CMA pressure, `gl.create_texture` can return Err with
  GL_OUT_OF_MEMORY — exactly the scenario the broader r38 arc
  is hardening.
- NOT in the production paint hot path (per the dispatch's
  characterization as "lower severity / capture/debug").

### FYS-relevance

**Indirect.** This path is not in FYS's per-frame hot loop,
but capture tooling DOES run against FYS for golden-image
production. Real bug for any flock member generating capture
PNGs under CMA pressure.

### Sibling-pattern reference

`capture_legacy_3pass_transition_mid_to_png` at hdmi.rs:4951-4960
already implements the correct cleanup pattern verbatim:

```rust
let t_ = gl
    .create_texture()
    .map_err(|e| {
        gl.delete_framebuffer(fbo_a);
        gl.delete_texture(tex_a);
        gl.delete_framebuffer(fbo_b);
        gl.delete_texture(tex_b);
        anyhow!("capture tex: {e}")
    })?;
```

r41 brings the fullres sibling in line with this pattern. The
fix is a direct mirror — same 4-line cleanup block, same
context-tag.

---

## §2 — Fix 2: `sdf_atlas_gl.rs:upload_all` per-iteration cleanup

### Location

`renderer/src/sdf_atlas_gl.rs:42-103` (pre-fix) — `upload_all`
loops over `&[MsdfAtlas]` and pushes each uploaded GL texture
into a local `out: Vec<MsdfAtlasGl>`. Two `?`-bubble sites:

1. Line 54: `.map_err(|e| anyhow!(...))?` after `create_texture`.
2. Line 88-94: `if err != 0 { return Err(...) }` after the
   tex_image_2d + tex_parameter_i32 calls (already cleans the
   current tex, but not prior).

### Failure mode

```
loop {
    tex = gl.create_texture()?    // line 54 — FAILS, ?-bubble
    // OR
    tex_image_2d(...); ...
    if get_error() != 0 { gl.delete_texture(tex); return Err(...) }
    out.push(MsdfAtlasGl { tex, ... })
}
// In either failure branch, `out` is dropped on function exit.
// MsdfAtlasGl has no Drop impl for tex (it's a glow::NativeTexture
// = u32 handle alias); delete_all() is only called by the caller
// via the SUCCESS path. The caller never receives `out` on Err, so
// its textures stay alive until the GL context dies at session
// teardown.
```

Plus: `gl.pixel_store_i32(UNPACK_ALIGNMENT, 1)` set at line 50 is
never restored to 4 on the failure path. GL state leak (other
texture uploads downstream get UNPACK_ALIGNMENT=1) — not memory
but unwanted shared state.

### Trigger surface

- Session bring-up only (called once from `with_egl_session`
  via the atlas upload pipeline).
- Each failure terminates the bring-up with a fatal Err — the
  renderer doesn't survive. So "leaks" are reaped when the
  process dies.

### FYS-relevance

**None practically** — session-startup fatal failures kill the
process which releases all GL state. The fix is "make the
canonical pattern survive future refactors" per the dispatch's
"code doesn't carry a leak-on-error pattern that a future
refactor might keep" framing.

### Fix shape

A `cleanup_partial` closure that walks `out.drain(..)` calling
`gl.delete_texture` on each entry's `tex` field, plus restores
`UNPACK_ALIGNMENT=4` and unbinds the current texture. Called
from BOTH `?`-bubble sites before propagating Err.

The closure is defined OUTSIDE the existing `unsafe { }` block
(top of function) with its own `unsafe { }` body — same pattern
as `cleanup_static` in `paint_and_present_one_transition_frame`
(r38b reference at hdmi.rs:4245-4251).

LOC: ~25 (closure def + 2 call sites + replacement-pattern of
the bare `?` at line 54 with a match-arm).

---

## §3 — Pattern alignment

Both fixes mirror existing canonical references:

- **Fix 1** is a verbatim mirror of the sibling
  `capture_legacy_3pass_transition_mid_to_png` at hdmi.rs:4906-4912.
- **Fix 2** mirrors the `cleanup_static` closure pattern from
  `paint_and_present_one_transition_frame` (r38b SHA `5ac3ca2`
  at hdmi.rs:4245-4251) — closure-captured-state cleanup with
  explicit per-resource release.

Both extend the r38b→r40 allocator-defense arc:
- **r38b**: transition closure scanout-target cleanup (16 MB
  bake-FBO leak fix)
- **r40 Fix 1+3**: NV12 y_tex orphan on uv_tex create fail (×2)
- **r40 Fix 2**: EGLImage leak on tex create fail (DMABUF path)
- **r41 Fix 1**: capture fullres cap_tex create fail leaking
  fbo_a/tex_a/fbo_b/tex_b
- **r41 Fix 2**: atlas upload partial-leak on per-iteration fail

---

## §F — Adjacent sweep findings (§F.new)

The sacred subagent scanned `/tmp/r41-work/renderer/src/` for
similar shapes — multi-step alloc/init sequences with mid-
sequence `?`-bubbles where the prior alloc would leak.

### §F.1 — SUSPECT (deferred to r42+)

- **`renderer/src/v4l2.rs:1089` — `allocate_buffers` EXPBUF loop.**
  Per-iteration `vidioc_expbuf(...)?` accumulates exported
  dma_buf fds into `fds: Vec<RawFd>` (line 1100 push). On
  iteration N>0 failure, fds 0..N-1 leak as open file descriptors
  -- the assignment `inner.capture_dmabuf_fds = fds;` at line 1102
  only happens after the loop completes successfully. `RawFd`
  has no Drop semantics; `DecoderInner::drop` only closes
  `self.capture_dmabuf_fds` which stays empty on this error path.

  **Same structural shape as r41 Fix 2** (partial-Vec-on-loop-Err
  leak), but different resource type (file descriptors vs GL
  texture handles) and different ownership model (kernel-owned
  dma_buf vs gl-owned texture). Per-session leak that compounds
  across video-session retries. **Low severity** because the
  failure mode is rare (EXPBUF mid-loop is unusual) and FYS
  doesn't run V4L2 video at all.

  Deferred per dispatch's "Pace yourself" + the subagent's own
  "out of r41 scope; flag for future allocator-defense round"
  recommendation. Would be a clean r42+ "v4l2 + non-GL allocator
  cleanup" dispatch — mirrors the r41 cleanup_partial closure
  pattern but adapted to `libc::close(fd)` instead of
  `gl.delete_texture`.

### §F.2 — SAFE (no action)

- **`renderer/src/gl_subtexture_smoke.rs:92`** — `gl.create_framebuffer()?`
  at line 92 (plus `?`-bubbles at lines 117 + 156) would leak
  `tex` (line 52) and `{tex, fbo}` respectively. **But**:
  `gl_subtexture_smoke` is a diagnostic smoke harness invoked
  inside `run_in_egl_session`. EGL teardown on Err reaps all GL
  state attached to the session. NOT in production hot path.
  Classify SAFE.

### §F.3 — Verified clean

All other `gl.create_*?` / `eglCreate*` / `add_framebuffer` /
`lock_front_buffer` callsites: clean per the cumulative
r38b + r40 + r41 sweep. The subagent re-verified no new sites
emerged from the r41 reading pass.

---

## §G — Open questions for qarl

**None expected.** Both fixes mirror existing canonical patterns,
both are flagged-by-prior-audit follow-ups, no design decisions
needed.

(Nice-to-have flagged: `sdf_atlas_gl.rs:upload_all` is currently
fail-fast. If a future "graceful degradation" stance wants the
renderer to come up with N-1 atlases instead of dying, the
cleanup_partial pattern + a per-atlas Err warn-and-skip would
be the natural shape. Out of r41 scope.)

---

## Push posture

Single commit. Pre-push hook will run cargo test + cross-build;
both should pass cleanly (fixes add cleanup branches on
previously-unreachable error paths; no test surface change).

— jimmy:openmarquee-code1 (lane: r38b/r40/r41 allocator defense)
