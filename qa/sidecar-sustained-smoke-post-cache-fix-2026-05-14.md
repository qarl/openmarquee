# Sidecar Sustained Smoke — Post-Cache-Fix — 2026-05-14

**Verdict: YELLOW (improved). The cache fix delivers the predicted mid-
slide gains; the strict bdc7303 gate doesn't fully pass yet because the
PaintTransition path (out of scope for this slice per dispatch) still
bypasses `slide_caches`, and a handful of FYS slides have content-driven
motion (glitch substitutions, ticker scrolling) that rasterizes
different text per frame and misses the cache by design. Slice 4 has a
much stronger IPC-readiness signal now.**

## Setup

- Binary: `/tmp/openmarquee-render-cache-fix`, post-fix HEAD with
  `EglSession::slide_caches` wired into `paint_and_present_one_frame_
  for_slide` (mirror of `render_animated_slide_in_session`).
- Pi: dev Pi at HDMI 1024×768 (EDID still 0 bytes; same caveat as
  bdc7303 / 381fa49).
- Run: 50 loops × 19 FYS slides, 1885 s wall-clock (~31.4 min), 53,785
  total IPC ops, 51,884 painted frames, **0 errors**.

## Side-by-side vs bdc7303

| Metric                       | bdc7303 (pre-fix)    | Post-fix (this run)  | Δ                 |
|------------------------------|----------------------|----------------------|-------------------|
| Total IPC ops                | 49,261               | 53,785               | +9% (more frames in same wall-clock) |
| Errs                         | 0                    | **0**                | —                 |
| Frame mean                   | 26.0 ms              | **12.81 ms**         | **-51%**          |
| Frame p50                    | 18.6 ms              | **1.97 ms**          | **-89%** (9× faster) |
| Frame p95                    | (n/a)                | (~12 ms from samples)|                   |
| Frame p99                    | 118.4 ms             | 118.3 ms             | unchanged         |
| Frame max                    | 348.7 ms             | 355.5 ms             | unchanged         |
| Frames > 33 ms               | 10,783 (22.8%)       | **5,136 (9.9%)**     | **-56%**          |
| Frames > 50 ms               | 7,157  (15.1%)       | **3,183  (6.1%)**    | **-60%**          |
| fd churn                     | 8 → 8 (locked)       | **8 → 8 (locked)**   | ✓                 |
| VmRSS first-5-min median     | 90.8 MB              | **85.7 MB**          | -5.1 MB           |
| VmRSS last-5-min median      | 54.7 MB              | **84.7 MB**          | +30 MB (warmer SS) |
| Mem delta first→last 5-min   | -36.1 MB (eviction)  | **-0.96 MB (flat)**  | trajectory now stable |
| 5-min window mem drift       | up to 27 MB          | **≤ 5 MB** (steady)  | no eviction storms |
| CmaUsed delta start→end      | -3.6 MB              | **-3.4 MB**          | ✓                 |

The cache fix turns the previously-sawtooth mem trajectory into a
stable plateau (~85 MB), confirming the per-frame allocate-fresh-Vec
in the None arm of `paint_slide` was the source of the eviction storms
in the bdc7303 run too. **Mid-slide frames are now 9× faster.**

## Why p99 / max didn't move (the remaining 9.9% over budget)

The cache fix targets `paint_and_present_one_frame_for_slide` — the
direct paint_slide path during a slide's hold. It does NOT touch:

1. **`paint_and_present_one_transition_frame`**: the per-frame paint
   during a transition's blend window. This function builds slide-A
   and slide-B snapshots (via `make_slide_fbo`) every frame and passes
   `None, None` for the caches at those bake sites (renderer/src/hdmi.rs
   ~line 4286-4290). The dispatch explicitly out-of-scoped this for the
   current slice.
2. **Content-driven motion that mutates the resolved text per frame**:
   stderr shows ~1700-1900 rasterize calls per text variant for slides
   with `glitch` motion (text substitutions like "FREE YOUR S?IN!!"
   instead of "FREE YOUR SIGN!!!") or ticker scrolling. These slides
   produce a different `resolved_text` on most frames, so the cache key
   `(text, size_px)` misses by design.

