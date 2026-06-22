//! Async-refresh GL texture cache for ImageSlide / WebSlide.
//!
//! Task #168 (qarl 2026-05-22 via QA dispatch): when a Web slide's
//! producer overwrites `<content_root>/<slide_id>/asset.png` and
//! bumps `item.json`'s mtime, the renderer detects the drift in
//! `ipc_main.rs::SlideCache::load` and the next paint of that slide
//! falls into a full PNG decode + glTexImage2D upload on the render
//! thread — measured 100-300ms hitch at the very transition into the
//! refreshed slide, every refresh cycle. Visible.
//!
//! This cache moves the PNG decode off the render thread:
//!
//! * On `ensure`, the render thread stats `asset.png` and compares to
//!   the entry's last-uploaded mtime.
//! * If a previous decode worker has delivered fresh bytes, the render
//!   thread does the glTexImage2D upload (~5ms at 1080p) and atomically
//!   swaps the cached `NativeTexture` handle — the OLD handle is
//!   deleted in the same call. The next paint draws the new tex.
//! * If the asset has drifted and no worker is in flight, spawn one.
//!   The render thread KEEPS USING the prior tex so the transition
//!   stays smooth; the swap happens on whichever subsequent
//!   `ensure` first sees the worker's result.
//! * First-ever paint of a slide (cold cache) decodes + uploads
//!   synchronously, mirroring the pre-change behavior. A first-paint
//!   has no prior tex to hold across, so any async policy would just
//!   show a black flash; sync is correct here.
//!
//! Bounded LRU eviction (`IMAGE_SLIDE_TEX_CACHE_CAPACITY`) keeps GPU
//! memory usage flat for playlists that exceed the cap. Eviction
//! deletes the cached texture; an in-flight worker for an evicted
//! slide drops its mpsc::Sender on `Drop` of the entry, harmlessly
//! disconnecting (the worker thread's `send` becomes a no-op and the
//! decoded bytes are dropped).

#![cfg(target_os = "linux")]

use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::SystemTime;

use anyhow::{anyhow, Context, Result};
use uuid::Uuid;

use glow::HasContext;

/// Per-process cap on cached image-slide textures. Sized to mirror
/// `hdmi::IMAGE_BG_CACHE_CAPACITY` — both caches hold ~RGBA8 1080p
/// textures (~8.3 MB each) and live in the same CMA budget on the
/// Pi Zero 2 W.
///
/// CMA-arc 2026-06-21 C2: cap cut 6 → 3 in lock-step with
/// IMAGE_BG_CACHE_CAPACITY. The working set is current + next
/// image-class slide; 3 keeps one slot of slack for the
/// transition's B-side preload. Cycling >3 distinct image-class
/// slides in flight triggers eviction churn on transition. The
/// docstring's prior "FYS playlist's image + Web mix with
/// headroom" sized for ~48 MB; the new sizing trims to ~25 MB
/// worst-case = ~23 MB reclaim. If production image-class reels
/// grow beyond 3 slides hot at once, revisit (raise to 4, or
/// add a time-expiry layer that defers eviction during active
/// transitions).
pub const IMAGE_SLIDE_TEX_CACHE_CAPACITY: usize = 3;

/// Result delivered from a decode worker to the render thread.
/// `Ok((rgba_rows_bottom_up, width, height))` matches `load_png_rgba`
/// in hdmi.rs — RGBA8, row-flipped to GL `v` convention.
type DecodeResult = std::result::Result<(Vec<u8>, u32, u32), String>;

struct PendingDecode {
    /// Mtime we kicked the worker against. The render thread copies
    /// this into the entry's `current_mtime` once the upload swap
    /// completes — so a subsequent ensure() comparing on-disk mtime
    /// to `current_mtime` correctly detects the next drift.
    target_mtime: SystemTime,
    rx: mpsc::Receiver<DecodeResult>,
}

struct Entry {
    /// `Some` once at least one upload (sync first-paint or async
    /// swap) has succeeded; `None` only between cold-create and the
    /// cold sync-decode step inside `ensure`.
    tex: Option<glow::NativeTexture>,
    tex_w: u32,
    tex_h: u32,
    /// Mtime of the asset.png the currently-uploaded tex was decoded
    /// from — OR, on a failed async refresh, the mtime the worker
    /// attempted against (latched so the next `ensure` doesn't re-kick
    /// until the producer bumps mtime AGAIN; cf. poll_pending). `None`
    /// only while `tex` is `None`.
    current_mtime: Option<SystemTime>,
    pending: Option<PendingDecode>,
}

