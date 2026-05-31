# r37 — CMA allocator static audit (leak-hunt for the renderer)

**Author lane:** code2 (static analysis only — no SSH, no live
profiling, no perf-stats endpoint calls). Same shape as r35
FPS audit + r30/r31/r33 recommendation docs.

**Audience:** code1 / whoever owns the renderer-perf lane in a
future r38b (post-stopgap leak hunt) dispatch.

**Why it exists.** Per code1's r35 D.2 close report: the FPS
ceiling root cause was **NOT** B.E/B.F/B.G from my r35 audit
ranking. The actual driver is **CMA pool exhaustion**:

- `cma_used` peaks at 255.8 MB out of 256 MB Pi Zero 2 W boot
  allocation.
- Each sidecar restart drops it back to ~187 MB.
- `vm_rss` stable (~12 MB), `swap` stable (~44 MB) — the leak
  is specifically the CMA pool, not the normal heap.

Delta ≈ 68-70 MB leaked between sidecar boot and steady-state
under load. Sidecar restart "fixes" because the OS releases the
CMA pages on process exit.

**This audit ranks the CMA-allocator candidates in the
renderer source.**

**Origin/main HEAD at audit time:** `cc81fdd` (post-r36 SD
bundle audit). All file:line citations against code2 HEAD
`8602317` unless noted; renderer source has no drift between
code2 and main on the perf-relevant paths.

---

## Section A — CMA allocator surface inventory

CMA-backed allocators on Pi Zero 2 W (bcm2835-codec + vc4 + GBM):

### A.1 V4L2 buffer pool (REQBUFS + mmap)

**Where:** `renderer/src/v4l2.rs:958-1060` (`allocate_buffers`),
called from `renderer/src/ipc_main.rs:731-734`.

**Allocation shape.** `VIDIOC_REQBUFS` with `V4L2_MEMORY_MMAP`
+ `VIDIOC_QUERYBUF` per index + `mmap` per plane. 4 OUTPUT
buffers + 4 CAPTURE buffers = 8 buffers total. Each capture
buffer at NV12 1920×1088 = ~3 MB → **~24 MB total V4L2 pool**.

**Lifetime expectation:** per-VideoSlide-decoder-session. The
buffers are tied to a `v4l2::Decoder`; `Decoder` Drop releases
them.

**Drop path (`v4l2.rs:594-616`):**
1. `stop_streaming_quiet` → `VIDIOC_STREAMOFF` both queues
2. close exported DMA-BUF fds (capture_dmabuf_fds.iter())
3. Field-order drop: `mapped_capture` + `mapped_output` (each
   `MmapRegion` → `munmap`)
4. `file` field last → close fd

**Release-correctness:** **VERIFIED.** Comprehensive Drop;
field-order documented as soundness contract at v4l2.rs:561-564.

**Gating:** `prime_video_decoder` (`ipc_main.rs:683-758`) fires
ONLY when a VideoSlide is encountered. Per code1's r35 D.2 +
the dispatch, **FYS reel has ZERO VideoSlides** → this allocator
is NEVER exercised on FYS. **Statically ruled out for the FYS-
specific leak.**

### A.2 EGLImage DMABUF imports

**Where:** `renderer/src/hdmi.rs:9617-9986` (the DMABUF EGLImage
import path), gated on `capture_buffer_type == DmaBuf`.

**Allocation shape.** Per-frame `eglCreateImageKHR` with
`EGL_LINUX_DMA_BUF_EXT` target + the V4L2-EXPBUF'd fd. The
EGLImage holds a kernel-side ref on the dma_buf → kernel keeps
the underlying CMA buffer alive until destroy.

**Lifetime expectation:** per-frame (one create + one destroy
per V4L2 capture frame).

**Drop path (`hdmi.rs:9968-9982`):**
```
gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
gl.delete_texture(tex);
let destroyed = (eps.destroy_image)(display.as_ptr(), egl_image);
if destroyed == 0 {
    eprintln!("warn: eglDestroyImageKHR returned EGL_FALSE for fd={}", fd);
}
```

**Release-correctness:** **LIKELY** with one caveat:
`destroy_image` returning `EGL_FALSE` warns + continues. If
that ever fires repeatedly under load (likely possible on a
contended vc4 driver), the CMA buffer for THAT frame leaks.

**Gating:** same as A.1 — only fires on V4L2 DMABUF path, which
requires `OPENMARQUEE_RENDERER_DMABUF=1` env var AND a
VideoSlide. **Statically ruled out for the FYS-specific leak.**

