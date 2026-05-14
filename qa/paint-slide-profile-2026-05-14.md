# paint_slide Internal Profile — 2026-05-14

**Headline: the dominant sub-phase is `raster_us` (CPU font rasterization)
at 85.9% of mean `paint_us` across the 4 heavy slides. Every layer is
rasterizing on every frame because the IPC-sidecar path passes
`glyph_cache: None` to `paint_slide` — the per-frame cache that
`render_animated_slide_in_session` uses to amortize glyph rasterization
across a hold loop is bypassed entirely. Fix is mechanical: thread a
session-owned `glyph_cache` through `paint_and_present_one_frame_for_slide`
to mirror the standalone-hold path. Expected payoff: ~50-65 ms per frame
on the heavy slides drops to ~4-9 ms (the `draw_us` floor).**

## Setup

- Binary: `/tmp/openmarquee-render-subtrace`, post-instrumentation build
  with `OPENMARQUEE_BOUNDARY_TRACE=1` extended into
  `paint_slide_with_viewport`: emits one `paint_sub` JSON line per
  invocation right before the outer `boundary` line.
- Pi: HDMI 1024×768 (EDID still 0 bytes).
- Driver: `/tmp/focused_smoke_driver.py` (Pi-side, ad-hoc), drives just
  the 4 heavy slides from 381fa49 with 50 advances each, paced at 30 fps.
- Run duration: ~7 s, 200 painted frames, 0 errors.

## Sub-phases captured

Inside `paint_slide_with_viewport` (renderer/src/hdmi.rs):
- **bg_us**: BgKind match arm (Solid/Pattern/Gradient/Image)
- **raster_us**: layer-rasterize-or-reuse loop (CPU side; calls
  `layout_text_to_alpha` when `should_rerasterize` returns true)
- **draw_us**: per-layer draw loop (GPU side; FS_GLYPH shader)
- **raster_misses**: count of layers that fired `layout_text_to_alpha`
  this frame (out of `layers`)

## Per-slide breakdown

| Slide                              | paint mean | raster mean (% of paint) | draw mean | bg mean | raster_misses/frame |
|------------------------------------|------------|--------------------------|-----------|---------|---------------------|
| 81296517 ('SIGN', breathe)         | 70.78 ms   | **61.41 ms (87.1%)**     | 9.07 ms   | 0.05 ms | **1.0 of 1**        |
| 06dbf60e ('YOUR', static)          | 61.74 ms   | **57.06 ms (92.8%)**     | 4.38 ms   | 0.05 ms | **1.0 of 1**        |
| b0f8211d ('UNCAGE...', shake+1)    | 60.43 ms   | **44.81 ms (75.2%)**     | 4.60 ms   | 10.18 ms| **2.0 of 2**        |
| 3964c302 ('FREE', static)          | 43.78 ms   | **40.01 ms (92.1%)**     | 3.39 ms   | 0.05 ms | **1.0 of 1**        |
| **aggregate (4 heavy slides)**     | 59.18 ms   | **50.83 ms (85.9%)**     | 5.36 ms   | 2.58 ms |                     |

**`raster_misses/frame` is 1.0 (or 2.0 for the 2-layer slide) on every
sampled frame.** That means every layer is going through
`layout_text_to_alpha` on every paint, even though the text never
changes between frames.

## Why is raster firing every frame?

The IPC sidecar's per-frame paint path is
`paint_and_present_one_frame_for_slide` (renderer/src/hdmi.rs:2393).
Its call to `paint_slide` (line ~2448) passes `glyph_cache: None`:

```rust
paint_slide(
    session.gl,
    mode_w, mode_h,
    &bg_kind,
    &text_layers,
    Some(&motion_states),
    wall_clock_unix,
    None,                            // ← glyph_cache: None
    Some(&mut session.image_bg_cache),
    None,                            // tex_cache: None too
)?;
```

Inside `paint_slide_with_viewport` (line ~7082-7095), the `None` arm
allocates a fresh local `Vec` cache:

```rust
None => {
    local_cache_storage = Vec::with_capacity(text_layers.len());
    local_cache_storage.resize_with(text_layers.len(), || None);
    &mut local_cache_storage
}
```

That `local_cache_storage` is freshly empty on every paint, so the
`should_rerasterize` check (line ~7116) hits the `None` branch for every
layer, every frame, and `layout_text_to_alpha` runs.

For comparison, the standalone hold-loop (`render_animated_slide_in_session`,
line 1166) keeps a session-owned `GlyphCache` across the hold's
per-frame loop, so warm-loop frames find the bitmap already cached and
skip the rasterize entirely (mean raster ~ 0).

## CPU vs GPU split

- **GL_TIME_ELAPSED**: not used in this slice. No `glxinfo` or
  `eglinfo` available on the dev Pi to verify extension support;
  EXT_disjoint_timer_query may or may not be exposed by the bcm2835
  vc4 driver. Per dispatch: documented + proceed with CPU wall-clock.
- The captured `raster_us` is **purely CPU work** (fontdue + outline
  layout to alpha bitmap, allocated in process memory). No GL calls
  fire inside the rasterize loop. So the 50.83 ms aggregate is
  unambiguously CPU-bound and unambiguously avoidable via the cache.
