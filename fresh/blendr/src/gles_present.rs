//! GLES2 presenter: compiles a tiny full-screen-quad shader,
//! uploads a static 256x256 checkerboard texture, and draws either
//! a hue-cycling solid color (Step A) or the texture (Step B) per
//! frame.

use anyhow::Result;

#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub enum Step {
    Solid,
    Checker,
    /// Phase 1 keystone: sample the latest texture handed in
    /// by gst_decode (one cutloop-style GStreamer pipeline)
    /// onto the fullscreen quad. --clip <PATH> required.
    Video,
    /// Phase 2: two decode pipelines, two EXTERNAL_OES
    /// textures, mix(texA, texB, u_alpha) shader. Requires
    /// --clip-a + --clip-b; --alpha defaults to 0.5 (static
    /// 50/50 blend); the alpha ramp / crossfade timing comes
    /// in Phase 3.
    Blend,
}

/// Black-flash detector (per QA pivot after #4b): a draw is
/// "black" when the clear-to-black happened but the textured
/// overlay was skipped because the expected video texture(s)
/// were None / unsupported. qarl reports a 1-2 frame black
/// flash at crossfade START (new since #2 + #2-alt async
/// create). frame-timing instrumentation can't see this
/// (black frames have normal timing); QA needs this enum
/// piped up to FrameStats to count + log occurrences.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DrawOutcome {
    /// Step painted a textured (or solid-hue) frame normally.
    Drew,
    /// Step::Solid clear (no overlay needed; never black).
    SolidClear,
    /// Frame was cleared to black but no overlay drawn.
    /// Carries the reason for diagnosis.
    Black(BlackReason),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BlackReason {
    /// Step::Video and video_tex_id is None / target missing.
    NoVideoTex,
    /// Step::Blend and BOTH slot tex_ids are None.
    NoBlendTexBoth,
    /// Step::Blend and slot A tex_id is None (B was Some).
    NoBlendTexA,
    /// Step::Blend and slot B tex_id is None (A was Some).
    NoBlendTexB,
    /// Step::Blend got a non-External target pair (skipped draw).
    NonExternalBlend,
}

#[cfg(target_os = "linux")]
pub use linux::*;

#[cfg(not(target_os = "linux"))]
pub use stub::*;

#[cfg(not(target_os = "linux"))]
mod stub {
    use super::*;
    pub struct Presenter;
    impl Presenter {
        pub fn new(_egl: &crate::egl_gbm::Egl, _w: u32, _h: u32, _step: Step)
            -> Result<Self>
        {
            anyhow::bail!("Presenter stub: Linux only")
        }
        pub fn draw_frame(&mut self, _frame_idx: u64) -> Result<DrawOutcome> {
            anyhow::bail!("draw_frame stub: Linux only")
        }
        pub fn capture_back_buffer_ppm(&self, _path: &std::path::Path) -> Result<()> {
            anyhow::bail!("capture stub: Linux only")
        }
        pub fn sample_back_buffer_is_near_black(&self) -> Result<bool> {
            anyhow::bail!("sample stub: Linux only")
        }
        pub fn set_video_texture(
            &mut self,
            _tex_id: u32,
            _target: crate::gst_decode::TexTarget,
        ) {
        }
        pub fn set_video_textures(
            &mut self,
            _a: (u32, crate::gst_decode::TexTarget),
            _b: (u32, crate::gst_decode::TexTarget),
            _alpha: f32,
        ) {
        }
        pub fn release_video_bindings(&mut self) {}
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::egl_gbm::Egl;
    use anyhow::{anyhow, Context};
    use glow::HasContext;

    // #4a: per-op GL error checks were ~10 sync FFI round-trips
    // per fade frame (each glGetError flushes the command queue
    // on bcm2835/vc4 -- per QA, biggest single CPU win for
    // shaving render_mean during fades). These macros compile
    // to NOTHING in release: the body (and the arg expressions)
    // are inside #[cfg(debug_assertions)] so the format!() args
    // are also dead-stripped (no heap alloc on the per-frame
    // bind_texture paths that used to format raw_id every frame).
    //
    // In debug builds: identical behavior to the old check_gl_err
    // / drain_gl_errors fns (early-return Err with the op string).
    //
    // Call site:    gl_check!(&self.gl, "op name");
    //               gl_check!(&self.gl, "op with arg: {}", x);
    //               gl_drain!(&self.gl);
    //
    // Hard rules:
    // - The enclosing fn must return Result<...> (gl_check uses
    //   `return Err(...)` in debug).
    // - The enclosing scope must be `unsafe` (gl_X calls
    //   glow::HasContext::get_error, which is unsafe). All call
    //   sites here are inside `unsafe fn` or `unsafe { ... }`.
    macro_rules! gl_check {
        ($gl:expr, $($arg:tt)*) => {
            #[cfg(debug_assertions)]
            {
                let err = ($gl).get_error();
                if err != glow::NO_ERROR {
                    return Err(anyhow!(
                        "GL error {:#x} after {}",
                        err,
                        format_args!($($arg)*)
                    ));
                }
            }
        };
    }

    macro_rules! gl_drain {
        ($gl:expr) => {
            #[cfg(debug_assertions)]
            {
                while ($gl).get_error() != glow::NO_ERROR {}
            }
        };
    }

    // #4b: fixed attribute-slot indices for the full-screen
    // quad. Every program (2d / ext / blend) uses the SAME
    // VS with "a_pos" + "a_uv" attributes; we
    // bind_attrib_location these slots in link() pre-link, so
    // all programs end up at identical indices. That lets us
    // set up enable_vertex_attrib_array + vertex_attrib_pointer
    // ONCE at Presenter::new() and drop the per-draw churn.
    const QUAD_ATTRIB_POS: u32 = 0;
    const QUAD_ATTRIB_UV: u32 = 1;

