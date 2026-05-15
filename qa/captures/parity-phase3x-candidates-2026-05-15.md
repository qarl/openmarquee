# Phase 3x: 3-candidate Pi-on-glass probe for FS_PATTERN_SCANLINES

**Date:** 2026-05-15
**Dispatch:** Probe before consolidated refactor. Phase-1c discipline:
get data on which of the 3 hypothesized fixes actually moves pixels
on vc4 mediump before committing to a ~10-shader rollout.
**Captures:** qa/captures/phase3x-{baseline,cand-A,cand-B,cand-C}.png

## Candidates (per Phase 3w findings doc)

- **A: Int-domain math** — cast to int early, integer mod, cast back.
  Tests if precision loss is in the float mod itself.
- **B: CPU-side y-flip uniform** — drop the `u_viewport.y -
  gl_FragCoord.y` subtraction; pass a precomputed y_phase uniform
  and do `mod(gl_FragCoord.y, tile)` directly. Tests if the
  subtraction at large magnitude is where precision dies.
- **C: Vertex-passed UV varying** — interpolate flipped y via a
  varying from a custom vertex shader (highp guaranteed in VS).
  Tests if VS→FS interpolation preserves precision.

## Methodology

Per candidate: edit FS_PATTERN_SCANLINES (+ supporting CPU/VS code),
cargo test 455/455, cross-build for Pi via
`scripts/renderer_cross_build.sh`, run `scripts/render_tests.sh`
(deploys binary to dev Pi @ openMarqueeDev and captures render
output). Copy `renderer/tests/captures/bg_pattern_scanlines.png`
to `qa/captures/phase3x-cand-{A,B,C}.png`. Revert source between
candidates. Sample pixel-level Y-transitions at column x=10 in
each capture vs Canvas2D reference.

## Results

| Candidate | mean_delta | max | pct≥200 | Scanline Y positions (x=10) | Visual: lines back? |
|-----------|-----------:|----:|--------:|------------------------------|---------------------|
| Baseline  | 16.894     | 229 | 7.207%  | [0]                          | NO — only y=0       |
| **A: int**| 33.314     | 229 | 14.376% | [0, 1, 14, 27, 40, 53, ...]  | YES but +1 px shift |
| **B: y-phase** | **0.477** | 229 | **0.036%** | [0, 13, 26, 39, 52, 65, ...] | **YES exact match** |
| C: VS UV  | 16.894     | 229 | 7.207%  | [] (empty!)                  | NO — no scanlines   |

Canvas2D reference: [0, 13, 26, 39, 52, 65, 78, ...].

## Verdict: **Candidate B wins decisively**

Cand B's pixel-level Y positions are an EXACT match to Canvas2D
(spacing 13, anchored at y=0). mean_delta drops from 16.894 to
0.477 — a 97% reduction. SSIM crosses the parity-gate threshold:
**SSIM=0.9933** (was ~0.7), gating-criterion of ≥0.95 satisfied.

The residual max=229 / pct≥200=0.036% is the Cause B architectural
floor at the "SCANLINES" text glyph AA, unrelated to the pattern.

## Why each candidate behaved as it did

### Cand A: scanlines back, but +1 px shift bug surfaces

`int(gl_FragCoord.y)` truncates 1066.5 → 1066. Row math
`viewport_h - 1 - y_bot` = 1080 - 1 - 1066 = 13. mod(13, 13) = 0,
so the scanline DOES appear at y_top=13. The +1 px shift is the
same bug visible in Phase 3v checker — once precision is fixed,
the underlying pixel-coord-convention mismatch with Canvas2D
surfaces. Worse mean_delta than baseline because misaligned
scanlines diff against ALL of Canvas2D's scanlines, not just the
y=0 one.

### Cand B: math is precision-tolerant

`mod(gl_FragCoord.y, u_tile)` operates on quantized integer
gl_FragCoord.y. If vc4 truncates 1066.5 → 1066, m = mod(1066, 13)
= 0. u_y_phase = mod(1079.5, 13) = 0.5. `abs(0 - 0.5) = 0.5`,
`step(0.5, 0.5) = 1` → color_b ✓. If vc4 happened to round UP to
1067 instead, m = 1, `abs(1 - 0.5) = 0.5`, same result ✓. The
±0.5 tolerance window catches both possible truncation outcomes
at every pixel center. This is precisely WHY it works where
Cand A's exact-equality test would fail at the +1 boundary.