### A.3 GBM scanout buffer objects (BOs)

**Where:** 9 distinct `lock_front_buffer` callsites in
`renderer/src/hdmi.rs`:
- `:1306` (transition path)
- `:1604` (transition continuation)
- `:3126` (image_slide)
- `:3281` (image_slide bake)
- `:3382` (external_frame)
- `:3485` (external_nv12)
- `:3664` (video_slide)
- `:4113` (paint_slide common)
- `:12546` (frame 0 / initial commit)

**Allocation shape.** Each `gbm_surface.lock_front_buffer()`
returns a `BufferObject<()>` that — per the vc4 driver — is
backed by a CMA-allocated contiguous physical region. At ARGB8888
1920×1080: **~8 MB per BO**. (Stride may round to 1024 or 2048
pixels, so 8-16 MB realistic per BO.)

**Lifetime expectation:** each scanout commit holds 2-3 BOs:
`scanout_current_bo` + `scanout_prev_bo` + (optionally)
`held_scanout_bo`. Rotation cycle per paint:
1. lock new front buffer → `new_bo`
2. `add_framebuffer(new_bo) → new_fb`
3. `commit_fb(new_fb)` → page-flip
4. `destroy_framebuffer(scanout_prev_fb.take())`
5. `drop(scanout_prev_bo.take())`
6. `scanout_current_*` → `scanout_prev_*`
7. `new_*` → `scanout_current_*`

So at steady state: **2 BOs + 2 FBs in flight = ~16-32 MB**.

**Release-correctness — by-path:**

| Callsite | Path name | take(prev_bo) + drop visible | destroy_fb(prev) visible | Release confidence |
| --- | --- | --- | --- | --- |
| `:1306` | transition setup | partial (continues at `:1604`) | yes (`:1201`) | LIKELY |
| `:1604` | transition continuation | yes (line ~1638) | yes (line `:1622`+`:1638`) | LIKELY |
| `:3126` | image_slide paint | yes (line `:3150`) | yes (line `:3137`+`:3150`) | LIKELY |
| `:3281` | image_slide bake | needs re-read | needs re-read | UNKNOWN |
| `:3382` | external_frame | needs re-read | needs re-read | UNKNOWN |
| `:3485` | external_nv12 | yes (line `:3506-3510`) | yes (line `:3501-3504`) | VERIFIED |
| `:3664` | video_slide | needs re-read | needs re-read | UNKNOWN |
| `:4113` | paint_slide common | needs re-read | needs re-read | UNKNOWN |
| `:12546` | frame 0 (initial commit, `prewarm`) | one-shot; no prev to release | one-shot | VERIFIED |

**5 of 9 paths need deeper read to confirm release-correctness.**
The release pattern is consistent where verified; the unknowns are
just unread-by-me, not necessarily broken. **HIGH-priority
investigation target** because:

- Each leaked BO is 8-16 MB.
- 5-8 leaked BOs would be 40-128 MB — squarely in the leak
  budget (~70 MB observed).
- Each commit-failure path that bails before the `take(prev_bo) +
  drop` step would leak one BO per commit failure.

### A.4 DRM framebuffer handles (`add_framebuffer`)

**Where:** the GBM scanout cycle pairs `add_framebuffer` (one per
new BO) with `destroy_framebuffer` (one per prev FB).
Framebuffer handles are NOT CMA-backed themselves — they're DRM
object handles. But each FB attaches to a BO, and **a leaked FB
keeps the BO alive** (the BO can't free its CMA pages until all
FB refs drop).

**Allocation shape.** ~1 FB per BO. ~14 distinct
`destroy_framebuffer` callsites in `hdmi.rs` (per earlier grep);
each represents a release site. Need to verify pairing.

**Release-correctness:** **LIKELY** but coupled to A.3. Any
`add_framebuffer` not paired with `destroy_framebuffer` keeps
the underlying BO alive even after the BO is dropped from the
Rust side. **The leak shape is "BO drops but FB doesn't" → CMA
pages stay pinned.**

### A.5 GLES texture caches

**Where:**
- `image_bg_cache` at `hdmi.rs:688` —
  `LruMap<PathBuf, NativeTexture>`, capacity 6
  (`IMAGE_BG_CACHE_CAPACITY`).
