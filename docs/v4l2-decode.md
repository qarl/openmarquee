# V4L2 H.264 decode on the Pi: device path + arc roadmap

VideoSlide rendering through the Rust IPC sidecar route depends on
the Pi's hardware H.264 decoder, exposed as a V4L2 M2M (Memory-to-
Memory) device. This doc captures what's already in place + the
arc roadmap for wiring the actual decode path.

## Device path + kernel state (verified 2026-05-14 on dev Pi)

**Device:** `/dev/video10` — `bcm2835-codec-decode` (platform driver).

```
$ v4l2-ctl --list-devices | head
bcm2835-codec-decode (platform:bcm2835-codec):
    /dev/video10
    /dev/video11
    /dev/video12
    /dev/video18
    /dev/video31
```

`/dev/video10` is the M2M decoder front-door; sibling `/dev/video11`
and `/dev/video12` are sub-devices the codec driver uses internally
(M2M devices typically expose multiple nodes for the OUTPUT and
CAPTURE queues; on bcm2835-codec the queues are multiplexed onto
`/dev/video10` and the others are scratch).

**Capabilities (per `v4l2-ctl -d /dev/video10 --info`):**
- `Video Memory-to-Memory Multiplanar`
- `Streaming`
- `Extended Pix Format`
- Driver: `bcm2835-codec`
- Kernel: `6.12.75` (Pi OS Lite trixie default at dev Pi provisioning)

**OUTPUT (compressed-in) formats:**
- `H264` — H.264 Annex-B byte stream
- `MPG4` — MPEG-4 Part 2 elementary stream
- `MJPG` — Motion-JPEG
- `H263` — H.263

**CAPTURE (decoded-out) formats:**
- `YU12` — YUV 4:2:0 planar
- `YV12` — YUV 4:2:0 planar (V/U swapped)
- `NV12` — Y/UV 4:2:0 semi-planar (Y plane + interleaved UV plane)
- `NV21` — Y/VU 4:2:0 semi-planar
- `NC12` — Y/CbCr 4:2:0 128-byte-column tiled
- `RGBP` — RGB565
- `AB24` — RGBA 8-8-8-8
- `BGR4` — BGRA/X 8-8-8-8

