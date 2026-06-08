# r83 Phase A — green-line root-cause probe (V4L2 CAPTURE geometry)

**Author:** jimmy:openmarquee-code2
**Date:** 2026-06-08
**Status:** SHIPPED on code2; cherry-picked to main; awaiting QA FYS deploy + journal capture
**Dispatch:** code2 dispatch (parallel to code1's r82 transition fix)
**Symptom on FYS (qarl, 2026-06-08):** "when playing these new
  videos there is a line of green pixels at the bottom of the
  screen."

## Hypothesis (per dispatch, confirmed by code-read; awaiting empirical confirmation)

The 1080p H.264 test videos in the new 4-slide playlist decode via
bcm2835-codec, which **rounds the CAPTURE allocation up to the
next H.264 macroblock multiple** — 1080 / 16 = 67.5 → rounded up
to 68 macroblocks = **1088 rows**. The bottom **8 rows** of every
NV12 frame are uninitialised padding. When the BT.709 limited-
range shader samples luma=0 + chroma=128 (the canonical zeroed
NV12 state), the output RGB clamps to a bright green strip on the
bottom row of the displayed frame.

The kernel exposes a `VIDIOC_G_SELECTION` ioctl with
`target = V4L2_SEL_TGT_COMPOSE` that returns the actually-valid
display region — `(0, 0, 1920, 1080)` against the
`(1920, 1088)` allocation. The renderer never calls this ioctl
today; it uses the full S_FMT-negotiated dimensions
(`cap_fmt.width`, `cap_fmt.height`) everywhere downstream.

## Code-read confirmation (no FYS data required yet)

The structural chain:

1. **`renderer/src/v4l2.rs:1486-1487`** — `next_frame()` snapshots
   `width = cap_fmt.width` and `height = cap_fmt.height` from the
   negotiated CAPTURE format. On bcm2835-codec + 1080p input,
   these are **1920 × 1088** (not 1920 × 1080).
2. **`Frame.width()` / `Frame.height()`** at `v4l2.rs:737-738`
   simply return the cached values, so the GL side sees 1088.
3. **`renderer/src/hdmi.rs:6605-6606`** —
   `bake_video_slide_to_current_fbo` reads
   `let f_w = frame.width(); let f_h = frame.height();` and
   passes those to:
   - `cover_quad_vbo(session.gl, f_w, f_h, mode_w, mode_h)` — the
     1088 height enters the aspect-ratio math, producing a quad
     that's ~0.7 % taller than expected (a sub-pixel error visible
     only on careful inspection — not the green strip).
   - `run_nv12_dmabuf_blit_pass(... f_w, f_h, stride)` — uploads /
     imports the **full 1088 rows of luma + 544 rows of chroma**
     into the GLES texture.