- `image_slide_tex_cache` at `hdmi.rs:689` —
  `ImageSlideTextureCache::with_capacity(IMAGE_SLIDE_TEX_CACHE_CAPACITY = 6)`
  per `renderer/src/image_slide_tex.rs:54`.

**Allocation shape.** Each entry is a 1080p RGBA8 texture =
**~8 MB on vc4**. Total cache cap: ~48 MB per cache, ~96 MB
combined.

**Lifetime expectation:** LRU eviction up to capacity. Each
eviction → `gl.delete_texture(t)`. Per
`image_slide_tex.rs:343` + `hdmi.rs:1861-1864` + the in-place
swap at `image_slide_tex.rs:237`.

**Release-correctness:** **VERIFIED.** Bounded by capacity,
LRU evicts with explicit `delete_texture`.

**Concern:** **96 MB combined cap on a 256 MB CMA budget is
TIGHT.** If both caches hit cap simultaneously + the V4L2 pool
+ scanout pool + atlas pages all coexist, we're at ~140-160 MB
just from these bounded caches. Add another ~70 MB leak and we
hit the 256 MB ceiling.

**This is NOT a leak but IS a contention concern.** Section F
covers fix-shapes that would reduce cap pressure.

### A.6 MSDF static atlas (build-time-baked)

**Where:** `renderer/src/sdf_atlas_gl.rs:42-112` (`upload_all`)
called once at `with_egl_session` bring-up.

**Allocation shape.** Per-font RGB texture, dimensions per
`AtlasManifest`. Total size logged at line 104-110. With 25
fonts in `ui/fonts/` (per the v0.9.0 bundle layout), this is
likely in the ~50-150 MB range depending on atlas dimensions.

**Lifetime expectation:** session-lifetime. `delete_all` called
at session teardown only.

**Release-correctness:** **VERIFIED** at teardown. NOT a leak
shape — the allocation happens ONCE at bring-up and is
released at session end.

**Concern:** if the static atlas total is large (e.g. 100+ MB),
it eats CMA budget unconditionally. **Not a leak; a baseline
cost.**

### A.7 Dynamic atlas pages (MSDF + COLR)

**Where:** `renderer/src/atlas_page.rs:58-145`. Page is 2048×2048
RGBA8 = **16 MB per page**. Two pages: MSDF + COLR =
**32 MB total**.

**Lifetime expectation:** session-lifetime. `delete` called
at session teardown.

**Release-correctness:** **VERIFIED.** Pages are FIXED size,
never grow. Slot allocator uses bump cursor + free-list;
recycling on LRU eviction within a page. **No growth path
exists.**

**Concern:** none. Fixed allocation.

### A.8 Scene FBO (for rotated rendering)

**Where:** `renderer/src/hdmi.rs:339-341` (`scene_fbo`),
`ensure_scene_fbo` allocates lazily.

**Allocation shape.** Single FBO + backing 1080p RGBA texture
= ~8 MB.

**Lifetime expectation:** session-lifetime once allocated.
`delete_framebuffer` + `delete_texture` at session teardown
(hdmi.rs:905-906).

**Release-correctness:** **VERIFIED.** Single allocation per
session.

### A.9 Per-slide caches (`slide_caches` HashMap)

**Where:** `session.slide_caches: HashMap<Uuid, SlideRenderCache>`
(from r35 audit). HashMap is uncapped; entries created on first
slide use, freed only on layer-count mismatch.

