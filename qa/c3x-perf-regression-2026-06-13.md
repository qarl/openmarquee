# r110 c3.x transition path is a perf regression — note, not a fix

**Date:** 2026-06-13
**Status:** DO NOT FIX YET — recorded for future scope. Production binary
on FYS stays at r103.1 (md5 75bd94bb); the c3.x poster machinery built on
top of r103.1 inside r110 stages 3.0 → 3.3.2 is shelved.

## What QA observed

Two soaks against FYS (Pi Zero 2 W, 720p text-over-video playlist,
`OPENMARQUEE_PRELOAD_MODE=defer` — the fixed-config production posture
verified on 2026-06-13):

| Renderer baseline | Load avg | Notes |
| ----------------- | -------- | ----- |
| **r103.1** (c935f2e — production)                | ~1   | Smooth fancy transitions; `delta_ms` median 40 ms; zero from-side starvation. |
| **HEAD with r110 c3.x stack** (a2f9123 prior to 2026-06-13 work) | 6 | Same playlist + same defer mode → load 6× higher. Renderer is keeping up frame-budget-wise but the CPU floor is dramatically elevated. |

The c3.x poster machinery (introduced 2026-06-11 across commits
`246d626` (r110 c3.1), `f545b6b` (c3.2.2), `f681f7e` (c3.3),
`8615b67` (c3.3.1), `a688966` (c3.3.2)) was designed for the dual-1080p
"frozen-entry strategy" — pre-load the bg-video's first-frame poster
PNG at session entry, swap it in during the transition window if the
live decoder underfeeds, and absorb the cost of cold-pipeline frames.

For 720p — where the live decode is reliably fast enough under the
`defer` preload mode — the poster machinery is dead weight:

- Per-tick poster cache lookups + GL state thrash on every transition
  tick that doesn't need them.
- Extra FBO bookkeeping for the poster swap path (cached poster
  texture, painted-flag gating, recreate signal).
- Async recreate worker spinning per poster source even when no
  recreate is needed.
- Larger renderer working set keeping more code paths warm in icache.

QA-Jimmy summarised it: "the c3.x poster path is heavy and abandoned for
720p. r103.1's plain dual-decode crossfade is lean."

## Why we're NOT fixing this now

- The fix would be either (a) a runtime gate that auto-disables the c3.x
  poster machinery for ≤720p sources, or (b) a wholesale revert of the
  c3.x stack back to r103.1 plus a separate dual-1080p investigation.
- (a) re-introduces a "is 720p?" branch that the codebase has explicitly
  refused to bake in (see `docs/hardware-ceilings.md` §"What r97
  explicitly does NOT do" — no `1080p_forbidden` constant, no pixel-rate
  cap).
- (b) loses the dual-1080p investigation surface r110 was meant to
  characterise. The cleaner forward path is the r106 feed/drain decouple
  work (already merged upstream of r103.1 in the dual-1080p arc per
  `project_dual_1080p_arc_closed_2026_06_10` memory).
- The production sign is FINE on r103.1 right now. Touching the c3.x
  code now risks losing visibility into what r110 was originally trying
  to learn.

## When to revisit

Open a follow-up dispatch when ANY of:

1. Operator-tier signs ship that NEED dual-1080p (Pi 4/5-class hardware
   where the bcm2835-codec VPU clock budget actually lifts), AND we want
   the c3.x poster machinery as a fallback for the cold-pipeline window.
2. Profile data lands showing the CPU floor difference is hurting a
   real workload other than the load-avg-as-cosmetics symptom (e.g. the
   Web slide refresh job stalls because the renderer is sitting on the
   CPU more).
3. The dual-1080p arc reopens (QA decides to characterise it on Pi Zero
   2 W class hardware specifically — currently parked per the
   "dual-1080p arc closed 2026-06-10" memory).

In any of those cases, the c3.x stack should be moved behind an
explicit, default-OFF env knob (mirror the
`OPENMARQUEE_TRANSITION_FBO_CACHE=off` pattern from r102.2) so QA can
A/B it on the same FYS deploy without a binary rebuild.

## What stays in-tree

- All r110 c3.x commits remain on `main` history (they merged via the
  pre-2026-06-13 work). No revert proposed.
- The lean production binary is r103.1 (`c935f2e`). Stage / deploy
  scripts point at that exact build artifact for FYS.
- `docs/hardware-ceilings.md` covers the dual-1080p decision logic; this
  note covers the 720p performance reality.

## Cross-references

- `project_dual_1080p_arc_closed_2026_06_10` (memory) — closed-out arc
  summary; the r106 feed/drain decouple is the upstream-recommended
  approach for dual-1080p when it's needed.
- `qa/r103-1-preload-mode-regression-2026-06-13.md` — the separate
  config-side regression closed today via `PRELOAD_MODE=defer`.
- `docs/hardware-ceilings.md` — production-vs-experiment knob contract.
