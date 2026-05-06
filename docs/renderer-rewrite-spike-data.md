# Renderer Rewrite — Hardware Spike Data

**Captured:** 2026-05-06 on `openMarqueeDev` (Pi Zero 2 W, vc4 V3D 2.1, Mesa 25.0.7-2+rpt4, kernel 6.12, 416 MB RAM).
**Spike script:** [`scripts/phase8_renderer_spike.py`](../scripts/phase8_renderer_spike.py)
**Raw JSON:** preserved at the bottom of this document.
**Purpose:** answer the BLOCKERs and IMPORTANT-needs-verification items the reviewer flagged in [`renderer-rewrite-plan.md`](renderer-rewrite-plan.md).

The spike ran *while the live backend was running* (default config, shader off), so CMA was heavily contested. That makes the bandwidth numbers slightly pessimistic vs a quiescent system.

---

## Headline findings vs reviewer's BLOCKERs

| Reviewer claim | Measured reality | Verdict |
|---|---|---|
| `MAX_TEXTURE_IMAGE_UNITS = 8` on vc4 V3D 2.1 | **= 16** | **BLOCKER WRONG.** 14-sampler shader fits with headroom. |
| Sampler array dynamic indexing won't compile | Compiles and links; emits warning "sampler arrays indexed with non-constant expressions is forbidden in GLSL ES 100" but driver accepts it (Mesa unrolls at compile time — fast: 28-45 ms). | **BLOCKER WRONG** in practice, but worth pinning down whether unroll holds for all loop shapes. |
| Bandwidth math: 14 samples × 1080p × 30 fps doesn't close | **Confirmed.** 14 samples/fragment at 1080p = 39 ms median = ~25 fps. Misses 30 fps by 16%. **8 samples = 23 ms = ~43 fps; well above 30.** | **BLOCKER CORRECT in spirit.** Unified shader with 14 samplers is too hot for the budget. ≤8 samples/fragment is the actual ceiling at 1080p × 30 fps. |
| GBM scanout chain at 2 buffers too thin | Not measurable in offscreen FBO spike — needs an atomic-commit prototype with HDMI bring-up to confirm. | **NOT YET MEASURED.** Must be tested in Step 0/-1 of the build. |

The unified-shader-with-14-samplers gambit doesn't fit, but for *different reasons* than the reviewer thought. Sampler unit count is fine; bandwidth at that sample count is not.

## All eight measurements

### 1. GL limits

```
MAX_TEXTURE_IMAGE_UNITS              = 16
MAX_VERTEX_TEXTURE_IMAGE_UNITS       = 16
MAX_COMBINED_TEXTURE_IMAGE_UNITS     = 32
MAX_FRAGMENT_UNIFORM_VECTORS         = 4095
MAX_VERTEX_UNIFORM_VECTORS           = 4088
MAX_VARYING_VECTORS                  = 8     ← modest; design around this
MAX_VERTEX_ATTRIBS                   = 8
MAX_TEXTURE_SIZE                     = 2048  ← 1920 fits, 2048 hard ceiling
MAX_RENDERBUFFER_SIZE                = 2048  ← same; FBO ≤ 2048 in any axis
```

Most generous limits we'd care about — fragment uniform vectors at 4095 means we can pile on per-layer uniforms without worry. The 2048 texture/renderbuffer ceiling is uncomfortable for 1080p (we have 6.7% headroom on the long axis). Strict 1920×1080 fits; portrait or rotated layouts fit; an oversampled 2× buffer does not.

### 2. Sampler array indexing

| Indexing form | Linked? | Compile+link time | Notes |
|---|---|---|---|
| `u_layers[CONST]` (constant index) | yes | 2928 ms (cold start) | First shader of session; subsequent constant-array shaders are fast |
| `u_layers[u_idx]` (uniform-int index) | yes | 28.7 ms | Driver emits "forbidden in GLSL ES 100" warning but accepts |
| `u_layers[i]` inside `for` loop | yes | 45.3 ms | Driver appears to unroll at compile time |

**Implication:** dynamic indexing of sampler arrays is *practically usable* on vc4 + Mesa 25 even though GLSL ES 100 spec forbids it. Compile times suggest the driver unrolls; the loop index variant linked without warnings, suggesting Mesa handled the small fixed-trip-count loop. We **should not depend on this** as a portable behavior, but it's available as an optimization affordance — and the more important point is that we don't need to declare the samplers as a single array; declaring them as `u_t0`, `u_t1`, ..., `u_tN` separately works fine and keeps the shader portable.

### 3. Sampler unit count linkage

| n samplers | Linked? | Time |
|---|---|---|
| 4  | yes | 17 ms |
| 8  | yes | 14 ms |
| 12 | yes | 16 ms |
| 14 | yes | 17 ms |
| 16 | yes | 19 ms |