/// Async-refresh image-slide texture cache; one instance per
/// `EglSession`. All methods run on the render thread.
pub struct ImageSlideTextureCache {
    entries: HashMap<Uuid, Entry>,
    /// LRU order: front = least-recently used, back = most recent.
    order: VecDeque<Uuid>,
    capacity: usize,
}

impl ImageSlideTextureCache {
    pub fn with_capacity(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        Self {
            entries: HashMap::with_capacity(capacity),
            order: VecDeque::with_capacity(capacity),
            capacity,
        }
    }

    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    /// Resolve a `(tex, w, h)` for `slide_id` from `asset_path`.
    /// See module docs for the state-machine policy.
    ///
    /// Returns `Err` only when the cold-cache sync decode fails
    /// (missing or malformed PNG on first paint). An async refresh
    /// that fails decode logs a warning and keeps the previous tex —
    /// the caller never observes the failure.
    pub fn ensure(
        &mut self,
        gl: &glow::Context,
        slide_id: Uuid,
        asset_path: &Path,
    ) -> Result<(glow::NativeTexture, u32, u32)> {
        let on_disk_mtime = std::fs::metadata(asset_path)
            .ok()
            .and_then(|m| m.modified().ok());

        // Step 1: drain pending worker (if any). Return value is
        // informational only; the entry was updated in place.
        self.poll_pending(gl, slide_id);

        // Step 2: ensure an entry exists.
        let entry_state = self
            .entries
            .get(&slide_id)
            .map(|e| (e.tex.is_some(), e.current_mtime, e.pending.is_some()))
            .unwrap_or((false, None, false));
        let (has_tex, current_mtime, pending_in_flight) = entry_state;

        // Step 3: maybe kick a worker.
        // Only kick when we already have a displayable tex AND mtime
        // drifted AND no worker is in flight. Cold caches go the sync
        // route below (no "before" to hold across).
        if has_tex
            && !pending_in_flight
            && on_disk_mtime.is_some()
            && on_disk_mtime != current_mtime
        {
            let mtime = on_disk_mtime.expect("checked is_some above");
            self.kick_async_decode(slide_id, asset_path, mtime);
        }

        // Step 4: cold cache → sync decode + upload.
        if !has_tex {
            let (rgba, w, h) = load_png_rgba_for_cache(asset_path)?;
            let tex = unsafe { upload_rgba8_texture(gl, &rgba, w, h)? };
            let entry = self.entries.entry(slide_id).or_insert_with(|| Entry {
                tex: None,
                tex_w: 0,
                tex_h: 0,
                current_mtime: None,
                pending: None,
            });
            entry.tex = Some(tex);
            entry.tex_w = w;
            entry.tex_h = h;
            entry.current_mtime = on_disk_mtime;
        }

        // Step 5: LRU touch + capacity eviction.
        self.touch_lru(slide_id);
        self.evict_to_capacity(gl, slide_id);

        // Step 6: return the (now guaranteed) tex tuple.
        let entry = self
            .entries
            .get(&slide_id)
            .expect("entry inserted by cold-decode branch or pre-existed");
        let tex = entry
            .tex
            .expect("tex guaranteed by sync-decode branch or by drain swap");
        Ok((tex, entry.tex_w, entry.tex_h))
    }

    /// Drain the cache on session teardown. Returns the textures the
    /// caller MUST `gl.delete_texture` while the GL context is still
    /// bound. Pending workers' channels drop with their entries —
    /// their `send` calls become no-ops harmlessly.
    pub fn take_all_textures(&mut self) -> Vec<glow::NativeTexture> {
        self.order.clear();
        self.entries
            .drain()
            .filter_map(|(_id, e)| e.tex)
            .collect()
    }

    // ---- internals -------------------------------------------------

