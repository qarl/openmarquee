# Phase 3ai: hybrid — Canvas2D breathe/pulse/bounce aligned to Rust/spec; ticker+shake+bounce-shape reclassified

**Date:** 2026-05-15
**Dispatch:** qarl's Option 3 (Hybrid) from the Phase 3ah (c093f87)
product-shape question. Tighten unintentional motion-formula drift
between Canvas2D and Rust; reclassify intentional structural
divergences as expected-divergent in the parity harness, mirroring
the confetti by-design RNG-family treatment.
**Status:** Source change + harness reclassification SHIPPED.
Closing 9 motion-fixture FAILs cleanly (8 via reclassification, 1
remaining FAIL via partial Canvas2D alignment).

## Canonical-intent decision

Phase 3ah named two formula drifts (pulse trough + breathe amp
baseline). It also implied Rust was the outlier. **Spec doc
`docs/text-layer-motion-spec.md:225-235` says the opposite:** Rust
formulas match the spec; Canvas2D + Python drifted.

| Effect  | Spec range (i=0 → i=100)         | Rust formula                          | Canvas2D OLD formula                  | Match? |
|---------|----------------------------------|---------------------------------------|---------------------------------------|--------|
| breathe | ±2% → ±20%                       | `amp = 0.02 + 0.18 * intensity_norm`  | `amp = (intensity/100) * 0.20`        | Rust ✓ |
| pulse   | 70-100% shallow → 0-100% deep    | `min_alpha = 0.70 * (1 - intensity_norm)` | `minA = 1 - intensity/100`         | Rust ✓ |
| bounce  | ±1% → ±10%                       | `amp = 0.01 + 0.09 * intensity_norm`  | `amp = (intensity/100) * 0.10`        | Rust ✓ |

Canvas2D was missing the small-amplitude baseline at intensity=0
for all three effects. Aligning Canvas2D to Rust closes the
unintentional-drift bucket.

## Source change: Canvas2D aligned to Rust/spec

`ui/src/canvas-motion.js`:
- **breathe** (lines 132-150): `amp = 0.02 + 0.18 * intensityNorm`.
  Adds the ±2% baseline at intensity=0 (was 0).
- **pulse** (lines 151-167): `minA = 0.70 * (1.0 - intensityNorm)`.
  Adds the 70-100% shallow sweep at intensity=0 (was alpha=1
  constant, no pulse at all).
- **bounce** (lines 168-184): `amp = 0.01 + 0.09 * intensityNorm`.
  Adds the ±1% baseline at intensity=0 (was 0).

Bounce SHAPE stays as `sin(2π·phase)` on Canvas2D — symmetric. Rust
uses `abs(sin)` for true ball-on-floor per qarl 2026-05-03 device
decision. The spec's Q3 lock at `docs/text-layer-motion-spec.md:203`
explicitly accepts approximate editor preview, so shape divergence
is by design.

Rust formulas at `renderer/src/hdmi_logic.rs:3454, :3468, :3489`
unchanged — they already match spec.

## Harness change: 9 fixtures reclassified divergent_by_design

`scripts/parity/fixtures.json`:
- `parity_motion_ticker` — Rust ticker right-edge-start single-copy
  NDC offset vs Canvas2D/Python wrap-around two-copy np.roll. Both
  correct for their context (device vs editor preview).
- `parity_motion_compound` — uses shake (different RNG families:
  FNV-1a vs splitmix64) and bounce (abs(sin) vs sin shape).
- `parity_animated_uncage` — shake structural divergence (RNG
  families).
- `parity_transition_fade/cut/wipe/slide/scroll/pixelate` — all
  share the same TO slide (2c858968) with 5 ticker-motion layers;
  ticker divergence flows through every transition's AB compositor.

`scripts/parity/run.py`:
- `report()` now honors `divergent_by_design: true`, marks fixture
  as `EXPECTED-DIVERGENT`, bypasses the SSIM/mean gate, still
  computes + prints metrics for visibility.

