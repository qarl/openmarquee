//! judder-instrument v3 (2026-06-22): PRESENT-side pixel + log
//! probe for the post-crossfade backward-jump (intermittent).
//!
//! qarl: snap-back is INTERMITTENT and on the INCOMING video POST-
//! crossfade (after fade fully completes, during steady playback).
//! Decode-side probes (live_frame_fp) show monotonic forward
//! decode/dequeue. The disagreement means the snap-back is in the
//! PRESENTATION path, not the decode path: either an out-of-order
//! present, a poster/held frame re-shown amid live playback, or a
//! scanout race.
//!
//! Three prior fix rounds (cfe6234 / cause-1 v2 / cause-1 v3) and
//! two hypotheses (decoder-reuse cache, outgoing-poster-preempts-
//! snapshot) were wrong. QA's mandate now: instrument + pixel-
//! capture every present, auto-detect a backward, write PNGs around
//! the anomaly. No more inferring from position counters; SEE the
//! defect.
//!
//! Env surface (OFF by default → zero cost in production):
//!   OPENMARQUEE_PRESENT_DUMP=1        (required to enable)
//!   OPENMARQUEE_PRESENT_DUMP_DIR      (default /tmp)
//!   OPENMARQUEE_PRESENT_DUMP_MAX      (default 5; stop after N anomalies)
//!
//! Design:
//!   - Per present (= every paint_and_present_* call's BEFORE-
//!     eglSwapBuffers point): log `[perf] present_emitted idx=N
//!     slide_id=X source=poster|live|transition_blend|text|image|
//!     external frame_pos=Z slide_play_id=W max_seen=M`.
//!   - Per-slide-play tracking: `max_frame_pos_seen`. Reset on
//!     slide_id change. `slide_play_id` is a monotonic per-process
//!     counter incremented on each slide_id transition (a single
//!     slide looping appears as ONE play_id; subsequent slide cycles
//!     get new ids).
//!   - Backward detection: current frame_pos < max_seen for this
//!     play instance OR (source=poster after a source=live with
//!     frame_pos > 0). Emit `[perf] PRESENT_BACKWARD ...`.
//!   - Ring buffer of last 4 presents (downsampled-region pixel
//!     captures). On backward, write the ring (= the 4 presents
//!     BEFORE the anomaly + the anomaly itself) to `/tmp/backjump-
//!     NNN-bM.png`, then arm a counter to capture the next 4
//!     presents to `/tmp/backjump-NNN-aM.png`. NNN = anomaly index;
//!     bM = before index 0..3; aM = after index 0..3.
//!   - Cost containment: glReadPixels a 480x270 center region (not
//!     full-res 1920x1080). Transfer ~520KB per present, ~5-10ms on
//!     Pi Zero 2W vs the full-res ~50-100ms that would halve frame
//!     rate. Visible coverage: center quarter of a 1920x1080 panel;
//!     adequate for visual confirmation of a backward jump for most
//!     content.

use std::env;
use std::path::PathBuf;

#[cfg(target_os = "linux")]
use anyhow::{anyhow, Context, Result};
#[cfg(target_os = "linux")]
use glow::HasContext;

const ENV_DUMP: &str = "OPENMARQUEE_PRESENT_DUMP";
const ENV_DIR: &str = "OPENMARQUEE_PRESENT_DUMP_DIR";
const ENV_MAX: &str = "OPENMARQUEE_PRESENT_DUMP_MAX";

const DEFAULT_MAX_ANOMALIES: usize = 5;
const DEFAULT_DIR: &str = "/tmp";

/// Pixel region read per present. 480x270 from the center of the
/// panel. 16:9, aspect-matched. Tunable if a future panel needs
/// different dims (probably should env-gate; for now the constants
/// are hardcoded to keep the cost predictable).
const SAMPLE_W: u32 = 480;
const SAMPLE_H: u32 = 270;
const SAMPLE_BYTES: usize = (SAMPLE_W as usize) * (SAMPLE_H as usize) * 4;

