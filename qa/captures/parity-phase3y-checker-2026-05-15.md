# Phase 3y: checker rollout — hypothesis refuted

**Date:** 2026-05-15
**Dispatch:** Consolidated rollout of Phase 3x Cand B (CPU y-phase
precompute) across 6 affected shaders. Checker first as
proof-of-concept (its Phase 3v +1 px shift bug was the Cand-A-
analog signature).
**Outcome:** Hypothesis REFUTED on checker. No source change shipped.

## What was tried

### Iteration 1: `u_y_anchor = mode_h - 1`, no half-pixel offset

```glsl
float fy_bot = floor(gl_FragCoord.y);
float fy_top = u_y_anchor - fy_bot;
float gy = floor(fy_top / u_tile);
```

Result: render_tests detected output change (1 fixture differ).
But empirical sample showed the Y-transition shifted to y=1
(massively wrong — previously at y=47 in baseline; Canvas2D at y=46).
Mean delta WORSE (13.872) than baseline (9.286).

Mechanism: at top of viewport, vc4 quantizes `gl_FragCoord.y =
1079.5` to either 1079 or 1080. With `u_y_anchor = 1079`, the
fy_top computation can yield negative values when quantization rounds
UP to 1080, breaking the tile-index math.

### Iteration 2: `u_y_anchor = mode_h`, subtract 0.5 in shader

```glsl
float fy_top = (u_y_anchor - fy_bot) - 0.5;
```

Result: render_tests reported 45/45 PASS — meaning the new shader
produces the SAME output as the existing buggy golden (with the
+1 px shift). The half-pixel offset didn't move pixels on Pi vc4.

Mechanism: vc4's quantization of gl_FragCoord.y is non-trivial.
Tried both round-down (truncate) and round-up theories; neither
predicts the exact empirical scanline positions from Phase 3w.
Without a precise model of vc4's actual quantization rule, any
formula derived from a wrong assumption can match the buggy
output by coincidence.

## Why scanlines (Phase 3x) succeeded but checker doesn't

Phase 3x Cand B's key trick was the **±0.5 step tolerance window**:
`step(abs(m - u_y_phase), 0.5)`. This catches BOTH possible
quantization outcomes of gl_FragCoord.y at every pixel center —
either truncate (1066) OR round-up (1067) gives the same yes/no
answer for "is this pixel on a scanline" because both land within
0.5 of u_y_phase.

For checker, the equivalent question is "WHICH TILE am I in", not
"yes/no on scanline". The answer is an integer tile index that
DIFFERS between the two possible quantization outcomes — at boundary
pixels (y_top=46, 92, 138...), the two outcomes give tile-index 0
vs tile-index 1, which produce OPPOSITE colors in the
`mod(gx+gy, 2)` parity check.

So the precision tolerance trick that worked for scanlines
fundamentally CAN'T work for checker without a different formulation.

## Mechanistic conclusion

The Phase 3x findings doc predicted: "Cand B-style precompute would
need x_phase AND y_phase. Likely fixable; one more probe cycle."

This Phase 3y attempt CONFIRMS that prediction was overly optimistic.
The checker bug requires more than a uniform-precompute — it requires
either:

1. **Exact knowledge of vc4's gl_FragCoord.y quantization rule**
   so we can derive a formula that produces correct tile indices.
2. **A pattern formulation that uses position-within-tile only**
   (which checker can't use because tile-index parity is the
   defining property).
3. **A fundamentally different rasterization approach** — e.g.,
   pre-rasterize the checker pattern CPU-side and upload as a
   texture; sample with GL_NEAREST. Sidesteps GPU shader math
   entirely.

## Per-shader generalization update (Phase 3x scope claims, revised)

| Shader | Cand B applicability | Notes |
|--------|----------------------|-------|
| FS_PATTERN_SCANLINES | ✅ Phase 3x worked | Position-within-tile + tolerance |
| FS_PATTERN_CHECKER | ❌ Phase 3y refuted | Needs absolute tile index |
| FS_PATTERN_DOTS | likely ❌ (same) | Cell-center distance requires tile parity |
| FS_PATTERN_HALFTONE | likely ❌ (same) | Two offset grids, same problem |
| FS_PATTERN_GRID | maybe ✅ | 1-px lines; yes/no question per axis |
| FS_PATTERN_RINGS | likely ❌ | Distance from center is absolute |
| FS_PATTERN_RAYS | likely ❌ | Angle from center is absolute |

Revised expectation: of the 6 originally targeted shaders, only
GRID is likely tractable via the same Cand B mod-direct + tolerance
pattern. The other 5 (checker, dots, halftone, rings, rays) need
a different formulation per the absolute-coordinate concern above.

## Recommendation

Three forward paths:

1. **Pre-rasterize approach**: write checker/dots/halftone/rings/
   rays patterns CPU-side to a 2D texture, upload, sample. Bypasses
   shader-precision entirely. Per-fixture cost: a CPU rasterizer
   each (some already exist in Python's reference). One uniform
   uniform texture sampler in shader. Approx 1-2 days work for all 5.
2. **vc4 quantization characterization**: write a calibration
   shader that reads gl_FragCoord.y across all 1080 rows, dumps
   the actual quantization outcome to a debug framebuffer, reads
   back via glReadPixels. Then derive correct formulas with
   knowledge of vc4's actual rule. Per-shader fix is ~10 LOC each
   after the rule is known. Cost: ~0.5 day for the calibration +
   ~2 hours per shader.
3. **Accept the visible bugs**: per qarl's morning AA-discrepancy-
   accepted rule, structural divergences are not OK — but the
   per-pixel "wrong color at tile boundary" might be acceptable
   if the visual effect is subtle. Probably NOT acceptable for
   checker (clear visible 1-px grid offset on white-on-orange
   text).

## Limitations

- Only checker probed in Phase 3y. The conclusion that the other 4
  shaders (dots/halftone/rings/rays) won't generalize is hypothesized
  from their math structure, not empirically tested.
- Iteration 2's "45/45 PASS" might be coincidence (mediump producing
  same buggy output as before) rather than the half-pixel offset
  being conceptually wrong. A different offset value might work.
  Not exhaustively explored.
- The "stop and surface" decision is per dispatch instruction;
  could have iterated further on offset values.
