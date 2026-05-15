# Phase 3w: broad-tier scanlines — bigger bug than expected

**Date:** 2026-05-15
**Dispatch:** Same Phase-3l/3s/3t/3u playbook for parity_bg_pattern_scanlines.
**Probe:** `scripts/parity/scanlines_diag.py`
**Outputs:** `qa/captures/scanlines-{canvas2d,rust,diff,tile-crop}.png`,
`qa/captures/scanlines-diag-summary.json`
**Source change:** NONE — fix attempted (highp), no output delta. See
"Fix attempted".

## Picked fixture

`parity_bg_pattern_scanlines` (broad tier, mean=9.256 — largest
broad-tier outstanding). Density=0.5 → curved 0.25 → tile=13.

## Cause: Rust renders ONLY the y=0 scanline; all others are missing

Pixel-level probe (column x=10):
```
Y positions where Canvas2D has color_b: [0, 13, 26, 39, 52, 65, 78,
    91, 104, 117, 130, 143, 156, 169, 182, 195, ...]
Y positions where Rust     has color_b: [0]
```

Visual: `qa/captures/scanlines-rust.png` is essentially a solid blue
field (color_a) with a single thin orange line at y=0 and the
"SCANLINES" text overlay. Canvas2D shows a proper scanline pattern.
This is **not** a parity-style 1px offset; the Rust render is
straight-up broken for this pattern.

## Theory: Y-axis precision loss at large gl_FragCoord.y

Shader at pixel y=13 (CSS y, top-origin):
```
gl_FragCoord.y_expected = u_viewport.y - 13 - 0.5 = 1066.5
pos.y_expected          = u_viewport.y - gl_FragCoord.y = 13.5
row_expected            = floor(13.5) = 13
mod(13, 13)             = 0
step(0, 0.5)            = 1  → color_b (scanline)
```

In mediump float at magnitude ~1066, granularity is 1.0 (only
integers representable). 1066.5 quantizes to 1066.0 or 1067.0
depending on round-to-nearest-even direction. If 1066.0:
```
pos.y = 1080 - 1066 = 14.0
row   = 14
mod(14, 13) = 1
step(1, 0.5) = 0  → color_a (no scanline). BUG matches golden.
```

Why y=0 still renders: gl_FragCoord.y at pixel 0 is 1079.5. Nearest
representable mediump values are 1079.0 and 1080.0. Round-to-even
selects 1080.0 → pos.y = 0 → row=0 → scanline. ✓

So the bug is sensitive to round-to-nearest direction at each pixel,
and only y=0 happens to round in the right direction at the
density=0.5 tile-stride pattern.

## Fix attempted: `precision highp float` in FS_PATTERN_SCANLINES

Bumped the fragment-shader precision declaration to highp. cargo
test 455/455 (after widening the precision-check assertion in
pattern_shaders_have_gles2_preamble to accept either mediump or
highp), cross-build green, render_tests.sh **45/45 PASS** —
meaning the new shader produces **identical output** to the old.
The buggy golden remains buggy under highp.

Per GLES2 spec, highp is **optional** in fragment shaders. Pi vc4
likely silently downgrades highp→mediump (or the highp annotation
isn't being honored for `gl_FragCoord` which is built-in). Either
way: `precision highp float;` is not a working fix on this hardware.

Reverted. Not committed.

## Mechanistic implication

If the y-axis precision loss theory is right, the same bug should
affect ALL fragment shaders that compute `u_viewport.y -
gl_FragCoord.y` at small tile sizes. So far ONLY scanlines is
visibly broken because:
- Scanlines is the only single-pixel-tall periodic pattern with
  small tile (13) and ZERO AA tolerance.
- Other patterns (dots/halftone) have AA-ring tolerance that masks
  ~1-px y-shifts; bricks/checker use larger tile sizes (39, 46)
  where precision granularity is more forgiving.
- Grid (1-px lines too) might exhibit a similar bug — check next.

## Recommendation: defer to a "fix all patterns at once" sub-attack

Three fix candidates that need Pi-on-glass testing:

1. **Replace `u_viewport.y - gl_FragCoord.y` with int math**:
   ```glsl
   int vy = int(u_viewport.y);
   int fy = int(gl_FragCoord.y);
   float row = float(vy - fy - 1);
   ```
   GLES2 int precision is min 16-bit (lowp int), so vy=1080 fits;
   fy at pixel 13 should be exactly 1066 (int truncation of 1066.5).
   Resulting row computed in pure integer space — no float subtraction.

2. **Y-flip on CPU side via uniform**: pre-compute the y-flip
   factor and pass a vec2 multiplier `(1, -1)` plus an offset
   uniform; avoid the large subtraction entirely.

3. **Use `gl_PointCoord` or a vertex-passed UV** for the y axis.
   Compute uv in the vertex shader (highp by default) and pass to
   fragment shader as varying — varyings preserve precision across
   the rasterizer/fragment boundary.

Approach 1 is the smallest change. But all 3 need Pi-on-glass
verification because the Phase 3w highp attempt proved that the
precision behavior is harder to reason about than the spec implies.

## Combined with Phase 3v

Phase 3v (checker, 1px right+down shift) and Phase 3w (scanlines,
missing rows after y=0) likely share the same root: mediump
precision loss in coordinate math at large viewport magnitudes.

If a single Phase 3x fix (e.g. int-domain coordinate computation
in all FS_PATTERN_* shaders) resolves both, the broad-tier landscape
could collapse significantly. SSIM gains could be 3-5x larger than
Phase 3l/3s/3t/3u's individual fixes.

This is now worth one consolidated `qarl-direct` decision: do we
do the per-shader refactor now (likely re-bless ~10 fixtures), or
defer to a release-candidate cleanup pass?

## Limitations

- One fixture probed (parity_bg_pattern_scanlines).
- The "Y-axis precision loss" theory is consistent with the
  observed missing-scanlines-after-y=0 behavior but NOT proven by
  in-shader instrumentation. The highp non-fix doesn't disprove
  it (since vc4 may not honor highp).
- Phase 3v + 3w are 2 consecutive no-fix slices. Per the playbook
  precedent (3l/3s/3t/3u each shipped a 1-line fix), this slice
  pattern has clearly broken — recommendation is to pause the
  per-fixture cycle and call qarl-direct for the broader strategy.