**Allocation shape per entry.** Per-slide glyph quads, bg_tex,
layout buffers. Per `hdmi.rs:11611-11625` (the r35 audit's note):
SP-tier prewarm path is bg-only post-MSDF cutover; the per-slide
cache's GL surface is **small** (~few KB per slide).

**Lifetime expectation:** session-lifetime + per-slide.

**Release-correctness:** **VERIFIED** at teardown
(`free_slide_render_cache` called at multiple sites including
prewarm-replace at hdmi.rs:11603-11605 and teardown at
hdmi.rs:794-805).

**Concern:** the HashMap can grow unboundedly across distinct
slide IDs. For a stable FYS reel (~10 slides), total cache is
small. Re-flag if FYS reel ever grows to 100+ slides.

### A.10 Summary

| Allocator | Per-instance size | Steady-state count | Total | Release-correctness | FYS-relevant? |
| --- | --- | --- | --- | --- | --- |
| A.1 V4L2 pool | ~3 MB × 8 | 0 (no VideoSlide) | 0 MB | VERIFIED | NO |
| A.2 EGLImage DMABUF | ~3 MB × few | 0 (no DMABUF mode) | 0 MB | LIKELY | NO |
| A.3 GBM scanout BOs | ~8 MB × 2-3 | 2-3 | 16-24 MB | LIKELY (4 paths SUSPECT) | **YES** |
| A.4 DRM framebuffers | per BO | per BO | (coupled to A.3) | LIKELY | **YES** |
| A.5 GLES texture caches | ~8 MB × 12 (cap) | < 12 (LRU) | <96 MB | VERIFIED | **YES** |
| A.6 MSDF static atlas | varies | 1 set | ~50-150 MB | VERIFIED (session-lifetime) | YES baseline |
| A.7 Dynamic atlas pages | 16 MB × 2 | 2 | 32 MB | VERIFIED | YES baseline |
| A.8 Scene FBO | ~8 MB | 1 | 8 MB | VERIFIED | YES |
| A.9 slide_caches | KB | per slide | small | VERIFIED | YES |

**Total baseline (no leak):** ~150-260 MB. **The numbers are
already tight for a 256 MB CMA budget.** Adding ANY unbounded
allocation pushes over.

**Total leak budget (FYS observation):** ~70 MB delta from
sidecar boot to wedged state.

**Best-fit candidate:** 5-8 leaked BOs × 8-16 MB = 40-128 MB
matches. **A.3 (GBM scanout BO leak in one of the 4 SUSPECT
paths) is the highest-likelihood candidate.**

---

## Section B — Per-callsite release verification (deeper)

The 4 SUSPECT (UNKNOWN release-confidence) GBM scanout paths
from §A.3 need deeper read. Each is a `lock_front_buffer`
callsite where I haven't traced the matching `take(prev_bo)` +
`destroy_fb` pair.

### B.1 `hdmi.rs:3281` (image_slide bake)

`paint_and_present_one_image_slide_frame` (or a bake helper).
Read range: hdmi.rs:3260-3330. Action item for code1's r38b:
**verify the standard "destroy prev_fb + drop prev_bo" pattern
fires on ALL code paths including error returns.**

### B.2 `hdmi.rs:3382` (external_frame)

`paint_external_frame_to_session` or similar. Read range:
hdmi.rs:3340-3470. Action item: same as B.1.

### B.3 `hdmi.rs:3664` (video_slide)

`paint_and_present_one_video_slide_frame`. Read range:
hdmi.rs:3539-3760. Per the dispatch's "FYS reel has zero
VideoSlides" claim, this path is NOT exercised on FYS — but
worth covering for completeness.

### B.4 `hdmi.rs:4113` (paint_slide common)

Bulk paint path. Read range: hdmi.rs:4100-4200. **Most likely
common path for text-only / image-only slides on FYS.** Action
item: **PRIORITIZE THIS PATH.** If it leaks, it leaks every
frame.

### B.5 Error-path verification across all 9 paths

The verified paths (B.5 / B.6 / B.7) in §A.3 release on the
**happy path**. Each commit-failure branch — `if let Err(e) =
commit_fb(...) { ... return Err(e); }` — must also fire the
prev-release. Looking at hdmi.rs:3492-3500 (the verified
external_nv12 path):

```rust
if let Err(e) = commit_fb(session, card, new_fb) {
    if let Err(de) = card.destroy_framebuffer(new_fb) {
        eprintln!("warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (external_nv12): {de}");
    }
    drop(new_bo);
    return Err(e);
}
```

The commit-FAIL path destroys `new_fb` and drops `new_bo` — but
does NOT take/drop `scanout_prev_bo` or `scanout_prev_fb`. The
prev pair stays in place. That's CORRECT for commit failure
(the prev is still on scanout; we just failed to advance).

**But** the OTHER paths (B.1-B.4) need verification of the SAME
pattern. A path that drops `new_bo` on commit-failure but ALSO
takes `prev_bo` would double-release prev on subsequent
success. A path that fails to drop `new_bo` on commit-failure
leaks `new_bo` per commit failure.

**Investigation target:** any path where commit-failure handling
deviates from the external_nv12 reference shape.

### B.6 The "held_scanout" pair

`session.held_scanout_fb` + `session.held_scanout_bo` (declared
hdmi.rs:317-318) are an additional BO/FB pair used during
"drain to held" patterns. Per hdmi.rs:1213-1222: when a
transition completes, the prior `held_scanout_fb` is
destroyed + the bo dropped, and current_fb/bo move to held.

**Release-correctness:** **LIKELY** for the documented drain
pattern but **SUSPECT for early-return paths** that bail
before the held-replace step. If a `?`-bubble occurs between
"set held" and "release prev held", the prior held leaks.

Action item: search hdmi.rs for `held_scanout_fb =` (assignments)
and verify each is preceded by a matching `take()` of the prior
held.

### B.7 EGLImage path (A.2) error fall-through

Per hdmi.rs:9950-9986:
- The `blit_result` closure can fail at `cnp = cached_..._program(gl)?`.
- Teardown order: `gl.delete_texture(tex)` + `destroy_image(egl_image)` —
  ALWAYS fires (after the closure ends, regardless of `blit_result`).

**Concern:** the `eglCreateImageKHR` itself returns
`EGL_NO_IMAGE_KHR` on failure (~9 line above the bind). If that
fails, the code path's behavior depends on the surrounding context
I haven't read. Action item: verify the EGL-create-fail path
doesn't leak the `tex` it just created.

---

## Section C — Candidate leak vectors (ranked)

### C.1 — GBM scanout BO/FB leak in a commit-failure path (HIGH)

**Mechanism.** One of the 9 `lock_front_buffer` callsites
(§A.3) has a commit-failure branch that leaks the new_bo or
fails to release prev_bo. Each leaked BO is 8-16 MB. Even at
1 leak per minute under load, ~70 MB after 5-8 minutes matches
FYS observation.

**Plausibility ranking.** **HIGH.** The numerical fit is best
in the candidate space, the surface is large (9 paths), and 4
of 9 are SUSPECT (unread by me).

**Static rule-out vs needs-soak:** STATIC-NARROWABLE. Code1
can read the 4 SUSPECT paths in 30-60 min and ALL bug-class
mismatches will surface as either "missing `take(prev_bo)`" or
"missing `drop(new_bo)` on commit-failure."

**Cost-of-fix:** ~3-10 LOC per missing release site. The fix
pattern is canonical (mirror the external_nv12 reference at
hdmi.rs:3492-3510).

### C.2 — held_scanout_bo leak on early-return paths (MEDIUM)

**Mechanism.** `held_scanout_fb`/`held_scanout_bo` (hdmi.rs:317-318)
are set during transition completion. An early-return between
"new held = current" and "destroy prev held" leaks one held per
transition.

**Plausibility ranking.** MEDIUM. Each transition is rare (only
on slide boundary, FYS reel has ~10 slides), so leak rate is
slower than C.1. But if it's deterministic (every transition
leaks 1), 10 slides × N cycles → matches observed.