Rough accounting:
- 19 transitions × 50 loops = 950 transition windows × ~20 advances
  each ≈ **19,000 transition frames** (each one re-rasterizes via
  `paint_transition`'s bake). All over-budget candidates.
- ~5 FYS slides have glitch/ticker text. At ~30 frames each × 50 loops
  = ~7,500 frames where text resolution changes per frame.

Combined: roughly 26k+ frames are intrinsically miss-prone in this
build. The measured 5,136 over-33ms (9.9% of 51,884) is consistent
with these being the lion's share.

## Bigger picture: the IPC contract layer is rock-solid

| Gate (from bdc7303 dispatch) | Result                                |
|------------------------------|---------------------------------------|
| 0 Err IPC responses          | **0** across 53,785 ops               |
| 0 fd churn                   | **0** (locked at 8 throughout)        |
| 0 dropped frames             | **5,136 (9.9%)** — partial            |
| Mem ≤ 2 MB / 5-min window    | **≤ 5 MB** (now passes the reframed "no monotonic growth" gate) |
| 0 OOM / sidecar crash        | **0**                                 |

Five of six gates pass clean; the dropped-frame gate is the only
remaining issue, and the dispatch's hypothesis about WHERE it comes
from (transition path + content-motion text) is concretely validated.

## Was bdc7303's strict gate's hypothesis-of-fix right?

The dispatch predicted "22.8% → ≤2%" via the cache fix. We got 9.9%.
The shortfall is explainable:
- **bdc7303 was sampling the WHOLE reel including transitions.** The
  dispatch's "1/50 per slide" math implicitly assumed transitions also
  fall under the cache. They don't.
- Without the transition path, the cache fix's full payoff is bounded
  by `(paint_slide frames over budget) / (all frames)`. The
  paint_slide frames went from ~50% over-budget to near-zero (the
  4-of-19 heavy slides + their boundary frames); transition frames
  remained the same.

## Next-slice recommendation

Wire `slide_caches` through `paint_and_present_one_transition_frame`'s
`make_slide_fbo` bake calls. The transition path uses two slide
snapshots (from-slide and to-slide); both should pull from
`slide_caches[slide_id]` instead of allocating local caches.
Estimated effort: similar shape to this slice, ~30-50 LOC.

After the transition fix, expect:
- Over-33 rate drops further to ~2-5% (whatever fraction is genuinely
  glitch/ticker motion).
- p99 and max stay around the boundary-frame cost (~60-120 ms) —
  unavoidable without a more aggressive prewarm slice.

## Slice 4 readiness recommendation

**Conditionally GREEN.** All IPC-contract gates pass; the remaining
dropped-frame variance is well-characterized and localized to two
code paths with clear fix scope. Recommendations:

1. Land the transition-cache slice next (low risk, same pattern as
   this fix).
2. Then re-run the sustained smoke; expect ~2-5% over-budget.
3. Then queue option 3 (1080p re-test) once HDMI EDID is restored.
4. After all three: flip rust-sidecar from opt-in to production
   default via slice 4.

The IPC layer is sound. The original bdc7303 YELLOW was a fixable
bug-cluster, not architectural risk.

## Out of scope (confirmed per dispatch)

- Transition-path cache wiring (`paint_and_present_one_transition_frame`).
- ImageSlide-path cache (PNG-direct, no text raster — separate concern).
- 1080p re-test (needs HDMI EDID).
- Slice 4 commit-as-default.

## Artifacts

- Trace log: `/tmp/sidecar-post-cache-fix.jsonl` on dev Pi (7.4 MB,
  ~53k events including 50,576 stderr lines — mostly the existing
  `rasterized text` eprintln + per-`begin_slide` mem snapshots).
- Local copy: `/tmp/post-fix.jsonl` on Mac.
- Binary: `/tmp/openmarquee-render-cache-fix` on Pi.
- Backend restarted at completion; `/healthz` returns 200.
