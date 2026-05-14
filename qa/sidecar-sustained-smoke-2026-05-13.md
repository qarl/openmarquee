# Sidecar Sustained Smoke — 2026-05-13

**Verdict: YELLOW (conditional). Slice 4 has the IPC-stability green light;
frame-budget concern needs resolution before committing the hot path.**

## Setup

- Binary: `/tmp/openmarquee-render-smoke`, cross-built from post-601820f
  HEAD (renderer/src + ipc_main.rs validators).
- Target: dev Pi `openmarquee@openMarqueeDev`, Linux 6.12.75 aarch64.
- Mode: HDMI 1024×768 (EDID still at 0 bytes per the standing item in
  project_phase7_pending_at_office.md — production target is 1920×1080
  which is NOT what this run exercised).
- Driver: `/tmp/sidecar_smoke_driver.py` (Pi-side ad-hoc; not committed
  per dispatch out-of-scope).
- Reel: full FYS playlist, 19 slides, all `TextSlide` (image / video
  variants would have exercised d6b4f6a ImageSlide path + the
  `Capture: video slides TBD` error path; this run hit neither).
- Loops: 50.
- Wall-clock duration: **1885.37 s ≈ 31.4 min**.
- Total IPC ops: **49,261** (47,360 advance + 950 begin_slide + 950
  begin_transition + 1 open + 1 close + ...).

## Per-metric summary

| Metric                       | Result                                | Gate         | Verdict |
|------------------------------|---------------------------------------|--------------|---------|
| Err responses                | **0** across 49,261 ops               | 0            | **GREEN** |
| Sidecar stderr lines         | **0** lines drained                   | 0            | **GREEN** |
| fd count                     | 8 → 8, range [8, 8] (no churn)        | delta 0      | **GREEN** |
| Net VmRSS drift              | 72.5 → 54.7 MB (**-17.8 MB**)         | n/a (no leak)| **GREEN** |
| 5-min window mem drift       | up to 27 MB in eviction windows       | ≤2 MB        | **YELLOW** (see below — not a leak) |
| Net CmaUsed delta            | 181.1 → 177.5 MB (**-3.6 MB**)        | n/a          | **GREEN** |
| Frame round-trip > 33 ms     | 10,783 / 47,360 (**22.8%**)           | 0            | **RED** as stated; nuance below |
| Frame round-trip > 50 ms     | 7,157 / 47,360 (**15.1%**)            | n/a          | concerning |
| Mean / p50 / p99 / max       | 26.0 / 18.6 / 118.4 / 348.7 ms        |              |         |
| OOM kill / sidecar crash     | none                                  | none         | **GREEN** |

## Memory trajectory (1-min smoothed)

The 5-min window gate (≤2 MB drift) technically fails 5 of 7 windows, but
the substance is the opposite of a leak:

- **min 0-1**: warmup 87 → 91 MB (initial FBO/atlas/texture allocations).
- **min 1-10**: stable plateau at **~90 MB** (working-set in steady state).
- **min 10-13**: gradual decline 90 → 86 MB (incremental LRU eviction).
- **min 13-20**: stable plateau at **~86 MB**.
- **min 20-21**: 86 → 80 MB (eviction event).
- **min 21-24**: stable plateau at **~80 MB**.
- **min 25-26**: 80 → 54 MB (large eviction event, -26 MB in one
  minute).
- **min 26-31**: stable plateau at **~54 MB**.

First-5-min median 90.8 MB → last-5-min median 54.7 MB. **Memory
DECREASED by 36 MB over the run.** The 6-slide-cache LRU (project memory
"Atlas SB P0: free bg_tex at 6 slide_caches eviction sites", commit
landed pre-this-test) appears to be doing exactly what was intended:
releasing slide-cache textures as the working set rolls. No leak; no
unbounded growth.

## Frame budget concern

10,783 frames > 33 ms (22.8%) at 30 fps target is the actionable signal.
A few notes on context:

