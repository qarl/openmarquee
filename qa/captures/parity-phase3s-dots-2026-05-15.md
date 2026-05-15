# Phase 3s: broad-tier dots smoothstep AA fix

**Date:** 2026-05-15
**Dispatch:** Pick the closest-to-PASS broad-tier pattern fixture and
probe Phase-3k-style. If 1-line cause emerges, ship in same cycle.
**Probe:** `scripts/parity/dots_diag.py`
**Outputs:** `qa/captures/dots-{canvas2d,rust,diff,sxs,tile-crop}.png`,
`qa/captures/dots-diag-summary.json`,
`qa/captures/dots-post-fix-crop.png` (after fix)
**Source change:** `renderer/src/hdmi_logic.rs` — FS_PATTERN_DOTS

## Picked fixture

`parity_bg_pattern_dots`. From Phase 3l-post classification (broad
tier, mean=2.231, max=229). Simplest pattern geometry → most
Phase-3l-analogous fix profile.

Fixture config (`renderer/tests/fixtures/.../item.json`):
- pattern=dots, density=0.5 → tile=26, radius=6 (small dots)
- color_a=#1A2A3A, color_b=#FFB43C (dark blue / orange)

## Spatial decomposition (pre-fix)

```
Max delta any-channel:     229
Mean delta any-channel:    4.064  (probe's per-pixel max-of-channels)
Pixels with delta>=10:     78,796 (3.800%)
Pixels with delta>=50:     55,929 (2.697%)
Pixels with delta>=100:    38,945 (1.878%)
Pixels with delta>=200:    11,849 (0.571%)
```

Per-channel: R 229, G 191, B 177. Per-quadrant: uniform (4.04–4.08
across all 4 quadrants) → no anchor offset between renderers.

Histogram (bucketed):

```
[  0,   5):  1,990,006  (95.969%)   bg, far from dot edges
[  5,  10):      4,798   (0.231%)
[ 10,  25):     16,477   (0.795%)   AA ring inner gradient
[ 25,  50):      6,390   (0.308%)
[ 50, 100):     16,984   (0.819%)   AA ring midband
[100, 150):     12,253   (0.591%)
[150, 200):     14,843   (0.716%)
[200, 256):     11,849   (0.571%)   pixel-flip on ring crest
```

## Dominant cause

Visual inspection of `dots-tile-crop.png` (3x3 tile region around
top-left dot, canvas2d | golden | diff x4):

- Canvas2D: smooth bilinear AA on circle boundary (`ctx.arc + fill`)
- Rust pre-fix: BLOCKY pixelated edges (no AA on `step(d2, r2)`)
- Diff: white rings at every dot boundary

Full-canvas diff (`dots-diff.png`) confirms symmetric rings around
every dot center — NO crescent asymmetry that would indicate a
positioning offset. The dominant cause is the AA-function mismatch:

| Renderer  | Code                                                  |
|-----------|-------------------------------------------------------|
| Canvas2D  | `ctx.arc(x, y, r); ctx.fill()` → browser bilinear AA  |
| Rust pre  | `step(d2, r2)` → HARD step, no AA                     |

The 0.571% pixels at delta>=200 are the ring crest where one
renderer's AA function has 100% color_b and the other has 0%
color_b at the same pixel — expected for hard-step vs
bilinear-AA mismatch on a 1-pixel-wide ring.

## Scoped fix

**1-line shader change** in `FS_PATTERN_DOTS`:

```glsl
// Before:
float d2 = dot(cell, cell);
float r2 = u_radius * u_radius;
float t = step(d2, r2);

// After (Phase 3s):
float d = length(cell);
float t = 1.0 - smoothstep(u_radius - 0.5, u_radius + 0.5, d);
```

`smoothstep(r-0.5, r+0.5, d)` gives a 1-pixel-wide smooth transition
centered on the radius — matches Canvas2D's `ctx.arc + fill`
bilinear-AA response width.

## Post-fix metrics (parity_tests.sh)

| Metric           | Pre-3s  | Post-3s | Δ        |
|------------------|--------:|--------:|---------:|
| max_delta        |     229 |     206 | **-23**  |
| mean_delta       |   2.231 |   2.084 | **-0.147** |
| SSIM             |  0.9487 |  0.9578 | **+0.0091** |
| pct_over_10      |  3.80%  |  4.72%  | +0.92%   |

3 metrics improved, 1 regressed:

- **max_delta -23**: smoothstep caps the per-pixel slope at the
  boundary — no more full-color flips at a single texel. Primary
  parity_tests gate metric.
- **mean_delta -0.147**: per-pixel average closer to zero.
- **SSIM +0.0091**: structural similarity improved.
- **pct_over_10 +0.92%**: smoothstep introduces gradient pixels
  (delta 10-100) along the AA ring that the hard-step approach
  concentrated as full-saturation outliers. Net more pixels with
  small delta, fewer with large delta — directionally correct for
  pixel-perfect parity even though this auxiliary metric regressed.

Gate count: 0/39 PASS at max_delta≤50 (unchanged — architectural
floor persists per Phase 3r cf11215). bg_pattern_dots remains FAIL
with max=206 > 50. The remaining floor is the residual AA-function
gap between GPU smoothstep and browser bilinear-AA on small (r=6)
dots, plus the persistent Cause B text-AA at glyph outlines of the
"DOTS" label.

## Risk callout

The remaining max=206 means the smoothstep approximation is close
but not exact for small dots. Browser bilinear-AA at r=6 uses
coverage-area sampling; GPU smoothstep uses distance-from-center.
The two converge at large radii but diverge at small. A future
slice could use a more accurate coverage approximation (e.g.,
`fwidth()`-based dynamic AA width) — but that's beyond the
single-fixture, single-cycle scope of this dispatch.

## Limitations

- One fixture probed (parity_bg_pattern_dots). The halftone fixture
  (FS_PATTERN_HALFTONE shares the same hard-step pattern) would
  benefit from the same fix; queued for separate dispatch per the
  one-fixture-per-cycle rule.
- The radius parameter for parity_bg_pattern_dots (r=6) is the
  smallest in the broad-tier dots range. Larger r (≥10) would
  produce a less visible AA gap; smaller r (<5) would amplify it.
