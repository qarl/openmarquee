//! Phase 1 (single-stream) + Phase 2 (two-stream blend) keystone
//! import: cutloop-shaped GStreamer pipeline(s) decoding H.264
//! into GL textures that blendr samples directly.
//!
//! Pipeline (per decoder, single sub-bin):
//!   filesrc -> qtdemux -> h264parse -> v4l2h264dec
//!            -> queue(leaky=downstream, cap=2)
//!            -> glupload -> appsink
//!
//! THE GL CONTEXT SHARE is the load-bearing piece. gst-gl creates
//! its own GstGLDisplay/GstGLContext by default and the textures
//! it emits are then VALID ONLY in its private context, NOT in
//! blendr's. We MUST hand it our wrapped display + context via
//! a SYNC bus handler responding to NEED_CONTEXT messages
//! BEFORE the pipeline transitions out of READY. Get this wrong
//! and the symptom is a silently-black/green --capture PPM (the
//! #1 risk per dispatch).
//!
//! BUG 2 SIMPLIFICATION (post-Phase-1-keystone): vc4's gst-gl
//! always hands back GL_TEXTURE_EXTERNAL_OES textures regardless
//! of caps. The earlier "try external-oes caps first, fall back
//! to RGBA" dance was pointless on this hardware -- external-oes
//! caps always failed preroll, RGBA always succeeded, and the
//! resulting texture was always EXTERNAL anyway. This commit
//! drops the dual-caps preroll and uses ONLY the RGBA caps path
//! (proven-good on vc4). adopt_sample queries the GLMemory's
//! ACTUAL texture_target and updates self.tex_target so the
//! present-side draw routes to samplerExternalOES vs sampler2D
//! based on RUNTIME truth, not caps-negotiation assumption.
//!
//! PHASE 2 PULL-THREAD ARCHITECTURE (the FREEZE-KILLER): each
//! GstDecoder spawns a background thread that loops on
//! appsink.try_pull_sample, atomically replacing a shared
//! latest-sample slot. The present (main) thread reads the
//! latest sample via `latest_texture()` -- non-blocking on
//! decode/import. GL ops (the GLVideoFrame map) STAY on the
//! main thread where blendr's EGL context is current; the
//! pull thread only shuffles gst::Sample handles.

use anyhow::Result;

