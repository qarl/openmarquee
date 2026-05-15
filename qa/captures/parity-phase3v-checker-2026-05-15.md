# Phase 3v: broad-tier checker — diag landed, fix deferred

**Date:** 2026-05-15
**Dispatch:** Same Phase-3l/3s/3t/3u playbook for parity_bg_pattern_checker.
**Probe:** `scripts/parity/checker_diag.py`
**Outputs:** `qa/captures/checker-{canvas2d,rust,diff,tile-crop}.png`,
`qa/captures/checker-diag-summary.json`
**Source change:** NONE — see "Fix attempted" + "Cause" sections.

## Picked fixture

`parity_bg_pattern_checker` (broad tier, ranked #2 candidate per
Phase 3u limitations list; step()-based shader analog to bricks).
Density=0.5 → curved d²=0.25 → tile=round(lerp(60,4,0.25))=46.

## Spatial decomposition (current state)

Per `bricks_diag.py`-style probe (`checker_diag.py`):

```
Max delta any-channel:   229
Mean delta any-channel:  9.286
  delta>= 10: 89,141 (4.30%)
  delta>= 50: 86,679 (4.18%)
  delta>=100: 83,659 (4.03%)
  delta>=200: 81,399 (3.93%)
```

Bimodal histogram: 95.66% pixels at delta<5 (interior match) and
3.93% at delta>=200 (full-color flips at tile boundaries). Almost
nothing intermediate — no AA-ring signature.

## Dominant cause: 1-pixel right+down shift in Rust render

Pixel-level probe revealed every Rust tile boundary is offset +1 px
right and +1 px down vs Canvas2D's integer-anchored
`ctx.fillRect(col*tile, row*tile, tile, tile)`:

```
Canvas2D row y=10:  blue→orange at x=46 (boundary AT x=46)
Rust    row y=10:  blue→orange at x=47 (boundary AT x=47)

Canvas2D col x=10:  blue→orange at y=46
Rust    col x=10:  blue→orange at y=47
```

Spacing between Rust transitions is exactly 46 px (matches tile=46),
so this is a global +1 pixel offset, NOT a tile-size mismatch.

## Cause is NOT in the shader

`FS_PATTERN_CHECKER` math, by spec:
```glsl
vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
float gx = floor(pos.x / u_tile);  // at pixel x=46: floor(46.5/46) = 1
```
Should give gx=1 at pixel x=46 (color_b orange). The actual golden
shows gx=0 (color_a blue) at x=46. Mismatch.

**Fix attempted:** rewrote shader to snap `floor(pos.x)` integer
before division, matching FS_PATTERN_BRICKS's pattern. Hypothesis:
mediump float precision loss on `(N*tile + 0.5) / tile` near 1.0.
Result: `render_tests.sh 45/45 PASS` — i.e., the new shader produces
**identical output** to the old one on Pi vc4. The +1 px shift is
**unaffected by the shader's divide vs floor-then-divide form**.

This rules out the shader as the cause. The shift must be in:

1. Rasterizer setup (glViewport / scissor offset by 1)
2. Framebuffer scanout (DRM-KMS plane offset)
3. glReadPixels / PNG-encode coordinate convention
4. The vertex shader / fullscreen-quad NDC mapping somehow shifting
   fragment positions by ~2/viewport-pixels

Phase 3l/3s/3t/3u all left bricks-shader-style (`floor(pos.x)`
first) divergence absent because their fixtures hide the shift
behind AA edges or sub-pixel mortar where 1-pixel offset isn't
visually loud. Checker is uniquely exposed: hard step at every
46-px boundary, no AA, every cell is the SAME color all the way
through, so a 1-pixel shift becomes a 1-pixel-wide grid of
full-color-flip mismatches.

## Risk callout

This is **NOT bricks/halftone-class** (single-shader fix). It's
likely a **pipeline-wide offset** that *also* shifts every other
pattern by 1 px, but only checker has the geometry to make it
visible at the parity-test level. If true:

- Fixing the pipeline shift would re-bless ~10+ goldens, not 1.
- The shift may have been baked into all current goldens since
  before Phase 3a, with Canvas2D parity captures the only evidence.
- Phase 3l stripes had a +7.56-px diagonal offset fix (5264c8f).
  That was a stripes-specific phase constant. A 1-pixel global
  rasterization shift would be a different mechanism — possibly
  the same root cause that prompted that fix, just rediscovered
  on a different fixture.

## Recommendation

Defer until next sub-attack with Pi-on-glass: add a glReadPixels-
based probe in the renderer that compares the actual rasterized
gl_FragCoord vs expected pixel coordinates. Or instrument the
shader to write gx into a debug framebuffer. Or test with a
checker fixture using density=1.0 (tile=4, max amplitude — would
show 50% mismatch if the shift exists at all tile sizes).

Filing as Phase 3w candidate alongside scanlines (mean=9.256),
which is the next-largest broad-tier mean. If scanlines turns out
to have the SAME global +1-pixel signature, that confirms the
pipeline-wide offset hypothesis and the fix becomes a single point
of rasterizer-setup investigation rather than per-shader.

## Limitations

- One fixture probed (parity_bg_pattern_checker).
- Local cargo test cannot reproduce — only the cross-built Pi
  binary exhibits the shift; render_tests.sh runs on Pi so the
  shift is visible in goldens but invisible to host-side unit
  tests.
- Fix attempted in shader (smoothstep-style floor-first) had zero
  effect on Pi rendering. Reverted, not committed.
