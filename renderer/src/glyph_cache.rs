// Bug 3 Slice 1 (2026-05-19) — runtime glyph cache infrastructure.
//
// HYBRID architecture (qa-Jimmy option D): static MSDF atlas covers
// the build-time-baked codepoints (Basic Latin + Latin-1 Supplement
// across 23 fonts); this dynamic cache covers the rest. Layout
// dispatch (hdmi_logic.rs) tries the static atlas first; only on
// miss does it touch the dynamic cache. Steady-state, both ●/∞
// (currently tofu on FYS) and any future operator-typed special
// codepoints route through here.
//
// Slot state machine:
//   Requested  → enqueued in mpsc; worker hasn't picked it up
//   Generating → worker is rasterizing (Slice 2 wires this)
//   Ready      → atlas slot allocated + GPU pixels uploaded
//
// Slice 2A scope: real msdfgen worker. Each worker reads the TTF
// file from its MissRequest's font_path, runs msdfgen at CELL_PX^2
// to produce one MSDF cell, and pushes the result back via the
// completion channel (or directly inserts FontMissing on
// font-lacks-codepoint / I/O failure). Slice 2B threads the
// production caller to actually exercise the dispatch hook + adds
// the DynamicMsdf CharKind variant + render path.
//
// Thread model:
//   Render thread:
//     - Calls get_or_request(key, font_path) on layout pass.
//     - Calls poll_completions() at frame start to drain Success
//       completions from the channel + perform glTexSubImage2D
//       uploads. FontMissing doesn't flow through this path -- the
//       worker inserts it directly into slots since it needs no GL
//       work.
//     - Single-threaded GL access; never blocks on workers.
//   Worker pool (4 std::thread workers via crossbeam-channel mpsc):
//     - Drain MissRequest from the shared work_rx.
//     - On Ready: send Completion via completion_tx (render thread
//       polls + uploads).
//     - On font-missing / I/O failure: direct-insert FontMissing
//       into the slots Arc<Mutex<HashMap>> (no upload needed).
//
// Capacity bookkeeping: GlyphCache holds N atlas pages (default 1
// for Slice 1; LRU eviction or page-grow is a Slice 1.x follow-up
// triggered by first observed pressure).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use crossbeam_channel::{Receiver, Sender};

use crate::atlas_page::SlotPos;
#[cfg(target_os = "linux")]
use crate::atlas_page::AtlasPage;

/// MSDF cell dimensions + range. MUST match build.rs's CELL_PX +
/// RANGE_PX so the runtime FS_MSDF_FIXED shader gets identical SDF
/// reconstruction across static + dynamic atlas slots. Changing
/// either of these here only is a parity break. CELL_PX is pub so
/// the dispatch (hdmi_logic) can derive UVs at layout time without
/// re-stashing the constant on every SlotState::Ready.
pub const CELL_PX: u32 = 48;

/// Bug 3 Slice 2D: ordered list of fallback font stems the dispatch
/// hook iterates when the primary font has SlotState::FontMissing
/// for a codepoint. Stems are the TTF filename without the `.ttf`
/// extension; the cache resolves them via `<fonts_dir>/<stem>.ttf`.
///
/// Chain: primary -> DejaVu Sans -> Tofu.
///
/// DejaVu Sans covers 5918 codepoints (Geometric Shapes, Mathematical
/// Operators, Box Drawing, Block Elements, Arrows, etc.) -- substantially
/// broader than Noto Sans Regular's Latin-only Google Fonts subset.
/// One stem in the chain is sufficient for the FYS reel's coverage gap
/// (● U+25CF on the Boot slide); more stems can be appended as the
/// operator-typed codepoint set grows.
///
/// The dispatch hook stops at the first fallback that resolves to
/// SlotState::Ready. Subsequent fallbacks (none today) would only
/// fire if DejaVu also reports FontMissing, which would mean the
/// codepoint is truly exotic.
pub const FALLBACK_FONT_STEMS: &[&str] = &["dejavu-sans"];

/// Bug 3 Slice 3B: stem of the COLRv1 emoji font for the runtime
/// cache dispatch (hdmi_logic.rs's layout_text_to_quads). Pairs
/// with `RenderMode::Colr` on the GlyphKey. The TTF lives at
/// `<fonts_dir>/<stem>.ttf` (gitignored, fetched at setup time
/// via scripts/download-emoji-font-colrv1.sh).
pub const COLR_EMOJI_FONT_STEM: &str = "noto-color-emoji-colrv1";

/// Bug 3 Slice 2D: derive a font_family_id from a font stem via
/// FNV-1a's low 32 bits. Matches the dispatch hook's identity-keying
/// at hdmi_logic.rs (which hashes atlas.manifest.font the same way),
/// so a fallback stem and a primary stem with the same low-32 hash
/// would collide -- but the static catalog is ~24 entries plus 1
/// fallback, so collisions are vanishingly rare. Slice 3+ can swap
/// to a Vec<String>-indexed registry if a future operator-added
/// font collides; today's 25-entry catalog has zero collisions.
pub fn font_family_id_from_stem(stem: &str) -> u32 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in stem.as_bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h as u32
}
const RANGE_PX: f64 = 4.0;
const EDGE_COLORING_ANGLE_THRESHOLD: f64 = 3.0;
const EDGE_COLORING_SEED: u64 = 0;

/// Cache key. font_family_id is a hash derived from the font's
/// stem name (FNV-1a low 32 bits); u32 keeps cross-font collisions
/// astronomically rare across a ~24-font catalog (vs 1/256 with the
/// u8 used during Slice 1's dormant-API scaffolding). The worker
/// re-derives the font path from the GlyphKey + the path passed in
/// MissRequest; the id itself is only the dedup key.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct GlyphKey {
    pub font_family_id: u32,
    pub codepoint: u32,
    pub render_mode: RenderMode,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum RenderMode {
    /// Build-time-style MSDF (matching the static atlas's
    /// FS_MSDF_FIXED shader). Used for text.
    Msdf,
    /// COLRv1 vector emoji — rasterized at runtime via skrifa
    /// (paint-tree traversal) + tiny-skia (vector raster). See
    /// `crate::glyph_cache_colr::rasterize_colr_cell`.
    Colr,
}

/// Dispatch-side handle bundling the cache + the fonts_dir the
/// worker reads TTF files from. Bundles them so the
/// layout_text_to_quads signature gets one Option<RuntimeGlyphCtx>
/// instead of two coupled Options (and so future Slice 3+ context
/// can be added without re-touching every test site that passes
/// None). Slice 2A: layout_text_to_quads + paint_slide_with_viewport
/// pass None until Slice 2B activates the production caller.
pub struct RuntimeGlyphCtx<'a> {
    pub cache: &'a GlyphCache,
    pub fonts_dir: &'a Path,
}

/// Plane bounds in em-units (matches sdf_atlas.rs GlyphEntry shape).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PlaneBounds {
    pub pl_left: f32,
    pub pl_right: f32,
    pub pl_top: f32,
    pub pl_bottom: f32,
}

/// Slot state visible to consumers. Render-thread reads via
/// get_or_request; only Ready slots have valid atlas coords.
#[derive(Clone, Debug)]
pub enum SlotState {
    Requested,
    Generating,
    Ready {
        slot: SlotPos,
        advance_em: f32,
        plane_bounds: PlaneBounds,
    },
    /// Worker tried + failed (font lacks the codepoint, or file I/O
    /// failed, or msdfgen rejected the shape). Permanent: dispatch
    /// should render Tofu and NOT re-enqueue. Without this, every
    /// frame would re-issue a MissRequest for the missing codepoint
    /// and pile up tens of thousands of worker invocations across a
    /// long FYS run.
    FontMissing,
}

