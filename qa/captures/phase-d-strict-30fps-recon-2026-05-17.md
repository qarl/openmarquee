# Phase D recon: strict-30 fps ship gating

Date: 2026-05-16
Author: Jimmy-openmarquee-code (recon only — no implementation in this commit)
Dispatch: QA, "Phase D — strict-30 fps ship gating" (post-deploy-GREEN 192.168.1.67)

Scope: map what exists today vs what's missing for a 30-fps-sustained
ship gate. RECON ONLY — does not commit instrumentation, parser, or
criterion changes. Recommended slice plan + open product-shape
questions for qarl at the end.

---

## 1. Spec §11 read

Two clauses are directly load-bearing for Phase D. Both are quoted
verbatim from `docs/renderer-rewrite-requirements.md`.

### §8.3 — Frame rate (lines 218-224)

> Smooth, judder-free playback at 1080p on the primary target.
> Concretely: motion ticks must not drop frames at steady state, and
> transitions must not show visible stutter on a 60 Hz HDMI display.
> Specific fps targets (e.g. 30 fps steady, some floor through
> transitions) are for the implementing agent to set based on what's
> achievable on the canonical hardware; surface the chosen targets to
> qarl in the design doc.

### §11 — Feature-parity acceptance test (lines 321-326)

> The Rust renderer is "done" for v1 when the FREE YOUR SIGN reel
> runs at 30 fps with shader transitions enabled and no OOM kills
> across an extended soak (see §8.2). Until then, the existing
> Python renderer (currently under `backend/openmarquee/rendering/`)
> stays in the tree as the live path; once Rust hits acceptance,
> Python is retired.

### Key finding: the literal phrase "strict 30 fps" does NOT appear in the spec

The spec language is "30 fps steady" (§8.3) and "30 fps with shader
transitions enabled" (§11). It leaves the *form* of the criterion
("per-frame strict" vs "windowed average" vs "p99 budget") to the
implementing agent. §8.3 explicitly delegates: "Specific fps targets
... are for the implementing agent to set ... surface the chosen
targets to qarl in the design doc."

This means Phase D is **less a new milestone and more a criterion-
lockdown task**: pick the strictness shape, document it as "the
chosen target", lock it into the gate. The dispatch framing
("strict-30 fps perf gating as a hard ship requirement") is QA's
operational interpretation, not a spec-literal phrase.

§8.3's qualitative requirements ("motion ticks must not drop frames
at steady state ... no visible stutter on 60 Hz HDMI") are stronger
than "fps_avg ≥ 30 over 10 min" because they prohibit visible
hitching. A purely averaged gate can pass an alternating 50ms/16ms
pattern that visibly stutters but averages 30 fps. This argues for
a p99 or per-frame budget alongside the average.

---

## 2. Current instrumentation audit

The §11 gate is **already substantially implemented**. The
following pieces exist on HEAD (831f471):

### 2.1 Wall-clock IPC paint timing — `IpcPaintMetrics`

File: `renderer/src/ipc_main.rs:104-190`.

Per-Advance paint timing accumulator inside the IPC sidecar's main
loop. Records `(IpcPaintKind, elapsed_us)` on every successful
`PaintSlide` / `PaintTransition` response (failures excluded — they'd
skew avg/max). Emits one journald-tail-friendly summary line every
30s.

Format contract (single line, key=value, anchor token `ipc.soak`):

```
ipc.soak window_s=W frames=F transitions=T fps_avg=A.A
         paint_us=avg/U/max/M
         session_frames=SF session_transitions=ST
```

Window stats (`frames`, `transitions`, `total_paint_us`,
`max_paint_us`) reset on emit. Session counters are cumulative since
session=open. New fields can be added on the right per the format
contract.

Hot-path cost: one `Instant::elapsed()` + branch per loop iteration
when window hasn't expired. Confirmed cheap.

### 2.2 Soak harness — `renderer_pi_soak_ipc.sh`

File: `scripts/renderer_pi_soak_ipc.sh` (227 lines, Phase 9b commit
efdefed).

Tails `journalctl -fu openmarquee-backend` on the dev Pi via ssh for
a configurable duration (default 6h), runs a heartbeat every N
minutes (default 10m), invokes the parser at end for the verdict.

Defaults:

- `--target openmarquee@openMarqueeDev` (Tailscale magic-DNS)
- `--duration 6h` (§11 acceptance window)
- `--heartbeat 10m`
- `--min-fps 30.0`
- `--rolling-window 10` (minutes)
- `--dry-run` flag for CI wiring without firing a real soak

