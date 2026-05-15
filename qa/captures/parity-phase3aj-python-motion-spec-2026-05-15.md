# Phase 3aj: Python motion.py aligned to Rust/spec canonical

**Date:** 2026-05-15
**Dispatch:** parallel mop-up to Phase 3ai's Canvas2D fix. Phase 3ai
flagged Python motion.py at `backend/openmarquee/motion.py:220/266/290`
as carrying the same amplitude drift Canvas2D had pre-3ai. qarl said
"go ahead with phase 3aj python motion.py alignment".
**Status:** Source change + test update SHIPPED.

## Canonical-intent decision

Same canonical anchor as Phase 3ai: `docs/text-layer-motion-spec.md:
225-235` defines the per-effect intensity-amplitude mappings. Rust at
`renderer/src/hdmi_logic.rs:3454, :3468, :3489` matches spec. Canvas2D
was aligned to Rust in Phase 3ai. Python carried identical drift; this
slice closes it.

| Effect  | Spec range (i=0 → i=100)         | Rust canonical                        | Python OLD formula                    | Python NEW |
|---------|----------------------------------|---------------------------------------|---------------------------------------|------------|
| breathe | ±2% → ±20%                       | `amp = 0.02 + 0.18 * intensity_norm`  | `amplitude = (intensity/100) * 0.20`  | matches Rust |
| pulse   | 70-100% shallow → 0-100% deep    | `min_alpha = 0.70 * (1.0 - intensity_norm)` | `min_a = 1.0 - intensity / 100.0` | matches Rust |
| bounce  | ±1% → ±10%                       | `amp = 0.01 + 0.09 * intensity_norm`  | `amplitude = (intensity/100) * 0.10`  | matches Rust |

Python's bounce SHAPE already used `abs(sin)` (line 308, qarl
2026-05-03 device decision). No shape change — only amplitude.

## Source change

`backend/openmarquee/motion.py`:
- **breathe** (line 220): `amplitude = 0.02 + 0.18 * (intensity / 100.0)`.
  Adds the ±2% baseline at intensity=0 (was 0).
- **pulse** (line 269): `min_a = 0.70 * (1.0 - intensity / 100.0)`.
  Adds the 70-100% shallow sweep at intensity=0 (was alpha=1 constant,
  no pulse at all).
- **bounce** (line 306): `amplitude = 0.01 + 0.09 * (intensity / 100.0)`.
  Adds the ±1% baseline at intensity=0 (was 0).

Each formula gets a comment block citing the spec line + Rust function
+ the previous-formula drift mechanism (same template as Canvas2D
Phase 3ai). No phase-tag / SHA / date rot.

## Test update

`backend/tests/test_motion.py`: one test broke under the new pulse
formula (the only intensity=0 assertion in the suite):

| Test | Pre-3aj | Post-3aj | Result |
|------|---------|----------|--------|
| `test_pulse_at_phase_zero_returns_full_alpha` (i=0, p=0) | asserts alpha==255 | NEW: a=0.85, alpha=216 | RENAMED |

Renamed to `test_pulse_at_intensity_zero_uses_shallow_baseline` with
docstring citing spec line + walkthrough showing 0.7 + 0.3*0.5 = 0.85
→ uint8 truncation gives alpha=216.

All other tests pass unchanged — checked at i=0,50,100 across phase
boundaries; the only test exercising intensity=0 with a non-trivial
phase-output expectation was the pulse one.

## Cross-formula i=0/50/100 verification

| Effect  | i=0          | i=50         | i=100        |
|---------|--------------|--------------|--------------|
| breathe | 0.02 ✓       | 0.11 ✓       | 0.20 ✓       |
| pulse   | min_a=0.70 ✓ | min_a=0.35 ✓ | min_a=0.00 ✓ |
| bounce  | 0.01 ✓       | 0.055 ✓      | 0.10 ✓       |

All values match Rust at all three boundaries.

## Test results

- `pytest backend/tests/test_motion.py`: 53/53 PASS.
- Subagent verdict: APPROVED. Math check pass across all three
  formulas. Test renames + truncation verified. Scope clean. No
  phase-tag rot.

## Callers / cross-refs

- `apply_motion` dispatcher at `motion.py:408-412` — only direct caller
  of `_apply_breathe/_pulse/_bounce`.
- `rendering/gpu_compositor.py:829` — comment reference only; not a
  code path that calls these helpers.
- Post-DELETE-PIL, Python motion.py is no longer the device renderer
  (Rust + Canvas2D are). Python remains in the repo for
  `compose_motion_frame` tests + as a reference path.

## What's shipped

- `backend/openmarquee/motion.py`: 3 amplitude formulas + 3 comment
  blocks (~15 LOC inc. comments).
- `backend/tests/test_motion.py`: 1 test renamed + assertion updated.

No goldens, no fixtures, no parity-harness changes. Python isn't a
parity path post-DELETE-PIL — this is pure spec-coherence work to
close the divergence Phase 3ai surfaced.

## Verdict + dispatch reply

- **All three engines (Rust + Canvas2D + Python) now match spec on
  breathe/pulse/bounce amplitudes.** Tri-engine motion-formula
  coherence achieved.
- **Python bounce shape (`abs(sin)`) matches Rust device path**, not
  Canvas2D's `sin` editor-preview shape. Canvas2D's `sin` is Q3 spec-
  lock approximate-preview; Python's `abs(sin)` was already device-
  canonical pre-3aj.
- **Out of scope / surfaced:** none. Phase 3aj closes the last
  unintentional motion-formula drift in the codebase. The only motion-
  related FAIL remaining (parity_animated_halftone_pulse at -0.0029)
  blocks on task #121 (fontdue-WASM Cause B text-AA), not on motion
  formulas.