/// Internal: a unit of work pushed onto the worker channel. The
/// font_path is resolved by the dispatch hook (hdmi_logic.rs) from
/// the atlas manifest's font stem + the renderer's fonts_dir; the
/// worker re-reads the TTF bytes per first-encounter-per-font (file
/// I/O is sub-millisecond on the Pi for the ~100-300 KB font files
/// we ship; redundancy across workers is irrelevant since each font
/// is rasterized at most once-per-codepoint-per-session).
#[derive(Clone, Debug)]
struct MissRequest {
    key: GlyphKey,
    font_path: PathBuf,
}

/// Internal: a completion pushed back to the render thread by a
/// worker. Two variants:
///   Ready       — successful rasterization; the render thread's
///                 poll uploads the cell to the atlas page +
///                 transitions the slot to SlotState::Ready.
///   FontMissing — worker confirmed the font lacks this codepoint
///                 (or I/O failure). The worker has ALREADY
///                 direct-inserted SlotState::FontMissing into the
///                 slots map (so tests + immediate get_or_request
///                 lookups see it without waiting for poll). The
///                 channel-side notification is purely a signal so
///                 the render thread can drain slide_caches on
///                 transition -- without it, the production reel's
///                 layout cache holds stale Tofu placeholders
///                 across the entire session for keys whose chain
///                 lookups depend on observing the primary's
///                 FontMissing state (Bug 3 Slice 2D follow-up
///                 2026-05-19).
#[derive(Debug)]
enum Completion {
    Ready {
        key: GlyphKey,
        rgba_bytes: Vec<u8>,
        cell_px: u32,
        advance_em: f32,
        plane_bounds: PlaneBounds,
    },
    FontMissing {
        key: GlyphKey,
    },
}

/// Slice 3C (2026-05-20): LRU recency bookkeeping for atlas
/// eviction. `clock` is a monotonically-rising tick; `last_used`
/// maps each Ready glyph to the tick of its most recent use.
/// Render-thread-only — the worker pool never touches it, so the
/// `slots`-then-`recency` lock order used by `evict_lru_ready`,
/// `get_or_request` (Ready-hit), and `poll_completions` (Ready
/// upload) has no second contender and cannot deadlock.
///
/// Round 8 (2026-05-25): added per-render-mode `BTreeMap<tick,
/// GlyphKey>` reverse index so eviction picks the coldest entry
/// in O(log N) via `first_key_value()` instead of O(N) HashMap
/// scan. Two maps (one per RenderMode) avoid the "walk past the
/// wrong mode" scan; eviction is mode-specific (the page that
/// just filled). Maintenance: every touch removes the old tick
/// from the appropriate map and inserts the new tick; eviction
/// pops the front and also removes from `last_used`.
struct RecencyState {
    clock: u64,
    last_used: HashMap<GlyphKey, u64>,
    /// MSDF Ready glyphs sorted by recency tick (ascending — front
    /// is coldest). Invariant: every key whose `last_used` value is
    /// `t` AND whose `render_mode == Msdf` appears here as
    /// `(t -> key)`. Maintained by `touch_recency` (insert/remove)
    /// and `evict_lru_ready` (remove).
    by_tick_msdf: BTreeMap<u64, GlyphKey>,
    /// Same shape, COLR Ready glyphs.
    by_tick_colr: BTreeMap<u64, GlyphKey>,
}

impl RecencyState {
    fn by_tick_mut(&mut self, mode: RenderMode) -> &mut BTreeMap<u64, GlyphKey> {
        match mode {
            RenderMode::Msdf => &mut self.by_tick_msdf,
            RenderMode::Colr => &mut self.by_tick_colr,
        }
    }
}

pub struct GlyphCache {
    /// state map; locked on every get / insert / completion drain.
    slots: Arc<Mutex<HashMap<GlyphKey, SlotState>>>,
    /// Slice 3C: per-Ready-glyph LRU recency. Separate Mutex (not
    /// shared with workers) so it stays private bookkeeping and
    /// SlotState remains a pure public state-machine type.
    recency: Mutex<RecencyState>,
    work_tx: Sender<MissRequest>,
    completion_rx: Receiver<Completion>,
    workers: Vec<JoinHandle<()>>,
    /// Held so workers can shut down cleanly on drop. Closing the
    /// work_tx clone here propagates to the receiver in each worker.
    shutdown_tx: Sender<()>,
    /// Stats: requests received + completions processed.
    request_count: Arc<Mutex<u64>>,
    completion_count: Arc<Mutex<u64>>,
}

impl GlyphCache {
    /// Spawn `num_workers` background threads. Each worker drains
    /// MissRequest from the shared mpsc, reads the TTF, runs msdfgen
    /// to produce an MSDF cell, and pushes the result back via the
    /// completion channel (for Success) or directly into the slots
    /// map (for FontMissing — no GL upload needed).
    pub fn new(num_workers: usize) -> Self {
        let (work_tx, work_rx) = crossbeam_channel::unbounded::<MissRequest>();
        let (completion_tx, completion_rx) = crossbeam_channel::unbounded::<Completion>();
        let (shutdown_tx, shutdown_rx) = crossbeam_channel::bounded::<()>(0);
        let request_count = Arc::new(Mutex::new(0u64));
        let slots: Arc<Mutex<HashMap<GlyphKey, SlotState>>> =
            Arc::new(Mutex::new(HashMap::new()));

        let mut workers = Vec::with_capacity(num_workers);
        for _ in 0..num_workers {
            let rx = work_rx.clone();
            let completion_tx = completion_tx.clone();
            let sd = shutdown_rx.clone();
            let req_ctr = Arc::clone(&request_count);
            let slots_for_worker = Arc::clone(&slots);
            workers.push(std::thread::spawn(move || {
                loop {
                    crossbeam_channel::select! {
                        recv(rx) -> msg => {
                            match msg {
                                Ok(req) => {
                                    {
                                        let mut ctr = req_ctr.lock().unwrap();
                                        *ctr += 1;
                                    }
                                    // Worker hand-off into Generating
                                    // state. The dispatch hook saw
                                    // Requested when it enqueued; this
                                    // window lets a frame-time observer
                                    // distinguish "not picked up yet" vs
                                    // "actively rasterizing".
                                    {
                                        let mut slots = slots_for_worker.lock().unwrap();
                                        // Only transition if still
                                        // Requested -- defensive against
                                        // a Drop racing in.
                                        if matches!(slots.get(&req.key), Some(SlotState::Requested)) {
                                            slots.insert(req.key, SlotState::Generating);
                                        }
                                    }
                                    // Bug 3 Slice 3A (2026-05-19): dispatch by
                                    // RenderMode. Msdf goes through the build-
                                    // time-symmetric msdfgen path; Colr goes
                                    // through skrifa's ColorPainter trait +
                                    // tiny-skia for COLRv1 paint trees. Both
                                    // produce a `RasterOutput` with rgba_bytes
                                    // + cell_px + advance_em + plane_bounds,
                                    // so the Completion::Ready
                                    // shape is identical across modes. The
                                    // dynamic atlas page is mode-agnostic at
                                    // the slot level (8-bit-RGBA cells); the
                                    // draw side disambiguates via the per-quad
                                    // GlyphKind tag (DynamicMsdf vs
                                    // DynamicEmoji in Slice 3B).
                                    let raster_result = match req.key.render_mode {
                                        RenderMode::Msdf => rasterize_msdf_cell(
                                            &req.font_path,
                                            req.key.codepoint,
                                        ),
                                        RenderMode::Colr => {
                                            crate::glyph_cache_colr::rasterize_colr_cell(
                                                &req.font_path,
                                                req.key.codepoint,
                                            )
                                        }
                                    };
                                    match raster_result {
                                        Ok(Some(out)) => {
                                            let _ = completion_tx.send(Completion::Ready {
                                                key: req.key,
                                                rgba_bytes: out.rgba_bytes,
                                                cell_px: out.cell_px,
                                                advance_em: out.advance_em,
                                                plane_bounds: out.plane_bounds,
                                            });
                                        }
                                        Ok(None) => {
                                            // Direct-insert so tests +
                                            // immediate get_or_request
                                            // lookups see FontMissing
                                            // without waiting for poll.
                                            {
                                                let mut slots = slots_for_worker.lock().unwrap();
                                                slots.insert(req.key, SlotState::FontMissing);
                                            }
                                            // Channel signal for render-
                                            // thread poll: paint_and_
                                            // present invalidates
                                            // slide_caches on completion
                                            // count > 0. Without this,
                                            // FontMissing state changes
                                            // would only be observed on
                                            // the next natural slide_
                                            // caches eviction.
                                            let _ = completion_tx.send(
                                                Completion::FontMissing { key: req.key },
                                            );
                                        }
                                        Err(e) => {
                                            eprintln!(
                                                "glyph_cache worker: rasterize {:?} cp=U+{:04X}: {e}",
                                                req.font_path, req.key.codepoint,
                                            );
                                            {
                                                let mut slots = slots_for_worker.lock().unwrap();
                                                slots.insert(req.key, SlotState::FontMissing);
                                            }
                                            let _ = completion_tx.send(
                                                Completion::FontMissing { key: req.key },
                                            );
                                        }
                                    }
                                }
                                Err(_) => return, // tx closed
                            }
                        }
                        recv(sd) -> _ => return, // shutdown signal
                    }
                }
            }));
        }

        Self {
            slots,
            recency: Mutex::new(RecencyState {
                clock: 0,
                last_used: HashMap::new(),
                by_tick_msdf: BTreeMap::new(),
                by_tick_colr: BTreeMap::new(),
            }),
            work_tx,
            completion_rx,
            workers,
            shutdown_tx,
            request_count,
            completion_count: Arc::new(Mutex::new(0)),
        }
    }

