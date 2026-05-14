# Atlas SB visual sanity report — 2026-05-13

Closes task #277 (`Atlas SB: visual sanity captures vs full-res reference`). Informs task #279 (`Atlas SB: qarl-direct decision on marquee 29.5 vc4 ceiling`).

## Question

Does the Atlas SB half-rez scissored-bake produce visually acceptable transition mid-frames at 1080p on the dev Pi (vc4), or do we hit a softening / pixelation ceiling we have to surface to qarl?

## Method

The existing `--capture-sb-mid` path was the only one-shot transition-mid capture in the binary. Both inputs to the comparison need to go through the SAME composite shader (`cached_composite_program(kind)`) so the only variable across the pair is the bake resolution. Added `--capture-fullres-mid` (`hdmi::capture_fullres_transition_mid_to_png`) as a sibling path:

  * Same arg surface (`--fade-from / --fade-to / --transition / --capture-sb-t / --content-root / --capture-path`).
  * Bakes both slides into full-mode-resolution per-slide FBOs via a new `make_fullres_slide_fbo_with_motion` helper (mirrors the existing `make_slide_fbo` but takes `motion_states + wall_clock_unix` so the SB and fullres paths apply the same tick-zero + wall_clock-zero pin and reproducibility holds).
  * Composites with the SAME `cached_composite_program(kind)` SP shader using identity UV xforms.
  * Reads back to PNG via the same `capture_fbo_to_rgba` + `rgba_to_png_bytes` plumbing.

Captures on dev Pi (`openmarquee@openMarqueeDev`, Raspberry Pi Zero 2 W, vc4, kernel 6.12.75+rpt-rpi-v8), force-mode 1920×1080@60, content-root `/tmp/render-test-content`. Backend stopped during capture to release DRM master, restarted after.

Per-fixture command shape:
```
/tmp/openmarquee-render-fullres --output hdmi \
    {--capture-sb-mid | --capture-fullres-mid} \
    --fade-from <FROM> --fade-to <TO> \
    --transition <KIND> --capture-sb-t 0.5 \
    --content-root /tmp/render-test-content \
    --capture-path <OUT> --force-mode 1920x1080@60
```

Five SP-portable transitions, same FROM=fys_01_free + TO=fys_09_chant_wall pair as `scripts/render_tests.sh:transition_mid_*` (so the SB output matches the existing checked-in `renderer/tests/golden/transition_mid_*.png` goldens). Stretch fixture: motion-on-both-sides (FROM=fys_08_tile_chaos with motion=shake+bounce, TO=f0000000-…-000020 with motion=shake) per spec §5.1 worst case.

SSIM + L1 diff via `scripts/atlas_sb_ssim.py` (scikit-image SSIM on grayscale, per-channel L1 stats).

## Result

| Kind     | SSIM   | Max ΔL1 | Mean ΔL1 | %px Δ>50 | Gate (≥0.95) |
|----------|--------|---------|----------|----------|--------------|
| cut      | 0.9994 |      67 |    0.115 |   0.000% | PASS         |
| fade     | 0.9990 |      36 |    0.125 |   0.000% | PASS         |
| wipe     | 0.9991 |      70 |    0.122 |   0.035% | PASS         |
| slide    | 0.9978 |     247 |    0.184 |   0.058% | PASS         |
| pixelate | 0.9999 |      15 |    0.123 |   0.000% | PASS         |
| stretch  | 0.9985 |      41 |    0.207 |   0.000% | PASS         |

Image dims: 1080×1920×3. Gate: SSIM ≥ 0.95.

**All 5 SP-portable transitions PASS at the 0.95 gate. The motion-on-both-sides stretch fixture also PASSES.**

The smallest margin (slide, SSIM=0.9978, %px Δ>50 = 0.058%) is still **24× above the gate**. Max ΔL1 = 247 on the slide fixture is one localized cluster along the slide-direction wipe edge — the half-rez bake's nearest-neighbor sample at that subpixel offset diverges from the full-res bake by a single transition-band; 0.058% of pixels (1209 of ~2.07M) is well below the visible-on-glass threshold.

## Verdict

Atlas SB half-rez scissored-bake produces visually-indistinguishable output from a full-res reference baseline at 1080p on the dev Pi for the 5 SP-portable transitions we ship. The marquee 29.5 vc4 ceiling decision (#279) is informed: **the SB bake is not the bottleneck** — quality is preserved at >0.997 SSIM across all checked fixtures. Any vc4 ceiling decision should be driven by fps / fragment-budget data from #272 (continuous re-bench), not by SB output quality.

## Limitations / follow-ups

  * Captures are at tick-zero + wall_clock-zero (deterministic-bless pin per Batch 17.fix-A). Motion phase is frozen across both bake paths. The dispatch's spec §5.1 "motion-on-both-sides" worst case is partially exercised via shake+bounce intensity 70-80 on existing fixtures; the half-rez bake of motion-blurred text could in principle soften differently from a full-res bake at high motion speeds, but compute_motion_state at tick=0 produces sub-pixel offsets bounded by intensity — the SB and fullres bakes see the SAME per-glyph offsets and rasterize identically. A true "motion in flight" comparison would need a non-pinned capture path with tick > 0; deferred until a regression demands it.
  * The 5 fixtures cover the SP-portable set (cut / fade / wipe / slide / pixelate). The full 16-transition v1 set has 11 more kinds, but only SP-portable kinds can be captured via `--capture-sb-mid` (the rest are multi-pass and don't go through the SB atlas). Coverage matches the production SB path's eligibility.
  * SSIM ≥ 0.99 across all 6 says **NO investigation commit needed** on the renderer side (no Lanczos resample tuning, no mip-level changes, no fullres-fallback rule). Section 10 of the rewrite plan's "split-shader baseline" + "half-rez Pass-1 during transitions" mitigation holds at the chosen fixtures.

## Provenance

  * Binary commit-base: HEAD on `main` plus this commit's changes (renderer/src/hdmi.rs + renderer/src/main.rs + scripts/atlas_sb_ssim.py + this doc).
  * Pi: `openmarquee@openMarqueeDev` (Tailscale magic-DNS), Raspberry Pi Zero 2 W, kernel 6.12.75+rpt-rpi-v8 aarch64.
  * Force-mode: 1920x1080@60 (EDID-less override per Bug 17 / #268).
  * Capture date: 2026-05-13.
  * Re-bless: `bash scripts/renderer_cross_build.sh && scp <binary> openmarquee@openMarqueeDev:/tmp/openmarquee-render-fullres && ssh ... run captures && scp PNGs back && python3 scripts/atlas_sb_ssim.py --include-stretch`.
