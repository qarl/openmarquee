//! Phase 1 keystone import: ONE GStreamer pipeline decoding
//! ONE H.264 clip into GL textures that blendr samples directly.
//!
//! Pipeline (cutloop-shaped, single sub-bin):
//!   filesrc -> qtdemux -> h264parse -> v4l2h264dec
//!            -> glupload -> appsink
//!
//! The load-bearing piece is GL CONTEXT SHARE. gst-gl creates
//! its own GstGLDisplay/GstGLContext by default; the textures
//! it emits are then VALID ONLY in its private context, NOT in
//! blendr's. We MUST hand it our wrapped display + context via
//! a SYNC bus handler responding to NEED_CONTEXT messages
//! BEFORE the pipeline transitions out of READY. Get this
//! wrong and the symptom is a silently-black/green --capture
//! PPM (the #1 risk for the whole phase per dispatch).
//!
//! samplerExternalOES vs sampler2D: glupload + DMABuf path on
//! Mesa V3D emits GL_TEXTURE_EXTERNAL_OES textures (zero copy
//! from V4L2 capture). On older Mesa OR if external-oes caps
//! negotiation fails, we fall back to GL_TEXTURE_2D RGBA
//! (glupload runs an internal YUV->RGB conversion). We try
//! external-oes first; gles_present.rs picks the matching
//! shader/binding target by reading TexTarget exposed here.

use anyhow::Result;