/// Ring buffer depth (= how many BEFORE-anomaly frames we capture).
/// 4 is a balance: enough context to see the "before" state, small
/// enough to keep memory at ~2 MB (4 × 520 KB).
const RING_DEPTH: usize = 4;

/// How many AFTER-anomaly frames to dump. Matches RING_DEPTH so each
/// anomaly produces 8 PNGs total (4 before + 4 after).
const AFTER_FRAMES: u8 = 4;

/// Stable source-path tags. Strings (not enum) so callers can pass
/// a literal at the call site without an import. Documented set:
///   "poster"            — use_poster_b / use_poster_a_now path
///   "snapshot"          — use_still_a_now path (outgoing snapshot)
///   "live"              — live V4L2 decode (steady video or A live tick)
///   "transition_blend"  — paint_and_present_one_transition_frame
///                         (blend of A + B; ambiguous frame_pos —
///                         caller passes B's pos as the canonical
///                         since qarl's bug is on the incoming)
///   "text"              — paint_and_present_one_frame_for_slide
///   "image"             — paint_and_present_one_image_slide_frame
///   "external"          — paint_and_present_external_*
pub type Source = &'static str;

/// One ring-buffer entry.
struct RingEntry {
    idx: u64,
    slide_play_id: u64,
    slide_id: uuid::Uuid,
    source: Source,
    frame_pos: u64,
    pixels: Vec<u8>,
}

pub struct JudderPresentDumpState {
    enabled: bool,
    dir: PathBuf,
    max_anomalies: usize,
    /// Global monotonic present index (across all slides).
    present_idx: u64,
    /// Per-slide-play tracking. Resets on slide_id change.
    current_slide_id: Option<uuid::Uuid>,
    slide_play_id: u64,
    /// Max frame_pos observed for the CURRENT play instance.
    /// Zero-initialized at slide change.
    max_frame_pos_this_play: u64,
    /// Did we observe at least one source=live present this play?
    /// Used to detect "poster after live" anomaly.
    saw_live_this_play: bool,
    /// Ring buffer of recent presents (downsampled pixels).
    ring: std::collections::VecDeque<RingEntry>,
    /// Cumulative anomalies detected this process. Capped at
    /// max_anomalies; further backward presents are logged but
    /// pixels not written (avoid disk fill).
    anomalies_detected: usize,
    /// "Capture the next N presents to /tmp/backjump-NNN-aM.png"
    /// counter; decremented on each capture, with NNN/M baked in.
    pending_after: u8,
    /// NNN of the most recent anomaly (used in pending-after PNG
    /// filenames).
    last_anomaly_idx: usize,
    /// AFTER-counter within the pending-after window (0..AFTER_FRAMES).
    after_subidx: u8,
    /// Reusable readback buffer (SAMPLE_BYTES). Allocated lazily.
    readback_buf: Vec<u8>,
}

impl Default for JudderPresentDumpState {
    fn default() -> Self {
        Self {
            enabled: false,
            dir: PathBuf::from(DEFAULT_DIR),
            max_anomalies: DEFAULT_MAX_ANOMALIES,
            present_idx: 0,
            current_slide_id: None,
            slide_play_id: 0,
            max_frame_pos_this_play: 0,
            saw_live_this_play: false,
            ring: std::collections::VecDeque::with_capacity(RING_DEPTH),
            anomalies_detected: 0,
            pending_after: 0,
            last_anomaly_idx: 0,
            after_subidx: 0,
            readback_buf: Vec::new(),
        }
    }
}

impl JudderPresentDumpState {
    /// Read env at session bring-up. Empty/unset OPENMARQUEE_PRESENT
    /// _DUMP → OFF, near-zero-cost no-op record_present.
    pub fn init_from_env() -> Self {
        let dump_enabled = match env::var(ENV_DUMP) {
            Ok(v) => v == "1" || v.eq_ignore_ascii_case("true"),
            Err(_) => false,
        };
        if !dump_enabled {
            return Self::default();
        }
        let dir = env::var(ENV_DIR).ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(DEFAULT_DIR));
        let max_anomalies = env::var(ENV_MAX).ok()
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(DEFAULT_MAX_ANOMALIES);
        eprintln!(
            "judder_present_dump: ENABLED dir={} max_anomalies={} sample={}x{} ring={}",
            dir.display(), max_anomalies, SAMPLE_W, SAMPLE_H, RING_DEPTH,
        );
        Self {
            enabled: true,
            dir,
            max_anomalies,
            ring: std::collections::VecDeque::with_capacity(RING_DEPTH),
            ..Self::default()
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.enabled
    }

