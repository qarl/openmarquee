# GL/Texture Memory Footprint Profile — code2 lane (perf-gl)

**Status:** WIP Phase 1 — saved pre-compaction. Sizing estimates derived from struct definitions + source-grep; FYS-side runtime measurements not yet correlated against admin's pmap/smem audit (parallel task on code's side).

**Dispatched by:** Jimmy-openmarquee + Jimmy-prime (4am footprint-reduction arc, post Option B green at 12a65c2).

**Goal:** rank GL memory consumers in hdmi.rs lane; identify the biggest measured-impact reductions for the (iii) memory-pressure component of QA's tail decomposition (pswpin 34 MB/min, VmSwap 43 MB on the 512 MB Pi Zero 2 W).

## Inventory — every owned EglSession allocation surface

| Field | Type | Size formula | At 1360×768 panel | At 1920×1080 panel | Lazy? | Notes |
|---|---|---|---|---|---|---|
| `transition_fbo_a` + `transition_tex_a` | NativeFB + NativeTexture | mode_w × mode_h × 4 | 4.17 MB | 7.91 MB | yes (r102.2 lazy) | Cached per-session per `transition_fbo_dims`. |
| `transition_fbo_b` + `transition_tex_b` | same | mode_w × mode_h × 4 | 4.17 MB | 7.91 MB | yes | Sibling. **Together 8.35 MB / 15.82 MB persistent CMA after first transition.** |
| `scene_fbo` + `scene_tex` | NativeFB + NativeTexture | mode_w × mode_h × 4 | 4.17 MB | 7.91 MB | yes (only when `!is_color_identity()` OR `rotation != 0`) | FYS typically identity + 0° → not allocated. |
| `scissored_bake_atlas` | (NativeFB, NativeTexture) | 2048 × 2048 × 4 = **16 MB** | 16 MB | 16 MB | yes (first scissored bake) | Fixed 16 MB regardless of panel. **Big single allocation.** |
| `msdf_atlases` | `Vec<MsdfAtlasGl>` (23 atlases) | likely 1024×1024 RGB888 × 23 ≈ 69 MB OR 512×512 × 23 ≈ 17 MB — **need to verify atlas dims** | 17-69 MB | same | NO (eager at session start) | **CANDIDATE: lazy-load by used-font set.** FYS reel uses ~3-5 fonts; 18+ atlases sit committed unused. Possible 50-60 MB save. |
| `dynamic_atlas_page_msdf` | AtlasPage | likely 2048×2048 RGBA8 = 16 MB OR cell-based | TBD | TBD | yes | Dynamic glyphs (●, ∞, runtime fonts). |
| `dynamic_atlas_page_colr` | AtlasPage | 96-px cells × N | TBD | TBD | yes | COLR emoji cache. |
| `slide_caches` (M-1 LruMap cap=24) | LruMap<Uuid, SlideRenderCache> | per-entry: glyph alpha bitmaps (CPU heap, ~1 MB) + tex + bg_tex + first_frame_tex (mode_w×mode_h×4) | 24 × ~5 MB worst-case = **120 MB CMA cap** | 24 × ~9 MB = 216 MB | yes | **CANDIDATE: drop cap to FYS reel size = 19 OR drop first_frame_tex entirely.** first_frame_tex is r62-era; with Option B's prewarm + iter-7 flush working, the first-frame still might be redundant. |
| `image_bg_cache` (LRU cap=6) | LruMap<PathBuf, (NativeTexture, w, h)> | per-entry: w × h × 4 (variable; FYS bg images vary) | ~2-8 MB × 6 = 12-48 MB | same | yes | Existing cap. |
| `image_slide_tex_cache` (cap=6) | per-entry texture | per-entry: w × h × 4 | ~2-8 MB × 6 = 12-48 MB | same | yes | Existing cap (Task #168). |
| `external_frame_tex` | Option<(NativeTexture, w, h)> | source × source × 4 | typ 4-8 MB | typ 4-8 MB | yes | STREAM/VLC only; FYS reel doesn't allocate. |
| `external_nv12_tex` | Option<(Y, UV, w, h)> | source × source × 1.5 (NV12) | typ 3-6 MB | typ 3-6 MB | yes | STREAM/VLC HW-decode path. |
| `transition_sp_quad_vbo` | Option<NativeBuffer> | 16 × 4 = 64 bytes | <1 KB | <1 KB | yes | Tiny. |
| **GBM scanout chain** | scanout_prev/current + held_scanout | 3 × phys_w × phys_h × 4 (RGBA8) | 11.9 MB | **23.7 MB** | always alloced | CMA-backed. C-2 candidate: 3→2 saves 7.91 MB at 1080p. MEDIUM risk vsync-miss. |
| **EGLImage cache** (v4l2.rs `capture_egl_images`) | per-decoder Vec<Option<EglImageHandle>> | handles only (~32 B each) × buffer_count × num_decoders | <1 KB total | <1 KB | yes | Tiny. Not a footprint candidate; was a Mutex/latency candidate (Option B fixed). |

## Top-3 candidates ranked by measured-impact / risk / LOC

### #1 (highest leverage): **MSDF atlas lazy-load by used-font set**

- **Surface:** `msdf_atlases: Vec<MsdfAtlasGl>` (hdmi.rs:472)
- **Current behavior:** all 23 RGB888 atlases uploaded at session bring-up (per comment lines 464-471). Persistent for session lifetime.
- **Expected delta:** if atlas = 1024×1024 RGB888 ×23 = ~69 MB committed; FYS reel uses 3-5 fonts → load only those → save ~50-60 MB. (If atlas = 512×512, save scales to ~12-15 MB; still meaningful.)
- **Fix shape:** parse playlist's used font set at session open; load only matching atlases. Hot-load on first-encounter for fonts not in the initial set.
- **LOC estimate:** MEDIUM (~50-100). The atlas-load is currently `msdf_atlases: Vec::new() → for each atlas { load }` at bring-up. Refactor to load-on-demand via the same MsdfAtlasGl::open path, keyed by `msdf_atlas_for_family(family)`.
- **Risk:** MEDIUM. First-use of a new font pays ~30-100 ms atlas load latency. Visible jitter on first appearance of any font; subsequent uses cached. Could be hidden behind a "loading" stub OR pre-warmed at session start for the playlist's catalog (deterministic).
- **Adversarial frame:** "Does the playlist actually use fewer than 23 fonts?" Verify against FYS content_root; if reel routinely uses 10+ fonts, savings shrink. ALSO: a future playlist with new fonts triggers cold-load mid-render = jitter. Worth a small warm-up sweep at BeginSlide.
- **DO-NOT:** the MsdfAtlasGl drop path freeing GPU texture handles must stay correct under partial-load.

### #2: **Drop `first_frame_tex` from SlideRenderCache (r62-era; redundant post-Option-B?)**

- **Surface:** `SlideRenderCache.first_frame_tex` (hdmi.rs:13740 area) inside `slide_caches` LruMap.
- **Current behavior:** every SlideRenderCache holds a mode_w × mode_h × 4 byte texture = 4.17 MB at 1360×768. At cap=24, worst case 100 MB CMA contribution.
- **Expected delta:** removes up to ~100 MB of CMA worst-case (FYS reel of 19 slides × 4.17 MB ≈ 79 MB realistic).
- **Fix shape:** remove the field + the capture site + the consumer site. With iter-7's offscreen-flush + Option B's EGLImage prewarm working, the original r62 "fast first-frame blit on slide re-enter" optimization may be redundant — the DMABUF zero-copy path is already first-frame-fast.
- **LOC estimate:** SMALL (~30) — delete-only across SlideRenderCache + free_slide_render_cache + the capture site.
- **Risk:** MEDIUM. Need to verify the r62 optimization isn't load-bearing on text-over-video first-paints. The texture might be hiding a meaningful latency.
- **Adversarial frame:** "What was the original r62 measured win?" Check git log; if it was tens of ms on text-over-video first-enter, that latency comes back. May need a smaller cache (just the active slide's first_frame_tex) instead of per-cache-entry.
- **DO-NOT:** can't regress the text-over-video transition latency QA bench-gated.

### #3: **Lower slide_caches LruMap cap from 24 → FYS reel size**

- **Surface:** `slide_caches` LruMap cap (hdmi.rs:434 — M-1 default = 24).
- **Current behavior:** 24 entries cap. FYS reel = 19 slides → 5 slots unused permanently. Per entry ~5 MB worst-case CMA.
- **Expected delta:** 5 × 5 MB = ~25 MB CMA + ~5 MB heap saved (if cap dropped to 19).
- **Fix shape:** change `SLIDE_CACHE_CAP_DEFAULT: usize = 24` → `19`. Env var override `OPENMARQUEE_SLIDE_CACHE_CAP` lets QA tune.
- **LOC estimate:** TRIVIAL (~1).
- **Risk:** LOW. M-1 specifically picked 24 for "FYS 19-slide reel + 5 headroom." With #2 (drop first_frame_tex) the per-entry size shrinks, making cap less important.
- **Adversarial frame:** "What's a partial playlist swap-in look like?" If admin loads a playlist of e.g. 22 slides, cap=19 forces evictions. With 19 slot evictions per cycle (every slide a miss), re-rasterization adds ms-tier cost. Cap=24 had 5-slot headroom precisely for this. **Recommend only paired with #2; standalone benefit is small.**
- **DO-NOT:** combined with #2 it's a one-line tighten; standalone it just removes headroom.

## Additional surfaces worth profiling later (not top-3)

- **GBM scanout 3 → 2:** C-2 from roadmap. 7.91 MB CMA at 1080p. MEDIUM risk vsync-miss; should be deferred until #1-3 measured.
- **scissored_bake_atlas:** 16 MB fixed. Already lazy. Could be smaller if the scissored-bake region geometry shrinks. LOW priority — 16 MB is one-time, no churn.
- **dynamic_atlas_page_msdf/colr:** size depends on cell count + page dims. Not yet measured.
- **COLR emoji unbounded growth (M-3):** flagged in roadmap as ~9 MB soft cap candidate. LOW priority — text-content dependent.
- **Image bg cache cap 6→4 (M-4):** ~4-8 MB save. LOW priority.

## Constraints (DO-NOT regress)

- F-1 typical-case win (transition p50 53 ms). 
- Option B EGLImage prewarm (12a65c2).
- iter-7 scoped offscreen-bake flush (`is_offscreen_bake && bake_offscreen_flush_enabled()`).
- r106 feed/drain decouple.
- r101 dmabuf-ref-leak invariant.
- Path A Stage 2 source-pin tags.
- v2v image PASS side=a+b all kinds.

## Next steps

1. **Verify MSDF atlas exact dims** — grep `MsdfAtlasGl::open` for the GL_RGB / GL_RGBA + width/height upload call. Confirms #1's expected delta.
2. **Audit r62 first_frame_tex callers** — git log + grep for first_frame_tex consumer sites to assess the redundancy claim in #2.
3. **Cross-reference with code's parallel pmap/smem audit on FYS** — convergent culprits become front-runners.
4. **PHASE 2 dispatch** waits for admin's read of this doc + code's audit.

— end WIP, saved pre-compaction.
