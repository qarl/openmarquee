# Phase 3k — pattern-shader stripes-bounce diagnostic

Date: 2026-05-15
Status: Findings-only commit. Two distinct causes localized; fix is
small-but-not-1-line, queued for daytime.
Prior: bde81b6 (Phase 3j text-quad pad fix shipped), 6762bec
(wifi_prefill audit close).

## TL;DR

`parity_animated_stripes_bounce` (worst SSIM in the gate at 0.5715)
has TWO distinct disagreement mechanisms, both clean to identify
from the data:

1. **Anti-aliasing at stripe boundaries** — Canvas2D's
   `repeating-linear-gradient` softens stripe edges over ~1-2px;
   Rust's `FS_PATTERN_STRIPES` uses `step()` for a hard boundary.
2. **Phase / anchor offset** — Canvas2D's 45deg gradient line is
   anchored at the gradient box's near-corner per CSS rules; Rust
   anchors at `(0,0)` per shader. At row=540 the boundary lands
   at x≈9 (Canvas2D) vs x≈22 (Rust) — a ~12px offset that
   recurs every 42px (the `tile` value).

Both colors agree byte-for-byte where the renderers are inside the
SAME stripe — fixture's `color_a=#5E1A1A → [94,26,26]` and
`color_b=#FFB43C → [255,180,60]` come out identical. The
disagreement is purely WHERE each stripe lives + HOW the boundary
between them is drawn.

## Numbers

(from `scripts/parity/stripes_diag.py` on
`renderer/tests/parity/captures/parity_animated_stripes_bounce.browser.png`
vs `renderer/tests/golden/animated_stripes_bounce.png`)

| metric                              | value                |
|-------------------------------------|---------------------:|
| image dims                          | 1920 × 1080          |
| per-channel max  R / G / B / A      | 161 / 229 / 229 / 0  |
| per-channel mean R / G / B / A      | 36.50 / 35.05 / 8.09 / 0.00 |
| delta_max overall / mean            | 229 / 36.75          |
| per-quadrant mean delta_max         | TL 36.39  TR 36.65  BL 36.72  BR 37.24 |
| histogram of delta_max              | 0 → 75.03%, 1-2 → 0.08%, 3-10 → 0.52%, 11-50 → 1.09%, 51-255 → **23.28%** |
| "loud" (>50) pixel bbox             | (0,0) → (1919,1079) — entire frame |

Per-quadrant means are uniform within 1 unit (36.4 / 36.7 / 36.7 /
37.2) — the disagreement is spatially **uniform across the whole
frame**, not localized to a corner. The 23% loud-pixel band is the
sum of all transition-zone + phase-shifted areas. Per-channel B
mean is much lower than R/G (8.1 vs 36.5 / 35.0) because the two
fixture colors share more B-axis distance than R/G axis.

## Row-mid samples (y=540, x stepped by 2, first 60 px)

```
   x:   Rust RGB         |  Canvas2D RGB
    0:  [94, 26, 26]    |  [94, 26, 26]   (both color_a)
    8:  [94, 26, 26]    |  [94, 26, 26]
   10:  [94, 26, 26]    |  [211, 138, 50]  <-- Canvas2D in AA transition
   12:  [94, 26, 26]    |  [255, 180, 60]  <-- Canvas2D crossed to color_b
   14..20: Rust still color_a, Canvas2D color_b
   22:  [255, 180, 60]  |  [255, 180, 60]  <-- Rust transition complete
   ...:  both color_b
   52:  [255, 180, 60]  |  [255, 180, 60]
   54:  [255, 180, 60]  |  [94, 26, 26]    <-- Canvas2D crossed back
   56-58: Rust still color_b, Canvas2D color_a
```

The transitions visibly walk in different positions: Canvas2D ~9
and ~53; Rust ~21. Period match (both are 42-tile, both wrap to
the same color), but offset and AA at boundaries diverge.

## Cause 1: `step()` vs CSS-AA at transition boundaries

**Rust** — `renderer/src/hdmi_logic.rs::FS_PATTERN_STRIPES`:

```glsl
float t = step(u_tile * 0.5, modv);
gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
```

`step()` is a hard 0-or-1 threshold; the boundary between stripes
is a single-pixel edge with no anti-aliasing.

**Canvas2D** — `ui/src/bg-system.js`:

```js
const tile = Math.round(lerp(80, 4, d));
const half = tile / 2;
return `repeating-linear-gradient(45deg, ${a} 0 ${half}px, ${b} ${half}px ${tile}px)`;
```

CSS `repeating-linear-gradient` with adjacent-stop hard ends should
in theory produce a hard edge too, but every browser softens
sub-pixel boundaries via image-rendering anti-aliasing. Canvas2D
ends up rendering ~1-2px ramped transitions between the two
colors.

**Fix (Cause 1)**: convert Rust's `step()` to `smoothstep()` with a
1-pixel-wide blend width:

