# r56 — Per-transition stall investigation + mitigation recommendation

**Author lane:** code1.

**Scope:** Phase A measurement instrumentation shipped; Phase B
deferred to a follow-up dispatch with actual numbers in hand.

**Origin/main HEAD at fix time:** `3163f6c` (r54 CHANGELOG
refresh). Stack: r46-r50 (mine), r49/r51/r52/r53 (code2), r54
(doc).

---

## §A — Phase A: instrumentation shipped

Two new `[perf]` log lines, no behavior changes.

### A.1 — Per-prime sub-phase breakdown (`renderer/src/video_decode.rs`)

Emitted once per `prime_video_decoder` call:

```
[perf] v4l2_prime device_open_us=N s_fmt_us=N reqbufs_us=N \
       streamon_us=N primer_feed_us=N warmup_us=N total_us=N \
       samples=N dims=WxH
```

Phase breakdown matches the bcm2835-codec V4L2 M2M recipe at
`prime_video_decoder` body (~95-200):

| Field             | Wraps                                                      |
|-------------------|------------------------------------------------------------|
| `device_open_us`  | `v4l2::Decoder::open("/dev/video10")`                     |
| `s_fmt_us`        | both `set_output_format(H264)` + `set_capture_format(NV12)` |
| `reqbufs_us`      | both `allocate_buffers(OUTPUT, 4)` + `allocate_buffers(CAPTURE, 4)` including mmap |
| `streamon_us`     | `start_streaming` (STREAMON OUTPUT then CAPTURE)           |
| `primer_feed_us`  | `feed(SPS + PPS + IDR + sample[0])`                        |
| `warmup_us`       | the 4-iteration warmup loop (with 6ms sleeps; nominal ~24ms) |
| `total_us`        | path-existence + dmabuf env-var + all of the above + assert_capture_quantization |

The diff between `total_us` and the sum-of-named is
`assert_capture_quantization_compatible` (subagent-corrected:
this is an in-memory read of the cached `set_capture_format`
result, NOT an ioctl — sub-µs) plus the path-existence check
plus the dmabuf env-var read. Should be sub-µs total.

### A.2 — Per-slide load total (`renderer/src/ipc_main.rs`)

Emitted once per video-slide `cache.load()` cold-load:

```
[perf] video_load slide_id=<uuid> mp4_open_us=N prime_us=N total_us=N
```

| Field         | Wraps                                                |
|---------------|------------------------------------------------------|
| `mp4_open_us` | `Mp4Demuxer::open` — file open + ftyp/moov parse + sample-table walk |
| `prime_us`    | the entire `prime_video_decoder` call (Linux only)   |
| `total_us`    | open + prime + dem insert + accounting               |

### A.3 — Existing telemetry (unchanged, useful for context)

Already in journal:
- `ipc: opened MP4 for video slide <id> (WxH, N samples)` — happens
  at MP4 demuxer success
- `ipc: paint_slide (video) <id>: first frame painted (sample idx N)`
  — at first-Frame-success
- The delta between these two lines on the same slide gives an
  upper bound on open-to-first-frame.

---

## §B — Expected measurement results (informed speculation)

I haven't run on FYS — Phase A's purpose is to gather real
numbers. From code reading:

| Phase            | Estimated cost on bcm2835 Pi Zero 2 W       |
|------------------|---------------------------------------------|
| `device_open_us` | ~1-5 ms (open + QUERYCAP)                   |
| `s_fmt_us`       | ~1-5 ms (two ioctls)                        |
| `reqbufs_us`     | **50-200 ms** (REQBUFS+mmap of 4 OUTPUT + 4 CAPTURE buffers at ~1.4 MB each = ~11 MB). **Subagent caveat**: byte-count estimate alone; CMA fragmentation could push this 2-5× higher in practice. Phase A measurement will replace this estimate. |
| `streamon_us`    | ~1-5 ms                                     |
| `primer_feed_us` | ~5-10 ms (one full-stream feed)             |
| `warmup_us`      | **~24-30 ms** (4 × 6 ms sleeps + per-iteration `feed()` QBUF + drain_output_quiet overhead) |
| `mp4_open_us`    | ~10-50 ms (ftyp/moov parse + sample table)  |
| **Total**        | **~100-300 ms per slide** (could climb under CMA fragmentation) |

That total maps cleanly to qarl's "stall at the beginning of
each new transition" — at 100-300 ms it would be operator-
visible as a stutter.