## Before-after metrics (Pi-side, full parity harness)

| Fixture                              | Pre-3ai SSIM    | Post-3ai      | Δ          | Verdict          |
|--------------------------------------|-----------------|---------------|------------|------------------|
| parity_motion_ticker                 | 0.6151 FAIL     | 0.6151        | —          | EXPECTED-DIVERGENT |
| parity_motion_compound               | 0.6598 FAIL     | 0.6635        | +0.0037    | EXPECTED-DIVERGENT |
| parity_animated_uncage               | 0.8341 FAIL     | 0.8341        | —          | EXPECTED-DIVERGENT |
| parity_transition_fade               | 0.7394 FAIL     | 0.7394        | —          | EXPECTED-DIVERGENT |
| parity_transition_cut                | 0.6295 FAIL     | 0.6295        | —          | EXPECTED-DIVERGENT |
| parity_transition_wipe               | 0.8151 FAIL     | 0.8151        | —          | EXPECTED-DIVERGENT |
| parity_transition_slide              | 0.8158 FAIL     | 0.8154        | -0.0004    | EXPECTED-DIVERGENT |
| parity_transition_scroll             | 0.7957 FAIL     | 0.7957        | —          | EXPECTED-DIVERGENT |
| parity_transition_pixelate           | 0.7610 FAIL     | 0.7610        | —          | EXPECTED-DIVERGENT |
| parity_animated_halftone_pulse       | 0.9161 FAIL     | 0.9171        | +0.0010    | **FAIL** (-0.0029) |
| parity_animated_stripes_bounce       | 0.9894 PASS     | 0.9894        | —          | PASS              |

10 broad-tier pattern fixtures + Phase 3ag halftone Cand-B + Phase
3x scanlines + Phase 3aa grid all unchanged.

## Why animated_halftone_pulse still FAILs

The dispatch target was: "clears 0.92 gate." Did NOT clear. Margin
went from -0.0039 to -0.0029 (+0.0010 SSIM improvement). The Phase
3ah estimate of +0.04-0.06 was over-optimistic.

Why smaller than predicted: the fixture captures at tick=0.5 of a
1Hz pulse → phase=0.5 → sin01=(sin(π)+1)/2=0.5. At sin01=0.5 (mid-
cycle, NOT the trough):
- OLD Canvas2D: a = 0.5 + 0.5*0.5 = 0.75
- NEW Canvas2D: a = 0.35 + 0.65*0.5 = 0.675
- Difference: 0.075 alpha (NOT the 0.15 trough-difference Phase 3ah
  cited).

The 0.15 figure was the WORST-CASE at phase=0.75 (the trough). At
tick=0.5 the realized phase is 0.5 and the alpha delta is half the
worst-case.

Additionally, the fixture's text layer covers ~20% of the canvas, so
even the 0.075 alpha-delta only affects ~20% of pixels.

The remaining ~0.0029 below gate appears to be a mix of:
- Cause B text-AA floor (max_delta=205 in the diff suggests text-
  glyph rasterization differences — same arc as the parity_font_*
  fixtures which are FAILing at SSIM 0.91-0.93 with the same root
  cause; out of scope per dispatch, tracked at task #121 fontdue-
  WASM).
- Halftone bg parity floor (SSIM 0.9267, from Phase 3ag).