4. **`renderer/src/hdmi_logic.rs:2880-2904`** —
   `FS_NV12_TO_RGB` samples `texture2D(u_tex_y, vec2(v_uv.x,
   1.0 - v_uv.y))`. With `v_uv.y = 0.0` at the top of the fullscreen
   quad and `v_uv.y = 1.0` at the bottom, the flipped sample at
   `1.0 - v_uv.y = 0.0` reads texture row 0 (top of allocation —
   **valid pixels for V4L2 NV12's bottom-up layout**), and
   `1.0 - v_uv.y = 1.0` reads texture row 1087 (the 8th row of the
   uninitialised pad).

The shader's BT.709-limited-range decode of NV12 (Y=0, U=V=128)
produces:

```
y' = (0/255 - 16/255) × (255/219) = -0.073
u' = (128/255 - 128/255) × (255/224) = 0
v' = (128/255 - 128/255) × (255/224) = 0
r  = y' + 1.5748 × v' = -0.073
g  = y' - 0.1873 × u' - 0.4681 × v' = -0.073
b  = y' + 1.8556 × u' = -0.073
```

All channels negative → GLES2 clamps to 0 → **black**. So pure
zero NV12 padding decodes to BLACK, not green.

The empirically-observed green tells us the codec is NOT zeroing
the padding rows — it's leaving them at whatever the kernel
allocator handed it. The kernel CMA carveout is reused across
allocations + the GBM pool churn during text-over-video means
the padding is stale data, often dominated by green chroma
patches. The fact that the line is consistently green (not
random across plays) suggests the codec writes the same "pad
with previous frame's NV12 data" pattern each time.

Either way — black or green — the fix is identical: **stop
sampling those rows.** G_SELECTION(COMPOSE) gives us the rect.

## Phase A — instrumentation shipped (this commit)

Two additions to `renderer/src/v4l2.rs`:

1. **`VIDIOC_G_SELECTION` ioctl** registered via `nix::ioctl_readwrite!`.
2. **`Decoder::capture_compose_rect()`** method returning
   `Result<Option<V4l2Rect>>`. `Ok(Some(rect))` on success;
   `Ok(None)` on `ENOTTY` (driver doesn't implement) or `EINVAL`
   (target not recognised); `Err` for genuine failures.
3. **eprintln probe inside `set_format`** after a successful
   CAPTURE S_FMT, logging both the pixfmt dims AND the compose
   rect side-by-side:

   ```
   [perf] v4l2_capture_geometry pixfmt_w=1920 pixfmt_h=1088 \
     plane_stride=1920 compose_x=0 compose_y=0 \
     compose_w=1920 compose_h=1080
   ```

   On a driver that doesn't expose COMPOSE:

   ```
   [perf] v4l2_capture_geometry pixfmt_w=1920 pixfmt_h=1088 \
     plane_stride=1920 compose=unsupported
   ```

   On a genuine ioctl failure:

   ```
   [perf] v4l2_capture_geometry pixfmt_w=1920 pixfmt_h=1088 \
     plane_stride=1920 compose_err=<errno>
   ```

The probe runs at every CAPTURE S_FMT — once per decoder init,
including the preload-path warmup and the per-slide
`prime_video_decoder`. Output goes to journald via the standard
sidecar capture (`journalctl -u openmarquee-backend.service`).

## What QA needs to do (Phase A verification)

After QA deploys this commit's binary to FYS and plays the test
playlist:

```
journalctl -u openmarquee-backend.service --since "10 minutes ago" \
    | grep "v4l2_capture_geometry"
```

Expected (confirms hypothesis):

```
[perf] v4l2_capture_geometry pixfmt_w=1920 pixfmt_h=1088 \
  plane_stride=1920 compose_x=0 compose_y=0 compose_w=1920 compose_h=1080
```

If `compose_h == pixfmt_h`, the hypothesis is REFUTED and the
green line has a different cause (color-space conversion off-by-
one, encoder-side green padding, panel timing). Phase B's
implementation plan below assumes the expected output; if QA
sees the refutation case, Phase B becomes a separate
investigation.

## Phase B — fix plan (NOT in this commit)

If Phase A confirms `compose_h < pixfmt_h`:

1. **Cache the compose rect.** `Decoder::set_format(Capture, ...)`
   already calls G_SELECTION; persist the returned rect onto
   `DecoderInner` so `next_frame` can pass `(width, height,
   compose_h)` triple to the Frame. Alternative: add a separate
   `Decoder::display_height()` accessor that callers must invoke.
   First option keeps Frame self-describing.

2. **Plumb display dims to bake.** `bake_video_slide_to_current_fbo`
   reads `f_w` and `f_h` from `frame`. Add `f_disp_h = frame.
   display_height()`; pass it to `cover_quad_vbo` (for correct
   aspect math) and into the GLES pass.

3. **Adjust UV sampling in `FS_NV12_TO_RGB` (and
   `FS_NV12_DMABUF_TO_RGB`).** Default approach per dispatch:
   add a `u_uv_y_scale: float` uniform = `compose_h / pixfmt_h`
   (= 1080/1088 ≈ 0.99265 for the FYS test videos), and clip
   sampling to `[0, u_uv_y_scale]` on the `1.0 - v_uv.y` axis:

   ```glsl
   vec2 uv_t = vec2(v_uv.x, (1.0 - v_uv.y) * u_uv_y_scale);
   ```

   That maps the fullscreen quad's bottom row (`v_uv.y = 1.0` →
   `1.0 - 1.0 = 0.0`) to texture row 0 (unchanged) and the top
   row (`v_uv.y = 0.0` → `1.0 - 0.0 = 1.0`) to texture row
   `pixfmt_h × u_uv_y_scale = 1080`, skipping the 8-row pad.

   Default `u_uv_y_scale = 1.0` so existing callers (external
   NV12 push path, image bake, etc.) that don't query
   G_SELECTION continue to behave as today.

4. **Same fix in the DMABUF path.** `FS_NV12_DMABUF_TO_RGB` at
   `hdmi_logic.rs:~3193` samples via `samplerExternalOES`; the
   underlying EGLImage import has size = pixfmt_w × pixfmt_h, so
   the same UV-clip applies.

5. **Transition bake.** `paint_and_present_one_transition_frame`
   composites video endpoints — per dispatch, the EOS-flush path
   is r82's surface and must NOT be touched. The fix is local to
   the bake helper: as long as the bake passes the cropped UVs
   into the shader, the transition compositor (which samples the
   bake's output FBO, not the V4L2 texture directly) is
   unaffected.

6. **Cover-fit math.** `cover_quad_vbo(gl, f_w, f_h, mode_w,
   mode_h)` uses `f_h` for the aspect ratio. For correctness it
   should use the display height (1080), not the pixfmt height
   (1088). 8/1088 ≈ 0.7 % aspect error is sub-pixel but worth
   correcting alongside the green-line fix.

7. **No `Frame::display_height()` for the chroma plane.** NV12's
   UV plane is at half height. The same `u_uv_y_scale` clips the
   bottom 4 chroma rows out via the same shader formula — the
   chroma sampling uses the same `uv_t` vector, so the math
   composes automatically.

## What's NOT touched

Per dispatch constraints:

- **`paint_and_present_one_transition_frame`** — code1's r82
  surface. NOT touched in this commit.
- **EOS-flush path** — code1's r82. NOT touched.
- **MMAL leak** — code1's r75. NOT touched.
- **Deploy** — QA owns FYS deploys; this commit ships the binary,
  QA bundles + deploys.

## Files

| File                                                      | Change                                                              | LOC |
| --------------------------------------------------------- | ------------------------------------------------------------------- | --- |
| `renderer/src/v4l2.rs`                                    | + `V4l2Rect`, `V4l2Selection` struct types                          | ~28 |
|                                                           | + `V4L2_SEL_TGT_COMPOSE` constant                                   | ~8  |
|                                                           | + `vidioc_g_selection` ioctl_readwrite!                             | ~18 |
|                                                           | + `Decoder::capture_compose_rect()` + internal helper               | ~50 |
|                                                           | + eprintln probe in `set_format` after CAPTURE S_FMT                | ~37 |
| `qa/r83-phase-a-green-line-probe-2026-06-08.md`           | This audit doc                                                      | -   |

Total: ~141 LOC in `renderer/src/v4l2.rs` + the audit doc. No
edits to `hdmi.rs` / `hdmi_logic.rs` / `mp4_demux.rs` in this
commit — those wait for Phase B once FYS journal confirms the
hypothesis.

## Test posture

- `cargo check`: clean (only pre-existing `unused import` warnings
  unrelated to this change).
- `cargo test`: **545/545 PASS** locally (was 545 at r71 — no
  net change; new code is reachable only via real V4L2 ioctls
  which the host tests don't exercise).
- v4l2-scoped test subset: **7/7 PASS**.
- The G_SELECTION ioctl is Linux-only behind the existing
  `#[cfg(target_os = "linux")]` gating; macOS cross-compile builds
  the const + structs but the ioctl wrapper itself is gated.

## Sacred subagent review

Pending — runs before this commit.

Reviewer should pressure-test:

1. **`V4l2Selection` struct layout matches the kernel definition.**
   Size 64 bytes: 4 (buf_type) + 4 (target) + 4 (flags) +
   16 (v4l2_rect) + 36 (reserved [u32; 9]) = 64. Match against
   `<linux/videodev2.h>` `struct v4l2_selection`.
2. **`V4l2Rect` field order.** Kernel definition: `left, top,
   width, height` with `left/top` as `s32`. My struct uses
   `i32` (Rust's signed-32) for those — verify the type sizes
   match.
3. **ioctl request number.** `_IOWR('V', 60, v4l2_selection)`.
   Verify against kernel videodev2.h.
4. **`V4L2_SEL_TGT_COMPOSE` value 0x0100.** Verify against
   kernel videodev2.h.
5. **Best-effort error handling.** `ENOTTY` and `EINVAL` both
   map to `Ok(None)`. Verify these are the right "soft failure"
   errnos vs the ones that should surface as `Err`.
6. **No edits to r82 / r75 surfaces.** Verify the diff is
   confined to `v4l2.rs` (struct/ioctl/method additions + one
   eprintln after CAPTURE S_FMT) — nothing in `hdmi.rs`,
   `hdmi_logic.rs`, `paint_and_present_one_transition_frame`,
   or the EOS-flush path.
7. **Probe runs unconditionally on CAPTURE S_FMT.** Verify the
   `matches!(dir, QueueDirection::Capture)` guard is correct,
   and the probe does NOT fire on OUTPUT S_FMT (where COMPOSE
   semantics differ).

## Lane

- 1 renderer/src/ file + 1 audit doc.
- code2 push; cherry-pick to main via /tmp clone.
- Pre-push hook applies (renderer/ touched): backend pytest,
  renderer cargo test, aarch64 cross-compile, UI vitest.
- No SYSTEM_SPEC.md edits.
- No CHANGELOG churn (Phase A instrumentation; release-notes
  refresh after Phase B closes the green line).

## §G — Open questions

### G.1 OUTPUT-side compose query?

The probe runs on CAPTURE S_FMT only. The OUTPUT queue (H.264
compressed-in) doesn't have a meaningful COMPOSE rect on a
decoder — the input is bitstream, not pixels. If a future
encoder integration ever needs it, the same method generalises
trivially (pass `QueueDirection` to `capture_compose_rect`).

### G.2 SEL_TGT_CROP vs SEL_TGT_COMPOSE?

V4L2 distinguishes:
- `SEL_TGT_CROP` (`0x0000`): on OUTPUT, what sub-rect of the
  input is decoded; on CAPTURE, what sub-rect of the source is
  available.
- `SEL_TGT_COMPOSE` (`0x0100`): what sub-rect of the allocated
  CAPTURE frame is to be displayed.

For a video decoder, COMPOSE on CAPTURE is the right query (per
the kernel docs and the bcm2835-codec source). CROP would be
the source-side "what's available" — same value for non-cropping
encoders, but COMPOSE is the spec-correct accessor for our use.

### G.3 Defer the eprintln when COMPOSE matches pixfmt?

Currently we always log the geometry line, even when
`compose_h == pixfmt_h` (no padding present). That keeps the
log signal-symmetric across decoder inits and lets QA grep
unconditionally. Marginal log volume cost; defensible.

---

End of r83 Phase A.
