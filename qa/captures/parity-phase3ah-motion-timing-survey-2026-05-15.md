# Phase 3ah: motion-apply timing survey — verdict D, formula-level divergence by spec

**Date:** 2026-05-15
**Dispatch:** Triage the 8+ motion-bearing parity fixtures sitting at
SSIM 0.6-0.8 below the 0.92 gate. Three candidate causes: (A)
capture-time misalignment between Canvas2D rAF and Rust per-vsync,
(B) motion-apply phase divergence (accumulator vs pure function),
(C) sub-frame interpolation differences.
**Status:** Diagnostic-only. **All three dispatched candidates
REFUTED.** Actual dominant cause is (D) — explicit motion FORMULA
divergence between `ui/src/canvas-motion.js` and Rust
`hdmi_logic::motion_*`, locked in by the Q3 design decision at
`docs/text-layer-motion-spec.md:203` ("Pixel-identical editor↔device
parity is over-engineering"). Qarl product-shape decision needed
before any fix work.

## The gap-list (HEAD parity, gate SSIM ≥ 0.92, mean_delta ≤ 8)

| Fixture                              | Motion shape          | SSIM   | mean  | pct≥10 | Verdict     |
|--------------------------------------|-----------------------|--------|-------|--------|-------------|
| parity_motion_ticker                 | 5 ticker layers       | 0.6151 | 56.14 | 34.0%  | **FAIL**    |
| parity_motion_compound               | blink+shake+pulse+bounce | 0.6598 | 40.01 | 31.9%  | **FAIL**    |
| parity_transition_fade               | underlying ticker     | 0.7394 | 26.58 | 32.4%  | **FAIL**    |
| parity_transition_cut                | underlying ticker     | 0.6295 | 52.61 | 32.1%  | **FAIL**    |
| parity_transition_wipe               | underlying ticker     | 0.8151 | 26.08 | 16.3%  | **FAIL**    |
| parity_transition_slide              | underlying ticker     | 0.8158 | 26.05 | 16.2%  | **FAIL**    |
| parity_transition_scroll             | underlying ticker     | 0.7957 | 26.66 | 18.5%  | **FAIL**    |
| parity_transition_pixelate           | underlying ticker     | 0.7610 | 30.68 | 37.6%  | **FAIL**    |
| parity_animated_uncage               | uncage motion         | 0.8341 | 30.60 | 27.3%  | **FAIL**    |
| parity_animated_halftone_pulse       | text pulse + halftone | 0.9161 | 6.05  | 21.8%  | **FAIL**    |
| parity_animated_stripes_bounce       | bounce + stripes      | 0.9894 | 0.80  | 3.1%   | PASS        |

10 failing motion-bearing fixtures, range SSIM 0.6151 to 0.9161.
1 passing (stripes_bounce — only PASS because amplitude is small
and the bg-shader-dominated pixel count drowns out the bounce's
small offset divergence).

## Refuting the three dispatched candidates

### A. Capture-time misalignment (REFUTED)

Both paths consume a deterministic `tick` from the fixture spec
(`scripts/parity/fixtures.json` per-fixture `tick` or
`transition_t`), with NO real-clock dependence:

- **Canvas2D** at `ui/parity-harness.html:74-77`:
  ```javascript
  function captureSingle(item, tick) {
      const state = stateFromItem(item);
      drawCanvas(canvas, state, { elapsed_s: tick });
  }
  ```
  Canvas's `elapsed_s` is the fixture's `tick` literal, NOT a
  `performance.now()` / `requestAnimationFrame` timestamp.

- **Rust** at `renderer/src/hdmi.rs:3972-3982`:
  ```rust
  let tick_seconds = tick_override.unwrap_or(0.0);
  let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
  let wall_clock_unix = if tick_override.is_some() { 0 } else { current_unix_seconds() };
  ```
  `tick_seconds` is the `--capture-slide-at-tick` CLI arg; wall
  clock is pinned to 0 when capturing (Phase 17.fix-A pin).

Identical `tick` value flows into both motion engines. No
timing-of-capture mismatch.

### B. Motion-apply phase divergence (REFUTED)

Both engines compute motion as a PURE FUNCTION of `tick` — no
accumulator, no delta-from-last-frame state:

- Canvas2D `computePhase` (`ui/src/canvas-motion.js:34-37`):
  ```javascript
  function computePhase(elapsed_s, freq, motion_phase) {
      const v = elapsed_s * freq + (motion_phase || 0);
      return v - Math.floor(v);
  }
  ```
  Pure function of (elapsed_s, freq, motion_phase). Idempotent.

- Rust `motion_ticker` (`renderer/src/hdmi_logic.rs:3425-3447`):
  ```rust
  let t = tick_seconds + (phase as f64) * period as f64;
  let cycle = (t.rem_euclid(period as f64)) / (period as f64);
  ```
  Pure function of (tick_seconds, phase, period). Idempotent.

For the same tick + intensity + motion_phase, both engines
mathematically derive the same `cycle` value (verified: phase math
algebraically equivalent — Canvas2D's `(elapsed*freq + phase) mod 1`
equals Rust's `((tick + phase*period) mod period) / period` since
freq = 1/period).

### C. Sub-frame interpolation (REFUTED)

Pattern fixtures (10/10) sit comfortably above gate, demonstrating
that the underlying slide-rasterization pipeline (bg shader + text
fontdue path) does NOT suffer pixel-interpolation drift. The
divergence is concentrated in the motion-transform LAYER applied
ON TOP of the otherwise-parity-clean pixels. No sub-frame
interpolation issue.

## The actual cause: D. Motion FORMULA divergence

`ui/src/canvas-motion.js` is a deliberate "visually approximate"
port of `backend/openmarquee/motion.py` (and by transitivity Rust's
`hdmi_logic::motion_*`). The two implementations have systematic
formula differences. Three concrete examples:

### Breathe amplitude

- Canvas2D (`ui/src/canvas-motion.js:133`): `amp = (intensity/100) * 0.20`
  At intensity=50 → amp = 0.10.
- Rust (`renderer/src/hdmi_logic.rs:3454`): `amp = 0.02 + 0.18 * intensity_norm`
  At intensity_norm=0.5 → amp = 0.11.

Rust has a +0.02 baseline (never fully static even at intensity=0);
Canvas2D zeros out at intensity=0. At intensity=50 the difference
is 0.01 (10% relative on the amp magnitude).

### Pulse alpha range

- Canvas2D (`ui/src/canvas-motion.js:144-147`):
  `minA = 1 - intensity/100; a = minA + (1-minA)*sin01`
  At intensity=50 → minA=0.5, alpha range [0.5, 1.0].
- Rust (`renderer/src/hdmi_logic.rs:3468`):
  `min_alpha = 0.70 * (1 - intensity_norm); frac = 0.5*(1+sin); alpha_mul = min_alpha + (1-min_alpha)*frac`
  At intensity_norm=0.5 → min_alpha=0.35, alpha range [0.35, 1.0].

At intensity=50 the alpha-trough differs by 0.15 (43% relative on
the trough). This swings the pulsing text's visible opacity at
phase=0.5 (mid-cycle).

### Bounce shape

- Canvas2D (`ui/src/canvas-motion.js:151-156`):
  `offsetY = amp * bh * sin(2*PI*phase)` — symmetric sin, goes
  ±amp around rest position.
- Rust (`renderer/src/hdmi_logic.rs:3488-3496`):
  `offset_y_norm = -amp * abs(sin(phase_rad))` — abs(sin), only
  goes UP from rest ("ball-on-floor", per qarl 2026-05-03 comment
  at `:3484` "abs(sin) for true bouncing").

**Different shape, not just different amplitude.** Canvas2D bounces
above AND below the rest position; Rust only bounces UP. At
tick=0.5 of a bounce cycle, Canvas2D shows max-down, Rust shows max-
up. Completely different position.

### Ticker copy-count

- Canvas2D (`ui/src/canvas-motion.js:120-130`): draws TWO copies of
  the text shifted by phase, so the wrap-around is always visible
  (text exiting left simultaneously with text entering right).
- Rust ticker: single layer with `offset_x_norm = 1 - 2*cycle`
  going from +1 (right edge) to -1 (left edge), single copy only.

Both visualize "scrolling text" but Canvas2D shows the wrap event
explicitly; Rust shows a single-position offset. Different visual
behavior at phases where the text spans the box wrap point.

### Spec lock

`docs/text-layer-motion-spec.md:203-204` (Q3 decision):
> "CSS keyframes editor preview is fine. Pixel-identical
> editor↔device parity is over-engineering at this stage."

And `:303-306`:
> "Editor preview uses CSS keyframes (Q3 lock above) — visually
> approximate, not pixel-identical."

`ui/src/canvas-motion.js:1-6` reiterates this in the file's opening
comment block. The divergence is **explicitly designed-in**, not a
bug.

## Why animated_stripes_bounce passes despite formula divergence

Bounce shape diverges (Canvas2D sin vs Rust abs(sin)), but the
fixture has small intensity=50 amplitude (0.10 box-height-fraction
in Canvas2D, 0.055 in Rust) AND the underlying stripes bg has
near-pixel-perfect parity (SSIM 0.9892). The text layer covers a
small fraction of the canvas; the bg-dominant pixel count drowns
out the motion divergence in the overall SSIM. The fixture passes
at 0.9894 by accident of the bg-text ratio, not because the bounce
motion is parity-correct.

## Why animated_halftone_pulse is the boundary case

Same bg-text ratio dynamic as stripes_bounce, but:
- Halftone bg has lower parity (SSIM 0.9267, Phase 3ag — close to
  gate, not as forgiving as stripes' 0.9892).
- Pulse alpha-range divergence (0.5→1.0 Canvas2D vs 0.35→1.0 Rust)
  is the largest formula divergence in the per-effect table, AND
  pulse manifests as a global-alpha change across the entire text
  bbox (vs bounce's small Y-offset).

Result: SSIM=0.9161, ~0.004 below gate. Not enough bg-margin to
absorb the pulse-amp divergence.

## Recommendation

This is the same species as **Phase 3ae CONFETTI** (explicitly-
divergent-by-design):
- Canvas2D motion = visually approximate by spec.
- Rust motion = device canonical.
- Diff is structurally large because the FORMULAS differ.

Three product-shape options for qarl to decide:

### Option 1: Tighten Canvas2D to match Rust (≈50 LOC, 5+ fixtures close)
Update `ui/src/canvas-motion.js` to mirror `motion_breathe / motion_pulse /
motion_bounce / motion_ticker` formulas EXACTLY. The editor's preview
will visually CHANGE (some motions will look subtly different to
operators using the editor today). Likely cleanest fix.

Closing impact (estimated):
- parity_motion_compound (uses blink + shake + pulse + bounce): +0.15-0.20 SSIM.
- parity_animated_halftone_pulse (pulse): +0.04-0.06 SSIM → clears gate.
- parity_motion_ticker (5 ticker layers): mixed; ticker is single-vs-two-copy not a 1-line fix.
- parity_animated_uncage (uncage = compound effect): +0.04-0.10 SSIM, may not clear gate.
- parity_transition_* (six): underlying ticker mismatch + transition; ticker fix is shared but the transition impl also has minor sampling differences.

### Option 2: Reclassify motion fixtures as expected-divergent
Add a `divergent_by_design: true` field to motion fixtures in
`scripts/parity/fixtures.json` and have `scripts/parity/run.py`
report them informationally (not gating). Mirrors how the broader
parity arc treats Phase 3ae CONFETTI.

Cost: ~10-line harness change. Benefit: parity gate immediately
reports cleanly. Risk: hides genuine future motion-implementation
regressions on the Rust side.

### Option 3: Hybrid — fix the cheap formula divergences (option 1 lite), accept the structural ones (option 2 partial)
- Cheap (single-formula change, 5-10 LOC each):
  - breathe amplitude (add +0.02 baseline)
  - pulse alpha range (0.5→1 vs 0.35→1; this is the BIGGEST per-pixel
    diff for pulse-bearing fixtures)
- Structural (ticker two-copy vs single-copy; bounce sin vs abs(sin);
  uncage compound): leave to Option 2 reclassification.

Estimated to close: animated_halftone_pulse + animated_pulse if it
existed + parts of parity_motion_compound's pulse contribution.

## Verdict + dispatch reply

- **Findings commit SHA:** (this commit, post-subagent-review).
- **Subagent verdict:** TBD (this doc is the survey to review).
- **Motion-fixture gap list:** 10 failing fixtures, SSIM 0.6151
  to 0.9161.
- **Verdict A/B/C:** All three REFUTED. Actual cause is **(D)
  motion FORMULA divergence** — see "The actual cause" section
  above with file:line evidence.
- **File:line evidence:** Canvas2D `ui/src/canvas-motion.js:133`
  (breathe), `:144-147` (pulse), `:151-156` (bounce), `:120-130`
  (ticker two-copy). Rust `renderer/src/hdmi_logic.rs:3454`
  (breathe), `:3468-3476` (pulse), `:3488-3496` (bounce),
  `:3425-3447` (ticker). Spec lock `docs/text-layer-motion-spec.md:
  203-204` + `:303-306`.
- **Would a single Phase 3ai fix close ≥3 fixtures?** YES — Option 1
  (canvas-motion.js rewrite to mirror motion.py / Rust). ~50 LOC.
  Likely closes parity_animated_halftone_pulse + parity_motion_compound
  + 2-3 transition fixtures that share the same underlying ticker bug.
- **Product-shape decision for qarl:** Should Canvas2D editor preview
  be tightened to pixel-match the Rust device output (deviating from
  the Q3 spec lock that explicitly accepts approximate preview), OR
  should motion fixtures be reclassified as expected-divergent like
  Phase 3ae CONFETTI? This is the gating question for any Phase 3ai
  implementation slice.

## Out of scope

- Source change to `ui/src/canvas-motion.js` (deferred pending qarl
  product-shape decision).
- Source change to `scripts/parity/fixtures.json` (deferred — pending
  decision on Option 2 reclassification).
- Per-effect deep-dive on shake, blink, breathe-pulse interactions
  (deferred to Phase 3ai or later, scoped to the chosen Option).

## Cross-refs

- Phase 3ae CONFETTI findings (commit 28df38d): the same
  explicit-by-design-divergent pattern in the pattern-class arc.
- Q3 spec lock at `docs/text-layer-motion-spec.md:203-204`,
  `:303-306`.
- Phase 3af / 3ag: pattern-class precision-floor findings (the
  preceding survey-then-fix arc that closed pattern parity).
