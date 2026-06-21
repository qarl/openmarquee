//! Phase 1 (single-stream) + Phase 2 (two-stream blend)
//! + Phase 3 v1 (slideshow w/ concat add-next looping)
//! keystone import: cutloop-shaped GStreamer pipeline(s)
//! decoding H.264 into GL textures that blendr samples
//! directly.
//!
//! Pipeline (PER decoder; dynamic sub-bins on the concat side):
//!
//!   filesrc(clip) ! qtdemux ! h264parse ! concat.sink_0  ┐
//!   filesrc(clip) ! qtdemux ! h264parse ! concat.sink_1  ├── pre-queued at init
//!   ...                                                  ┘
//!   concat ! v4l2h264dec ! queue(leaky) ! glupload ! appsink
//!
//! INITIAL_QUEUE_DEPTH=2 sub-bins are pre-queued at init.
//! Each sub-bin's concat sink pad has an EVENT_DOWNSTREAM
//! probe; when concat absorbs an intra-stream EOS at switch,
//! the probe sends (AddNextClip, Retire(serial)) commands to
//! the present-thread channel. The present thread drains the
//! channel per iteration via `process_pending()` and runs the
//! retire + add on its own thread (cutloop's GLib.idle_add
//! pattern adapted to Rust without a GLib main loop).
//!
//! Why concat instead of seek-to-0 (per BUG A glass):
//! v4l2h264dec on bcm2835-codec is brittle around FLUSH-seek
//! (memory note: "bcm2835-codec STREAMOFF + EOS — VIDIOC_TRY
//! _DECODER_CMD for probes; CMD_STOP on empty pipe wedges").
//! sync=true + seek-to-0 on EOS: clip plays to end, decoder
//! freezes on last frame, no further EOS fires, seek never
//! triggered. cutloop.py's PROVEN approach (17 clips back-to-
//! back, real Pi soak): concat absorbs intra-stream EOS at
//! sub-bin boundaries; v4l2h264dec stays running across the
//! switch (handles the new SPS/PPS via h264parse config-
//! interval=-1). Single-clip looping = same clip queued as
//! each new sub-bin; concat handles the loop boundary as just
//! another source-to-source switch.

use anyhow::Result;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TexTarget {
    /// GL_TEXTURE_EXTERNAL_OES (0x8D65) -- zero-copy DMABuf
    /// from glupload; samplerExternalOES in the shader.
    External,
    /// GL_TEXTURE_2D -- RGBA. Documented fallback; vc4 always
    /// returns External in practice.
    TwoD,
}

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(not(target_os = "linux"))]
pub use stub::*;