    /// Per-present hook. MUST be called AFTER paint+present pass and
    /// BEFORE eglSwapBuffers (so the just-composited frame is still
    /// in FBO 0). Errors are swallowed + logged (an instrument bug
    /// must never take down live rendering).
    ///
    /// `slide_id` = the slide_id whose play instance this present
    /// belongs to. For steady paints, the current slide. For
    /// transitions, the TO slide (incoming = where qarl's bug
    /// manifests; using the FROM would make the per-play tracking
    /// span two slides). For text/image with no decoder, `frame_pos=0`.
    ///
    /// `source` is one of the `Source` constants documented above.
    ///
    /// `src_w` / `src_h` = the EGL window surface dims (used to
    /// center the SAMPLE_W × SAMPLE_H readback region).
    #[cfg(target_os = "linux")]
    pub fn record_present(
        &mut self,
        gl: &glow::Context,
        src_w: u32,
        src_h: u32,
        slide_id: uuid::Uuid,
        source: Source,
        frame_pos: u64,
    ) {
        if !self.enabled {
            return;
        }
        // Per-slide-play state transition.
        let new_play = match self.current_slide_id {
            Some(prev) if prev == slide_id => false,
            _ => true,
        };
        if new_play {
            self.current_slide_id = Some(slide_id);
            self.slide_play_id = self.slide_play_id.wrapping_add(1);
            self.max_frame_pos_this_play = 0;
            self.saw_live_this_play = false;
            // Drop the ring on slide change — the prior slide's
            // captures aren't useful context for the new slide's
            // anomaly.
            self.ring.clear();
            // Reset any pending-after window: an anomaly on slide N
            // doesn't extend its capture window into slide N+1.
            self.pending_after = 0;
            self.after_subidx = 0;
        }

        self.present_idx = self.present_idx.wrapping_add(1);

        // Backward-detection #1: frame_pos < max_seen this play.
        // Backward-detection #2: poster after live (frame_pos>0
        // observed) this play. The latter catches the "old/poster
        // frame re-presented amid forward live sequence" case
        // QA called out.
        let backward_by_frame_pos =
            frame_pos < self.max_frame_pos_this_play && frame_pos > 0;
        let backward_by_poster_after_live =
            self.saw_live_this_play && source == "poster";
        let is_backward = backward_by_frame_pos || backward_by_poster_after_live;

        // Update tracking BEFORE the dump logic (so the post-anomaly
        // captures reflect the latest max).
        if frame_pos > self.max_frame_pos_this_play {
            self.max_frame_pos_this_play = frame_pos;
        }
        if source == "live" && frame_pos > 0 {
            self.saw_live_this_play = true;
        }

        // Always log the present (cheap line, greppable).
        eprintln!(
            "[perf] present_emitted idx={} slide_play_id={} slide_id={} \
             source={} frame_pos={} max_seen={} backward={}",
            self.present_idx, self.slide_play_id, slide_id,
            source, frame_pos, self.max_frame_pos_this_play, is_backward,
        );

        // Capture the readback unconditionally (the ring + pending-
        // after window both need it). Failure is non-fatal.
        let captured_pixels = match self.read_center_region(gl, src_w, src_h) {
            Ok(()) => Some(self.readback_buf.clone()),
            Err(e) => {
                eprintln!(
                    "[perf] present_dump_readback_err idx={} err={:#}",
                    self.present_idx, e,
                );
                None
            }
        };

        // Backward → dump ring + arm AFTER window.
        if is_backward && self.anomalies_detected < self.max_anomalies {
            self.anomalies_detected += 1;
            self.last_anomaly_idx = self.anomalies_detected;
            self.after_subidx = 0;
            self.pending_after = AFTER_FRAMES;
            eprintln!(
                "[perf] PRESENT_BACKWARD anomaly_idx={} present_idx={} \
                 slide_play_id={} slide_id={} source={} frame_pos={} \
                 max_seen={} by_frame_pos={} by_poster_after_live={} \
                 ring_depth={}",
                self.last_anomaly_idx, self.present_idx, self.slide_play_id,
                slide_id, source, frame_pos, self.max_frame_pos_this_play,
                backward_by_frame_pos, backward_by_poster_after_live,
                self.ring.len(),
            );
            // Dump ring (the BEFORE-anomaly presents).
            for (i, entry) in self.ring.iter().enumerate() {
                let path = self.dir.join(format!(
                    "backjump-{:03}-b{}.png", self.last_anomaly_idx, i,
                ));
                if let Err(e) = write_rgba_png(&path, &entry.pixels, SAMPLE_W, SAMPLE_H) {
                    eprintln!(
                        "[perf] present_dump_write_err path={} err={:#}",
                        path.display(), e,
                    );
                } else {
                    eprintln!(
                        "[perf] backjump_dumped slot=b{} path={} present_idx={} \
                         source={} frame_pos={}",
                        i, path.display(), entry.idx, entry.source, entry.frame_pos,
                    );
                }
            }
            // Dump the anomaly frame itself (= b{ring_depth}).
            if let Some(ref px) = captured_pixels {
                let path = self.dir.join(format!(
                    "backjump-{:03}-b{}.png",
                    self.last_anomaly_idx, self.ring.len(),
                ));
                if let Err(e) = write_rgba_png(&path, px, SAMPLE_W, SAMPLE_H) {
                    eprintln!(
                        "[perf] present_dump_write_err path={} err={:#}",
                        path.display(), e,
                    );
                } else {
                    eprintln!(
                        "[perf] backjump_dumped slot=b{}(anomaly) path={} \
                         present_idx={} source={} frame_pos={}",
                        self.ring.len(), path.display(), self.present_idx,
                        source, frame_pos,
                    );
                }
            }
        } else if self.pending_after > 0 {
            // Inside the AFTER window of a previous anomaly: write
            // this frame as backjump-NNN-aM.png.
            if let Some(ref px) = captured_pixels {
                let path = self.dir.join(format!(
                    "backjump-{:03}-a{}.png",
                    self.last_anomaly_idx, self.after_subidx,
                ));
                if let Err(e) = write_rgba_png(&path, px, SAMPLE_W, SAMPLE_H) {
                    eprintln!(
                        "[perf] present_dump_write_err path={} err={:#}",
                        path.display(), e,
                    );
                } else {
                    eprintln!(
                        "[perf] backjump_dumped slot=a{} path={} present_idx={} \
                         source={} frame_pos={}",
                        self.after_subidx, path.display(), self.present_idx,
                        source, frame_pos,
                    );
                }
            }
            self.after_subidx = self.after_subidx.saturating_add(1);
            self.pending_after -= 1;
        }

        // Push to ring (the new entry becomes the most-recent
        // "BEFORE" for any future anomaly).
        if let Some(px) = captured_pixels {
            if self.ring.len() == RING_DEPTH {
                self.ring.pop_front();
            }
            self.ring.push_back(RingEntry {
                idx: self.present_idx,
                slide_play_id: self.slide_play_id,
                slide_id,
                source,
                frame_pos,
                pixels: px,
            });
        }
    }

