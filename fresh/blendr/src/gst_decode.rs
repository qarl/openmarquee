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
        AwaitingWorker,
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
        pub fn last_was_new_sample(&self) -> bool {
            false
        }
        pub fn process_pending(&mut self) -> Result<()> {
            Ok(())
        }
        pub fn switch_to_clip(&mut self, _new_clip: &std::path::Path) -> Result<()> {
            anyhow::bail!("switch_to_clip: Linux only")
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
            PendingState::AwaitingWorker
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
        // Shared with cadence worker. switch_to_clip mutates
        // via .lock() (present thread). Cadence worker reads
        // via .lock() at each add_next_clip_inner. Eventually-
        // consistent semantics (race between switch and an
        // in-flight add resolves via next add reading NEW;
        // same as lazy-switch convention).
        clip_path: Arc<Mutex<PathBuf>>,
        /// Live sub-bins queue (FIFO; front = active source
        /// concat is currently consuming).
        // Shared with cadence worker. The worker holds Arc
        // clones and locks per-op (add/retire). Drop owner is
        // GstDecoder; cadence worker exits before Drop's
        // retire-all so no contention at teardown.
        sub_bins: Arc<Mutex<VecDeque<SubBin>>>,
        /// Monotonic sub-bin serial for EOS-probe identification.
        // Shared with cadence worker (fetch_add per
        // add_next_clip_inner call).
        serial_counter: Arc<AtomicU64>,
        /// EOS probes send commands here from gst streaming
        /// threads. process_pending() drains on the present
        /// thread.
        // Sender stays on GstDecoder so EOS-probe closures
        // (created inside add_next_clip_inner) can clone +
        // capture. Receiver moved INTO the cadence worker
        // (see cadence_thread). process_pending is now a
        // no-op since the worker drains the rx directly.
        pipe_cmd_tx: mpsc::Sender<PipelineCmd>,
        #[allow(dead_code)]
        gst_display: gst_gl::GLDisplay,
        #[allow(dead_code)]
        gst_context: gst_gl::GLContext,
        /// Dedicated EGL share-group context that gst-gl
        /// wraps (instead of the presenter context). gst-gl's
        /// streaming threads eglMakeCurrent THIS context for
        /// glupload/GLBufferPool work -- they never touch the
        /// presenter context or window surface, eliminating
        /// the black-flash root cause (all-cheap-fixes-refuted
        /// black during create).
        ///
        /// Textures created on this context are visible from
        /// the presenter context via share-group semantics.
        ///
        /// Dropped LAST in the Drop sequence (after pipeline
        /// NULL + bus unset so gst-gl has released its hold).
        /// ShareGroupContext::Drop destroys the EGL context +
        /// pbuffer surface.
        #[allow(dead_code)]
        share_group: crate::egl_gbm::ShareGroupContext,
        /// Per-frame trace probe state: whether the most
        /// recent latest_texture() returned a NEW sample
        /// (adopted from the pull-thread slot) vs reused the
        /// cached current_frame (= decoder starvation: no new
        /// frame delivered this tick). Read by FrameTrace via
        /// last_was_new_sample(). False if no sample has been
        /// pulled yet (initial startup window).
        last_was_new_sample: bool,
        /// Long-lived cadence worker handle. Worker owns
        /// pipe_cmd_rx + holds Arc clones of sub_bins/
        /// clip_path/serial_counter/etc; sequentially
        /// processes AddNextClip and Retire commands from
        /// EOS probes off the present thread. Drop signals
        /// cadence_stop + joins before existing teardown.
        cadence_thread: Option<JoinHandle<()>>,
        /// Stop flag for cadence worker. Drop sets true; the
        /// worker's recv loop checks each iter (recv_timeout
        /// short tick) and exits when set.
        cadence_stop: Arc<AtomicBool>,
    }

    impl GstDecoder {
        pub fn last_pts_ns(&self) -> Option<u64> {
            self.last_pts_ns
        }

        /// Per-frame trace probe accessor. true iff the most
        /// recent latest_texture() call adopted a NEW sample
        /// from the pull-thread slot. false iff the cached
        /// current_frame was reused (= no new frame delivered
        /// this tick = decoder starvation). Used by FrameTrace
        /// to surface qarl's "1s into new clip, frame sticks
        /// for 50-125ms" symptom.
        pub fn last_was_new_sample(&self) -> bool {
            self.last_was_new_sample
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
        /// (main.rs initial decoders, Streams::Single). Calls
        /// PendingDecoder::start_async + tight poll loop with
        /// 10ms sleep until Ready. Uses the same worker-thread
        /// + share-group architecture as mid-run cycles so all
        /// gst-gl GL ops are isolated from the presenter
        /// context regardless of when the decoder is created.
        pub fn new(egl: &Egl, clip: &Path) -> Result<Self> {
            // Startup paths (no outgoing decoder to drop).
            let mut p = PendingDecoder::start_async(egl, clip, None)?;
            let timeout = std::time::Duration::from_secs(30);
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
    /// Black-flash fix: gst-gl's GL work (set_state PAUSED/
    /// PLAYING preroll + glupload negotiation) now runs on a
    /// WORKER THREAD holding a dedicated share-group EGL
    /// context. The present thread sees only:
    ///   start_async (~ms; create share-group context + spawn
    ///                worker)
    ///   poll        (~µs; non-blocking mpsc try_recv)
    ///   finalize    (~ns; move the worker-delivered
    ///                GstDecoder out)
    ///
    /// Pre-fix history:
    ///   - #2 (78bcdaf): off-present-thread retire.
    ///   - #2-alt (4df9b2a): replace pipeline.state(5s/10s)
    ///     waits with non-blocking poll on present thread.
    ///     RESULT: traded ~600ms freeze for ~160-500ms black
    ///     flash (QA proof: solid-black PNG mid-create).
    ///   - 96e6c78 + d917239: cheap fixes (EGL steal repair,
    ///     FBO rebind) -- ALL REFUTED (egl_steals=0,
    ///     fbo_dirty=0). gst-gl was doing actual draw/clear
    ///     ops on OUR window surface during its glupload
    ///     negotiation, not corrupting bindings we could
    ///     re-assert.
    ///   - THIS (worker + share-group): full isolation. Worker
    ///     holds share-group context; gst-gl streaming threads
    ///     eglMakeCurrent THAT context; presenter context +
    ///     window surface never touched.
    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub enum PendingState {
        /// Worker thread is building the decoder (pipeline +
        /// PAUSED preroll + PLAYING transition + pull-thread
        /// spawn). All gst-gl work happens on the worker's
        /// share-group context, not the presenter's.
        AwaitingWorker,
        /// Worker has delivered the finished GstDecoder via
        /// the mpsc channel; finalize() can move it out.
        Ready,
    }

    pub struct PendingDecoder {
        state: PendingState,
        rx: mpsc::Receiver<Result<GstDecoder>>,
        completed: Option<GstDecoder>,
        started_at: std::time::Instant,
        clip_basename: String,
    }

    impl PendingDecoder {
        pub fn state(&self) -> PendingState {
            self.state
        }

        /// Spawn a worker thread that:
        ///   (1) optionally drops a `pre_drop` GstDecoder
        ///       (the OUTGOING slot's decoder being retired
        ///       this cycle); and
        ///   (2) calls build_on_worker for the new decoder.
        ///
        /// Doing both on the SAME worker sequentially is the
        /// pattern-(a) fix per QA c012339 analysis: the
        /// outgoing retire (drop) and the new create were
        /// previously on DIFFERENT worker threads → wall-
        /// clock overlap → both contending for vc4 GPU
        /// (share_group_ctx 19→144-192ms) and HW
        /// /dev/video10 (paused_reached 151→297ms). With
        /// serialization, drop completes BEFORE create
        /// starts → no contention → create stays at baseline
        /// ~150ms. Total hold window shrinks from 340-813ms
        /// to ~200-300ms typical.
        ///
        /// Present thread still does ZERO EGL/GL calls in
        /// this function (just clones the seed + moves
        /// pre_drop into the worker).
        ///
        /// pre_drop = None for startup paths (sync new
        /// wrapper); Some(old_decoder) for mid-run Cycle
        /// retire+create.
        pub fn start_async(
            egl: &Egl,
            clip: &Path,
            pre_drop: Option<GstDecoder>,
        ) -> Result<Self> {
            let clip_basename = clip
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("?")
                .to_string();
            let clip_path = clip.to_path_buf();

            // Send-able seed for the worker to call
            // eglCreateContext on its own thread. ZERO EGL
            // calls on the present thread.
            let seed = egl.share_group_seed();

            let (tx, rx) = mpsc::channel::<Result<GstDecoder>>();
            let basename_for_worker = clip_basename.clone();
            let has_pre_drop = pre_drop.is_some();
            std::thread::Builder::new()
                .name(format!("blendr-pending-{clip_basename}"))
                .spawn(move || {
                    // (1) Pre-drop the outgoing decoder
                    //     SERIALLY on this worker (the fix).
                    //     Trace SPAWN/DONE so QA can see the
                    //     drop's wall-clock cost + confirm
                    //     it now precedes (doesn't overlap)
                    //     the build.
                    if let Some(old) = pre_drop {
                        let retire_id = crate::kms::trace_ts_ms();
                        let t_spawn = std::time::Instant::now();
                        if crate::kms::trace_enabled() {
                            crate::kms::trace_writeln(&format!(
                                "[trace-retire] ts_ms={} id={retire_id} \
                                 phase=SPAWN serialized=true",
                                crate::kms::trace_ts_ms()
                            ));
                        }
                        drop(old);
                        if crate::kms::trace_enabled() {
                            let drop_ms = t_spawn.elapsed().as_millis();
                            crate::kms::trace_writeln(&format!(
                                "[trace-retire] ts_ms={} id={retire_id} \
                                 phase=DONE drop_ms={drop_ms} serialized=true",
                                crate::kms::trace_ts_ms()
                            ));
                        }
                    }
                    // (2) Build new decoder. With pre_drop
                    //     complete, the GPU + HW decoder have
                    //     no retire contention.
                    let r = build_on_worker(
                        seed,
                        clip_path,
                        basename_for_worker,
                    );
                    let _ = tx.send(r);
                })
                .context("spawn PendingDecoder worker")?;

            log::info!(
                "[gst] [{clip_basename}] worker spawned \
                 (pre_drop={has_pre_drop}; serialized retire-then-create); \
                 present thread unblocked"
            );
            Ok(PendingDecoder {
                state: PendingState::AwaitingWorker,
                rx,
                completed: None,
                started_at: std::time::Instant::now(),
                clip_basename,
            })
        }

        /// Non-blocking check whether the worker has delivered
        /// the GstDecoder. Per-call cost: one mpsc try_recv
        /// (~ns). When Ready, the caller calls finalize() to
        /// take the GstDecoder out.
        pub fn poll(&mut self) -> Result<PendingState> {
            if self.state == PendingState::Ready {
                return Ok(self.state);
            }
            match self.rx.try_recv() {
                Ok(Ok(dec)) => {
                    let elapsed = self.started_at.elapsed().as_millis();
                    log::info!(
                        "[gst] [{}] worker delivered GstDecoder after {elapsed}ms",
                        self.clip_basename
                    );
                    self.completed = Some(dec);
                    self.state = PendingState::Ready;
                }
                Ok(Err(e)) => {
                    let elapsed = self.started_at.elapsed().as_millis();
                    log::error!(
                        "[gst] [{}] worker FAILED after {elapsed}ms: {e:#}",
                        self.clip_basename
                    );
                    return Err(e);
                }
                Err(mpsc::TryRecvError::Empty) => {}
                Err(mpsc::TryRecvError::Disconnected) => {
                    return Err(anyhow!(
                        "[{}] PendingDecoder worker disconnected without \
                         sending (panic?)",
                        self.clip_basename
                    ));
                }
            }
            Ok(self.state)
        }

        /// Move the completed GstDecoder out. Errors if poll()
        /// hasn't returned Ready yet.
        pub fn finalize(mut self) -> Result<GstDecoder> {
            if self.state != PendingState::Ready {
                return Err(anyhow!(
                    "PendingDecoder::finalize called in state {:?}; \
                     poll until Ready first",
                    self.state
                ));
            }
            self.completed.take().ok_or_else(|| {
                anyhow!(
                    "[{}] PendingDecoder already finalized",
                    self.clip_basename
                )
            })
        }
    }

    /// Build the GstDecoder on a worker thread, isolated from
    /// the presenter context. Runs entirely on the spawning
    /// worker; gst-gl's streaming threads use the share-group
    /// context for their own work. When this returns Ok, the
    /// GstDecoder is fully-prerolled and the pull thread is
    /// running.
    ///
    /// The worker thread itself releases its hold on the
    /// share-group context (eglMakeCurrent EGL_NO_CONTEXT)
    /// before returning so subsequent gst-gl operations on
    /// their streaming threads can take it freely (EGL
    /// single-thread-current semantics).
    fn build_on_worker(
        seed: crate::egl_gbm::ShareGroupSeed,
        clip: PathBuf,
        clip_basename: String,
    ) -> Result<GstDecoder> {
        // Per-phase trace timing (BLENDR_FRAME_TRACE=1): each
        // phase emits a [trace-create] line with ts_ms (since
        // trace_init) + elapsed_ms (within this create). QA
        // correlates ts_ms across creates + retires to identify
        // HW decoder contention windows.
        let t_start = std::time::Instant::now();
        let create_id = crate::kms::trace_ts_ms(); // unique per-create id; just the ts
        let log_phase = |phase: &str, t_phase_start: std::time::Instant| {
            if !crate::kms::trace_enabled() { return; }
            let phase_ms = t_phase_start.elapsed().as_millis();
            let total_ms = t_start.elapsed().as_millis();
            crate::kms::trace_writeln(&format!(
                "[trace-create] ts_ms={} id={create_id} clip={clip_basename} \
                 phase={phase} phase_ms={phase_ms} total_ms={total_ms}",
                crate::kms::trace_ts_ms()
            ));
        };
        if crate::kms::trace_enabled() {
            crate::kms::trace_writeln(&format!(
                "[trace-create] ts_ms={} id={create_id} clip={clip_basename} \
                 phase=START",
                crate::kms::trace_ts_ms()
            ));
        }

        let t_phase = std::time::Instant::now();
        gst::init().context("gst::init (worker)")?;
        log_phase("gst_init", t_phase);

        // (a.1) Create the share-group EGL context on THIS
        //       worker thread (NOT on present). eglCreate-
        //       Context with share_context=presenter triggers
        //       vc4/Mesa share-group setup that disrupts the
        //       currently-current context's rendering -- if we
        //       did this on present, it would black our screen
        //       for ~160ms (QA proof at bbf4daa). On the
        //       worker, it just disrupts the worker's nothing.
        let t_phase = std::time::Instant::now();
        let share_group = crate::egl_gbm::ShareGroupContext::create_in_share_group(
            seed.lib,
            seed.display,
            seed.config,
            seed.parent_context,
        )
        .context("create share-group context on worker")?;
        log_phase("share_group_ctx", t_phase);

        // (a.2) Make the share-group context current on THIS
        //       worker thread so the GstGLContext::new_wrapped
        //       call below sees an active context for fill_info.
        let t_phase = std::time::Instant::now();
        share_group
            .make_current_here()
            .context("share-group make_current on worker")?;
        log_phase("share_group_make_current", t_phase);

        // (b) Wrap the SHARE context (not presenter's) for
        //     gst-gl. All subsequent gst-gl operations route
        //     through THIS context; the presenter context is
        //     untouched.
        let egl_display_ptr = share_group.display_handle_as_usize();
        let gst_display: gst_gl::GLDisplay = unsafe {
            gst_gl_egl::GLDisplayEGL::with_egl_display(egl_display_ptr)
                .map_err(|e| anyhow!("GLDisplayEGL::with_egl_display: {e}"))?
        }
        .upcast();
        let egl_ctx_handle = share_group.context_handle_as_usize();
        let gst_context: gst_gl::GLContext = unsafe {
            gst_gl::GLContext::new_wrapped(
                &gst_display,
                egl_ctx_handle,
                gst_gl::GLPlatform::EGL,
                gst_gl::GLAPI::GLES2,
            )
        }
        .ok_or_else(|| anyhow!("GLContext::new_wrapped(share-group) returned None"))?;
        let t_phase = std::time::Instant::now();
        gst_context
            .activate(true)
            .map_err(|e| anyhow!("gst_context.activate(true): {e}"))?;
        gst_context
            .fill_info()
            .map_err(|e| anyhow!("gst_context.fill_info: {e}"))?;
        gst_context
            .activate(false)
            .map_err(|e| anyhow!("gst_context.activate(false): {e}"))?;
        log_phase("gst_gl_wrap", t_phase);
        let t_phase = std::time::Instant::now();

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
            //     get filled here; sub_bins starts empty. The
            //     cadence_thread is spawned LATER (after the
            //     pipeline reaches PLAYING) so the worker
            //     starts draining the rx only when commands
            //     might actually arrive.
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
                clip_path: Arc::new(Mutex::new(clip)),
                sub_bins: Arc::new(Mutex::new(VecDeque::new())),
                serial_counter: Arc::new(AtomicU64::new(0)),
                pipe_cmd_tx,
                gst_display,
                gst_context,
                share_group,
                last_was_new_sample: false,
                cadence_thread: None,
                cadence_stop: Arc::new(AtomicBool::new(false)),
            };
            // Stash pipe_cmd_rx locally; the cadence worker
            // will receive it via move at spawn time below.
            let pipe_cmd_rx_for_cadence = pipe_cmd_rx;
            log_phase("pipeline_build", t_phase);

            // (e) Pre-queue INITIAL_QUEUE_DEPTH sub-bins. concat
            //     plays the first; the rest are pending.
            let t_phase = std::time::Instant::now();
            for _ in 0..INITIAL_QUEUE_DEPTH {
                me.add_next_clip()?;
            }
            log_phase("pre_queue", t_phase);

            // (f) State -> PAUSED + wait, then PLAYING + wait.
            //     Both waits BLOCK the worker thread; that's
            //     fine because we ARE the worker. Present
            //     thread keeps drawing uninterrupted via its
            //     OWN context (presenter context; gst-gl
            //     never touches it now that we wrap the
            //     share-group context instead).
            //
            //     The 5s + 10s timeouts mirror the original
            //     pre-#2-alt sync new(). Typical wall-clock
            //     to PLAYING is ~140-500ms per QA history.
            log::info!("[gst] [{clip_basename}] (worker) set_state(PAUSED)");
            let t_phase = std::time::Instant::now();
            let preroll_ret = me
                .pipeline
                .set_state(gst::State::Paused)
                .map_err(|e| anyhow!("set_state(PAUSED): {e:?}"))?;
            log::info!(
                "[gst] [{clip_basename}] (worker) set_state(PAUSED) -> {preroll_ret:?}"
            );
            log_phase("paused_set", t_phase);

            let t_phase = std::time::Instant::now();
            let (wait_res, cur, pending) =
                me.pipeline.state(gst::ClockTime::from_seconds(5));
            log::info!(
                "[gst] [{clip_basename}] (worker) state-wait PAUSED: \
                 {wait_res:?} cur={cur:?} pending={pending:?}"
            );
            log_phase("paused_reached", t_phase);
            if cur != gst::State::Paused {
                return Err(anyhow!(
                    "(worker) preroll did not settle to PAUSED \
                     (cur={cur:?}, pending={pending:?})"
                ));
            }

            log::info!("[gst] [{clip_basename}] (worker) set_state(PLAYING)");
            let t_phase = std::time::Instant::now();
            let play_ret = me
                .pipeline
                .set_state(gst::State::Playing)
                .map_err(|e| anyhow!("set_state PLAYING: {e:?}"))?;
            log::info!(
                "[gst] [{clip_basename}] (worker) set_state(PLAYING) -> {play_ret:?}"
            );
            log_phase("playing_set", t_phase);

            let t_phase = std::time::Instant::now();
            let (wait_res, cur, pending) =
                me.pipeline.state(gst::ClockTime::from_seconds(10));
            log::info!(
                "[gst] [{clip_basename}] (worker) state-wait PLAYING: \
                 {wait_res:?} cur={cur:?} pending={pending:?}"
            );
            log_phase("playing_reached", t_phase);
            if cur != gst::State::Playing {
                return Err(anyhow!(
                    "(worker) pipeline did NOT reach PLAYING within 10s \
                     (cur={cur:?} pending={pending:?})"
                ));
            }
            log::info!(
                "[gst] [{clip_basename}] (worker) PLAYING confirmed; \
                 concat add-next looping armed"
            );

            // (g) Spawn pull thread (per #2: first-sample wait
            //     deferred -- pull thread populates
            //     latest_sample naturally).
            log::info!(
                "[gst] [{clip_basename}] (worker) first-sample wait DEFERRED; \
                 spawning pull thread"
            );
            let appsink_for_pull = me.appsink.clone();
            let slot_for_pull = me.latest_sample.clone();
            let stop_for_pull = me.stop.clone();
            let thread_name = format!("blendr-gst-pull-{}", me.clip_basename);
            let pull_thread = thread::Builder::new()
                .name(thread_name)
                .spawn(move || pull_loop(appsink_for_pull, slot_for_pull, stop_for_pull))
                .context("spawn pull thread")?;
            me.pull_thread = Some(pull_thread);

            // (g.2) Spawn the long-lived cadence worker. Takes
            //       ownership of pipe_cmd_rx + Arc clones of
            //       sub_bins/clip_path/serial_counter/pipe_
            //       cmd_tx/last_concat_add_at + pipeline +
            //       concat. Processes EOS-probe-fired
            //       AddNextClip/Retire commands off the
            //       present thread (the ~33-80ms blocking
            //       sync_state_with_parent goes here).
            log::info!(
                "[gst] [{clip_basename}] (worker) spawning cadence worker"
            );
            let cadence_stop_for_worker = me.cadence_stop.clone();
            let pipeline_for_cadence = me.pipeline.clone();
            let concat_for_cadence = me.concat.clone();
            let sub_bins_for_cadence = me.sub_bins.clone();
            let clip_path_for_cadence = me.clip_path.clone();
            let serial_counter_for_cadence = me.serial_counter.clone();
            let pipe_cmd_tx_for_cadence = me.pipe_cmd_tx.clone();
            let last_concat_add_at_for_cadence =
                me.last_concat_add_at.clone();
            let basename_for_cadence = me.clip_basename.clone();
            let cadence_thread_name =
                format!("blendr-gst-cadence-{}", me.clip_basename);
            let cadence_handle = thread::Builder::new()
                .name(cadence_thread_name)
                .spawn(move || {
                    cadence_loop(
                        pipe_cmd_rx_for_cadence,
                        cadence_stop_for_worker,
                        pipeline_for_cadence,
                        concat_for_cadence,
                        sub_bins_for_cadence,
                        clip_path_for_cadence,
                        serial_counter_for_cadence,
                        pipe_cmd_tx_for_cadence,
                        last_concat_add_at_for_cadence,
                        basename_for_cadence,
                    );
                })
                .context("spawn cadence worker")?;
            me.cadence_thread = Some(cadence_handle);

            // (h) Release the share-group context on THIS
            //     worker thread so gst-gl's streaming threads
            //     (and future GstDecoder Drops on the present
            //     thread) can eglMakeCurrent it freely (EGL
            //     single-thread-current). The wrapped context
            //     itself is alive until ShareGroupContext::
            //     Drop runs (after pipeline NULL during
            //     GstDecoder::Drop).
            me.share_group.release_here();

            if crate::kms::trace_enabled() {
                let total_ms = t_start.elapsed().as_millis();
                crate::kms::trace_writeln(&format!(
                    "[trace-create] ts_ms={} id={create_id} clip={clip_basename} \
                     phase=DONE total_ms={total_ms}",
                    crate::kms::trace_ts_ms()
                ));
            }

            Ok(me)
        }

    /// Free-function add_next_clip used by BOTH the
    /// build_on_worker pre-queue (synchronous on the build
    /// worker) AND the long-lived cadence worker (per-slot
    /// thread that drains pipe_cmd_rx). Operates on
    /// Arc-wrapped state so it can run on any thread.
    fn add_next_clip_inner(
        pipeline: &gst::Pipeline,
        concat: &gst::Element,
        sub_bins: &Arc<Mutex<VecDeque<SubBin>>>,
        clip_path: &Arc<Mutex<PathBuf>>,
        serial_counter: &Arc<AtomicU64>,
        pipe_cmd_tx: &mpsc::Sender<PipelineCmd>,
        last_concat_add_at: &Arc<Mutex<Option<std::time::Instant>>>,
    ) -> Result<()> {
        let serial = serial_counter.fetch_add(1, Ordering::Relaxed);
        // Snapshot the current clip_path under lock; release
        // before doing any blocking gst ops (lock-hold-narrow).
        let (clip_path_buf, clip_basename) = {
            let g = clip_path.lock().map_err(|p| {
                anyhow!("clip_path lock poisoned: {p}")
            })?;
            let pb = g.clone();
            let bn = pb
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("?")
                .to_string();
            (pb, bn)
        };

        let sub = gst::Bin::with_name(&format!("sub_{serial}"));
        let filesrc = gst::ElementFactory::make("filesrc")
            .property("location", clip_path_buf.to_str().ok_or_else(|| {
                anyhow!("clip path not UTF-8: {}", clip_path_buf.display())
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

        let h264_src = h264parse.static_pad("src").ok_or_else(|| {
            anyhow!("h264parse has no src pad [{serial}]")
        })?;
        let ghost = gst::GhostPad::with_target(&h264_src)
            .with_context(|| format!("GhostPad::with_target [{serial}]"))?;
        sub.add_pad(&ghost)
            .with_context(|| format!("sub.add_pad [{serial}]"))?;

        pipeline
            .add(&sub)
            .with_context(|| format!("pipeline.add(sub) [{serial}]"))?;

        let concat_sink = concat
            .request_pad_simple("sink_%u")
            .ok_or_else(|| anyhow!("concat.request_pad_simple [{serial}]"))?;
        ghost
            .link(&concat_sink)
            .map_err(|e| anyhow!("ghost->concat link [{serial}]: {e:?}"))?;

        let tx_for_probe = pipe_cmd_tx.clone();
        let serial_for_probe = serial;
        let basename_for_probe = clip_basename.clone();
        let eos_fire_count = Arc::new(AtomicU64::new(0));
        let eos_fire_count_for_probe = eos_fire_count.clone();
        let concat_add_stamp = last_concat_add_at.clone();
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

        // sync_state_with_parent: blocks until sub-bin reaches
        // parent's state. This is the ~33-80ms cost that we
        // moved off the present thread (cadence worker handles
        // it now; build_on_worker also OK since it's the
        // build worker).
        sub.sync_state_with_parent()
            .with_context(|| format!("sub.sync_state [{serial}]"))?;

        let depth = {
            let mut q = sub_bins.lock().unwrap();
            q.push_back(SubBin {
                serial,
                bin: sub,
                qtdemux,
                pad_added_id: Some(pad_added_id),
                concat_sink,
                probe_id,
                eos_fire_count,
            });
            q.len()
        };
        log::info!(
            "[gst-cl] {clip_basename} ADD sub-bin serial={serial} \
             (queue_depth={depth})"
        );
        Ok(())
    }

    /// Free-function retire_subgraph (cadence-worker-safe).
    fn retire_subgraph_inner(
        pipeline: &gst::Pipeline,
        concat: &gst::Element,
        sub_bins: &Arc<Mutex<VecDeque<SubBin>>>,
        serial: u64,
    ) -> Result<()> {
        let mut sub_bin: SubBin = {
            let mut q = sub_bins.lock().unwrap();
            match q.iter().position(|s| s.serial == serial) {
                Some(idx) => q.remove(idx).expect("position found ensures Some"),
                None => {
                    log::warn!(
                        "[gst-cl] retire serial={serial} not found"
                    );
                    return Ok(());
                }
            }
        };
        if let Some(id) = sub_bin.pad_added_id.take() {
            sub_bin.qtdemux.disconnect(id);
        }
        let probe_owned = sub_bin.probe_id.take();
        if let Some(pid) = probe_owned {
            sub_bin.concat_sink.remove_probe(pid);
        }
        let _ = sub_bin.bin.set_state(gst::State::Null);
        let _ = pipeline.remove(&sub_bin.bin);
        concat.release_request_pad(&sub_bin.concat_sink);
        let depth_after = sub_bins.lock().unwrap().len();
        let fired = sub_bin.eos_fire_count.load(Ordering::Relaxed);
        log::info!(
            "[gst-cl] RETIRE serial={serial} fired={fired} \
             (queue_depth_after={depth_after})"
        );
        Ok(())
    }

    /// Cadence worker loop. Spawned once per GstDecoder (in
    /// build_on_worker, after the pipeline reaches PLAYING).
    /// Owns pipe_cmd_rx + Arc clones of pipeline/concat/
    /// sub_bins/clip_path/serial_counter/pipe_cmd_tx/
    /// last_concat_add_at. Sequentially processes AddNextClip
    /// and Retire commands from the EOS probes off the
    /// present thread.
    ///
    /// Exit conditions:
    /// - cadence_stop set true (Drop signals this) -> exit cleanly
    /// - All probe txs dropped (Senders gone) -> recv returns Err -> exit
    #[allow(clippy::too_many_arguments)]
    fn cadence_loop(
        rx: mpsc::Receiver<PipelineCmd>,
        cadence_stop: Arc<AtomicBool>,
        pipeline: gst::Pipeline,
        concat: gst::Element,
        sub_bins: Arc<Mutex<VecDeque<SubBin>>>,
        clip_path: Arc<Mutex<PathBuf>>,
        serial_counter: Arc<AtomicU64>,
        pipe_cmd_tx: mpsc::Sender<PipelineCmd>,
        last_concat_add_at: Arc<Mutex<Option<std::time::Instant>>>,
        clip_basename_initial: String,
    ) {
        log::info!(
            "[gst-cadence] worker up for {clip_basename_initial}"
        );
        // Use recv_timeout so we can check cadence_stop
        // periodically (recv() would block indefinitely).
        loop {
            if cadence_stop.load(Ordering::Relaxed) {
                log::info!(
                    "[gst-cadence] stop flag set for {clip_basename_initial}; exiting"
                );
                break;
            }
            match rx.recv_timeout(std::time::Duration::from_millis(250)) {
                Ok(cmd) => match cmd {
                    PipelineCmd::AddNextClip => {
                        if let Err(e) = add_next_clip_inner(
                            &pipeline,
                            &concat,
                            &sub_bins,
                            &clip_path,
                            &serial_counter,
                            &pipe_cmd_tx,
                            &last_concat_add_at,
                        ) {
                            log::warn!(
                                "[gst-cadence] add_next_clip failed: {e:#}"
                            );
                        }
                    }
                    PipelineCmd::Retire(serial) => {
                        if let Err(e) = retire_subgraph_inner(
                            &pipeline,
                            &concat,
                            &sub_bins,
                            serial,
                        ) {
                            log::warn!(
                                "[gst-cadence] retire serial={serial} failed: {e:#}"
                            );
                        }
                    }
                },
                Err(mpsc::RecvTimeoutError::Timeout) => {
                    // Periodic wakeup; loop back to check stop.
                }
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    log::info!(
                        "[gst-cadence] all senders dropped; exiting"
                    );
                    break;
                }
            }
        }
    }

    impl GstDecoder {
        /// Append a new sub-bin. Used by build_on_worker for
        /// pre-queue (cadence worker not yet spawned at that
        /// point). After finalize, the cadence worker handles
        /// EOS-triggered AddNextClip directly.
        fn add_next_clip(&mut self) -> Result<()> {
            add_next_clip_inner(
                &self.pipeline,
                &self.concat,
                &self.sub_bins,
                &self.clip_path,
                &self.serial_counter,
                &self.pipe_cmd_tx,
                &self.last_concat_add_at,
            )
        }

        /// Tear down a sub-bin. Used by Drop's retire-all step
        /// (cadence worker has been joined by then; no
        /// contention on sub_bins).
        fn retire_subgraph(&mut self, serial: u64) -> Result<()> {
            retire_subgraph_inner(
                &self.pipeline,
                &self.concat,
                &self.sub_bins,
                serial,
            )
        }

        /// Persistent-pipeline clip switch (cutloop EOS-swap
        /// pattern; M1 of the real fix for the morning's
        /// vc4/V3D create-blanks-presenter root). Replaces the
        /// retire+recreate handoff: same v4l2h264dec + same
        /// pipeline state PLAYING throughout; only the
        /// concat sub-bin queue is updated.
        ///
        /// Mechanics:
        ///   1. self.clip_path := new_clip (so future
        ///      add_next_clip calls -- triggered by EOS-probe
        ///      -> AddNextClip command -- naturally queue the
        ///      NEW clip).
        ///   2. Retire all NON-FRONT sub-bins (they were
        ///      queued OLD; we want NEW queued instead). The
        ///      front sub-bin (currently playing OLD) stays
        ///      and plays to its natural EOS.
        ///   3. add_next_clip() -- queues ONE NEW-clip sub-bin
        ///      behind the OLD-front.
        ///
        /// At natural EOS of OLD-front (~0-5s for our 4.75s
        /// clips), concat absorbs the EOS, our EOS-probe sends
        /// AddNextClip via mpsc, process_pending adds another
        /// NEW sub-bin (clip_path is now NEW), queue becomes
        /// [NEW-promoted, NEW-queued] = natural NEW looping
        /// resumes.
        ///
        /// What this AVOIDS (= the morning's root cause):
        /// no eglCreateContext, no GLContext::new_wrapped, no
        /// new GLBufferPool, no glupload caps renegotiation,
        /// no decoder STREAMOFF/STREAMON, no FLUSH events on
        /// v4l2h264dec (cutloop principle: "permanent never-
        /// flush pads"). The vc4 V3D has no per-transition
        /// work to do; the presenter renders normally.
        ///
        /// Caps assumption: NEW clip has same caps as OLD
        /// (h264 720p 24fps Main 0-B uniform for our 17-clip
        /// reel per QA ffprobe). If caps differ, glupload
        /// renegotiates -> we're back to the contention.
        /// h264parse config-interval=-1 + matching caps make
        /// the SPS/PPS update seamless on the shared decoder.
        ///
        /// Timing: switch_to_clip must be called early enough
        /// that OLD-front EOSes BEFORE the next FadingX
        /// requires this slot. With default hold_sec=20s and
        /// 4.75s clips, OLD EOSes within ~5s of the call ->
        /// ample margin. For short hold_sec or long clips,
        /// the NEW clip might not be ready in time -- caller
        /// (Streams::tick) chooses when to invoke.
        pub fn switch_to_clip(&mut self, new_clip: &Path) -> Result<()> {
            // LAZY-SWITCH REWRITE (post-91acb3f present-thread
            // stall): the eager retire+add design called
            // gst-pipeline state-change ops (sync_state_with_
            // parent, set_state Null on the retire side) on
            // the PRESENT thread inside switch_to_clip. On
            // glass these took ~7s, blocking render -> watchdog
            // fired -> exit(7). Even the cadence-drain cure
            // (process_pending at start) ran the same blocking
            // ops just queued from EOS-probes.
            //
            // The cure: do NOTHING on the present thread except
            // update self.clip_path + self.clip_basename. The
            // EXISTING EOS-probe-triggered cadence does the
            // rest: at next natural clip-EOS (~5s for 4.75s
            // clips), the probe sends AddNextClip via mpsc;
            // process_pending (called from run_loop on present
            // OUTSIDE switch_to_clip) calls add_next_clip,
            // which reads self.clip_path = NEW and queues a
            // NEW-clip sub-bin. After OLD-front + OLD-queued
            // play through, concat advances to the fresh NEW
            // sub-bin.
            //
            // Materialization timeline (default 4.75s clips):
            //   t=0:  switch_to_clip mutates clip_path (~ns)
            //   t=~5s: OLD-front EOS -> probe adds NEW
            //   t=~10s: OLD-queued EOS -> concat advances to NEW
            //   NEW visible from t=~10s onward
            //
            // Constraint: switch_to_clip must be called >=10s
            // before the next FadingX needs this slot. With
            // default hold_sec=20s, called at HoldingY entry,
            // there's ~20s margin -> safe.
            //
            // What this AVOIDS:
            //   - present-thread stall (no gst state ops)
            //   - cadence-drain race (no retire-non-front)
            //   - bus DOWNSTREAM EOS (concat queue never drops
            //     below depth=2; existing EOS-cadence keeps it)
            //   - EGL panic codepath (gst-gl never invoked
            //     from switch_to_clip)
            //
            // What this gives up:
            //   - immediate-visibility of the new clip on the
            //     non-visible slot (lazy ~5-10s instead of
            //     eager 0-5s). Not user-visible during hold.
            let old_basename = self.clip_basename.clone();
            let new_basename = new_clip
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("?")
                .to_string();

            // Mutate clip_path via Arc<Mutex> (now shared
            // with cadence worker). switch_to_clip is on
            // present thread; cadence worker reads clip_path
            // at each add_next_clip_inner call. Eventually-
            // consistent: any in-flight add reads OLD; the
            // next add reads NEW.
            {
                let mut g = self.clip_path.lock().map_err(|p| {
                    anyhow!("clip_path lock poisoned: {p}")
                })?;
                *g = new_clip.to_path_buf();
            }
            self.clip_basename = new_basename.clone();

            log::info!(
                "[gst-cl] {old_basename}->{new_basename} switch_to_clip: \
                 LAZY (clip_path updated; NEW takes effect at next \
                 ~5-10s natural EOS cadence)"
            );

            if crate::kms::trace_enabled() {
                crate::kms::trace_writeln(&format!(
                    "[trace-switch] ts_ms={} event=DONE \
                     old={old_basename} new={new_basename} \
                     mode=lazy",
                    crate::kms::trace_ts_ms(),
                ));
            }

            Ok(())
        }

        /// NO-OP shim. Pre-cadence-worker design called this
        /// from kms::run_loop per iter to drain pipe_cmd_rx
        /// on the present thread (the source of the
        /// near_concat_add ~33-80ms hitches). The long-lived
        /// cadence worker now owns pipe_cmd_rx and drains it
        /// directly off-thread. Kept as a stub so the
        /// existing run_loop call site stays compile-clean.
        pub fn process_pending(&mut self) -> Result<()> {
            Ok(())
        }

        /// Take latest sample (if any) and map to GL texture id.
        /// If no new sample, reuse cached current_frame.
        /// Sets last_was_new_sample so FrameTrace can attribute
        /// stuck frames to decoder starvation vs render cost.
        pub fn latest_texture(&mut self) -> Result<Option<(u32, TexTarget)>> {
            let new_sample = {
                let mut guard = self.latest_sample.lock().map_err(|p| {
                    anyhow!("latest_sample lock poisoned: {p}")
                })?;
                guard.take()
            };
            if let Some(sample) = new_sample {
                self.last_was_new_sample = true;
                let tex_id = self.adopt_sample(sample)?;
                return Ok(Some((tex_id, self.tex_target)));
            }
            self.last_was_new_sample = false;
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

            log::info!("[gst-drop] {name} step=0 stop+join cadence worker");
            // Stop the cadence worker FIRST. It owns
            // pipe_cmd_rx + Arc clones of sub_bins / clip_path /
            // serial_counter; we need it out of the way before
            // the synchronous retire-all + pipeline NULL steps
            // below race with worker-side mutations.
            self.cadence_stop.store(true, Ordering::Relaxed);
            if let Some(h) = self.cadence_thread.take() {
                let _ = h.join();
            }
            log::info!("[gst-drop] {name} step=0 cadence joined");

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