    const VS: &str = r#"
        attribute vec2 a_pos;
        attribute vec2 a_uv;
        varying vec2 v_uv;
        void main() {
            v_uv = a_uv;
            gl_Position = vec4(a_pos, 0.0, 1.0);
        }
    "#;

    /// Sampler2D path (Step::Checker + Step::Video TexTarget::TwoD).
    const FS_2D: &str = r#"
        precision mediump float;
        varying vec2 v_uv;
        uniform sampler2D u_tex;
        void main() {
            gl_FragColor = texture2D(u_tex, v_uv);
        }
    "#;

    /// samplerExternalOES path (Step::Video TexTarget::External).
    /// V3D's GL_OES_EGL_image_external does YUV->RGB at sample
    /// time -- zero-copy from V4L2 capture via DMABuf.
    const FS_EXTERNAL_OES: &str = r#"
        #extension GL_OES_EGL_image_external : require
        precision mediump float;
        varying vec2 v_uv;
        uniform samplerExternalOES u_tex;
        void main() {
            gl_FragColor = texture2D(u_tex, v_uv);
        }
    "#;

    /// Phase 2 blend shader: samplerExternalOES x 2 + mix.
    /// Both textures bind to GL_TEXTURE_EXTERNAL_OES via the
    /// extension; u_alpha selects between them (0.0 = pure A,
    /// 1.0 = pure B, 0.5 = 50/50 dissolve). For Phase 2
    /// u_alpha is static; Phase 3 animates it for the
    /// crossfade timing.
    const FS_BLEND_EXT_EXT: &str = r#"
        #extension GL_OES_EGL_image_external : require
        precision mediump float;
        varying vec2 v_uv;
        uniform samplerExternalOES u_tex_a;
        uniform samplerExternalOES u_tex_b;
        uniform float u_alpha;
        void main() {
            vec4 a = texture2D(u_tex_a, v_uv);
            vec4 b = texture2D(u_tex_b, v_uv);
            gl_FragColor = mix(a, b, u_alpha);
        }
    "#;

    pub struct Presenter {
        gl: glow::Context,
        w: i32,
        h: i32,
        step: Step,
        /// sampler2D program (Solid clear unused, Checker, Video TwoD).
        prog_2d: glow::Program,
        /// samplerExternalOES program (Video External only).
        /// Compiled lazily on first Video frame with TexTarget::External
        /// so a non-OES driver (where the extension parse fails) does
        /// not break Step::Checker. None until first set.
        prog_ext: Option<glow::Program>,
        /// Phase 2 blend program (samplerExternalOES x 2). Lazily
        /// compiled on first Step::Blend frame.
        prog_blend: Option<glow::Program>,
        /// VBO handle. Lives for Presenter's lifetime;
        /// Presenter::Drop calls delete_buffer on this. After
        /// #4b the field is no longer used per-draw (the
        /// vertex_attrib_pointer setup at init captured the
        /// VBO on the attrib slots), only at teardown.
        vbo: glow::Buffer,
        /// CPU checkerboard for Step::Checker.
        tex_checker: glow::Texture,
        // #4b: a_pos_loc_X / a_uv_loc_X removed -- with
        // bind_attrib_location at link, all programs pin a_pos
        // to QUAD_ATTRIB_POS=0 and a_uv to QUAD_ATTRIB_UV=1.
        // The actual values are asserted at compile/link time.
        u_tex_loc_2d: glow::UniformLocation,
        u_tex_loc_ext: Option<glow::UniformLocation>,
        u_tex_a_loc_blend: Option<glow::UniformLocation>,
        u_tex_b_loc_blend: Option<glow::UniformLocation>,
        u_alpha_loc_blend: Option<glow::UniformLocation>,
        /// Phase-1 state: the latest video texture id + its target.
        /// Updated by set_video_texture each iteration.
        video_tex_id: Option<u32>,
        video_target: Option<crate::gst_decode::TexTarget>,
        /// Phase-2 state: the second stream's texture + target +
        /// the blend alpha. Updated by set_video_textures each
        /// iteration. video_tex_id / video_target above hold
        /// stream A.
        video_tex_id_b: Option<u32>,
        video_target_b: Option<crate::gst_decode::TexTarget>,
        blend_alpha: f32,
    }

    impl Presenter {
        pub fn new(egl: &Egl, w: u32, h: u32, step: Step) -> Result<Self> {
            let gl = unsafe {
                glow::Context::from_loader_function(|name| {
                    egl.get_proc_address(name)
                })
            };
            unsafe {
                let vs = compile(&gl, glow::VERTEX_SHADER, VS)?;
                let fs_2d = compile(&gl, glow::FRAGMENT_SHADER, FS_2D)?;
                let prog_2d = link(&gl, vs, fs_2d)?;
                gl.delete_shader(vs);
                gl.delete_shader(fs_2d);

                gl.use_program(Some(prog_2d));
                // #4b: bind_attrib_location at link() pinned
                // a_pos -> 0 + a_uv -> 1. Sanity-check the
                // driver honored it (a bind_attrib_location
                // failure leaves the location at driver-
                // assigned). If this assert fires, the per-
                // draw hoisted attrib pointers would source
                // from the wrong slots -> garbage geometry.
                let pos_loc = gl
                    .get_attrib_location(prog_2d, "a_pos")
                    .ok_or_else(|| anyhow!("attribute a_pos missing"))?;
                if pos_loc != QUAD_ATTRIB_POS {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_2d.a_pos failed: \
                         expected slot {QUAD_ATTRIB_POS}, got {pos_loc}"
                    ));
                }
                let uv_loc = gl
                    .get_attrib_location(prog_2d, "a_uv")
                    .ok_or_else(|| anyhow!("attribute a_uv missing"))?;
                if uv_loc != QUAD_ATTRIB_UV {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_2d.a_uv failed: \
                         expected slot {QUAD_ATTRIB_UV}, got {uv_loc}"
                    ));
                }
                let u_tex_loc_2d = gl
                    .get_uniform_location(prog_2d, "u_tex")
                    .ok_or_else(|| anyhow!("uniform u_tex missing"))?;
                gl.uniform_1_i32(Some(&u_tex_loc_2d), 0);