    /// No-op stub for non-Linux builds.
    #[cfg(not(target_os = "linux"))]
    pub fn record_present(
        &mut self,
        _src_w: u32,
        _src_h: u32,
        _slide_id: uuid::Uuid,
        _source: Source,
        _frame_pos: u64,
    ) {
    }

    /// glReadPixels a centered SAMPLE_W × SAMPLE_H region from FBO 0
    /// into self.readback_buf. Drains any pre-existing GL error first
    /// so the post-read get_error attribution is clean (same pattern
    /// as live_preview::do_capture).
    #[cfg(target_os = "linux")]
    fn read_center_region(
        &mut self,
        gl: &glow::Context,
        src_w: u32,
        src_h: u32,
    ) -> Result<()> {
        if src_w < SAMPLE_W || src_h < SAMPLE_H {
            return Err(anyhow!(
                "scanout {}x{} smaller than sample {}x{}",
                src_w, src_h, SAMPLE_W, SAMPLE_H,
            ));
        }
        if self.readback_buf.len() != SAMPLE_BYTES {
            self.readback_buf.resize(SAMPLE_BYTES, 0);
        }
        let x = (src_w - SAMPLE_W) / 2;
        let y = (src_h - SAMPLE_H) / 2;
        unsafe {
            loop {
                let stale = gl.get_error();
                if stale == glow::NO_ERROR {
                    break;
                }
                eprintln!(
                    "judder_present_dump: drained stale GL error 0x{stale:x} \
                     before readback (paint-pass-origin, NOT a probe bug)",
                );
            }
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.read_pixels(
                x as i32,
                y as i32,
                SAMPLE_W as i32,
                SAMPLE_H as i32,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                glow::PixelPackData::Slice(&mut self.readback_buf[..SAMPLE_BYTES]),
            );
            let err = gl.get_error();
            if err != glow::NO_ERROR {
                return Err(anyhow!("glReadPixels: GL error 0x{err:x}"));
            }
        }
        Ok(())
    }
}

