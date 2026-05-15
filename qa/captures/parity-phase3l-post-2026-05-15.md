# Phase 3l-post: parity fixture classification

**Date:** 2026-05-15
**Dispatch:** Tight measurement probe. Partition the 39 parity_tests
fixtures into text-bearing vs non-text. Tell us whether Cause B
(text-AA hairlines) is the universal blocker, OR if non-text fixtures
have crossable gates we haven't tested.
**Inputs:** `/tmp/parity_phase3l_v2.log` (post-Phase 3l), `scripts/parity/fixtures.json`
**Outputs:** `qa/captures/parity-fixture-classification-2026-05-15.json`,
`scripts/parity/classify_fixtures.py`

## Headline

**The original framing doesn't apply: there are zero non-text fixtures
in the corpus.** Every fixture — including `parity_bg_pattern_solid`
and `parity_bg_pattern_gradient` — carries at least one text layer
(small "SOLID" / "GRADIENT" label used as a visual identifier).

Reframed: classify by mean_delta tier (proxy for "is the diff
dominated by text-AA hairlines, or is it structural?"). That
preserves the original question's answer:

- **11 fixtures sit in the `hairlines` tier** (mean_delta < 1.0).
  Their max_delta is high (197–231) but mean is tiny — the diff is a
  handful of loud pixels, almost certainly the text-AA edge hairlines
  that Phase 3i–3j sized at the canvas2D/fontdue sub-pixel boundary.
- **12 fixtures in the `broad` tier** (1.0 ≤ mean < 10): broader
  mismatch (pattern AA, fill divergence).
- **16 fixtures in the `structural` tier** (mean ≥ 10): motion,
  transitions, large-text fonts. Diff is everywhere, not just edges.

## Gate-pass count

**0 / 39 fixtures cross SSIM ≥ 0.95 AND max_delta ≤ 50** post-Phase 3l.

| Closest to gate     | max_delta | SSIM   | mean   |
|---------------------|-----------|--------|--------|
| animated_uncage     | 184       | 0.8341 | 30.60  |
| bg_pattern_gradient | 197       | 0.9881 |  0.66  |
| transition_fade     | 203       | 0.7394 | 26.58  |
| font_rye            | 214       | 0.9973 |  0.18  |
| bg_pattern_solid    | 221       | 0.9981 |  0.11  |

## By category

| Category   | n  | pass | closest max_delta | avg mean_delta |
|------------|----|------|-------------------|----------------|
| animated   |  3 | 0    | 184               | 13.35          |
| bg         | 12 | 0    | 197               |  8.19          |
| font       | 11 | 0    | 214               |  7.64          |
| other      |  7 | 0    | 223               | 15.99          |
| transition |  6 | 0    | 203               | 31.45          |

## By mean tier

| Tier        | n  | pass | closest max_delta |
|-------------|----|------|-------------------|
| hairlines   | 11 | 0    | 197               |
| broad       | 12 | 0    | 223               |
| structural  | 16 | 0    | 184               |

## Hairlines tier (mean_delta < 1.0) — the Cause-B-only candidates

These 11 are the cleanest test of "fix Cause B → fixture crosses
gate". Their diff is concentrated in a few loud pixels, almost
certainly text-glyph AA edges.

| Fixture                        | max_delta | SSIM   | mean   |
|--------------------------------|-----------|--------|--------|
| bg_pattern_gradient            | 197       | 0.9881 | 0.660  |
| font_rye                       | 214       | 0.9973 | 0.179  |
| bg_pattern_solid               | 221       | 0.9981 | 0.113  |
| blend_screen                   | 223       | 0.9830 | 0.934  |
| bg_pattern_rays                | 229       | 0.9935 | 0.308  |
| animated_stripes_bounce        | 229       | 0.9894 | 0.799  |
| bg_pattern_stripes             | 229       | 0.9892 | 0.871  |
| font_inter                     | 231       | 0.9961 | 0.245  |
| font_cinzel                    | 231       | 0.9961 | 0.255  |
| font_pacifico                  | 231       | 0.9906 | 0.533  |
| font_oswald                    | 231       | 0.9891 | 0.689  |

All 11 hit SSIM ≥ 0.95 already; only max_delta blocks them. If a
Cause-B fix drops max_delta below 50, these 11 sweep into PASS in one
move.

## Verdict

**Cause B (text-AA hairline divergence) is consistent with the
observed floor for the hairlines tier.** All 11 hairlines fixtures
share a max_delta band of 197–231 — wide enough to argue a single
dominant mechanism with per-fixture variation in glyph coverage,
though the 197/214/221/223/229/231 quantization could indicate a few
related sub-causes (see Limitations).

Path forward:

1. **Attack Cause B next** (text-AA / fontdue edge coverage). One arc
   could potentially flip 11 fixtures green.
2. **The structural tier (16 fixtures) is a separate problem.** Motion
   phase, transition mid-state pixel divergence, large-text glyph
   coverage at 100+px — none will be fixed by a Cause-B repair. Defer
   to follow-up arcs (motion-phase already filed as
   `project_motion_phase_discontinuity_at_transitions`).
3. **The broad tier (12 fixtures) is mixed.** bg_pattern_dots/grid/
   confetti/checker/bricks/halftone/scanlines all show small but
   non-trivial pattern-shader divergence — could be next-target after
   Cause B (similar to Phase 3l stripes fix).

## Limitations

- "Hairlines tier" via mean_delta < 1.0 is a proxy. A motion fixture
  could in principle have mean < 1 with structural divergence in one
  small region; visual inspection of one or two diff images would
  tighten the inference.
- The 184/197/203/214/221/223/229/231 distribution suggests at least
  two or three distinct quantized causes (not one). Worth eyeballing
  a diff PNG of bg_pattern_gradient (197) vs bg_pattern_solid (221) —
  if the loud pixels are in the *same* locations, it's one cause; if
  different, it's two.
