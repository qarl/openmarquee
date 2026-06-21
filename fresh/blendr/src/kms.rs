//! KMS state save/restore + page-flip run loop.
//!
//! Owns the buffer-object/framebuffer state machine. Discipline
//! lifted from the OLD renderer's `commit_fb` drain pattern but
//! pared down to Phase 0 minimum.

use anyhow::Result;

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(not(target_os = "linux"))]
pub use stub::*;

/// Phase 1 / Phase 2 / Phase 3 stream dispatch. Owned by
/// main; passed as &mut into run_loop.
pub enum Streams {
    /// Step::Solid / Step::Checker: no gst decoders.
    None,
    /// Step::Video: one decoder (one clip; concat-loops
    /// internally).
    Single(crate::gst_decode::GstDecoder),
    /// Step::Blend (Phase 2 static) / Phase 3 slideshow.
    /// Arbitrary-N playlist with 2-slot retire+recreate.
    ///
    /// Invariants:
    /// - At most 2 GstDecoder instances are alive at any
    ///   moment (the 2-decoder hard ceiling on this Pi; 3
    ///   live = black per cutloop history).
    /// - Slot 0 maps to the shader's "A" texture; slot 1 to
    ///   "B". SlideshowState's HoldingA shows slot 0, HoldingB
    ///   shows slot 1, fades blend between them.
    /// - For N >= 3: each HoldingX entry triggers
    ///   recreate_pending = true; the NEXT tick retires the
    ///   non-visible slot (= just-faded-out clip) and creates
    ///   a fresh decoder with playlist[next_idx]; next_idx
    ///   advances (next_idx + 1) % N. Slot 0 and 1 alternate
    ///   ownership of the "current visible" role naturally
    ///   via the slideshow phase.
    /// - For N == 2: no retire+recreate (would just churn
    ///   between the same two clips). Both decoders alive
    ///   forever; matches Phase 3 v1 behavior.
    /// - For N == 1: Streams::Single is used instead (no
    ///   slideshow).
    Cycle {
        playlist: Vec<std::path::PathBuf>,
        slots: [Option<crate::gst_decode::GstDecoder>; 2],
        /// Index into playlist of the NEXT clip to load when
        /// retire+recreate fires.
        next_idx: usize,
        /// Set on entering a Holding phase (N>=3 only).
        /// Drained on the next tick by retire+recreate of the
        /// non-visible slot.
        recreate_pending: bool,
        slideshow: SlideshowState,
        /// Measurement infra: stamp at the start of each
        /// retire (Drop call) inside tick(). Read by
        /// outlier_context for [frame-long] "near_retire" flag.
        last_retire_at: Option<std::time::Instant>,
        /// Measurement infra: stamp at the start of each
        /// create (GstDecoder::new call) inside tick().
        last_create_at: Option<std::time::Instant>,
    },
}

/// Outlier context returned from Streams::outlier_context.
/// Used by FrameStats to format the [frame-long] log line
/// with phase / cycle / cycle-event flags.
pub struct OutlierCtx {
    pub phase_str: String,
    pub cycle: u64,
    pub ms_since_cycle_event: Option<u128>,
    pub flags: String,
}

impl Streams {
    /// Snapshot context for an outlier-frame log line.
    /// Called only when frame_ms > threshold (rare); the
    /// Mutex locks + format allocations are out of the
    /// per-frame hot path.
    pub fn outlier_context(&self) -> OutlierCtx {
        match self {
            Streams::Cycle { slots, slideshow, last_retire_at, last_create_at, .. } => {
                let now = std::time::Instant::now();
                let ms_retire = last_retire_at.map(|t| (now - t).as_millis());
                let ms_create = last_create_at.map(|t| (now - t).as_millis());
                let ms_concat: Option<u128> = slots
                    .iter()
                    .filter_map(|s| s.as_ref())
                    .filter_map(|d| d.ms_since_last_concat_add())
                    .min();
                let mut flags: Vec<&'static str> = Vec::new();
                if ms_retire.map_or(false, |m| m < 500) { flags.push("near_retire"); }
                if ms_create.map_or(false, |m| m < 500) { flags.push("near_create"); }
                if ms_concat.map_or(false, |m| m < 500) { flags.push("near_concat_add"); }
                if flags.is_empty() { flags.push("none"); }
                let ms_since_cycle = [ms_retire, ms_create, ms_concat]
                    .iter()
                    .filter_map(|x| *x)
                    .min();
                OutlierCtx {
                    phase_str: format!("{:?}", slideshow.phase()),
                    cycle: slideshow.cycle_count(),
                    ms_since_cycle_event: ms_since_cycle,
                    flags: flags.join("|"),
                }
            }
            Streams::Single(d) => OutlierCtx {
                phase_str: "single".to_string(),
                cycle: 0,
                ms_since_cycle_event: d.ms_since_last_concat_add(),
                flags: if d
                    .ms_since_last_concat_add()
                    .map_or(false, |m| m < 500)
                {
                    "near_concat_add".to_string()
                } else {
                    "none".to_string()
                },
            },
            Streams::None => OutlierCtx {
                phase_str: "none".to_string(),
                cycle: 0,
                ms_since_cycle_event: None,
                flags: "none".to_string(),
            },
        }
    }
}