/// Write `pixels` (RGBA8, row 0 at BOTTOM per glReadPixels
/// convention) to `path` as a PNG (row 0 at TOP per PNG
/// convention). Single-pass Y-flip during PNG row emission via the
/// helper in hdmi_logic; here we just hand off the buffer.
#[cfg(target_os = "linux")]
fn write_rgba_png(path: &std::path::Path, pixels: &[u8], w: u32, h: u32) -> Result<()> {
    // Y-flip in-place buffer (PNG expects row 0 at TOP, glReadPixels
    // returns row 0 at BOTTOM). Allocate a flipped copy + encode.
    let stride = (w as usize) * 4;
    let mut flipped = vec![0u8; pixels.len()];
    for src_y in 0..(h as usize) {
        let dst_y = (h as usize) - 1 - src_y;
        let src_base = src_y * stride;
        let dst_base = dst_y * stride;
        flipped[dst_base..dst_base + stride]
            .copy_from_slice(&pixels[src_base..src_base + stride]);
    }
    let png = crate::hdmi_logic::rgba_to_png_bytes(&flipped, w, h)
        .context("rgba_to_png_bytes (present_dump)")?;
    // Atomic write: tmp + rename, same pattern as live_preview.
    let tmp = path.with_extension("png.tmp");
    std::fs::write(&tmp, &png)
        .with_context(|| format!("write {}", tmp.display()))?;
    std::fs::rename(&tmp, path)
        .with_context(|| format!("rename {} -> {}", tmp.display(), path.display()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_state_is_disabled() {
        let s = JudderPresentDumpState::default();
        assert!(!s.is_enabled());
    }

    #[test]
    fn init_disabled_when_env_unset() {
        // Unset the env var to ensure deterministic test (other tests
        // in this binary may set it). SAFETY: cargo test serializes
        // within a process; the env mutation here is scoped to this
        // test.
        unsafe { std::env::remove_var(ENV_DUMP); }
        let s = JudderPresentDumpState::init_from_env();
        assert!(!s.is_enabled());
    }

    #[test]
    fn init_enabled_when_env_set() {
        unsafe { std::env::set_var(ENV_DUMP, "1"); }
        let s = JudderPresentDumpState::init_from_env();
        assert!(s.is_enabled());
        unsafe { std::env::remove_var(ENV_DUMP); }
    }

    #[test]
    fn ring_depth_constant() {
        // Pin the documented ring depth. If this changes, update
        // QA's filename interpretation (backjump-NNN-bM.png M range)
        // and the after-frames PNG count.
        assert_eq!(RING_DEPTH, 4);
        assert_eq!(AFTER_FRAMES, 4);
    }
}
