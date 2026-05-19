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
// Slice 1 scope: infrastructure only. Worker thread is a STUB that
// receives MissRequest and discards (returns no completion). The
// dispatch in layout_text_to_quads will see a cache slot in state
// Requested and fall through to Tofu for now. Slice 2 wires the
// real msdfgen-based worker.
//
// Thread model:
//   Render thread:
//     - Calls get_or_request(key) on layout pass.
//     - Calls poll_completions() at frame start to drain worker
//       output + perform glTexSubImage2D uploads.
//     - Single-threaded GL access; never blocks on workers.
//   Worker pool (Slice 2: 4 std::thread workers):
//     - Drain MissRequest from crossbeam-channel mpsc.
//     - Run msdfgen on background.
//     - Push Completion back to render thread via second channel.
//
// Capacity bookkeeping: GlyphCache holds N atlas pages (default 1
// for Slice 1; LRU eviction or page-grow is a Slice 1.x follow-up
// triggered by first observed pressure).

use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;

use crossbeam_channel::{Receiver, Sender};

use crate::atlas_page::SlotPos;
#[cfg(target_os = "linux")]
use crate::atlas_page::AtlasPage;

/// Cache key. font_family_id is the renderer's local 0..N font index
/// (NOT the family name) to keep the key Copy + Hash cheap.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct GlyphKey {
    pub font_family_id: u8,
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
}

/// Internal: a unit of work pushed onto the worker channel. Slice 2
/// will add font_bytes + size_px so the worker has everything it
/// needs to run msdfgen without touching shared state.
#[derive(Clone, Debug)]
struct MissRequest {
    key: GlyphKey,
}

/// Internal: a completion pushed back to the render thread by a
/// worker. Slice 1 stub workers never emit these; Slice 2 will.
#[allow(dead_code)] // fields used in Slice 2
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
    /// Spawn `num_workers` background threads. Each runs a stub loop
    /// for Slice 1 (drains work + does nothing). Slice 2 will replace
    /// the loop body with msdfgen rasterization.
    pub fn new(num_workers: usize) -> Self {
        let (work_tx, work_rx) = crossbeam_channel::unbounded::<MissRequest>();
        let (completion_tx, completion_rx) = crossbeam_channel::unbounded::<Completion>();
        let (shutdown_tx, shutdown_rx) = crossbeam_channel::bounded::<()>(0);
        let request_count = Arc::new(Mutex::new(0u64));

        let mut workers = Vec::with_capacity(num_workers);
        for _ in 0..num_workers {
            let rx = work_rx.clone();
            let _completion_tx = completion_tx.clone();
            let sd = shutdown_rx.clone();
            let req_ctr = Arc::clone(&request_count);
            workers.push(std::thread::spawn(move || {
                // Slice 1 worker stub: receive MissRequest, increment
                // counter, discard. Slice 2 replaces this with real
                // msdfgen + completion_tx.send().
                loop {
                    crossbeam_channel::select! {
                        recv(rx) -> msg => {
                            match msg {
                                Ok(_req) => {
                                    let mut ctr = req_ctr.lock().unwrap();
                                    *ctr += 1;
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
            slots: Arc::new(Mutex::new(HashMap::new())),
            work_tx,
            completion_rx,
            workers,
            shutdown_tx,
            request_count,
            completion_count: Arc::new(Mutex::new(0)),
        }
    }

    /// Look up a glyph by key. Returns:
    ///   Some(Ready { .. })       — atlas slot is allocated + GPU-resident
    ///   Some(Requested|Generating) — worker is on it; render-side should
    ///                                use a placeholder (Tofu for Slice 1)
    ///   None                     — first encounter; a MissRequest is
    ///                                enqueued; caller should render as
    ///                                placeholder this frame
    /// Idempotent across calls — second-call for an in-flight key
    /// returns the existing state (does NOT re-enqueue).
    pub fn get_or_request(&self, key: GlyphKey) -> Option<SlotState> {
        let mut slots = self.slots.lock().unwrap();
        if let Some(state) = slots.get(&key) {
            return Some(state.clone());
        }
        slots.insert(key, SlotState::Requested);
        // Send is unbounded so this never blocks; ignore the result
        // because send-error only happens if all workers panicked,
        // which we'd surface via panic-handler elsewhere.
        let _ = self.work_tx.send(MissRequest { key });
        None
    }

    /// Drain completed-work queue and upload to the GPU. Render-thread
    /// only — `gl` and `page` must be the active EGL session's. Bounded
    /// by `max_uploads_per_call` so a backlog doesn't blow the 16 ms
    /// frame budget at first encounter.
    ///
    /// Slice 1: stub workers emit no completions, so this is a no-op
    /// at runtime. Slice 2 wires the actual completion → upload flow.
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

    /// Test seam: deliver a fake completion. Slice 2 will replace with
    /// real worker output. Lets unit tests verify the upload-side
    /// state-machine transitions without needing a real msdfgen run.
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

    #[test]
    fn glyph_cache_first_get_returns_none_and_enqueues_request() {
        let cache = GlyphCache::new(1);
        let state = cache.get_or_request(k(0x25CF));
        assert!(state.is_none());
    }

    #[test]
    fn glyph_cache_second_get_returns_requested_state_does_not_re_enqueue() {
        let cache = GlyphCache::new(1);
        let _ = cache.get_or_request(k(0x25CF));
        // give the worker a moment to drain the queue
        std::thread::sleep(std::time::Duration::from_millis(50));
        let state = cache.get_or_request(k(0x25CF));
        assert!(state.is_some());
        match state.unwrap() {
            SlotState::Requested => {} // workers ran but no completion
            SlotState::Generating => {}
            SlotState::Ready { .. } => {} // shouldn't happen in Slice 1 stub
        }
    }

    #[test]
    fn glyph_cache_request_count_increments_per_unique_key() {
        let cache = GlyphCache::new(1);
        cache.get_or_request(k(0x25CF));
        cache.get_or_request(k(0x221E));
        cache.get_or_request(k(0x25CF)); // duplicate, NOT counted again
        std::thread::sleep(std::time::Duration::from_millis(50));
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
        cache.get_or_request(k_msdf);
        cache.get_or_request(k_colr);
        std::thread::sleep(std::time::Duration::from_millis(50));
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
        cache.get_or_request(k0);
        cache.get_or_request(k1);
        std::thread::sleep(std::time::Duration::from_millis(50));
        assert_eq!(cache.request_count(), 2);
    }

    #[test]
    fn glyph_cache_zero_workers_is_valid() {
        // Edge case: 0 workers means no one will ever process the
        // queue. get_or_request still works; state stays Requested
        // forever (or until cache is dropped).
        let cache = GlyphCache::new(0);
        cache.get_or_request(k(0x25CF));
        let s = cache.get_or_request(k(0x25CF)).unwrap();
        assert!(matches!(s, SlotState::Requested));
    }

    #[test]
    fn inject_completion_marks_slot_ready() {
        let cache = GlyphCache::new(1);
        let key = k(0x25CF);
        cache.get_or_request(key);
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
        let state = cache.get_or_request(key).unwrap();
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
            cache.get_or_request(k(0x2500 + i));
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