impl Streams {
    /// Per-iteration driver. Updates slideshow + processes any
    /// pending retire+recreate. Called from kms::run_loop
    /// BEFORE pulling textures so the slot pointers passed to
    /// the presenter are always valid.
    ///
    /// MUST be called on the present thread (where blendr's
    /// EGL is current). Mid-run GstDecoder::new is a synchronous
    /// 5s-blocking call (first-sample wait); the present
    /// thread stalls during retire+recreate, which is bounded
    /// by hold_sec - tick_overhead. Defer to a worker thread
    /// only if soak shows the stall exceeds the hold.
    pub fn tick(
        &mut self,
        #[allow(unused_variables)] egl: &crate::egl_gbm::Egl,
    ) -> Result<()> {
        if let Streams::Cycle {
            playlist,
            slots: _,
            next_idx,
            recreate_pending,
            slideshow,
            last_retire_at: _,
            last_create_at: _,
        } = self
        {
            // Update alpha + check for phase change.
            let (_alpha, phase_changed) = slideshow.update();
            let n = playlist.len();

            // On phase-change INTO a Holding phase, mark for
            // retire+recreate next tick. N=2 short-circuits
            // (both clips stay loaded forever, like Phase 3 v1).
            if phase_changed && n >= 3 {
                match slideshow.phase() {
                    BlendPhase::HoldingA | BlendPhase::HoldingB => {
                        *recreate_pending = true;
                    }
                    _ => {}
                }
            }

            // Drain recreate_pending (single retire+recreate
            // per phase, regardless of how many ticks elapse
            // before we get here).
            if *recreate_pending {
                *recreate_pending = false;
                // visible_slot derives from phase:
                //   HoldingA / FadingAtoB -> slot 0 visible
                //   HoldingB / FadingBtoA -> slot 1 visible
                let visible_slot = match slideshow.phase() {
                    BlendPhase::HoldingA | BlendPhase::FadingAtoB => 0,
                    BlendPhase::HoldingB | BlendPhase::FadingBtoA => 1,
                };
                let retire_slot = 1 - visible_slot;
                let clip_to_load = playlist[*next_idx].clone();
                let clip_name = clip_to_load
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or("?")
                    .to_string();

                // --- Step 1: log + read CMA pre-retire ----
                let cma_pre = read_cma_free_kb();
                log::info!(
                    "[cycle] retire slot={retire_slot} \
                     (CmaFree={cma_pre:?}kB) -- starting"
                );
                let t_retire = std::time::Instant::now();
                // Measurement infra: stamp last_retire_at for
                // [frame-long] near_retire flag.
                let last_retire_at_slot = match self {
                    Streams::Cycle { last_retire_at, .. } => last_retire_at,
                    _ => unreachable!("outer match guarantees Cycle"),
                };
                *last_retire_at_slot = Some(t_retire);
                // The local destructuring above re-borrowed
                // self; from this point on use the existing
                // outer destructured bindings (playlist, slots,
                // ...). Re-shadow them by re-entering the match.
                let (playlist, slots, next_idx, recreate_pending, slideshow, last_create_at) =
                    if let Streams::Cycle { playlist, slots, next_idx, recreate_pending, slideshow, last_create_at, .. } = self {
                        (playlist, slots, next_idx, recreate_pending, slideshow, last_create_at)
                    } else { unreachable!() };
                let _ = (playlist, recreate_pending);  // silence unused
                let visible_slot = match slideshow.phase() {
                    BlendPhase::HoldingA | BlendPhase::FadingAtoB => 0,
                    BlendPhase::HoldingB | BlendPhase::FadingBtoA => 1,
                };
                let retire_slot = 1 - visible_slot;

                // --- Step 2: retire (Drop the old decoder).
                //     BUG 3 sequence runs inside GstDecoder::Drop;
                //     state(5s)-wait IS the V4L2 STREAMOFF barrier
                //     that prevents MMAL slot leak.
                slots[retire_slot] = None;
                let retire_ms = t_retire.elapsed().as_millis();
                log::info!(
                    "[cycle] retire slot={retire_slot} done elapsed_ms={retire_ms}"
                );

                // --- Step 3: log + create new decoder.
                log::info!(
                    "[cycle] create slot={retire_slot} clip={clip_name} -- starting"
                );
                let t_create = std::time::Instant::now();
                *last_create_at = Some(t_create);
                let new_dec =
                    match crate::gst_decode::GstDecoder::new(egl, &clip_to_load) {
                        Ok(d) => d,
                        Err(e) => {
                            log::error!(
                                "[cycle] create slot={retire_slot} clip={clip_name} \
                                 FAILED: {e:#}"
                            );
                            return Err(e);
                        }
                    };
                let create_ms = t_create.elapsed().as_millis();
                let cma_post = read_cma_free_kb();
                slots[retire_slot] = Some(new_dec);
                log::info!(
                    "[cycle] create slot={retire_slot} done elapsed_ms={create_ms} \
                     CmaFree={cma_post:?}kB"
                );

                // CMA brick-floor guard per project memory
                // (Pi Zero 2 W bricks below ~50MB CmaFree).
                if let Some(kb) = cma_post {
                    if kb < 50_000 {
                        log::error!(
                            "[cycle] CMA LOW WARNING: CmaFree={kb}kB < 50MB \
                             floor; soak may brick"
                        );
                    }
                }

                *next_idx = (*next_idx + 1) % n;
                log::info!(
                    "[cycle] next_idx={} playlist_len={n} visible_slot={visible_slot} \
                     cycle={}",
                    *next_idx,
                    slideshow.cycle_count(),
                );
            }
        }
        Ok(())
    }
}

/// Per-frame timing measurement (QA overnight mandate). Zero
/// per-frame allocation: counters + Vec<f32> push + Instant::
/// now. Rare format/log only on outlier frames (>25ms) and at
/// the ~30s summary boundary + exit.
///
/// Tracks both frame_ms (present-to-present delta) and
/// render_ms (just the GL draw+swap cost; excludes vsync
/// drain). Cumulative samples in cum_frame_ms / cum_render_ms
/// (sorted at exit for cumulative p50/p99/p999). Window
/// stats derived from a slice (last 30s).
pub struct FrameStats {
    cum_frame_ms: Vec<f32>,
    cum_render_ms: Vec<f32>,
    /// Index into cum_frame_ms where the CURRENT summary window
    /// starts. Window samples = &cum_frame_ms[window_start_idx..].
    window_start_idx: usize,
    /// Cumulative count of long frames (frame_ms > LONG_FRAME_MS).
    cum_long_count: u64,
    /// Long frames inside the current window.
    window_long_count: u64,
    /// Wall-clock at the previous frame's end. None on first
    /// frame (no delta to compute).
    last_present_end: Option<std::time::Instant>,
    last_summary_at: std::time::Instant,
    run_start: std::time::Instant,
}