```glsl
float t = smoothstep(u_tile * 0.5 - 0.5, u_tile * 0.5 + 0.5, modv);
```

~3-character change to one shader. Predicted impact: collapses the
transition-zone disagreement (probably half the 23% loud-pixel
mass).

## Cause 2: Gradient anchor / phase offset

CSS `repeating-linear-gradient(45deg, ...)`:

CSS-side: the gradient line at 45deg starts at the GRADIENT BOX's
bottom-left corner moving up-right (per CSS Image Values Level 3
§ 3.4.1). For a 1920×1080 box, the line origin is (0, 1080) in
image coords and runs along the (1, -1) direction. The pattern
"phase 0" lies on that line.

Rust shader: `proj = (pos.x + pos.y) / 1.41421356` where `pos.y` is
image-y (top-down). Phase 0 lies along the line where x + y = 0
— the top-left corner.

These two anchors are NOT the same. At image row 540, the
gradient-origin offset means Canvas2D's stripe phase = (offset
from 45deg-line through (0, 1080)) while Rust's stripe phase =
(offset from 45deg-line through (0, 0)). Difference = 1080 along
the y-axis, projected onto the 45deg axis = 1080 / sqrt(2) =
763.7 → mod 42 = 7.7. So Canvas2D's stripe boundaries SHOULD be
~7.7 px earlier along the diagonal than Rust's under a strict
top-left↔bottom-left anchor difference.

Observed offset is **~12 px** at row 540. The 4-px gap between
predicted 7.7 and observed 12 is too large to attribute to
float precision (sub-pixel at 1080p ≠ multi-px) — the geometric
attribution is not yet fully pinned down. Two refinements worth
checking before shipping any "drop the y-flip" fix:

(a) CSS 45deg gradient axis math for non-square boxes anchors at
    the gradient box's geometric center, not a literal corner —
    the apparent offset relative to top-left vs bottom-left
    might be shifted by W/2 or H/2 in some projection.
(b) Anti-aliasing at the boundary smears the apparent "transition
    x position" by 1-2 px on each side, which when stepped at 2-px
    granularity in the probe shows up as a 2-4 px ambiguity.

**The existence of a phase offset is solid**; the precise mechanism
is not. Phase 3l cheap follow-up: re-run the probe with TWO more
y rows (e.g., y=270, y=810). If the offset is linear in y, the
top-left↔bottom-left anchor model is right (just shift); if not,
the CSS-axis-center model wins (different fix shape).

**Fix candidate (Cause 2)** — DEFER until the multi-row probe
confirms the model. The naive "drop the y-flip" gets the
direction right but is likely to under- or over-correct without
the disambiguation data.

## What was added

- `scripts/parity/stripes_diag.py` — reusable pattern-shader probe
  (per-channel max + per-quadrant mean + histogram + row sampling
  + diff/sxs PNG generation). ~135 LOC.
- `qa/captures/stripes-diag.json` — probe output.
- `qa/captures/stripes-canvas2d.png`, `stripes-rust.png`,
  `stripes-diff.png`, `stripes-sxs.png` — visual artifacts.
- This findings doc.

No renderer source change shipped. Both candidate fixes (Cause 1
smoothstep and Cause 2 y-flip drop) are <5-line changes but
require:

1. cross-build + deploy to Pi
2. render_tests.sh re-bless (12 background_pattern fixtures + the 2
   animated_pattern fixtures all change geometry slightly)
3. parity_tests.sh measure
4. subagent review per the QA charter

Scope estimate: **small (≤4 hour slice)**, gated on qarl-direct OR
QA dispatch — touches every pattern fixture and the change has
visible-on-glass implications (smoothstep softens existing stripe
look; phase shift changes where each stripe lands).

## Suggested next slice

Three paths, in priority order:

1. **Phase 3l multi-row probe** (cheapest): re-run `stripes_diag.py`
   sampling y=270 + y=810 + y=540, compare offsets, decide between
   top-left-anchor vs CSS-center-anchor models. <30 min.
2. **Phase 3l fix** (after the multi-row probe): smoothstep on Cause
   1 + the correct anchor adjustment from probe (1). Predicted:
   parity_animated_stripes_bounce SSIM 0.57 → > 0.95, max_delta
   stays ~229 (single-pixel residual at boundaries from glyph AA),
   disagreement-pixel mass drops > 90%. Requires re-bless of ~12
   pattern fixtures. ≤4-hour slice.
3. **OR**: relax the gate's max_delta≤50 threshold to >=200 for
   single-pixel anti-alias edges. Phase 3j data + Phase 3k data
   both show the residual is at sub-pixel-positioning edges that
   are visually invisible. The gate is too strict for sub-pixel
   AA differences that don't matter on real-world displays.

Recommend (1) → (2) — the multi-row probe is cheap and prevents a
"naive drop-the-y-flip" fix from shipping with wrong geometry.