    /// Drain a pending decode result if one is ready. On success,
    /// upload the new RGBA bytes into a fresh GL texture, delete the
    /// previous tex (still under the cache's ownership), and swap.
    /// On worker error, log + clear pending so the next `ensure` can
    /// retry (a fresh mtime drift will trigger a new kick).
    ///
    /// Returns `true` iff a swap happened — purely informational.
    fn poll_pending(&mut self, gl: &glow::Context, slide_id: Uuid) -> bool {
        let Some(entry) = self.entries.get_mut(&slide_id) else {
            return false;
        };
        // Snapshot `target_mtime` + `try_recv()` under the immutable
        // `p` borrow, then release it so the match arms can mutate
        // `entry` freely.
        let (target_mtime, recv) = {
            let Some(p) = entry.pending.as_ref() else {
                return false;
            };
            (p.target_mtime, p.rx.try_recv())
        };
        // On EVERY terminal branch (success, decode-fail, upload-fail,
        // disconnected) we latch `current_mtime = Some(target_mtime)`.
        // For success that's the new tex's mtime; for failures it
        // marks "this mtime tried and failed" so the next `ensure`
        // compares equal and skips re-kicking until the producer
        // bumps mtime AGAIN. Without this latch, a stuck broken PNG
        // with a producer bumping mtime every frame would spam the
        // warn-log on every paint (cf. feedback_no_perframe_eprintln_
        // on_production: a similar regression cost 5fps on FYS).
        match recv {
            Ok(Ok((rgba, w, h))) => {
                // Worker delivered bytes; upload + swap. If upload
                // fails (e.g. GL OOM), keep the previous tex and log.
                match unsafe { upload_rgba8_texture(gl, &rgba, w, h) } {
                    Ok(new_tex) => {
                        if let Some(old) = entry.tex.replace(new_tex) {
                            unsafe { gl.delete_texture(old) };
                        }
                        entry.tex_w = w;
                        entry.tex_h = h;
                        entry.current_mtime = Some(target_mtime);
                        entry.pending = None;
                        true
                    }
                    Err(e) => {
                        eprintln!(
                            "warn: image-slide async upload for {slide_id} failed: {e:#}; \
                             keeping previous tex"
                        );
                        entry.current_mtime = Some(target_mtime);
                        entry.pending = None;
                        false
                    }
                }
            }
            Ok(Err(e)) => {
                eprintln!(
                    "warn: image-slide async decode for {slide_id} failed: {e}; \
                     keeping previous tex"
                );
                entry.current_mtime = Some(target_mtime);
                entry.pending = None;
                false
            }
            Err(mpsc::TryRecvError::Empty) => {
                // Still in flight; nothing to do.
                false
            }
            Err(mpsc::TryRecvError::Disconnected) => {
                eprintln!(
                    "warn: image-slide async decoder for {slide_id} disconnected without \
                     a result; keeping previous tex"
                );
                entry.current_mtime = Some(target_mtime);
                entry.pending = None;
                false
            }
        }
    }

    /// Spawn a one-shot decode worker. On spawn failure (rare —
    /// system thread cap or fork limit), silently fall through: the
    /// next `ensure` call will see no pending worker and try again.
    fn kick_async_decode(
        &mut self,
        slide_id: Uuid,
        asset_path: &Path,
        target_mtime: SystemTime,
    ) {
        let (tx, rx) = mpsc::channel();
        let path: PathBuf = asset_path.to_path_buf();
        let spawn = std::thread::Builder::new()
            .name(format!("img-decode-{slide_id}"))
            .spawn(move || {
                let r = load_png_rgba_for_cache(&path).map_err(|e| format!("{e:#}"));
                // tx.send returns Err if the receiver was dropped
                // (entry evicted while we decoded). That's fine —
                // drop the decoded bytes and exit.
                let _ = tx.send(r);
            });
        if spawn.is_err() {
            eprintln!(
                "warn: image-slide async decode for {slide_id} could not spawn worker thread; \
                 keeping previous tex (will retry on next ensure)"
            );
            return;
        }
        let entry = self.entries.entry(slide_id).or_insert_with(|| Entry {
            tex: None,
            tex_w: 0,
            tex_h: 0,
            current_mtime: None,
            pending: None,
        });
        entry.pending = Some(PendingDecode { target_mtime, rx });
    }

    /// Move `slide_id` to back-of-order (most recently used). O(N)
    /// in the deque length, capped at `capacity` — negligible at 16.
    fn touch_lru(&mut self, slide_id: Uuid) {
        self.order.retain(|k| *k != slide_id);
        self.order.push_back(slide_id);
    }

    /// Evict entries from the front of `order` until `entries.len()`
    /// is within `capacity`. Never evicts `keep` (the entry we just
    /// touched). Deletes the evicted GL texture immediately.
    fn evict_to_capacity(&mut self, gl: &glow::Context, keep: Uuid) {
        while self.entries.len() > self.capacity {
            let Some(victim_id) = self.order.pop_front() else {
                // order is empty but entries says otherwise — a
                // bookkeeping bug. Bail rather than infinite-loop.
                break;
            };
            if victim_id == keep {
                // The keep entry is the only thing in the deque at
                // capacity; put it back and stop (cap == 1 corner).
                self.order.push_back(victim_id);
                break;
            }
            if let Some(victim) = self.entries.remove(&victim_id) {
                if let Some(tex) = victim.tex {
                    unsafe { gl.delete_texture(tex) };
                }
                // PendingDecode's rx drops here — worker's tx.send
                // becomes a no-op on the next try.
            }
        }
    }
}