The dominant phase is almost certainly `reqbufs_us` (CMA pool
allocation + mmap). The next dominant is `warmup_us` (24 ms of
deliberate sleep).

---

## §C — Mitigation options ranked

### C.1 — Drop the warmup sleep (smallest possible win, low risk)

The 24 ms warmup sleep at `video_decode.rs:185` exists because
of the pre-r48 single-OUTPUT-buffer race (feed() always used
buf_idx=0, so 6 ms gave the kernel time to release the previous
QBUF before the next collided). **Post-r48 the OUTPUT free-list
rotates through 4 buffers**, so the 6 ms sleep should no longer
be load-bearing.

- **LOC**: ~3 (delete the sleep)
- **Win**: 24 ms / prime
- **Risk**: small but nonzero. Need a soak test on FYS to confirm
  no regression in steady-state video playback. The perf-night
  r5 comment says the sleep "saves ~10s per slide at runtime" —
  that 10 s was the pre-r5 cold-start cost, not a steady-state
  cost. r48 fixed the underlying buffer-collision issue so the
  sleep is now redundant.
- **CMA**: unchanged.
- **Suggested**: ship as a 1-commit follow-up after Phase A
  numbers confirm warmup_us is meaningful AND r48's free-list
  is empirically robust. Cheap enough to spawn an r57 dispatch
  on its own.

### C.2 — Pre-warm next decoder during current slide's tail (best UX win, real implementation cost)

The cleanest fix for the symptom. While slide K is playing, open
the MP4 + prime the V4L2 decoder for slide K+1 in the background,
so when BeginTransition fires the cache.load short-circuits.

- **LOC**: ~150-250 across renderer + backend. Requires a new IPC
  op `PreloadSlide { slide_id }` that the backend calls during
  the last ~500 ms of the current slide's hold. Renderer-side
  handler runs `cache.load(slide_id)` exactly as BeginSlide does.
  Backend playback loop needs to know "what's next" 500 ms ahead
  — already does (the playlist iterator).
- **Win**: hides the entire ~100-300 ms stall, assuming preload
  completes before transition fires.
- **Risk**: **CMA budget**. Pre-warm holds two decoders concurrent
  during the overlap window (last 500 ms of slide K + transition).
  This is the SAME concurrent-pool state that already happens
  during transitions, so no new MAX peak — but the 2-pool window
  grows from ~1.5 s (transition) to ~2 s (preload + transition).
  FYS measured 251.8 MB peak under the 254 MB watchdog (r50
  verify); the wider 2-pool window doesn't change the peak,
  just its duration. Should be safe.
- **CMA**: peak unchanged; 2-pool duration grows by ~500 ms.
- **Compat with text-over-video**: must include
  `TextSlide.background_video_slide_id` in the preload logic
  (recurse through `ensure_bg_video_for_text_slide` — already
  in place at `cache.load` for the BeginSlide path).
- **Suggested**: this is the right architectural fix. Recommend
  ship as r57 after Phase A confirms the per-prime cost actually
  warrants the IPC-op investment.

### C.3 — Cache first-frame texture per video slide (medium win, medium cost)

Render the first frame of every video slide once at slide-load
time and hold it as a normal RGBA8 texture (GLES-side, not CMA).
At transition start, paint the cached first-frame texture while
the bg video decoder is still cold-priming. Switch to live video
when the decoder catches up.

- **LOC**: ~100-180. New texture cache field on `SlideCache`;
  cache populated at first `paint_and_present_one_video_slide_
  frame` success; transition path uses cached texture for the
  first N transition ticks if decoder isn't ready.
- **Win**: instant visual first frame (operator sees the right
  content), but the actual decode timeline is still ~100-300 ms
  behind. There's a potential seam when the cached frame hands
  off to live video.
- **Risk**: visual seam. If the cached frame is from sample 0
  but the live decoder starts at sample 0, they should match
  exactly. But B-frame ordering could introduce a ~33 ms
  mismatch.
- **CMA**: minimal (one RGBA8 texture per video slide; ~3.5 MB
  for 1280×720). Held in GLES, not CMA.
- **Suggested**: viable fallback if C.2 turns out to push CMA
  over budget. Lower priority than C.2.

### C.4 — Single decoder, kept open + reset (biggest refactor, possibly best long-term)