    /// Slice 3C: stamp `key` as used-now in the LRU recency map.
    /// Called on a Ready cache hit (dispatch path) and when a Ready
    /// completion lands (a freshly-uploaded glyph starts hot, not
    /// instantly the coldest). A rising `clock` orders all uses.
    ///
    /// Round 7: called from BOTH call sites with the `slots` Mutex
    /// already held (nested slots-then-recency lock order, matching
    /// evict_lru_ready). Workers never lock `recency` so this nesting
    /// cannot deadlock.
    ///
    /// Round 8: also maintains the per-render-mode `by_tick_*`
    /// reverse index. On re-touch of an existing key we remove the
    /// old (old_tick -> key) entry before inserting the fresh
    /// (new_tick -> key); on first-touch we just insert. Eviction's
    /// O(log N) BTreeMap.first_key_value() depends on this invariant
    /// — a stale (tick -> key) for an evicted/already-re-touched key
    /// would silently mis-pick the victim.
    fn touch_recency(&self, key: GlyphKey) {
        let mut r = self.recency.lock().unwrap();
        r.clock += 1;
        let tick = r.clock;
        let old_tick = r.last_used.insert(key, tick);
        let by_tick = r.by_tick_mut(key.render_mode);
        if let Some(old) = old_tick {
            by_tick.remove(&old);
        }
        by_tick.insert(tick, key);
    }

    /// Slice 3C: evict the least-recently-used `Ready` slot of the
    /// given `render_mode`, freeing its atlas cell. Returns true if
    /// a slot was freed (the caller retries allocate_slot), false
    /// if there was no eligible victim.
    ///
    /// IN-FLIGHT SAFETY — the subtle correctness point: only
    /// `SlotState::Ready` entries are eviction candidates.
    /// Requested / Generating entries hold NO atlas slot (a slot is
    /// allocated only when a Ready completion is processed in
    /// poll_completions), so a worker mid-rasterization can never
    /// have its target cell reused out from under it — it has no
    /// cell yet. FontMissing entries likewise hold no slot. The
    /// invariant: atlas-slot occupancy is 1:1 with Ready entries,
    /// and only Ready entries are ever freed.
    ///
    /// An evicted glyph needed again later is a plain cache miss:
    /// get_or_request returns None, re-enqueues a MissRequest, and
    /// the worker re-rasterizes into a fresh slot. That re-raster
    /// cost is the expected, correct price of a bounded atlas.
    ///
    /// Lock order: `recency` first to pull the victim key out of the
    /// per-mode BTreeMap, then `slots` for the actual remove +
    /// SlotPos retrieval. This INVERTS the pre-r8 slots-then-recency
    /// order — safe because workers still never touch `recency`, so
    /// no opposite-direction nesting exists anywhere in the cache.
    /// `get_or_request` and `poll_completions` still acquire
    /// slots-then-recency on their NESTED hit/upload path; this
    /// function holds them sequentially (release recency before
    /// acquiring slots), so there is no actual nested-order conflict
    /// between the two call families.
    ///
    /// Round 8: O(log N) eviction via BTreeMap.pop_first(). Pre-r8
    /// was O(N) linear scan over the entire slots HashMap, holding
    /// both mutexes the whole time -- with worker contention on
    /// `slots`, that was a worker-stall window proportional to cache
    /// size. The BTreeMap reverse index maintained by touch_recency
    /// reduces eviction to a single pop_first + a scoped slots
    /// remove.
    fn evict_lru_ready(
        &self,
        page: &mut crate::atlas_page::AtlasPage,
        render_mode: RenderMode,
    ) -> bool {
        let victim_key = {
            let mut recency = self.recency.lock().unwrap();
            let by_tick = recency.by_tick_mut(render_mode);
            match by_tick.pop_first() {
                Some((_, key)) => {
                    recency.last_used.remove(&key);
                    key
                }
                None => return false,
            }
        };
        // recency lock released; now acquire slots only for the
        // SlotPos read + remove. SlotState::Ready invariant holds
        // because by_tick only ever contains keys inserted via
        // touch_recency, which is called only when the slots entry
        // is in SlotState::Ready (get_or_request gate via the
        // `matches!(state, SlotState::Ready)` check; poll_completions
        // calls touch_recency right after inserting Ready).
        let mut slots = self.slots.lock().unwrap();
        match slots.remove(&victim_key) {
            Some(SlotState::Ready { slot, .. }) => {
                page.free_slot(slot);
                true
            }
            // Defensive: a key was popped from by_tick but the slots
            // entry is gone (or in another state). Under the current
            // invariant this shouldn't happen, but treat it as
            // "victim was already cleaned up" rather than panicking.
            // Returns false so the caller's allocate-retry loop bails
            // cleanly instead of looping.
            _ => false,
        }
    }