const LONG_FRAME_MS: f32 = 25.0;
const SUMMARY_INTERVAL_S: u64 = 30;

impl FrameStats {
    pub fn new() -> Self {
        // 60fps * 600s = ~36k samples per 10min; ~144KB.
        // 1hr soak ~216k * 4B = ~864KB. Linear scaling; fine.
        let cap = 60 * 60 * 10;
        let now = std::time::Instant::now();
        Self {
            cum_frame_ms: Vec::with_capacity(cap),
            cum_render_ms: Vec::with_capacity(cap),
            window_start_idx: 0,
            cum_long_count: 0,
            window_long_count: 0,
            last_present_end: None,
            last_summary_at: now,
            run_start: now,
        }
    }

    /// Record one frame. Cheap: 2 pushes + 1 compare +
    /// possibly 1 increment. Logs an outlier line if
    /// frame_ms > LONG_FRAME_MS. Maybe emits a 30s summary.
    pub fn record(
        &mut self,
        frame_ms: f32,
        render_ms: f32,
        frame_idx: u64,
        streams: &Streams,
    ) {
        self.cum_frame_ms.push(frame_ms);
        self.cum_render_ms.push(render_ms);
        if frame_ms > LONG_FRAME_MS {
            self.cum_long_count += 1;
            self.window_long_count += 1;
            // Outlier log (rare; formatting cost acceptable).
            let ctx = streams.outlier_context();
            log::info!(
                "[frame-long] frame={frame_idx} delta={frame_ms:.1}ms \
                 render_ms={render_ms:.1} phase={} cycle={} \
                 flags=[{}] ms_since_last_cycle_event={:?}",
                ctx.phase_str,
                ctx.cycle,
                ctx.flags,
                ctx.ms_since_cycle_event,
            );
        }
        if self.last_summary_at.elapsed().as_secs() >= SUMMARY_INTERVAL_S {
            self.emit_window_summary();
            self.last_summary_at = std::time::Instant::now();
            self.window_start_idx = self.cum_frame_ms.len();
            self.window_long_count = 0;
        }
    }

    fn emit_window_summary(&self) {
        let win_frame: &[f32] = &self.cum_frame_ms[self.window_start_idx..];
        let win_render: &[f32] = &self.cum_render_ms[self.window_start_idx..];
        if win_frame.is_empty() {
            return;
        }
        let (mean_f, p50_f, p99_f, p999_f, max_f) = pct_stats(win_frame);
        let (mean_r, _p50_r, _p99_r, _p999_r, max_r) = pct_stats(win_render);
        let win_sec = self.last_summary_at.elapsed().as_secs_f32().max(0.001);
        let long_per_min = (self.window_long_count as f32 / win_sec) * 60.0;
        log::info!(
            "[frame-stats] window={SUMMARY_INTERVAL_S}s frames={} mean={mean_f:.2}ms \
             p50={p50_f:.2} p99={p99_f:.2} p999={p999_f:.2} max={max_f:.2} \
             long(>{LONG_FRAME_MS:.0}ms)={} (K/min={:.1}) \
             render_mean={mean_r:.2}ms render_max={max_r:.2}ms",
            win_frame.len(),
            self.window_long_count,
            long_per_min,
        );
    }

    /// Cumulative summary at exit. Sorts the full Vec once.
    pub fn emit_final_summary(&mut self) {
        if self.cum_frame_ms.is_empty() {
            log::info!("[frame-stats] FINAL: no frames recorded");
            return;
        }
        let (mean_f, p50_f, p99_f, p999_f, max_f) = pct_stats(&self.cum_frame_ms);
        let (mean_r, _p50_r, _p99_r, _p999_r, max_r) = pct_stats(&self.cum_render_ms);
        let run_sec = self.run_start.elapsed().as_secs_f32().max(0.001);
        let long_per_min = (self.cum_long_count as f32 / run_sec) * 60.0;
        log::info!(
            "[frame-stats] FINAL run={run_sec:.1}s frames={} mean={mean_f:.2}ms \
             p50={p50_f:.2} p99={p99_f:.2} p999={p999_f:.2} max={max_f:.2} \
             long(>{LONG_FRAME_MS:.0}ms)={} (K/min={:.1}) \
             render_mean={mean_r:.2}ms render_max={max_r:.2}ms",
            self.cum_frame_ms.len(),
            self.cum_long_count,
            long_per_min,
        );
    }

    /// Stamp a present-end timestamp and return the frame_ms
    /// delta from the previous stamp (None on the first call).
    pub fn stamp_present_end(&mut self, now: std::time::Instant) -> Option<f32> {
        let prev = self.last_present_end.replace(now);
        prev.map(|p| (now - p).as_secs_f32() * 1000.0)
    }
}

/// Compute (mean, p50, p99, p999, max) of a sample slice.
/// Sorts a copy (so the caller's data is untouched).
fn pct_stats(samples: &[f32]) -> (f32, f32, f32, f32, f32) {
    if samples.is_empty() {
        return (0.0, 0.0, 0.0, 0.0, 0.0);
    }
    let mut sorted: Vec<f32> = samples.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = sorted.len();
    let sum: f64 = samples.iter().map(|x| *x as f64).sum();
    let mean = (sum / n as f64) as f32;
    let pick = |q: f32| -> f32 {
        let idx = ((n as f32) * q) as usize;
        sorted[idx.min(n - 1)]
    };
    let p50 = pick(0.50);
    let p99 = pick(0.99);
    let p999 = pick(0.999);
    let max = *sorted.last().unwrap();
    (mean, p50, p99, p999, max)
}

