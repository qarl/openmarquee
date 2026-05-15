# Phase 3af: halftone + bricks polish — diagnostic-only, accepted floor

**Date:** 2026-05-15
**Dispatch:** Both fixtures pass new SSIM + mean_delta gate (09b381a)
but borderline. Halftone SSIM=0.9225 (+0.0025 margin), bricks
SSIM=0.9431 (+0.0231 margin). Goal: widen margin 1-3% so future
drift can't tip them below gate.
**Status:** Fix attempted, REVERTED. vc4 mediump precision floor
on `pos.y = u_viewport.y - gl_FragCoord.y` caps the achievable Pi-
side gain below the f64-sim prediction. Same accepted-floor pattern
as Phase 3ad RAYS and Phase 3ae CONFETTI. No source change shipped.

## Diagnostic-first measurement

`scripts/parity_tests.sh` at HEAD:
- `parity_bg_pattern_halftone`: SSIM=0.9225, mean=5.026, max=229,
  pct≥10=11.14% (margin +0.0025).
- `parity_bg_pattern_bricks`: SSIM=0.9431, mean=3.281, max=229,
  pct≥10=3.20% (margin +0.0231).

## Halftone divergence: AA shape + composition

`FS_PATTERN_HALFTONE` (Phase 3t) uses smoothstep AA on
`min(d_layer1, d_layer2)`. Canvas2D mirror (`ui/src/bg-system.js:
286-306`) draws each layer as `ctx.arc + ctx.fill` with source-over.
Two mismatches against Canvas2D:

1. **AA SHAPE.** Canvas2D's coverage-based AA is approximately
   LINEAR (`coverage ≈ clamp(r - d + 0.5, 0, 1)`), not Hermite.
   Smoothstep over-saturates the mid-AA band: at d=r±0.25
   smoothstep gives t=0.156/0.844 vs linear 0.25/0.75.

2. **PER-LAYER COMPOSITION.** Canvas2D draws two layers source-over;
   for same color_b that's screen blend `c1+c2-c1*c2`. min(d1, d2)
   under-counts the union where both layers contribute partial
   coverage in narrow gaps (~1.2 px at curved density 0.25).

## Bricks divergence: half-pixel offset on course-1 vertical mortar

`FS_PATTERN_BRICKS` (Phase 3u) uses `floor(pos.x)` + integer-step
mortar detection. Density=0.5 curves to 0.25 → bw=109, bh=55,
u_half=54.5 (fractional for odd bw).

For pixel col 54 (center pos.x=54.5):
- Canvas2D `ctx.fillRect(54.5, y, 2, h)` covers x ∈ [54.5, 56.5];
  pixel col 54 [54, 55] gets coverage 0.5 (mortar 50%).
- Rust col=floor(54.5)=54; c1=mod(54 - 54.5, 109)=108.5; v_mortar=0
  (no mortar at all). 

For pixel col 56: Canvas2D 50% / Rust 100%. Net: 0.5-px-right offset
+ 1-px-narrower mortar on every course-1 vertical line; ~19k pixels
in delta=[100, 150) histogram bucket.

Course-0 (offset=0) is unaffected because `mod(col, bw)` is integer-
aligned with the integer-x `fillRect(0, y, 2, h)`.

## Attempted fix: linear-coverage trapezoidal AA + per-layer screen

For both shaders, tried a pixel-coverage-accurate model matching
Canvas2D's rasterization.

### Halftone rewrite (attempted)

```glsl
float c1 = clamp(u_radius + 0.5 - d1, 0.0, 1.0);
float c2 = clamp(u_radius + 0.5 - d2, 0.0, 1.0);
float t = c1 + c2 - c1 * c2;
```

### Bricks rewrite (attempted, two variants)

V1: full trapezoidal AA on both axes
```glsl
float vy = mod(pos.y, u_bh);
float h_mortar = clamp(min(vy + 0.5, 2.5 - vy), 0.0, 1.0);
// ... v_mortar same shape
```

V2: hybrid (step+floor for h_mortar, trapezoidal for v_mortar)
```glsl
float row = floor(pos.y);
float h_mortar = step(mod(row, u_bh), 1.5);
// ... v_mortar trapezoidal
```

## Numerical validation (f64 sim vs Canvas2D capture, text-masked)