Per `feedback_no_soak_during_dev`: the harness is built but the
actual 6h soak is release-candidate-gated.

### 2.3 Gate parser — `renderer_pi_soak_ipc_parse.py`

File: `scripts/renderer_pi_soak_ipc_parse.py` (282 lines, Phase 9b
commit efdefed).

Reads a journalctl capture, parses `ipc.soak` lines via regex,
computes:

- Total window seconds + paints + overall fps
- Max `paint_us` across the soak
- Rolling minimum fps over a sliding window (default 10 min)
- OOM signal detection (`Out of memory|oom-killer|Killed process N
  (openmarquee-render)`)
- Backend crash signal detection
  (`openmarquee-backend.service.*(Main process exited|Failed with
  result)`)

Exit code 0 on PASS, non-zero on FAIL. JSON dump option for CI.

### 2.4 Profile module — `profile.rs` (LOCAL DIAGNOSTIC, NOT IPC-WIRED)

File: `renderer/src/profile.rs` (168 lines).

Full per-frame phase histogram with mean/p50/p95/p99/max. Enabled
by `--profile-frames N` CLI flag on the renderer binary. Records
~40 phases across `hdmi.rs` (paint, swap, lockfb, commit, rotate,
bake_a, bake_b, composite, sb_*, frame_total, tex_upload,
draw_tex_hit/miss, link_program, create_vbo, ...).

**Critical gap:** this is a *local diagnostic* dumped to stderr on
exit. It is NOT plumbed into the IPC sidecar's `ipc.soak` summary
line. The percentile machinery already exists in
`summarize_samples` (returns `(sum, mean, p50, p95, p99, max)`),
but it's gated behind the `--profile-frames N` flag and the soak
gate doesn't see it.

### 2.5 What the parser checks today

`renderer_pi_soak_ipc_parse.py:172-194`:

1. **rolling_min_fps ≥ --min-fps-avg (default 30.0)** over a
   rolling 10-min window. If the soak is shorter than 10 min, falls
   back to overall fps.
2. **No OOM signal in journalctl capture.**
3. **No backend crash signal in journalctl capture.**

No p99 paint_us gate. No per-frame budget gate. No dropped-frame
counter.

---

## 3. Criterion shape — recommendation

The dispatch surfaces three candidate shapes. Mapped to what
exists today:

### (a) Per-frame strict ≤33.33 ms

Every successful paint must complete in ≤33333µs. Hardest to pass
on vc4: any single GL hiccup (driver reschedule, V4L2 decoder
startup blip, page fault during glReadPixels) fails the soak.

**Cost to implement:** small. Add `max_paint_us` gate on
`paint_us_max` field, threshold 33333. Already emitted. Or add a
`drops_window` counter (paints > 33333µs per window) and gate on
`drops_session == 0` over the soak.

**Cost to pass:** high. The §8.2 6h soak almost certainly contains
≥1 outlier paint > 33ms even on a healthy system (V4L2 decode
warm-up, GBM buffer reclaim, kernel pre-emption). Likely
false-failures from non-perf causes.

**Verdict:** too strict for a ship gate. Useful as an internal
regression alarm, not a ship signal.

### (b) Windowed strict — p99 paint_us ≤ 33.33 ms AND fps_avg ≥ 30 (recommended)

Existing rolling-10min `fps_avg ≥ 30.0` gate PLUS new
`p99(paint_us) ≤ 33333` gate over the same rolling 10-min window.

**Cost to implement:** ~80 LOC. Add a bounded ring buffer (e.g.
last 2000 `paint_us` samples per window) to `IpcPaintMetrics`. On
window emit, sort + compute p50/p95/p99. Append three new fields
to the format contract:

```
ipc.soak window_s=W frames=F transitions=T fps_avg=A.A
         paint_us=avg/U/max/M
         paint_us_p50=P50 paint_us_p95=P95 paint_us_p99=P99
         session_frames=SF session_transitions=ST
```

Parser side: add `--max-p99-paint-us` (default 33333); gate fails
if rolling-window p99 exceeds budget. Use the same
`rolling_min_fps`-style sliding logic but track p99 instead of fps.

**Cost to pass:** moderate. Allows up to 1% outlier paints per
window. Rejects "averaged but jittery" failure mode (50ms/16ms
alternating that averages 30 fps but visibly stutters per §8.3).

**Verdict:** recommended. Matches §8.3's qualitative "no visible
stutter" requirement closer than (c), much cheaper to pass than (a).
Minimum-delta path from existing infrastructure.

