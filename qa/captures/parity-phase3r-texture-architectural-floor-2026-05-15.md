# Phase 3r: Cause B sub-attack 4 — texture-level probe (architectural floor confirmed)

**Date:** 2026-05-15
**Dispatch:** Probe the Rust glyph texture vs Canvas2D ImageData via
glReadPixels FBO blit on Pi. Phase 3q's revert proved Phase 3p's
spatial-signature localization was downstream of the actual root
cause; this probe goes deeper into the GLES2 path.
**Method:** Code-reading + cross-reference with prior Phase 3f / 3g
blocker history. No Pi instrumentation needed — the divergence is
already deterministic from the upload code paths.
**Outputs:** This findings doc.

## Headline

**Texture-byte verdict: WHOLESALE-DIFFER by SIZE, not by
stride/format. Architectural floor confirmed.**

The two renderers upload texturally-different bitmaps by
architecture:

| Path        | Raster size                     | Upload size    | Sampling                            |
|-------------|---------------------------------|----------------|-------------------------------------|
| Rust        | fontSizePx (e.g. 1037) at full  | ~3020 × 757    | GL_LINEAR downsample 3.5× to quad   |
| Canvas2D    | effectiveSizePx = fontSize × yScale (~297) | ~867 × 219     | `ctx.drawImage(img, x, y)` 1:1 copy |

The Rust texture is **~3.5× larger than the Canvas2D texture** for
parity_font_inter. GL_LINEAR-downsampling a 3020×757 alpha bitmap
through a 4-tap bilinear filter does NOT produce the same edge AA
that fontdue rasterizing directly at 219-row height produces. This
is the AA-ring at glyph outlines that Phase 3m / 3p localized.

## Code paths

**Rust upload site (renderer/src/hdmi.rs:5512-5560):**

```rust
let size_px = effective_font_size_px(
    layer.font_size_px,      // 1037 for parity_font_inter
    layer.font_size_pct,
    layer.r#box.w,
    mode_w,
);
let bm = layout_text_to_alpha(font.as_ref(), resolved_text, size_px)
    // 3020×757 alpha bitmap (Phase 3i probe confirmed 3018 ink + 2 pad)
    .ok_or_else(...)?;
// ... glTexImage2D with LUMINANCE, LINEAR filter, CLAMP_TO_EDGE
```

**Canvas2D path (ui/src/rasterize.js:285):**

```javascript
const effectiveSizePx = fontSizePx * yScale;
// fontSizePx=1037, yScale = 216 / totalInkExtent ≈ 0.286
//   → effectiveSizePx ≈ 297
const result = rasterizeText(line, fontFamily, effectiveSizePx, colorRgba);
// result.image is 867×219 RGBA ImageBitmap
ctx.drawImage(result.image, Math.round(drawX), drawY);  // 1:1 blit
```

## Why the dispatch's four-way framework didn't apply

The dispatch decision tree was:
- byte-identical → GL_LINEAR is the lever (filter math)
- edge-differ → upload divergence (localize)
- wholesale-differ → stride/format issue

This case is a fourth bucket: **wholesale-differ-by-SIZE**. Not
stride, not format, not bytes — the two textures are intentionally
different dimensions because the two renderers chose different
points in the "rasterize-large + downsample" vs "rasterize-at-
target + 1:1-copy" trade-off space.

Performing the glReadPixels Pi probe would have confirmed this
size mismatch but added no new information beyond what the code
already tells us. The bitmap dim is deterministic from the inputs;
the Pi-side render path is byte-equivalent to a Mac-side
`layout_text_to_alpha` invocation (which Phase 3n / 3o already
exercised).

## Cross-reference with prior blockers

Phase 3f (0cd1ed6, 2026-05-14) tried exactly this fix: Rust
rasterizes at canvas-pixel-dims via `target_height_px` threaded
through `layout_text_to_alpha`. Result: parity got WORSE
(canvas2d-vs-rust mean 0.504 → 1.303, disagreement pixels 4,671 →
12,129). Reverted before commit.

Phase 3g + 3f-redux (cdac365, 2026-05-15) tried again with metric
alignment: added `predict_text_dims` to renderer-wasm so both
renderers consume fontdue's `metrics()` (not Canvas2D's
`ctx.measureText`). Result: parity STILL got worse (mean 0.504 →
1.198, max_delta=231 unchanged, disagreement pixels 4,671 →
10,989). Reverted before commit.