                // Full-screen quad: 2 triangles, interleaved [x y u v].
                #[rustfmt::skip]
                let quad: [f32; 24] = [
                    -1.0, -1.0,   0.0, 0.0,
                     1.0, -1.0,   1.0, 0.0,
                    -1.0,  1.0,   0.0, 1.0,

                    -1.0,  1.0,   0.0, 1.0,
                     1.0, -1.0,   1.0, 0.0,
                     1.0,  1.0,   1.0, 1.0,
                ];
                let vbo = gl.create_buffer()
                    .map_err(|e| anyhow!("glGenBuffers: {e}"))?;
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                gl.buffer_data_u8_slice(
                    glow::ARRAY_BUFFER,
                    bytemuck_quad(&quad),
                    glow::STATIC_DRAW,
                );

                // #4b: hoist vertex-attrib pointer setup +
                // enable to init. Per GLES2 spec
                // (glVertexAttribPointer): "this information is
                // saved as vertex array state" -- the attrib
                // slot captures the VBO that was bound at the
                // time of the call, INDEPENDENT of subsequent
                // ARRAY_BUFFER binding. So we don't need to
                // re-bind the VBO per draw; the attrib slots
                // hold a reference. enable_vertex_attrib_array
                // is also persistent state; once enabled, it
                // stays enabled until disable_vertex_attrib_
                // array (which we no longer call per draw).
                //
                // Risk note: if gst-gl's pull/upload thread were
                // to issue disable_vertex_attrib_array on slot 0
                // or 1, our subsequent draws would source from
                // generic vertex attribs (zeros) instead of the
                // VBO. gst-gl's glupload/GLBufferPool do NOT
                // issue user draws or modify generic vertex
                // attrib state -- they're confined to texture
                // upload + PBO state. Verified on glass via
                // QA's previous #2/#3b/#2-alt runs (no vertex
                // glitches; same pattern is implicit). If a
                // future QA report shows quad geometry
                // corruption, the revert is trivial: restore
                // bind_quad_attribs + per-draw enable/disable.
                let stride = 4 * std::mem::size_of::<f32>() as i32;
                gl.enable_vertex_attrib_array(QUAD_ATTRIB_POS);
                gl.vertex_attrib_pointer_f32(
                    QUAD_ATTRIB_POS, 2, glow::FLOAT, false, stride, 0,
                );
                gl.enable_vertex_attrib_array(QUAD_ATTRIB_UV);
                gl.vertex_attrib_pointer_f32(
                    QUAD_ATTRIB_UV,
                    2,
                    glow::FLOAT,
                    false,
                    stride,
                    (2 * std::mem::size_of::<f32>()) as i32,
                );

