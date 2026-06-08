# r83 Phase B — green-line crop fix (NV12 padding-row mitigation)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-08
**Status:** SHIPPED on code2; cherry-picked to main; awaiting QA FYS deploy + visual verify
**Dispatch:** code2 Phase B dispatch from Jimmy-openmarquee-qa
**Predecessor:** r83 Phase A (code2 c1f93ce / main ad12813) — added
  the V4L2 `G_SELECTION(COMPOSE)` probe.

## Phase A data recap (from QA)

QA built code2's Phase A binary, deployed to fresh-rebooted FYS,
played the 1080p H.264 test playlist. Probe log line:

```
[perf] v4l2_capture_geometry
  pixfmt_w=1920 pixfmt_h=1088
  plane_stride=1920
  compose=unsupported
```

Sampled across 8 distinct video opens; same values every time.

**Verdict (QA):**
- Padding hypothesis CONFIRMED: 1920 × 1088 alloc for a 1920 × 1080
  source. 8 rows of macroblock-rounded padding.
- `G_SELECTION(COMPOSE)` REFUTED as the data source for the display
  height: bcm2835-codec returns ENOTTY (Phase A's `compose=unsupported`
  log shape). The Phase A method `Decoder::capture_compose_rect()`
  still exists + works correctly on any driver that DOES support
  COMPOSE; it's just not load-bearing on FYS.

## What this commit does