    /// Look up a glyph by key. Returns:
    ///   Some(Ready { .. })        — atlas slot is allocated + GPU-resident
    ///   Some(Requested|Generating) — worker is on it; render-side should
    ///                                 use a Tofu placeholder this frame
    ///   Some(FontMissing)         — worker confirmed the font lacks this
    ///                                codepoint; permanent Tofu
    ///   None                      — first encounter; a MissRequest is
    ///                                enqueued; caller should render as
    ///                                Tofu placeholder this frame
    /// Idempotent across calls — second-call for an in-flight or resolved
    /// key returns the existing state (does NOT re-enqueue).
    ///
    /// `font_path` is a thunk that materializes the TTF file path. The
    /// dispatch hook resolves it from the atlas manifest's font stem +
    /// the renderer's fonts_dir. We accept a closure rather than an
    /// already-built PathBuf because the per-call materialization
    /// (`fonts_dir.join(format!("{}.ttf", stem))`) heap-allocates a
    /// String + a PathBuf; on the steady-state Ready hit path that
    /// PathBuf was previously dropped unread. Deferring to the miss
    /// branch eliminates those allocs per char on every layout pass.
    pub fn get_or_request(
        &self,
        key: GlyphKey,
        font_path: impl FnOnce() -> PathBuf,
    ) -> Option<SlotState> {
        let mut slots = self.slots.lock().unwrap();
        if let Some(state) = slots.get(&key).cloned() {
            // Slice 3C: a Ready hit on the dispatch/layout path is a
            // USE — refresh recency so LRU eviction keeps hot glyphs
            // and discards cold ones. Requested / Generating /
            // FontMissing hold no atlas slot, so recency is moot for
            // them (they are never eviction candidates).
            //
            // Round 7 perf: nest the recency-lock acquisition UNDER
            // the slots guard instead of dropping slots first. Same
            // lock count but eliminates the drop-then-reacquire
            // cache-transition window between the two mutexes. Lock
            // order slots-then-recency matches evict_lru_ready;
            // workers never lock recency so nesting cannot deadlock.
            if matches!(state, SlotState::Ready { .. }) {
                self.touch_recency(key);
            }
            drop(slots);
            return Some(state);
        }
        slots.insert(key, SlotState::Requested);
        // Send is unbounded so this never blocks; ignore the result
        // because send-error only happens if all workers panicked,
        // which we'd surface via panic-handler elsewhere. Only NOW
        // do we materialize the PathBuf — the hit branch above
        // returned without paying the alloc cost.
        let _ = self.work_tx.send(MissRequest { key, font_path: font_path() });
        None
    }

    /// Drain completed-work queue and upload to the GPU. Render-thread
    /// only — `gl` and `page` must be the active EGL session's. Bounded
    /// by `max_uploads_per_call` so a backlog doesn't blow the 16 ms
    /// frame budget at first encounter.
    ///
    /// Two completion variants are drained:
    ///   Ready       — Allocates an atlas slot, uploads via
    ///                 glTexSubImage2D, transitions SlotState to
    ///                 Ready in the slots map. Counted toward the
    ///                 return value so the caller can invalidate
    ///                 slide_caches.
    ///   FontMissing — Slot is ALREADY in SlotState::FontMissing
    ///                 (worker direct-inserted before sending the
    ///                 channel notification). Counted toward the
    ///                 return value so paint_and_present invalidates
    ///                 slide_caches — without this, the production
    ///                 reel's layout cache would hold stale Tofu
    ///                 placeholders for the entire session on keys
    ///                 whose chain depends on the FontMissing state.
    ///                 No GL work performed (no slot allocation, no
    ///                 upload).
    #[cfg(target_os = "linux")]
    pub fn poll_completions(
        &mut self,
        gl: &glow::Context,
        msdf_page: &mut AtlasPage,
        colr_page: &mut AtlasPage,
        max_uploads_per_call: usize,
    ) -> usize {
        let mut count = 0;
        for _ in 0..max_uploads_per_call {
            match self.completion_rx.try_recv() {
                Ok(Completion::Ready {
                    key,
                    rgba_bytes,
                    cell_px,
                    advance_em,
                    plane_bounds,
                }) => {
                    // Bug 3 Slice 3B (2026-05-19): route the upload
                    // to the page matching this completion's render
                    // mode. MSDF cells are 48 px on the msdf_page;
                    // COLRv1 cells are 96 px on the colr_page (the
                    // two pages have different cell_px so cells
                    // cannot share a slot allocator). The completion
                    // already carries cell_px so we trust the worker's
                    // size match rather than re-deriving from the
                    // page's cell_px.
                    let page = match key.render_mode {
                        RenderMode::Msdf => &mut *msdf_page,
                        RenderMode::Colr => &mut *colr_page,
                    };
                    // CMA-arc 2026-06-21: lazy-allocate the page's
                    // GPU texture on first Ready completion of this
                    // mode. Pre-arc the texture was unconditionally
                    // allocated at session bring-up (16 MB per page
                    // x 2 pages = 32 MB CMA on every session, even
                    // text-only with no dynamic glyphs). Now the
                    // page stays at zero CMA until the worker pool
                    // produces a Ready completion that needs to be
                    // uploaded. allocate_texture is idempotent
                    // (atlas_page.rs:91-93 early-return on Some) so
                    // subsequent completions are a no-op cost.
                    if let Err(e) = page.allocate_texture(gl) {
                        eprintln!(
                            "glyph_cache: AtlasPage texture lazy-alloc \
                             failed ({:?}): {e}; dropping completion for {:?}",
                            key.render_mode, key,
                        );
                        continue;
                    }
                    // Slice 3C: on a full page, evict the LRU Ready
                    // slot of this render_mode and retry. A free
                    // immediately follows a successful eviction so
                    // the retry cannot fail; the .expect() asserts
                    // that invariant. Only if there is NO Ready slot
                    // to evict (page somehow full of non-Ready — not
                    // reachable: slots are allocated solely for
                    // Ready completions of this mode) do we drop.
                    let slot_pos = match page.allocate_slot() {
                        Some(s) => s,
                        None => {
                            if self.evict_lru_ready(page, key.render_mode) {
                                page.allocate_slot().expect(
                                    "allocate_slot must succeed right after a successful eviction",
                                )
                            } else {
                                eprintln!(
                                    "glyph_cache: atlas page full ({:?}), no Ready slot to evict; dropping completion for {:?}",
                                    key.render_mode, key,
                                );
                                continue;
                            }
                        }
                    };
                    if let Err(e) = page.upload_slot(
                        gl, slot_pos.x, slot_pos.y, cell_px, cell_px, &rgba_bytes,
                    ) {
                        eprintln!("glyph_cache: upload_slot failed for {:?}: {e}", key);
                        continue;
                    }
                    let mut slots = self.slots.lock().unwrap();
                    slots.insert(key, SlotState::Ready {
                        slot: slot_pos,
                        advance_em,
                        plane_bounds,
                    });
                    // Slice 3C: a just-uploaded glyph starts HOT —
                    // stamp recency now so it is not instantly the
                    // coldest candidate on the very next eviction.
                    //
                    // Round 7 perf: same nested-lock pattern as
                    // get_or_request — bump recency UNDER the slots
                    // guard to eliminate the drop-then-reacquire
                    // cache-transition window. Slots-then-recency
                    // lock order is consistent across this call site,
                    // get_or_request, and evict_lru_ready; workers
                    // never lock recency.
                    self.touch_recency(key);
                    drop(slots);
                    *self.completion_count.lock().unwrap() += 1;
                    count += 1;
                }
                Ok(Completion::FontMissing { key: _ }) => {
                    // Slot is already FontMissing (worker direct-
                    // inserted before sending this notification).
                    // Count it so the caller invalidates slide_caches.
                    *self.completion_count.lock().unwrap() += 1;
                    count += 1;
                }
                Err(_) => break, // nothing pending
            }
        }
        count
    }

    /// Diagnostic: how many MissRequests have been received by workers.
    /// Used for tests + logging.
    pub fn request_count(&self) -> u64 {
        *self.request_count.lock().unwrap()
    }