/// Read /proc/meminfo CmaFree value in KB. Returns None on
/// any parse / IO error (logged but non-fatal). Used by
/// retire+recreate to log CMA per cycle so QA can spot
/// drift across long soaks (Pi Zero 2 W bricks below ~50MB
/// CmaFree).
fn read_cma_free_kb() -> Option<u64> {
    let s = std::fs::read_to_string("/proc/meminfo").ok()?;
    for line in s.lines() {
        if let Some(rest) = line.strip_prefix("CmaFree:") {
            // "CmaFree:       65432 kB"
            let trimmed = rest.trim();
            let num = trimmed.split_whitespace().next()?;
            return num.parse::<u64>().ok();
        }
    }
    None
}

/// Phase 3 v1: 2-clip A<->B slideshow state machine running on
/// the present thread. Each tick run_loop calls update() which
/// returns the current alpha + a "phase changed" flag for
/// logging.
///
/// Cycle (with default hold=20s + crossfade=1s):
///   HoldingA (20s, alpha=0.0)
///   FadingAtoB (1s, alpha 0.0->1.0)
///   HoldingB (20s, alpha=1.0)
///   FadingBtoA (1s, alpha 1.0->0.0)
///   repeat
///
/// For test runs --hold-sec 3 + --crossfade-sec 1 = 8s/cycle.
/// QA needs short hold to exercise many crossfades in a 60s
/// run.
///
/// Per-clip looping: each GstDecoder's bus handler issues a
/// seek_simple(FLUSH|KEY_UNIT, 0) on EOS. So a 4.75s clip
/// plays/rewinds/plays continuously regardless of where the
/// slideshow cycle is.
pub struct SlideshowState {
    pub hold_sec: f32,
    pub crossfade_sec: f32,
    phase: BlendPhase,
    phase_started: std::time::Instant,
    cycle_count: u64,
    /// Debug override per QA ask: when set, update() returns
    /// this alpha forever and the phase machine is frozen at
    /// HoldingA (cosmetic; alpha is what's actually read).
    /// Lets QA deterministically capture at a known blend
    /// ratio instead of guessing frame numbers.
    static_alpha: Option<f32>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BlendPhase {
    HoldingA,
    FadingAtoB,
    HoldingB,
    FadingBtoA,
}

impl SlideshowState {
    pub fn new(hold_sec: f32, crossfade_sec: f32) -> Self {
        Self {
            hold_sec,
            crossfade_sec,
            phase: BlendPhase::HoldingA,
            phase_started: std::time::Instant::now(),
            cycle_count: 0,
            static_alpha: None,
        }
    }

    /// Debug override: freeze the slideshow at a fixed alpha.
    /// Phase transitions stop firing; update() always returns
    /// the same alpha + phase_changed=false. Per QA ask for
    /// deterministic mid-fade captures.
    pub fn set_static_alpha(&mut self, alpha: f32) {
        self.static_alpha = Some(alpha.clamp(0.0, 1.0));
    }

    /// Returns (current_alpha, phase_changed_this_tick).
    pub fn update(&mut self) -> (f32, bool) {
        // Static-alpha debug override (QA's --static-alpha
        // flag): freeze at the configured value; no phase
        // transitions.
        if let Some(alpha) = self.static_alpha {
            return (alpha, false);
        }
        let elapsed = self.phase_started.elapsed().as_secs_f32();
        let mut changed = false;
        let alpha = match self.phase {
            BlendPhase::HoldingA => {
                if elapsed >= self.hold_sec {
                    self.phase = BlendPhase::FadingAtoB;
                    self.phase_started = std::time::Instant::now();
                    changed = true;
                }
                0.0
            }
            BlendPhase::FadingAtoB => {
                if elapsed >= self.crossfade_sec {
                    self.phase = BlendPhase::HoldingB;
                    self.phase_started = std::time::Instant::now();
                    changed = true;
                    1.0
                } else {
                    (elapsed / self.crossfade_sec).clamp(0.0, 1.0)
                }
            }
            BlendPhase::HoldingB => {
                if elapsed >= self.hold_sec {
                    self.phase = BlendPhase::FadingBtoA;
                    self.phase_started = std::time::Instant::now();
                    changed = true;
                }
                1.0
            }
            BlendPhase::FadingBtoA => {
                if elapsed >= self.crossfade_sec {
                    self.phase = BlendPhase::HoldingA;
                    self.phase_started = std::time::Instant::now();
                    self.cycle_count += 1;
                    changed = true;
                    0.0
                } else {
                    (1.0 - elapsed / self.crossfade_sec).clamp(0.0, 1.0)
                }
            }
        };
        (alpha, changed)
    }

