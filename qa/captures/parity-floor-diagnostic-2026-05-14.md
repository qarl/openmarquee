# Phase 3e Floor-Diagnostic Findings — 2026-05-14

## Headline verdict

The 229/231 max_delta floor is **cumulative per-glyph position drift**: ~1 px per character between Canvas2D and Rust renderers, accumulating to ~4 px across "INTER" (6 stems). Surfaces as full-height vertical-column disagreements at each glyph stem.

**Hypotheses (i)/(ii)/(iii)/(iv) named in dispatch REFUTED. Hypothesis (v) cumulative-advance-rounding SUPPORTED but at small magnitude (~1 px/glyph), not the centroid-based 42 px my initial reading claimed.** Pre-commit subagent review caught the centroid-interpretation flaw; per-stem extraction added to the diagnostic produced the correct measurement.

## Raw data — font_inter

Script: `scripts/parity/floor_diag.py`. Fixture: `parity_font_inter` (closest to PASS by mean=0.504).

| metric | value |
|---|---|
| image dims | 1920 × 1080 |
| max_delta | 231 |
| mean_delta | 0.504 |
| pixels `|delta|`>100 | 4,671 (0.225%) |
| pixels `|delta|`=0 | 2,064,995 (99.585%) |
| direction split | canvas2d=glyph rust=bg: 2,847 (61%); rust=glyph canvas2d=bg: 1,824 (39%) |
| top disagreement columns | x=554, 555, 634, 1049, 1227 each at exactly 216 rows (full box height) |

**Per-stem position deltas** (rust_center − canvas2d_center for each "INTER" glyph stem):

| stem | canvas2d_x | rust_x | δ |
|---|---|---|---|
| I | 647 | 648 | +1 |
| N-left | 791 | 791 | 0 |
| N-right | 927 | 927 | 0 |
| T | 1063 | 1062 | −1 |
| E | 1241 | 1239 | −2 |
| R | 1349 | 1346 | **−3** |

Cumulative drift across the text strip: +1 at I → −3 at R, total spread = 4 px. Per-glyph drift ≈ −0.8 px. The 4,671 disagreement pixels are concentrated at the stem columns where the two renderers' stems land 1-3 px apart (×216 rows tall each).

## Float-precision check (refutes hypothesis iii)

| | norm | canvas px | binary-int? |
|---|---|---|---|
| box.x | 0.05 | 96.0 | ✓ |
| box.y | 0.4 | 432.0 | ✓ |
| box.w | 0.9 | 1728.0 | ✓ |
| box.h | 0.2 | 216.0 | ✓ |

All four box coords round-trip to exact integers in float64. Refutes hypothesis (iii).

## Hypothesis verdicts

| # | hypothesis | verdict | evidence |
|---|---|---|---|
| (i) | Math.round(drawX) integer-snap | REFUTED | A 1-px-snap would produce 1-px-wide slivers, not 1-3 px wide stem-aligned full-height columns at multiple positions |
| (ii) | Center-alignment fractional drawX | REFUTED | boxX=96, boxW=1728 — both integer; natural drawX is integer |
| (iii) | box.x non-binary-representable | REFUTED | all four box coords binary-integer in float64 |
| (iv) | combination | REFUTED | individual components refuted |
| (v) | **cumulative per-glyph divergence** | **SUPPORTED** | per-stem δ grows from +1 to −3 across 6 stems; matches a per-glyph rounding-or-pad-asymmetry effect |

## What drives the per-glyph drift

Two interacting mechanisms, both surfacing as Canvas2D-vs-Rust divergence per glyph:

1. **`advance_width.round()` at different scales**. Canvas2D Phase 3c rasterizes at `effective_size_px ≈ 196`; per-char advance ≈ 140 (rounded). Rust rasterizes at original 1037; per-char advance ≈ 700 (rounded). Same code, different scales. The cumulative rounding error per char at Canvas2D's scale is `±0.5 / 140` = 0.36%, vs Rust's `±0.5 / 700` × yScale = 0.014%. Per-glyph drift: ~0.3-0.5 px.

2. **Bitmap pad survives differently**. Both renderers add a 1-px pad on each side (Phase 2). On Canvas2D the 1-px pad is at canvas scale → glyph offset by +1 inside bitmap → +1 canvas-pixel. On Rust the 1-px pad is at *original* scale, then GL_LINEAR-downscaled by yScale=0.21 → effective canvas-pixel pad ~0.21 px. Per-glyph offset diff: ~0.79 canvas-pixels.

The two mechanisms compound. Per-glyph net drift ≈ 0.5-1 px. Cumulative across "INTER": 4 px. Matches the observed +1 → −3 stem drift.

## Phase 3f fix options

| option | impact | risk |
|---|---|---|
| **A** Make Rust ALSO rasterize at `effective_size_px` (mirror Canvas2D Phase 3c on the Rust side). Glyph bitmap at canvas-pixel dims; no GL_LINEAR glyph-texture downscale. Both renderers round advances + apply pad at the same scale. | **Closes both mechanisms.** Predicted max_delta drops to <50 on every WASM-path fixture. First PASS-count crossing. | Touches Rust hot path. Re-bless every golden. |
| B | Revert Canvas2D Phase 3c (back to bilinear post-rasterize) | Both renderers downscale at draw time. UnifrakturCook regression returns. Mean wins from Phase 3c disappear (font_inter goes 0.50 → 16.0). | Visual quality degrades on thin-stroke fonts. |

**Recommendation: A.** The Phase 3c mean-delta wins are real visual improvements (font_inter 16 → 0.5, blend_screen 60 → 1.9, etc.). Mirroring the approach on Rust is the right architectural answer — both renderers should rasterize at canvas-pixel dims so the operator's preview matches Pi output. Re-bless is needed regardless to capture the post-Phase-3c output as the new golden.

Phase 3f scope: thread `target_height_px` (or `effective_size_px`) through `renderer/src/hdmi_logic.rs::layout_text_to_alpha` + `paint_slide` quad placement. Handful of lines + cargo test updates + render_tests.sh re-bless + parity_tests.sh re-bless.