Phase B uses the **REQUESTED CAPTURE height** as the display-h truth
source (QA dispatch's Option 2), threads it to the GL paint, and
modifies the FS_NV12 shaders to skip the padding rows.

### Why `capture_display_height` = requested height (not source dim)

The MP4 demuxer at `mp4_demux.rs:98-100` parses the video track's
width/height from the avc1 sample entry. `prime_video_decoder` at
`ipc_main.rs:707-708` reads those as `w = dem.width; h = dem.height`
and passes them VERBATIM to `set_capture_format(NV12, w, h)`. The
kernel returns the rounded-up dim (1088 for 1080) in the
NegotiatedFormat.

Snapshotting "what we requested" at `set_capture_format` time IS
the source dim — `prime_video_decoder` is the only caller that
goes through this path in production. The snapshot is captured
inside `set_format(Capture, ...)` at `v4l2.rs:set_format` so it
survives across re-priming.

If a future caller passes a request dim that ISN'T the source
(e.g. some explicit display-window override), this design surfaces
that dim — and the GL paint correctly crops to it. That's the
right behavior; the spec for the field is "the actually-valid
display region inside the kernel's CAPTURE allocation", and the
requested height IS that region by definition.

## Implementation

### `renderer/src/v4l2.rs` (+95 LOC across the diff)

**New `DecoderInner` field** (Linux-gated, set inside `set_format`):

```rust
/// r83 Phase B: snapshotted at set_capture_format time.
/// 0 = sentinel for "not yet set". For bcm2835-codec 1080p
/// input this is 1080 while capture_format.height is 1088.
capture_display_height: u32,
```

**Set point** inside `set_format`:

```rust
QueueDirection::Capture => {
    inner.capture_format = Some(neg.clone());
    inner.capture_display_height = height;  // the REQUESTED height
}
```

**New accessors:**

```rust
pub fn capture_display_height(&self) -> Option<u32>;
pub fn capture_y_crop_max(&self) -> f32;
```

`capture_y_crop_max()` delegates to a pure-Rust helper
`compute_y_crop_max(display_h, alloc_h) -> f32` that fail-softs to
`1.0` (= no crop) on:
- display height not yet set (sentinel 0)
- allocated height not yet negotiated (`capture_format = None` →
  treated as 0)
- `display >= alloc` (would yield a ratio ≥ 1.0; identity)

Otherwise returns `display / alloc` as `f32 ∈ (0, 1)`.

The helper is cross-platform (no Linux gating) so the Mac-side host
tests can exercise it. 2 new unit tests cover the fail-soft branches
and the canonical 1080/1088 case.

### `renderer/src/hdmi_logic.rs` (shader updates)

Both `FS_NV12_TO_RGB` (MMAP path) and `FS_NV12_DMABUF_TO_RGB` get a
new uniform + modified UV computation:

```glsl
uniform float u_y_crop_max;
// ...
vec2 uv_t = vec2(v_uv.x, (1.0 - v_uv.y) * u_y_crop_max);
```

With `u_y_crop_max = 1.0` the math reduces to the pre-Phase-B
formula `uv_t = vec2(v_uv.x, 1.0 - v_uv.y)` — byte-identical
fallback behavior when source dims aren't known. With
`u_y_crop_max = 1080/1088 ≈ 0.99265`, the flipped-v sampling stays
in `[0, 0.99265]` of texture y-space, leaving the high-y rows
(1080-1087 in absolute texture-row terms) unsampled.

The same crop applies to BOTH Y and UV planes because NV12
sub-samples 2:1 on both axes — the relative padding ratio is the
same (within a half-row rounding). Sub-pixel UV bleed at the very
bottom of the displayed frame is < 0.2 % of frame height and
clamped by `TEXTURE_WRAP_T = CLAMP_TO_EDGE`.

4 new shader-string assertions pin both the uniform declaration
and the formula application:

```rust
assert!(FS_NV12_TO_RGB.contains("uniform float u_y_crop_max"));
assert!(FS_NV12_TO_RGB.contains("(1.0 - v_uv.y) * u_y_crop_max"));
// same pair for FS_NV12_DMABUF_TO_RGB
```

### `renderer/src/hdmi.rs` (uniform plumbing + call-site updates)

**Cached-program structs** gain a `u_y_crop_max` slot:

```rust
struct CachedNv12Program {
    // ...existing fields...
    u_y_crop_max: Option<glow::NativeUniformLocation>,
}
// same addition on CachedNv12DmaBufProgram
```

**Lazy lookups** query `gl.get_uniform_location(program, "u_y_crop_max")`
and stash it.

**Blit-pass signatures grow a `y_crop_max: f32` parameter:**

```rust
unsafe fn run_nv12_blit_pass(
    gl, vbo, y_tex, uv_tex,
    y_crop_max: f32,  // NEW
) -> Result<()>;

pub unsafe fn run_nv12_dmabuf_blit_pass(
    gl, vbo, egl_lib, display, fd, width, height, stride,
    y_crop_max: f32,  // NEW
) -> Result<bool>;
```

Both set the uniform via `gl.uniform_1_f32(cnp.u_y_crop_max.as_ref(), y_crop_max)`
EVERY pass — defensive against Mesa's default-0 on the first
frame. GLES2 DOES preserve uniform values across `use_program`
calls on the same program object, but the very first call after
program link has the uniform at Mesa's default of `0.0`, which
would short-circuit `(1.0 - v_uv.y) * 0` for every texel and
collapse the frame to texture row 0 (= source bottom). Setting
every pass is one extra ioctl-equivalent per frame — negligible
cost, eliminates an entire failure class.

**`bake_video_slide_to_current_fbo`** queries the decoder ONCE at
the top of the function and passes the value down both paths:

```rust
let y_crop_max = decoder.capture_y_crop_max();
// ...
if let Some(fd) = frame.dma_buf_fd() {
    run_nv12_dmabuf_blit_pass(... y_crop_max)?;
} else {
    run_nv12_blit_pass(gl, cover_vbo, y_tex, uv_tex, y_crop_max);
}
```

The lock is acquired + released inside `capture_y_crop_max()`; the
GL paint holds only the resulting `f32`. No lock spans GL ops.

### Transition-bake compositor coverage

`bake_video_slide_to_current_fbo` is called from TWO sites:

| Caller                                            | Line   |
| ------------------------------------------------- | ------ |
| `paint_and_present_one_video_slide_frame`         | ~3709  |
| `paint_and_present_one_transition_frame`          | ~7153  |

Both go through the same bake helper → the crop fix applies to
BOTH the in-slide paint AND the transition compositor
automatically. Per dispatch: "Make the fix correct in both paths
anyway."

## Direction-of-padding assumption

QA's dispatch flagged direction-uncertainty explicitly:

> "Check whether you got the ratio in the right direction (a
> flipped y could end up sampling 8 rows of garbage at the TOP
> instead of the bottom)"

The shipped formula `(1.0 - v_uv.y) * u_y_crop_max` assumes:
- Padding rows live at the END of the buffer (high addresses /
  high texture-y).
- The existing v-flip routes texture-high-y to displayed-bottom.
- Cropping the high-y end of the sample range removes the
  displayed-bottom green.

This is the standard bcm2835-codec convention per the kernel
driver source. If FYS verify shows the green moved to the TOP
instead of disappearing, the fix becomes an offset rather than a
scale:

```glsl
// Hypothetical Phase B follow-up (NOT shipped):
uniform float u_y_crop_min;  // = (alloc_h - display_h) / alloc_h
// uv_t.y = u_y_crop_min + (1.0 - v_uv.y) * (1.0 - u_y_crop_min);
```

Empirical verification will tell. The shipped formula is the
dispatch's literal recommendation.

## What QA needs to do (Phase B verification)

1. Build code2's Phase B binary; deploy to FYS.
2. Play the same 4-slide 1080p test playlist as Phase A.
3. Photo of any video frame.

Expected:
- The bright green band at the bottom of every 1080p video
  frame should be gone.
- The video content should fill the screen normally (cover-fit
  unchanged from current behavior).