### (c) Soak-equivalent — fps_avg ≥ 30 over rolling 10 min, no OOM, no crash

**This is exactly what exists today.** No new code needed. Lock the
parser defaults (`--min-fps-avg 30.0 --rolling-window-min 10`) as
the ship gate; declare Phase D done.

**Cost to implement:** 0 (parameter lockdown only).

**Cost to pass:** lowest. Misses the visible-stutter case described
in §8.3.

**Verdict:** ship-safe **if** combined with a qualitative QA pass
("watch the FYS reel for 5 min on glass, no visible hitching"). If
the §11 gate is the *only* perf signal, (b) is stronger.

### Recommendation: (b)

The p99 budget is the smallest delta that closes the §8.3
"judder-free / no visible stutter" gap. ~80 LOC, no architectural
changes, reuses the existing `summarize_samples` percentile math
from `profile.rs`. Per `feedback_no_soak_during_dev` the 6h soak
remains release-candidate-gated; (b) just changes what the parser
verdict checks at the end of that gated run.

---

## 4. Slice plan

If (b) is selected. Total ~110 LOC + tests.

### Slice 1 — IPC instrumentation (~80 LOC)

File: `renderer/src/ipc_main.rs`.

- Add a bounded `paint_us_samples: Vec<u64>` to
  `IpcPaintMetrics` (capacity ~2000 — caps memory at ~16 KB per
  window; 2000 > 30s × 30 fps headroom for transitions).
- In `record()`: push `elapsed_us`. Drop oldest when capacity
  exceeded (or just truncate at capacity floor — single 30s window
  shouldn't exceed 2000 paints under any realistic workload).
- In `maybe_emit_summary()`: copy + sort the ring, compute
  p50/p95/p99 using the same percentile math as
  `profile.rs:summarize_samples` (which is already
  cross-target-safe pure stdlib). Refactor `summarize_samples`
  out of `profile.rs` into a shared util module, or inline the
  percentile compute in `ipc_main.rs` to avoid the cross-file
  coupling — implementer's call. Slight preference for inline
  (the percentile math is 4 lines) to keep `profile.rs` opt-in
  diagnostic and not bring it into hot-path scope.
- Extend the eprintln format string with three new fields
  immediately before `session_frames` per the format contract:
  `paint_us_p50=N paint_us_p95=N paint_us_p99=N`.
- Reset the samples vec alongside other window stats.
- Unit test in `ipc_main.rs` `mod tests` (the file already has
  cross-platform `#[cfg(test)]` coverage for `IpcPaintMetrics`
  — extend with a synthetic-payload p99 assertion).

### Slice 2 — Parser update (~30 LOC)

File: `scripts/renderer_pi_soak_ipc_parse.py`.

- Extend `PAT_IPC_SOAK` to capture three new groups
  `paint_us_p50`, `paint_us_p95`, `paint_us_p99`. Keep them
  optional in the regex (`(?:\s+paint_us_p99=...)?`) so old
  captures still parse.
- Add `--max-p99-paint-us` argument, default 33333.
- Add a `rolling_max_p99` helper modeled after `rolling_min_fps`:
  slide a window, compute the **maximum** of per-window p99
  values over the window, gate fails if it exceeds the budget.
  (Or: average the per-window p99 values weighted by frames.
  Pick "max of p99" — it's the more conservative gate and
  surfaces the worst window.)
- Augment the `summarize()` failure list and human report.
- Update the harness shell script's `--min-fps` block to also pass
  `--max-p99-paint-us` (or rely on parser default).

### Slice 3 — OPTIONAL: dropped-frame counter (~30 LOC)

File: `renderer/src/ipc_main.rs` + parser.

- Track `drops_window` and `drops_session` counters in
  `IpcPaintMetrics`: increment when `elapsed_us > 33333`.
- Append to the format contract:
  `drops_window=X drops_session=Y`.
- Parser: tally `total_drops = sum(drops_window)`. Diagnostic
  only — emit in human report; not gated unless qarl says
  otherwise. Useful for distinguishing "p99 was 35ms once" from
  "p99 was 35ms in 40% of windows".

Slice 3 is a *diagnostic* aid, not a gate. Recommend deferring
unless qarl explicitly asks for it. Adds visibility without
changing PASS/FAIL semantics.

---

## 5. Open questions for qarl

These are product-shape decisions that need qarl's call before
implementation lands. Per `feedback_no_stop_ask_in_renderer_rewrite`
these are minimized to genuine product-shape forks (not
ergonomics):

### Q1: Criterion shape — (a), (b), or (c)?

The recommendation is (b) windowed p99 ≤ 33.33ms + avg fps ≥ 30.
But (c) (status quo, lock current defaults as ship gate) is also
defensible if you trust qualitative QA-on-glass to catch
visible-stutter cases that average-fps would miss.

Default if you don't reply: (b). It's the smallest delta that
closes the §8.3 visible-stutter gap.

### Q2: Should the gate budget allow tick-jitter slack?

A render loop perfectly delivering 30 fps will sometimes measure
29.96 due to monotonic-clock jitter on the 30s window boundary. The
current `--min-fps-avg 30.0` gate will occasionally false-fail.
Options:

- Keep 30.0 floor, accept the false-failure rate (estimate < 1%).
- Drop the floor to 29.5 to absorb tick jitter (still rejects 28).
- Use the session-cumulative `session_frames / session_window_s`
  for the gate instead of rolling, which is jitter-free at long
  windows.

Default if you don't reply: drop floor to 29.5 (it preserves the
"30 fps" semantic while not punishing tick jitter).

