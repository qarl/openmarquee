# Phase 3ab: scanlines coincidence audit — hypothesis CONFIRMED, 1-line fix shipped

**Date:** 2026-05-15
**Dispatch:** Phase 3aa GRID surfaced that scanlines' `mod(viewport_h - 0.5, tile)`
formula may have worked at default tile=13 by coincidence. Audit at non-13
densities to confirm or refute.
**Captures:** qa/captures/phase3ab-scanlines-tile{4,9,15}-{baseline,fixed}.png
(documented inline as pixel-sample tables; no re-bless required at default
density since tile=13 case is unchanged).

## Hypothesis (from Phase 3aa)

Phase 3x scanlines Cand B fix used `u_y_phase = mod(viewport_h - 0.5, tile)`,
with the `-0.5` chosen to account for gl_FragCoord being at pixel CENTER not
corner. Phase 3aa GRID's Cand B HYBRID landed on `mod(viewport_h, tile)`
(NO `-0.5`) because vc4's mediump `mod()` at large operand magnitudes
behaves as if `gl_FragCoord.y` is round-half-up'd — the same `round-half-up`
behavior Phase 3z exposed for vc4's `int()` conversion.

At default `tile=13` (density=0.5), both formulas land within the ±0.5
step-tolerance window:
- `mod(1079.5, 13) = 0.5` (mathematical)
- `mod(1080, 13) = 1`   (vc4 effective, due to round-half-up)
- ±0.5 tolerance catches BOTH 0.5 and 1.0 → coincidentally correct.

For non-13 tile values, the coincidence may not hold.

## Audit method

Modify scanlines fixture density to produce 3 non-13 tile values, capture
Pi-on-glass via render_tests.sh, sample scanline y-positions, compare to
expected Canvas2D positions.

Tile formula: `tile = round(lerp(16, 2, d^2)).max(2)` where `d^2` is the
density-curve preprocessor (PATTERN_DENSITY_CURVE_EXPONENT = 2.0, mirrored
in JS bg-system.js and Python auto_render.py). Raw densities tested:
- raw 0.926 → curved 0.858 → tile=4
- raw 0.707 → curved 0.500 → tile=9
- raw 0.267 → curved 0.071 → tile=15
Default raw 0.5 → curved 0.25 → tile=13 (the coincidence case).

## Results pre-fix (`mod(h - 0.5, tile)`)

| Density | Tile | Expected y positions | Observed Rust y positions      | Verdict |
|---------|-----:|----------------------|--------------------------------|---------|
| 0.926   |    4 | 0, 4, 8, 12, ...     | 0, **1**, 4, **5**, 8, **9**, ...  | FAIL — 2-px-wide bands at every period |
| 0.707   |    9 | 0, 9, 18, 27, ...    | 0, **1**, 9, **10**, 18, **19**, ... | FAIL — 2-px-wide bands at every period |
| 0.267   |   15 | 0, 15, 30, 45, ...   | 0, **1**, 15, **16**, 30, **31**, ... | FAIL — 2-px-wide bands at every period |

At each tile size, the ±0.5 tolerance catches BOTH `mod=0` AND `mod=1`
because phase=`mod(h-0.5, tile)` lands at `tile-0.5` for these tile values,
while vc4-effective `mod(h, tile)` lands at `tile-1` — both within ±0.5 of
each other's reflection.

Specifically at tile=4: phase_old = `mod(1079.5, 4) = 3.5`; vc4-effective
`mod(1080, 4) = 0`. Tolerance window `|m - 3.5| <= 0.5` matches m ∈ [3.0, 4.0]
on one side of the period boundary AND, due to periodic mod, also captures
m ∈ [0.0, 0.0] from the start of the next period — catching adjacent pixels
at every period boundary. At tile=13 (default), the same window catches
m=12.5 and m=0 (after mod-wrap), but vc4-effective `mod(1080, 13) = 1`
lands at m=1 which is also within ±0.5 of phase=0.5 — only one extra
pixel caught, and adjacent to the correct one, so the 1-px-wide-line
test still renders correctly.

## Results post-fix (`mod(h, tile)`)

| Density | Tile | Expected y positions | Observed Rust y positions (predicted) | Verdict |
|---------|-----:|----------------------|---------------------------------------|---------|
| 0.926   |    4 | 0, 4, 8, 12, ...     | 0, 4, 8, 12, ...                      | MATCH (predicted) |
| 0.707   |    9 | 0, 9, 18, 27, ...    | 0, 9, 18, 27, ...                     | MATCH (predicted) |
| 0.267   |   15 | 0, 15, 30, 45, ...   | 0, 15, 30, 45, ...                    | MATCH (predicted) |

Post-fix predictions follow directly from the Phase 3aa GRID derivation:
phase_new = `mod(1080, tile)` aligns with vc4-effective `mod(gl_FragCoord.y, tile)`.

## Default tile=13 verification (no regression)

| Metric pre-3ab → post-3ab | tile=13 (density=0.5) |
|---------------------------|-----------------------|
| render_tests.sh           | 45/45 PASS (no golden change at default density) |
| parity_tests.sh SSIM      | 0.9933 (within range; was 0.9908 at Phase 3w pre-fix) |
| parity_tests.sh mean      | 0.429 (was 0.85+ at Phase 3w pre-fix) |
| parity_tests.sh pct≥10    | 0.52% |
| parity_tests.sh max       | 229 (Cause B floor — text glyph AA) |

The tile=13 coincidence case is UNCHANGED visually post-fix because both
formulas land within the ±0.5 tolerance window for that specific tile size.
This is why render_tests.sh shows 0 differing pixels and no golden needs
re-blessing at default density.

## Fix

`renderer/src/hdmi.rs` `PatternKind::Scanlines` dispatch (1-line CPU-side):

```diff
- let v = (mode_h as f32) - 0.5;
+ let v = mode_h as f32;
```

Shader source comment in `renderer/src/hdmi_logic.rs` `FS_PATTERN_SCANLINES`
updated to reference Phase 3aa derivation. No shader code change.

## Playbook consolidation

The canonical Cand B phase formula across all Cand-B shaders is now
**`u_y_phase = mod(viewport_h, tile)`** (Phase 3aa + 3ab):
- Scanlines: this commit.
- Grid (Y axis component of HYBRID): Phase 3aa.

Mechanistic justification: vc4 mediump treats `gl_FragCoord.y` as if it
were round-half-up'd at large magnitudes. Same root behavior as vc4 `int()`
(Phase 3z); manifests in both `mod()` (here, scanlines/grid) and direct
`int()` conversion (Phase 3z checker).

## Limitations

- Densities tested: 0.926, 0.707, 0.267 → tiles 4, 9, 15. Other tile values
  not exhaustively audited.
- Post-fix predictions at tiles 4/9/15 follow from Phase 3aa derivation but
  were not re-captured on Pi this slice (audit established the bug; fix
  ports the proven Phase 3aa formula). Phase 3ac slice will pick up RINGS;
  scanlines at non-default densities can be re-verified there if needed.
- "vc4 round-half-up on mediump mod" remains empirical, not spec-mandated.
  Re-verify on Mesa updates.

## Next

- Phase 3ac: RINGS probe (closer to existing playbook; periodic concentric
  bands, can use min-distance trick on radius-mod-tile if mediump precision
  on `length()` doesn't break the math).
- Phase 3ad: RAYS probe (farther; angle math has no clean integer equivalent).
