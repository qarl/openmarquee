# v2v Motion-Through-Transitions Design Memo — 1-Decoder Hardware Wall

**Date:** 2026-06-16
**Author:** code2 (post R-106-LIVE-MOTION RED bench on e549d10 / 8a3a61f7)
**Status:** DESIGN, awaiting Jimmy-openmarquee routing decision before implement

---

## Context

R-106-LIVE-MOTION (e549d10) — remove reuse-cached paths, force dual-live decode — benched RED on FYS full 21-slide reel:
- `transition_skip_tick_live_only = 136` over 5 transitions = ~27/transition (5× escalation threshold)
- worst frame gaps: 5426 / 2565 / 2460 / 2428 / 2383 ms
- qarl on glass: "the transitions are entirely gone" — skip-tick holds prior frame so long that transitions read as freeze/cut, not transitions

QA's root-cause pin: **bcm2835 hardware codec is a single-instance HW block. It CANNOT decode outgoing + incoming H.264 streams simultaneously at typical reel resolutions (720p30 + 720p30 = 60fps total throughput → codec saturates).**

This is a throughput limit, not a latency one. "Poll harder / extend deadline" worsens it.

eeb84ec (snapshot-incoming during transition) is now back on fireplacesign. Karl rates the snapshot "watchable but a gripe" — visible incoming-side animation through transitions is the non-negotiable requirement that remains unmet.

## Constraint Surface

| Constraint | Source | Impact |
|---|---|---|
| 1 active H.264 decode at a time | bcm2835 firmware | Cannot dual-live decode |
| ~96 MB non-CMA RAM | Pi Zero 2 W 512 MB, cma=320M | Pre-buffer memory tight |
| ~300 MB CMA budget | cma=320M | DMABUF buffer budget |
| Motion-through-transitions | qarl NON-NEGOTIABLE | Cannot freeze incoming side |
| codec_fail=0 | 2ead796 + bed1681 + 33bccec | Must not regress decoder lifecycle |
| v2v image PASS | NON-NEGOTIABLE | No shader/uniform/vertex regressions |
| display_rotation=90 | qarl reel cfg | Portrait orientation; downstream of bake |
| 17 videos in reel | qarl reel cfg | Cycle pacing matters |
| transition_ms typically 500-1500 ms | reel cfg | Buffer-size frame count |

## Option A — ALT-15: Alternating 15fps Per Side

### Mechanism
- Track tick counter per transition (`session.transition_tick_count`).
- On EVEN ticks: bake side A live (1 V4L2 dequeue), reuse cached B (last good baked frame).
- On ODD ticks: bake side B live (1 V4L2 dequeue), reuse cached A.
- Both sides advance at ~15fps. Codec is single-tasked at any instant.

### Implementation Estimate
- Add `transition_tick_count: u32` field to EglSession.
- Reset to 0 at transition start (mirror the painted-flag reset hook from R-106-FREEZE-FIX).
- In `paint_and_present_one_transition_frame`, dispatch bake-or-reuse per side based on `tick_count % 2`.
- Reuse path is essentially the deleted r106 + Path A Stage 2 branches, but DELIBERATE not REACTIVE.
- Add `[perf] transition_alt15_side={a,b}` emit for QA bench parity.
- LOC: ~100-150 (~50 source + ~100 docstring + source-pin tests).

