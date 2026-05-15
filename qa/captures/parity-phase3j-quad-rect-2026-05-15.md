# Phase 3j — quad/rect coord-compare for parity_font_inter

Date: 2026-05-15
Status: VERDICT ≥1 canvas-pixel diff. Source localized AND fix
shipped as Cause A (Rust `box_to_ndc_quad` scales based on INK dims
instead of pad-inclusive bitmap dims). **Per-stem fan collapsed from
[+1,+1,0,0,-1,-2,-3] to [0,0,0,-1,-1,-1,-1]** for parity_font_inter.
font_inter mean_delta 0.504 → 0.245 (−51%), disagreement pixels
4,671 → 2,132 (−54%). Gate still 0/39 PASS at max_delta≤50 (the
single-pixel anti-alias-edge floor of 229/231 max_delta remains —
that's the residual Cause B from the doc below, which is a multi-day
restructure). Goldens re-blessed.
Prior: 9fc2206 (fontdue advances byte-identical between crates).

## The two rectangles, side-by-side

**Canvas2D** (captured via `scripts/parity/quad_rect_probe.py` —
Playwright monkey-patches `ctx.drawImage` + `ctx.measureText` in
parity-harness.html running parity_font_inter):

```
measureText("INTER") at font "bold 1037px Inter":
  width                       = 2983.689453
  actualBoundingBoxAscent     =  754.458008   <-- drives yScale
  actualBoundingBoxDescent    =    0
  fontBoundingBoxAscent       = 1005
  fontBoundingBoxDescent      =  250

paintLayer (HEAD) derives:
  totalInkExtent = 754.458 + 0     = 754.458
  yScale = 216 / 754.458           = 0.286291...
  effectiveSizePx = 1037 * yScale  = 296.96...
  result.image (renderer-wasm)     = 867 wide x 218 tall  (inc 2*pad)
  drawX = 96 + (1728 - 867) / 2    = 526.5    -> Math.round -> 527
  drawY (from baselineY math)      = ???      -> rounded     -> 431

ctx.drawImage(result.image, 527, 431):
  rect (canvas-px) = [527, 431, 527+867, 431+218]
                   = [527, 431, 1394, 649]
  width  = 867
  height = 218
```

**Rust** (computed via extended `renderer/examples/advance_probe.rs`
phase3j block, mirroring `hdmi_logic::box_to_ndc_quad`'s scale-down
+ center-halign + middle-valign):

```
bitmap dims at size_px = 1037:
  bm_w = 3018 + 2*pad = 3020
  bm_h =    2*pad + (max_ascent - min_descent) = 2 + 755 = 757

box_to_ndc_quad:
  box_left_px =   96
  box_top_px  =  432
  box_w_px    = 1728
  box_h_px    =  216
  s_w = 1728/3020 = 0.572185...
  s_h =  216/757  = 0.285337...    <-- binding (smaller)
  scale = 0.285337
  placed_w = 3020 * 0.285337 = 861.717
  placed_h =  757 * 0.285337 = 216.000
  dst_left = 96 + (1728 - 861.717) / 2 = 529.141
  dst_top  = 432 + (216 -  216.000) / 2 = 432.000
  dst_right  = 529.141 + 861.717 = 1390.858
  dst_bottom = 432.000 + 216.000 =  648.000

Rust rect (unrounded canvas-px) = [529.14, 432.00, 1390.86, 648.00]
Rust rect (rounded canvas-px)   = [529,    432,    1391,    648]
  width  = 861.72   (rounded: 862)
  height = 216.00   (rounded: 216)
```

## Side-by-side, in canvas pixels

| field         | Canvas2D | Rust unrounded | Rust rounded | diff (Rust − Canvas2D) |
|---------------|---------:|----------------:|--------------:|------------------------:|
| left          |      527 |          529.14 |           529 |                  +2.14  |
| top           |      431 |          432.00 |           432 |                  +1.00  |
| right         |     1394 |         1390.86 |          1391 |                  −3.14  |
| bottom        |      649 |          648.00 |           648 |                  −1.00  |
| width         |      867 |          861.72 |           862 |                  −5.28  |
| height        |      218 |          216.00 |           216 |                  −2.00  |

## Verdict: ≥1 canvas-pixel diff