A future text-parity slice (task #121) closing Cause B should close
this fixture too. Not reclassifying it now because the divergence
ISN'T structural-by-design — it's a known-pending arc.

## Sister-fixture regression check

| Fixture        | SSIM     | Status     |
|----------------|----------|------------|
| dots           | 0.9578   | unchanged  |
| solid          | 0.9981   | unchanged  |
| gradient       | 0.9881   | unchanged  |
| halftone       | 0.9267   | unchanged (Phase 3ag) |
| stripes        | 0.9892   | unchanged  |
| scanlines      | 0.9933   | unchanged (Phase 3x)  |
| checker        | 0.9960   | unchanged  |
| grid           | 0.9986   | unchanged (Phase 3aa) |
| rings          | 0.9744   | unchanged  |
| rays           | 0.9935   | unchanged  |
| confetti       | 0.9400   | unchanged  |
| bricks         | 0.9431   | unchanged  |

Zero regressions on pattern parity. The Canvas2D edits are
motion-only; pattern-class rasterization untouched.

## What's shipped

- `ui/src/canvas-motion.js`: breathe + pulse + bounce amplitudes
  aligned to Rust/spec (~12 LOC + comments).
- `scripts/parity/fixtures.json`: 9 motion-bearing fixtures marked
  `divergent_by_design: true` + per-fixture `divergent_reason`.
- `scripts/parity/run.py`: `report()` honors divergent_by_design,
  prints EXPECTED-DIVERGENT, bypasses gate.

No goldens re-blessed (Rust output unchanged).

## Out of scope / surfaced for follow-up

- **animated_halftone_pulse still failing.** Margin -0.0029. Driven
  by text-AA Cause B + halftone-bg + pulse-AA-at-text-boundary mix.
  Closing it requires task #121 (fontdue-WASM Cause B fix), not a
  motion-formula slice.
- **Python motion.py drift not addressed.** `backend/openmarquee/
  motion.py:220, :266, :290` still have the Canvas2D-shape formulas
  (zero baseline). Python is no longer the rendering path for the
  device (post DELETE-PIL phase), but is referenced by
  `rendering/gpu_compositor.py:10` and tested at
  `backend/tests/test_motion.py`. Aligning Python to Rust/spec is a
  parallel small slice (Phase 3aj candidate) — not blocking parity.
- **Canvas2D bounce SHAPE stays as sin().** Q3 spec lock at
  `docs/text-layer-motion-spec.md:203` accepts approximate preview;
  qarl 2026-05-03 device decision (abs(sin) ball-on-floor) applies
  to device-side only. By-design divergence; covered by
  parity_motion_compound's divergent_by_design tag.

## Cross-refs

- Phase 3ah survey (commit c093f87): named the Hybrid option that
  qarl picked.
- Phase 3ae confetti pattern: prior art for the by-design RNG-family
  reclassification approach.
- `docs/text-layer-motion-spec.md:203, 225-235`: spec lock + per-
  effect intensity-mapping table that anchors the canonical-intent
  call.
- Task #121 (fontdue-WASM): the still-open Cause B text-AA arc that
  blocks animated_halftone_pulse from clearing gate.

## Verdict + dispatch reply

- **Findings + source commit SHA:** (this commit, post-subagent-
  review).
- **Subagent verdict:** REVIEW APPROVED with three nits addressed
  pre-commit (phase-tag rot scrubbed; bounce amp drift fixed in-
  slice; commit-message direction clarified).
- **Per-fixture before/after:** see table above.
- **Did animated_halftone_pulse clear gate?** No (margin -0.0029;
  improved +0.0010 but residual is text-AA Cause B, not motion-
  formula). Honest reporting.
- **Reclassified fixtures count + names:** 9 — ticker, compound,
  uncage, transition_{fade,cut,wipe,slide,scroll,pixelate}.
- **Surviving motion FAILs needing Phase 3aj?** ONE:
  animated_halftone_pulse. Not motion-formula; closes when task
  #121 (fontdue-WASM Cause B) lands. Not a Phase 3aj candidate;
  rolls into the text-fonts arc.
- **Product-shape decisions surfaced for qarl?** Python motion.py
  has the same drift Canvas2D had. Optional Phase 3aj slice to
  align Python to Rust/spec (~10 LOC); affects backend/tests/
  test_motion.py and `rendering/gpu_compositor.py` formula
  documentation; doesn't affect parity since Python isn't a parity
  path anymore (post-DELETE-PIL). Flag for qarl awareness, not
  required for parity gate closure.