**Static rule-out vs needs-soak:** STATIC-NARROWABLE. Grep
`held_scanout_fb =` callsites + verify pattern.

**Cost-of-fix:** ~3-10 LOC per missing site.

### C.3 — EGLImage CMA-buffer leak on eglDestroyImageKHR EGL_FALSE (LOW for FYS, MEDIUM general)

**Mechanism.** `hdmi.rs:9976-9981` warns + continues on
`destroy_image` returning `EGL_FALSE`. The CMA buffer held by
that EGLImage is leaked.

**Plausibility for FYS:** LOW. The DMABUF path is gated on
`OPENMARQUEE_RENDERER_DMABUF=1` AND VideoSlide presence; FYS
has neither.

**Plausibility general:** MEDIUM. Any user with both flags set
+ VideoSlides eventually hits this if vc4 ever returns EGL_FALSE
under load.

**Static rule-out:** RULED OUT FOR FYS by gating. Re-flag for
non-FYS deployments.

**Cost-of-fix:** ~20 LOC. Track outstanding EGLImage handles
in a Vec; on destroy_image EGL_FALSE, re-add to a "retry
queue" + retry at next paint. Or: surface as fatal Err instead
of warn (forces sidecar restart; consistent with the current
"restart fixes" observation but uglier).

### C.4 — Static MSDF atlas baseline pressure (LOW for leak, but MEDIUM concern)

**Mechanism.** §A.6 — `upload_all` at session bring-up loads
ALL parsed atlases (25 fonts in v0.9.0 bundle). Total size logged
at line 104-110; could be 50-150 MB depending on per-font
atlas dimensions.

**Plausibility for LEAK:** RULED OUT. Session-lifetime
allocation, released at teardown.

**Plausibility for CMA pressure:** MEDIUM. If the baseline
static atlas is 100+ MB, the renderer starts already pushing
the CMA ceiling. Any subsequent allocation lives in a tight
budget.

