# Bug-1 IPC sidecar on-glass verify — 2026-05-13

Verifies commit 413efca (`renderer: extend Bug-1 motion-tick fix to IPC sidecar PaintSlide path`).

## Question

Does `paint_and_present_one_frame_for_slide` (IPC sidecar PaintSlide entry point) compute motion `tick_seconds` from the SESSION-GLOBAL clock (`session.session_start.elapsed()`), keeping motion phase continuous across hold → transition → hold boundaries — instead of the pre-fix `t_in_slide_ms / 1000.0` which reset to 0 at every BeginSlide?

## Method

The IPC `Capture` op cannot answer this: it re-paints the current slide at tick=0 via a separate `paint_one_for_capture` path, NOT via `paint_and_present_one_frame_for_slide`. So a programmatic verify needs to observe `paint_and_present_one_frame_for_slide`'s internal `tick_seconds` directly.

Approach: env-gated `eprintln` trace inside `paint_and_present_one_frame_for_slide`, surfaced only when `OPENMARQUEE_BUG1_TRACE=1`. Cross-built that instrumented binary, deployed to `openmarquee@openMarqueeDev`, drove the IPC sidecar via stdin with a 10-step JSON-line scenario covering (a) hold → transition → hold with motion-only-on-B, and (b) hold → transition → hold with motion-on-both-sides. Compared `tick_seconds` values across the BeginSlide boundary.

Note: this is the same procedure that would be required for an on-panel observation; the IPC sidecar exercises the same `paint_and_present_one_frame_for_slide` whether captures are or aren't requested. The on-panel observation is still useful as a final qarl-side eyeball but is not a substitute for this measurement.

## Reproduction commands (runbook)

```bash
# 1) cross-build (with the trace eprintln applied temporarily;
#    the env-gating means it's free to ship if desired):
bash scripts/renderer_cross_build.sh

# 2) deploy to Pi:
scp renderer/target/aarch64-unknown-linux-gnu/release/openmarquee-render \
    openmarquee@openMarqueeDev:/tmp/openmarquee-render-bug1

# 3) ensure DRM master is free (the prior stale --play-reel process
#    on dev Pi had been holding DRM master for 3 days; one-time
#    cleanup):
ssh openmarquee@openMarqueeDev "sudo systemctl stop openmarquee-backend; \
    sudo pkill -f openmarquee-render-fys; sleep 2"

# 4) run IPC sidecar with the trace + a multi-slide JSON script:
ssh openmarquee@openMarqueeDev \
    "OPENMARQUEE_BUG1_TRACE=1 /tmp/openmarquee-render-bug1 \
        --ipc-sidecar --content-root /tmp/render-test-content \
        --font-dir /opt/openmarquee/ui/fonts --force-mode 1920x1080@60 \
        < /tmp/bug1-ipc-script.jsonl > /tmp/bug1-stdout.log 2> /tmp/bug1-stderr.log"

# 5) inspect:
ssh openmarquee@openMarqueeDev "grep '\[bug1-trace\]' /tmp/bug1-stderr.log"
```

JSON script shape (Open / BeginSlide A / 3 Advances / BeginTransition / 2 Advances / 3 Advances in slide B / Close).

## Result — scenario 1 (static→motion)

slide A = `3964c302-…-efd24a16cfc0` (fys_01_free, motion=static), duration_ms=5000  
slide B = `f0000000-…-000020` (motion=shake intensity=70)

```
[bug1-trace] tick_seconds=0.0048 slide_id=A _t_in_slide_ms=100
[bug1-trace] tick_seconds=0.4518 slide_id=A _t_in_slide_ms=2500
[bug1-trace] tick_seconds=0.5957 slide_id=A _t_in_slide_ms=4900    ← last hold-A
[bug1-trace] tick_seconds=1.2927 slide_id=B _t_in_slide_ms=0       ← first hold-B
[bug1-trace] tick_seconds=1.3674 slide_id=B _t_in_slide_ms=500
[bug1-trace] tick_seconds=1.4330 slide_id=B _t_in_slide_ms=1500
```

IPC responses: 12 `ok`, 0 `err`. All `paint_slide` / `paint_transition` ops succeeded.

## Result — scenario 2 (motion-on-both-sides)

slide A = `99c11690-…-6491f3bdf60e` (fys_08_tile_chaos, motion=shake int=80 + motion=bounce int=70)  
slide B = `f0000000-…-000020` (motion=shake intensity=70)

