# Slide-Boundary Characterization — 2026-05-14

**Headline finding: the slide-boundary spike hypothesis from bdc7303 was
wrong. `paint_us` dominates 92-96% of every frame; the over-budget rate
is nearly identical at first-frame (23.0%) vs mid-slide (17.5%). The
bottleneck is content-dependent paint cost on a subset of slides, not a
setup/cache-cold cost at slide boundaries.**

## Setup

- Binary: `/tmp/openmarquee-render-trace`, post-instrumentation build
  with `OPENMARQUEE_BOUNDARY_TRACE=1` env-gated per-phase Instant deltas
  in `paint_and_present_one_frame_for_slide` (renderer/src/hdmi.rs).
- Pi: `openmarquee@openMarqueeDev`, HDMI 1024×768 (EDID still 0 bytes,
  same as bdc7303).
- Run: 10 loops × 19 FYS slides = 190 slide entries, 6,982 paint frames
  traced, 9,818 total IPC ops, 0 errors, 377 s wall-clock.

## Phases captured

Per `paint_and_present_one_frame_for_slide`:

1. **setup_us**: resolve_slide_layers + motion_states + scene_fbo bind
2. **paint_us**: `paint_slide()` call (bg + per-layer raster + composite)
   + post-paint `gl.flush()`
3. **postpass_us**: optional brightness/gamma post-pass (identity
   settings = no-op)
4. **swap_us**: `eglSwapBuffers`
5. **gbm_us**: `lock_front_buffer` + `add_framebuffer`
6. **commit_us**: `commit_fb` (page flip) + scanout cleanup/rotation

## Phase share, first-frame vs mid-frame

| Phase        | First-frame mean | Mid-slide mean | Diff   |
|--------------|------------------|----------------|--------|
| setup        | 0.19 ms (0.8%)   | 0.05 ms (0.3%) | -0.14  |
| **paint**    | **21.66 ms (91.6%)** | **18.44 ms (96.0%)** | -3.22  |
| postpass     | 0.33 ms (1.4%)   | 0.30 ms (1.6%) | -0.03  |
| swap         | 1.16 ms (4.9%)   | 0.17 ms (0.9%) | -0.99  |
| gbm          | 0.11 ms (0.5%)   | 0.10 ms (0.5%) | -0.01  |
| commit       | 0.20 ms (0.9%)   | 0.15 ms (0.8%) | -0.05  |
| **total**    | **23.66 ms**     | **19.22 ms**   | -4.44  |

**Boundary cost is ~4.4 ms** — not the 30-100 ms spike the original
sustained-smoke hypothesis suggested. The first-frame `paint_us` is
only 3.2 ms slower than mid-slide on average; this is the texture-upload
+ shader-program-bind one-time cost. The over-33ms rate splits cleanly:

| Cohort       | n    | over-33ms | % over |
|--------------|------|-----------|--------|
| First frames | 191  | 44        | 23.0%  |
| Mid frames   | 6,791| 1,187     | 17.5%  |

If the cost were boundary-clustered, first-frames would be dramatically
worse. They're not. The slide-content distribution drives it.

## Per-slide breakdown (slowest first)

| Slide ID  | n   | paint mean | paint p95 | paint p99 | over-33% |
|-----------|-----|------------|-----------|-----------|----------|
| 81296517  | 296 | 66.45 ms   | 70.07 ms  | 76.49 ms  | **100%** |
| 06dbf60e  | 228 | 66.97 ms   | 68.31 ms  | 70.03 ms  | **100%** |
| b0f8211d  | 285 | 61.02 ms   | 63.07 ms  | 63.58 ms  | **100%** |
| 3964c302  | 333 | 44.43 ms   | 46.52 ms  | 47.72 ms  | **100%** |
| 99c11690  | 461 | 30.54 ms   | 33.75 ms  | 34.90 ms  | 11.9%    |
| 2c858968  | 621 | 19.64 ms   | 22.22 ms  | 26.19 ms  | 0%       |
| 70f9d701  | 330 | 15.30 ms   | 16.44 ms  | 17.21 ms  | 0%       |
| 60536155  | 446 | 10.13 ms   | 12.08 ms  | 12.99 ms  | 0%       |
| (10 more slides) | | < 10 ms |          |           | 0%       |
| 3d50c5fd  | 355 | 1.76 ms    | 2.50 ms   | 3.29 ms   | 0%       |

**4 of 19 slides** (81296517, 06dbf60e, b0f8211d, 3964c302) drop EVERY
frame. 14 of 19 are comfortably under budget. 1 (99c11690) is borderline.

## What's expensive about the 4 heavy slides?

Their content shapes (from `item.json` inspection):

