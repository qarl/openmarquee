# Phase 3ad: RAYS diagnostic — AA-only / accepted-floor verdict

**Date:** 2026-05-15
**Dispatch:** Diagnostic-first; eyeball Pi vs Canvas2D BEFORE picking a fix
profile. Lesson from Phase 3ac (rings) was that committing to a precision
narrative on SSIM alone wastes a 3-candidate probe when the cause turns
out to be semantic.
**Status:** No source change committed. Diagnostic findings only.

## Verdict: AA-only / accepted floor

The Pi-on-glass golden (renderer/tests/golden/bg_pattern_rays.png) and the
Canvas2D reference (renderer/tests/parity/captures/parity_bg_pattern_rays.browser.png)
render **visually identical patterns**:
- Same ray count: 26 wedges (confirmed via `rays_uniforms(0.5).slices = 26`
  per the existing unit test; rays does not apply the density curve, so the
  fixture density=0.5 feeds directly into `2 * round(lerp(2, 24, 0.5)) = 26`).
- Same rotation: vertical seam at the top, alternating wedges clockwise.
- Same parity: color_a wedges in the same angular positions in both.
- Same hard-edge boundaries between adjacent wedges.

The only pixel-level difference is **anti-aliasing along ray edges**:
Canvas2D's `ctx.arc` + `ctx.fill` produces a soft sub-pixel-blended
boundary; the Rust GLES2 shader's `step()` produces a crisp hard
boundary. This is the canonical "AA-only" divergence category that
qarl's "AA-accept-first" rule explicitly accepts.

## Parity metrics (parity_tests.sh, current HEAD = d023488)

| Metric        | Value    | Gate    | Result |
|---------------|---------:|---------|--------|
| SSIM          | 0.9935   | >= 0.95 | **PASS** |
| mean_delta    | 0.308    | low is good | PASS  |
| pct >= 10     | 0.54%    | low is good | PASS  |
| max_delta     | 229      | <= 50   | FAIL (Cause B text-glyph floor — same across all 7 prior shipped fixtures) |

The "FAIL" line in parity_tests.sh comes only from the max_delta cap,
which is the documented Cause B floor at the "RAYS" text glyph (all
shipped fixtures hit this same max=229; it's a global property of the
text raster, not a per-shader issue).

SSIM=0.9935 is **better than** the post-fix rings result (0.9744 after
the Phase 3ac thin-rings ship). RAYS does not need a shader rewrite.

## Comparison to prior categories

| Phase | Pattern | SSIM pre  | SSIM post | Verdict category |
|-------|---------|----------:|----------:|------------------|
| 3w/3x | scanlines | ~0.70  | 0.9933 | precision (Cand B) |
| 3z    | checker  | 0.9054  | 0.9960  | precision (Cand E int-domain) |
| 3aa   | grid     | 0.9080  | 0.9986  | precision (Cand B hybrid) |
| 3ab   | scanlines audit | n/a | 0.9933 | precision generalization |
| 3ac   | rings    | 0.7146  | 0.9744  | **SEMANTIC** (Option C honor-docstring) |
| 3ad   | rays     | **0.9935** | n/a  | **AA-only / accepted floor** |

RAYS' SSIM at HEAD already sits between rings-post-fix (0.9744) and
grid-post-fix (0.9986). It's effectively a shipped-quality pattern.

## Math sanity check (confirmation, not investigation)

The dispatch noted I had pre-assessed `bg-system.js:383-399` (Canvas2D)
and `hdmi_logic.rs:2417-2433` (Rust) as "compatible math". Visual
inspection confirms this in practice — both produce the same wedge
geometry. The atan2 + slice-index + parity check is small-magnitude
arithmetic per pixel (atan2 returns [-pi, pi]; normalized to [0, 1);
multiplied by `slices` <= 48; floor + mod-2 yields 0 or 1). No
operand ever crosses mediump's range concerns. This is fundamentally
different from the rings `length()`-at-1.2M case (which turned out
not to matter empirically either, but at least had a plausible
precision worry to test).

## Recommended action

**Mark as accepted floor.** No shader change, no uniform change, no
re-bless. The current golden is correct; the Canvas2D vs Pi delta is
entirely AA fringe at hard wedge edges, which qarl's rules accept.

## Confetti forecast

Quick eyeball of confetti (out of scope, just answering the forecast
ask): the Pi golden and Canvas2D reference both show similar dot
density and similar dot sizes, but **individual dot positions differ**
across the frame. This looks like a per-cell RNG seed/position
divergence — same algorithm sketch on both sides, mismatched per-cell
randomness output.

This is NOT Phase 3ac-style "different algorithm" semantic divergence
(both sides clearly do the same cell-grid-with-jitter approach).
Closer to Phase 3o/3p "byte-compare divergence" category — likely
addressable via a structural-identity audit on the seed derivation
(cf. user memory `[[feedback_structural_identity_for_seeds.md]]`).
Could be a 1-line fix once the seed-derivation chain is matched, or
a multi-stop audit if the cells use different orderings.

SSIM=0.9400 mean=3.087 pct>=10=2.90% — borderline category, but the
visual distinguishability between Pi and Canvas2D is greater than for
rays.

## Limitations

- Visual inspection done at thumbnail-scale via the conversation image
  viewer. Per-pixel pixel-coordinate-level verification not performed
  (relying on SSIM=0.9935 and the convergent visual identity).
- Confetti forecast based on a single side-by-side look; not a
  diagnostic. Phase 3ae will need its own diagnostic-first slice.

## Next

- Phase 3ad: complete (this slice).
- Phase 3ae forecast: confetti per-cell-RNG audit (likely structural-
  identity territory, not precision and not Phase 3ac-style semantic).
