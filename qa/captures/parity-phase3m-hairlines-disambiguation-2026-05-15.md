# Phase 3m: hairlines-tier sub-cause disambiguation

**Date:** 2026-05-15
**Probe:** Phase 3l-post left an open question — is the 197/214/221/
223/229/231 quantization in the hairlines-tier max_delta a sign of
one mechanism with per-fixture coverage variance, or 2–3 distinct
sub-causes? Phase 3m answers it by visualizing the loud-pixel
locations.
**Inputs:** browser captures from `renderer/tests/parity/captures/`,
goldens from `renderer/tests/golden/`
**Outputs:** `qa/captures/hairlines-{diff,loud,overlay}-*.png`,
`qa/captures/hairlines-diff-summary.json`
**Script:** `scripts/parity/hairlines_diff_probe.py`

## Headline

**One mechanism.** Loud-pixel bounding boxes for all three target
fixtures share the same vertical band — exactly the text-layer box.

## Data

| Fixture                 | text     | loud px | bbox (l,t,r,b)               | max_delta |
|-------------------------|----------|---------|------------------------------|-----------|
| bg_pattern_gradient     | GRADIENT |  7,291  | (538, **432**, 1387, **648**)| 197       |
| bg_pattern_solid        | SOLID    |  2,093  | (710, **432**, 1207, **648**)| 221       |
| font_inter              | INTER    |  3,084  | (581, **432**, 1384, **648**)| 231       |

All three text boxes per `item.json`: `box.y=0.4`, `box.h=0.2` on a
1080-tall canvas → text region = y ∈ [432, 648]. Observed loud bbox
is **fully contained** in the text region. Zero bg-pattern loud
pixels — i.e., Phase 3l's pattern-shader fix is holding, and the
gradient/solid bg rasters are within threshold everywhere outside
the text band.

## Interpretation

The hairlines-tier max_delta band 197–231 is **single-mechanism
text-AA divergence at glyph edges**, modulated by per-fixture
glyph-coverage variance:

- **solid** (5 chars, simple geometry) → fewest loud edges, max=221
- **font_inter** (5 chars in Inter font) → 3084 loud edges, max=231
- **gradient** (8 chars) → most loud edges (7291), max=197

The inverse correlation between loud_px count and max_delta is
expected: max_delta is a single-pixel extreme, not a count. The
worst pixel happens at whichever glyph edge has the worst sub-pixel
mismatch — independent of how many other edges are over threshold.

## Revised Phase 3l-post verdict

Phase 3l-post called the 197–231 spread "wide enough to argue a
single dominant mechanism … though the quantization could indicate a
few related sub-causes". **Phase 3m closes that ambiguity in favor
of one mechanism**: spatially co-located loud pixels, all within the
text-layer bbox, across three structurally different bg situations
(pattern, gradient, font-spotlight).

## Implication for Cause B

The 11 hairlines-tier fixtures from Phase 3l-post all share this
single mechanism. **One repair of the Canvas2D-WASM ↔ Rust fontdue
glyph-AA divergence should sweep all 11 fixtures into PASS.** The
Cause B arc is now unambiguously the next attack target.

Remaining open questions for the Cause B repair itself (defer to
that arc, not Phase 3m):

- Is the glyph-AA divergence a coverage-table difference, a
  sub-pixel positioning difference, or both?
- Does it appear at all sizes or only at squish-target sizes (where
  the Rust path rasterizes at the canvas-pixel dim after Phase 3f
  bde81b6)?
- Is `parity_text_static` (the canonical large-text fixture, mean
  10.67, structural tier) repaired by the same fix, or does it
  carry an additional layout-level divergence?

## Limitations

- Three fixtures is a small sample; the visual conclusion is strong
  only because all three boxes land on the same band. If a single
  outlier had loud pixels elsewhere it would reopen the
  multi-mechanism question.
- "Same vertical band" doesn't prove the per-pixel deltas come from
  the same glyph-AA edge function — it proves they're in the same
  text region. A future spot-check could overlay the loud mask on
  the actual rasterized text and confirm pixels sit precisely on
  glyph edges (not on baseline / lineHeight off-by-one bands).