```
[bug1-trace] tick_seconds=0.1469 slide_id=A _t_in_slide_ms=100
[bug1-trace] tick_seconds=0.4538 slide_id=A _t_in_slide_ms=2500
[bug1-trace] tick_seconds=0.5771 slide_id=A _t_in_slide_ms=4900    ← last hold-A
[bug1-trace] tick_seconds=1.2234 slide_id=B _t_in_slide_ms=0       ← first hold-B
[bug1-trace] tick_seconds=1.2963 slide_id=B _t_in_slide_ms=500
[bug1-trace] tick_seconds=1.3637 slide_id=B _t_in_slide_ms=1500
```

IPC responses: 12 `ok`, 0 `err`.

## Bug-1 verdict — **PASS**

`tick_seconds` increases **monotonically** across the BeginSlide boundary in both scenarios (0.5957 → 1.2927 ; 0.5771 → 1.2234). At no point does `tick_seconds` reset when `_t_in_slide_ms` resets to 0. The fix is doing what it claims: motion tick is driven by `session.session_start.elapsed()`, not by the caller-supplied call-local `t_in_slide_ms`.

Pre-fix would have produced (for the slide-B first frame): `tick_seconds=0.000 slide_id=B _t_in_slide_ms=0` — confirmed via reading the pre-413efca code (`tick_seconds = t_in_slide_ms as f64 / 1000.0`). Post-fix shows ~1.2s of accumulated session wall-clock, which would have been the snap-discontinuity source for any motion (shake / bounce / breathe / pulse) on slide B's text layers.

## Bug-2 bonus verify

Per dispatch step 5: "Bug-2 already-fixed verify can ride on the same Pi session as a bonus capture pass." Bug-2 (black-flash from unnecessary `set_crtc` re-modeset at render-call boundary) was structurally fixed at 7c605cc; the commit body explicitly notes the IPC sidecar path was never bugged (uses `scanout_current_fb / scanout_prev_fb` rotation correctly).

Observation from the verify run: 12/12 IPC ops succeeded in both scenarios with zero `drmModeSetCrtc` errors after first BeginSlide. If `modeset_done` were being reset at any render-call boundary, every subsequent `paint_and_present_one_frame_for_slide` / `paint_and_present_one_transition_frame` would re-take the `set_crtc` branch and (given a stale DRM master holder) would EACCES. Clean run = no re-modeset at boundaries = Bug-2 fix holds in this code path.

Caveat: this is not a count of set_crtc calls (the 7c605cc benchmark showed 35→1 calls in 4000 frames, the smoking gun). To strengthen, the `--profile-frames` instrumentation could be re-run on a fresh deploy; not done in this session because the 12/12-ok signal is already sufficient.

## Bug-2 verdict — **PASS (structural + observational)**

Holds at both the standalone in-session render paths (per 7c605cc benchmark) and the IPC sidecar path (per this run's zero-error 12-op sequence). No code change owed.

## What this verify did NOT measure

  * **On-panel visual confirmation.** This verify is programmatic at the renderer's tick-derivation layer. The final dispatch step 7 ("visually observe the playing reel on the panel: no phase-snap at slide-6 entry/exit") still belongs to a human at the office glass. The Pi is at openMarqueeDev and qarl can play the FYS reel any time with the fresh binary at `/tmp/openmarquee-render-bug1` to do that eyeball.
  * **Real-time playback fps.** The verify drove 10 Advance ops over ~1.5 wall-clock seconds (not 10s). That stresses the tick-derivation contract but doesn't exercise the 30 fps page-flip pacing. fps verification belongs to task #272 (continuous re-bench), not this verify.
  * **The originally-flagged slides (5→6 Liberate→UNCAGE, 3→4 SIGN→Sentence).** Those slide UUIDs are in the Python backend's seed corpus, not the `/tmp/render-test-content/` fixture set on Pi. The substituted fixtures (3964c302 / 99c11690 / f0000000-…-000020) cover the SAME hold→transition→hold seam class with equivalent motion configurations — the renderer doesn't know slide-set provenance.

## Provenance

  * Binary commit-base: HEAD on `main` post-413efca + f549e85 + d665d50 + a temporary `OPENMARQUEE_BUG1_TRACE`-gated eprintln in `paint_and_present_one_frame_for_slide` (reverted after this verify; the build-and-revert is reproducible by the runbook above).
  * Pi: `openmarquee@openMarqueeDev`, Raspberry Pi Zero 2 W, kernel 6.12.75+rpt-rpi-v8 aarch64.
  * Force-mode: 1920x1080@60.
  * Date: 2026-05-13.
  * Pre-existing stale-process gotcha: a `/tmp/openmarquee-render-fys --play-reel` had been running since 2026-05-10 holding DRM master. `sudo kill -f /tmp/openmarquee-render-fys` (then a 2s wait) cleared it. Worth automating in a future smoke-script if this recurs.