**Recommended capture format for piece 2:** `NV12`. Reasons:
- Semi-planar Y + UV is what most HW decoders emit natively (this
  one too — `RGBP`/`AB24` are software conversions the driver does
  for us, costing decode-side cycles we'd rather spend elsewhere).
- One Y texture + one UV texture = 2 GLES texture binds, vs 3 for
  YUV planar. Less state overhead.
- Single-pass YUV → RGB fragment shader is straightforward (one
  matrix multiply + offset, ~10 lines GLSL ES 2.0).
- Mature DMA-BUF zero-copy path (piece 4): NV12 buffers export
  cleanly as a single dma_buf fd with two-plane EGLImage on vc4.

## Kernel module autoload (no dtoverlay needed)

`bcm2835-codec` is part of the standard Pi linux kernel package
(`linux-image-rpi-*` from raspbian) and autoloads via udev when the
`vc4-kms-v3d` device tree node is present. That overlay is the
graphics overlay -- already in the stock `/boot/firmware/config.txt`
on Pi OS Lite trixie.

```
$ lsmod | grep -E "codec|v4l2|bcm2835"
bcm2835_codec       49152  0
bcm2835_v4l2        49152  0
bcm2835_isp         28672  0
bcm2835_mmal_vchiq  36864  3 bcm2835_codec,bcm2835_v4l2,bcm2835_isp
v4l2_mem2mem        45056  1 bcm2835_codec
```

So the V4L2 dispatch's "piece 1: kernel + tooling sanity" needs NO
dtoverlay template change to `system/openmarquee-firstboot.sh`.
Fresh SD-burns get the decoder for free.

## What WAS templated for piece 1

Two small `stage_sd_card.sh` changes:

1. The `openmarquee` system user is added to the `video` group (and
   `render` for tighter Pi OS Lite trixie udev defaults). Without
   this, `/dev/video10` -- root-owned `crw-rw---- video` -- raises
   `EACCES` when the rust-sidecar opens it.

2. `v4l-utils` is added to the package list. Not load-bearing for
   the rendering path (the Rust V4L2 client uses ioctls directly),
   but a Pi without `v4l2-ctl` is much harder to field-debug if
   VideoSlide goes sideways.

Both apply only via the SD-burn flow. Existing devices (like dev Pi)
already have the user in the right groups + v4l-utils installed.

## Arc status (pieces 2–4: SHIPPED 2026-05-13 → 2026-05-14)

The dispatch sized the full arc at 10-20h of focused Rust work,
broken into 5 pieces. Piece 1 was the bounded investigation +
SD-burn prep. Pieces 2–4 shipped on the 2026-05-13 → 14 flight;
piece 5 (backend wiring) was rolled into the slice-4 followup that
removed the `"video slides TBD"` Unsupported marker.

- **Piece 2 — Rust V4L2 client crate.** SHIPPED.
  - 2a (`343fe15`): `renderer/src/v4l2.rs` Decoder client scaffold
    + cap query. Used raw `libc` ioctls (the `v4l` crate didn't
    expose the multiplanar M2M variant cleanly).
  - 2b (`5f67ea5`): decode loop + mmap buffer pool + `Frame`
    lifetime via `Arc<Mutex<DecoderInner>>`.
- **Piece 3 — VideoSlide IPC wire.** SHIPPED.
  - 3a (`2dbe775`): hand-rolled `mp4_demux.rs` (no `mp4` crate dep;
    parses the simple-box structure inline) + 7 tests + 320×240 +
    720p fixtures.
  - 3b (`c56793b`): `SlideCache.video_demuxers` populated on
    `BeginSlide(Video)`.
  - 3c (`89f9591`): Linux-only `prime_video_decoder` opens
    `/dev/video10` + format-set + REQBUFS + STREAMON + SPS+PPS+IDR
    feed.
  - 3d (`6ffcb33`): `FS_NV12_TO_RGB` BT.601 limited-range shader +
    `CachedNv12Program` + `run_nv12_blit_pass`.
  - 3e (`e7be17f`): `paint_and_present_one_video_slide_frame` end-
    to-end; `validate_paint_slide_inputs` accepts Video; Python
    proxy classifier docstring updated.
  - 3f (live-Pi smoke, no commit): 720p smoke on dev Pi —
    150/150 PaintSlide responses, mean 28.55 ms, p99 46.48 ms,
    max 292 ms (first-frame spike), 70 MB RSS, no EAGAIN stalls.
- **Piece 4 — DMA-BUF zero-copy handoff.** SHIPPED (see §DMA-BUF
  zero-copy pathway below for the architecture).
  - 4a (`077642c`) + 4a-fix (`634eae2`): DMA-BUF CAPTURE wire via
    `VIDIOC_EXPBUF` + `CaptureBufferType::DmaBuf` mode +
    `Frame::dma_buf_fd()`.
  - 4b (`648cd54`): `FS_NV12_DMABUF_TO_RGB` shader using
    `samplerExternalOES` (`GL_OES_EGL_image_external` extension);
    Mesa driver handles YUV→RGB internally.
  - 4c (`9fcd4f1`): `run_nv12_dmabuf_blit_pass` —
    `eglCreateImageKHR` import + `GL_TEXTURE_EXTERNAL_OES` +
    external-OES program cache.
  - 4d (`89f97c8`): `paint_and_present_one_video_slide_frame`
    Mmap-vs-DmaBuf branch on `Frame::dma_buf_fd()`; opt-in via
    `OPENMARQUEE_RENDERER_DMABUF=1` (see §Diagnostics).
  - 4f (`07a6baa`): first-frame profile gate behind
    `OPENMARQUEE_FIRSTFRAME_PROFILE=1` (see §Diagnostics).
- **Piece 5 — Backend wiring.** SHIPPED in the slice-4 transitions
  followup. `_play_via_rust_ipc` already handled Video the moment
  piece 3e dropped the `UnsupportedSlideError` emission.

The rust-sidecar route is feature-complete for all 3 slide types.
Production default flip (`OPENMARQUEE_RENDERER=rust-sidecar` and
`OPENMARQUEE_RENDERER_DMABUF=1`) is qarl-eyeball-gated; perf is
sub-33ms p99 on both MMAP and DMABUF paths per piece 4f.

## DMA-BUF zero-copy pathway (piece 4)

Pieces 4a–c land the EXPBUF → EGLImage → external-OES → BT.601-via-
Mesa chain. The MMAP path (piece 3d) stays for comparison and as
the default until production-default-flip.

### Architecture

```
V4L2 CAPTURE queue (REQBUFS=MMAP, kernel allocates)
   │
   │  VIDIOC_EXPBUF per buffer  → dma_buf fd (kernel-allocated
   │                              NV12 buffer with an additional
   │                              dma_buf fd view referring to
   │                              the same memory)
   │
   ▼
eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT, attribs[fd,offset,stride])
   │
   │  attribs: 9 attribute pairs (Y plane + UV plane on same fd
   │           with UV at offset Y_SIZE) + EGL_NONE terminator.
   │
   ▼
EGLImage (NV12, BT.601 metadata via EGL_YUV_COLOR_SPACE_HINT_EXT)
   │
   │  glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, image)
   │
   ▼
samplerExternalOES (FS_NV12_DMABUF_TO_RGB)
   │
   │  Mesa vc4 driver handles YUV → RGB conversion internally on
   │  external-OES sampling (see §Mesa YUV→RGB note below). The
   │  shader does a straight RGB sample — no inline BT.601 math.
   │
   ▼
RGBA framebuffer  →  KMS scanout
```

### Why REQBUFS=MMAP and NOT REQBUFS=DMABUF (piece 4a-fix `634eae2`)

The intuitive mistake — fixed retroactively — was passing
`V4L2_MEMORY_DMABUF` to `VIDIOC_REQBUFS` to "request dma_buf
buffers." That is the **IMPORT** direction: userspace tells the
kernel "I'll give you a dma_buf fd when I QBUF." It does not
allocate buffers, so a subsequent `VIDIOC_EXPBUF` returns
`EINVAL` (there is nothing to export).

The canonical pattern to **EXPORT** kernel-allocated buffers as
dma_buf fds is:

```
REQBUFS(memory=V4L2_MEMORY_MMAP, count=N)
  → kernel allocates N CAPTURE buffers
for each buffer i in 0..N:
  EXPBUF(index=i, plane=0)
  → kernel returns an additional dma_buf fd view of buffer i
QBUF / DQBUF run as usual with memory=V4L2_MEMORY_MMAP
The dma_buf fd from EXPBUF refers to the same kernel buffer
as the mmap'd userspace pointer (just a different way to look
at it). Both can coexist; the dma_buf fd is what we pass to
eglCreateImageKHR.
```

Anchored in code at `renderer/src/v4l2.rs:900-920` (REQBUFS
always uses MMAP regardless of `CaptureBufferType`; EXPBUF runs
as a post-allocation step when `CaptureBufferType::DmaBuf`).

### Mesa YUV→RGB note (Pi vc4 driver fast-path)

The `FS_NV12_DMABUF_TO_RGB` shader has only one texture bind
(`samplerExternalOES` on the external-OES texture from the
EGLImage) and samples `vec4` RGB directly with **no inline BT.601
matrix**. This works because the Mesa vc4 driver detects an
external-OES NV12 EGLImage and inserts the YUV→RGB conversion
into the shader compile output internally. The shader source
looks naive ("just sample, get RGB") but the compiled binary
that vc4 runs has the conversion baked in.

The `FS_NV12_TO_RGB` shader (MMAP path, `renderer/src/hdmi_logic.rs`)
keeps the explicit BT.601 limited-range math because the MMAP
path uses two ordinary `sampler2D` binds (Y + UV planes uploaded
via `glTexImage2D`), and the driver has no signal to insert a
YUV→RGB conversion automatically. **Same image, different
shader, identical visual output** — verified in piece 4e smoke.

### Perf

Piece 4f live-Pi profile (1024×768 dev Pi, EDID-restricted) ran
the same test fixture through both paths back-to-back:

| Path | mean | p99 | max | over_33ms |
|------|------|-----|-----|-----------|
| MMAP (piece 3 default) | ~11 ms | ~15 ms | ~16 ms | 0 / N |
| DMABUF (piece 4 opt-in) | ~11 ms | ~12 ms | ~14 ms | 0 / N |

Sub-33ms target: mean YES, p99 YES on both paths. DMABUF wins
the tail (~3ms p99 advantage at 1024×768) and ties the mean. At
1080p where the MMAP upload crosses ~6 MB/frame the gap widens —
piece 3f's 720p smoke saw 28.55ms mean / 46.48ms p99 on MMAP
(at full 1280×720 NV12 textures before EDID restricted to
1024×768).

The 306ms first-frame max from the piece 4e MMAP measurement
(`qa/captures/v4l2-piece4e-dmabuf-smoke-2026-05-14.md`) did NOT
reproduce in piece 4f across 3 back-to-back runs — Pi-thermal /
cold-page-table artifact, not a codec or import cost.

## Diagnostics

Two env-var-gated diagnostics live in tree as future maintenance
aids:

- **`OPENMARQUEE_RENDERER_DMABUF=1`** (piece 4d). Default off →
  MMAP path (uploads Y + UV planes via `glTexImage2D` per frame).
  Set to `1` to opt into the DMA-BUF zero-copy path (EXPBUF +
  EGLImage import). Default ships off so production-default-flip
  is an explicit deliberate change after qarl color-quality
  eyeball at the office. Anchored at
  `renderer/src/ipc_main.rs:237-242`.
- **`OPENMARQUEE_FIRSTFRAME_PROFILE=1`** (piece 4f, `07a6baa`).
  Default off. When set, the paint helper logs per-checkpoint µs
  timings between `Decoder::new_h264`, first dequeue, first
  EGLImage import (DMABUF path) or first `glTexImage2D` upload
  (MMAP path), and first paint. Gate condition is
  `next_sample_idx == 1 && frames_decoded == 0` so the
  instrumentation fires once per session. Zero overhead when off.
  Anchored at `renderer/src/hdmi.rs:3560` (DMABUF path) +
  `renderer/src/hdmi.rs:6418` (MMAP path) — line numbers refreshed
  2026-05-31 (file restructured during the perf-night arc; the
  gate logic is unchanged).

## Quantization range — known latent gap

The MMAP-path `FS_NV12_TO_RGB` shader (see §"NV12 → RGB shader
sketch" below) hardcodes BT.601 **limited-range** coefficients on
the assumption that `bcm2835-codec` defaults its CAPTURE
quantization to limited-range. The code at
`renderer/src/ipc_main.rs:250-270` calls `set_capture_format` but
does **not** set `V4L2_CID_QUANTIZATION` explicitly. If a future
codec build flips the default or an operator overrides it,
blacks/whites will clip without a loud failure. Tracked as a P1
in `qa/v1-spec-delta-2026-05-14.md`; cheap fail-loud fix is ~20
LOC (post-`S_FMT` ctrl-get + assert limited-range).

The DMABUF path is not affected — Mesa reads the colorimetry hint
out of the EGLImage and inserts the right matrix.

## NV12 → RGB shader sketch (for piece 4)

For the future pieces' reference, the fragment shader to sample
NV12 (Y plane texture + UV plane texture) and emit RGB:

```glsl
precision mediump float;
uniform sampler2D u_tex_y;       // Y plane, 8-bit single-channel
uniform sampler2D u_tex_uv;      // UV plane, 8-bit two-channel
varying vec2 v_uv;
void main() {
    // BT.601 FULL-range matrix. Most software decoders emit
    // full-range; bcm2835-codec defaults to limited-range
    // (Y in [16/255, 235/255], UV in [16/255, 240/255]) UNLESS
    // V4L2_CID_QUANTIZATION is set to V4L2_QUANTIZATION_FULL_RANGE
    // on the CAPTURE queue. Piece 2 should set that ctrl after
    // configure so this shader works as-is; if not, the limited-
    // range version below applies instead.
    float y  = texture2D(u_tex_y,  v_uv).r;
    vec2  uv = texture2D(u_tex_uv, v_uv).rg - 0.5;
    float r = y + 1.402   * uv.y;
    float g = y - 0.344   * uv.x - 0.714 * uv.y;
    float b = y + 1.772   * uv.x;
    gl_FragColor = vec4(r, g, b, 1.0);
}
```

For BT.601 **limited-range** (the codec's default without an
explicit V4L2 quantization-range ctrl), pre-scale Y + UV before
the matrix:

```glsl
    float y  = (texture2D(u_tex_y,  v_uv).r - 16.0/255.0) * 255.0/219.0;
    vec2  uv = (texture2D(u_tex_uv, v_uv).rg - 128.0/255.0) * 255.0/224.0;
    // ... same matrix coefficients on full-range y/uv after scaling.
```

Without the scaling, blacks are crushed (Y=0.063 maps to 0 instead
of 0) and whites clamp early. Use BT.709 matrix if profiling shows
the codec emits BT.709 (Pi 4+ typically BT.709 for 1080p sources).
Matrix coefficients live in a uniform so we can flip without a
recompile; the quantization range is harder to runtime-toggle since
it changes the scaling math too -- piece 2 should pick one in the
V4L2 configure step + stick with it.
