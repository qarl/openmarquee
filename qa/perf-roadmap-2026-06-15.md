# OpenMarquee Phase-2 Optimization Roadmap (2026-06-15)

**Owner:** Jimmy-openmarquee-code2 (read-only analysis; doc only — no
code touched in this artifact)

**Scope:** Prioritized opportunities for framerate, memory, CMA, and
waste, anchored on the DEPLOYED renderer
(`task/v2v-r106-decouple-2026-06-14`, HEAD `4b6e93a` = r103.1 + iter-7
scoped offscreen-bake flush + r106 feed/drain decouple). Each item is
file:line + expected impact + effort + risk + DO-NOT flag for the v2v
transition fix.

## Bench baseline (per QA, fireplacesign)

| Metric | Current | Target |
|---|---|---|
| Steady-state video paint | 38–40 ms / 25 fps | ≤ 33.3 ms / 30 fps |
| Transition spikes | 137–667 ms + 1.5–2.6 s freezes | ≤ 200 ms; no >1 s freeze |
| Process swap | ~41 MB | <10 MB |
| CMA used / pool | ~235 MB / 327 MB | ≤ 220 MB peak (≥ 50 MB headroom rule) |

## DO-NOT list

Hard regression risks. Do not touch in any Phase-2 optimization:

- **The iter-7 scoped `gl.flush()` gate at `renderer/src/hdmi.rs:8691,
  8829`.** The `is_offscreen_bake && bake_offscreen_flush_enabled()`
  guard is the cure for the vc4/Mesa offscreen-FBO tile-store
  deferral. Removing it OR widening it to steady-state both regress.
  (Waste audit confirms: scoping is correct; zero production cost.)
- **The r106 feed/drain decouple call shape at
  `renderer/src/hdmi.rs:8482-8483` + `renderer/src/video_decode.rs`
  `try_feed_nonblocking` path.** Topup cadence may be RETUNED (see
  Framerate-2) but the decouple invariant — feed continues while
  DQBUF polls — is load-bearing for dual-1080p.
- **The `transition_fbo_a/b_painted` flag pattern at
  `renderer/src/hdmi.rs:354/360, 8443`.** Cached per-side FBO reuse
  across transition frames is r106 Path A's correctness foundation.
- **Eviction in `cache.evict_other_video_state(&keep_ids)` for the
  to-slide during BeginTransition.** The to-side decoder must persist
  through the transition window or the in-transition switch breaks.
- **code1's golden-test work** (per QA dispatch). Don't refactor the
  fixtures or shared assertion helpers.

## Cross-bucket priority table (top 10)

Ordered by **impact / effort**. Bench every change before/after on
fireplacesign; merge to main only on STRICTLY BETTER.