### Cand C: VS UV varying gets clobbered by FS mediump

`v_uv = a_pos * 0.5 + 0.5` in the vertex shader yields uv.y in
[0, 1] at highp. But once interpolated into the fragment shader
declared as `precision mediump float`, the varying's per-pixel
value gets quantized. `v_uv.y * u_viewport.y` at pixel y=13
should be (13.5 / 1080) * 1080 ≈ 13.5, but mediump precision on
the multiplication at magnitude ~1080 introduces enough error
that the result quantizes to 14 (or similar), shifting the
scanline check off by 1 pixel — and step()=0 at every scanline
row, so NO scanlines appear. Confirms that the precision issue
is genuinely fragment-shader-side and a highp-in-VS handoff
doesn't survive the rasterizer interpolation under default
mediump fragment precision.

## Decision tree (per dispatch)

**One candidate is a clear winner on Pi** → Phase 3y ships the
winning approach consolidated. Status here: Cand B applied to
scanlines in same cycle (Phase 3x ships scanlines fix; Phase 3y
scope below).

## Phase 3y consolidated rollout scope

The "y-phase precompute + mod against gl_FragCoord.y" pattern
generalizes to any shader using `u_viewport.y - gl_FragCoord.y`
where the resulting `pos.y` then goes through `mod` or `floor`
math. Affected shaders (per FS_PATTERN_* grep):

- FS_PATTERN_STRIPES — uses `(pos.x + pos.y) / sqrt(2)` so the
  Cand B mod-direct pattern doesn't directly apply. Already fixed
  in Phase 3l via diagonal phase offset (f2896f7).
- FS_PATTERN_CHECKER — has the +1 px shift bug (Phase 3v).
  Cand B-style precompute would need x_phase AND y_phase. Likely
  fixable; one more probe cycle.
- FS_PATTERN_DOTS — uses `mod(pos, u_tile) - vec2(u_tile*0.5)`.
  Cand B mod-direct would replace pos with gl_FragCoord directly
  plus 2 phase uniforms.
- FS_PATTERN_HALFTONE — same shape as dots; same approach.
- FS_PATTERN_GRID — uses `mod(pos.y, tile)` and `mod(pos.x, tile)`,
  prime Cand B candidate.
- FS_PATTERN_RINGS / FS_PATTERN_RAYS — use distance from center;
  the magnitude-loss problem applies; Cand B-style would need
  center-coord uniforms precomputed CPU-side.
- FS_PATTERN_BRICKS — uses `floor(pos.x)`, `floor(pos.y)`. Phase 3u
  already partially fixed the per-uniform `half` math. Bricks
  works on Pi because the floor-snap absorbs the precision loss.
- FS_PATTERN_CONFETTI — cell-based; likely independent.

**Projected re-bless count: 6-7 fixtures** (checker, dots,
halftone, grid, rings, rays, scanlines re-blessed in this slice).

**Recommend Phase 3y dispatch** to do checker first (since its
+1 px shift is the visible analog to Cand A here) — it's a great
proof-of-concept for whether the Cand B pattern generalizes
beyond scanlines.

## Risk callout

- Cand B's u_y_phase uniform is mode_h-dependent. Computed CPU-side
  per-draw via `((mode_h - 0.5) % tile + tile) % tile`. Defensive
  double-mod handles negative-mod edge case if mode_h were ever
  smaller than tile (shouldn't happen but free safety).
- The ±0.5 step tolerance assumes scanlines are 1 px thick. If
  Canvas2D ever switches to 2 px scanlines, tolerance would need
  to bump to 1.0.
- vc4 truncation behavior of gl_FragCoord.y is empirical, not
  spec-mandated. If a future Pi Mesa update rounds differently,
  the ±0.5 window might miss some scanlines. Re-test on Mesa
  upgrade.

## Limitations

- Single fixture (scanlines). Cand B's generalizability to other
  shaders is hypothesized in the Phase 3y scope but not yet
  verified per-shader.
- No isolation between probe iterations — `renderer/tests/captures/`
  is overwritten each render_tests run, captures persisted via
  /tmp before revert.
- Did not test highp-on-int variants (precision highp int) which
  could also be a Cand-A-flavor fix without the int truncation
  bug. Deferred; Cand B already meets the gate.