All link cleanly up to the 16-unit limit. The plan's 14-sampler design (2 bg + 12 layer) is sampler-budget-feasible.

### 4. Shader compile times

| Shader | Compile + link time |
|---|---|
| Trivial (varying-only) | 11 ms |
| 4 const-index samplers | 11 ms |
| Unified 8-sampler / 4-blend / motion+transition | **66 ms** |

First-shader-of-session cold start is **2.9 seconds** (driver cache warm-up). Subsequent shaders are tens of milliseconds.

**Cold-start budget impact:** §8.4 of the spec targets ≤4s from process start to first frame. The first shader compile alone consumes ~3s of that. To keep cold start in budget we need to either pre-warm with a single trivial shader, OR ship the full set as a `glProgramBinary` cache (the `GL_OES_get_program_binary` extension is present in the extension list and supports this).

### 5. Bandwidth at 1080p

Time to render one full-screen quad at 1920×1080 with N texture samples per fragment, including a `glFinish()` to drain the pipeline:

| n samples/fragment | Median ms | p99 ms | Effective fps | Margin vs 33 ms (30 fps) |
|---|---|---|---|---|
| 1  | 9.0  | 10.1 | 110 | +73% |
| 2  | 13.8 | 14.7 | 72  | +58% |
| 4  | 15.1 | 15.5 | 66  | +55% |
| 6  | 18.9 | 19.0 | 53  | +43% |
| 8  | 22.8 | 24.4 | 44  | +31% |
| 14 | **39.2** | 41.5 | **25** | **−16%** |

**This is the central finding.** At ≤8 samples/fragment we're comfortably above 30 fps. At 14 samples we're at 25 fps and have missed the budget. Each additional sample costs ~1.5-2 ms.

Translates to a real-renderer budget:
- "Slide A bg + 4 layer textures" = 5 samples = 16 ms = 60 fps. Steady-state slide is fine.
- "Slide A (bg + 4 layers) + slide B (bg + 4 layers) + transition" = 10 samples = ~28 ms = 36 fps. Tight but feasible.
- "Slide A (bg + 6 layers) + slide B (bg + 6 layers)" = 14 samples = 39 ms = 25 fps. **Misses target.**

So the unified-shader design works if we cap effective per-side layer count at ~4. With more layers per side, the split-shader (motion baked to a flat per-side texture, transition consumes 2 inputs + bg = ~4 samples) is the correct architecture. The plan's §7 "scope cut #1" should indeed be the baseline at 1080p.

The numbers above are with the live backend running; quiescent system would be ~10-15% better.

### 6. EGL extensions for dmabuf import

```
EGL_EXT_image_dma_buf_import:           PRESENT
EGL_EXT_image_dma_buf_import_modifiers: PRESENT
EGL_KHR_image_base:                     PRESENT
EGL_MESA_image_dma_buf_export:          PRESENT (bonus — we can also export)
```

The plan's H.264 dmabuf-import path is supported by the EGL implementation. **Untested in this spike:** whether the kernel-side `bcm2835-codec` v4l2_m2m decoder produces dmabufs in a format the EGL importer accepts (DRM_FORMAT_YUV420 vs multi-plane YUV420 vs NV12 — the modifier negotiation is where this often falls apart). Must be confirmed by an actual decode-and-import smoke test in Step -1 of the build, not at integration time.

### 7. glReadPixels stall at 1080p RGBA

```
median: 265 ms  (range 248–298 ms)
```

**Reviewer was correct.** A blocking `glReadPixels` of a full 1080p RGBA frame on vc4 takes ~265 ms — at 30 fps target that's 8 dropped frames per snapshot. The plan's 60-second-gating mitigates frequency but not the fact that *each* snapshot drops frames.

Mitigations:
- Use `glReadPixels` into a `GL_PIXEL_PACK_BUFFER` (PBO async readback). `GL_NV_pixel_buffer_object` is in the extension list. Issue the readback then continue rendering; pick up the data later.
- Schedule snapshots between slides, when a brief stall is invisible.
- Render snapshots at lower resolution (720p or 360p) — bandwidth-bound, ~4× faster.

### 8. DRM / display state

- HDMI-A-1: **connected**.
- modetest reports 5 modes: 1024×768, 800×600 (×2), 848×480, 640×480.
  - **No 1080p mode in the list.** Consistent with the project memory note "HDMI EDID stuck at 0 bytes (needs cable replug or reboot)" — without EDID, the kernel falls back to safe-mode list. Any 1080p design requires forcing the mode via `video=HDMI-A-1:1920x1080@30` on the kernel cmdline or a DRM atomic-commit override.