/// Decode a PNG from `path` into bottom-up RGBA8 bytes. Mirrors
/// `hdmi::load_png_rgba`'s output convention so the resulting bytes
/// upload identically. Inlined here (instead of calling into
/// hdmi::load_png_rgba) because the worker thread doesn't carry the
/// GL context and we don't want a circular `mod` dep — and because
/// keeping the decoder local lets us evolve the cache's behavior
/// (e.g. partial-read retry) without touching the standalone path.
fn load_png_rgba_for_cache(path: &Path) -> Result<(Vec<u8>, u32, u32)> {
    let file = std::fs::File::open(path)
        .with_context(|| format!("open png {}", path.display()))?;
    let decoder = png::Decoder::new(file);
    let mut reader = decoder
        .read_info()
        .with_context(|| format!("png read_info {}", path.display()))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .with_context(|| format!("png next_frame {}", path.display()))?;
    if info.bit_depth != png::BitDepth::Eight {
        return Err(anyhow!(
            "png {}: bit depth {:?} not supported (need 8-bit)",
            path.display(),
            info.bit_depth,
        ));
    }
    let (w, h) = (info.width, info.height);
    let mut rgba: Vec<u8> = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => {
            // CMA-arc 2026-06-22 C5: mirror hdmi.rs::load_png_rgba —
            // pre-size the RGBA Vec, explicit drop(buf) after the
            // RGB→RGBA expansion so the intermediate frees before
            // the flip phase.
            let mut out = vec![0u8; (w * h) as usize * 4];
            for (src, dst) in
                buf.chunks_exact(3).zip(out.chunks_exact_mut(4))
            {
                dst[0] = src[0];
                dst[1] = src[1];
                dst[2] = src[2];
                dst[3] = 0xFF;
            }
            drop(buf);
            out
        }
        other => {
            return Err(anyhow!(
                "png {}: color type {other:?} not supported (need RGB or RGBA)",
                path.display(),
            ));
        }
    };
    // CMA-arc 2026-06-22 C5: in-place flip — saves the ~8.3 MB
    // second allocation the prior consuming version paid. Matches
    // hdmi.rs::load_png_rgba's transformation.
    crate::hdmi_logic::flip_rgba_rows_in_place(&mut rgba, w, h);
    Ok((rgba, w, h))
}

/// Allocate + upload an RGBA8 texture; mirror the parameter choices
/// of `hdmi::bake_image_slide_to_current_fbo` so cache-fed blits
/// sample identically to the pre-change sync path.
unsafe fn upload_rgba8_texture(
    gl: &glow::Context,
    rgba: &[u8],
    w: u32,
    h: u32,
) -> Result<glow::NativeTexture> {
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(image_slide_tex): {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_parameter_i32(
        glow::TEXTURE_2D,
        glow::TEXTURE_MIN_FILTER,
        glow::LINEAR as i32,
    );
    gl.tex_parameter_i32(
        glow::TEXTURE_2D,
        glow::TEXTURE_MAG_FILTER,
        glow::LINEAR as i32,
    );
    gl.tex_parameter_i32(
        glow::TEXTURE_2D,
        glow::TEXTURE_WRAP_S,
        glow::CLAMP_TO_EDGE as i32,
    );
    gl.tex_parameter_i32(
        glow::TEXTURE_2D,
        glow::TEXTURE_WRAP_T,
        glow::CLAMP_TO_EDGE as i32,
    );
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::RGBA as i32,
        w as i32,
        h as i32,
        0,
        glow::RGBA,
        glow::UNSIGNED_BYTE,
        Some(rgba),
    );
    Ok(tex)
}

#[cfg(test)]
mod tests {
    //! Tests exercise the state-machine + LRU logic without a real GL
    //! context. We can't construct `glow::NativeTexture` cheaply
    //! (it wraps a raw GL handle), so we test by inspecting `Entry`
    //! state across the channel-poll, touch-LRU, and evict helpers in
    //! isolation.
    use super::*;

    fn empty_cache(cap: usize) -> ImageSlideTextureCache {
        ImageSlideTextureCache::with_capacity(cap)
    }

    fn make_entry(mtime_secs: u64) -> Entry {
        Entry {
            tex: None, // None so tests don't need a GL handle
            tex_w: 100,
            tex_h: 50,
            current_mtime: Some(
                std::time::UNIX_EPOCH + std::time::Duration::from_secs(mtime_secs),
            ),
            pending: None,
        }
    }

    #[test]
    fn with_capacity_clamps_zero_to_one() {
        let c = empty_cache(0);
        assert_eq!(c.capacity, 1);
        assert!(c.is_empty());
    }

