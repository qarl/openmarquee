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
    /// Step::Video: one decoder.
    Single(crate::gst_decode::GstDecoder),
    /// Step::Blend (Phase 2 static / Phase 3 slideshow): two
    /// long-lived decoders. SlideshowState drives the alpha
    /// each iteration (Phase 3); a static alpha is a special
    /// case where hold_sec is huge.
    Blend {
        a: crate::gst_decode::GstDecoder,
        b: crate::gst_decode::GstDecoder,
        slideshow: SlideshowState,
    },
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
        }
    }

    /// Returns (current_alpha, phase_changed_this_tick).
    pub fn update(&mut self) -> (f32, bool) {
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
                    super::Streams::Blend { a, b, slideshow } => {
                        // Phase 3: state machine drives alpha.
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
                            .context("egl.make_current pre-gst-pull(blend-a)")?;
                        let a_pair = match a.latest_texture() {
                            Ok(p) => p,
                            Err(e) => {
                                log::warn!(
                                    "[kms] gst A pull err at frame {frame_idx}: {e:#}"
                                );
                                None
                            }
                        };
                        egl.make_current()
                            .context("egl.make_current post-gst-pull(blend-a)")?;
                        let b_pair = match b.latest_texture() {
                            Ok(p) => p,
                            Err(e) => {
                                log::warn!(
                                    "[kms] gst B pull err at frame {frame_idx}: {e:#}"
                                );
                                None
                            }
                        };
                        egl.make_current()
                            .context("egl.make_current post-gst-pull(blend-b)")?;
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

                // Cycle: front -> prev; new -> front.
                prev_bo = front_bo.take();
                prev_fb = front_fb.take();
                front_bo = Some(new_bo);
                front_fb = Some(new_fb);

                frame_idx += 1;

                if last_log.elapsed() >= Duration::from_secs(1) {
                    // Include slideshow phase + alpha if we're in
                    // Blend mode so QA can grep the cadence + see
                    // crossfade transitions in the tick stream
                    // without needing the [slideshow] event lines.
                    if let super::Streams::Blend { slideshow, .. } = &*streams {
                        log::info!(
                            "[kms] tick frame={frame_idx} elapsed={:.1}s \
                             phase={:?} cycle={}",
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