    pub fn phase(&self) -> BlendPhase {
        self.phase
    }
    pub fn cycle_count(&self) -> u64 {
        self.cycle_count
    }
}

#[cfg(not(target_os = "linux"))]
mod stub {
    use super::*;
    pub struct SavedState;
    pub struct ModePick { pub w: u32, pub h: u32 }
    pub fn pick_connector_mode(_: &crate::drm_probe::Card, _: ()) -> Result<ModePick> {
        anyhow::bail!("KMS stub: Linux only")
    }
    pub fn save_current_state(_: &crate::drm_probe::Card, _: ()) -> Result<SavedState> {
        anyhow::bail!("KMS stub: Linux only")
    }
    pub fn run_loop(
        _: &crate::drm_probe::Card,
        _: &ModePick,
        _: &mut crate::egl_gbm::Gbm,
        _: &mut crate::egl_gbm::Egl,
        _: &mut crate::gles_present::Presenter,
        _: &mut super::Streams,
        _: u64,
        _: Option<&std::path::Path>,
        _: u64,
        _: &std::sync::atomic::AtomicBool,
    ) -> Result<()> {
        anyhow::bail!("KMS stub: Linux only")
    }
    pub fn restore(_: &crate::drm_probe::Card, _: &SavedState) -> Result<()> {
        anyhow::bail!("KMS stub: Linux only")
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::drm_probe::Card;
    use crate::egl_gbm::{Egl, Gbm};
    use crate::gles_present::Presenter;
    use anyhow::{anyhow, bail, Context};
    use drm::buffer::{Buffer as DrmBuffer, DrmFourcc, Handle as DrmHandle};
    use drm::control::{
        connector, crtc, encoder, framebuffer, Device as ControlDevice,
        Event, Mode, PageFlipFlags,
    };
    use drm::Device;
    use gbm::{BufferObject, Format as GbmFormat};
    use std::os::fd::AsFd;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};

    pub struct ModePick {
        pub connector: connector::Handle,
        /// Captured for diagnostics + future phases; not read in
        /// the Phase-0 run loop (the encoder is implicit in the
        /// CRTC we already hold).
        #[allow(dead_code)]
        pub encoder: encoder::Handle,
        pub crtc: crtc::Handle,
        pub mode: Mode,
        pub w: u32,
        pub h: u32,
    }

    pub struct SavedState {
        pub crtc: crtc::Handle,
        pub mode: Option<Mode>,
        pub fb: Option<framebuffer::Handle>,
        pub pos: (u32, u32),
        pub connector: connector::Handle,
    }

    /// Save the CRTC currently driving `connector_id`. Must run
    /// BEFORE we modeset our own FB; restore() at exit replays
    /// these values to hand scanout back to fbcon (so we don't
    /// leave the screen black).
    pub fn save_current_state(
        card: &Card,
        connector_id: connector::Handle,
    ) -> Result<SavedState> {
        let conn = card.get_connector(connector_id, false)
            .context("get_connector(saved)")?;
        let enc_id = conn.current_encoder()
            .ok_or_else(|| anyhow!("connector has no current encoder"))?;
        let enc = card.get_encoder(enc_id)
            .context("get_encoder(saved)")?;
        let crtc_id = enc.crtc()
            .ok_or_else(|| anyhow!("encoder has no current CRTC"))?;
        let crtc_info = card.get_crtc(crtc_id)
            .context("get_crtc(saved)")?;
        log::info!(
            "[kms] saved state: crtc={:?} fb={:?} pos={:?} mode={}",
            crtc_id,
            crtc_info.framebuffer(),
            crtc_info.position(),
            crtc_info.mode().map(|m| m.name().to_string_lossy().to_string())
                .unwrap_or_else(|| "none".into()),
        );
        Ok(SavedState {
            crtc: crtc_id,
            mode: crtc_info.mode(),
            fb: crtc_info.framebuffer(),
            pos: crtc_info.position(),
            connector: connector_id,
        })
    }

    /// Pick the connector's preferred mode (else modes[0]) and a
    /// CRTC that can drive its encoder. On FYS we expect
    /// 1920x1080@60 from an HDMI TV.
    pub fn pick_connector_mode(
        card: &Card,
        connector_id: connector::Handle,
    ) -> Result<ModePick> {
        let conn = card.get_connector(connector_id, false)
            .context("get_connector(pick)")?;
        let modes = conn.modes();
        if modes.is_empty() {
            bail!("connector has no modes");
        }
        let mode = modes
            .iter()
            .find(|m| m.mode_type().contains(drm::control::ModeTypeFlags::PREFERRED))
            .copied()
            .unwrap_or(modes[0]);
        let (w, h) = mode.size();
        log::info!(
            "[kms] {} modes; picked {}x{}@{} ({})",
            modes.len(),
            w,
            h,
            mode.vrefresh(),
            mode.name().to_string_lossy(),
        );
        // Encoder: prefer the connector's currently-attached one
        // if present, else the first compatible encoder reported
        // by the connector.
        let enc_id = conn.current_encoder()
            .or_else(|| conn.encoders().iter().copied().next())
            .ok_or_else(|| anyhow!("connector has no encoders"))?;
        let enc = card.get_encoder(enc_id)
            .context("get_encoder(pick)")?;
        let res = card.resource_handles()
            .context("resource_handles(pick)")?;
        // CRTC: prefer the encoder's currently-attached CRTC; else
        // the first CRTC compatible with the encoder.
        let crtc_id = enc.crtc()
            .or_else(|| {
                res.filter_crtcs(enc.possible_crtcs())
                    .into_iter()
                    .next()
            })
            .ok_or_else(|| anyhow!("no CRTC for encoder"))?;
        log::info!("[kms] encoder={enc_id:?} crtc={crtc_id:?}");
        Ok(ModePick {
            connector: connector_id,
            encoder: enc_id,
            crtc: crtc_id,
            mode,
            w: w as u32,
            h: h as u32,
        })
    }

    /// Restore the CRTC the way it was at save_current_state. Run
    /// in main even on error paths; the screen must not stay
    /// black after exit.
    pub fn restore(card: &Card, saved: &SavedState) -> Result<()> {
        log::info!(
            "[kms] restore crtc={:?} fb={:?} mode={}",
            saved.crtc,
            saved.fb,
            saved.mode.map(|m| m.name().to_string_lossy().to_string())
                .unwrap_or_else(|| "none".into()),
        );
        card.set_crtc(saved.crtc, saved.fb, saved.pos, &[saved.connector], saved.mode)
            .context("restore set_crtc")?;
        card.release_master_lock().ok();
        Ok(())
    }

