# Phase 3z: checker tile-index-parity probe — Cand E refined wins

**Date:** 2026-05-15
**Dispatch:** Phase 3y refuted Cand B (position-within-tile tolerance)
on checker. Re-probe with 3 candidates targeting integer tile-index
preservation rather than position-within-tile precision.
**Captures:** qa/captures/phase3z-{baseline,cand-D,cand-E-iter1,cand-E-refined}.png

## Candidates

- **D: CPU-precomputed tile-row count uniform** — pass
  `u_y_tiles_minus_1` per frame; shader computes
  `gy_top = u_y_tiles_minus_1 - floor(gl_FragCoord.y / tile)`.
- **E: int-domain coord reconstruction** — cast `gl_FragCoord.xy`
  + `u_viewport.y` to `int` early; do tile-index math in pure
  integer space; cast back at the very end for color selection.
- **F: vertex-passed flat tile-index varying** — emit (gx, gy) at
  vertex stage. NOT TESTED in this slice (skipped after Cand E
  delivered decisive win; GLES2 1.00 doesn't have `flat` varying
  qualifier, would need workaround that's likely brittle).

## Results

| Candidate    | mean   | max | pct≥200 | SSIM    | First X trans | First Y trans | Cleared? |
|--------------|-------:|----:|--------:|--------:|--------------:|--------------:|----------|
| Baseline     | 9.489  | 229 |  4.014% | 0.9054  | x=47          | y=47          | NO       |
| **D**        | 109.975| 229 | 47.904% | n/a     | x=47          | y=22          | NO, WORSE|
| **E iter 1** | 4.893  | 229 |  2.007% | n/a     | x=46 ✓        | y=47          | partial  |
| **E refined**| 0.316  | 229 |  0.009% | 0.9960* | x=46 ✓        | y=46 ✓        | **YES**  |
| F            | not tested | | | | | | n/a       |

Canvas2D reference: first X transition at x=46, first Y at y=46.

\*SSIM measured via parity_tests.sh against freshly-blessed golden.

## Mechanistic explanation

### Why Cand D failed

Tile-rows-from-bottom are NOT a simple flip of tile-rows-from-top
when viewport_h isn't an integer multiple of tile. At
viewport=1080, tile=46: `1080/46 = 23.478` (not integer). The
"remainder" 22 pixels at the top creates an asymmetric mapping
where Cand D's `(tiles - 1 - gy_bot)` math overshoots by 24
pixels. First Y transition at y=22 instead of y=46.

### Why Cand E iter 1 partially failed

`y_top = viewport_h - 1 - y_bot` with `y_bot = int(gl_FragCoord.y)`
got X right but kept Y at +1 shift. Diagnostic: vc4's `int(float)`
conversion appears to ROUND (half-away-from-zero) rather than
TRUNCATE. At pixel y_top=46, gl_FragCoord.y=1033.5, vc4 returns
int=1034 (rounded up). Then `y_top = 1080 - 1 - 1034 = 45`, which
floor-divides to gy=0 — wrong for tile-row-46.

### Why Cand E refined wins

Dropping the `-1` from `y_top = viewport_h - y_bot` compensates
for vc4's int-rounding behavior:

```glsl
int y_bot = int(gl_FragCoord.y);     // 1080→1080, 1033.5→1034 (vc4 rounds)
int y_top = viewport_h - y_bot;       // 1080-1080=0, 1080-1034=46
int gy = y_top / tile_i;              // 0/46=0, 46/46=1
```

This recovers correct tile-row indices for ALL y_top values
without precision loss. X already worked because vc4's int-rounding
at pixel x=46 still yields a valid tile-1 result.

## Decision

**Ship Cand E refined as Phase 3z fix.**

Parity metrics (parity_tests.sh) post-fix + re-bless:
- mean_delta: 9.489 → **0.227** (-97%)
- SSIM: 0.9054 → **0.9960** (gate crossed)
- pct≥10: 4.30% → 0.38%
- max=229 (Cause B floor at "CHECKER" text glyph — accepted)

## Phase 3z-followup scope

Cand E's int-domain pattern *may* generalize to other shaders.
Likely-applicable signatures vs unclear cases:

- **FS_PATTERN_GRID**: 1-px lines at every N rows + cols. The
  "is this pixel on a tile-boundary line" question is more
  position-within-tile (Cand B profile) than absolute parity,
  but the y-flip large-magnitude subtraction is the same root
  bug Cand E sidesteps. Worth probing as a hybrid.
- **FS_PATTERN_RINGS**: distance-from-center via `length()` is
  fundamentally float; Cand E int-domain doesn't trivially port.
  May need a different profile entirely.
- **FS_PATTERN_RAYS**: angle math has no clean integer equivalent.
  Likely NOT a Cand E candidate.

Recommend Phase 3aa dispatch: probe GRID first to see if Cand E
hybrid (int for y-flip, float for line-detection) works. Defer
RINGS until a 3rd fix profile is articulated.

## Broader playbook: parity-gate as Phase 3l/3s/3t/3u-style "fix
one shader, ship it, move to next"

The Phase 3 arc has now produced two complementary fix profiles:

| Profile | Examples | When it applies |
|---------|----------|-----------------|
| **Cand B** (position-within-tile) | Scanlines (Phase 3x) | Yes/no per-pixel question; precision tolerance via ±0.5 step window |
| **Cand E** (int-domain) | Checker (Phase 3z), candidates grid/rings | Absolute tile-index parity needed; int math sidesteps fragment-shader mediump |

A shader might need BOTH if it has tile-index parity AND sub-tile
positioning concerns (e.g., dots/halftone where dot centers depend
on tile index AND dot radius is sub-tile). But neither dots nor
halftone showed a structural divergence post-Phase 3s/3t — they
landed within mean<8 with the AA-smoothstep fix alone. So no
shader currently needs BOTH.

## Risk callout

- Cand E assumes vc4's int() conversion is CONSISTENT
  (round-half-up). If a Mesa update changes the rule, the `-1`
  drop might bite differently. Empirical, not spec-mandated;
  re-verify on Mesa upgrade.
- `precision highp int` in fragment shader is optional in GLES2
  but Pi vc4 supports it (compiled + ran on Pi without issue).
  If a future Pi target doesn't, the int() math could silently
  downgrade. Consider explicit precision detection in install.sh.

## Limitations

- Cand F (vertex-passed flat varying) NOT TESTED. GLES2 1.00
  doesn't have `flat` qualifier; would need a workaround.
  Skipped given Cand E's decisive win + context budget.
- "Cand E refined" is iter 2 of Cand E; iter 1 (with `-1`)
  partially worked, demonstrating that vc4's int-rounding is
  the key variable. Both iterations counted as "Cand E" in
  decision-tree framing.
- The `((gx + gy) / 2) * 2` integer-mod trick avoids a separate
  `mod()` call; equivalent to `(gx + gy) % 2` in C. Verified
  parity output is 0 or 1 only.