### Q3: Is `paint_us` the right "frame" — or should the gate measure end-to-end?

The current `IpcPaintMetrics.paint_us` measures from the IPC
sidecar's response-tag through `run_paint_hook` return. This
covers GL paint + bake + composite + swap + commit. It does NOT
include:

- Playback-loop iteration overhead between Advance calls (in
  the FastAPI backend).
- Time the IPC sidecar is idle waiting for the next Advance
  request.
- HDMI scanout latency (vc4 buffer flip + display refresh).

If the gate's purpose is "frames hit the display at 30 Hz", the
right measurement is **interval between Advance responses on the
backend side**, not paint duration. If the gate's purpose is
"the GPU keeps up", then `paint_us` is correct as-is.

Default if you don't reply: keep `paint_us` (GPU-side budget). The
backend playback loop is single-threaded asyncio sitting on an IPC
read; loop overhead is negligible at 30 Hz compared to vc4 paint
budget. End-to-end interval gating is a future expansion if Phase D
(b) lands and we still see visible judder.

### Q4: Phase D done = (b) shipped, or stronger?

If (b) is the recommendation, the post-Phase-D ship gate is:
"6h soak on dev Pi → `renderer_pi_soak_ipc.sh` exits 0, parser
emits PASS verdict with rolling_max_p99 ≤ 33ms and overall fps ≥
29.5."

Is that the GA bar, or are there additional gates (cold start
≤4s per §8.4? CMA slope-free per §8.2? Visual parity diff
against `golden/` per existing render_tests.sh?) that need to be
bundled into a single "Phase D done" verdict?

Default if you don't reply: Phase D = (b) shipped. §8.4 cold start
+ §8.2 CMA slope are separate gates with their own existing
machinery; bundle later if needed.

---

## 6. Recon summary

- Spec §11 specifies "30 fps with shader transitions" but leaves
  the criterion form (per-frame / windowed / p99) to the
  implementing agent. "Strict 30 fps" is QA's operational framing,
  not a spec-literal phrase.
- Existing instrumentation (Phase 9a + 9b commits ffbb437 +
  efdefed) is **substantially complete**: `IpcPaintMetrics` emits
  30s summary lines; `renderer_pi_soak_ipc.sh` orchestrates the
  capture; `renderer_pi_soak_ipc_parse.py` gates `fps_avg ≥
  30.0` over a rolling 10-min window + checks OOM/crash signals.
- The principal gap is **per-frame distribution visibility**.
  `paint_us` ships only `avg` + `max`. The
  `summarize_samples` percentile machinery already exists in
  `profile.rs` but is local-diagnostic only, not piped into
  the soak summary.
- Recommendation: criterion (b) — add `paint_us_p99` to the IPC
  summary + add a p99 ≤ 33.33ms gate to the parser. ~110 LOC
  across two slices. Closes the §8.3 "no visible stutter" gap.
- Recommendation is rebuttable: if qarl prefers (c) status quo +
  qualitative QA-on-glass, Phase D collapses to parameter
  lockdown and is functionally done.
- Four open questions for qarl listed in §5; defaults documented
  for each in case no answer arrives.

---

**Update 2026-05-17:** Phase D shipped on the documented defaults
— implemented as slice 1 (8a2e043, IPC paint_us_p99 instrumentation)
and slice 2 (f03ee91, parser gate p99 ≤ 33333us). Phase D complete;
goes live on the Pi at next deploy.