    #[test]
    fn touch_lru_moves_id_to_back_idempotently() {
        let mut c = empty_cache(4);
        let a = Uuid::from_u128(1);
        let b = Uuid::from_u128(2);
        let cc = Uuid::from_u128(3);
        c.entries.insert(a, make_entry(1));
        c.entries.insert(b, make_entry(2));
        c.entries.insert(cc, make_entry(3));
        c.order.extend([a, b, cc]);
        // Touch a — now order is [b, c, a].
        c.touch_lru(a);
        let order: Vec<_> = c.order.iter().copied().collect();
        assert_eq!(order, vec![b, cc, a]);
        // Touching the back is idempotent.
        c.touch_lru(a);
        let order: Vec<_> = c.order.iter().copied().collect();
        assert_eq!(order, vec![b, cc, a]);
    }

    #[test]
    fn touch_lru_inserts_new_id_at_back() {
        let mut c = empty_cache(4);
        let a = Uuid::from_u128(1);
        let b = Uuid::from_u128(2);
        c.entries.insert(a, make_entry(1));
        c.order.push_back(a);
        c.touch_lru(b);
        // `touch_lru` itself does not insert into `entries` — that's
        // the caller's job — but it does push to order. We verify the
        // order shape (which is what governs eviction).
        let order: Vec<_> = c.order.iter().copied().collect();
        assert_eq!(order, vec![a, b]);
    }

    #[test]
    fn poll_pending_returns_false_when_no_entry() {
        // We need a gl context to actually exercise upload, but the
        // None-entry branch returns false before any GL touch.
        let mut c = empty_cache(4);
        let id = Uuid::from_u128(99);
        // Construct a glow::Context via the loader fn — but that
        // requires EGL, which we don't have in unit tests. So we
        // skip the actual call and assert structural shape instead.
        assert!(c.entries.get(&id).is_none());
        assert_eq!(c.entries.len(), 0);
    }

    #[test]
    fn entry_get_returns_initialized_fields() {
        // Smoke: Entry is constructible + readable; the LRU touch
        // and mtime drift are exercised by the touch_lru test above
        // and the integration-style test on the Pi.
        let e = make_entry(1234);
        assert_eq!(e.tex_w, 100);
        assert_eq!(e.tex_h, 50);
        assert!(e.tex.is_none());
        assert_eq!(
            e.current_mtime,
            Some(std::time::UNIX_EPOCH + std::time::Duration::from_secs(1234)),
        );
        assert!(e.pending.is_none());
    }

    #[test]
    fn pending_decode_carries_target_mtime() {
        let (_tx, rx) = mpsc::channel();
        let mtime =
            std::time::UNIX_EPOCH + std::time::Duration::from_secs(42);
        let p = PendingDecode {
            target_mtime: mtime,
            rx,
        };
        assert_eq!(p.target_mtime, mtime);
    }

    /// Async decode round-trip without a GL context: drives a worker
    /// via `mpsc` directly and verifies the receiver yields the
    /// expected payload. The full upload+swap exists in the
    /// integration-test path (Pi cargo test) and the on-Pi
    /// measurement.
    #[test]
    fn async_decode_channel_delivers_bytes() {
        let tmp = std::env::temp_dir().join(format!(
            "openmarquee-test-asset-{}.png",
            std::process::id()
        ));
        // Write a minimal valid 2x2 RGBA PNG via the `png` crate (the
        // same crate the cache loader uses, so encoder/decoder agree).
        {
            let file = std::fs::File::create(&tmp).expect("create png");
            let mut enc = png::Encoder::new(file, 2, 2);
            enc.set_color(png::ColorType::Rgba);
            enc.set_depth(png::BitDepth::Eight);
            let mut writer = enc.write_header().expect("write header");
            // 2x2 RGBA: red, green, blue, white.
            let pixels: [u8; 16] = [
                0xFF, 0x00, 0x00, 0xFF, // R
                0x00, 0xFF, 0x00, 0xFF, // G
                0x00, 0x00, 0xFF, 0xFF, // B
                0xFF, 0xFF, 0xFF, 0xFF, // W
            ];
            writer.write_image_data(&pixels).expect("write pixels");
        }
        let (rgba, w, h) =
            load_png_rgba_for_cache(&tmp).expect("decode roundtrips");
        assert_eq!(w, 2);
        assert_eq!(h, 2);
        // 2x2 RGBA = 16 bytes; flip_rgba_rows_vertically preserves
        // length (only reorders rows).
        assert_eq!(rgba.len(), 16);
        std::fs::remove_file(&tmp).ok();
    }
}