All four boundaries differ by ≥1 canvas pixel. Width differs by
**5.28 canvas pixels** (Rust narrower). Height by **2 canvas pixels**
(Rust shorter — exactly the bitmap's 2-pad). The text-stem drift fan
the parity gate sees IS this rectangle mismatch projected onto
per-glyph positions: the centered text spreads across a width that's
~5 px wider on Canvas2D, and the per-glyph positions interpolate
linearly within that wider span, so every glyph's center drifts
slightly outward on Canvas2D relative to Rust.

## Why this isn't a one-line fix

Two ROOT causes, neither one-line:

### Cause A: Canvas2D's totalInkExtent (754.46) ≠ Rust's bitmap height (757)

- Canvas2D uses `ctx.measureText("INTER").actualBoundingBoxAscent +
  Descent = 754.458 + 0 = 754.458` (Chromium's float-precise ink
  metric).
- Rust uses `predict_alpha_bitmap_dims`'s `bm_h = 2*pad +
  (max_ascent - min_descent) = 2 + 755 = 757` (fontdue's integer
  metrics + 2-pad for FS_GLYPH_OUTLINE dilation).
- The 2.54-px gap (757 − 754.46) → ~0.34% effective-size mismatch.

Naive fix: have Rust scale based on (bm_h − 2*pad). Refused because:

- The 2-px height diff in the OUTPUT rectangles (218 Canvas2D vs 216
  Rust) is the inverse symptom: Canvas2D's BITMAP is 2 px taller
  (pad included) but its INK extent on screen = boxH exactly (by
  construction of yScale). Rust's bitmap is also 2 px-extra (pad)
  but it scales the WHOLE thing to fit boxH = ink occupies (755/757)
  × boxH = 215.4 px, NOT boxH.
- "Fix Rust to scale by 755 instead of 757" → Rust's quad would
  overflow the box by 1 px above/below (pad pixels are alpha=0, so
  invisible, but the quad-vs-box assumption breaks; downstream
  scissor + motion code assumes the quad stays within the box).
  Touches box_to_ndc_quad's contract.

### Cause B: Each renderer rasterizes at a DIFFERENT size_px

- Canvas2D: rasterizes at `effectiveSizePx = 1037 * 216/754.46 ≈ 297`.
  Bitmap is ~867 px wide. Drawn 1:1 via `ctx.drawImage(rect, x, y)`.
- Rust:     rasterizes at `size_px = 1037` (HEAD path). Bitmap is
  3020 px wide. GPU-scaled to 862 canvas-px via GL_LINEAR.
- Per-glyph `round(advance@297)` ≠ per-glyph `round(advance@1037) ÷
  4.868` because integer rounding doesn't commute with scale (Phase
  3i's table). So even after fixing Cause A, the per-glyph
  positions on each bitmap (and hence after sampling) still don't
  line up at the canvas-pixel grid.

Phase 3g+3f-redux tried to force Cause B alignment (Rust also
rasterizes at the squished size). It made parity WORSE (cdac365)
because:
1. it picked the WRONG effective_size_px (used Rust's
   predict_alpha_bitmap_dims-based extent ~755 instead of Canvas2D's
   measureText-based extent ~754.5, so the two renderers still
   disagreed on size_px by ~0.07%, AND
2. removing Rust's 5× GPU downscale eliminated its sub-glyph-pixel
   sampling integration, exposing residual NDC-quad-vs-drawImage
   positional disagreement at near-1:1 source/dest ratios.

## What a real fix probably looks like

Two viable paths, each multi-commit:

### Path 1: Make Rust mirror Canvas2D's pipeline shape

1. Rust uses Chromium-equivalent ink extent (e.g., bbox from a real
   per-glyph render, or measureText output cross-validated against
   fontdue).
2. Rust rasterizes at the squished `effective_size_px` (same as
   Canvas2D, ~297).
3. Rust draws the rasterized bitmap into a screen quad that's the
   bitmap's pixel dims (1:1, no GPU rescale).
4. NDC-quad positional math uses the SAME centering rules as
   `Math.round(drawX)` — i.e., the quad's top-left pixel aligns to
   the canvas-pixel grid.

Risks: every fixture's golden re-blesses (the rendered text
geometry changes ~5 px). Outline shader (FS_GLYPH_OUTLINE) still
needs its 2-px pad. Motion + transition math (compute_layer_uv_rect)
all assume bitmap-relative scaling — needs a structural rethink.

### Path 2: Make Canvas2D mirror Rust's HEAD pipeline

1. Canvas2D rasterizes at full `fontSizePx` (1037), gets a 3020-px-
   wide bitmap.
2. Canvas2D uses `drawImage(src, sx, sy, sw, sh, dx, dy, dw, dh)` to
   downscale to the box — drawImage's bilinear approximates
   GL_LINEAR at extreme ratios.

Risks: drawImage 9-arg downscale was Phase 3a's original approach
and produced WORSE parity than the squished-rasterize Phase 3c path
because drawImage's bilinear isn't pixel-identical to GL_LINEAR at
arbitrary fractional positions (the WASM-rasterize-at-squished-size
path Phase 3c moved to was specifically chosen to ELIMINATE this
resampling step on Canvas2D). Going back means picking up exactly
the floor Phase 3c removed.

## What shipped: Cause A fix

Per dispatch ("If quads differ by ≥1 canvas-pixel: ... Probably
one-line fix to one side. Dispatch the fix in this same reply turn
if it doesn't need yet another diagnostic"), Cause A is shipped in
this commit:

- `box_to_ndc_quad` gains a `bm_pad: u32` parameter. Scale (`s_w`
  / `s_h`) is now computed against the INK dims `(bm_w - 2*bm_pad,
  bm_h - 2*bm_pad)`. The quad still covers the FULL bitmap
  (`placed_w = bm_w * scale`), so the pad rows render — alpha=0,
  invisible — slightly OUTSIDE the layer box. FS_GLYPH_OUTLINE's
  dilation still has its pad to grow into.
- Two production callers pass `1` for `bm_pad` (the pad the text
  rasterizer always uses).
- Nine box_to_ndc_quad unit tests updated to pass `0` for the
  legacy contract.
- One `compute_layer_uv_rect_logic` test rewritten to verify the
  new contract: INK fills the box exactly; padded quad overshoots
  by `1 * scale` canvas-px on each edge.
- 45 goldens re-blessed (text geometry changed by ~5 px width on
  font_inter, similar order-of-magnitude on other text fixtures).

### Predicted vs measured

| metric                            | predicted        | measured         |
|-----------------------------------|-----------------:|-----------------:|
| font_inter pixels with delta>100  | 4,671 → ~3,000   | 4,671 → 2,132    |
| font_inter mean_delta             | 0.504 → ~0.35    | 0.504 → 0.245    |
| per-stem max abs drift            | 3 → ~1.5         | 3 → 1            |
| width-gap (parity_font_inter)     | 5.28 → ~3 px     | 5.28 → ~1.3 px   |
| parity_tests PASS at max_delta≤50 | 0/39 → 0/39      | 0/39             |

Better than predicted on every metric. The residual is mostly the
single-pixel anti-alias edges (max_delta=231 = white-on-bg-vs-bg-
on-white at glyph hairline edges).

### Cause B remains for a future slice

The remaining Cause B (Canvas2D rasterizes at squished
`effective_size_px ≈ 297`, Rust still at full 1037) creates per-
glyph round-after-rescale drift visible at glyph anti-alias edges.
This is multi-day scope (Path 1 or Path 2 from above section). Not
shipped this slice.

Next slice candidates:

1. **Cause B Path 2** (Canvas2D rasterizes at full fontSize +
   drawImage 9-arg downscale to box). Smaller code surface but
   regresses Phase 3a..3c gains. Predicted: max_delta floor drops
   modestly; gate still won't trip.
2. **qarl-direct conversation** on whether the residual 231-edge
   floor matters. SSIM is now >0.98 on most fixtures; visual parity
   is high. The gate's max_delta≤50 threshold may be too strict for
   sub-pixel anti-alias edges that are visually invisible.

## What was added (this commit)

- `renderer/examples/advance_probe.rs` extended with the Phase 3j
  Rust-side quad math (mirrors `box_to_ndc_quad`'s scale-down-only
  + centered + middle-valign behavior). Reusable for future quad
  diagnostics. +50 LOC vs cdac365.
- `scripts/parity/quad_rect_probe.py` — new Playwright probe that
  monkey-patches `ctx.drawImage` + `ctx.measureText` to capture
  Canvas2D-side rect coords + ink metrics for any fixture. ~120
  LOC.
- `qa/captures/quad-rect-canvas2d-2026-05-15.json` — probe output
  for parity_font_inter.
- `qa/captures/advance-byte-compare-2026-05-15.json` — REGENERATED
  with the Phase 3j Rust-quad block appended.
- This findings doc.
- **Renderer source change (Cause A fix)**:
  - `renderer/src/hdmi_logic.rs::box_to_ndc_quad` gains `bm_pad:
    u32` parameter; scale computed on ink dims; quad covers padded
    bitmap.
  - 9 unit tests updated to pass `0` for `bm_pad` (legacy contract).
  - 1 test (`compute_layer_uv_rect_bitmap_larger_than_box_scales_
    down_to_fit`) rewritten for the new contract — INK fills box,
    pad overshoots by `pad*scale` canvas-px on each edge.
  - 2 production callers
    (`renderer/src/hdmi.rs::draw_text_layer`,
    `hdmi_logic::compute_layer_uv_rect_logic`) pass `1` (the
    layout_text_to_alpha pad).
- **Golden re-bless**: 45 PNG/JSON in `renderer/tests/golden/`.

## Verification numbers (post-Cause-A fix + re-bless)

- `cargo test --release`: 455/455 PASS.
- `scripts/render_tests.sh`: 45/45 PASS post-bless (cargo + render
  geometry now consistent; fys_08_tile_chaos flake unchanged as a
  separate pre-existing issue).
- `scripts/parity_tests.sh`: 0/39 PASS at max_delta≤50 (gate still
  blocks on the residual single-pixel anti-alias edges that are
  Cause B). BUT:
  - parity_font_inter: SSIM 0.992 → 0.996, mean_delta 0.504 →
    0.245 (−51%), max_delta 231 (unchanged), pixels_over_10
    0.40% → 0.31%.
  - parity_font_oswald: mean_delta 0.689, pixels_over_10 0.50%.
  - parity_font_rye: max_delta 214 (FIRST text fixture under 231!),
    mean_delta 0.179, pixels_over_10 0.24%.
  - parity_font_cinzel: mean_delta 0.255, pixels_over_10 0.36%.
- floor_diag.py parity_font_inter: per-stem deltas collapsed from
  `[+1, +1, +0, +0, -1, -2, -3]` (max abs 3) to `[+0, +0, +0, -1,
  -1, -1, -1]` (max abs 1). pixels with delta>100: 4,671 → 2,132.