- **81296517**: 1 layer, "SIGN", motion=breathe, bg solid #050608
- **06dbf60e**: 1 layer, "YOUR", motion=static, bg solid #050608
- **b0f8211d**: 2 layers, "UNCAGE\nYOUR SIGN!!" (shake) +
  "// synonyms.length === 2" (static), bg solid #050608
- **3964c302**: 1 layer, "FREE", motion=static, bg solid #050608

Compared with the borderline-fast 5-layer slide (99c11690: "FREE YOUR
SIGN!!!" × 5 motion variants, paint mean 30.5 ms): more layers, more
motion variety, but **smaller per-layer glyph footprint** (the long-text
"FREE YOUR\nSIGN!!!" wraps and uses smaller per-glyph font sizes than the
single-word "SIGN" / "FREE" / "YOUR" / "UNCAGE..." slides which auto-fit
to the panel mode at very large sizes).

**Hypothesis** (consistent with the vc4 being bandwidth- not GFLOPS-
bound — see `project_vc4_shader_feasibility`): large-glyph slides cover
more screen pixels with anti-aliased alpha blending, blowing the
fragment shader's bandwidth budget per `paint_slide` invocation.
Verifying this would need cycle-counter / fragment-counter access on
the GPU (out of scope here).

## Dominant phase: **paint_us**

| Aggregate first+mid (n=6982) | mean      | p50       | p95       | p99       |
|------------------------------|-----------|-----------|-----------|-----------|
| paint_us                     | 18.53 ms  | 9.17 ms   | 65.93 ms  | 67.50 ms  |
| total_us                     | 19.34 ms  | 9.93 ms   | 66.78 ms  | 68.18 ms  |
| paint_us / total_us          | **95.8%** |           |           |           |

Everything else — setup, postpass, swap, gbm, commit — is < 5% combined.
Mitigations that target those phases (prewarm cache, defer FBO realloc,
shorten commit wait) cannot move the needle.

## Recommendation for the next slice

**Do NOT dispatch prewarm / cache / FBO-defer mitigations.** The
phase-share data shows none of those would help; the cost is inside
`paint_slide`.

Three reasonable next-slice options for QA / qarl:

1. **Accept the variance, ship slice 4 as-is.** 4 of 19 FYS slides
   produce visible stutter at 30 fps. Operators can author "expensive"
   slides at their own risk; the system stays correct. Content
   guidance: avoid very-large single-glyph slides if smooth motion at
   30 fps matters. This is the **lowest-effort path**.

2. **Profile `paint_slide` internals for the heavy-slide case.**
   Per-slide GPU-side instrumentation (glow timer queries OR rdtsc
   between bg-paint / per-layer-raster / composite phases) could
   localize the bandwidth hotspot. Likely outcome: fragment shader on
   the alpha-blit pass dominates. Mitigation would be slide-scale-
   aware (downsample large-glyph layers, or switch to MSAA-free
   coverage-mask path). **~1-2 day investigation slice.**

3. **Defer slice 4 commit-as-default until at-office 1080p re-test.**
   This 1024×768 measurement is a floor. At 1080p the same heavy slides
   will paint 2.0-2.5× slower (proportional to pixel count). 67 ms ×
   2.5 = 168 ms — well off-budget. Re-running with the trace at 1080p
   would confirm; that needs HDMI EDID restored (office-glass item).

**My read**: option 1 is acceptable for slice 4 (factory + systemd path
already accept that operators flip the opt-in env explicitly).
Option 2 is the cleanest follow-up. Option 3 is a hard prerequisite
before flipping `OPENMARQUEE_RENDERER=rust-sidecar` as production
default.

## Side observations

- **0 errors across 6,982 paint frames + 9,818 total IPC ops.** IPC
  layer continues to be rock-solid (consistent with bdc7303 50k-op run).
- **First-frame `swap_us` is 1.16 ms vs mid-frame 0.17 ms.** The 0.99
  ms first-frame swap delta is real but small. Likely first-frame EGL
  driver bookkeeping; not actionable.
- **Trace overhead when ON is ~30 syscalls/s** (1 eprintln per painted
  frame at 30 fps). Negligible at this scale; safe to leave the gate
  in place in production (off by default).

## Out of scope

- Mitigation implementation (per-slice-4 decision above).
- 1080p re-test (needs HDMI EDID).
- `paint_slide` internal phase split.

## Artifacts

- Trace log: `/tmp/sidecar-trace-10loops.jsonl` on dev Pi (5.6 MB,
  ~10800 events).
- Local copy: `/tmp/trace-10loops.jsonl` on Mac.
- Driver: `/tmp/sidecar_smoke_driver.py` (Pi side, updated to pass the
  trace env var + log boundary_trace events).
- Binary: `/tmp/openmarquee-render-trace`.
- Backend restarted at completion; `/healthz` returns 200.