### Pros
- Minimal memory: 0 extra (uses existing `transition_fbo_a/b` handles).
- Codec is single-tasked at any instant — by design, no contention.
- Implementation is small + reverts to a well-tested code path (r106's reuse) but with deliberate scheduling.
- Skip-tick fallback still available for true codec hiccups.

### Cons
- 15fps per side may look choppy vs 30fps native; visible motion present but judder noticeable on fast content.
- Doesn't solve the case where transition_ms > 1.5s + slide content has fast motion.

### Risk
- Sacred MEDIUM. The reuse path is resurrected; need negative-pin discipline to prevent accidental reactive reuse (the bug we just fixed) creeping back.
- Codec scheduling assumption: feeding samples to V4L2 OUTPUT queue at 15fps will produce CAPTURE frames at 15fps. Need to verify with QA's bench.

### Memory cost
- 0 MB. Uses existing FBO/tex handles.

## Option B — PRE-BUFFER: Pre-decode N frames during preload

### Mechanism (per QA's suggestion)
- Extend `preload_handoff` to drain N = `transition_ms * 30 / 1000` frames from the incoming decoder's CAPTURE queue.
- Store frames as either (a) raw NV12 bytes in CPU RAM, OR (b) pre-uploaded GL textures.
- During transition: side B bake reads from buffer[tick_index] instead of live V4L2 dequeue. Side A bakes live.
- At transition complete: free buffer; live V4L2 decoder for incoming continues from sample N.

### Implementation Estimate
- New buffer field on Decoder: `transition_buffer: Option<Vec<TransitionFrame>>`.
- Modify `preload_handoff` to loop N times.
- Branch `bake_video_slide_to_current_fbo` to read from buffer when `is_offscreen_bake=true` AND buffer is Some.
- LOC: ~500-1000 (~200 buffer storage + ~300 bake branch + ~100 docstring + ~100 source-pin tests).

### Pros
- Both sides at native 30fps quality during transition.
- Live outgoing + buffered incoming = only 1 live decode at any instant.
- Resolves the motion-through-transitions requirement fully.

### Cons
- Memory: 30 frames × 720p NV12 = ~42 MB per transition. Recoverable per transition (free after).
- TIGHT on 96 MB non-CMA + ~300 MB CMA. Need to share with V4L2 CAPTURE pool (~12 MB × 2 active decoders = 24 MB), scanout chain (~24 MB at 1080p / ~12 MB at 720p), GL state (~30 MB MSDF atlas + dynamic pages).
- Reductions available:
  - 15fps buffered = 15 frames = ~21 MB (jerky motion)
  - 360p half-res = ~10.5 MB at 30fps / ~5 MB at 15fps (visible quality drop)
  - Pre-buffer only first ~0.5s, cross-fade to live in transition's second half = ~10 MB but artifact at hand-off
- Burst-decode during preload window may degrade outgoing slide's playback during preload (codec time-shares).

### Risk
- Sacred MEDIUM-HIGH. Touches the V4L2 + EGLImage cache + Option B prewarm + r101 lifecycle.
- Memory pressure: may push us back near the 96 MB ceiling that drove the night's R-1/G-1/G-2/G-3 work.
- Burst-decode timing: getting it right requires coordinating with preload_handoff's existing drain logic.

### Memory cost
- 30fps native: ~42 MB (TIGHT)
- 15fps native: ~21 MB
- 30fps half-res: ~10.5 MB
- 15fps half-res: ~5 MB (likely the sweet spot if A insufficient)

## Option C — HYBRID: ALT-15 first, PRE-BUFFER if A insufficient

### Mechanism
1. Ship ALT-15 first (Option A) as the primary motion-through-transitions candidate.
2. QA benches on glass + Karl judges.
3. If 15fps per side passes Karl's "motion is present" bar → ship.
4. If too choppy → ship Option B (pre-buffer 15fps half-res, ~5 MB).

### Pros
- Incremental risk: start with smallest change.
- Each iteration is on-glass verified before next.
- Skips Option B if A is already sufficient.

### Cons
- Requires 2 bench cycles vs 1 if we go straight to B.

## Cross-Cutting Diagnostic Improvement

**Restore `transition_tex_probe` visibility on skip-tick paths.** QA noted she lost the probe on R-106-LIVE-MOTION. Cause: probe at hdmi.rs:5589 fires AFTER both bakes succeed; when either side hits skip-tick + returns Ok(false), probe never runs. With the snapshot-incoming behavior live (eeb84ec), probe fires because reuse-cached path counts as "success." Recommend adding skip-tick probe variants for any motion-through-transitions candidate so QA retains diagnostic visibility regardless of codec contention.

## Routing Recommendation

**Recommend Option C (HYBRID) — ship ALT-15 first.** Rationale:
- Smallest change, smallest risk, smallest review surface.
- If 15fps satisfies Karl, we're done.
- If not, pre-buffer (Option B) is the natural follow-up + we've validated the codec assumption (1 decode at a time scheduling) before introducing buffer memory pressure.

If Jimmy-openmarquee prefers to skip directly to Option B (per QA's explicit suggestion), I'll implement B with 15fps half-res (~5 MB) as the memory-conservative default. Easy to tune up to 30fps / 720p if Karl's bench shows quality is the blocker, not motion.

## Awaiting Routing

- (1) Option A — ALT-15 first (Hybrid Stage 1)
- (2) Option B direct — pre-buffer 15fps half-res
- (3) Option B direct — pre-buffer 30fps 720p (memory-tight)
- (4) Different shape — please specify

Standing by.
