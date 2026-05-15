# Phase 3aa: GRID fix — Cand B hybrid wins

**Date:** 2026-05-15
**Dispatch:** Apply playbook from Phase 3w/3x scanlines + 3z checker
to FS_PATTERN_GRID, picking fix profile per diagnostic.
**Outputs:** qa/captures/phase3aa-grid-{baseline,fixed}.png

## Diagnostic verdict: Cand B profile, dual-axis, with min-distance variant

Baseline empirical: Rust render had only x=0 and y=0 grid lines —
ALL OTHER lines at x=91, 182, 273 and y=91, 182, etc. were MISSING.
Same mediump-precision-loss signature as Phase 3w scanlines, but
manifesting on BOTH axes because grid has both vertical AND
horizontal 1-px lines.

Diagnostic categorization (per dispatch's 4 options):
- (a) +1 px shift like checker — **NO**: lines completely absent, not shifted.
- (b) missing/duplicate lines like scanlines — **YES** on both axes.
- (c) AA fringe at line edges — N/A (hard-step lines, no AA).
- (d) tile-coord parity-flip — N/A (lines, not tile parity).

Cand B (position-within-tile, ±0.5 tolerance) is the matching profile.
Adaptations needed beyond Phase 3x scanlines:
- **X axis**: at small magnitudes, vc4 mediump preserves
  fractional gl_FragCoord.x precision. Use `min(mx, tile - mx) <= 0.5`
  to catch mod near both 0 and tile (the two boundary positions in
  the periodic pattern). No phase uniform needed for X.
- **Y axis**: same large-magnitude precision loss as scanlines.
  Use Cand B's phase-anchored test, with phase = `mod(mode_h, tile)`
  (NOT `mod(mode_h - 0.5, tile)` like scanlines). The empirical
  Phase 3z lesson: vc4 rounds half-up on int conversion, and the
  same round-half-up behavior shows up in mediump mod() at large
  magnitudes — so the gl_FragCoord.y=1079.5 quantizes to 1080 (not
  1079), and the matching phase is `mod(1080, 91)=79` (not 78.5).

## Iteration trail

| Iter | y_phase | X test                       | Result                                    |
|------|---------|------------------------------|-------------------------------------------|
| 1    | `mod(h-0.5, tile)=78.5` | `step(abs(mx-0.5), 0.5)` | Lines back but +1 px shift (line at x=1, 92 vs Canvas2D 0, 91) — too-wide tolerance catches mod=0 AND mod=1 |
| 2    | `mod(h-0.5, tile)=78.5` | `step(min(mx, tile-mx), 0.5)` (no phase for X) | X axis clean (line at x=0, 91); Y still 2-px wide (lines at y=0 AND y=1) — phase off by 0.5 |
| 3    | `mod(h, tile)=79`       | `step(min(mx, tile-mx), 0.5)` (no phase for X) | **EXACT match both axes** |

## Post-fix metrics (parity_tests.sh)

| Metric        | Pre-3aa | Post-3aa | Δ           |
|---------------|--------:|---------:|------------:|
| max_delta     | 229     | **218**  | -11         |
| mean_delta    | 2.544   | **0.063**| **-97%**    |
| SSIM          | 0.9080  | **0.9986**| +0.091     |
| pct_over_10   | 2.19%   | **0.18%**| -92%        |

Note: max_delta=218 (NOT the Cause B 229 floor!) — grid's max
floor is slightly LOWER than other patterns because the "GRID"
text glyph happens to AA at lower-contrast amplitude. The
remaining 0.18% diff pixels are all Cause B text outline.

## Mechanistic insight: vc4 round-half-up on EVERYTHING

The Phase 3z checker fix exposed that vc4 int() rounds half-up.
Phase 3aa now confirms vc4's mediump mod() at large magnitudes
ALSO behaves as if gl_FragCoord.y was round-half-up'd. The grid
fix's correct phase = `mod(mode_h, tile)` (no -0.5) only works
if the gl_FragCoord.y=1079.5 is being treated as 1080 by vc4.

Scanlines' Cand B used `mod(mode_h - 0.5, tile)` and worked.
Why? Because scanlines tile=13: `mod(1079.5, 13)` and
`mod(1080, 13)` both equal `0.5` and `1` respectively — both
within ±0.5 of phase=0.5. So scanlines worked DESPITE the
mismatched -0.5 assumption due to a coincidence in tile=13.

Grid tile=91 doesn't have that coincidence: phase=78.5 vs the
correct phase=79 are exactly 0.5 apart, so the tolerance window
shifts by half a tile-width's worth of mod-space, catching the
wrong line.

**Playbook update**: the canonical Cand B phase formula for shaders
that need y-flip-precision-safety should be `mod(mode_h, tile)`
or equivalently `mod(viewport_h, tile)` — NOT `mod(viewport_h - 0.5,
tile)`. The scanlines fix should be retroactively reviewed for
correctness on non-13-tile densities; if it shows similar bug at
other densities, it needs the same `-0.5` drop. (Out of scope this
slice; queue Phase 3ab for scanlines audit.)

## Per-axis verification

Both X and Y boundary positions exactly match Canvas2D at every
tested transition (x ∈ {0, 91, 182, 273} and y ∈ {0, 91, 182, 273}).

## Subagent review TBD

Awaiting pre-commit subagent review.

## Phase 3aa-followup scope

Per Phase 3z scope notes: RINGS uses `length()` (float distance
from center), which is fundamentally different from grid/scanlines/
checker's tile-coord math. RAYS uses angles. Neither is a direct
Cand B/E port; each needs a new fix profile probe.

Ranked candidates remaining (closer to existing playbook → further):
1. **RINGS** — closer; periodic concentric bands, can use min-distance
   trick on radius-mod-tile if mediump precision on `length()`
   doesn't break the math.
2. **RAYS** — farther; angle math has no clean integer equivalent.

Recommend Phase 3ab dispatch (separate): scanlines audit at multiple
densities to confirm or refute the "tile=13 coincidence" hypothesis.
Phase 3ac: RINGS probe.

## Limitations

- One fixture probed (grid). The "vc4 round-half-up on mediump mod"
  hypothesis is consistent across grid/checker/scanlines empirically
  but not proven by direct instrumentation.
- Density tested: 0.5 (curve → 0.25 → tile=91). Other densities
  not exercised in this slice.