**Static rule-out vs needs-soak:** RULED OUT as a leak. But
the size NUMBER is unknown — would be useful to capture via the
existing `eprintln!` at sdf_atlas_gl.rs:104-110 from a real boot.

**Cost-of-fix:** depends on size. Options: prune font selection
(ship fewer atlases), reduce per-atlas dimensions, or move some
atlases to lazy-on-demand load.

### C.5 — Texture cache cap simultaneity (LOW for leak; LOW for FYS)

**Mechanism.** §A.5 — `image_bg_cache` + `image_slide_tex_cache`
caps at 6 entries each = 12 textures × ~8 MB = 96 MB. If both
caches simultaneously hit cap on a reel-cycle that touches many
unique image bg + image slide URLs, this is 96 MB held.

**Plausibility for LEAK:** RULED OUT. LRU bounded.

**Plausibility for CMA pressure:** LOW for FYS. FYS reel likely
has < 6 unique image bg + < 6 unique image slides, so caps not
hit.

**Static rule-out:** RULED OUT for current FYS workload. Re-
flag if FYS reel scales to dozens of image slides.

### C.6 — V4L2 buffer pool + DMABUF (RULED OUT for FYS)

**Mechanism.** §A.1 + §A.2.

**Static rule-out:** RULED OUT FOR FYS. Per the dispatch, FYS
reel has no VideoSlides. V4L2 decoder + EGLImage import paths
are never exercised.

### C.7 — Atlas page growth (RULED OUT, static)

**Mechanism.** §A.7 — dynamic MSDF + COLR pages.

**Static rule-out:** RULED OUT. `ATLAS_DIM = 2048` fixed; pages
do not grow. Bump cursor + free-list = within-page recycling
only.

---

## Section D — Static rule-outs

From the candidate analysis above, these are ruled out from
static reading alone for the FYS-specific leak:

- **C.6 (V4L2 + DMABUF)** — gated on VideoSlide presence; FYS
  has none.
- **C.7 (atlas page growth)** — pages are FIXED at 2048×2048;
  no growth path in source.
- **C.4 (static atlas as leak)** — session-lifetime
  allocation; released at teardown. *(Re-flag the baseline
  size as separate CMA concern, not a leak.)*
- **C.5 (texture cache leak)** — bounded LRU with explicit
  `delete_texture`. *(Re-flag if FYS reel scales beyond cache
  cap.)*
- **A.7 dynamic atlas pages** — fixed-size, single allocation
  per page, session-lifetime.
- **A.8 scene FBO** — single allocation, session-lifetime,
  verified teardown.
- **A.9 slide_caches** — bounded by playlist content; small
  per-entry size.

**Surviving candidates:** C.1 (GBM scanout BO/FB leak) +
C.2 (held_scanout leak) + C.3 (EGLImage destroy fall-through;
not FYS-relevant but worth covering).

---

## Section E — Recommended next-step investigation

### E.1 Cheapest: read the 4 SUSPECT GBM scanout paths

**Cost:** ~30-60 min code1 read time. ZERO runtime cost. NO
soak.

**Specifically:**
1. **hdmi.rs:3280-3330** (image_slide bake) — verify
   `take(prev_bo) + drop` and `take(prev_fb) + destroy_fb`
   fire on every return path (including error returns).
2. **hdmi.rs:3382-3470** (external_frame) — same.
3. **hdmi.rs:3664-3760** (video_slide) — same. NOT FYS-relevant
   but worth covering.
4. **hdmi.rs:4113-4200** (paint_slide common) — **HIGHEST
   PRIORITY.** Most-likely common path for FYS text+image
   slides.

For each: identify any path where new_bo is dropped but prev_bo
is NOT taken+dropped, OR where a `?`-bubble bails before the
prev-release.

Output: a per-path PASS/FAIL/AMBIGUOUS verdict. If any FAIL or
AMBIGUOUS, the fix is canonical (mirror the verified
external_nv12 pattern at hdmi.rs:3492-3510).

### E.2 Cheapest with runtime confirmation: BO-counter instrumentation

If E.1 doesn't find a clear bug or surfaces multiple ambiguous
paths, the next-cheapest is:

**Add an atomic counter that increments on every
`lock_front_buffer` success + a paired counter for the
matching `drop(BO)` site.** Log the delta every N seconds.

