# FYS reel parity divergence table (Step 2c)

Baseline: post-gamma-flip rust path (HEAD `d7af1a0`, blessed 2026-05-17
via `scripts/parity/bless_fys_goldens.py`). Browser side: Canvas2D +
`ui/parity-harness.html` via Playwright Chromium.

## Summary

**37 fixtures total. 6 PASS, 24 FAIL, 7 BROWSER-SKIP.**

Failure clusters:

| cluster | count | what's diverging |
|---|---:|---|
| **font-clamp + Cause-B AA** (single-word large headlines / dense text) | 12 | fontdue + 2048px atlas-cap clamp vs Canvas2D scaled-text antialiasing |
| **motion phase / motion-bearing slides at tick=0.5** | 6 | per-layer motion (breathe/pulse/bounce/shake/ticker) phase mismatch between Rust slide-baked-into-FBO and JS per-frame redraw |
| **transition mid w/ text-heavy slides on both ends** | 6 | mostly inheriting the single-fixture divergences end-to-end (cut transitions just amplify pre-existing single-slide drift) |
| **browser-unsupported transition kinds** | 7 | `parity-harness.html` `TRANSITION_FNS` only implements 6 of 16 kinds: `push, flip, shutter, marquee, scanline, glitch, iris` aren't mapped |