|                                | max | mean  | pct≥10 |
|--------------------------------|-----|-------|--------|
| halftone smoothstep min-dist   |  47 | 1.524 | 5.799% |
| halftone linear screen (NEW)   |  39 | 1.213 | 5.606% |
| bricks step + floor (CURRENT)  | 115 | 1.019 | 0.890% |
| bricks linear screen (NEW)     |  10 | 0.009 | 0.000% |

f64 sim predicted huge gain on bricks (~pixel-perfect) and modest
gain on halftone. So the fix SHAPE is correct.

## Pi-side measurement: vc4 mediump precision is the actual floor

After re-blessing goldens from the new shaders and running parity:

|                                | SSIM    | mean  | pct≥10  |
|--------------------------------|---------|-------|---------|
| halftone (HEAD)                | 0.9225  | 5.026 | 11.14%  |
| halftone (linear-screen 3af)   | 0.9267  | 4.976 | 11.74%  |
| bricks (HEAD)                  | 0.9431  | 3.281 | 3.20%   |
| bricks (linear-screen V1)      | 0.9211  | 4.671 | 5.47%   |
| bricks (hybrid V2)             | 0.9388  | 3.565 | 3.67%   |

Halftone gained +0.0042 SSIM — present but tiny vs the f64 prediction.
Bricks REGRESSED in both V1 and V2 vs HEAD. Reverted.

### Why the Pi-side underperforms the f64 sim

`pos.y = u_viewport.y - gl_FragCoord.y` is the Phase 3w/3y precision
trap: vc4 mediump's 10-bit-mantissa subtraction of a large operand
from another large operand collapses to ~1-px integer-noise. The
Phase 3u step+floor formulation absorbed this noise by integer-
quantizing `floor(row)` before any modular arithmetic, so the
fractional jitter never leaked into the mortar comparison.

Trapezoidal AA + linear-coverage formulas, by design, propagate
fractional `pos.y` directly into the coverage output. On the Pi
that means the vc4 precision noise smears every mortar line into a
soft 1-2 px band -- visible as wider mortar in the Pi golden vs
Canvas2D's crisp 2-px line. The "fix" was conceptually right at f64
but architecturally wrong on vc4: the y-flip subtraction is the
underlying problem, and treating its output as precision-clean
fractional float is what the Phase 3w lesson already taught us not
to do.

A *complete* Cand-B-style fix would precompute `u_y_phase_bh` =
mod(viewport_h, bh) CPU-side and rewrite the shader to use
gl_FragCoord.y directly (no large subtraction), mirroring what
Phase 3x did for scanlines and Phase 3aa did for grid. That would
let the linear-coverage AA produce its f64-predicted gain. Cost: +1 uniform
+ shader rewrite for halftone (and a 2*bh phase uniform for bricks'
course detection). Deferred -- the current margins clear the SSIM
gate with headroom, and the absolute SSIM improvement is small
enough that the architectural cost isn't justified for this slice.

## Verdict (accepted-floor pattern)

**No source change.** Same diagnostic-first conclusion as:
- Phase 3ad RAYS — AA-only / accepted-floor (max=229 at AA edges,
  shape correct, no fix-needed).
- Phase 3ae CONFETTI — explicitly-divergent-by-design (different
  RNG families).

Phase 3af adds a third species: **AA-shape-correct-but-precision-
floor** — the fix shape works in f64 but vc4 mediump caps the gain.
Future polish slice (if the gate is ever tightened) could ship the
full Cand-B-style precomputed-y-phase rewrite to unlock the f64-
predicted gain. Until then, the existing step+floor + smoothstep
formulation is the better Pi-tuned shape.

## Risk callout

- No source change. Goldens not re-blessed for this slice (the
  attempted-then-reverted rev sequence cycled the golden bytes
  through bless during testing; revert pass restores HEAD goldens).
- The Cand-B-precomputed-y-phase generalization is a candidate
  Phase 3ag if a future drift narrows halftone's +0.0025 margin.
- `parity_animated_halftone_pulse` continues to FAIL at SSIM=0.9116
  (pre-existing, observed at HEAD before this slice; same shader
  family as halftone but with motion offsets that move dots through
  many fractional positions, amplifying the AA shape + vc4 precision
  mismatch). Out of scope for Phase 3af -- the dispatch was static
  halftone + bricks. Separate slice candidate.