    /// Page-flip run loop. Two-buffer cycle: at most one flip in
    /// flight; drain via poll+receive_events before issuing the
    /// next. PAGE_FLIP_EVENT only (no ASYNC) for firmware-paced
    /// 60Hz vsync.
    ///
    /// If `capture_path` is Some, glReadPixels-dumps the rendered
    /// back buffer once on the `capture_after_frame`-th frame
    /// BEFORE eglSwapBuffers (so we read what was just drawn,
    /// not the swapped-out one). One-shot per run.
    pub fn run_loop(
        card: &Card,
        pick: &ModePick,
        gbm: &mut Gbm,
        egl: &mut Egl,
        presenter: &mut Presenter,
        streams: &mut super::Streams,
        duration_sec: u64,
        capture_path: Option<&std::path::Path>,
        capture_after_frame: u64,
        exit_flag: &AtomicBool,
    ) -> Result<()> {
        // We have to keep the master lock alive across the whole
        // loop. acquire here (probe released its own briefly); on
        // EBUSY emit the remediation hint.
        card.acquire_master_lock()
            .context("acquire_master_lock for run_loop")?;

        let mut front_bo: Option<BufferObject<()>> = None;
        let mut front_fb: Option<framebuffer::Handle> = None;
        let mut prev_bo: Option<BufferObject<()>> = None;
        let mut prev_fb: Option<framebuffer::Handle> = None;

        let mut modeset_done = false;
        let mut flip_pending = false;
        let mut frame_idx: u64 = 0;
        let start = Instant::now();
        let max_dur = Duration::from_secs(duration_sec);
        let mut last_log = Instant::now();
        // Log the FIRST video tex bound to the presenter so we can
        // distinguish "gst delivered" (gst_decode logs that
        // separately) from "blendr accepted + bound" (this log).
        let mut first_video_tex_logged = false;
        // Track which exit cause fires so the post-loop summary
        // log can name it explicitly (otherwise we have to infer
        // from the absence of a duration / signal log).
        let mut exit_cause: &'static str = "loop body returned";

        // Per-frame timing instrumentation (QA overnight
        // mandate). Cheap counters + occasional log. Captures
        // frame_ms (present-to-present) + render_ms (GL draw +
        // swap, no vsync wait) + outlier context. Summary
        // every 30s + at exit.
        let mut stats = FrameStats::new();
        log::info!("[frame-stats] instrumentation armed; summary every 30s");

        let result: Result<()> = (|| {
            loop {
                if exit_flag.load(Ordering::Relaxed) {
                    log::info!("[kms] exit flag set; breaking loop");
                    exit_cause = "exit flag";
                    break;
                }
                if start.elapsed() >= max_dur {
                    log::info!(
                        "[kms] duration {duration_sec}s reached; breaking loop"
                    );
                    exit_cause = "duration reached";
                    break;
                }

                // Phase 3 v2 driver: tick the Streams variant
                // BEFORE the pull/draw cycle. For Cycle:
                // updates slideshow alpha + executes any
                // recreate_pending (retire non-visible slot's
                // decoder + spawn fresh with playlist[next_idx]).
                // Runs on the present thread (EGL current).
                if let Err(e) = streams.tick(&*egl) {
                    log::error!(
                        "[kms] streams.tick failed at frame {frame_idx}: {e:#}"
                    );
                    return Err(e);
                }

                // Phase 1 / Phase 2: pull latest texture(s)
                // from each stream BEFORE the draw. The pull
                // thread keeps the slot warm; latest_texture is
                // non-blocking on decode/import. GLVideoFrame
                // map happens on THIS thread (blendr's EGL
                // current). Re-claim make_current both BEFORE
                // pull (gst-gl's streaming may have stolen for
                // upload) AND AFTER each map (the map itself
                // can poke eglMakeCurrent under gst-gl).
                match streams {
                    super::Streams::None => {}
                    super::Streams::Single(g) => {
                        if let Err(e) = g.process_pending() {
                            log::warn!("[kms] process_pending: {e:#}");
                        }
                        egl.make_current()
                            .context("egl.make_current pre-gst-pull(single)")?;
                        match g.latest_texture() {
                            Ok(Some((tex_id, tgt))) => {
                                presenter.set_video_texture(tex_id, tgt);
                                if !first_video_tex_logged {
                                    log::info!(
                                        "[kms] FIRST video tex bound: \
                                         tex_id={tex_id} target={tgt:?} frame={frame_idx}",
                                    );
                                    first_video_tex_logged = true;
                                }
                            }
                            Ok(None) => {}
                            Err(e) => log::warn!(
                                "[kms] gst pull err at frame {frame_idx}: {e:#}"
                            ),
                        }
                        egl.make_current()
                            .context("egl.make_current post-gst-pull(single)")?;
                    }
                    super::Streams::Cycle {
                        slots,
                        slideshow,
                        playlist: _,
                        next_idx: _,
                        recreate_pending: _,
                        last_retire_at: _,
                        last_create_at: _,
                    } => {
                        // Phase 3 v2: drain each slot decoder's
                        // pending pipeline commands (concat add-
                        // next on EOS) on the present thread.
                        // `slots` may have None in a recreate
                        // window — skip if so.
                        for slot_opt in slots.iter_mut() {
                            if let Some(d) = slot_opt.as_mut() {
                                if let Err(e) = d.process_pending() {
                                    log::warn!("[kms] process_pending: {e:#}");
                                }
                            }
                        }
                        // Phase 3 state machine: alpha.
                        // (Streams::tick() was already called
                        // above and may have done retire+
                        // recreate; this is the per-frame alpha
                        // read; cheap.)
                        let (alpha, phase_changed) = slideshow.update();
                        if phase_changed {
                            log::info!(
                                "[slideshow] PHASE -> {:?} alpha={alpha:.2} \
                                 cycle={} frame={frame_idx} \
                                 elapsed_total={:.2}s",
                                slideshow.phase(),
                                slideshow.cycle_count(),
                                start.elapsed().as_secs_f32(),
                            );
                        }
                        egl.make_current()
                            .context("egl.make_current pre-gst-pull(slot0)")?;
                        let a_pair = match slots[0].as_mut() {
                            Some(d) => match d.latest_texture() {
                                Ok(p) => p,
                                Err(e) => {
                                    log::warn!(
                                        "[kms] slot0 pull err at frame {frame_idx}: {e:#}"
                                    );
                                    None
                                }
                            },
                            None => None,
                        };
                        egl.make_current()
                            .context("egl.make_current post-gst-pull(slot0)")?;
                        let b_pair = match slots[1].as_mut() {
                            Some(d) => match d.latest_texture() {
                                Ok(p) => p,
                                Err(e) => {
                                    log::warn!(
                                        "[kms] slot1 pull err at frame {frame_idx}: {e:#}"
                                    );
                                    None
                                }
                            },
                            None => None,
                        };
                        egl.make_current()
                            .context("egl.make_current post-gst-pull(slot1)")?;
                        if let (Some(a_t), Some(b_t)) = (a_pair, b_pair) {
                            presenter.set_video_textures(a_t, b_t, alpha);
                            if !first_video_tex_logged {
                                log::info!(
                                    "[kms] FIRST blend tex pair bound: \
                                     A=(tex={} target={:?}) B=(tex={} target={:?}) \
                                     alpha={alpha:.2} frame={frame_idx}",
                                    a_t.0, a_t.1, b_t.0, b_t.1,
                                );
                                first_video_tex_logged = true;
                            }
                        }
                    }
                }

                // Measurement: stamp before draw to time the GL
                // draw + swap work (render_ms), excluding the
                // vsync drain that happens later in the iter.
                let t_draw_start = Instant::now();
                presenter
                    .draw_frame(frame_idx)
                    .with_context(|| format!("draw_frame {frame_idx}"))?;

                // One-shot capture BEFORE swap_buffers so we read
                // the just-drawn back buffer. Capture is
                // non-destructive: render loop continues after.
                if let Some(path) = capture_path {
                    if frame_idx == capture_after_frame {
                        presenter
                            .capture_back_buffer_ppm(path)
                            .with_context(|| {
                                format!(
                                    "capture_back_buffer_ppm({})",
                                    path.display()
                                )
                            })?;
                    }
                }

                egl.swap_buffers()
                    .with_context(|| format!("swap_buffers {frame_idx}"))?;
                // render_ms = draw_frame + swap (no vsync wait).
                let render_ms =
                    (Instant::now() - t_draw_start).as_secs_f32() * 1000.0;

                // Pull the freshly-rendered BO off the GBM surface.
                // SAFETY: surface is current; lock_front_buffer must
                // be paired with release_buffer.
                let new_bo: BufferObject<()> = unsafe {
                    gbm.surface.lock_front_buffer()
                        .map_err(|e| anyhow!("gbm lock_front_buffer {frame_idx}: {e:?}"))?
                };
                let fb_buf = GbmBufferAdapter::new(&new_bo)
                    .with_context(|| format!("GbmBufferAdapter::new {frame_idx}"))?;
                let new_fb = card
                    .add_framebuffer(&fb_buf, 32, 32)
                    .with_context(|| format!("add_framebuffer {frame_idx}"))?;

                // Drain any in-flight flip BEFORE issuing the next.
                if flip_pending {
                    drain_one_flip(card, Duration::from_millis(500))
                        .with_context(|| format!("drain_one_flip {frame_idx}"))?;
                    flip_pending = false;
                    if let Some(fb) = prev_fb.take() {
                        let _ = card.destroy_framebuffer(fb);
                    }
                    drop(prev_bo.take());
                }

                if !modeset_done {
                    card.set_crtc(
                        pick.crtc,
                        Some(new_fb),
                        (0, 0),
                        &[pick.connector],
                        Some(pick.mode),
                    )
                    .with_context(|| format!("set_crtc initial modeset {frame_idx}"))?;
                    modeset_done = true;
                    log::info!(
                        "[kms] initial modeset done crtc={:?} fb={:?}",
                        pick.crtc,
                        new_fb
                    );
                } else {
                    card.page_flip(
                        pick.crtc,
                        new_fb,
                        PageFlipFlags::EVENT,
                        None,
                    )
                    .with_context(|| format!("page_flip {frame_idx}"))?;
                    flip_pending = true;
                }

                // Frame-stats record (zero-cost per frame:
                // counters + Vec push; rare outlier log).
                // Stamp present-end NOW (after page_flip issued;
                // the actual vsync drain happens at the top of
                // NEXT iteration so frame_ms is roughly
                // page_flip-call to page_flip-call cadence =
                // present cadence).
                if let Some(frame_ms) = stats.stamp_present_end(Instant::now()) {
                    stats.record(frame_ms, render_ms, frame_idx, &*streams);
                }

                // Cycle: front -> prev; new -> front.
                prev_bo = front_bo.take();
                prev_fb = front_fb.take();
                front_bo = Some(new_bo);
                front_fb = Some(new_fb);

                frame_idx += 1;

                if last_log.elapsed() >= Duration::from_secs(1) {
                    // Phase 3 v2: per-slot PTS read from
                    // whichever decoder is currently in each
                    // slot (may be None during a retire window).
                    if let super::Streams::Cycle { slots, slideshow, .. } = &*streams {
                        let pts_a = slots[0]
                            .as_ref()
                            .and_then(|d| d.last_pts_ns())
                            .map(|n| n as f64 / 1e9)
                            .unwrap_or(-1.0);
                        let pts_b = slots[1]
                            .as_ref()
                            .and_then(|d| d.last_pts_ns())
                            .map(|n| n as f64 / 1e9)
                            .unwrap_or(-1.0);
                        log::info!(
                            "[kms] tick frame={frame_idx} elapsed={:.1}s \
                             phase={:?} cycle={} ptsA={pts_a:.2}s ptsB={pts_b:.2}s",
                            start.elapsed().as_secs_f32(),
                            slideshow.phase(),
                            slideshow.cycle_count(),
                        );
                    } else {
                        log::info!(
                            "[kms] tick frame={frame_idx} elapsed={:.1}s",
                            start.elapsed().as_secs_f32()
                        );
                    }
                    last_log = Instant::now();
                }
            }
            Ok(())
        })();

        // Cumulative frame-stats summary at exit.
        stats.emit_final_summary();

        // Log the exit cause + result BEFORE the cleanup so QA
        // sees it even if a downstream Drop in main panics
        // somehow. Per QA Phase 1 ccb27ea: blendr exited at
        // ~frame 5 silently; this log makes that impossible.
        match &result {
            Ok(()) => log::info!(
                "[kms] run_loop EXIT Ok at frame={frame_idx} \
                 elapsed={:.2}s cause={exit_cause}",
                start.elapsed().as_secs_f32()
            ),
            Err(e) => log::error!(
                "[kms] run_loop EXIT ERR at frame={frame_idx} \
                 elapsed={:.2}s cause={exit_cause}: {e:#}",
                start.elapsed().as_secs_f32()
            ),
        }

        // Drain trailing flip so the kernel is not racing scan-out
        // with our front_bo when restore() retargets.
        if flip_pending {
            let _ = drain_one_flip(card, Duration::from_millis(500));
        }
        if let Some(fb) = prev_fb.take() {
            let _ = card.destroy_framebuffer(fb);
        }
        if let Some(fb) = front_fb.take() {
            let _ = card.destroy_framebuffer(fb);
        }
        drop(prev_bo);
        drop(front_bo);

        result
    }