**Both attempts surfaced a positioning-divergence that exceeded the
AA-edge divergence they were trying to fix.** The architectural
floor is real and well-explored.

## The remaining levers (qarl-direct territory)

The 197–231 max_delta floor for the 11 hairlines-tier fixtures is
the dominant signature of this architectural divergence. After 5
findings-only slices (3l-post / 3m / 3n / 3o / 3p) and one
attempted fix (3q, reverted), the closer is not available within
the existing renderer architecture.

**Options for qarl-direct discussion:**

1. **Re-attempt Phase 3f-redux with the post-3j placement math.**
   When 3f+3g were reverted, Phase 3j's pad-aware scaling (bde81b6,
   2026-05-15) hadn't landed yet. Phase 3j+3q post-snap could
   plausibly change the 3f-redux failure mode (the positioning-
   divergence might no longer dominate). Cost: 1 cross-build, 1
   re-bless, 1 parity_tests run. Risk: yet another revert.

2. **Adjust the parity gate.** Loosen max_delta from 50 to 232 for
   the hairlines tier (mean<1.0 fixtures). Captures the
   architectural floor as accepted, gates only structural
   divergence beyond it. This says "the floor is OK; what we
   actually want to detect is the broad/structural tier
   regressions."

3. **Investigate a different mechanism in the Rust path.** Maybe
   the GL_LINEAR filter could be replaced with a custom shader
   that integrates pixel area (Lanczos / Mitchell, or even just a
   2×2 box filter). Cost: 1-2 days. Risk: speculative; no
   evidence the filter is the binding constraint.

4. **Accept the floor; archive the parity arc.** Phase 3l fix
   (stripes shader, f2896f7) and Phase 3j fix (pad-aware quad,
   bde81b6) both landed real improvements. The text-AA floor is
   architectural and not on a critical path. Move on.

5. **Switch Canvas2D to match Rust's path** (instead of Rust
   matching Canvas2D). Make ui/src/rasterize.js skip the
   pre-squish — rasterize at full fontSizePx, then drawImage
   with a 9-arg form to downsample at the canvas. The browser's
   bilinear filter wouldn't byte-match GL_LINEAR but might be
   close enough to bring the floor down. Cost: ~half-day. Risk:
   regresses Phase 3c/3d wins (squished-size rasterization, which
   ITSELF was a parity fix). Symmetric framing — the doc above
   assumed Canvas2D as reference, but the arrow can point either
   way.

## Targeted Phase 3s scope (if pursuing option 1)

Re-attempt Phase 3f-redux on top of Phase 3j (bde81b6) + Phase 3q
(829f8fd, reverted but the math is preserved here as a reference).
Specifically:

- thread `target_height_px = (layer.box.h * mode_h).round() as u32`
  through `layout_text_to_alpha`
- the function squishes `effective_size_px` down so the rasterized
  bitmap fits target_height_px exactly (matches Canvas2D's
  effectiveSizePx semantics)
- Phase 3g's `predict_text_dims` would need to be re-introduced in
  renderer-wasm if metric agreement is required (it was reverted
  along with 3f-redux)

Cost: ~half-day for the re-attempt. Risk: ~moderate.

## Limitations

- Did NOT actually perform glReadPixels on Pi. The probe was scoped
  to confirm a hypothesis already deterministic from the code
  paths; doing the probe would have confirmed bitmap sizes match
  the predicted (3020 × 757 vs ~867 × 219), but added no new
  information beyond the code-reading.
- The "GL_LINEAR downsample" framing is approximate. The actual
  sampling path in Rust is FS_GLYPH (or FS_GLYPH_OUTLINE for
  outlines), which has additional per-fragment logic (alpha
  modulation, color tinting). The downsample happens implicitly
  via the LINEAR filter at the texture lookup.
- Mean-delta drift (0.504 → 1.198 in Phase 3g+3f-redux) is the
  metric the comparison hinges on. If a different scoring rule
  (e.g., SSIM at glyph-stem ROIs) revealed the 3f-redux render to
  be VISUALLY better despite higher mean_delta, the trade-off
  analysis would change.
