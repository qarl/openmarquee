# Phase 3g + 3f-redux BLOCKED — predict_text_dims agreement did NOT close the floor

Date: 2026-05-15
Status: REVERTED; no source changes landed.
Prior context: `qa/captures/parity-phase3f-blocked-2026-05-14.md` (0cd1ed6).

## TL;DR

Phase 3g (predict_text_dims WASM export + paintLayer use) + Phase 3f-redux
(Rust rasterize at target_height_px squished size) landed together as the
dispatch required. They made parity **WORSE on font_inter**, not better:

| metric                                    | pre-3g | 3g+3f (pad bug) | 3g+3f (pad fix) |
|-------------------------------------------|-------:|---------------:|----------------:|
| floor_diag mean_delta                     |  0.504 |          1.290 |           1.198 |
| floor_diag pixels with delta > 100        |  4,671 |         12,123 |          10,989 |
| floor_diag max abs per-stem drift         |   3 px |           5 px |            5 px |
| parity_font_inter mean_delta              |  0.504 |          1.290 |           1.198 |
| parity_tests PASS at max_delta ≤ 50       |    0/39 |           0/39 |            0/39 |

Hypothesis refuted: metric source agreement (Canvas2D + Rust both reading
fontdue's `metrics(ch, size_px)` for totalInkExtent) does not collapse the
229/231 max_delta floor. The floor persists AND the disagreement-pixel
count more than doubles on font_inter.

## What was tried

1. `renderer-wasm/src/lib.rs`: added `predict_text_dims(text, font_name,
   size_px) -> [width, ascent, descent]` using fontdue's `Font::metrics`.
2. `ui/src/wasm-renderer.js`: exported `predictTextDims` with a 1024-entry
   LRU cache; stub in `wasm-renderer.test-stub.js`.
3. `ui/src/rasterize.js::paintLayer`: ascent/descent source chain became
   predictTextDims → ctx.measureText → fontSizePx * 0.8/0.2 fallback.
4. `renderer/src/hdmi_logic.rs`: added
   `layout_text_to_alpha_at_target_height(font, text, size_px,
   target_height_px)`. Squished size_px down so the rasterized bitmap
   fits in target_height_px. Pre-existing `layout_text_to_alpha`
   became a `u32::MAX` shim.
5. `renderer/src/hdmi.rs`: 2 call sites pass `target_height_px = round(
   layer.box.h * mode_h)`.
6. Cross-built for Pi, deployed, re-blessed `renderer/tests/golden/*.png`
   to capture the new Rust output, ran `scripts/parity_tests.sh`.

After observing the regression I also added
`predict_alpha_bitmap_ink_extent` (matches Canvas2D's totalInkExtent —
excludes the 2-px bitmap pad that `predict_alpha_bitmap_dims` adds for
FS_GLYPH_OUTLINE dilation). The pad fix shaved 1,134 disagreement pixels
but kept the same 5-px max stem drift.

## Why the hypothesis failed (best guess, not verified)

Pre-3g, Rust rasterized at full `size_px` (1037 for font_inter) and the
GPU sampled that into the 216-px-tall box at LINEAR. The 5× downscale
**masked** per-glyph advance rounding because each destination pixel
integrated many source texels.

Post-3g+3f, both renderers rasterize at the same `effective_size_px ≈
203`. Now the source/dest ratio is close to 1.0, and per-glyph
advance rounding decisions become visible at the destination. Canvas2D
uses `drawImage` with bilinear; Rust uses GL_LINEAR sampling. Even
when both fontdue rasterizers produce **bit-identical glyph alpha
bitmaps** (which they should, given identical fontdue 0.9 + identical
effective_size_px), the per-glyph `round(advance_width)` cursor
positions appear to drift by 1 px between the two paths, compounding
to ±5 px across the 5 glyphs of "INTER".

The drift signature is symmetric around the centered text's center
(+5, +4, +2, 0, -2, -4, -5), which is exactly what you'd expect if
the cursor positions were correct but the SAMPLED bitmap edges differ
by sub-pixel amounts on a per-glyph basis between drawImage and
GL_LINEAR.

Followup hypothesis worth checking before any next slice: **drawImage
sub-pixel positioning vs GL_LINEAR sampling at near-1:1 ratios** is
the actual residual signal, NOT advance-width rounding or metric
source mismatch.

## What was reverted

- `renderer/src/hdmi_logic.rs`, `renderer/src/hdmi.rs`,
  `ui/src/rasterize.js` → back to HEAD.
- `renderer/tests/golden/*.png` → back to HEAD (was re-blessed during
  the experiment; `bash scripts/render_tests.sh` confirms 44/45 PASS
  at HEAD; the 1 failure is `fys_08_tile_chaos`, an unrelated motion-
  dependent flake that does not reproduce on the `animated_multi_chaos`
  variant of the same content).
- `renderer-wasm/src/lib.rs`, `ui/src/wasm-renderer.js`,
  `ui/src/wasm-renderer.test-stub.js` → back to HEAD. predict_text_dims
  was useful scaffolding but is dead code without the (refuted)
  caller, so it ships with the next attempt rather than as orphaned
  surface area.

`git diff HEAD` should report only this findings doc.

## Verification numbers at HEAD (post-revert)

- `cargo test --release`: 455/455 PASS.
- `scripts/render_tests.sh`: 44/45 PASS (the one FAIL is
  fys_08_tile_chaos, pre-existing).
- `scripts/parity_tests.sh`: 0/39 PASS at default threshold; mean
  metrics match the pre-3g baseline (font_inter mean_delta=0.504,
  pixels_over_10=0.40%).
- `floor_diag.py` on font_inter: mean=0.504, max=231, 4,671
  disagreement pixels, per-stem deltas [+1, +1, 0, 0, -1, -2, -3].

## Suggested next move

The metric-source-mismatch theory is dead. The disagreement signature
points at **sampling-stage differences at near-1:1 ratios** rather
than rasterization or metric agreement. Two cheap diagnostics
worth running before designing the next slice:

1. Capture Canvas2D `parity_font_inter.browser.png` with WASM
   rasterization OFF (force `isWasmReady()` to false). Compares pure
   `ctx.fillText` vs Rust-fontdue. If the per-stem drift LOOKS THE
   SAME, the gap is sampling-stage, not rasterizer.
2. Add a Rust-side debug capture that draws the alpha bitmap with
   **nearest** texture filtering instead of LINEAR (1-call change).
   If the floor drops, GL_LINEAR vs drawImage at near-1:1 is
   confirmed as the gap.