    /// Poll-gate one drmModePageFlip completion event. Without
    /// the poll gate, receive_events() could block on a wedged HW
    /// vblank forever. Returns Ok(()) when one PageFlip event is
    /// drained or the timeout elapses (we treat timeout as
    /// recoverable: log a warn but don't fail the loop).
    fn drain_one_flip(card: &Card, timeout: Duration) -> Result<()> {
        use nix::poll::{poll, PollFd, PollFlags, PollTimeout};

        let fd = card.0.as_fd();
        let mut fds = [PollFd::new(fd, PollFlags::POLLIN)];
        let ms: i32 = timeout
            .as_millis()
            .try_into()
            .unwrap_or(i32::MAX);
        let n = poll(&mut fds, PollTimeout::try_from(ms).unwrap_or(PollTimeout::NONE))
            .context("poll(drm fd)")?;
        if n == 0 {
            log::warn!(
                "[kms] drain_one_flip timeout {}ms (HW vblank stalled?)",
                ms
            );
            return Ok(());
        }
        let events = card.receive_events().context("receive_events")?;
        for ev in events {
            if matches!(ev, Event::PageFlip(_)) {
                return Ok(());
            }
        }
        // No PageFlip in this batch; treat as benign (next iteration
        // will retry).
        Ok(())
    }