/// Which texture target the negotiated caps resolved to.
/// Read by gles_present once the first sample arrives so the
/// right shader + glBindTexture target is picked.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TexTarget {
    /// GL_TEXTURE_EXTERNAL_OES (0x8D65) -- zero-copy DMABuf
    /// from glupload; samplerExternalOES in the shader. V3D
    /// does YUV->RGB at sample time. This is the path vc4
    /// always returns.
    External,
    /// GL_TEXTURE_2D -- RGBA; glupload converted NV12
    /// internally. Sampler2D shader. Documented fallback for
    /// non-vc4 hardware; not exercised on FYS.
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
    impl GstDecoder {
        pub fn new(_egl: &crate::egl_gbm::Egl, _clip: &std::path::Path) -> Result<Self> {
            anyhow::bail!("GstDecoder: Linux only")
        }
        pub fn latest_texture(&mut self) -> Result<Option<(u32, TexTarget)>> {
            anyhow::bail!("GstDecoder: Linux only")
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::egl_gbm::Egl;
    use anyhow::{anyhow, Context};
    use gst::prelude::*;
    use gst_gl::prelude::*;
    use std::path::Path;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{Arc, Mutex};
    use std::thread::{self, JoinHandle};

    /// GL_TEXTURE_EXTERNAL_OES -- not in glow's enum table.
    /// 0x8D65 per GL_OES_EGL_image_external spec; matches the
    /// OLD renderer's use at code2/renderer/src/hdmi.rs.
    pub const GL_TEXTURE_EXTERNAL_OES: u32 = 0x8D65;

    /// Pull-thread loop tick. 33ms = ~30Hz, enough to deliver
    /// 24fps clip frames promptly without busy-spinning.
    const PULL_TICK_MS: u64 = 33;
    /// First-sample wait at construction (5s). Pipeline must
    /// reach PLAYING + decode frame 1 within this window.
    const FIRST_SAMPLE_TIMEOUT_S: u64 = 5;

    /// Owns the GStreamer pipeline + GL ctx share state + the
    /// pull thread + the latest-sample slot + the currently-
    /// mapped GLVideoFrame (held so the texture stays valid for
    /// the present iteration that pulled it).
    pub struct GstDecoder {
        pipeline: gst::Pipeline,
        // appsink retained for Drop ordering; the pull thread
        // owns its own clone for try_pull_sample.
        #[allow(dead_code)]
        appsink: gst_app::AppSink,
        /// The pull thread atomically replaces this; the present
        /// thread takes via `latest_texture()`. None until the
        /// pull thread populates (which seeded the first sample
        /// synchronously in `new()`).
        latest_sample: Arc<Mutex<Option<gst::Sample>>>,
        /// Signals the pull thread to exit.
        stop: Arc<AtomicBool>,
        /// Joined in Drop.
        pull_thread: Option<JoinHandle<()>>,
        /// The most-recently-mapped GLVideoFrame. Holds the
        /// GLMemory ref alive so blendr's GL keeps a valid
        /// texture id across iterations that don't pull a new
        /// sample. Replaced (NEW mapped, then OLD dropped) each
        /// time `latest_texture()` consumes a new sample.
        current_frame: Option<gst_gl::GLVideoFrame<gst_gl::gl_video_frame::Readable>>,
        /// The texture target the LAST mapped sample reported.
        /// Updated by adopt_sample from the GLMemory's actual
        /// texture_target() (not the caps-negotiated label).
        /// kms::run_loop reads this AFTER `latest_texture()` to
        /// route the draw to the matching shader.
        pub tex_target: TexTarget,
        /// Held to extend lifetime past pipeline drop ordering;
        /// pipeline holds an internal ref via set_context, but
        /// we keep clones so the Drop order doesn't free them
        /// before our currently-held GLVideoFrame.
        #[allow(dead_code)]
        gst_display: gst_gl::GLDisplay,
        #[allow(dead_code)]
        gst_context: gst_gl::GLContext,
    }

    impl GstDecoder {
        pub fn new(egl: &Egl, clip: &Path) -> Result<Self> {
            gst::init().context("gst::init")?;

            // (a) Wrap blendr's EGLDisplay as a GstGLDisplayEGL.
            //     The display handle from khronos-egl is a raw
            //     EGLDisplay (pointer) -- pass as `usize` for
            //     gst_gl's wrapper API.
            let egl_display_ptr = egl.display.as_ptr() as usize;
            let gst_display: gst_gl::GLDisplay = unsafe {
                gst_gl_egl::GLDisplayEGL::with_egl_display(egl_display_ptr)
                    .map_err(|e| anyhow!("GLDisplayEGL::with_egl_display: {e}"))?
            }
            .upcast();

            // (b) Wrap blendr's EGLContext as a GstGLContext on
            //     platform=EGL, api=GLES2. The wrapped handle
            //     SHARES the EGL context group with our blendr
            //     context -- any texture id created on it is
            //     valid in our context.
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

            // Activate + fill_info so gst-gl probes extensions
            // against OUR context. fill_info must run from a
            // thread with the context current (main thread; EGL
            // is made-current after egl_gbm::Egl::bring_up).
            gst_context
                .activate(true)
                .map_err(|e| anyhow!("gst_context.activate(true): {e}"))?;
            gst_context
                .fill_info()
                .map_err(|e| anyhow!("gst_context.fill_info: {e}"))?;
            // Release on main; gst-gl's streaming thread will
            // make-current its own pair as needed. We re-assert
            // make-current on the main thread before each draw
            // (see kms::run_loop).
            gst_context
                .activate(false)
                .map_err(|e| anyhow!("gst_context.activate(false): {e}"))?;

            // (c) Build pipeline programmatically.
            //
            // queue(leaky=downstream, cap=2) between dec and
            // glupload per Phase 2 plan: gives the decoder a
            // place to push without backpressure-stalling the
            // V4L2 capture loop; on overflow drops oldest rather
            // than blocking, so one stream stalling doesn't
            // starve the other when they share a CMA pool.
            let pipeline = gst::Pipeline::new();
            let clip_str = clip.to_str().ok_or_else(|| {
                anyhow!("clip path is not valid UTF-8: {}", clip.display())
            })?;
            let filesrc = gst::ElementFactory::make("filesrc")
                .property("location", clip_str)
                .build()
                .context("make filesrc")?;
            let qtdemux = gst::ElementFactory::make("qtdemux")
                .build()
                .context("make qtdemux")?;
            let h264parse = gst::ElementFactory::make("h264parse")
                .property("config-interval", -1i32)
                .build()
                .context("make h264parse")?;
            let v4l2dec = gst::ElementFactory::make("v4l2h264dec")
                .build()
                .context("make v4l2h264dec")?;
            let outq = gst::ElementFactory::make("queue")
                .property_from_str("leaky", "downstream")
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
                .add_many([
                    &filesrc, &qtdemux, &h264parse, &v4l2dec, &outq, &glupload, &appsink_el,
                ])
                .context("pipeline.add_many")?;

            // Static links (qtdemux's pad is dynamic).
            filesrc
                .link(&qtdemux)
                .context("link filesrc -> qtdemux")?;
            gst::Element::link_many([&h264parse, &v4l2dec, &outq, &glupload, &appsink_el])
                .context("link h264parse->dec->outq->glupload->appsink")?;

            // qtdemux pad-added: link video pad to h264parse
            // sink. Mirrors cutloop.py:236-244.
            let h264parse_for_pad = h264parse.clone();
            qtdemux.connect_pad_added(move |_demux, pad| {
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
                    log::warn!("[gst] qtdemux pad link to h264parse failed: {e:?}");
                }
            });

            // appsink properties + caps. BUG 2 simplification:
            // use ONLY the RGBA GLMemory caps path. vc4's gst-gl
            // hands back EXTERNAL_OES textures under this path
            // anyway (queried at adopt_sample), so trying
            // external-oes caps first was always failure-then-
            // fallback noise.
            //
            // drop=true (changed from Phase 1 false): with the
            // pull thread polling at ~30Hz and the decoder
            // delivering at clip-fps, drop-newer would block
            // the decoder; we want LATEST not COMPLETE for
            // present-thread render.
            let caps_rgba = gst::Caps::builder("video/x-raw")
                .features(["memory:GLMemory"])
                .field("format", "RGBA")
                .build();
            appsink.set_caps(Some(&caps_rgba));
            appsink.set_max_buffers(2);
            appsink.set_drop(true);
            appsink.set_sync(false);
            appsink.set_property("emit-signals", false);

            // (d) Install SYNC bus handler for NEED_CONTEXT +
            //     log STATE_CHANGED / ERROR / WARNING / EOS so
            //     we see what's actually happening on the
            //     pipeline bus. (No GLib main loop, so the
            //     async bus would otherwise be silent.) MUST run
            //     before set_state(PAUSED).
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
                        let from_pipeline = src_name == "pipeline0"
                            || src_name == "pipeline1"
                            || src_name.starts_with("pipeline");
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
                        log::info!("[gst-bus] EOS src={src_name}");
                    }
                    MessageView::AsyncDone(_) => {
                        log::info!("[gst-bus] ASYNC_DONE src={src_name}");
                    }
                    _ => {}
                }
                gst::BusSyncReply::Pass
            });

            // (e) State -> PAUSED with state-wait confirmation.
            log::info!("[gst] set_state(PAUSED) RGBA-caps path");
            let preroll_ret = pipeline
                .set_state(gst::State::Paused)
                .map_err(|e| anyhow!("set_state(PAUSED): {e:?}"))?;
            log::info!(
                "[gst] set_state(PAUSED) returned {preroll_ret:?}; waiting..."
            );
            let (wait_res, cur, pending) =
                pipeline.state(gst::ClockTime::from_seconds(5));
            log::info!(
                "[gst] state-wait after PAUSED: {wait_res:?} \
                 cur={cur:?} pending={pending:?}"
            );
            if cur != gst::State::Paused {
                return Err(anyhow!(
                    "preroll did not settle to PAUSED \
                     (cur={cur:?}, pending={pending:?})"
                ));
            }
            log::info!("[gst] preroll PAUSED confirmed");

            // (f) PLAYING with state-wait confirmation.
            log::info!("[gst] set_state(PLAYING) starting");
            let play_ret = pipeline
                .set_state(gst::State::Playing)
                .map_err(|e| anyhow!("set_state PLAYING: {e:?}"))?;
            log::info!("[gst] set_state(PLAYING) returned {play_ret:?}; waiting...");
            let (wait_res, cur, pending) =
                pipeline.state(gst::ClockTime::from_seconds(10));
            log::info!(
                "[gst] state-wait after PLAYING: {wait_res:?} \
                 cur={cur:?} pending={pending:?}"
            );
            if cur != gst::State::Playing {
                return Err(anyhow!(
                    "pipeline did NOT reach PLAYING within 10s \
                     (cur={cur:?} pending={pending:?}); buffers will \
                     never flow"
                ));
            }
            log::info!("[gst] PLAYING confirmed; pipeline streaming");

            // (g) Block on the first sample so caller sees a
            //     ready decoder (no "is the pipeline ready?"
            //     race in main / kms).
            let first_sample = appsink
                .try_pull_sample(gst::ClockTime::from_seconds(FIRST_SAMPLE_TIMEOUT_S))
                .ok_or_else(|| {
                    anyhow!(
                        "first sample timed out ({FIRST_SAMPLE_TIMEOUT_S}s); \
                         pipeline is PLAYING but no buffer delivered"
                    )
                })?;
            log::info!("[gst] first sample seeded; spawning pull thread");

            // (h) Set up shared state + spawn pull thread.
            let latest_sample = Arc::new(Mutex::new(Some(first_sample)));
            let stop = Arc::new(AtomicBool::new(false));
            let appsink_for_pull = appsink.clone();
            let slot_for_pull = latest_sample.clone();
            let stop_for_pull = stop.clone();
            let clip_basename = clip
                .file_name()
                .and_then(|s| s.to_str())
                .unwrap_or("?")
                .to_string();
            let thread_name = format!("blendr-gst-pull-{clip_basename}");
            let pull_thread = thread::Builder::new()
                .name(thread_name)
                .spawn(move || pull_loop(appsink_for_pull, slot_for_pull, stop_for_pull))
                .context("spawn pull thread")?;

            Ok(GstDecoder {
                pipeline,
                appsink,
                latest_sample,
                stop,
                pull_thread: Some(pull_thread),
                current_frame: None,
                // Updated on first adopt_sample (which queries the
                // GLMemory's actual texture_target).
                tex_target: TexTarget::TwoD,
                gst_display,
                gst_context,
            })
        }

        /// Take the latest sample from the slot and map it to a
        /// GL texture id. Non-blocking: returns Ok(None) only if
        /// no sample has ever arrived (shouldn't happen after
        /// new() blocks on the first one). If no NEW sample is
        /// in the slot, reuses the previously-mapped current_frame.
        ///
        /// MUST be called on a thread with blendr's EGL current
        /// (the present/main thread). GLVideoFrame::from_buffer_
        /// readable does GL work on the calling thread.
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
            // No new sample this tick. Reuse the cached frame
            // (its GLMemory ref is still alive in current_frame).
            if let Some(frame) = self.current_frame.as_ref() {
                let tex_id = frame
                    .texture_id(0)
                    .map_err(|e| anyhow!("cached texture_id: {e:?}"))?;
                return Ok(Some((tex_id, self.tex_target)));
            }
            // Pre-first-sample window (pull thread hasn't seeded
            // yet AND new() didn't seed -- shouldn't happen, but
            // defensive). Caller draws black for this frame.
            Ok(None)
        }

        /// Map a sample to a GLVideoFrame + extract its texture
        /// id. Updates self.tex_target from the queried
        /// GLMemory target (vc4 returns ExternalOes even under
        /// RGBA caps). Replaces self.current_frame, dropping the
        /// previous mapping AFTER the new one is bound.
        ///
        /// GL ops happen here; caller must hold blendr's EGL
        /// current.
        fn adopt_sample(&mut self, sample: gst::Sample) -> Result<u32> {
            let buffer = sample
                .buffer_owned()
                .ok_or_else(|| anyhow!("sample has no buffer"))?;
            let caps = sample
                .caps()
                .ok_or_else(|| anyhow!("sample has no caps"))?;

            // Query the actual GL texture target from the
            // underlying GLMemory BEFORE moving the buffer into
            // from_buffer_readable. On vc4 this is always
            // ExternalOes regardless of caps; on other hardware
            // it follows the caps-negotiated layout.
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
                    "[gst] tex_target query: glmemory_actual={:?} \
                     -> routing draw path to {:?}",
                    queried_target, real_tex_target
                );
            } else if real_tex_target != self.tex_target {
                log::warn!(
                    "[gst] tex_target CHANGED mid-stream: was {:?} now {:?}",
                    self.tex_target, real_tex_target
                );
            }
            self.tex_target = real_tex_target;

            let video_info = gst_video::VideoInfo::from_caps(&caps)
                .map_err(|e| anyhow!("VideoInfo::from_caps: {e:?}"))?;
            let new_frame =
                gst_gl::GLVideoFrame::from_buffer_readable(buffer, &video_info)
                    .map_err(|_| {
                        anyhow!(
                            "GLVideoFrame::from_buffer_readable failed \
                             (sample is not GLMemory? caps mismatch?)"
                        )
                    })?;
            let tex_id = new_frame.texture_id(0).map_err(|e| {
                anyhow!("GLVideoFrame::texture_id(0): {e:?}")
            })?;
            // Assignment drops the previous current_frame AFTER
            // new_frame has its replacement seat -- no
            // zero-textures-in-flight moment.
            self.current_frame = Some(new_frame);
            Ok(tex_id)
        }
    }

    /// Pull thread loop. Polls appsink and atomically replaces
    /// the shared latest-sample slot. Exits when `stop` is set.
    fn pull_loop(
        appsink: gst_app::AppSink,
        slot: Arc<Mutex<Option<gst::Sample>>>,
        stop: Arc<AtomicBool>,
    ) {
        log::info!("[gst-pull] thread up");
        while !stop.load(Ordering::Relaxed) {
            // Block briefly so we don't busy-spin. Sample is
            // non-GL data (gst::Buffer ref + caps); no GL ops
            // here.
            let sample =
                appsink.try_pull_sample(gst::ClockTime::from_mseconds(PULL_TICK_MS));
            if let Some(s) = sample {
                // Replace the slot. The OLD sample (if any) is
                // dropped here -- its gst::Buffer ref + GLMemory
                // ref drop unless another holder (current_frame
                // on main) still references it. The mutex is
                // held for microseconds.
                if let Ok(mut g) = slot.lock() {
                    *g = Some(s);
                }
            }
            // Pull miss = no new frame this tick; keep looping.
        }
        log::info!("[gst-pull] stop flag set; exiting");
    }

    impl Drop for GstDecoder {
        fn drop(&mut self) {
            // 1. Signal pull thread to stop BEFORE NULLing the
            //    pipeline so the thread's next try_pull_sample
            //    sees stop=true and exits cleanly (avoids racing
            //    the appsink teardown).
            self.stop.store(true, Ordering::Relaxed);
            // 2. NULL the pipeline; unblocks any in-flight
            //    try_pull_sample on the pull thread + tears down
            //    decoder + glupload.
            let _ = self.pipeline.set_state(gst::State::Null);
            // 3. Drop the latest unconsumed sample so its
            //    GLMemory ref is released BEFORE the pipeline's
            //    GL ctx finalizes.
            if let Ok(mut g) = self.latest_sample.lock() {
                *g = None;
            }
            // 4. Drop the currently-mapped GLVideoFrame for the
            //    same reason.
            self.current_frame = None;
            // 5. Join the pull thread. Worst case ~PULL_TICK_MS
            //    + scheduling delay; bounded.
            if let Some(h) = self.pull_thread.take() {
                let _ = h.join();
            }
            if let Some(bus) = self.pipeline.bus() {
                bus.unset_sync_handler();
            }
        }
    }
}