                let tex = gl.create_texture()
                    .map_err(|e| anyhow!("glGenTextures: {e}"))?;
                gl.bind_texture(glow::TEXTURE_2D, Some(tex));
                gl.tex_parameter_i32(
                    glow::TEXTURE_2D,
                    glow::TEXTURE_MIN_FILTER,
                    glow::NEAREST as i32,
                );
                gl.tex_parameter_i32(
                    glow::TEXTURE_2D,
                    glow::TEXTURE_MAG_FILTER,
                    glow::NEAREST as i32,
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
                let checker = gen_checker(256, 256, 32);
                gl.tex_image_2d(
                    glow::TEXTURE_2D,
                    0,
                    glow::RGB as i32,
                    256,
                    256,
                    0,
                    glow::RGB,
                    glow::UNSIGNED_BYTE,
                    Some(&checker[..]),
                );

                Ok(Presenter {
                    gl,
                    w: w as i32,
                    h: h as i32,
                    step,
                    prog_2d,
                    prog_ext: None,
                    prog_blend: None,
                    vbo,
                    tex_checker: tex,
                    u_tex_loc_2d,
                    u_tex_loc_ext: None,
                    u_tex_a_loc_blend: None,
                    u_tex_b_loc_blend: None,
                    u_alpha_loc_blend: None,
                    video_tex_id: None,
                    video_target: None,
                    video_tex_id_b: None,
                    video_target_b: None,
                    blend_alpha: 0.5,
                })
            }
        }

        /// Update the "current video texture" -- called once per
        /// iteration by kms::run_loop from gst_decode's pull.
        /// Lazily compiles the samplerExternalOES program on
        /// first External-target frame.
        pub fn set_video_texture(
            &mut self,
            tex_id: u32,
            target: crate::gst_decode::TexTarget,
        ) {
            self.video_tex_id = Some(tex_id);
            self.video_target = Some(target);
            if target == crate::gst_decode::TexTarget::External
                && self.prog_ext.is_none()
            {
                if let Err(e) = self.compile_ext_program() {
                    log::error!(
                        "[gl] failed to compile external-OES program: {e:#}"
                    );
                    // Caller will see no draw; gl error escalation
                    // happens in draw_frame's draw branch.
                }
            }
        }

        /// Phase 2: update both video textures + blend alpha.
        /// Lazily compiles prog_blend on first call. Per Phase 2
        /// plan: only the (External, External) target pair is
        /// supported; other combos log an error (vc4 always
        /// returns External, so this is the only path
        /// exercised on FYS).
        pub fn set_video_textures(
            &mut self,
            a: (u32, crate::gst_decode::TexTarget),
            b: (u32, crate::gst_decode::TexTarget),
            alpha: f32,
        ) {
            self.video_tex_id = Some(a.0);
            self.video_target = Some(a.1);
            self.video_tex_id_b = Some(b.0);
            self.video_target_b = Some(b.1);
            self.blend_alpha = alpha;
            if self.prog_blend.is_none() {
                if let Err(e) = self.compile_blend_program() {
                    log::error!(
                        "[gl] failed to compile blend program: {e:#}"
                    );
                }
            }
            // #3 fast-path uses prog_ext for constant-alpha holds
            // (alpha ~= 0 or ~= 1). Compile it eagerly here too --
            // on vc4 both targets are always External, and a fresh
            // SlideshowState starts in HoldingA (alpha=0) so the
            // very first Blend frame hits the fast-path. (Lazy-on-
            // first-fast-path would error frame 0; #3 regression.)
            if (a.1 == crate::gst_decode::TexTarget::External
                || b.1 == crate::gst_decode::TexTarget::External)
                && self.prog_ext.is_none()
            {
                if let Err(e) = self.compile_ext_program() {
                    log::error!(
                        "[gl] failed to compile external-OES program: {e:#}"
                    );
                }
            }
        }

        fn compile_blend_program(&mut self) -> Result<()> {
            unsafe {
                let vs = compile(&self.gl, glow::VERTEX_SHADER, VS)?;
                let fs = compile(
                    &self.gl,
                    glow::FRAGMENT_SHADER,
                    FS_BLEND_EXT_EXT,
                )?;
                let prog = link(&self.gl, vs, fs)?;
                self.gl.delete_shader(vs);
                self.gl.delete_shader(fs);
                let pos_loc = self
                    .gl
                    .get_attrib_location(prog, "a_pos")
                    .ok_or_else(|| anyhow!("blend a_pos missing"))?;
                if pos_loc != QUAD_ATTRIB_POS {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_blend.a_pos failed: \
                         expected slot {QUAD_ATTRIB_POS}, got {pos_loc}"
                    ));
                }
                let uv_loc = self
                    .gl
                    .get_attrib_location(prog, "a_uv")
                    .ok_or_else(|| anyhow!("blend a_uv missing"))?;
                if uv_loc != QUAD_ATTRIB_UV {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_blend.a_uv failed: \
                         expected slot {QUAD_ATTRIB_UV}, got {uv_loc}"
                    ));
                }
                self.u_tex_a_loc_blend = Some(
                    self.gl
                        .get_uniform_location(prog, "u_tex_a")
                        .ok_or_else(|| anyhow!("blend u_tex_a missing"))?,
                );
                self.u_tex_b_loc_blend = Some(
                    self.gl
                        .get_uniform_location(prog, "u_tex_b")
                        .ok_or_else(|| anyhow!("blend u_tex_b missing"))?,
                );
                self.u_alpha_loc_blend = Some(
                    self.gl
                        .get_uniform_location(prog, "u_alpha")
                        .ok_or_else(|| anyhow!("blend u_alpha missing"))?,
                );
                self.prog_blend = Some(prog);
                log::info!("[gl] blend program compiled");
                Ok(())
            }
        }

        fn compile_ext_program(&mut self) -> Result<()> {
            unsafe {
                let vs = compile(&self.gl, glow::VERTEX_SHADER, VS)?;
                let fs =
                    compile(&self.gl, glow::FRAGMENT_SHADER, FS_EXTERNAL_OES)?;
                let prog = link(&self.gl, vs, fs)?;
                self.gl.delete_shader(vs);
                self.gl.delete_shader(fs);
                let pos_loc = self
                    .gl
                    .get_attrib_location(prog, "a_pos")
                    .ok_or_else(|| anyhow!("ext a_pos missing"))?;
                if pos_loc != QUAD_ATTRIB_POS {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_ext.a_pos failed: \
                         expected slot {QUAD_ATTRIB_POS}, got {pos_loc}"
                    ));
                }
                let uv_loc = self
                    .gl
                    .get_attrib_location(prog, "a_uv")
                    .ok_or_else(|| anyhow!("ext a_uv missing"))?;
                if uv_loc != QUAD_ATTRIB_UV {
                    return Err(anyhow!(
                        "bind_attrib_location for prog_ext.a_uv failed: \
                         expected slot {QUAD_ATTRIB_UV}, got {uv_loc}"
                    ));
                }
                self.u_tex_loc_ext = Some(
                    self.gl
                        .get_uniform_location(prog, "u_tex")
                        .ok_or_else(|| anyhow!("ext u_tex missing"))?,
                );
                self.prog_ext = Some(prog);
                log::info!("[gl] external-OES program compiled");
                Ok(())
            }
        }

        pub fn draw_frame(&mut self, frame_idx: u64) -> Result<DrawOutcome> {
            let outcome = unsafe {
                self.gl.viewport(0, 0, self.w, self.h);
                let out = match self.step {
                    Step::Solid => {
                        // Hue cycle: ~6 deg/frame -> 60s per loop @ 60fps.
                        let h = (frame_idx as f32 * 6.0 / 360.0).fract();
                        let (r, g, b) = hsv_to_rgb(h, 0.8, 0.9);
                        self.gl.clear_color(r, g, b, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        DrawOutcome::SolidClear
                    }
                    Step::Checker => {
                        self.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        self.draw_quad_2d(self.tex_checker)?;
                        DrawOutcome::Drew
                    }
                    Step::Video => {
                        self.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        match (self.video_tex_id, self.video_target) {
                            (Some(tex_id), Some(crate::gst_decode::TexTarget::TwoD)) => {
                                self.draw_quad_2d_raw(tex_id)?;
                                DrawOutcome::Drew
                            }
                            (Some(tex_id), Some(crate::gst_decode::TexTarget::External)) => {
                                self.draw_quad_external(tex_id)?;
                                DrawOutcome::Drew
                            }
                            _ => DrawOutcome::Black(BlackReason::NoVideoTex),
                        }
                    }
                    Step::Blend => {
                        self.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        use crate::gst_decode::TexTarget;
                        match (
                            self.video_tex_id,
                            self.video_target,
                            self.video_tex_id_b,
                            self.video_target_b,
                        ) {
                            (
                                Some(a_id),
                                Some(TexTarget::External),
                                Some(b_id),
                                Some(TexTarget::External),
                            ) => {
                                // #3 SOURCE B fast-path: constant-
                                // alpha holds skip the 2-tex mix
                                // and hit the already-compiled
                                // single-tex prog_ext.
                                const ALPHA_EPS: f32 = 0.001;
                                let a = self.blend_alpha;
                                if a <= ALPHA_EPS {
                                    self.draw_quad_external(a_id)?;
                                } else if a >= 1.0 - ALPHA_EPS {
                                    self.draw_quad_external(b_id)?;
                                } else {
                                    self.draw_quad_blend(a_id, b_id, a)?;
                                }
                                DrawOutcome::Drew
                            }
                            (
                                Some(_),
                                Some(a_t),
                                Some(_),
                                Some(b_t),
                            ) if a_t != TexTarget::External
                                || b_t != TexTarget::External =>
                            {
                                // Phase 2 plan: only (External,
                                // External) supported. Other combos
                                // would need a third shader program.
                                if frame_idx % 60 == 0 {
                                    log::warn!(
                                        "[gl] Step::Blend got non-External \
                                         target pair (a={a_t:?} b={b_t:?}); \
                                         skipping draw"
                                    );
                                }
                                DrawOutcome::Black(BlackReason::NonExternalBlend)
                            }
                            _ => {
                                // Classify which slot is missing.
                                // QA wants to know: is it A, B, or
                                // both? That picks the fix target
                                // (gate-fade-start vs alternative).
                                let a_missing = self.video_tex_id.is_none();
                                let b_missing = self.video_tex_id_b.is_none();
                                let reason = match (a_missing, b_missing) {
                                    (true, true) => BlackReason::NoBlendTexBoth,
                                    (true, false) => BlackReason::NoBlendTexA,
                                    (false, true) => BlackReason::NoBlendTexB,
                                    // Both Some but match-arm hit
                                    // _ only if one target is None
                                    // (target=None classified same
                                    // as tex_id=None for detector
                                    // purposes; either way the
                                    // textured overlay was skipped).
                                    (false, false) => BlackReason::NoBlendTexBoth,
                                };
                                DrawOutcome::Black(reason)
                            }
                        }
                    }
                };
                let err = self.gl.get_error();
                if err != glow::NO_ERROR {
                    return Err(anyhow!(
                        "glGetError={err:#x} step={:?} video_tex_id={:?} \
                         video_target={:?}",
                        self.step,
                        self.video_tex_id,
                        self.video_target
                    ));
                }
                out
            };
            Ok(outcome)
        }

        /// Helper: draw fullscreen quad sampling a glow::Texture
        /// (Checker path; owns the texture handle).
        unsafe fn draw_quad_2d(&self, tex: glow::Texture) -> Result<()> {
            // SAFETY: GL context current; tex is a valid handle.
            unsafe { self.draw_quad_2d_inner(Some(tex), 0) }
        }

        /// Helper: draw fullscreen quad sampling a raw u32
        /// texture id (Video TwoD path; gst_decode owns the tex).
        unsafe fn draw_quad_2d_raw(&self, tex_id: u32) -> Result<()> {
            // SAFETY: tex_id was just produced by gst-gl in our
            // shared EGL context; bind_texture-by-id is the GLES2
            // way for non-glow-owned textures.
            unsafe { self.draw_quad_2d_inner(None, tex_id) }
        }

        unsafe fn draw_quad_2d_inner(
            &self,
            owned: Option<glow::Texture>,
            raw_id: u32,
        ) -> Result<()> {
            unsafe {
                // Drain stale GL errors so per-op checks below
                // see only NEW errors caused by this draw's ops.
                gl_drain!(&self.gl);

                self.gl.use_program(Some(self.prog_2d));
                gl_check!(&self.gl, "2d.use_program(prog_2d)");

                self.gl.active_texture(glow::TEXTURE0);
                gl_check!(&self.gl, "2d.active_texture(TEXTURE0)");

                if let Some(t) = owned {
                    self.gl.bind_texture(glow::TEXTURE_2D, Some(t));
                    gl_check!(&self.gl, "2d.bind_texture(TEXTURE_2D, owned)");
                } else {
                    // Bind by raw id: glow::NativeTexture wraps
                    // NonZeroU32; build one to satisfy the API.
                    let nz = std::num::NonZeroU32::new(raw_id)
                        .ok_or_else(|| anyhow!("video tex_id is 0"))?;
                    self.gl
                        .bind_texture(glow::TEXTURE_2D, Some(glow::NativeTexture(nz)));
                    gl_check!(&self.gl, "2d.bind_texture(TEXTURE_2D, raw={raw_id})");
                }

                self.gl.uniform_1_i32(Some(&self.u_tex_loc_2d), 0);
                gl_check!(&self.gl, "2d.uniform_1_i32(u_tex_2d)");

                // #4b: vertex-attrib setup hoisted to
                // Presenter::new (enable + pointer + VBO bind).
                // Slots 0/1 stay enabled for the process
                // lifetime; the attrib slots captured the VBO
                // reference at init via vertex_attrib_pointer.

                self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                gl_check!(&self.gl, "2d.draw_arrays");
                Ok(())
            }
        }

        unsafe fn draw_quad_external(&self, tex_id: u32) -> Result<()> {
            unsafe {
                gl_drain!(&self.gl);

                let prog = self.prog_ext.ok_or_else(|| {
                    anyhow!("external-OES program not compiled yet")
                })?;
                let u_loc = self.u_tex_loc_ext.as_ref().ok_or_else(|| {
                    anyhow!("external-OES u_tex uniform missing")
                })?;

                self.gl.use_program(Some(prog));
                gl_check!(&self.gl, "ext.use_program(prog_ext)");

                self.gl.active_texture(glow::TEXTURE0);
                gl_check!(&self.gl, "ext.active_texture(TEXTURE0)");

                // glow doesn't expose GL_TEXTURE_EXTERNAL_OES; use
                // the literal lifted from the OLD renderer's
                // hdmi.rs.
                use crate::gst_decode::GL_TEXTURE_EXTERNAL_OES;
                let nz = std::num::NonZeroU32::new(tex_id)
                    .ok_or_else(|| anyhow!("video tex_id is 0"))?;
                self.gl.bind_texture(
                    GL_TEXTURE_EXTERNAL_OES,
                    Some(glow::NativeTexture(nz)),
                );
                gl_check!(&self.gl, "ext.bind_texture(EXTERNAL_OES, raw={tex_id})");

                self.gl.uniform_1_i32(Some(u_loc), 0);
                gl_check!(&self.gl, "ext.uniform_1_i32(u_tex_ext)");

                // #4b: vertex-attrib setup hoisted to init.

                self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                gl_check!(&self.gl, "ext.draw_arrays");
                Ok(())
            }
        }

        /// Phase 2 blend draw: bind both EXTERNAL_OES textures to
        /// TEXTURE0 / TEXTURE1, set the 3 uniforms, draw.
        /// Restores TEXTURE0 as the active unit at the end so
        /// subsequent single-tex draws (if Streams ever changed
        /// mid-run, which it doesn't today) start clean.
        unsafe fn draw_quad_blend(
            &self,
            tex_a_id: u32,
            tex_b_id: u32,
            alpha: f32,
        ) -> Result<()> {
            unsafe {
                gl_drain!(&self.gl);

                let prog = self.prog_blend.ok_or_else(|| {
                    anyhow!("blend program not compiled yet")
                })?;
                let u_tex_a = self.u_tex_a_loc_blend.as_ref().ok_or_else(|| {
                    anyhow!("blend u_tex_a missing")
                })?;
                let u_tex_b = self.u_tex_b_loc_blend.as_ref().ok_or_else(|| {
                    anyhow!("blend u_tex_b missing")
                })?;
                let u_alpha = self.u_alpha_loc_blend.as_ref().ok_or_else(|| {
                    anyhow!("blend u_alpha missing")
                })?;

                self.gl.use_program(Some(prog));
                gl_check!(&self.gl, "blend.use_program");

                use crate::gst_decode::GL_TEXTURE_EXTERNAL_OES;
                let nz_a = std::num::NonZeroU32::new(tex_a_id)
                    .ok_or_else(|| anyhow!("blend tex_a_id is 0"))?;
                let nz_b = std::num::NonZeroU32::new(tex_b_id)
                    .ok_or_else(|| anyhow!("blend tex_b_id is 0"))?;

                // Slot 0 = texA
                self.gl.active_texture(glow::TEXTURE0);
                gl_check!(&self.gl, "blend.active_texture(0)");
                self.gl.bind_texture(
                    GL_TEXTURE_EXTERNAL_OES,
                    Some(glow::NativeTexture(nz_a)),
                );
                gl_check!(&self.gl, "blend.bind_texture(EXTERNAL_OES, raw_a={tex_a_id})");
                self.gl.uniform_1_i32(Some(u_tex_a), 0);
                gl_check!(&self.gl, "blend.uniform_1_i32(u_tex_a)");

                // Slot 1 = texB
                self.gl.active_texture(glow::TEXTURE1);
                gl_check!(&self.gl, "blend.active_texture(1)");
                self.gl.bind_texture(
                    GL_TEXTURE_EXTERNAL_OES,
                    Some(glow::NativeTexture(nz_b)),
                );
                gl_check!(&self.gl, "blend.bind_texture(EXTERNAL_OES, raw_b={tex_b_id})");
                self.gl.uniform_1_i32(Some(u_tex_b), 1);
                gl_check!(&self.gl, "blend.uniform_1_i32(u_tex_b)");

                // alpha uniform
                self.gl.uniform_1_f32(Some(u_alpha), alpha);
                gl_check!(&self.gl, "blend.uniform_1_f32(u_alpha)");

                // #4b: vertex-attrib setup hoisted to init.

                self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                gl_check!(&self.gl, "blend.draw_arrays");

                // Restore TEXTURE0 as active so subsequent
                // single-tex paths start clean (defense in depth;
                // every helper sets active_texture(0) itself).
                self.gl.active_texture(glow::TEXTURE0);
                Ok(())
            }
        }

        // #4b: bind_quad_attribs removed. Its 5 FFI calls
        // (bind_buffer + enable*2 + vertex_attrib_pointer*2)
        // happen ONCE in Presenter::new() and are persistent
        // across the process lifetime per GLES2 vertex-array
        // state semantics (the attrib slot captures the VBO at
        // call time; subsequent ARRAY_BUFFER binding doesn't
        // affect it).
    }

    impl Presenter {
        /// glReadPixels the back buffer (the just-drawn frame,
        /// BEFORE eglSwapBuffers) and write a P6 binary PPM to
        /// `path`. Vertically flipped on write so the file rows
        /// run top-to-bottom (matches what's on screen; GL's
        /// glReadPixels returns rows bottom-up).
        ///
        /// PPM is chosen over PNG for zero new deps; QA converts
        /// with `magick foo.ppm foo.png` or
        /// `ffmpeg -i foo.ppm foo.png`.
        /// Content-level black detector (QA pivot after the
        /// None-detector returned 0 black_draws while qarl
        /// reports the flash still visible). Sample a small
        /// region of the back buffer via glReadPixels; report
        /// whether every sampled channel is near-zero.
        ///
        /// Hypothesis being tested: the recreated slot's FIRST
        /// current_frame is a black frame (V4L2/vc4 placeholder
        /// or concat seam buffer). The None-detector sees
        /// Some(frame); a content read sees the actual pixels
        /// rendered.
        ///
        /// CALLER MUST GATE THIS BEHIND --black-detect-content:
        /// glReadPixels is a SYNCHRONOUS GL queue flush (waits
        /// for the GPU to finish all queued work + DMA-readback
        /// from the back buffer); per-frame in production would
        /// regress render_ms. Only call during diagnostic soaks.
        ///
        /// Sample: center NxN region of the back buffer
        /// (default 16x16 = 256 pixels = 1024 bytes RGBA).
        /// Tests R, G, B (alpha ignored) against
        /// NEAR_BLACK_THRESHOLD = 8 (out of 255). Returns true
        /// iff ALL sampled pixels' R+G+B channels are <=
        /// threshold (i.e. uniformly near-black; a black FRAME,
        /// not a black PIXEL in a normal image).
        ///
        /// CAVEAT for QA: clips with genuinely dark CENTERS
        /// (a fade-to-black source, a letterboxed shot framed
        /// on a dark subject) will register as content-black.
        /// For unambiguous results, choose test clips that are
        /// bright-in-the-middle, OR sample multiple non-center
        /// regions and require all of them to be near-black
        /// before flagging.
        ///
        /// Cost when enabled: glReadPixels of 16*16*4 = 1KB
        /// (plus the full pipeline flush). Typical 1-5ms on
        /// vc4 depending on queue depth.
        pub fn sample_back_buffer_is_near_black(&self) -> Result<bool> {
            const SAMPLE_SIDE: i32 = 16;
            const NEAR_BLACK_THRESHOLD: u8 = 8;
            // Center region: clamp in case the surface is
            // smaller than SAMPLE_SIDE (degenerate; not on FYS).
            let cx = (self.w / 2 - SAMPLE_SIDE / 2).max(0);
            let cy = (self.h / 2 - SAMPLE_SIDE / 2).max(0);
            let sw = SAMPLE_SIDE.min(self.w);
            let sh = SAMPLE_SIDE.min(self.h);
            let mut rgba = vec![0u8; (sw * sh * 4) as usize];
            unsafe {
                // SAFETY: GLES2 context current. Buffer sized
                // exactly to the requested region.
                self.gl.read_pixels(
                    cx, cy, sw, sh,
                    glow::RGBA,
                    glow::UNSIGNED_BYTE,
                    glow::PixelPackData::Slice(&mut rgba),
                );
                let err = self.gl.get_error();
                if err != glow::NO_ERROR {
                    return Err(anyhow!(
                        "sample_back_buffer glReadPixels glGetError={err:#x}"
                    ));
                }
            }
            // All-near-black iff every R/G/B byte <= threshold.
            // (Alpha is don't-care: our clear sets alpha=1.0 but
            // a real-content black pixel would too; we compare
            // luma only.)
            let near_black = rgba
                .chunks_exact(4)
                .all(|px| {
                    px[0] <= NEAR_BLACK_THRESHOLD
                        && px[1] <= NEAR_BLACK_THRESHOLD
                        && px[2] <= NEAR_BLACK_THRESHOLD
                });
            Ok(near_black)
        }

        pub fn capture_back_buffer_ppm(
            &self,
            path: &std::path::Path,
        ) -> Result<()> {
            use std::io::Write;
            let w = self.w as usize;
            let h = self.h as usize;
            let mut rgba = vec![0u8; w * h * 4];
            unsafe {
                // SAFETY: GLES2 context is current; back buffer
                // is the GL_BACK draw target by default for our
                // window surface; format/type match what we asked
                // for (the buffer is sized exactly w*h*4).
                self.gl.read_pixels(
                    0,
                    0,
                    self.w,
                    self.h,
                    glow::RGBA,
                    glow::UNSIGNED_BYTE,
                    glow::PixelPackData::Slice(&mut rgba),
                );
                let err = self.gl.get_error();
                if err != glow::NO_ERROR {
                    return Err(anyhow!(
                        "glReadPixels glGetError={err:#x}"
                    ));
                }
            }
            // Build PPM body: row-flip so PPM row 0 = on-screen
            // top row, strip alpha to get RGB.
            let mut body: Vec<u8> = Vec::with_capacity(w * h * 3);
            for screen_row in 0..h {
                let gl_row = h - 1 - screen_row;
                let row_off = gl_row * w * 4;
                for x in 0..w {
                    let p = row_off + x * 4;
                    body.extend_from_slice(&rgba[p..p + 3]);
                }
            }
            let mut f = std::fs::File::create(path)
                .with_context(|| format!("create {}", path.display()))?;
            writeln!(f, "P6")?;
            writeln!(f, "{w} {h}")?;
            writeln!(f, "255")?;
            f.write_all(&body)
                .with_context(|| format!("write PPM body to {}", path.display()))?;
            log::info!(
                "[capture] wrote PPM {} ({}x{}, {} bytes body)",
                path.display(),
                w,
                h,
                body.len(),
            );
            Ok(())
        }
    }

    impl Presenter {
        /// BUG 4 fix: release any GL bindings into gst-managed
        /// textures BEFORE gst pipeline teardown. Called from
        /// main.rs after run_loop returns + before drop(streams).
        ///
        /// SYMPTOM (Phase 3 v1 glass): --capture runs deadlock
        /// at teardown -- "[gst-pull] stop flag set; exiting"
        /// logs then process spins 100% CPU holding /dev/video10
        /// forever. WITHOUT --capture, clean exit. Theory: the
        /// GL state still has TEXTURE0 / TEXTURE1 bound to a
        /// soon-to-be-freed EXTERNAL_OES texture. When the
        /// wrapped GstGLContext finalizes during pipeline NULL,
        /// it walks tracked GL state to clean up; a binding
        /// into a texture whose underlying GLMemory is being
        /// concurrently released by gst-gl's pool finalize can
        /// race -> deadlock or wedge.
        ///
        /// THIS METHOD: unbind both texture units (TEXTURE0 +
        /// TEXTURE1) for both GL_TEXTURE_2D and
        /// GL_TEXTURE_EXTERNAL_OES targets, clear our internal
        /// video tex tracking, glFinish() to drain any pending
        /// GL ops. After this, presenter holds no refs into
        /// gst-managed textures; safe to drop streams.
        ///
        /// CRITICAL for Phase 3 v2: v2's retire+recreate runs
        /// the GstDecoder::Drop path on EVERY clip change.
        /// Without releasing the bindings between cycles, every
        /// crossfade would hang the slideshow. v2 must call
        /// this (or an equivalent) between hold-phase
        /// transitions.
        pub fn release_video_bindings(&mut self) {
            self.video_tex_id = None;
            self.video_target = None;
            self.video_tex_id_b = None;
            self.video_target_b = None;
            unsafe {
                use crate::gst_decode::GL_TEXTURE_EXTERNAL_OES;
                self.gl.active_texture(glow::TEXTURE0);
                self.gl.bind_texture(glow::TEXTURE_2D, None);
                self.gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
                self.gl.active_texture(glow::TEXTURE1);
                self.gl.bind_texture(glow::TEXTURE_2D, None);
                self.gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
                // Restore TEXTURE0 as the active unit.
                self.gl.active_texture(glow::TEXTURE0);
                // glFinish: block until all pending GL ops
                // (including any sync objects gst-gl may have
                // created) complete, so the subsequent
                // gst-gl teardown sees a quiesced context.
                self.gl.finish();
            }
            log::info!(
                "[gl] release_video_bindings: TEXTURE0 + TEXTURE1 \
                 unbound, glFinish complete"
            );
        }
    }

    impl Drop for Presenter {
        fn drop(&mut self) {
            // SAFETY: GLES2 context is still current (EGL drops
            // AFTER Presenter per main.rs ordering); these FFI
            // ops are well-defined object-delete calls.
            unsafe {
                self.gl.delete_texture(self.tex_checker);
                self.gl.delete_buffer(self.vbo);
                self.gl.delete_program(self.prog_2d);
                if let Some(p) = self.prog_ext.take() {
                    self.gl.delete_program(p);
                }
                if let Some(p) = self.prog_blend.take() {
                    self.gl.delete_program(p);
                }
            }
        }
    }

    // #4a: the fn versions of check_gl_err / drain_gl_errors were
    // replaced by the gl_check! / gl_drain! macros at module top.
    // The macros compile to nothing in release (per-op
    // get_error() FFI + heap-allocated op-name strings both gone)
    // and retain identical diagnostic behavior in debug builds.

    unsafe fn compile(
        gl: &glow::Context,
        kind: u32,
        src: &str,
    ) -> Result<glow::Shader> {
        // SAFETY: caller (Presenter::new) holds the current GLES2
        // context; each glow op is FFI into libGLESv2 which is
        // sequenced through the EGL-bound context.
        unsafe {
            let s = gl.create_shader(kind)
                .map_err(|e| anyhow!("glCreateShader: {e}"))?;
            gl.shader_source(s, src);
            gl.compile_shader(s);
            if !gl.get_shader_compile_status(s) {
                let log = gl.get_shader_info_log(s);
                gl.delete_shader(s);
                return Err(anyhow!("shader compile: {log}"));
            }
            Ok(s)
        }
    }

    unsafe fn link(
        gl: &glow::Context,
        vs: glow::Shader,
        fs: glow::Shader,
    ) -> Result<glow::Program> {
        // SAFETY: same as compile() — context is current.
        unsafe {
            let p = gl.create_program()
                .map_err(|e| anyhow!("glCreateProgram: {e}"))?;
            gl.attach_shader(p, vs);
            gl.attach_shader(p, fs);
            // #4b: pin "a_pos" and "a_uv" to fixed attrib slots
            // 0 and 1 BEFORE link. All three programs (2d /
            // ext / blend) use the same VS with the same
            // attribute names, so all three end up at identical
            // slot indices. That lets us set up
            // enable_vertex_attrib_array + vertex_attrib_pointer
            // ONCE at init and skip the per-frame bind_quad_attribs
            // churn (5 FFI/draw) + 2 disable_vertex_attrib_array
            // (2 FFI/draw). bind_attrib_location must be called
            // BEFORE link_program -- after link it's ignored.
            gl.bind_attrib_location(p, QUAD_ATTRIB_POS, "a_pos");
            gl.bind_attrib_location(p, QUAD_ATTRIB_UV, "a_uv");
            gl.link_program(p);
            if !gl.get_program_link_status(p) {
                let log = gl.get_program_info_log(p);
                gl.delete_program(p);
                return Err(anyhow!("program link: {log}"));
            }
            Ok(p)
        }
    }

    fn gen_checker(w: usize, h: usize, square: usize) -> Vec<u8> {
        let mut buf = Vec::with_capacity(w * h * 3);
        for y in 0..h {
            for x in 0..w {
                let on = ((x / square) + (y / square)) & 1 == 1;
                let v = if on { 255 } else { 0 };
                buf.extend_from_slice(&[v, v, v]);
            }
        }
        buf
    }

    fn hsv_to_rgb(h: f32, s: f32, v: f32) -> (f32, f32, f32) {
        let c = v * s;
        let h6 = h * 6.0;
        let x = c * (1.0 - ((h6 % 2.0) - 1.0).abs());
        let m = v - c;
        let (r1, g1, b1) = match h6 as i32 {
            0 => (c, x, 0.0),
            1 => (x, c, 0.0),
            2 => (0.0, c, x),
            3 => (0.0, x, c),
            4 => (x, 0.0, c),
            _ => (c, 0.0, x),
        };
        (r1 + m, g1 + m, b1 + m)
    }

    /// Convert an f32 slice to a u8 slice for glBufferData. No
    /// alignment issues on common targets; this is just a
    /// reinterpret without copying. We do NOT pull in bytemuck for
    /// 6 lines of code.
    fn bytemuck_quad(q: &[f32; 24]) -> &[u8] {
        // SAFETY: f32 -> u8 reinterpret over a fixed-size array;
        // alignment of u8 is 1 (always <= f32 alignment); length is
        // exact bytes.
        unsafe {
            std::slice::from_raw_parts(
                q.as_ptr() as *const u8,
                std::mem::size_of_val(q),
            )
        }
    }
}