/// Which texture target the appsink's negotiated caps resolved
/// to. Read by gles_present once the first sample arrives so
/// the right shader + glBindTexture target is picked.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TexTarget {
    /// GL_TEXTURE_EXTERNAL_OES (0x8D65) — zero-copy DMABuf
    /// from glupload; samplerExternalOES in the shader. V3D
    /// does YUV->RGB at sample time.
    External,
    /// GL_TEXTURE_2D — RGBA; glupload converted NV12 internally.
    /// Sampler2D shader. Slightly less efficient but always
    /// available.
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
        pub fn pull_first_texture(&mut self) -> Result<u32> {
            anyhow::bail!("GstDecoder: Linux only")
        }
        pub fn try_pull_texture(&mut self) -> Result<Option<u32>> {
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

    /// GL_TEXTURE_EXTERNAL_OES — not in glow's enum table.
    /// 0x8D65 per GL_OES_EGL_image_external spec; matches the
    /// OLD renderer's use at code2/renderer/src/hdmi.rs.
    pub const GL_TEXTURE_EXTERNAL_OES: u32 = 0x8D65;

    /// Owns the GStreamer pipeline + the GL ctx share state +
    /// the currently-mapped GL frame (held so the texture is
    /// valid for the lifetime of the present-loop iteration
    /// that pulled it).
    pub struct GstDecoder {
        pipeline: gst::Pipeline,
        appsink: gst_app::AppSink,
        /// Holds the most-recent mapped GLVideoFrame so the
        /// underlying GstGLMemory stays alive while blendr
        /// samples its texture. Replaced (NEW mapped, then OLD
        /// dropped) atomically each iteration so we never have
        /// zero textures in flight.
        current_frame: Option<gst_gl::GLVideoFrame<gst_gl::gl_video_frame::Readable>>,
        /// Which target the negotiated caps gave us. Set on
        /// first pull, read by gles_present.
        pub tex_target: TexTarget,
        /// True once first sample has been pulled.
        first_done: bool,
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
            //     EGLDisplay (pointer) — pass as `usize` for
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
            //     context — any texture id created on it is
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
                    &filesrc, &qtdemux, &h264parse, &v4l2dec, &glupload, &appsink_el,
                ])
                .context("pipeline.add_many")?;

            // Static links (qtdemux's pad is dynamic).
            filesrc
                .link(&qtdemux)
                .context("link filesrc -> qtdemux")?;
            gst::Element::link_many([&h264parse, &v4l2dec, &glupload, &appsink_el])
                .context("link h264parse->dec->glupload->appsink")?;

            // qtdemux pad-added: link video pad to h264parse
            // sink. Mirrors cutloop.py:236-244.
            let h264parse_for_pad = h264parse.clone();
            qtdemux.connect_pad_added(move |_demux, pad| {
                // pad.current_caps() -> Option<Caps>; pad.query_caps(None) -> Caps.
                // Wrap the latter so the or_else branches both yield Option<Caps>.
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

            // appsink properties + caps.
            //
            // Try the zero-copy external-oes path first; fall
            // back to RGBA sampler2D if state-change to PAUSED
            // fails on this caps (see below).
            let caps_external = gst::Caps::builder("video/x-raw")
                .features(["memory:GLMemory"])
                .field("format", "NV12")
                .field("texture-target", "external-oes")
                .build();
            appsink.set_caps(Some(&caps_external));
            appsink.set_max_buffers(2);
            appsink.set_drop(false);
            appsink.set_sync(false);
            // emit-signals: pull-based API; don't fire callbacks.
            // gstreamer-app 0.23 exposes this via the GObject
            // property setter (no typed wrapper at the time of
            // this writing).
            appsink.set_property("emit-signals", false);

            // (d) Install SYNC bus handler for NEED_CONTEXT.
            // MUST run before set_state(PAUSED).
            let bus = pipeline
                .bus()
                .ok_or_else(|| anyhow!("pipeline has no bus"))?;
            let display_for_bus = gst_display.clone();
            let context_for_bus = gst_context.clone();
            bus.set_sync_handler(move |_bus, msg| {
                use gst::MessageView;
                if let MessageView::NeedContext(nc) = msg.view() {
                    let ctx_type = nc.context_type();
                    // gst::Context is a MiniObject (refcounted). To
                    // mutate the structure we must obtain a mutable
                    // ref via make_mut() — it clones on write if
                    // the context is shared. Freshly created here,
                    // so the clone path is never taken.
                    if ctx_type == "gst.gl.GLDisplay" {
                        let mut c = gst::Context::new(ctx_type, true);
                        c.make_mut()
                            .structure_mut()
                            .set("gst.gl.GLDisplay", &display_for_bus);
                        if let Some(el) =
                            msg.src().and_then(|s| s.downcast_ref::<gst::Element>())
                        {
                            el.set_context(&c);
                        }
                    } else if ctx_type == "gst.gl.app_context" {
                        let mut c = gst::Context::new(ctx_type, true);
                        c.make_mut()
                            .structure_mut()
                            .set("context", &context_for_bus);
                        if let Some(el) =
                            msg.src().and_then(|s| s.downcast_ref::<gst::Element>())
                        {
                            el.set_context(&c);
                        }
                    }
                    return gst::BusSyncReply::Drop;
                }
                gst::BusSyncReply::Pass
            });

            // (e) State -> PAUSED. If FAILURE, retry with RGBA
            //     sampler2D fallback caps.
            let mut tex_target = TexTarget::External;
            let preroll =
                pipeline.set_state(gst::State::Paused).map_err(|e| anyhow!("{e:?}"));
            let preroll = match preroll {
                Ok(_) => Ok(()),
                Err(e) => {
                    log::warn!(
                        "[gst] external-oes preroll failed ({e}); \
                         retrying with sampler2D RGBA fallback"
                    );
                    let _ = pipeline.set_state(gst::State::Null);
                    let caps_2d = gst::Caps::builder("video/x-raw")
                        .features(["memory:GLMemory"])
                        .field("format", "RGBA")
                        .build();
                    appsink.set_caps(Some(&caps_2d));
                    tex_target = TexTarget::TwoD;
                    pipeline
                        .set_state(gst::State::Paused)
                        .map_err(|e| anyhow!("preroll fallback also failed: {e:?}"))
                        .map(|_| ())
                }
            };
            preroll?;
            log::info!(
                "[gst] preroll OK; negotiated tex_target={tex_target:?}"
            );

            // Pump to PLAYING.
            pipeline
                .set_state(gst::State::Playing)
                .map_err(|e| anyhow!("set_state PLAYING: {e:?}"))?;

            Ok(GstDecoder {
                pipeline,
                appsink,
                current_frame: None,
                tex_target,
                first_done: false,
                gst_display,
                gst_context,
            })
        }

        /// Pull the very first sample with a generous timeout
        /// (5s) so the pipeline finishes negotiation + decodes
        /// the first frame before blendr's loop presents black.
        /// Returns the GL texture id (valid in blendr's context).
        pub fn pull_first_texture(&mut self) -> Result<u32> {
            let sample = self
                .appsink
                .try_pull_sample(gst::ClockTime::from_seconds(5))
                .ok_or_else(|| anyhow!("first pull_sample timed out (5s)"))?;
            let tex = self.adopt_sample(sample)?;
            self.first_done = true;
            log::info!(
                "[gst] first frame in; tex_id={tex} target={:?}",
                self.tex_target
            );
            Ok(tex)
        }

        /// Non-blocking-ish pull (~1 vsync timeout). If no new
        /// sample is ready, returns Ok(None) and the caller
        /// reuses the previous texture (no-op present frame).
        pub fn try_pull_texture(&mut self) -> Result<Option<u32>> {
            if !self.first_done {
                return Ok(Some(self.pull_first_texture()?));
            }
            let sample = match self
                .appsink
                .try_pull_sample(gst::ClockTime::from_mseconds(16))
            {
                Some(s) => s,
                None => return Ok(None),
            };
            Ok(Some(self.adopt_sample(sample)?))
        }

        /// Replace `current_frame` with a fresh GLVideoFrame
        /// mapped over the sample's buffer. The NEW frame is
        /// mapped BEFORE the OLD one is dropped so we never
        /// have a zero-textures-in-flight moment.
        fn adopt_sample(&mut self, sample: gst::Sample) -> Result<u32> {
            let buffer = sample
                .buffer_owned()
                .ok_or_else(|| anyhow!("sample has no buffer"))?;
            let caps = sample
                .caps()
                .ok_or_else(|| anyhow!("sample has no caps"))?;
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
            // Drop happens here: previous current_frame released
            // AFTER new_frame is mapped. (Rust drop semantics:
            // the old value is dropped when the assignment ends,
            // i.e. after new_frame has its replacement seat.)
            self.current_frame = Some(new_frame);
            Ok(tex_id)
        }
    }

    impl Drop for GstDecoder {
        fn drop(&mut self) {
            // Drop the mapped frame FIRST so its GLMemory unmap
            // happens before pipeline NULL teardown unbinds the
            // GL context.
            self.current_frame = None;
            // Pipeline to NULL synchronously; ignore err (best-
            // effort cleanup).
            let _ = self.pipeline.set_state(gst::State::Null);
            // Bus sync handler holds clones of gst_display + gst
            // _context; replacing it with default drops them.
            if let Some(bus) = self.pipeline.bus() {
                bus.unset_sync_handler();
            }
        }
    }

}