1. **Slide boundaries dominate**. 47,360 frames / 950 slide boundaries
   = ~50 advances per slide (~2 s hold × 30 fps). 10,783 over-budget /
   950 boundaries ≈ **11 over-budget frames per slide boundary** — i.e.,
   the spike concentrates at the first ~11 advances after `begin_slide`
   / `begin_transition`, consistent with texture upload + scene FBO
   realloc + first paint costs. Steady-state mid-slide frames are
   p50 ≈ 18 ms (well under budget).
2. **The IPC round-trip measure includes the entire GPU path** —
   `paint_slide` + scanout commit + return-to-Python. The Rust side may
   have internal timing instrumentation that splits this into phases;
   the driver only measures Python-side wall-clock around `_send_op`.
3. **1024×768 is NOT the production target**. The HDMI EDID is still
   stuck at 0 bytes (project memory project_phase7_pending_at_office.md,
   awaiting at-office glass time). 1080p will likely increase the
   slide-boundary spike further; this 22.8% figure is a floor, not a
   ceiling, for the production case.

## Outcomes the dispatch asked about

- **Mem max−min per 5-min window ≤ 2 MB**: fails (up to 27 MB in
  eviction windows). But mem is *decreasing* over the run; no leak.
  The 2-MB gate was designed to catch monotonic growth; LRU eviction
  cycles produce drift in the favorable direction. Suggest reframing
  the gate as "no monotonic growth" rather than strict drift bound.

  **Addendum (2026-05-14, per dispatch followup):** the preferred gate
  framing for future sustained-smoke runs is **"no monotonic growth
  across the run window"** — i.e., comparing first-N-min median VmRSS
  to last-N-min median VmRSS, asserting `last <= first + small_epsilon`.
  This catches leaks (monotonic upward drift) while not penalizing LRU
  eviction (which produces large but favorable drops). For this run:
  first-5-min median 90.8 MB → last-5-min median 54.7 MB → delta
  -36.1 MB → **PASS** under the proposed framing.
- **fd delta == 0**: ✓ Perfect.
- **frames > 33 ms count**: 10,783 (22.8%). Concentrated at slide
  boundaries; investigate before slice 4 production commit.
- **Non-Ok IPC responses**: 0 / 49,261. ✓ Perfect.

## Slice 4 readiness recommendation

**Conditional green light.** The IPC layer itself is rock-solid:
- 0 errors across ~50 k ops.
- 0 stderr emissions from the binary.
- 0 fd leak.
- 0 OOM / crash.
- Memory does NOT leak — it actively releases.

Slice 4 (playback.py hot-path bypass) can proceed at the IPC-contract
level. But the slide-boundary frame budget should be characterized OR
flat-out accepted before opting an operator into rust-sidecar by default
in production:

- **If accepted**: dropped frames at slide boundaries are visible as a
  brief stutter at each transition. FYS demo is forgiving (transitions
  are 600 ms with motion); a sustained-playback customer may notice.
- **If investigated**: candidates are (a) texture-upload cost,
  (b) ensure_scene_fbo realloc on dim/settings change, (c) first-frame
  modeset (one-time only, doesn't explain 950×).

Additionally: re-run at 1080p once the HDMI EDID is back (operator
glass-time item). 1024×768 understates the real GPU pressure.

## Out of scope

- Slice 4 itself: still gated on qarl's VideoSlide design call.
- Spec §11 6-hour soak: gated on release-candidate per
  feedback_no_soak_during_dev.
- HDMI EDID 1080p restoration: at-office hands-on item.

## Artifacts

- Driver: `/tmp/sidecar_smoke_driver.py` (Pi side, ad-hoc, not
  committed).
- Raw log: `/tmp/sidecar-smoke-30min-v2.jsonl` on Pi (2604 lines,
  443 KB). Local copy at `/tmp/sidecar-smoke-final.jsonl` (Mac).
- Binary: `/tmp/openmarquee-render-smoke` on Pi (post-601820f).
- Backend: stopped during the run, restarted at completion; `/healthz`
  returns 200.
