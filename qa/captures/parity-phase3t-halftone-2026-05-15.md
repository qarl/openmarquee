# Phase 3t: broad-tier halftone smoothstep AA + sub-pixel anchor fix

**Date:** 2026-05-15
**Dispatch:** Same Phase-3l/3s playbook for parity_bg_pattern_halftone.
**Probe:** `scripts/parity/halftone_diag.py`
**Outputs:** `qa/captures/halftone-{canvas2d,rust,diff,tile-crop}.png`,
`qa/captures/halftone-diag-summary.json`

## Picked fixture

`parity_bg_pattern_halftone` (broad tier, mean=7.625, max=229).
Shares the hard-step shader pattern with dots (Phase 3s, c8dc73f).
Density=0.5 → tile=33 (ODD!), radius=11, two grids offset by half.

## Spatial decomposition (pre-fix)

```
Max delta any-channel:   229
Mean delta any-channel:  14.135
Pixels with delta>=10:    230,183 (11.10%)
Pixels with delta>=50:    175,034  (8.44%)
Pixels with delta>=100:   129,975  (6.27%)
Pixels with delta>=200:    68,221  (3.29%)
```

Per-quadrant uniform (14.0-14.3). R/G/B: max 229/213/198. 88.16% of
pixels at delta<5; the remaining 12% spreads across all delta bands
with a heavy 200+ tail (3.29% — 6× worse than dots' tail).

Visual `halftone-tile-crop.png`: Canvas2D smooth AA circles, Rust
blocky hard-stepped circles. Diff shows symmetric concentric rings
at every circle boundary — same AA signature as dots. Plus heavier
overlap areas where dot grids cross.

## Dominant cause — TWO mechanisms

After applying the Phase 3s smoothstep AA, max_delta UNCHANGED at
229 (mean improved only -0.3). Deeper diag of the residual found a
second mechanism:

**Cause 1 — AA-function mismatch** (same as dots).
**Cause 2 — sub-pixel anchor offset between Rust and Canvas2D.**

`halftone_uniforms` had:
```rust
let half = (tile * 0.5).floor();  // tile=33 → half=16 (integer floor)
```
The comment said "Python uses `tile // 2` (integer floor divide)" —
but Phase 3l-post abandoned Python as the reference (Canvas2D is now
canonical). Canvas2D's `ui/src/bg-system.js:291` uses JS
`tile / 2 = 16.5` (no floor — keeps the half-pixel offset for odd
tiles).

The 0.5-px grid misalignment between Rust's layer-with-half and
Canvas2D's layer-0 produced full-color-flip pixels at locations
where one renderer placed a dot and the other placed bg. That's the
3.29% pixels-over-200 tail.

## Two-part fix

1. **`FS_PATTERN_HALFTONE`**: smoothstep AA on the min-distance to
   either grid's nearest center (Phase 3s playbook):
   ```glsl
   float d_min = min(length(cell1), length(cell2));
   float t = 1.0 - smoothstep(u_radius - 0.5, u_radius + 0.5, d_min);
   ```

2. **`halftone_uniforms`**: drop the `.floor()` on `half`:
   ```rust
   let half = tile * 0.5;  // was (tile * 0.5).floor()
   ```

Updated unit test (`halftone_uniforms_match_canvas2d_anchors`) adds
density=0.5 case asserting half=16.5 (sub-pixel) for tile=33 (odd).

## Post-fix metrics (parity_tests.sh)

| Fixture                        | Metric        | Pre-3t  | Post-3t | Δ        |
|--------------------------------|---------------|--------:|--------:|---------:|
| parity_bg_pattern_halftone     | max_delta     |     229 |     229 | 0        |
|                                | mean_delta    |   7.625 |   5.026 | **-2.60** |
|                                | SSIM          |  0.8621 |  0.9225 | **+0.060** |
|                                | pct_over_10   |  11.10% |  11.14% | +0.04%   |
| parity_animated_halftone_pulse | max_delta     |     229 |     211 | **-18**   |
|                                | mean_delta    |   8.652 |   6.106 | **-2.55** |
|                                | SSIM          |  0.8489 |  0.9116 | **+0.063** |

Largest mean_delta + SSIM improvements of any broad-tier fix so far
(stripes f2896f7 was similar magnitude; dots c8dc73f was smaller).

Gate count: 0/39 PASS at max_delta≤50 (unchanged — architectural
floor per cf11215 persists). The residual max=229 on the static
fixture is a few pixels at deep overlap regions where the min-
distance smoothstep produces a sharp transition that Canvas2D's
per-arc bilinear-AA composition doesn't.

## Risk callout

Changing `half` from integer to float for odd tiles is a behavioral
change that affects only FS_PATTERN_HALFTONE rendering. Verified
isolated: render_tests.sh showed only 2/45 fixtures changed
(bg_pattern_halftone.png + animated_halftone_pulse.png). No other
shader or layout code consumes `half`.

The PYTHON renderer would have produced different output with this
change (Python `// 2 = 16`), but Python is no longer the canonical
reference (Phase 3l-post). The test name renamed from
`match_python_anchors` → `match_canvas2d_anchors` to reflect this.

## Limitations

- The residual max=229 on the static halftone could be addressed by
  per-layer smoothstep (max(t1, t2)) instead of min-distance
  smoothstep. Deferred — current fix is already substantial.
- Other broad-tier candidates ranked by likely similar cause:
  bricks (mean=7.451), checker (mean=5.045), confetti (mean=3.087),
  scanlines (mean=9.256). All use `step()` in their shaders.
  Bricks/checker most analogous to dots/halftone (round-edge AA);
  scanlines uses thin lines (different AA profile).