    /// Diagnostic: how many completions have been uploaded.
    pub fn completion_count(&self) -> u64 {
        *self.completion_count.lock().unwrap()
    }

    /// Test seam: deliver a synthetic "ready" slot. Bypasses the
    /// worker + GL upload path so tests without a GL context can
    /// verify the upload-side state-machine transitions in
    /// isolation. Live runtime never uses this — production
    /// rasterization goes through the worker pool + real msdfgen.
    #[cfg(test)]
    fn inject_completion_for_test(
        &self,
        key: GlyphKey,
        rgba_bytes: Vec<u8>,
        cell_px: u32,
        advance_em: f32,
        plane_bounds: PlaneBounds,
    ) {
        // Reach into the completion_rx by going through a backdoor
        // sender. For Slice 1 tests we don't have a backdoor sender;
        // instead, just push the Completion + key directly into the
        // slots map at Ready state. This skips the upload step
        // (which needs a GL context tests don't have).
        let _ = (rgba_bytes, cell_px);
        let mut slots = self.slots.lock().unwrap();
        slots.insert(key, SlotState::Ready {
            slot: SlotPos { x: 0, y: 0 },
            advance_em,
            plane_bounds,
        });
        *self.completion_count.lock().unwrap() += 1;
    }

    /// Test seam: directly stamp a `Ready` slot into the state map.
    /// Like `inject_completion_for_test` but `pub` so cross-module
    /// tests (e.g. the `hdmi_logic` layout suite) can exercise the
    /// COLR / DynamicMsdf dispatch path without a worker pool, a GL
    /// context, or a real TTF on disk. Live runtime never uses this.
    #[cfg(test)]
    pub fn insert_ready_slot_for_test(
        &self,
        key: GlyphKey,
        slot: SlotPos,
        advance_em: f32,
        plane_bounds: PlaneBounds,
    ) {
        self.slots.lock().unwrap().insert(
            key,
            SlotState::Ready { slot, advance_em, plane_bounds },
        );
    }
}

/// Worker-side raster output. Mirrors build.rs's per-glyph atlas
/// cell + metrics so the runtime FS_MSDF_FIXED shader path sees
/// identical SDF reconstruction across static-baked + dynamic-
/// cached slots.
pub(crate) struct RasterOutput {
    pub(crate) rgba_bytes: Vec<u8>,
    pub(crate) cell_px: u32,
    pub(crate) advance_em: f32,
    pub(crate) plane_bounds: PlaneBounds,
}

/// Rasterize one codepoint to a CELL_PX^2 MSDF cell. Returns:
///   Ok(Some(out)) — successful rasterization
///   Ok(None)      — font is loadable but lacks this codepoint OR
///                   the shape is degenerate (zero-area, invalid)
///   Err(e)        — I/O or parse error
///
/// Matches build.rs's bake_one_font glyph loop byte-for-byte (same
/// CELL_PX, RANGE_PX, edge-coloring threshold + seed, unorm8 mapping,
/// Y-flip, plane-bounds derivation). Diverges only in (a) output
/// pixel format (RGBA8 with A=255 vs build's packed-RGB888 atlas:
/// the dynamic atlas page is RGBA8, the static atlas page is RGB8;
/// the FS_MSDF_FIXED shader reads .rgb either way) and (b) per-cell
/// scope vs whole-font (build packs an N-cell grid; runtime ships
/// one cell at a time).
fn rasterize_msdf_cell(
    font_path: &Path,
    codepoint: u32,
) -> Result<Option<RasterOutput>, anyhow::Error> {
    use msdfgen::{Bitmap, FillRule, FontExt, MsdfGeneratorConfig, Range, Rgb};
    use ttf_parser::Face;

    let ttf_bytes = std::fs::read(font_path)
        .map_err(|e| anyhow::anyhow!("read TTF {:?}: {e}", font_path))?;
    let face = Face::parse(&ttf_bytes, 0)
        .map_err(|e| anyhow::anyhow!("parse TTF {:?}: {e}", font_path))?;
    let upem = face.units_per_em();

    let c = match char::from_u32(codepoint) {
        Some(c) => c,
        None => return Ok(None),
    };
    let Some(gid) = face.glyph_index(c) else {
        return Ok(None);
    };
    let Some(mut shape) = face.glyph_shape(gid) else {
        return Ok(None);
    };

    if !shape.validate() {
        return Ok(None);
    }
    shape.normalize();

    let bound = shape.get_bound();
    let Some(framing) = bound.autoframe(
        CELL_PX,
        CELL_PX,
        Range::Px(RANGE_PX),
        None,
    ) else {
        return Ok(None); // degenerate / zero-area shape
    };

    shape.edge_coloring_simple(EDGE_COLORING_ANGLE_THRESHOLD, EDGE_COLORING_SEED);

    let mut bitmap: Bitmap<Rgb<f32>> = Bitmap::new(CELL_PX, CELL_PX);
    let cfg = MsdfGeneratorConfig::default();
    shape.generate_msdf(&mut bitmap, &framing, &cfg);
    shape.correct_sign(&mut bitmap, &framing, FillRule::default());
    shape.correct_msdf_error(&mut bitmap, &framing, &cfg);

    // Pack RGB888 -> RGBA8 with Y-flip. msdfgen's bitmap origin is
    // bottom-left (C++ convention); the dynamic atlas (like the
    // static atlases) uses top-left origin to match GL texture
    // upload semantics. A=255 since FS_MSDF_FIXED samples .rgb only.
    let mut rgba = vec![0u8; (CELL_PX * CELL_PX * 4) as usize];
    for y in 0..CELL_PX {
        for x in 0..CELL_PX {
            let src_y = CELL_PX - 1 - y;
            let px = bitmap.pixel(x, src_y);
            let dst = ((y * CELL_PX + x) * 4) as usize;
            rgba[dst] = unorm8(px.r);
            rgba[dst + 1] = unorm8(px.g);
            rgba[dst + 2] = unorm8(px.b);
            rgba[dst + 3] = 255;
        }
    }

    // Plane bounds: inverse of msdfgen's Projection (pixel = scale *
    // (shape + translate)). The cell's atlas corners project back
    // to shape-space coords (raw font units); dividing by upem turns
    // them into em.
    let sx = framing.projection.scale.x;
    let sy = framing.projection.scale.y;
    let tx = framing.projection.translate.x;
    let ty = framing.projection.translate.y;
    let upem_f = upem as f32;

    let shape_l = 0.0 / sx - tx;
    let shape_r = CELL_PX as f64 / sx - tx;
    let shape_b = 0.0 / sy - ty;
    let shape_t = CELL_PX as f64 / sy - ty;

    let advance_em = face.glyph_hor_advance(gid).unwrap_or(0) as f32 / upem_f;

    Ok(Some(RasterOutput {
        rgba_bytes: rgba,
        cell_px: CELL_PX,
        advance_em,
        plane_bounds: PlaneBounds {
            pl_left: (shape_l as f32) / upem_f,
            pl_bottom: (shape_b as f32) / upem_f,
            pl_right: (shape_r as f32) / upem_f,
            pl_top: (shape_t as f32) / upem_f,
        },
    }))
}

/// f32 -> u8 truncating clamp; matches build.rs's encoding exactly
/// so the runtime FS_MSDF_FIXED shader sees byte-identical SDF
/// reconstruction across static + dynamic atlas slots.
fn unorm8(v: f32) -> u8 {
    let scaled = (v * 256.0).floor();
    if scaled < 0.0 {
        0
    } else if scaled > 255.0 {
        255
    } else {
        scaled as u8
    }
}