- The captured `draw_us` covers GL command submission only. Actual GPU
  execution synchronizes at `eglSwapBuffers` (the outer trace's
  `swap_us`, ~0.2 ms mid-frame); from bdc7303 + 381fa49 measurements,
  GPU wait time at swap is small. So `draw_us` ≈ GPU command-build
  time, GPU execution is fast.

## Dominant sub-phase: `raster_us` — and it's a cache bug, not a content cost

The 381fa49 report's "large-glyph slides cover more screen pixels"
hypothesis was the wrong tree. The actual cost is **fontdue
rasterizing the same 1000+ px high glyph bitmap 30 times per second
because no cache survives between paints in the sidecar path**.

The mid-slide measurement in 381fa49 (which showed paint mean ~18 ms
for non-heavy slides) was deceptive: those slides have small enough
glyphs that even re-rasterizing at 30 fps is cheap. The heavy slides
are exactly the ones where fontdue's alpha-bitmap output is large
(short text auto-fit to panel → large per-glyph dims), so the cache
miss cost shows up.

## Recommended fix

The infrastructure already exists. `EglSession` carries
`slide_caches: HashMap<Uuid, SlideRenderCache>` (renderer/src/hdmi.rs
~line 345) with `glyph` + `tex` fields, and the standalone hold-loop
in `render_animated_slide_in_session` uses it via a lookup-or-init
helper. The IPC sidecar path was just never wired through.

Single mechanical change in `paint_and_present_one_frame_for_slide`
(and matching ImageSlide / transition paths where applicable):

1. Get-or-init the `SlideRenderCache` entry in `session.slide_caches`
   keyed by `slide.id` (same pattern as the standalone hold-loop).
2. Pass `Some(&mut entry.glyph)` and `Some(&mut entry.tex)` to
   `paint_slide` instead of `None`.

No need to add new EglSession fields. No new invalidation logic
needed either: `slide_caches` is keyed by `slide_id`, so different
slides naturally get different entries, and the existing 6-slide LRU
eviction (project memory: Atlas SB P0) handles bounded growth.

Expected payoff: every frame after the first paint of a slide should
drop from ~60 ms total down to ~4-9 ms (the `draw_us` floor measured
here). That puts even the heaviest slide comfortably under the 33 ms
budget for 30 fps.

**The first frame of each slide stays at ~60 ms** while the cache
warms (CPU rasterize on first paint), which is consistent with the
boundary cost the original sustained-smoke report hypothesized —
only the FIRST frame after BeginSlide, not all 50. The 22.8%
over-budget rate in bdc7303 should drop to roughly (1 / 50 frames-per-
slide) × 100 ≈ 2% — orders of magnitude better.

## Why this didn't show up before

Note: production `playback.py` doesn't yet drive the Rust renderer at
all — it uses the Python PIL push-frame path via `Renderer.render_
frame(bytes)`. The comparison here is with the **standalone Rust CLI**
path (`render_playlist_reel` → `render_animated_slide_in_session`),
which has the cache and which the bdc7303 + 381fa49 smokes did NOT
exercise (those went through the IPC sidecar). The IPC sidecar was a
newer path (slice (c) of v1-spec-delta #9, 2026-05-08) that bypassed
the cache because per-paint was the natural shape of the IPC contract.

The mid-frame characterization in 381fa49 missed the bug because
(a) most FYS slides paint cheaply even without the cache, (b) the
average across all 19 slides hid the cache-miss signal for the 4 heavy
ones, and (c) the prior trace stopped at the outer `paint_us` boundary
without splitting CPU rasterize from GPU draw.

## Out of scope for this slice

- The fix itself (per dispatch operating discipline: profile first,
  dispatch fix separately).
- ImageSlide path sub-trace (FYS demo is all TextSlide; the image
  path's tex cache works differently and warrants its own profile).
- 1080p re-test (still needs HDMI EDID restored).
- `GL_TIME_ELAPSED` integration (no Pi-side tool to verify support;
  CPU wall-clock was sufficient to localize this cost).

## Recommendation for the slice-4 readiness gate

**The known issue is no longer a content-cost variance — it's a fixable
cache bug.** The 4-of-19 heavy-slide problem from 381fa49 should
disappear once the glyph cache is wired into the sidecar paint path.
Estimated effort: **~30 LOC, 1 commit, 1 day**. After the fix lands:
- Re-run the sustained smoke (bdc7303-style 30-min) to confirm the
  over-33ms rate drops below the gate.
- Re-run at 1080p once EDID is back (option 3 from 381fa49 is still
  the right hard prerequisite for the production-default flip).

Slice 4 (playback.py hot-path bypass) can proceed with the fix in
place. The IPC contract is sound; the only sidecar-specific perf
regression vs the Python path was this cache omission.

## Artifacts

- Trace log: `/tmp/focused-trace.jsonl` on dev Pi, 766 events
  (200 paint_sub + 200 boundary + headers).
- Local copy: `/tmp/focused.jsonl` on Mac.
- Driver: `/tmp/focused_smoke_driver.py` (Pi side, ad-hoc, not
  committed).
- Binary: `/tmp/openmarquee-render-subtrace`.
- Backend restarted at completion; `/healthz` returns 200.