#[cfg(not(target_os = "linux"))]
mod stub {
    use super::*;
    pub struct GstDecoder {
        pub tex_target: TexTarget,
    }
    pub struct PendingDecoder;
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum PendingState {
        AwaitingPaused,
        AwaitingPlaying,
        Ready,
    }
    impl GstDecoder {
        pub fn new(_egl: &crate::egl_gbm::Egl, _clip: &std::path::Path) -> Result<Self> {
            anyhow::bail!("GstDecoder: Linux only")
        }
        pub fn latest_texture(&mut self) -> Result<Option<(u32, TexTarget)>> {
            anyhow::bail!("GstDecoder: Linux only")
        }
        pub fn last_pts_ns(&self) -> Option<u64> {
            None
        }
        pub fn ms_since_last_concat_add(&self) -> Option<u128> {
            None
        }
        pub fn process_pending(&mut self) -> Result<()> {
            Ok(())
        }
    }
    impl PendingDecoder {
        pub fn start_async(
            _egl: &crate::egl_gbm::Egl,
            _clip: &std::path::Path,
        ) -> Result<Self> {
            anyhow::bail!("PendingDecoder: Linux only")
        }
        pub fn poll(&mut self) -> Result<PendingState> {
            anyhow::bail!("PendingDecoder: Linux only")
        }
        pub fn finalize(self) -> Result<GstDecoder> {
            anyhow::bail!("PendingDecoder: Linux only")
        }
        pub fn state(&self) -> PendingState {
            PendingState::AwaitingPaused
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::egl_gbm::Egl;
    use anyhow::{anyhow, Context};
    use gst::glib;
    use gst::prelude::*;
    use gst_gl::prelude::*;
    use std::collections::VecDeque;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::mpsc;
    use std::sync::{Arc, Mutex};
    use std::thread::{self, JoinHandle};

    pub const GL_TEXTURE_EXTERNAL_OES: u32 = 0x8D65;

    /// Pull-thread tick.
    const PULL_TICK_MS: u64 = 33;
    // FIRST_SAMPLE_TIMEOUT_S removed per QA #2 fix -- the first
    // sample is no longer awaited at construction (pull thread
    // populates latest_sample whenever it arrives).
    /// Always pre-queue this many sub-bins so concat never runs dry.
    const INITIAL_QUEUE_DEPTH: usize = 2;

    /// Tracking handle for one dynamically-added sub-bin.
    /// The bin owns its own filesrc/qtdemux/h264parse; we keep
    /// the metadata needed to disconnect/remove on retire.
    struct SubBin {
        serial: u64,
        bin: gst::Bin,
        qtdemux: gst::Element,
        pad_added_id: Option<glib::SignalHandlerId>,
        concat_sink: gst::Pad,
        probe_id: Option<gst::PadProbeId>,
        /// BUG A v3 diagnostic: count how many times the EOS
        /// probe fires for THIS sub-bin. Should be exactly 1
        /// per sub-bin in steady state. >1 indicates sticky-
        /// EOS double-fire (QA's H2 hypothesis); the
        /// retire-then-add cycle still acts on the first; the
        /// extras are just log noise here but would cause
        /// runaway adds if act-on-every-fire was the policy.
        /// Logged each fire so QA can distinguish "premature
        /// EOS" (each probe fires once, but at 1.5s instead of
        /// 4.75s = back-pressure issue) from "duplicate fire"
        /// (probe re-fires for same EOS = needs latch).
        eos_fire_count: Arc<AtomicU64>,
    }

    /// Commands sent from EOS probes (on gst streaming threads)
    /// to the present thread, which processes them in
    /// `process_pending()` per iteration. Decouples gst-side
    /// pad-probe firing from the actual pipeline mutation
    /// (which we want on a single thread for safety).
    enum PipelineCmd {
        /// Append a new sub-bin (same clip) to concat.
        AddNextClip,
        /// Retire the sub-bin whose serial matches.
        Retire(u64),
    }

    pub struct GstDecoder {
        pipeline: gst::Pipeline,
        concat: gst::Element,
        #[allow(dead_code)]
        appsink: gst_app::AppSink,
        /// Measurement infra (per QA overnight mandate). Set
        /// from the EOS probe (gst streaming thread) to mark
        /// when concat switches sub-bin (= per-clip loop point).
        /// Read by Streams::outlier_context() on the present
        /// thread for outlier log "flags=near_concat_add".
        /// Mutex held microseconds; contention negligible at
        /// ~1 event per clip-loop per stream.
        last_concat_add_at: Arc<Mutex<Option<std::time::Instant>>>,
        /// Latest sample slot (replaced by pull thread; taken
        /// by present thread via latest_texture()).
        latest_sample: Arc<Mutex<Option<gst::Sample>>>,
        stop: Arc<AtomicBool>,
        pull_thread: Option<JoinHandle<()>>,
        current_frame: Option<gst_gl::GLVideoFrame<gst_gl::gl_video_frame::Readable>>,
        pub tex_target: TexTarget,
        last_pts_ns: Option<u64>,
        clip_basename: String,
        /// Clip path; re-fed on every add_next_clip for the
        /// single-clip looping case. (Phase 3 v2 will extend
        /// this to a playlist at the OUTER Streams::Cycle level
        /// by retire+recreate of the whole GstDecoder; each
        /// GstDecoder stays single-clip internally.)
        clip_path: PathBuf,
        /// Live sub-bins queue (FIFO; front = active source
        /// concat is currently consuming).
        sub_bins: Mutex<VecDeque<SubBin>>,
        /// Monotonic sub-bin serial for EOS-probe identification.
        serial_counter: AtomicU64,
        /// EOS probes send commands here from gst streaming
        /// threads. process_pending() drains on the present
        /// thread.
        pipe_cmd_tx: mpsc::Sender<PipelineCmd>,
        pipe_cmd_rx: mpsc::Receiver<PipelineCmd>,
        #[allow(dead_code)]
        gst_display: gst_gl::GLDisplay,
        #[allow(dead_code)]
        gst_context: gst_gl::GLContext,
    }

    impl GstDecoder {
        pub fn last_pts_ns(&self) -> Option<u64> {
            self.last_pts_ns
        }

        /// Measurement infra: ms since the last concat sub-bin
        /// add (= clip loop boundary) on this decoder. None if
        /// no boundary has fired yet. Read on the present
        /// thread for [frame-long] outlier context.
        pub fn ms_since_last_concat_add(&self) -> Option<u128> {
            let g = self.last_concat_add_at.lock().ok()?;
            let t = (*g)?;
            Some(t.elapsed().as_millis())
        }

        /// SYNC wrapper around PendingDecoder for startup paths
        /// (main.rs initial decoders, Streams::Single). Mid-run
        /// Streams::Cycle uses PendingDecoder directly to keep
        /// the present thread non-blocking (#2-alt).
        ///
        /// Polls PendingDecoder in a tight loop until Ready or
        /// 20s timeout. Equivalent behavior to the pre-#2-alt
        /// blocking new() for callers that need a fully-driven
        /// decoder in one call.
        pub fn new(egl: &Egl, clip: &Path) -> Result<Self> {
            let mut p = PendingDecoder::start_async(egl, clip)?;
            let timeout = std::time::Duration::from_secs(20);
            let start = std::time::Instant::now();
            while p.poll()? != PendingState::Ready {
                if start.elapsed() > timeout {
                    return Err(anyhow!(
                        "GstDecoder::new sync timeout (state={:?})",
                        p.state()
                    ));
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            p.finalize()
        }
    }

    /// State of an in-progress async decoder construction.
    /// #2-alt: GStreamer state transitions (PAUSED, PLAYING)
    /// run on internal streaming threads. The expensive
    /// 140-771ms in the old new() was the CALLER blocking in
    /// pipeline.state(5s)/state(10s) waits. PendingDecoder
    /// replaces those blocking waits with non-blocking
    /// pipeline.state(ZERO) polls driven by the present thread
    /// once per tick (~16ms). Per-poll cost is ~µs; total
    /// wall-clock to Ready is the same as the sync path (the
    /// actual work happens off-thread either way).
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum PendingState {
        /// set_state(PAUSED) issued; waiting for preroll
        /// (v4l2h264dec decoding first frame).
        AwaitingPaused,
        /// PAUSED reached; set_state(PLAYING) issued; waiting
        /// for clock-sync to settle.
        AwaitingPlaying,
        /// PLAYING reached; ready for finalize() to spawn the
        /// pull thread and return a usable GstDecoder.
        Ready,
    }

    pub struct PendingDecoder {
        inner: Option<GstDecoder>,
        state: PendingState,
        started_at: std::time::Instant,
    }

    impl PendingDecoder {
        pub fn state(&self) -> PendingState {
            self.state
        }

        /// Build everything (pipeline + sub-bins + bus handler +
        /// GL context wrapping), issue set_state(PAUSED), and
        /// return WITHOUT waiting. The caller drives subsequent
        /// state transitions via poll() until Ready, then calls
        /// finalize() to spawn the pull thread and get a
        /// usable GstDecoder.
        ///
        /// pull_thread is None on the inner GstDecoder until
        /// finalize(). Drop on a non-finalized PendingDecoder
        /// works because GstDecoder::Drop handles pull_thread=
        /// None and the BUG C FlushStart pattern is safe at
        /// any pipeline state.
        pub fn start_async(egl: &Egl, clip: &Path) -> Result<Self> {
            gst::init().context("gst::init")?;

            // (a) Wrap blendr's EGLDisplay + EGLContext per
            //     proven Phase 1 setup.
            let egl_display_ptr = egl.display.as_ptr() as usize;
            let gst_display: gst_gl::GLDisplay = unsafe {
                gst_gl_egl::GLDisplayEGL::with_egl_display(egl_display_ptr)
                    .map_err(|e| anyhow!("GLDisplayEGL::with_egl_display: {e}"))?
            }
            .upcast();
            let egl_ctx_handle = egl.context.as_ptr() as usize;
            let gst_context: gst_gl::GLContext = unsafe {
                gst_gl::GLContext::new_wrapped(
                    &gst_display,
                    egl_ctx_handle,
                    gst_gl::GLPlatform::EGL,
                    gst_gl::GLAPI::GLES2,
                )
            }
            .ok_or_else(|| anyhow!("GLContext::new_wrapped returned None"))?;
            gst_context
                .activate(true)
                .map_err(|e| anyhow!("gst_context.activate(true): {e}"))?;
            gst_context
                .fill_info()
                .map_err(|e| anyhow!("gst_context.fill_info: {e}"))?;
            gst_context
                .activate(false)
                .map_err(|e| anyhow!("gst_context.activate(false): {e}"))?;

            // (b) Build the STATIC downstream half:
            //     concat -> v4l2h264dec -> queue -> glupload
            //              -> appsink.
            //     Sub-bins (filesrc + qtdemux + h264parse) are
            //     added DYNAMICALLY via add_next_clip below.
            let pipeline = gst::Pipeline::new();
            let concat = gst::ElementFactory::make("concat")
                .build()
                .context("make concat")?;
            let v4l2dec = gst::ElementFactory::make("v4l2h264dec")
                .build()
                .context("make v4l2h264dec")?;
            // BUG A v3: leaky=NO (NOT downstream) so the
            // queue back-pressures the decoder when full.
            // Combined with appsink.set_drop(false) below +
            // appsink.set_sync(true) (which paces presentation
            // against the pipeline clock), the chain becomes
            // decoder → queue (blocks at 2 buffers) → glupload
            // → appsink (paced real-time). Decoder runs at the
            // clip's native frame rate; EOS fires when the
            // clip's last frame is CONSUMED by appsink, not
            // when the decoder finishes producing. Without
            // this, decoder free-runs at ~3x real-time, EOS
            // fires early (~1.5s for a 4.75s clip per QA glass
            // f3c0d7c), rapid add/retire churn confuses
            // v4l2h264dec, periodic 5-15s decoder stalls.
            //
            // cutloop.py uses the same non-leaky pattern with
            // kmssink sync=true as the pacing consumer. Our
            // appsink sync=true plays the same role.
            //
            // CMA: with 2-buffer cap + back-pressure, only 2
            // decoded NV12 buffers held downstream at any
            // moment. Decoder's V4L2 CAPTURE pool holds ~4-6
            // more; total per-stream peak is identical to the
            // leaky-queue setup. The DROPS we lose (leaky=down
            // would drop oldest on overflow) weren't necessary
            // for CMA -- only for "lossy real-time display"
            // semantics that don't apply when the consumer
            // (appsink) paces correctly.
            let outq = gst::ElementFactory::make("queue")
                .property_from_str("leaky", "no")
                .property("max-size-buffers", 2u32)
                .property("max-size-bytes", 0u32)
                .property("max-size-time", 0u64)
                .build()
                .context("make queue (post-dec)")?;
            let glupload = gst::ElementFactory::make("glupload")
                .build()
                .context("make glupload")?;
            let appsink_el = gst::ElementFactory::make("appsink")
                .name("sink")
                .build()
                .context("make appsink")?;
            let appsink = appsink_el
                .clone()
                .dynamic_cast::<gst_app::AppSink>()
                .map_err(|_| anyhow!("appsink dynamic_cast failed"))?;
            pipeline
                .add_many([&concat, &v4l2dec, &outq, &glupload, &appsink_el])
                .context("pipeline.add_many (static elements)")?;
            gst::Element::link_many([&concat, &v4l2dec, &outq, &glupload, &appsink_el])
                .context("link concat->dec->outq->glupload->appsink")?;

            // appsink config: RGBA GLMemory caps; sync=true for
            // real-time pacing (BUG A fix from b5e12b5 PRESERVED);
            // drop=true so we keep latest not complete.
            let caps_rgba = gst::Caps::builder("video/x-raw")
                .features(["memory:GLMemory"])
                .field("format", "RGBA")
                .build();
            appsink.set_caps(Some(&caps_rgba));
            appsink.set_max_buffers(2);
            // BUG A v3: drop=FALSE so appsink back-pressures
            // upstream (queue) when full instead of dropping
            // newer samples. Pair with the non-leaky queue
            // above for end-to-end back-pressure. With
            // sync=true pacing presentation against the clock,
            // the decoder runs at clip framerate (not
            // decoder-max), EOS fires at the natural end (not
            // 3x early), and concat add/retire cadence matches
            // wall-clock clip duration.
            appsink.set_drop(false);
            appsink.set_sync(true);
            appsink.set_property("emit-signals", false);

            // (c) Bus SYNC handler: NEED_CONTEXT + visibility
            //     logging. We do NOT EOS-handle on the bus
            //     anymore -- concat absorbs intra-stream EOS at
            //     the per-sub-bin boundary (caught by our
            //     per-pad probes, NOT the bus). A bus EOS would
            //     mean concat ran out of sources entirely =
            //     bug; log it loudly.
            let bus = pipeline
                .bus()
                .ok_or_else(|| anyhow!("pipeline has no bus"))?;
            let display_for_bus = gst_display.clone();
            let context_for_bus = gst_context.clone();
            bus.set_sync_handler(move |_bus, msg| {
                use gst::MessageView;
                let src_name = msg
                    .src()
                    .map(|s| s.name().to_string())
                    .unwrap_or_else(|| "?".into());
                match msg.view() {
                    MessageView::NeedContext(nc) => {
                        let ctx_type = nc.context_type();
                        log::info!(
                            "[gst-bus] NEED_CONTEXT type={ctx_type} src={src_name}"
                        );
                        if ctx_type == "gst.gl.GLDisplay" {
                            let mut c = gst::Context::new(ctx_type, true);
                            c.make_mut()
                                .structure_mut()
                                .set("gst.gl.GLDisplay", &display_for_bus);
                            if let Some(el) = msg
                                .src()
                                .and_then(|s| s.downcast_ref::<gst::Element>())
                            {
                                el.set_context(&c);
                            }
                        } else if ctx_type == "gst.gl.app_context" {
                            let mut c = gst::Context::new(ctx_type, true);
                            c.make_mut()
                                .structure_mut()
                                .set("context", &context_for_bus);
                            if let Some(el) = msg
                                .src()
                                .and_then(|s| s.downcast_ref::<gst::Element>())
                            {
                                el.set_context(&c);
                            }
                        }
                        return gst::BusSyncReply::Drop;
                    }
                    MessageView::StateChanged(sc) => {
                        let reaching_playing =
                            sc.current() == gst::State::Playing;
                        let from_pipeline = src_name.starts_with("pipeline");
                        if reaching_playing || from_pipeline {
                            log::info!(
                                "[gst-bus] STATE src={src_name} {:?}->{:?} pending={:?}",
                                sc.old(),
                                sc.current(),
                                sc.pending(),
                            );
                        }
                    }
                    MessageView::Error(e) => {
                        log::error!(
                            "[gst-bus] ERROR src={src_name} {} ({:?})",
                            e.error(),
                            e.debug(),
                        );
                    }
                    MessageView::Warning(w) => {
                        log::warn!(
                            "[gst-bus] WARN src={src_name} {} ({:?})",
                            w.error(),
                            w.debug(),
                        );
                    }
                    MessageView::Eos(_) => {
                        // concat ran out of sources -> bug in
                        // our add_next_clip cadence. Should
                        // never happen with INITIAL_QUEUE_DEPTH
                        // + per-EOS add_next.
                        log::error!(
                            "[gst-bus] DOWNSTREAM EOS src={src_name} \
                             -- concat ran out of sub-bins! add_next \
                             cadence broken; pipeline will halt"
                        );
                    }
                    MessageView::AsyncDone(_) => {
                        log::info!("[gst-bus] ASYNC_DONE src={src_name}");
                    }
                    _ => {}
                }
                gst::BusSyncReply::Pass
            });

            // (d) Build the GstDecoder struct skeleton so we can
            //     call add_next_clip on it. The channel + state
            //     get filled here; sub_bins starts empty.
            let (pipe_cmd_tx, pipe_cmd_rx) = mpsc::channel::<PipelineCmd>();
            let clip_basename = clip
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("?")
                .to_string();

            let mut me = GstDecoder {
                pipeline,
                concat,
                appsink: appsink.clone(),
                last_concat_add_at: Arc::new(Mutex::new(None)),
                latest_sample: Arc::new(Mutex::new(None)),
                stop: Arc::new(AtomicBool::new(false)),
                pull_thread: None,
                current_frame: None,
                tex_target: TexTarget::TwoD,
                last_pts_ns: None,
                clip_basename: clip_basename.clone(),
                clip_path: clip.to_path_buf(),
                sub_bins: Mutex::new(VecDeque::new()),
                serial_counter: AtomicU64::new(0),
                pipe_cmd_tx,
                pipe_cmd_rx,
                gst_display,
                gst_context,
            };

            // (e) Pre-queue INITIAL_QUEUE_DEPTH sub-bins. concat
            //     plays the first; the rest are pending.
            for _ in 0..INITIAL_QUEUE_DEPTH {
                me.add_next_clip()?;
            }

            // (f) #2-alt: State -> PAUSED (NON-blocking).
            //     The old new() blocked here in pipeline.state(5s)
            //     and again in state(10s) after PLAYING. That's
            //     the 140-771ms freeze QA's SOURCE A residual
            //     traced to.
            //
            //     set_state(PAUSED) returns StateChangeReturn
            //     immediately (ASYNC for non-live pipelines like
            //     ours: filesrc->qtdemux->h264parse->v4l2h264dec).
            //     The actual transition (decoder open + caps
            //     negotiation + first-buffer preroll) runs on
            //     GStreamer streaming threads.
            //
            //     PendingDecoder::poll() drives non-blocking
            //     pipeline.state(ZERO) checks per present-thread
            //     tick. When AwaitingPaused observes settled
            //     PAUSED, it issues set_state(PLAYING) and
            //     transitions to AwaitingPlaying. When that
            //     settles, state becomes Ready.
            log::info!("[gst] [{clip_basename}] set_state(PAUSED) (async; will poll)");
            let preroll_ret = me
                .pipeline
                .set_state(gst::State::Paused)
                .map_err(|e| anyhow!("set_state(PAUSED): {e:?}"))?;
            log::info!(
                "[gst] [{clip_basename}] set_state(PAUSED) returned {preroll_ret:?}"
            );

            Ok(PendingDecoder {
                inner: Some(me),
                state: PendingState::AwaitingPaused,
                started_at: std::time::Instant::now(),
            })
        }

        /// Drive the async state machine. Call once per present
        /// thread tick. Returns the current state; when Ready,
        /// the caller should call finalize() to spawn the pull
        /// thread and consume self into a usable GstDecoder.
        ///
        /// Per-call cost: one Element::state(ZERO) call =
        /// ~microseconds (mutex lock + state read). Plus one
        /// log::info! per state transition (not per poll).
        ///
        /// Timeouts mirror the old sync new(): 10s for PAUSED,
        /// 20s total. On timeout returns Err; caller drops the
        /// PendingDecoder (its inner GstDecoder Drop cleans up
        /// the pipeline normally).
        pub fn poll(&mut self) -> Result<PendingState> {
            let inner = self
                .inner
                .as_mut()
                .ok_or_else(|| anyhow!("PendingDecoder already finalized"))?;
            match self.state {
                PendingState::AwaitingPaused => {
                    let (_, cur, pending) =
                        inner.pipeline.state(gst::ClockTime::ZERO);
                    if cur == gst::State::Paused
                        && pending == gst::State::VoidPending
                    {
                        let elapsed = self.started_at.elapsed().as_millis();
                        log::info!(
                            "[gst] [{}] PAUSED reached after {elapsed}ms; \
                             set_state(PLAYING) (async)",
                            inner.clip_basename
                        );
                        let play_ret = inner
                            .pipeline
                            .set_state(gst::State::Playing)
                            .map_err(|e| anyhow!("set_state PLAYING: {e:?}"))?;
                        log::info!(
                            "[gst] [{}] set_state(PLAYING) returned {play_ret:?}",
                            inner.clip_basename
                        );
                        self.state = PendingState::AwaitingPlaying;
                    } else if self.started_at.elapsed()
                        > std::time::Duration::from_secs(10)
                    {
                        return Err(anyhow!(
                            "[{}] timeout awaiting PAUSED \
                             (cur={cur:?} pending={pending:?})",
                            inner.clip_basename
                        ));
                    }
                }
                PendingState::AwaitingPlaying => {
                    let (_, cur, pending) =
                        inner.pipeline.state(gst::ClockTime::ZERO);
                    if cur == gst::State::Playing
                        && pending == gst::State::VoidPending
                    {
                        let elapsed = self.started_at.elapsed().as_millis();
                        log::info!(
                            "[gst] [{}] PLAYING reached after {elapsed}ms; \
                             concat add-next looping armed; ready to finalize",
                            inner.clip_basename
                        );
                        self.state = PendingState::Ready;
                    } else if self.started_at.elapsed()
                        > std::time::Duration::from_secs(20)
                    {
                        return Err(anyhow!(
                            "[{}] timeout awaiting PLAYING \
                             (cur={cur:?} pending={pending:?})",
                            inner.clip_basename
                        ));
                    }
                }
                PendingState::Ready => {}
            }
            Ok(self.state)
        }

        /// Consume self into a fully-driven GstDecoder. Spawns
        /// the pull thread (per QA #2 fix: first-sample wait
        /// deferred -- latest_sample populated naturally once
        /// samples flow).
        ///
        /// Errors if state is not Ready. Idempotent panic-safe:
        /// inner.take() leaves None behind so a second call
        /// errors cleanly.
        pub fn finalize(mut self) -> Result<GstDecoder> {
            if self.state != PendingState::Ready {
                return Err(anyhow!(
                    "PendingDecoder::finalize called in state {:?}; \
                     poll until Ready first",
                    self.state
                ));
            }
            let mut inner = self
                .inner
                .take()
                .ok_or_else(|| anyhow!("PendingDecoder already finalized"))?;
            log::info!(
                "[gst] [{}] first-sample wait DEFERRED; spawning pull thread",
                inner.clip_basename
            );
            let appsink_for_pull = inner.appsink.clone();
            let slot_for_pull = inner.latest_sample.clone();
            let stop_for_pull = inner.stop.clone();
            let thread_name = format!("blendr-gst-pull-{}", inner.clip_basename);
            let pull_thread = thread::Builder::new()
                .name(thread_name)
                .spawn(move || pull_loop(appsink_for_pull, slot_for_pull, stop_for_pull))
                .context("spawn pull thread")?;
            inner.pull_thread = Some(pull_thread);
            Ok(inner)
        }
    }

    impl GstDecoder {
        /// Append a new sub-bin (same clip) to concat. cutloop's
        /// add_next_clip ported to Rust. Called at init for
        /// pre-queue + by process_pending() on each EOS.
        fn add_next_clip(&mut self) -> Result<()> {
            let serial = self.serial_counter.fetch_add(1, Ordering::Relaxed);

            let sub = gst::Bin::with_name(&format!("sub_{serial}"));
            let filesrc = gst::ElementFactory::make("filesrc")
                .property("location", self.clip_path.to_str().ok_or_else(|| {
                    anyhow!("clip path not UTF-8: {}", self.clip_path.display())
                })?)
                .build()
                .with_context(|| format!("make filesrc [{serial}]"))?;
            let qtdemux = gst::ElementFactory::make("qtdemux")
                .build()
                .with_context(|| format!("make qtdemux [{serial}]"))?;
            let h264parse = gst::ElementFactory::make("h264parse")
                .property("config-interval", -1i32)
                .build()
                .with_context(|| format!("make h264parse [{serial}]"))?;
            sub.add_many([&filesrc, &qtdemux, &h264parse])
                .with_context(|| format!("sub.add_many [{serial}]"))?;
            filesrc
                .link(&qtdemux)
                .with_context(|| format!("link filesrc->qtdemux [{serial}]"))?;

            // qtdemux pad-added: link to h264parse.sink
            // (mirrors cutloop:236-244). Store the handler id
            // so retire can disconnect; cutloop showed without
            // disconnect the closure leaks the sub-bin.
            let h264parse_for_pad = h264parse.clone();
            let pad_added_id = qtdemux.connect_pad_added(move |_demux, pad| {
                let caps = pad.current_caps().or_else(|| Some(pad.query_caps(None)));
                let caps_str = caps.map(|c| c.to_string()).unwrap_or_default();
                if !caps_str.starts_with("video/") {
                    return;
                }
                let sink_pad = match h264parse_for_pad.static_pad("sink") {
                    Some(p) => p,
                    None => return,
                };
                if sink_pad.is_linked() {
                    return;
                }
                if let Err(e) = pad.link(&sink_pad) {
                    log::warn!(
                        "[gst-cl] qtdemux pad link to h264parse failed: {e:?}"
                    );
                }
            });

            // Ghost h264parse.src up to sub.src.
            let h264_src = h264parse.static_pad("src").ok_or_else(|| {
                anyhow!("h264parse has no src pad [{serial}]")
            })?;
            let ghost = gst::GhostPad::with_target(&h264_src)
                .with_context(|| format!("GhostPad::with_target [{serial}]"))?;
            sub.add_pad(&ghost)
                .with_context(|| format!("sub.add_pad [{serial}]"))?;

            self.pipeline
                .add(&sub)
                .with_context(|| format!("pipeline.add(sub) [{serial}]"))?;

            let concat_sink = self
                .concat
                .request_pad_simple("sink_%u")
                .ok_or_else(|| anyhow!("concat.request_pad_simple [{serial}]"))?;
            ghost
                .link(&concat_sink)
                .map_err(|e| anyhow!("ghost->concat link [{serial}]: {e:?}"))?;

            // EOS probe on the CONCAT SINK PAD (not on the sub-
            // bin ghost; cutloop:275-303). When concat switches
            // AWAY from this pad to the next pending sink, the
            // absorbed EOS flows through this pad and we fire.
            // The probe sends commands to the channel; the
            // present thread processes via process_pending().
            //
            // BUG A v3 diagnostic: eos_fire_count is the per-
            // sub-bin counter. Logged each fire. Distinguishes
            // QA's two hypotheses:
            //   H1 (back-pressure): each sub-bin's probe fires
            //       EXACTLY ONCE but at wrong wall-clock time
            //       (~1.5s instead of ~4.75s). add/retire ratio
            //       matches sub-bin count.
            //   H2 (duplicate fire): per-sub-bin probe fires
            //       MULTIPLE times for the same actual EOS
            //       event (sticky-pad replay). add/retire ratio
            //       N:1 → runaway depth.
            let tx_for_probe = self.pipe_cmd_tx.clone();
            let serial_for_probe = serial;
            let basename_for_probe = self.clip_basename.clone();
            let eos_fire_count = Arc::new(AtomicU64::new(0));
            let eos_fire_count_for_probe = eos_fire_count.clone();
            // Measurement infra: stamp last_concat_add_at on
            // each EOS (= concat switch). Read by
            // outlier_context for [frame-long] flags.
            let concat_add_stamp = self.last_concat_add_at.clone();
            let probe_id = concat_sink.add_probe(
                gst::PadProbeType::EVENT_DOWNSTREAM,
                move |_pad, info| {
                    if let Some(gst::PadProbeData::Event(ev)) = info.data.as_ref() {
                        if ev.type_() == gst::EventType::Eos {
                            let n = eos_fire_count_for_probe
                                .fetch_add(1, Ordering::Relaxed)
                                + 1;
                            log::info!(
                                "[gst-cl] {basename_for_probe} concat-sink EOS \
                                 serial={serial_for_probe} fire_count={n} \
                                 -> queue add+retire"
                            );
                            if let Ok(mut g) = concat_add_stamp.lock() {
                                *g = Some(std::time::Instant::now());
                            }
                            let _ = tx_for_probe.send(PipelineCmd::AddNextClip);
                            let _ = tx_for_probe.send(PipelineCmd::Retire(serial_for_probe));
                        }
                    }
                    gst::PadProbeReturn::Ok
                },
            );

            sub.sync_state_with_parent()
                .with_context(|| format!("sub.sync_state [{serial}]"))?;

            let depth = {
                let mut q = self.sub_bins.lock().unwrap();
                q.push_back(SubBin {
                    serial,
                    bin: sub,
                    qtdemux,
                    pad_added_id: Some(pad_added_id),
                    concat_sink,
                    // probe_id is already Option<PadProbeId> in
                    // gst-rs 0.23 (add_probe returns Option for
                    // failure cases).
                    probe_id,
                    eos_fire_count,
                });
                q.len()
            };
            log::info!(
                "[gst-cl] {} ADD sub-bin serial={serial} (queue_depth={depth})",
                self.clip_basename
            );
            Ok(())
        }

        /// Tear down the sub-bin with the matching serial.
        /// Disconnect handler + remove probe FIRST (releases
        /// closures), then NULL + remove + release concat pad
        /// (cutloop's proven leak-safe order, :314-379).
        fn retire_subgraph(&mut self, serial: u64) -> Result<()> {
            // VecDeque::remove(idx) returns Option<SubBin>; unwrap to
            // the inner struct or bail if not found.
            let mut sub_bin: SubBin = {
                let mut q = self.sub_bins.lock().unwrap();
                match q.iter().position(|s| s.serial == serial) {
                    Some(idx) => q.remove(idx).expect("position found ensures Some"),
                    None => {
                        log::warn!(
                            "[gst-cl] {} retire serial={serial} not found",
                            self.clip_basename
                        );
                        return Ok(());
                    }
                }
            };
            if let Some(id) = sub_bin.pad_added_id.take() {
                sub_bin.qtdemux.disconnect(id);
            }
            // probe_id may already have been consumed by remove_probe
            // returning Some; take() to be safe.
            let probe_owned = sub_bin.probe_id.take();
            if let Some(pid) = probe_owned {
                sub_bin.concat_sink.remove_probe(pid);
            }
            let _ = sub_bin.bin.set_state(gst::State::Null);
            let _ = self.pipeline.remove(&sub_bin.bin);
            self.concat.release_request_pad(&sub_bin.concat_sink);
            let depth_after = self.sub_bins.lock().unwrap().len();
            let fired = sub_bin.eos_fire_count.load(Ordering::Relaxed);
            log::info!(
                "[gst-cl] {} RETIRE serial={serial} fired={fired} \
                 (queue_depth_after={depth_after})",
                self.clip_basename
            );
            Ok(())
        }

        /// Drain pending pipeline commands from EOS probes.
        /// Called by kms::run_loop per iteration on the present
        /// thread (where pipeline mutations happen safely;
        /// cutloop's GLib.idle_add equivalent).
        pub fn process_pending(&mut self) -> Result<()> {
            while let Ok(cmd) = self.pipe_cmd_rx.try_recv() {
                match cmd {
                    PipelineCmd::AddNextClip => {
                        if let Err(e) = self.add_next_clip() {
                            log::warn!(
                                "[gst-cl] {} add_next_clip failed: {e:#}",
                                self.clip_basename
                            );
                        }
                    }
                    PipelineCmd::Retire(serial) => {
                        if let Err(e) = self.retire_subgraph(serial) {
                            log::warn!(
                                "[gst-cl] {} retire serial={serial} failed: {e:#}",
                                self.clip_basename
                            );
                        }
                    }
                }
            }
            Ok(())
        }

        /// Take latest sample (if any) and map to GL texture id.
        /// If no new sample, reuse cached current_frame.
        pub fn latest_texture(&mut self) -> Result<Option<(u32, TexTarget)>> {
            let new_sample = {
                let mut guard = self.latest_sample.lock().map_err(|p| {
                    anyhow!("latest_sample lock poisoned: {p}")
                })?;
                guard.take()
            };
            if let Some(sample) = new_sample {
                let tex_id = self.adopt_sample(sample)?;
                return Ok(Some((tex_id, self.tex_target)));
            }
            if let Some(frame) = self.current_frame.as_ref() {
                let tex_id = frame
                    .texture_id(0)
                    .map_err(|e| anyhow!("cached texture_id: {e:?}"))?;
                return Ok(Some((tex_id, self.tex_target)));
            }
            Ok(None)
        }

        fn adopt_sample(&mut self, sample: gst::Sample) -> Result<u32> {
            let buffer = sample
                .buffer_owned()
                .ok_or_else(|| anyhow!("sample has no buffer"))?;
            self.last_pts_ns = buffer.pts().map(|t| t.nseconds());
            let caps = sample
                .caps()
                .ok_or_else(|| anyhow!("sample has no caps"))?;

            let queried_target = buffer
                .peek_memory(0)
                .downcast_memory_ref::<gst_gl::GLMemory>()
                .map(|m| m.texture_target());
            let real_tex_target = match queried_target {
                Some(gst_gl::GLTextureTarget::ExternalOes) => TexTarget::External,
                Some(_) => TexTarget::TwoD,
                None => self.tex_target,
            };
            if self.current_frame.is_none() {
                log::info!(
                    "[gst] {} tex_target query: glmemory_actual={:?} -> {:?}",
                    self.clip_basename, queried_target, real_tex_target
                );
            } else if real_tex_target != self.tex_target {
                log::warn!(
                    "[gst] {} tex_target CHANGED mid-stream: was {:?} now {:?}",
                    self.clip_basename, self.tex_target, real_tex_target
                );
            }
            self.tex_target = real_tex_target;

            let video_info = gst_video::VideoInfo::from_caps(&caps)
                .map_err(|e| anyhow!("VideoInfo::from_caps: {e:?}"))?;
            let new_frame =
                gst_gl::GLVideoFrame::from_buffer_readable(buffer, &video_info)
                    .map_err(|_| {
                        anyhow!(
                            "GLVideoFrame::from_buffer_readable failed"
                        )
                    })?;
            let tex_id = new_frame.texture_id(0).map_err(|e| {
                anyhow!("GLVideoFrame::texture_id(0): {e:?}")
            })?;
            self.current_frame = Some(new_frame);
            Ok(tex_id)
        }
    }

    fn pull_loop(
        appsink: gst_app::AppSink,
        slot: Arc<Mutex<Option<gst::Sample>>>,
        stop: Arc<AtomicBool>,
    ) {
        log::info!("[gst-pull] thread up");
        while !stop.load(Ordering::Relaxed) {
            let sample =
                appsink.try_pull_sample(gst::ClockTime::from_mseconds(PULL_TICK_MS));
            if let Some(s) = sample {
                if let Ok(mut g) = slot.lock() {
                    *g = Some(s);
                }
            }
        }
        log::info!("[gst-pull] stop flag set; exiting");
    }

    impl Drop for GstDecoder {
        fn drop(&mut self) {
            // BUG B step-localization preserved. New steps for
            // sub-bin retire (concat path).
            // Clone basename out so the later self.retire_subgraph
            // calls don't borrow-conflict with the log strings.
            let name = self.clip_basename.clone();

            log::info!("[gst-drop] {name} step=1 set stop flag");
            self.stop.store(true, Ordering::Relaxed);

            // BUG C (Phase 3 v2 prereq): the back-pressure fix
            // (appsink set_drop(false) in BUG A v3) blocks the
            // streaming thread upstream when appsink is full.
            // At teardown, after stop flag is set, the pull
            // thread stops pulling -> appsink fills -> streaming
            // thread parks in blocking push -> set_state(NULL)
            // at step=6 deadlocks (no thread to handle the
            // transition). Symptom (QA 3x reproduce on 66e8fa2):
            // RuntimeMaxSec timeout, ExecMainStatus=9.
            //
            // FIX: send FLUSH_START on the pipeline BEFORE join.
            // FlushStart propagates upstream + returns blocked
            // src-pad pushes with FLOW_FLUSHING; streaming
            // thread can return, observe stop intent, and let
            // NULL transition proceed. We don't send FlushStop
            // because we're going to NULL, not resuming.
            //
            // CRITICAL for Phase 3 v2: retire+recreate runs
            // this Drop on EVERY clip change. Without FlushStart
            // here, every retire would hang the slideshow.
            log::info!("[gst-drop] {name} step=1.5 sending FLUSH_START");
            self.pipeline
                .send_event(gst::event::FlushStart::new());

            log::info!("[gst-drop] {name} step=2 joining pull thread");
            if let Some(h) = self.pull_thread.take() {
                let _ = h.join();
            }
            log::info!("[gst-drop] {name} step=3 joined; clearing slot");

            if let Ok(mut g) = self.latest_sample.lock() {
                *g = None;
            }
            log::info!(
                "[gst-drop] {name} step=4 slot cleared; clearing current_frame"
            );

            self.current_frame = None;
            log::info!(
                "[gst-drop] {name} step=5 current_frame cleared; retiring sub-bins"
            );

            // Retire all sub-bins (disconnect handlers + remove
            // probes + NULL each + release concat pads). Drains
            // the deque.
            let serials: Vec<u64> = self
                .sub_bins
                .lock()
                .unwrap()
                .iter()
                .map(|s| s.serial)
                .collect();
            for s in serials {
                let _ = self.retire_subgraph(s);
            }
            log::info!(
                "[gst-drop] {name} step=6 sub-bins retired; set_state(NULL)"
            );

            let _ = self.pipeline.set_state(gst::State::Null);
            log::info!(
                "[gst-drop] {name} step=7 set_state(NULL) returned; awaiting transition"
            );
            let (_res, cur, _pending) =
                self.pipeline.state(gst::ClockTime::from_seconds(5));
            log::info!(
                "[gst-drop] {name} step=8 state-wait done cur={cur:?}; unsetting bus"
            );
            if cur != gst::State::Null {
                log::warn!(
                    "[gst] pipeline did not reach NULL on Drop (cur={cur:?})"
                );
            }

            if let Some(bus) = self.pipeline.bus() {
                bus.unset_sync_handler();
            }
            log::info!("[gst-drop] {name} step=9 bus unset; Drop returning");
        }
    }
}