    // ---------- GbmBufferAdapter (drm-rs Buffer over a gbm BO) ----------

    /// drm-rs's `add_framebuffer` takes &impl Buffer. gbm 0.15's
    /// BufferObject does NOT implement drm::buffer::Buffer
    /// directly, so we read the four required fields at construction
    /// and present them via a tiny adapter. Pattern lifted from the
    /// OLD renderer's GbmBufferAdapter (hdmi.rs:15458).
    struct GbmBufferAdapter {
        width: u32,
        height: u32,
        format: DrmFourcc,
        pitch: u32,
        handle: DrmHandle,
    }

    impl GbmBufferAdapter {
        fn new<T: 'static>(bo: &BufferObject<T>) -> Result<Self> {
            let width = bo.width().context("gbm bo width")?;
            let height = bo.height().context("gbm bo height")?;
            let stride = bo.stride().context("gbm bo stride")?;
            let gbm_fmt = bo.format().context("gbm bo format")?;
            let format = match gbm_fmt {
                GbmFormat::Argb8888 => DrmFourcc::Argb8888,
                GbmFormat::Xrgb8888 => DrmFourcc::Xrgb8888,
                other => bail!("unsupported gbm format for AddFB: {other:?}"),
            };
            // gbm_bo_handle is a C union; for DRM we read .u32_.
            let bo_handle = bo.handle().context("gbm bo handle")?;
            // SAFETY: u32_ is the variant DRM uses for handle ids;
            // reading any union field is unsafe in Rust regardless.
            let raw = unsafe { bo_handle.u32_ };
            let nz = std::num::NonZeroU32::new(raw)
                .ok_or_else(|| anyhow!("gbm bo handle was 0"))?;
            Ok(GbmBufferAdapter {
                width,
                height,
                format,
                pitch: stride,
                handle: DrmHandle::from(nz),
            })
        }
    }

    impl DrmBuffer for GbmBufferAdapter {
        fn size(&self) -> (u32, u32) {
            (self.width, self.height)
        }
        fn format(&self) -> DrmFourcc {
            self.format
        }
        fn pitch(&self) -> u32 {
            self.pitch
        }
        fn handle(&self) -> DrmHandle {
            self.handle
        }
    }
}