If green appears at the TOP instead of disappearing: the padding
is at LOW addresses, not HIGH. Ship Phase B follow-up adding the
offset uniform per the §"Direction-of-padding assumption" note
above.

If a small (~1-pixel) dark band appears at the very bottom: the
sub-pixel UV bleed described above is visible. Mitigation is to
add a smidge of margin to `y_crop_max` (e.g. `display_h - 1` in
the numerator) — also a Phase B follow-up.

## What's NOT touched

Per dispatch constraints:

- `paint_and_present_one_transition_frame` body (code1's r82 surface)
- EOS-flush path (code1)
- MMAL leak (code1's r75, dormant on 4-slide playlist)
- `mp4_demux.rs`
- No FYS deploy from code2 (QA owns FYS deploys)

`git diff --stat`:
```
renderer/src/hdmi.rs       ~45 LOC
renderer/src/hdmi_logic.rs ~20 LOC
renderer/src/v4l2.rs       ~95 LOC
qa/r83-phase-b-...md       (this file)
```

NO touches in `paint_and_present_one_transition_frame` body
itself; the fix lands in `bake_video_slide_to_current_fbo` which
the transition path already calls.

## Test posture

- `cargo check --tests`: clean (only pre-existing `unused import`
  warnings).
- `cargo test`: **547 PASS / 0 FAIL / 1 ignored** locally. Up
  from 545 — 2 new tests:
  - `compute_y_crop_max_no_crop_when_dims_unknown_or_equal`
  - `compute_y_crop_max_returns_ratio_for_padded_capture`
- The shader-string assertions (4 new) pin the uniform
  declaration + formula application in both FS_NV12_TO_RGB +
  FS_NV12_DMABUF_TO_RGB.

## Sacred subagent review

SAFE-TO-COMMIT (pending this audit doc landing alongside the code,
which it does — this commit includes the doc). Reviewer
pressure-tested 9 dimensions:

  A. Shader formula direction (matches dispatch literal; padding-at-END
     assumption matches bcm2835-codec convention; QA verify is
     the empirical confirmation)
  B. Uniform set every pass defensively (correctness in face of
     Mesa default-0 on first frame)
  C. Lock scope: `capture_y_crop_max()` lock drops inside the
     accessor; GL paint holds only the resulting `f32`
  D. Default 1.0 = no crop = byte-identical fallback verified
  E. NV12 sub-sampling: same ratio for Y + UV; sub-pixel UV bleed
     < 0.2% of frame, clamped by `WRAP_T = CLAMP_TO_EDGE`
  F. Transition-bake coverage: single point of truth in
     `bake_video_slide_to_current_fbo`; both call sites covered
  G. No r80/r81/r82 EOS-flush body touches; no r75 MMAL touches;
     diff confined to v4l2.rs + hdmi.rs + hdmi_logic.rs + this doc
  H. Build clean; 547/547 tests pass
  I. Lane discipline (no SYSTEM_SPEC, no .claude/, no emoji, no
     CHANGELOG)

2 NITs (both fixed pre-commit):
  - Comment overstated GLES2 uniform persistence ("GLES2 doesn't
    preserve uniforms across use_program") — fixed to say
    "GLES2 DOES preserve uniform values, but the first frame
    would read Mesa's default of 0".
  - Audit doc was pending → this doc.

1 NIT remaining (non-blocking, documented):
  - Sub-pixel UV bleed at the very bottom row — visually < 0.2%
    of frame, gated by CLAMP_TO_EDGE. If empirically visible,
    a Phase B follow-up trims `display_h - 1`.

## Lane

- 3 renderer/ files + 1 audit doc
- code2 push; cherry-pick to main via /tmp clone (Phase A's
  cherry-pick required manual conflict resolution where r46.4 +
  r83 Phase A added structs in the same struct/size-guard
  region; Phase B should be cleaner since both deltas are
  contained inside well-separated lexical sections)
- Pre-push hook applies (renderer/ touched)
- No SYSTEM_SPEC.md edits
- No CHANGELOG churn (deferred to Phase C once QA confirms green
  band gone on FYS)

## §G — Open questions

### G.1 Direction-of-padding empirical

Already covered in body — if green moves to TOP after deploy, ship
the offset-uniform follow-up.

### G.2 Sub-pixel UV bleed

Already covered — mitigation is `display_h - 1` if visible.

### G.3 Audit Phase A's `Decoder::capture_compose_rect()` for
removal?

Phase A's method is now dead code on bcm2835-codec FYS but still
valuable on any driver that implements COMPOSE. Per
`feedback_perf_audit_enumerate_allocators`, keep methods that
work-when-the-kernel-cooperates rather than removing them
preemptively. Park for now; revisit if/when we add a non-FYS
deployment.

---

End of r83 Phase B.