- Connector properties: DPMS, link-status, max bpc, Colorspace (full enum incl. BT2020/sRGB/etc.), margins, Broadcast RGB. **No rotation property on the connector.**
- The `rotation` property lives on the *plane*, not the connector — the spike's `modetest -c` only enumerated connectors. Plane rotation availability needs `modetest -p` follow-up. Filed as a tactical follow-up before Step 2 of the build.

### Memory snapshot during the spike

```
MemTotal:     426 MB    (Pi Zero 2 W physical)
MemAvailable: 103 MB    (live backend running, default config)
CmaTotal:     256 MB    (kernel CMA reservation)
CmaFree:      69  MB    (i.e. ~187 MB CMA in use by live backend)
Slab:         42  MB    (kernel data structures)
```

Confirms the §4.1 plan's "kernel CMA is the binding wall." With the live backend's default config holding ~187 MB of CMA, a renderer that opens its own GBM/EGL session needs to either replace that backend's allocation or fit in the remaining 69 MB. The rewrite is the same process so this is naturally consolidated.

## Implications for the rewrite plan

Net of the spike:

1. **Sampler unit count is not a blocker.** Reviewer was wrong; we have 16. Drop that BLOCKER entirely.
2. **Dynamic sampler array indexing works in practice.** Use it as an optimization affordance, but design the shader to still work with separately-declared samplers for portability.
3. **The unified single-pass shader with 14 samplers does not hit 30 fps at 1080p.** The bandwidth ceiling is real. The split-shader architecture (≤8 samples/fragment in the transition pass) is the correct baseline at 1080p × 30 fps.
4. **dmabuf import is feasible** at the EGL level but needs a smoke-test to confirm bcm2835-codec produces compatible dmabufs.
5. **`glReadPixels` snapshots will drop frames** unless we use PBO async readback or schedule them between slides. Plan must commit to one of these.
6. **Cold-start budget**: 3-second first-shader cost dominates. Pre-warm with a trivial shader OR use program-binary caching (`GL_OES_get_program_binary` is available).
7. **HDMI mode**: 1080p must be force-modeset (no EDID).
8. **Plane rotation**: not yet measured; quick follow-up needed.

## Raw JSON

```json
{
  "gl_info": {
    "vendor": "Broadcom",
    "renderer": "VC4 V3D 2.1",
    "version": "OpenGL ES 2.0 Mesa 25.0.7-2+rpt4",
    "glsl": "OpenGL ES GLSL ES 1.0.16"
  },
  "gl_limits": {
    "MAX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 32,
    "MAX_FRAGMENT_UNIFORM_VECTORS": 4095,
    "MAX_VERTEX_UNIFORM_VECTORS": 4088,
    "MAX_VARYING_VECTORS": 8,
    "MAX_VERTEX_ATTRIBS": 8,
    "MAX_TEXTURE_SIZE": 2048,
    "MAX_RENDERBUFFER_SIZE": 2048
  },
  "egl_extensions_relevant": {
    "EGL_EXT_image_dma_buf_import": true,
    "EGL_EXT_image_dma_buf_import_modifiers": true,
    "EGL_KHR_image_base": true,
    "EGL_MESA_image_dma_buf_export": true
  },
  "sampler_array_indexing": {
    "constant_index":  { "linked": true, "ms": 2927.96 },
    "uniform_index":   { "linked": true, "ms": 28.70, "warning": "sampler arrays indexed with non-constant expressions is forbidden in GLSL ES 100" },
    "loop_index":      { "linked": true, "ms": 45.28 }
  },
  "sampler_unit_count_link_test": {
    "n4":  "linked", "n8":  "linked", "n12": "linked",
    "n14": "linked", "n16": "linked"
  },
  "shader_compile_times_ms": {
    "trivial": 10.5,
    "4_const_samplers": 10.5,
    "unified_8sampler_4blend": 65.75,
    "first_shader_cold_start": 2927.96
  },
  "bandwidth_1080p_ms_per_frame": {
    "n1":  { "median": 9.0,  "p99": 10.1 },
    "n2":  { "median": 13.8, "p99": 14.7 },
    "n4":  { "median": 15.1, "p99": 15.5 },
    "n6":  { "median": 18.9, "p99": 19.0 },
    "n8":  { "median": 22.8, "p99": 24.4 },
    "n14": { "median": 39.2, "p99": 41.5 }
  },
  "glReadPixels_1080p_rgba_ms": { "median": 264.7, "min": 248.3, "max": 298.0 },
  "drm": {
    "hdmi_connected": true,
    "modes_listed": ["1024x768@60", "800x600@60", "800x600@56", "848x480@60", "640x480@59.94"],
    "rotation_on_connector": "not present (rotation is a plane property)"
  },
  "meminfo_during_spike": {
    "MemTotal_kB": 426076,
    "MemAvailable_kB": 103320,
    "CmaTotal_kB": 262144,
    "CmaFree_kB": 69540,
    "Slab_kB": 42664
  }
}
```