impl Drop for GlyphCache {
    fn drop(&mut self) {
        // Signal shutdown to workers + join. Closes the bounded
        // shutdown_tx by drop; receivers wake from select! with Err.
        drop(std::mem::replace(
            &mut self.shutdown_tx,
            crossbeam_channel::bounded(0).0,
        ));
        for h in self.workers.drain(..) {
            let _ = h.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(cp: u32) -> GlyphKey {
        GlyphKey {
            font_family_id: 0,
            codepoint: cp,
            render_mode: RenderMode::Msdf,
        }
    }

    /// Tests pass a non-existent path; the worker fails to read and
    /// records FontMissing. Tests that need to verify pre-resolution
    /// state must observe quickly (before the worker drains) or use
    /// num_workers=0.
    fn nonexistent_font_path() -> PathBuf {
        PathBuf::from("/nonexistent/font/__unit_test_only__.ttf")
    }

    #[test]
    fn glyph_cache_first_get_returns_none_and_enqueues_request() {
        // 0 workers: state stays Requested forever, no FontMissing race.
        let cache = GlyphCache::new(0);
        let state = cache.get_or_request(k(0x25CF), nonexistent_font_path);
        assert!(state.is_none());
    }

    #[test]
    fn glyph_cache_second_get_returns_resolved_state_does_not_re_enqueue() {
        let cache = GlyphCache::new(1);
        let _ = cache.get_or_request(k(0x25CF), nonexistent_font_path);
        // give the worker a moment to drain + record FontMissing
        std::thread::sleep(std::time::Duration::from_millis(100));
        let state = cache.get_or_request(k(0x25CF), nonexistent_font_path);
        assert!(state.is_some());
        match state.unwrap() {
            SlotState::Requested => {}    // worker hasn't reached it yet
            SlotState::Generating => {}   // worker mid-rasterize
            SlotState::FontMissing => {}  // worker recorded read-fail
            SlotState::Ready { .. } => panic!("nonexistent path can't resolve to Ready"),
        }
    }

    #[test]
    fn glyph_cache_request_count_increments_per_unique_key() {
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x25CF), nonexistent_font_path);
        cache.get_or_request(k(0x221E), nonexistent_font_path);
        cache.get_or_request(k(0x25CF), nonexistent_font_path); // duplicate, NOT counted again
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert_eq!(cache.request_count(), 2);
    }

    #[test]
    fn glyph_cache_different_render_modes_are_distinct_keys() {
        let cache = GlyphCache::new(1);
        let k_msdf = GlyphKey {
            font_family_id: 0,
            codepoint: 0x25CF,
            render_mode: RenderMode::Msdf,
        };
        let k_colr = GlyphKey {
            font_family_id: 0,
            codepoint: 0x25CF,
            render_mode: RenderMode::Colr,
        };
        cache.get_or_request(k_msdf, nonexistent_font_path);
        cache.get_or_request(k_colr, nonexistent_font_path);
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert_eq!(cache.request_count(), 2);
    }

    #[test]
    fn glyph_cache_different_fonts_are_distinct_keys() {
        let cache = GlyphCache::new(1);
        let k0 = GlyphKey {
            font_family_id: 0,
            codepoint: 0x41, // 'A'
            render_mode: RenderMode::Msdf,
        };
        let k1 = GlyphKey {
            font_family_id: 1,
            codepoint: 0x41,
            render_mode: RenderMode::Msdf,
        };
        cache.get_or_request(k0, nonexistent_font_path);
        cache.get_or_request(k1, nonexistent_font_path);
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert_eq!(cache.request_count(), 2);
    }

    #[test]
    fn glyph_cache_zero_workers_is_valid() {
        // Edge case: 0 workers means no one will ever process the
        // queue. get_or_request still works; state stays Requested
        // forever (or until cache is dropped).
        let cache = GlyphCache::new(0);
        cache.get_or_request(k(0x25CF), nonexistent_font_path);
        let s = cache.get_or_request(k(0x25CF), nonexistent_font_path).unwrap();
        assert!(matches!(s, SlotState::Requested));
    }