Rather than per-slide Decoder open/close, keep one V4L2 decoder
open across the whole sidecar lifetime. Reset state (S_FMT to
new dims if needed) between slides.

- **LOC**: ~300-500. Significant refactor of `cache.video_
  decoders` semantics. Single decoder needs a `reset_for_new_
  stream(w, h)` that handles dim-change via REQBUFS(0) +
  REQBUFS(N) + new mmap, which is most of the cost we're trying
  to avoid anyway.
- **Win**: avoids `device_open_us` (small) + most of `s_fmt_us`
  if dims match. If dims DON'T match across slides, we still pay
  reqbufs. So the win depends heavily on whether playlists tend
  to mix video resolutions.
- **Risk**: large refactor. Likely surfaces bugs around state
  leak across slides (capture_drained, output_eof_sent, etc.).
- **CMA**: lower steady-state peak (no per-slide pool alloc) but
  worst-case same as today (one pool).
- **Suggested**: defer until C.2 + C.1 are exhausted. Not the
  right next step.

### C.5 — Pre-fill OUTPUT queue with more samples (incorrect for this symptom)

Useful if the stall were "first frame slow to decode", but the
journal evidence points to "decoder bring-up at slide boundary".
Skip.

---

## §D — Recommended sequence

1. **NOW (this commit)**: ship Phase A instrumentation. QA
   deploys, gathers numbers from FYS journal over a few playlist
   cycles. Compute min/median/max of `total_us` for the
   `[perf] video_load` line.

2. **r57 (after data)**: if `warmup_us` > ~15 ms, ship C.1 as a
   3-line commit dropping the warmup sleeps. Cheap win.

3. **r58 (after data + r57)**: if total stall (after r57) is
   still operator-visible, ship C.2 as the architectural fix.
   New IPC op + backend playback-loop hook.

4. **Defer**: C.3 + C.4 unless r58 falls short.

---

## §E — What Phase A doesn't measure (and why that's fine)

- `first_frame_decode_us` from the dispatch's example field list
  is not directly measured in this round. The reason: the first
  frame is produced at paint time (after the IPC handler returns
  + the playback loop calls Advance), not inside `prime_video_
  decoder`. The existing telemetry line `ipc: paint_slide
  (video) <id>: first frame painted (sample idx N)` provides
  this on the paint side; combined with the new `[perf]
  video_load` total_us it gives a full open-to-first-frame
  upper bound.

- Per-sample decode time. Phase A doesn't expose internal V4L2
  decode latency. If the stall turns out to be decode-side
  starvation rather than init-side, follow-up instrumentation
  would go inside `bake_video_slide_to_current_fbo` or
  `paint_and_present_one_video_slide_frame`.

---

## §F — Caveats + risks

- **No behavior change** in r56. Phase A is pure instrumentation;
  performance impact of the `Instant::now()` calls is ~ns each.
  Total instrumentation overhead per prime: ~20 ns, well below
  measurement noise.

- **Log volume**: two new lines per video-slide cold-load. In a
  4-slide video playlist with typical dwell, that's ~8-24 new
  lines per minute depending on transition rate (more transitions
  → more lines). Negligible regardless.

- **Test plan**: cross-build green (cargo zigbuild aarch64
  release, 7.60 s). Pre-push hook should run cargo test +
  cross-build. No new unit tests — the change is two eprintln
  lines wrapping timer math.

- **CMA**: unchanged from r48-r50 baseline.

- **Parity with Canvas2D**: pure backend telemetry; no rendering
  change → no parity implications.

- **Subagent review**: sacred pre-commit review still required
  for Phase A's tiny diff. Even though the change is
  ~150 LOC of "wrap with Instant::now()", subagent will catch
  any typo'd field name or borrow issue in the eprintln tuple.

---

## §G — Reply contract

`r56 CLOSED commit=<sha> phase=measure+doc`

`summary`: shipped Phase A instrumentation (per-prime sub-phase
breakdown in video_decode.rs + per-slide load total in
ipc_main.rs); deferred Phase B (mitigation pick) to r57+ pending
real numbers from FYS journal. Estimated stall from code reading
~100-300 ms per cold-load, dominated by `reqbufs_us` + 24 ms
hard-coded warmup sleep. Recommended sequence: r57 = drop warmup
sleeps (C.1), r58 = pre-warm next decoder via new IPC op (C.2)
if r57 doesn't close the gap.

— jimmy:openmarquee-code1 (lane: r56 measure + recommend)
