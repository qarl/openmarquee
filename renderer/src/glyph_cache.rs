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

use std::collections::HashMap;
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
/// either of these here only is a parity break.
const CELL_PX: u32 = 48;
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
    /// COLRv1 vector emoji — Slice 3 will wire the colr crate
    /// behind this variant. Slice 1 stubs it.
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
/// worker. Carries the rasterized MSDF cell + the per-glyph metrics
/// the dispatch hook needs to position the quad. The FontMissing
/// path does NOT flow through this channel — workers insert that
/// directly into the slots map since it needs no GL upload work.
struct Completion {
    key: GlyphKey,
    rgba_bytes: Vec<u8>,
    cell_px: u32,
    advance_em: f32,
    plane_bounds: PlaneBounds,
}

pub struct GlyphCache {
    /// state map; locked on every get / insert / completion drain.
    slots: Arc<Mutex<HashMap<GlyphKey, SlotState>>>,
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
                                    match rasterize_msdf_cell(&req.font_path, req.key.codepoint) {
                                        Ok(Some(out)) => {
                                            let _ = completion_tx.send(Completion {
                                                key: req.key,
                                                rgba_bytes: out.rgba_bytes,
                                                cell_px: out.cell_px,
                                                advance_em: out.advance_em,
                                                plane_bounds: out.plane_bounds,
                                            });
                                        }
                                        Ok(None) => {
                                            // Font genuinely lacks this
                                            // codepoint; record permanent
                                            // miss so dispatch stops
                                            // re-enqueueing.
                                            let mut slots = slots_for_worker.lock().unwrap();
                                            slots.insert(req.key, SlotState::FontMissing);
                                        }
                                        Err(e) => {
                                            eprintln!(
                                                "glyph_cache worker: rasterize {:?} cp=U+{:04X}: {e}",
                                                req.font_path, req.key.codepoint,
                                            );
                                            let mut slots = slots_for_worker.lock().unwrap();
                                            slots.insert(req.key, SlotState::FontMissing);
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
            work_tx,
            completion_rx,
            workers,
            shutdown_tx,
            request_count,
            completion_count: Arc::new(Mutex::new(0)),
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
    /// `font_path` is the TTF file the worker should rasterize from. The
    /// dispatch hook resolves it from the atlas manifest's font stem +
    /// the renderer's fonts_dir.
    pub fn get_or_request(&self, key: GlyphKey, font_path: PathBuf) -> Option<SlotState> {
        let mut slots = self.slots.lock().unwrap();
        if let Some(state) = slots.get(&key) {
            return Some(state.clone());
        }
        slots.insert(key, SlotState::Requested);
        // Send is unbounded so this never blocks; ignore the result
        // because send-error only happens if all workers panicked,
        // which we'd surface via panic-handler elsewhere.
        let _ = self.work_tx.send(MissRequest { key, font_path });
        None
    }

    /// Drain completed-work queue and upload to the GPU. Render-thread
    /// only — `gl` and `page` must be the active EGL session's. Bounded
    /// by `max_uploads_per_call` so a backlog doesn't blow the 16 ms
    /// frame budget at first encounter.
    ///
    /// FontMissing doesn't flow through this path — the worker
    /// inserts it directly into slots since no GL upload is needed.
    /// This drain only handles Success completions (cell rasterized,
    /// allocate-slot + glTexSubImage2D + transition to Ready).
    #[cfg(target_os = "linux")]
    pub fn poll_completions(
        &mut self,
        gl: &glow::Context,
        page: &mut AtlasPage,
        max_uploads_per_call: usize,
    ) -> usize {
        let mut uploaded = 0;
        for _ in 0..max_uploads_per_call {
            match self.completion_rx.try_recv() {
                Ok(c) => {
                    let Some(slot_pos) = page.allocate_slot() else {
                        // Page full; Slice 1.x will add eviction.
                        // For now, drop the completion; the slot
                        // entry stays in its prior state (Requested
                        // in Slice 1; Generating once Slice 2 wires
                        // the worker-side state transition) until
                        // restart.
                        eprintln!("glyph_cache: atlas page full; dropping completion for {:?}", c.key);
                        continue;
                    };
                    if let Err(e) = page.upload_slot(
                        gl, slot_pos.x, slot_pos.y, c.cell_px, c.cell_px, &c.rgba_bytes,
                    ) {
                        eprintln!("glyph_cache: upload_slot failed for {:?}: {e}", c.key);
                        continue;
                    }
                    let mut slots = self.slots.lock().unwrap();
                    slots.insert(c.key, SlotState::Ready {
                        slot: slot_pos,
                        advance_em: c.advance_em,
                        plane_bounds: c.plane_bounds,
                    });
                    *self.completion_count.lock().unwrap() += 1;
                    uploaded += 1;
                }
                Err(_) => break, // nothing pending
            }
        }
        uploaded
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
}

/// Worker-side raster output. Mirrors build.rs's per-glyph atlas
/// cell + metrics so the runtime FS_MSDF_FIXED shader path sees
/// identical SDF reconstruction across static-baked + dynamic-
/// cached slots.
struct RasterOutput {
    rgba_bytes: Vec<u8>,
    cell_px: u32,
    advance_em: f32,
    plane_bounds: PlaneBounds,
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
        let state = cache.get_or_request(k(0x25CF), nonexistent_font_path());
        assert!(state.is_none());
    }

    #[test]
    fn glyph_cache_second_get_returns_resolved_state_does_not_re_enqueue() {
        let cache = GlyphCache::new(1);
        let _ = cache.get_or_request(k(0x25CF), nonexistent_font_path());
        // give the worker a moment to drain + record FontMissing
        std::thread::sleep(std::time::Duration::from_millis(100));
        let state = cache.get_or_request(k(0x25CF), nonexistent_font_path());
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
        cache.get_or_request(k(0x25CF), nonexistent_font_path());
        cache.get_or_request(k(0x221E), nonexistent_font_path());
        cache.get_or_request(k(0x25CF), nonexistent_font_path()); // duplicate, NOT counted again
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
        cache.get_or_request(k_msdf, nonexistent_font_path());
        cache.get_or_request(k_colr, nonexistent_font_path());
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
        cache.get_or_request(k0, nonexistent_font_path());
        cache.get_or_request(k1, nonexistent_font_path());
        std::thread::sleep(std::time::Duration::from_millis(100));
        assert_eq!(cache.request_count(), 2);
    }

    #[test]
    fn glyph_cache_zero_workers_is_valid() {
        // Edge case: 0 workers means no one will ever process the
        // queue. get_or_request still works; state stays Requested
        // forever (or until cache is dropped).
        let cache = GlyphCache::new(0);
        cache.get_or_request(k(0x25CF), nonexistent_font_path());
        let s = cache.get_or_request(k(0x25CF), nonexistent_font_path()).unwrap();
        assert!(matches!(s, SlotState::Requested));
    }

    #[test]
    fn worker_records_font_missing_on_unreadable_path() {
        let cache = GlyphCache::new(2);
        cache.get_or_request(k(0x25CF), nonexistent_font_path());
        // Worker reads "/nonexistent/...", fails, inserts FontMissing.
        // 200 ms should be more than enough on any host.
        for _ in 0..40 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if matches!(
                cache.get_or_request(k(0x25CF), nonexistent_font_path()),
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
        cache.get_or_request(k(0x41), font_path.clone()); // 'A'
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
        assert_eq!(c.cell_px, 48);
        assert_eq!(c.rgba_bytes.len(), 48 * 48 * 4);
        // Every fourth byte (alpha) must be 255.
        for i in (0..c.rgba_bytes.len()).step_by(4) {
            assert_eq!(c.rgba_bytes[i + 3], 255, "alpha not 255 at byte {i}");
        }
        // 'A' has positive advance.
        assert!(c.advance_em > 0.0, "advance_em should be positive for 'A'");
        // 'A' has positive ink extent.
        assert!(
            c.plane_bounds.pl_right > c.plane_bounds.pl_left,
            "plane bounds inverted",
        );
        assert!(
            c.plane_bounds.pl_top > c.plane_bounds.pl_bottom,
            "plane bounds inverted",
        );
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
        cache.get_or_request(k(0x2603), font_path.clone()); // snowman
        for _ in 0..200 {
            std::thread::sleep(std::time::Duration::from_millis(5));
            if matches!(
                cache.get_or_request(k(0x2603), font_path.clone()),
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
        cache.get_or_request(key, nonexistent_font_path());
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
        let state = cache.get_or_request(key, nonexistent_font_path()).unwrap();
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
            cache.get_or_request(k(0x2500 + i), nonexistent_font_path());
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
}