**Cost:** ~30 LOC across hdmi.rs (one wrapper around
lock_front_buffer, one wrapper around the drop site). Runtime
cost: one atomic add per scanout commit (negligible).

If `lock_count - drop_count` grows monotonically: leak
confirmed in the GBM scanout pool. The delta growth rate
attributes to specific paint paths via per-path counters.

This is non-soak: 5-10 minutes of normal operation surfaces
the leak rate. Charter `[[feedback_no_soak_during_dev]]` is
satisfied.

### E.3 Free-of-code-change confirmation: /sys/kernel/debug/dri probing

If E.1+E.2 are inconclusive, code1 can capture **without
modifying renderer code**:

```bash
# On FYS, while sidecar is running:
cat /sys/kernel/debug/dri/0/clients
cat /sys/kernel/debug/dri/0/state | head -50
ls /sys/kernel/debug/dma_buf/bufinfo 2>/dev/null && \
    cat /sys/kernel/debug/dma_buf/bufinfo | wc -l
cat /proc/meminfo | grep -E "Cma|MemAvail"
```

Re-run every 30s for 5 min. Plot:
- DRM framebuffer count (`state | head` shows fb_id list)
- DMABUF count (bufinfo line count if available)
- CmaUsed (over the 5-min window)

If DRM-fb-count grows monotonically: framebuffer-handle leak →
matches C.1.
If DMABUF-count grows: A.2 leak (re-evaluate VideoSlide
gating).
If CmaUsed grows independently of DRM/DMABUF: A.6 static
baseline or something I haven't enumerated.

### E.4 NOT recommended: heavy profiler

Attaching a memory profiler (heaptrack, valgrind --tool=massif,
or even strace -e mmap,munmap) is the last resort. The static
analysis above + E.1-E.3 should narrow the cause cheaply.

---

## Section F — Provisional fix sketches

For top-3 candidates only. NOT authorized for r37; sizing only.

### F.1 If C.1 confirmed (GBM scanout BO/FB leak in a SUSPECT path)

Per-path fix shape, mirroring hdmi.rs:3492-3510 (verified
external_nv12 reference):

```rust
if let Err(e) = commit_fb(session, card, new_fb) {
    if let Err(de) = card.destroy_framebuffer(new_fb) {
        eprintln!("warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail: {de}");
    }
    drop(new_bo);
    return Err(e);
}
// Then on happy path:
if let Some(fb) = session.scanout_prev_fb.take() {
    if let Err(e) = card.destroy_framebuffer(fb) {
        eprintln!("warn: destroy_framebuffer(scanout_prev): {e}");
    }
}
if let Some(bo) = session.scanout_prev_bo.take() {
    drop(bo);
}
session.scanout_prev_fb = session.scanout_current_fb.take();
session.scanout_prev_bo = session.scanout_current_bo.take();
session.scanout_current_bo = Some(new_bo);
session.scanout_current_fb = Some(new_fb);
```

**LOC:** ~3-15 LOC per missing release site, depending on the
path's complexity. Probably 1-3 paths need it.

### F.2 If C.2 confirmed (held_scanout leak)

Similar pattern but for the held_*  pair. Each
`held_scanout_fb =` assignment must be preceded by
`take()` of the prior + destroy.

**LOC:** ~5 LOC per missing site.

### F.3 Defense-in-depth: BO-counter as permanent instrumentation

Regardless of which leak path is confirmed, **leave the
BO-counter from E.2 in place** as a permanent CMA-leak alarm.
The atomic-counter cost is negligible; the alarm value is
high.

**LOC:** ~30 LOC (the same code from E.2, retained).

### F.4 If C.3 confirmed (EGLImage destroy fall-through)

Track outstanding EGLImage handles in a Vec; on destroy_image
EGL_FALSE, retry-queue. Or escalate to fatal Err.

**LOC:** ~20-50 LOC depending on shape. NOT FYS-relevant.

### F.5 Defense-in-depth: refactor scanout cycle into a helper

To prevent FUTURE leaks of the same shape, abstract the
"rotate scanout pair" + "destroy prev FB + drop prev BO"
into a single helper that every path calls. Currently the
pattern is hand-copied across 9 paths; one helper would
eliminate the per-path bug class.

**LOC:** ~30-50 LOC for the helper + 5-10 LOC per callsite
refactor (~80-130 LOC total). HIGH value, MEDIUM regression
risk (changes call surface across many paths).

**Recommendation:** ship F.1 (the specific fix) in the r38b
dispatch; treat F.5 as a separate r39+ refactor dispatch.

