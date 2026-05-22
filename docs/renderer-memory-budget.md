# Renderer Memory Budget

**Spec ref:** §8.1 (memory bounded), §8.2 (no leaks), §11 (Pi Zero 2 W
target). This doc satisfies the §8.1 "defensible budget breakdown
before writing the first line of compositor code" requirement —
landing late in the rewrite arc as a post-hoc audit + canonical
reference for slice 12(b) instrumentation and slice 12(c) soak gate.

The numbers below are *targets*. The companion runtime instrumentation
(slice 12(b)) emits the *actuals*, and the soak harness (slice 12(c))
gates on the deltas across an extended run.

## 1. Hardware ceiling

Pi Zero 2 W as canonical target (per §11):

| Resource          | Total    | Notes                                       |
| ----------------- | -------- | ------------------------------------------- |
| Physical RAM      | 416 MB   | Spec; measured `MemTotal`: 426 MB           |
| CMA carveout      | 256 MB   | Kernel-reserved DMA-coherent region         |
| Slab (kernel)     | ~42 MB   | Measured at idle with backend running       |
| Page tables / fw  | ~10 MB   | Estimated                                   |
| **Userspace**     | ~370 MB  | RAM minus kernel/slab — what we get to use  |

CMA is the binding wall in practice: every GBM scanout BO, every
dmabuf-imported texture, and every video-decoder ring entry comes
out of CMA — *not* userspace heap. A renderer that respects the heap
target but blows CMA still OOMs.

## 2. Budget breakdown — steady state

FREE YOUR SIGN canonical reel (per §11), 1080p × 30 fps,
post-Python-renderer retirement (Rust process owns the budget).

### 2a. CMA — target ≤ 90 MB (35% of 256 MB)

| Allocation                      | Size       | Count | Subtotal |
| ------------------------------- | ---------- | ----- | -------- |
| GBM scanout BOs (1080p RGBA)    | 8.3 MB     | 3     | 25 MB    |
| Scene FBO renderbuffer (1080p)  | 8.3 MB     | 1     | 8 MB     |
| Glyph atlas (static + dynamic)† | ~6 MB      | 1     | 6 MB     |
| Pattern texture cache           | ~0.3 MB    | 6     | 2 MB     |
| Image-bg texture cache          | 8.3 MB     | ≤4    | ≤33 MB   |
| Video decoder ring (720p H.264) | ~3 MB      | 4     | 12 MB    |
| **CMA target**                  |            |       | **~86 MB** |

*Note:* `MAX_TEXTURE_SIZE = 2048` and `MAX_RENDERBUFFER_SIZE = 2048`
on vc4 V3D 2.1 (per spike data §6) — bound 1080p single-attachment
textures and FBOs. We're at 1920 × 1080, comfortably under the limit.
Above 2048 in either dim would force tiled compositing.

