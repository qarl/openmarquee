# Phase 3f BLOCKED — metric-source dependency surfaced — 2026-05-14

## Summary

Implemented the dispatch's Phase 3f source change (Rust rasterizes at
`target_height_px` via new `layout_text_to_alpha_at_target_height` wrapping
the existing function). Cross-built, deployed to Pi, captured `font_inter`
fresh. Result **made parity WORSE**, not better.

**REVERTED before commit.** Source changes not landed; this doc + the
Phase 3e diagnostic (`33ded3a`) are the only artifacts of this slice.

## What the data showed

Canvas2D `font_inter` capture vs three Rust renders:

| Rust render | RGB max | mean | pixels>100 |
|---|---|---|---|
| Pre-Phase-3f (checked-in golden) | 231 | 0.504 | 4,671 |
| Phase 3f v1 (cap-clamp then squish) | 231 | 1.315 | 11,942 |
| Phase 3f v2 (squish then cap-clamp) | 231 | 1.303 | 12,129 |

Phase 3f WORSENED Canvas2D-vs-Rust agreement by ~2.5× on font_inter.

## Why

Phase 3f assumed both renderers would arrive at the SAME
`effective_size_px` for the same fixture. They don't:

- **Canvas2D Phase 3c** computes `effectiveSizePx = fontSizePx * yScale`
  where `yScale = boxH / totalInkExtent` and `totalInkExtent` comes from
  `ctx.measureText().actualBoundingBoxAscent + Descent` (Canvas2D's
  browser-measured font metrics).
- **Rust Phase 3f (proposed)** computes the same shape but with
  `totalInkExtent` from `predict_alpha_bitmap_dims` which uses
  `fontdue::Font::metrics()` (fontdue's native metrics).

`ctx.measureText` and `fontdue::Font::metrics` produce **different** ascent
+ descent values for the same font, same size. For `Inter` at size_px=1037:

- JS measureText reports total ≈ 1050 → effective_size_px ≈ 213
- Rust fontdue.metrics reports total ≈ 1140 → effective_size_px ≈ 196

A ~17 px difference in effective_size_px → different bitmap widths
(`Inter` glyph advance scales linearly with size) → different per-glyph
positioning when both are rendered onto the canvas. The 12,129
disagreement pixels are the consequence.

## Phase 3g prerequisite

Before Phase 3f can land, both renderers must use the SAME metric source
to compute `totalInkExtent` / `effective_size_px`. The clean architectural
fix:

1. Add `predict_text_dims(text, font, size_px) -> (width, ascent, descent)`
   to renderer-wasm (delegates to fontdue::Font::metrics).
2. Canvas2D `paintLayer` uses `predict_text_dims` instead of
   `ctx.measureText` to compute `maxAscent` / `maxDescent` / `totalInkExtent`.
3. Both Canvas2D AND Rust now derive `effective_size_px` from fontdue's
   own metrics → they agree.
4. THEN apply the Phase 3f Rust-side rasterize-at-target_height_px change.
5. Re-bless goldens. Re-run parity_tests. Verify gate trips.

## Bonus finding: font_inter golden is stale

While debugging Phase 3f, I rebuilt the Rust renderer from HEAD (no Phase
3f) and re-captured font_inter. The fresh capture differs from the
checked-in `renderer/tests/golden/font_inter.png` by max=231, mean=0.829,
14,314 pixels. text_static (per pipeline_diag.py, `944c525`) showed
fresh-vs-golden = 0; so the staleness is fixture-specific, not universal.

Cause likely: subtle build-environment / cross-compile variation. Worth a
golden refresh pass as part of the re-bless arc anyway.

## Code state

- Local: reverted (`git checkout -- renderer/src/hdmi.rs renderer/src/hdmi_logic.rs`)
- Pi binary: rebuilt + redeployed (same HEAD source; the v2 Phase 3f
  binary is gone)
- Tests: 455/455 cargo + 518 vitest both unaffected (no source change
  landed)

## Recommendation

Dispatch Phase 3g first: align metric source via WASM `predict_text_dims`.
Then re-attempt Phase 3f. The two together are the actual fix.