    #[test]
    fn worker_records_font_missing_on_unreadable_path() {
        let cache = GlyphCache::new(2);
        cache.get_or_request(k(0x25CF), nonexistent_font_path);
        // Worker reads "/nonexistent/...", fails, inserts FontMissing.
        // 200 ms should be more than enough on any host.
        for _ in 0..40 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if matches!(
                cache.get_or_request(k(0x25CF), nonexistent_font_path),
                Some(SlotState::FontMissing)
            ) {
                return; // pass
            }
        }
        panic!("worker never recorded FontMissing for unreadable path");
    }

    #[test]
    fn worker_rasterizes_codepoint_present_in_font() {
        // Round-trip via the real msdfgen worker: read a TTF from the
        // build artifacts (ui/fonts/inter.ttf -- always present in the
        // checkout), rasterize 'A', verify a Completion lands in the
        // channel (Mac/Linux both run the worker; only the GL upload
        // is Linux-only).
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let font_path = manifest_dir
            .parent()
            .expect("renderer parent dir")
            .join("ui/fonts/inter.ttf");
        if !font_path.exists() {
            // Some build environments may not have ui/fonts/ checked
            // out; skip rather than fail.
            eprintln!("skip: {:?} not present", font_path);
            return;
        }
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x41), || font_path.clone()); // 'A'
        // Wait up to ~2 s for the worker to rasterize. msdfgen on Mac
        // takes <30 ms per cell; the longer ceiling is for the slowest
        // CI worker class.
        let mut got = None;
        for _ in 0..400 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if let Ok(c) = cache.completion_rx.try_recv() {
                got = Some(c);
                break;
            }
        }
        let c = got.expect("worker should produce a Completion within 2s");
        match c {
            Completion::Ready {
                key: _,
                rgba_bytes,
                cell_px,
                advance_em,
                plane_bounds,
            } => {
                assert_eq!(cell_px, 48);
                assert_eq!(rgba_bytes.len(), 48 * 48 * 4);
                for i in (0..rgba_bytes.len()).step_by(4) {
                    assert_eq!(rgba_bytes[i + 3], 255, "alpha not 255 at byte {i}");
                }
                assert!(advance_em > 0.0, "advance_em should be positive for 'A'");
                assert!(
                    plane_bounds.pl_right > plane_bounds.pl_left,
                    "plane bounds inverted",
                );
                assert!(
                    plane_bounds.pl_top > plane_bounds.pl_bottom,
                    "plane bounds inverted",
                );
            }
            Completion::FontMissing { .. } => {
                panic!("expected Completion::Ready for 'A' in inter.ttf, got FontMissing");
            }
        }
    }

    #[test]
    fn worker_records_font_missing_for_codepoint_not_in_font() {
        // Inter doesn't have U+2603 (snowman). The worker should
        // distinguish "font lacks codepoint" from "I/O failure" but
        // both surface as FontMissing -- dispatch behavior is the
        // same (Tofu permanently).
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let font_path = manifest_dir
            .parent()
            .expect("renderer parent dir")
            .join("ui/fonts/inter.ttf");
        if !font_path.exists() {
            return;
        }
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x2603), || font_path.clone()); // snowman
        for _ in 0..200 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if matches!(
                cache.get_or_request(k(0x2603), || font_path.clone()),
                Some(SlotState::FontMissing)
            ) {
                return; // pass
            }
        }
        panic!("worker never recorded FontMissing for absent codepoint");
    }

    #[test]
    fn inject_completion_marks_slot_ready() {
        let cache = GlyphCache::new(1);
        let key = k(0x25CF);
        cache.get_or_request(key, nonexistent_font_path);
        cache.inject_completion_for_test(
            key,
            vec![0; 48 * 48 * 4],
            48,
            0.5,
            PlaneBounds {
                pl_left: -0.1,
                pl_right: 0.6,
                pl_top: 0.8,
                pl_bottom: -0.1,
            },
        );
        let state = cache.get_or_request(key, nonexistent_font_path).unwrap();
        match state {
            SlotState::Ready { advance_em, plane_bounds, .. } => {
                assert!((advance_em - 0.5).abs() < 1e-6);
                assert!((plane_bounds.pl_right - 0.6).abs() < 1e-6);
            }
            other => panic!("expected Ready, got {:?}", other),
        }
        assert_eq!(cache.completion_count(), 1);
    }

    #[test]
    fn glyph_cache_drops_cleanly_when_workers_busy() {
        // Make sure Drop doesn't hang when workers have unread queue.
        let cache = GlyphCache::new(4);
        for i in 0..100 {
            cache.get_or_request(k(0x2500 + i), nonexistent_font_path);
        }
        drop(cache); // should join all workers cleanly
    }

    #[test]
    fn plane_bounds_default_is_zero() {
        let pb = PlaneBounds::default();
        assert_eq!(pb.pl_left, 0.0);
        assert_eq!(pb.pl_right, 0.0);
        assert_eq!(pb.pl_top, 0.0);
        assert_eq!(pb.pl_bottom, 0.0);
    }

    #[test]
    fn glyph_key_hash_works_in_hashmap() {
        let mut map: HashMap<GlyphKey, u32> = HashMap::new();
        map.insert(k(0x41), 1);
        map.insert(k(0x42), 2);
        assert_eq!(map.get(&k(0x41)), Some(&1));
        assert_eq!(map.get(&k(0x42)), Some(&2));
        assert_eq!(map.get(&k(0x43)), None);
    }

    #[test]
    fn font_family_id_from_stem_is_deterministic() {
        let a = font_family_id_from_stem("vt323");
        let b = font_family_id_from_stem("vt323");
        assert_eq!(a, b);
    }

    #[test]
    fn font_family_id_differs_across_stems() {
        // Bug 3 Slice 2D collision sanity: the production catalog
        // (~24 fonts + 1 fallback) should have zero FNV-1a low-32
        // collisions. Spot-check a few critical pairs.
        let pairs: &[(&str, &str)] = &[
            ("vt323", "dejavu-sans"),
            ("anton", "dejavu-sans"),
            ("inter", "dejavu-sans"),
            ("vt323", "anton"),
        ];
        for (a, b) in pairs {
            assert_ne!(
                font_family_id_from_stem(a),
                font_family_id_from_stem(b),
                "stems {:?} / {:?} collide on FNV-1a low 32 bits",
                a, b,
            );
        }
    }

    #[test]
    fn fallback_chain_contains_dejavu_sans() {
        // Bug 3 Slice 2D: the fallback chain must include DejaVu
        // Sans as the canonical Geometric Shapes / Mathematical
        // Operators / Box Drawing fallback. Empty chain would
        // silently revert Slice 2D to permanent-Tofu behavior.
        assert!(!FALLBACK_FONT_STEMS.is_empty());
        assert!(FALLBACK_FONT_STEMS.contains(&"dejavu-sans"));
    }

    #[test]
    fn worker_rasterizes_geometric_shape_via_dejavu_sans() {
        // Bug 3 Slice 2D: confirm the bundled DejaVu Sans TTF can
        // rasterize a Geometric Shape codepoint (● U+25CF -- the
        // FYS Boot slide gap). End-to-end worker test: read TTF
        // -> ttf-parser -> msdfgen -> Completion via channel.
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let font_path = manifest_dir
            .parent()
            .expect("renderer parent dir")
            .join("ui/fonts/dejavu-sans.ttf");
        if !font_path.exists() {
            eprintln!("skip: {:?} not present", font_path);
            return;
        }
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x25CF), move || font_path); // ●
        let mut got = None;
        for _ in 0..400 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if let Ok(c) = cache.completion_rx.try_recv() {
                got = Some(c);
                break;
            }
        }
        let c = got.expect("DejaVu Sans worker should produce ● Completion within 2s");
        match c {
            Completion::Ready {
                key: _,
                rgba_bytes,
                cell_px,
                advance_em,
                plane_bounds,
            } => {
                assert_eq!(cell_px, 48);
                assert_eq!(rgba_bytes.len(), 48 * 48 * 4);
                for i in (0..rgba_bytes.len()).step_by(4) {
                    assert_eq!(rgba_bytes[i + 3], 255);
                }
                assert!(advance_em > 0.0);
                assert!(
                    plane_bounds.pl_right > plane_bounds.pl_left,
                    "● plane bounds inverted",
                );
                assert!(
                    plane_bounds.pl_top > plane_bounds.pl_bottom,
                    "● plane bounds inverted",
                );
            }
            Completion::FontMissing { .. } => {
                panic!("expected Completion::Ready for ● in dejavu-sans.ttf, got FontMissing");
            }
        }
    }

    #[test]
    fn worker_sends_font_missing_via_channel() {
        // Bug 3 Slice 2D follow-up (2026-05-19): when the worker
        // direct-inserts SlotState::FontMissing into the slots
        // map (font lacks codepoint OR I/O failure), it ALSO sends
        // a Completion::FontMissing notification through the
        // completion channel so the render-thread's poll can
        // observe the transition and invalidate slide_caches.
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x25CF), nonexistent_font_path);
        let mut got = None;
        for _ in 0..400 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if let Ok(c) = cache.completion_rx.try_recv() {
                got = Some(c);
                break;
            }
        }
        let c = got.expect("worker should send Completion::FontMissing within 2s");
        match c {
            Completion::FontMissing { key } => {
                assert_eq!(key.codepoint, 0x25CF);
            }
            Completion::Ready { .. } => {
                panic!("expected FontMissing notification, got Ready");
            }
        }
    }

    // ---- Slice 3C: LRU atlas eviction --------------------------------

    fn colr_key(cp: u32) -> GlyphKey {
        GlyphKey {
            font_family_id: 7,
            codepoint: cp,
            render_mode: RenderMode::Colr,
        }
    }

    /// Inject a Ready entry straight into the cache's private maps:
    /// allocate a real slot on `page`, record SlotState::Ready,
    /// stamp recency. Lets the eviction tests build a known cache
    /// state without spinning up workers + real fonts.
    fn inject_ready(
        cache: &GlyphCache,
        page: &mut crate::atlas_page::AtlasPage,
        key: GlyphKey,
    ) -> SlotPos {
        let slot = page
            .allocate_slot()
            .expect("page has room for the test fixture");
        cache.slots.lock().unwrap().insert(
            key,
            SlotState::Ready {
                slot,
                advance_em: 1.0,
                plane_bounds: PlaneBounds::default(),
            },
        );
        cache.touch_recency(key);
        slot
    }

    #[test]
    fn evict_lru_ready_removes_the_coldest_ready_slot() {
        let cache = GlyphCache::new(0);
        let mut page = crate::atlas_page::AtlasPage::new(96);
        // 3 Ready COLR glyphs; recency rises 1,2,3 as injected.
        let a = colr_key(0xA);
        let b = colr_key(0xB);
        let c = colr_key(0xC);
        let slot_a = inject_ready(&cache, &mut page, a);
        inject_ready(&cache, &mut page, b);
        inject_ready(&cache, &mut page, c);
        // Touch B and C again so A is unambiguously the LRU.
        cache.touch_recency(b);
        cache.touch_recency(c);
        // Evict — A (coldest) must go; its cell must be freed.
        assert!(cache.evict_lru_ready(&mut page, RenderMode::Colr));
        {
            let slots = cache.slots.lock().unwrap();
            assert!(!slots.contains_key(&a), "coldest key A must be evicted");
            assert!(slots.contains_key(&b), "B was used more recently — keep");
            assert!(slots.contains_key(&c), "C was used most recently — keep");
        }
        assert!(
            !cache.recency.lock().unwrap().last_used.contains_key(&a),
            "evicted key must be dropped from recency too",
        );
        // A's freed cell is handed back by the next allocate.
        assert_eq!(page.allocate_slot().unwrap(), slot_a);
    }

    #[test]
    fn evict_lru_ready_never_touches_in_flight_or_fontmissing_slots() {
        // THE subtle correctness point: only Ready entries are
        // eviction candidates. Requested / Generating hold no atlas
        // slot — a worker mid-rasterize must never have its target
        // cell reused. FontMissing holds no slot either.
        let cache = GlyphCache::new(0);
        let mut page = crate::atlas_page::AtlasPage::new(96);
        let ready = colr_key(0x1);
        let requested = colr_key(0x2);
        let generating = colr_key(0x3);
        let missing = colr_key(0x4);
        inject_ready(&cache, &mut page, ready);
        {
            let mut slots = cache.slots.lock().unwrap();
            slots.insert(requested, SlotState::Requested);
            slots.insert(generating, SlotState::Generating);
            slots.insert(missing, SlotState::FontMissing);
        }
        // Only the single Ready entry is eligible.
        assert!(cache.evict_lru_ready(&mut page, RenderMode::Colr));
        {
            let slots = cache.slots.lock().unwrap();
            assert!(!slots.contains_key(&ready), "the Ready slot was evicted");
            assert!(
                matches!(slots.get(&requested), Some(SlotState::Requested)),
                "in-flight Requested slot must NOT be evicted",
            );
            assert!(
                matches!(slots.get(&generating), Some(SlotState::Generating)),
                "in-flight Generating slot must NOT be evicted",
            );
            assert!(
                matches!(slots.get(&missing), Some(SlotState::FontMissing)),
                "FontMissing slot must NOT be evicted",
            );
        }
        // With no Ready entries left, a further eviction finds no
        // victim and reports false (caller then drops the completion).
        assert!(!cache.evict_lru_ready(&mut page, RenderMode::Colr));
    }

    #[test]
    fn evict_lru_ready_only_evicts_the_matching_render_mode() {
        // The 48 px MSDF page and the 96 px COLR page evict
        // independently — a full COLR page must not steal an MSDF
        // slot, and vice versa.
        let cache = GlyphCache::new(0);
        let mut msdf_page = crate::atlas_page::AtlasPage::new(48);
        let mut colr_page = crate::atlas_page::AtlasPage::new(96);
        let msdf_glyph = GlyphKey {
            font_family_id: 1,
            codepoint: 0x41,
            render_mode: RenderMode::Msdf,
        };
        let colr_glyph = colr_key(0x1F600);
        inject_ready(&cache, &mut msdf_page, msdf_glyph);
        inject_ready(&cache, &mut colr_page, colr_glyph);
        // Evicting for COLR takes the COLR glyph, leaves MSDF intact.
        assert!(cache.evict_lru_ready(&mut colr_page, RenderMode::Colr));
        let slots = cache.slots.lock().unwrap();
        assert!(!slots.contains_key(&colr_glyph));
        assert!(
            slots.contains_key(&msdf_glyph),
            "an MSDF glyph must survive a COLR-page eviction",
        );
    }

    /// Round 8 tripwire: eviction's O(log N) BTreeMap path picks the
    /// truly-coldest entry even when there are many entries AND when
    /// re-touched entries shift the cold-end. Pre-r8 was O(N) HashMap
    /// scan -- this lock-in regression-fences the BTreeMap reverse
    /// index against silent staleness (e.g., touch_recency forgetting
    /// to remove the old tick from by_tick would leave a stale
    /// (old_tick -> key) at the front of the map, mis-picking a
    /// previously-hot key as the victim).
    #[test]
    fn evict_lru_ready_btreemap_picks_coldest_among_many_and_after_retouch() {
        let cache = GlyphCache::new(0);
        let mut page = crate::atlas_page::AtlasPage::new(96);
        // 8 entries: 0..8, touched in insertion order so tick = 1..8.
        let keys: Vec<GlyphKey> = (0..8).map(|i| colr_key(0x100 + i)).collect();
        for k in &keys {
            inject_ready(&cache, &mut page, *k);
        }
        // Re-touch keys[0] (the originally coldest). Now its tick is
        // 9 (post the 8 inserts). The new coldest is keys[1].
        cache.touch_recency(keys[0]);
        // Confirm the BTreeMap reflects the re-touch (no stale tick
        // remains for keys[0]) -- this is the precise invariant the
        // r8 refactor depends on.
        {
            let r = cache.recency.lock().unwrap();
            let by_tick: Vec<GlyphKey> =
                r.by_tick_colr.values().copied().collect();
            // by_tick is sorted by tick ascending; the front should
            // now be keys[1] (originally tick=2 after inject_ready),
            // and keys[0] should be at the back (tick=9, the most
            // recent re-touch).
            assert_eq!(by_tick.first().copied(), Some(keys[1]));
            assert_eq!(by_tick.last().copied(), Some(keys[0]));
            // No stale (old_tick=1 -> keys[0]) entry surviving the
            // re-touch.
            assert_eq!(by_tick.len(), 8);
        }
        // Evict: must pick keys[1] (the post-re-touch coldest),
        // NOT keys[0] (which would be the pre-fix HashMap-scan
        // victim if the BTreeMap held stale ticks).
        assert!(cache.evict_lru_ready(&mut page, RenderMode::Colr));
        let slots = cache.slots.lock().unwrap();
        assert!(
            !slots.contains_key(&keys[1]),
            "coldest after re-touch (keys[1]) must be the victim",
        );
        assert!(
            slots.contains_key(&keys[0]),
            "re-touched keys[0] must NOT be evicted (no stale tick)",
        );
        // The other 6 keys also survive.
        for k in keys.iter().skip(2) {
            assert!(slots.contains_key(k), "key {k:?} should remain cached");
        }
    }

    #[test]
    fn evicted_glyph_re_requests_on_next_lookup() {
        // An evicted glyph needed again is a plain cache miss:
        // get_or_request returns None and re-enqueues a MissRequest
        // — the worker re-rasterizes it. Expected, correct cost.
        let cache = GlyphCache::new(0);
        let mut page = crate::atlas_page::AtlasPage::new(96);
        let key = colr_key(0x2764);
        inject_ready(&cache, &mut page, key);
        // Pre-eviction: a lookup is a Ready hit.
        assert!(matches!(
            cache.get_or_request(key, nonexistent_font_path),
            Some(SlotState::Ready { .. }),
        ));
        // Evict it.
        assert!(cache.evict_lru_ready(&mut page, RenderMode::Colr));
        // Post-eviction: the lookup misses → None → re-Requested.
        assert!(
            cache
                .get_or_request(key, nonexistent_font_path)
                .is_none(),
            "an evicted glyph must miss + re-request",
        );
        assert!(matches!(
            cache.slots.lock().unwrap().get(&key),
            Some(SlotState::Requested),
        ));
    }
}
