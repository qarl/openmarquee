# Phase 3p: Cause B sub-attack 3 — upload-stage spatial localization

**Date:** 2026-05-15
**Dispatch:** Phase 3n + 3o triangulated Cause B to the upload-stage.
Phase 3p disambiguates among 4 ranked candidates by spatial signature
analysis of the loud-pixel distribution for parity_font_inter.
**Probe:** `scripts/parity/cause_b_localize.py`
**Outputs:** `qa/captures/cause-b-localization-font-inter.png`
(overlay), `qa/captures/cause-b-localization-summary-2026-05-15.json`

## Headline

**Candidate (2) — texture filter AA mismatch at glyph outlines.**

- **100.00%** of loud pixels (|delta| ≥ 50) lie within 2 px of a
  glyph-outline pixel (Sobel-detected from the Rust golden).
- **100.00%** of loud pixels sit EXACTLY on outline pixels (no
  dilation needed). Every loud pixel is a glyph-edge pixel.
- **No translation** offset improves the match. Best
  cross-correlation gain across ±4-pixel shifts: 0 (a tie at
  (1, −1) with the zero-offset match, not a positive gain).
- **Only 2.17%** of loud pixels are within 2 px of the QUAD
  rectangle perimeter (rules out candidate 1).

## Spatial signature table

| Candidate                          | Predicted signature                                | Observed |
|------------------------------------|----------------------------------------------------|----------|
| (1) Quad placement / scaling round | Loud pixels at quad-rectangle perimeter            | NO (2%)  |
| (2) Texture filter mode AA         | Uniform AA-ring on every glyph outline             | **YES (100%)** |
| (3) DPR mismatch                   | Translation by 1 or 2 px in one direction          | NO       |
| (4) WASM header parse off-by-N     | Uniform horizontal translation                     | NO       |

## Data

```
Image dims:                          1920 x 1080
Predicted quad rect (Phase 3j math): (528.00, 431.71, 1392.00, 648.29)
Loud pixels (>=50):                  3,084  (0.1487 % of canvas)
Glyph outline pixels:                13,902
Quad perimeter pixels (1 px thick):  2,160

Loud pixels within 2px of GLYPH OUTLINE:   100.00 %
Loud pixels within 2px of QUAD PERIMETER:    2.17 %

Outline match at offset (0,0):    3,084   (100 % of loud pixels)
Best translation in ±4 search:    (1,-1)  3,084  (gain = 0)
```

Visualize: `qa/captures/cause-b-localization-font-inter.png` shows
the golden Rust render with red-tinted loud-pixel overlay + cyan
quad-rectangle outline. The red traces every glyph stroke; the
cyan box is empty (no red on the cyan rectangle).

## Secondary signature (surface for follow-up)

The predicted quad rectangle has a sub-pixel y-offset: `dst_top =
431.71` and `dst_bottom = 648.29`. The placed bitmap extends 0.29 px
above and below the integer pixel grid because Phase 3j's pad-aware
scaling gives `placed_h = 757 * (216/755) = 216.58`. The 0.29 px
offset is small but real.

This means **candidate (1) is partially correct too**: the texture
sampling AA-edge response in Rust is offset by 0.29 px from
whatever sub-pixel convention Canvas2D's `drawImage` uses. The
spatial signature primarily matches candidate (2) because the
0.29 px offset propagates into a 1-pixel-thick AA-ring at every
glyph edge — visually indistinguishable from a pure filter-mode
mismatch. The root cause is likely **placement-driven, not
filter-driven**.

This is the "multiple candidates showing partial support" case the
dispatch flagged. Phase 3q should consider both:
- Snapping the Rust quad to integer pixel coords (eliminates the
  0.29 px offset → tests candidate-1-flavored fix)
- Adjusting the Canvas2D drawImage rect to match the
  Rust-computed sub-pixel rect (matches the Rust offset → tests
  candidate-2-flavored fix)

Whichever path lands in pixel-grid alignment should sweep the 11
hairlines-tier fixtures into PASS.

## Targeted Phase 3q fix scope

**Most likely single-line fix:** pixel-align the Rust quad placement
in `box_to_ndc_quad` so `dst_top`, `dst_bottom`, `dst_left`,
`dst_right` are all integer. Rounding to pixel grid (`.round()`)
after computing the centered offset would close the 0.29 px gap.

Risk: the Phase 3j pad-aware scaling change (bde81b6) was carefully
made non-overflowing for the bitmap pad. Adding `.round()` to the
already-padded coords might re-introduce the off-by-one Phase 3j
was meant to fix. The next slice should test pixel-snapping
carefully against the Phase 3j-blessed goldens (which were
re-blessed in 5caef9c / fb3f6a3).

**Alternative single-line fix:** if the Canvas2D-side `drawImage`
rect is computed in JS with `Math.round`, change it to pass the
floating-point sub-pixel rect verbatim. This aligns Canvas2D's
sub-pixel offset to Rust's. (Less safe — Canvas2D rendering at
sub-pixel coords may produce different filter response across
browser versions.)

## Limitations

- One fixture (parity_font_inter). Spatial signature should be
  consistent across the 11 hairlines-tier fixtures (same upload
  path, same fontdue → outline divergence). A spot-check on
  parity_font_rye or parity_bg_pattern_solid would confirm.
- Edge-detection threshold (Sobel ≥ 60) is empirical. A different
  threshold might shift the "100%" metric slightly but not the
  verdict (loud pixels are unambiguously on glyph outlines, not on
  quad rectangle).
- Translation search ranges ±4 pixels only. Larger DPR mismatches
  (e.g. DPR=3) would not be caught; ruled out by the 100% match at
  offset 0.