GBM scanout chain: 3 BOs explicitly tracked by `EglSession` at the
slice-9d N-2 rotation (`scanout_prev_bo` + `scanout_current_bo` +
the new BO acquired at this frame's `lock_front_buffer`). The
underlying libgbm pool may rotate 3-4 BOs internally; we hand back
each prev BO to libgbm immediately after `drmModeRmFB`, so the
high-water mark is bounded.

Image-bg texture cache: `image_bg_cache: LruMap<PathBuf, ...>` in
`EglSession`. **LRU-evicted on insert at the §4 hard cap of 6
entries** (eviction policy landed 2026-05-08; see §6 risk item #2
for the full surface + cross-platform test set in
`renderer/src/lru.rs`). The FYS reel runs with ≤4 distinct images
in practice, so the hard cap of 6 sits as headroom over the
typical working set rather than as a binding constraint.

Glyph atlas (†): the `~6 MB` single-atlas figure predates the SDF
arc and Bug 3. The text path now uses a build-time **static MSDF
atlas** (`sdf_atlas_gl.rs`, RGB888, per-font) plus runtime **dynamic
atlas page(s)** (`atlas_page.rs`, Bug 3 Slice 3B): an MSDF
font-fallback page and a COLRv1 emoji page, each a 2048×2048 RGBA8
GPU texture (~16 MB). Every dynamic page is bounded — a free-list +
LRU eviction (Slice 3C; see §6.4) recycles cold cells rather than
growing the texture. The `~6 MB` target therefore undercounts the
current architecture; this row is flagged for re-measure (§7) once
slice-12(b) instrumentation reports glyph-atlas actuals.

### 2b. Heap (Rust process malloc) — target ≤ 60 MB

| Allocation                      | Size      |
| ------------------------------- | --------- |
| Rust runtime + jemalloc/system  | 12 MB     |
| Mesa userspace state            | 18 MB     |
| Content/playlist parse cache    | 4 MB      |
| Reel resolution intermediates   | 2 MB     |
| GLES2 program objects + IR      | 4 MB     |
| PNG decode scratch buffers      | 4 MB peak |
| Stack (main + worker threads)   | 8 MB      |
| **Heap target**                 | **~52 MB** |

### 2c. Coexisting backend (FastAPI) — observed ~80-100 MB

The Rust renderer lives as a sidecar process to the Python FastAPI
backend (per §11 process-boundary decision). Backend RSS is not the
renderer's budget but it counts against the 416 MB ceiling.

Measured during spike (default config, shader off): backend ~87 MB
RSS, ~187 MB CMA — but the CMA was the old Python renderer that the
rewrite retires. **Projection** for post-retirement: FastAPI alone
~80-100 MB RSS / ≤10 MB CMA. Re-measure after Python renderer cutover
and revise this section.

### 2d. Total steady-state

| Component                 | Budget      |
| ------------------------- | ----------- |
| CMA (renderer)            | 90 MB       |
| RSS (renderer process)    | 80 MB       |
| RSS (backend FastAPI)     | 100 MB      |
| Kernel + slab             | 50 MB       |
| Page cache + free         | ≥90 MB      |
| **Total**                 | **≤320 MB / 416 MB** |

(Renderer RSS = heap §2b + Mesa userspace + thread stacks ≈ 80 MB.)

Headroom: ~96 MB. Margin for transients (PNG decode burst, image-bg
swap, snapshot capture, glyph atlas growth on long playlists,
transition FBO spike — see §3).

## 3. Budget breakdown — peak

Worst-case concurrent pressure during a transition + image-bg swap
+ on-demand snapshot capture. The transition path is the dominant
spike: `render_transition_animated_in_session` allocates **three**
1080p RGBA FBOs (`scene_fbo_a` + `scene_fbo_b` + `layer_fbo`) for
the duration of the transition, freed on completion. Steady state
holds only the persistent `scene_fbo`; transitions briefly add ~25 MB
CMA on top.

| Spike source                    | Δ CMA     | Δ Heap    |
| ------------------------------- | --------- | --------- |
| Transition FBOs (3 × 1080p)     | +25 MB    | 0         |
| Image-bg cache miss + reload    | +8 MB temp | +4 MB PNG decode |
| Snapshot PBO readback           | +8 MB     | +4 MB encode |
| Video keyframe decode burst     | +6 MB     | 0         |

Peak CMA (transition + capture concurrent): ~127 MB / 256 MB (50%).
Peak total RSS+CMA+kernel: ~330 MB / 416 MB (79%). Headroom at peak:
~86 MB.

The transition + capture concurrent case is unlikely (capture is
gated on slide-boundaries per slice 11, transitions are inter-slide)
but the budget tolerates it.

## 4. Hard limits — never exceed

| Resource                      | Hard ceiling | Reason                       |
| ----------------------------- | ------------ | ---------------------------- |
| CMA used (system-wide)        | 250 MB       | 1080p@60 + atlas-SB baseline; see note below |
| Total RSS (renderer process)  | 100 MB       | Backend headroom             |
| Single texture/FBO dimension  | 2048 px      | vc4 hardware limit           |
| Concurrent FBOs allocated     | 8            | Mesa state-tracker pressure  |
| GBM BO chain depth (explicit) | 4            | CMA cost vs latency tradeoff |
| Image-bg cache entries        | 6            | 6 × 8 MB = 48 MB CMA cap (LRU eviction landed in slice 12 image-bg)        |

**CMA ceiling note (2026-05-10):** Raised from 200 MB to 250 MB
after the §8.2 1h soak (atlas-sb-2026-05-09 tip = 7417ae0,
canonical FYS reel @1080p@60 forced) measured CmaUsed=234.9 MB
at pass=0 (first-frame allocations: bake atlas + per-slide
bg-cache + GBM BO chain) and 225.3 MB at pass=97. The cma slope
was DECREASING (-11.4 MB/h) over the run — no leak; the
baseline is structural for the 1080p+atlas-SB architecture.

The prior 200 MB ceiling pre-dated the atlas-SB redirect (single
2048×2048 atlas adds ~16 MB) AND the bg-cache prewarm (per-slide
bg cache; ~12 MB at 1080p×3 slides cached) AND the 1080p-forced-
mode requirement (was 1024×768 EDID-default, needs ~×1.4 more
GBM BO area). The pre-existing "OOM-killer trips ≥220 MB" note
was empirically wrong on this Pi (1h run held 225-234 MB stable
with no OOM); 256 MB CMA carveout is the absolute kernel-side
ceiling, 250 leaves ~6 MB headroom over the observed first-frame
maximum.

If a future architectural change pushes the baseline higher,
re-measure on a fresh 1h soak and update this row alongside
the change.

## 5. Verification methodology

Slice 12(b) lands runtime instrumentation that emits at session
boundaries (open / per-pass / close):

```
[mem] <label> vm_rss=AA.AMB vm_data=BB.BMB swap=CC.CMB cma_used=DD.DMB
```

Slice (b-1) covers the /proc-derived subset (above). The GPU-side
counters (BO/FB/texture/FBO) are slice (b-2) and append to the same
line shape — soak parsers should regex-extract the keys they need
rather than positional-parse, so unknown fields don't break older
parsers.

Sources:

- `VmRSS`, `VmData`, `VmSwap` (a.k.a. `Swap` in the soak gate
  shorthand) from `/proc/self/status` (one read, KB → MB).
- `CmaUsed` = `CmaTotal − CmaFree` from `/proc/meminfo`.
- BO/FB counts from `/sys/kernel/debug/dri/0/state` (when accessible)
  or from internal counters in `EglSession`.
- Texture/FBO counts from internal counters incremented at
  `gen_texture` / `gen_framebuffer` / `delete_*` paths.

Slice 12(c) is the soak harness: launch FYS reel for a configurable
duration (default 6 hours per §8.2 starting point), scrape the `[mem]`
lines, gate on:

- **No monotonic growth** in any of `VmRSS`, `VmData`, `Swap`, `CmaUsed`
  across the full soak (linear-regression slope test, |slope| <
  threshold). Per §8.2 this is the leak gate.
- **Steady-state values within budget** in §2d. Mid-soak averages
  (excluding warmup window) below the targets.
- **Peak values within hard limits** in §4. No single sample exceeds.
- **30 fps sustained** across the soak (per §11). Frame timing
  embedded in the same `[mem]` line shape or sibling line.
- **No OOM kills** (per §11). Process must still be running and
  responsive at end-of-soak.

## 6. Risks tracked for future slices

1. **Per-transition FBO bake cache (slice 9(d) followup).** If a
   transition's source/dest snapshots become persistent across
   adjacent slides, peak FBO count grows. Currently lazy-bake
   per-transition; this doc presumes that cadence holds.

2. **Image-bg cache eviction — LANDED 2026-05-08.** `image_bg_cache`
   is now a `crate::lru::LruMap<PathBuf, ...>` with the §4 hard cap
   of 6 entries enforced via LRU eviction on insert. The eviction
   policy is unit-tested cross-platform in `renderer/src/lru.rs`
   (8 tests covering: under-cap no-evict, at-cap evicts LRU, get
   touches MRU, replace returns prior value no-evict, capacity-0
   clamp, drain-then-reinsert, cycle-N>cap keeps recent only,
   touch-then-insert evicts other-entry). Caller (draw_image_bg)
   handles InsertOutcome: deletes any returned evicted-LRU
   texture + replaced texture via the gl context.

3. **Video decoder ring depth.** H.264 main-profile B-frame chains
   can require 4-deep DPB. Raising to 6-deep is +6 MB CMA. Item #8
   (c+) work re-evaluates this on real hardware decode.

4. **Glyph atlas growth — BOUNDED, Bug 3 Slice 3C (2026-05-20).** The
   dynamic glyph atlas (`renderer/src/atlas_page.rs`) is a fixed
   2048×2048 page with a bump allocator backed by a free-list; it does
   **not** grow monotonically. When a page fills, `GlyphCache`
   (`renderer/src/glyph_cache.rs` — `evict_lru_ready`) evicts the
   least-recently-used `Ready` glyph and `AtlasPage::free_slot`
   recycles its cell. A high-character-variance reel exhausts the page
   slot count (1764 MSDF / 441 COLR) and then recycles cold cells
   instead of wedging or growing; an evicted glyph needed again is a
   plain cache miss. The canonical FYS reel has a bounded character
   set and saturates well under capacity.

5. **Mesa userspace state under shader churn.** Each shader
   compile + program object adds to Mesa's internal state. The
   current shader set is small (≤10 fragment programs); a future
   spec expansion (per-slide custom shaders, etc.) would put
   pressure on the heap.

## 7. Re-evaluate cadence

This doc is a target document, not a measurement record. Re-evaluate
the budgets when:

- Item #8 (c+) lands real H.264 decode (revises §2a video decoder
  row and §6 #3).
- Slice 9(d) followup lands per-transition FBO bake cache (revises
  §6 #1).
- Resolution targets change (revises every line item — currently
  1080p frozen per §11).
- The SDF arc + Bug 3 dynamic glyph atlas have landed (build-time
  static MSDF atlas + runtime dynamic MSDF/COLR pages, LRU-evicted) —
  the §2a glyph-atlas row needs a re-measure.
- Soak measurements (slice 12c) come back outside the §2d totals.

The instrumentation slice (12b) is what tells us whether the budget
holds. The soak slice (12c) is what tells us whether it holds *over
time* under load. Both are required before claiming v1-spec §11
acceptance.