| # | Item | File:line | Bucket | Effort | Expected gain | Risk |
|---|---|---|---|---|---|---|
| 1 | Cap `slide_caches` HashMap (LRU 12) | hdmi.rs:434 | MEM/SWAP | SMALL | 40–80 MB swap reduction; eliminates page churn under playlist cycling | LOW |
| 2 | V4L2 CAPTURE buffer pool 4 → 2 | video_decode.rs:344-347 | MEM + CMA | TRIVIAL | 5.93 MB CMA + 6–10 MB RSS per decoder; ~12 MB on dual-decoder transition | MEDIUM (DPB depth validation) |
| 3 | Expand r65 async-preload coverage to BeginSlide's cold cache.load | ipc_main.rs:2917 + 2877 | FPS | MEDIUM | 0.5–1.5 s eliminated per multi-slide chain; no more multi-second freeze | LOW (the worker path already ships) |
| 4 | Cache `cover_quad_vbo` per `(frame_w, frame_h)` in EglSession | hdmi.rs:8610 | WASTE | SMALL | 0.5–1 µs/frame + reduces per-tick VBO churn | LOW |
| 5 | Pool `motion_states` Vec per session, `.clear() + .extend()` | hdmi.rs:7949-7964 | WASTE | SMALL | 2–4 µs/frame on 4-layer text slides; eliminates 4 malloc+free/tick | LOW |
| 6 | Cache `preload_mode()` + boundary-trace + firstframe-profile env reads in session | hdmi.rs:3414, 13665, 4608, 8417; ipc_main.rs:248 | WASTE | TRIVIAL | ~1.5 µs/frame; reduces per-tick `getenv` syscall pressure | LOW |
| 7 | Tune r106 topup cap 16 → 24 (env: `OPENMARQUEE_R106_TOPUP_CAP`) | hdmi.rs:8482-8483 | FPS | TRIVIAL | 2–5 fps on dual-1080p transitions; r106 hit 22 fps at cap=16 | LOW (must verify CAPTURE doesn't saturate, r73 finding) |
| 8 | Conditional from-slide re-prime in BeginTransition — skip if not evicted | ipc_main.rs:2997-3001 | FPS | SMALL | 20–40 ms off transition entry latency (1–2 % fps) | MEDIUM (need defensive fallback at paint time) |
| 9 | GBM scanout BO chain 3 → 2 (DRM_FORMAT_MOD_LINEAR pool) | hdmi.rs:750-755 implicit | CMA | SMALL | 7.91 MB CMA at 1080p | MEDIUM (vsync-miss stutter risk; monitor frame-drop rate) |
| 10 | Skip transition `layer_fbo` on iris (no text overlay) | hdmi.rs:8443 + bake_slide_to_fbo iris arm | CMA | SMALL | 8 MB peak CMA during iris transitions | LOW |

**Why these 10:** items 1+2 together knock 50–90 MB off the memory
footprint (slide_caches LRU + CAPTURE pool); item 3 kills the visible
1.5–2.6 s freezes; items 4–7 squeeze the steady-state hot path
(~4 µs/frame combined = ~12 % of the 30 fps budget on text-heavy
slides); 7 reclaims the r106 cadence headroom; 8 tightens transition
entry; 9–10 give CMA headroom for future feature growth.

Items 11+ live in the per-bucket sections below for completeness; QA
can promote them as the top-10 lands.

## Framerate (target: 30 fps steady, <200 ms transition spike)

Steady-state runs 38–40 ms / 25 fps. The 30 fps budget is 33.3 ms.
Need to claw back ~5–7 ms / frame.

**F-1. Expand r65 async-preload coverage** (Priority #3 above).
`ipc_main.rs:2917` (BeginSlide) + `ipc_main.rs:2877` (text-over-video
recurse). The `r65` worker exists but BeginSlide's synchronous
`cache.load` is still the cold-cache fallback. Route cold cache.load
through the worker too, with a short ensure_preload_complete deadline
(50 ms) before falling through to sync. **Killing the multi-second
freezes is the single biggest user-visible framerate win.** Risk: the
worker's `prime_video_decoder_for_preload` must be Send-safe (it is —
PreloadSlide already uses it). DO-NOT regress the to-side decoder
persistence through BeginTransition.

**F-2. r106 topup cap retune** (Priority #7 above). `hdmi.rs:
8482-8483` — the `topup_count < 16` cap on `try_feed_nonblocking`.
r106 hit 22 fps on dual-1080p; the firmware admission depth is ~16
AUs but the call shape is per-tick at ~30 ms cadence, so up to 24
should be safe. Promote to env-var `OPENMARQUEE_R106_TOPUP_CAP=24`
for A/B benching before hardcoding. DO-NOT exceed CAPTURE pool depth
(r73 wedged on warmup_count=3 with CAPTURE saturated).

**F-3. Conditional from-slide re-prime** (Priority #8 above).
`ipc_main.rs:2997-3001`. The defensive re-prime runs UNCONDITIONALLY
every BeginTransition. Add cache-membership check
(`cache.video_decoders.contains_key(&from_id)`) — skip the re-prime
on the common path (decoder already present). Defensive fallback: if
the paint hook can't find the decoder, error-path emits the
preserved `"to decoder ... state missing"` line and Python recovers.

**F-4. Feed/drain wait budget retune** (`v4l2.rs:2840-2842`). The
100 ms default `OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS` covers ~3
frames on 1080p; tightening to 50 ms reduces tail-latency but raises
EAGAIN rate. Bench at 50/75/100 ms. Marginal win unless
back-pressure is actually the bottleneck.

**F-5. Audit per-tick `gl.get_uniform_location` calls**
(`hdmi.rs:1839-2076` pattern). The waste analysis found uniform
lookups inside the per-frame bake. `prewarm_shader_programs` at
`hdmi.rs:858` compiles programs but doesn't pre-cache uniform
locations. Adding per-program uniform-location cache at prewarm time
removes 4–8 `glGetUniformLocation` calls per frame (~1–2 µs).

## Memory (target: <10 MB swap, RSS bounded under playlist cycling)

**M-1. `slide_caches` HashMap LRU cap** (Priority #1 above).
`hdmi.rs:434`. Currently an UNBOUNDED HashMap; comment at line
427-430 acknowledges "If future workloads need eviction, swap to
LruMap." Per-entry ~4.2 MB at 1360×768 (glyph alpha bitmaps +
textures + first_frame_tex). Cap at 12 entries; evict oldest on
overflow. Drain via existing `free_slide_render_cache` helper —
single source-of-truth for GL handle cleanup. **40–80 MB swap
reduction on long playlists**, eliminates the page-churn baseline.

**M-2. V4L2 CAPTURE buffer pool 4 → 2** (Priority #2 above).
`video_decode.rs:344-347`. The `request_buffers(V4L2_BUF_TYPE_VIDEO_
CAPTURE_MPLANE, 4, …)` was sized for "4 reference frames" per docs
but bcm2835-codec h.264 main-profile DPB depth is 2–3. **5.93 MB CMA
+ 6–10 MB RSS per decoder.** Bench-verify NO decode stalls on
real FYS playlists (especially high-motion content). The r106 docs
flag this — line  comment notes the "4 reference frames" sizing was
never validated against actual codec behavior. CMA + RSS double win.

**M-3. COLR emoji bitmap soft cap** (`glyph_cache_colr.rs`). The 96×96
RGBA cells are 36 KB each; no per-emoji eviction cap. Worst case 1000
unique emoji × 36 KB = 35 MB. Add a per-page LRU cap of 256 cells
(9 MB ceiling). LOW risk — same LRU pattern as the MSDF cache.

**M-4. Drop unused `image_slide_tex_cache` cap to 4** (`image_slide_
tex.rs:54`). Currently LRU cap=6 ≈ 12 MB baseline. FYS slide cycle
rarely revisits more than 2–3 image slides in the working set;
cap=4 saves 4–8 MB. Trade: cold image slides re-decode PNG (~80 ms,
already off-thread via Task #168 worker). Acceptable.

**M-5. Pool `motion_states` Vec** (Priority #5 above). `hdmi.rs:
7949-7964`. Per-frame `Vec::collect()` for 4-layer text slides =
4 malloc+free/tick. Pool a reusable `Vec<MotionState>` in
`EglSession`, `.clear() + .extend()`. ~2-4 µs/frame.

## CMA (target: ≥ 50 MB headroom; current ~92 MB)

**C-1. V4L2 CAPTURE buffer pool 4 → 2** (same as M-2 + Priority #2).
5.93 MB CMA per decoder; 12 MB total on dual-decoder transition. The
single biggest CMA win.

**C-2. GBM scanout BO chain 3 → 2** (Priority #9 above). `hdmi.rs:
750-755`. 7.91 MB at 1080p. Trade: vsync-miss tolerance drops; under
heavy load a stutter risk emerges. Monitor frame-drop rate post-
change. MEDIUM risk; revisit after F-1 (async preload) lands so the
transition path doesn't have the freeze that would amplify the
stutter risk.

**C-3. Skip `layer_fbo` on iris/fade transitions** (Priority #10).
Iris and fade have no text layer overlay; the layer_fbo is allocated
but unused. Conditional allocation in `prepare_bake_fbo_pair` skips
it for transitions where source/dest endpoints are both opaque
(detect via TransitionEndpoint discriminant). 8 MB peak CMA during
iris/fade transitions. r106 caches the FBO pair across transition
boundaries so consecutive iris hits = 100 % cache hit.

**C-4. Reclaim 16 MB from kernel CMA reservation (cma=256M → 240M)**.
Boot cmdline only; not in repo. Only safe AFTER C-1 + C-2 land (peak
CMA must stay <190 MB). Gives 16 MB back to userspace heap (reduces
swap pressure indirectly).

## Waste (per-frame allocations, env reads, redundant work)

**W-1. Cache `cover_quad_vbo` by (frame_w, frame_h)** (Priority #4 +
WASTE finding 10). `hdmi.rs:8610`. Computes aspect-fit NDC coords +
uploads VBO every video bake tick. Cache in EglSession, update only
on dims_changed.

**W-2. Cache 3 env-var reads in session** (Priority #6 + WASTE
findings 1, 3, 6). `preload_mode()`, `OPENMARQUEE_BOUNDARY_TRACE`,
`OPENMARQUEE_FIRSTFRAME_PROFILE` — all read on every paint. Cache at
session Open (none change mid-session). ~1.5 µs/frame.

**W-3. Pool `motion_states` Vec** (covered as M-5 + Priority #5).

**W-4. `glyph_cache.poll_completions()` drain via thread-local pool**
(`hdmi.rs:3443-3447`). `.drain().collect()` allocates per call. Rare
fire (~1 per session per the comment) but MEDIUM risk because the
cache lifecycle is subtle — verify drain ordering vs slide_caches
free in the LRU eviction path before fixing.

**W-5. NO WASTE in DMABUF / MMAP paths.** Confirmed by the waste
analysis: DMABUF EGLImage cache (r101) is correct + lazy; MMAP plane
uploads are the only CPU copy in that path + load-bearing.

## Process notes

- **Bench before/after on fireplacesign for every commit.** No
  speculative pushes; measured wins only.
- **Don't co-land with Path A iter-2 / regression guard** (
  openmarquee-code's WIP on `task/v2v-r106-decouple-2026-06-14`).
  Base perf branches on `task/v2v-r106-decouple-2026-06-14` AFTER her
  iter-2 + guards land so the v2v fix is the foundation.
- **Branching:** one perf branch per item (`perf-{bucket}-{name}-
  2026-06-14`) keeps the regression-test surface tractable + lets QA
  revert any single item.
- **Sacred subagent review before each commit** (per AGENTS.md).
- **Regression-test the v2v fix on every perf change** — re-run QA's
  transition_tex_probe + framerate harness; reject on side=a/b
  luma<30 OR delta_ms>1000.
- **No soaks** (per dispatch + standing rule). Smoke + bench only.
- **Order of attack** (suggested): M-1 (slide_caches LRU) → W-1, W-2,
  W-3 (steady-state hot-path waste) → F-3 (conditional from re-prime)
  → F-2 (topup cap retune) → C-1+M-2 (CAPTURE pool) → F-1 (async
  preload) → C-2 (GBM 3→2) → C-3 (layer_fbo skip) → C-4 (CMA cmdline).
  Reorders OK based on bench surprises.