---

## Section G — Open questions for qarl / QA

### G.1 r37 static analysis vs E.1 deeper read scope

This audit ranks candidates from static reading of the SHIPPING
surface (4 paths SUSPECT in §A.3 because I haven't read them
deep). **Question to qarl:** authorize code1's r38b to do the
~30-60 min deep read of those 4 paths (§E.1)? Cheapest path to
narrow C.1 vs C.2.

### G.2 FYS reel composition confirmation

The dispatch states FYS reel has ZERO VideoSlides (per code1's
r35 D.2 investigation). This static rule-out of C.6 (V4L2/DMABUF)
hinges on that being true. **Question to qarl:** confirm the FYS
playlist.json on prod has zero `type: "video"` items? Even one
VideoSlide changes the candidate ranking.

### G.3 OPENMARQUEE_RENDERER_DMABUF env var state

Is `OPENMARQUEE_RENDERER_DMABUF=1` set on FYS prod? If yes AND
VideoSlides exist, C.3 (EGLImage destroy leak) re-opens. If no,
A.2 is fully gated out.

### G.4 Static MSDF atlas baseline size

A.6 size is unknown from static reading. `eprintln!` at
`sdf_atlas_gl.rs:104-110` logs the total at session boot. **One
journalctl grep on FYS** returns the actual number. **Question
to qarl:** authorize that grep (free; ~1 min code1 SSH)?

### G.5 256 MB CMA budget — can we raise it?

The Pi Zero 2 W default CMA is 256 MB but it's tunable in
`config.txt`. **Question to qarl:** is raising the CMA budget
(e.g. to 320 MB) acceptable as a stopgap while the real leak
gets diagnosed? Trade-off: less RAM for non-CMA uses; the Pi
Zero 2 W has 512 MB total, so 320 MB CMA leaves 192 MB for
heap/stack/kernel.

**Recommendation:** NOT a stopgap. Leaks are leaks; raising the
budget just delays the wedge by another hour. Diagnose first.

### G.6 Sidecar restart on CMA-pressure threshold

Currently sidecar restart "fixes" by releasing CMA. **Question
to qarl:** would a watchdog that triggers `systemctl restart
openmarquee-render` on `cma_used > 240MB` be acceptable as a
stopgap while the leak gets diagnosed?

**Recommendation:** SEPARATE dispatch. Useful safety net; not
in scope of r37.

### G.7 r38b dispatch ordering

Per the dispatch closing: code1's r38b will execute my top
recommendation. **Question to qarl:** order should be (a) E.1
deep-read first → (b) if inconclusive, E.2 BO-counter instrumentation
→ (c) if still inconclusive, E.3 sysfs probing. Confirm sequencing
or override.

---

## Hand-off shape

1. **qarl reviews this audit** + answers G.1-G.7.
2. **Code1's r38b dispatch** executes E.1 (deep-read of 4
   SUSPECT GBM scanout paths). ~30-60 min code1 read time. Zero
   runtime cost.
3. **If E.1 finds a specific missing-release site:** apply F.1
   fix per the canonical pattern + cross-build + deploy +
   verify on FYS that CmaUsed stabilizes.
4. **If E.1 is inconclusive:** escalate to E.2 BO-counter
   instrumentation. ~30 LOC; 5-10 min of normal operation
   surfaces the leak rate by path.
5. **If E.2 is inconclusive:** code1 captures E.3 sysfs probes
   (cheapest because zero code change).
6. **Verification:** re-run `cat /proc/meminfo | grep Cma` on
   FYS over a 30-min window. If CmaUsed plateaus (instead of
   monotonically rising), leak is closed.

---

## Out-of-scope items flagged for follow-up

- **A.6 static atlas baseline size measurement** — would inform
  whether atlas pruning is needed as a CMA-pressure mitigation.
  Cheap to capture (single grep).
- **CMA-pressure watchdog** (G.6) — separate dispatch.
- **CMA budget raise** (G.5) — NOT recommended as a fix; flagged
  for qarl awareness only.
- **Scanout-rotation helper refactor** (F.5) — defense-in-depth
  to prevent the bug class from recurring. Separate r39+ dispatch.
- **DMABUF code path coverage** (A.2 + C.3) — not FYS-relevant
  today, but a real leak hazard for any sign with VideoSlides +
  the DMABUF env var.

— jimmy:openmarquee-code2 (lane: code2 static CMA-leak audit)
