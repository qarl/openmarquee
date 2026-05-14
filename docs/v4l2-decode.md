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

## Arc roadmap (pieces 2-5, future dispatches)

The dispatch sized the full arc at 10-20h of focused Rust work,
broken into 5 pieces. Piece 1 (this commit) is the bounded
investigation + SD-burn prep. Pieces 2-5 are open for follow-up
dispatches:

- **Piece 2: Rust V4L2 client crate.** Add a `v4l2_decode` module
  to `renderer/` with open/configure/queue/dequeue plumbing. Use
  either the `v4l` crate or raw `libc` ioctls -- decision depends
  on whether `v4l` exposes the multiplanar M2M variant cleanly.
  Unit tests against a baked H.264 fixture.
- **Piece 3: VideoSlide IPC wire.** `renderer/src/ipc_main.rs` --
  on `BeginSlide(VideoSlide)`, spawn a decoder loop that demuxes
  the MP4 (likely via the `mp4` crate or `bytes`-level parsing of
  the simple boxes), feeds NAL units to the V4L2 OUTPUT queue, and
  surfaces dequeued frames per advance. Stop emitting
  `"video slides TBD"` for `ContentItem::Video`.
- **Piece 4: DMA-BUF zero-copy handoff.** Export CAPTURE buffers
  as `dma_buf` fds, import as `EGLImage` via the
  `EGL_EXT_image_dma_buf_import` extension, sample directly in
  the existing slide shader. The diff between this and an
  MMAP+`glTexImage2D` copy is roughly 60% CPU at 1080p30 on
  Pi Zero 2 W -- gated for hitting the 30fps target.
- **Piece 5: Backend wiring.** Minimal: VideoSlide just stops
  raising `RustRendererUnsupportedSlideError` once piece 3 lands.
  Existing `_play_via_rust_ipc` advance loop handles it.

After piece 5, the rust-sidecar route is feature-complete for all
3 slide types and the production default flip is one qarl-eyeball
pass away.

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