The PASS set is the small subset where text is small enough to escape
the atlas clamp AND the slide has minimal/no motion AND any transition
end-state isn't a heavily-text-bearing slide. Worth noting: `parity_fys_
01_free` PASSES at SSIM=0.96 even though it has the 1382→1203 atlas clamp
— the LARGE-glyph blockiness inside the clamp dominates pixel volume but
preserves enough structure that SSIM stays above 0.92. The FAIL on
slides 02/03 is the SAME atlas-clamp behavior on shorter words where
fewer pixels = relatively-more-structural drift.

## Worst SSIM offenders (top 5, single-fixture)

| # | fixture | SSIM | mean Δ | root-cause hypothesis |
|---|---|---:|---:|---|
| 1 | `parity_fys_11_chant_wall` | 0.633 | 51.6 | "WE WANT" repeated across many layers w/ shake motion; layer-density × motion-phase divergence compounds |
| 2 | `parity_fys_10_tile_chaos` | 0.662 | 39.7 | ~30 motion-bearing tile layers, each at independent shake/bounce phase; Rust splitmix64 vs JS FNV-1a seed family already divergent-by-design |
| 3 | `parity_fys_02_your` | 0.686 | 46.8 | atlas clamp on 4-glyph headline. Cause-B AA + clamped-bitmap rescale (same code path as Bug-flag from FREE-rust 2026-05-17 capture) |
| 4 | `parity_fys_03_sign` | 0.707 | 47.2 | same as #3, 4-glyph headline, atlas clamp engaged |
| 5 | `parity_fys_07_07a_typo_oops` | 0.775 | 49.2 | typo-oops headline; atlas clamp + a "wrong-glyph" layer composited |

## Worst SSIM offenders (transitions)

| # | fixture | trans | SSIM | mean Δ | root-cause hypothesis |
|---|---|---|---:|---:|---|
| 1 | `parity_fys_t10_tile_chaos_to_chant_wall` | pixelate | 0.613 | 41.5 | from/to both in top-5 single divergences; pixelate cell-sampling amplifies the per-glyph AA divergence |
| 2 | `parity_fys_t02_your_to_sign` | slide | 0.686 | 48.4 | slide composites two atlas-clamped headlines side-by-side; both ends already at 0.69 SSIM solo |
| 3 | `parity_fys_t08_07b_typo_mid_fix_to_07c_typo_fixed` | cut | 0.782 | 47.7 | cut at t=0.5 picks the to-slide (07c_typo_fixed which is also a FAIL at 0.78 solo) |
| 4 | `parity_fys_t07_07a_typo_oops_to_07b_typo_mid_fix` | cut | 0.787 | 27.2 | cut picks to-slide; 07b is a milder FAIL (0.83 solo) but the transition diff also accumulates motion-tick mismatch |
| 5 | `parity_fys_t01_free_to_your` | wipe | 0.829 | 23.9 | wipe at t=0.5 = left half FREE / right half YOUR; YOUR side is the 0.69 SSIM solo failure |

## Per-fixture table

### Single fixtures (19 entries)

| # | name | verdict | SSIM | mean Δ | %>10 | max Δ |
|---|---|---|---:|---:|---:|---:|
| 01 | free               | PASS | 0.9615 | 3.20 | 2.8% | 250 |
| 02 | your               | FAIL | 0.6859 | 46.80 | 31.0% | 234 |
| 03 | sign               | FAIL | 0.7072 | 47.18 | 29.0% | 250 |
| 04 | the_sentence       | PASS | 0.9673 | 1.83 | 1.6% | 225 |
| 05 | liberate           | FAIL | 0.9329 | 8.65 | 4.8% | 225 |
| 06 | uncage             | FAIL | 0.8320 | 31.35 | 27.9% | 184 |
| 07 | 07a_typo_oops      | FAIL | 0.7745 | 49.21 | 20.4% | 250 |
| 08 | 07b_typo_mid_fix   | FAIL | 0.8271 | 7.75 | 19.5% | 41  |
| 09 | 07c_typo_fixed     | FAIL | 0.7828 | 47.67 | 19.7% | 250 |
| 10 | tile_chaos         | FAIL | 0.6623 | 39.75 | 31.8% | 250 |
| 11 | chant_wall         | FAIL | 0.6334 | 51.59 | 31.2% | 250 |
| 12 | scream             | FAIL | 0.8912 | 13.64 | 8.9% | 183 |
| 13 | silence            | PASS | 0.9868 | 0.49 | 0.8% | 122 |
| 14 | stadium            | PASS | 0.9296 | 5.46 | 5.8% | 250 |
| 15 | 13a_panic_1        | FAIL | 0.7900 | 31.25 | 20.2% | 250 |
| 16 | 13b_panic_2        | FAIL | 0.8056 | 28.26 | 17.6% | 250 |
| 17 | 13c_panic_3        | FAIL | 0.8474 | 22.74 | 13.1% | 247 |
| 18 | cooldown           | FAIL | 0.9308 | 9.70 | 6.8% | 250 |
| 19 | boot               | PASS | 0.9353 | 4.33 | 3.8% | 250 |

### Transition fixtures (18 entries)

| # | name | trans | verdict | SSIM | mean Δ | %>10 | max Δ |
|---|---|---|---|---:|---:|---:|---:|
| t01 | free → your | wipe | FAIL | 0.8289 | 23.91 | 16.3% | 250 |
| t02 | your → sign | slide | FAIL | 0.6859 | 48.45 | 31.0% | 255 |
| t03 | sign → the_sentence | fade | FAIL | 0.8926 | 23.98 | 29.7% | 218 |
| t04 | the_sentence → liberate | cut | FAIL | 0.9322 | 8.69 | 5.0% | 225 |
| t05 | liberate → uncage | push | BROWSER-SKIP | – | – | – | – |
| t06 | uncage → 07a_typo_oops | flip | BROWSER-SKIP | – | – | – | – |
| t07 | 07a_typo_oops → 07b_typo_mid_fix | cut | FAIL | 0.7873 | 27.21 | 19.8% | 142 |
| t08 | 07b_typo_mid_fix → 07c_typo_fixed | cut | FAIL | 0.7822 | 47.68 | 19.8% | 250 |
| t09 | 07c_typo_fixed → tile_chaos | shutter | BROWSER-SKIP | – | – | – | – |
| t10 | tile_chaos → chant_wall | pixelate | FAIL | 0.6126 | 41.47 | 51.4% | 250 |
| t11 | chant_wall → scream | marquee | BROWSER-SKIP | – | – | – | – |
| t12 | scream → silence | cut | PASS | 0.9880 | 0.28 | 0.8% | 70  |
| t13 | silence → stadium | scanline | BROWSER-SKIP | – | – | – | – |
| t14 | stadium → 13a_panic_1 | glitch | BROWSER-SKIP+RUST-SKIP | – | – | – | – |
| t15 | 13a_panic_1 → 13b_panic_2 | cut | FAIL | 0.8063 | 28.03 | 17.5% | 250 |
| t16 | 13b_panic_2 → 13c_panic_3 | cut | FAIL | 0.8465 | 22.85 | 13.2% | 247 |
| t17 | 13c_panic_3 → cooldown | iris | BROWSER-SKIP | – | – | – | – |
| t18 | cooldown → boot | scroll | FAIL | 0.8941 | 12.20 | 8.8% | 250 |

### Skip detail

| fixture | reason | path |
|---|---|---|
| t05 (push) | `unknown transition: push` | `parity-harness.html:TRANSITION_FNS` |
| t06 (flip) | `unknown transition: flip` | same |
| t09 (shutter) | `unknown transition: shutter` | same |
| t11 (marquee) | `unknown transition: marquee` | same |
| t13 (scanline) | `unknown transition: scanline` | same |
| t14 (glitch) | `capture_sb_mid: kind 'glitch' not in SP-portable set` (rust) **and** `unknown transition: glitch` (browser) | rust SB pipeline + parity-harness.html |
| t17 (iris) | `unknown transition: iris` | parity-harness.html |

## Not pre-fixed (per dispatch)

- 7 browser-side `TRANSITION_FNS` gaps (push/flip/shutter/marquee/scanline/glitch/iris)
- 1 rust-side `capture_sb_mid` SP-portability gap (glitch on the legacy two-input shader path)
- Cause-B fontdue-vs-Canvas2D antialiasing floor (12 fixtures)
- 2048px atlas-cap clamp behavior on the 4 worst-offender single-word headlines (02, 03, 07, 09)
- Motion-bearing slide phase divergence (6 fixtures)

Each is real and known; the table is the artifact. SDF + the
single-pass motion-uniform plumbing will retire most of these on
the rust side; the JS side will need TRANSITION_FNS entries added
to close the BROWSER-SKIP rows.

## Reproducing

```bash
# Bless (only when re-baselining against a new rust path):
python3 scripts/parity/bless_fys_goldens.py

# Diff (this report):
bash scripts/parity_tests.sh
# results land in /Users/qarl/project/openmarquee/code/renderer/tests/parity/captures/metrics.json
```
