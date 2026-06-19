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
        pub fn draw_frame(&mut self, _frame_idx: u64) -> Result<()> {
            anyhow::bail!("draw_frame stub: Linux only")
        }
        pub fn capture_back_buffer_ppm(&self, _path: &std::path::Path) -> Result<()> {
            anyhow::bail!("capture stub: Linux only")
        }
        pub fn set_video_texture(
            &mut self,
            _tex_id: u32,
            _target: crate::gst_decode::TexTarget,
        ) {
        }
    }
}

#[cfg(target_os = "linux")]
mod linux {
    use super::*;
    use crate::egl_gbm::Egl;
    use anyhow::{anyhow, Context};
    use glow::HasContext;

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
        vbo: glow::Buffer,
        /// CPU checkerboard for Step::Checker.
        tex_checker: glow::Texture,
        /// Pos/UV attribute locations on prog_2d (same indices on
        /// prog_ext because the VS is identical and uses bind-by-name).
        a_pos_loc_2d: u32,
        a_uv_loc_2d: u32,
        u_tex_loc_2d: glow::UniformLocation,
        /// On prog_ext (Some when compiled).
        a_pos_loc_ext: u32,
        a_uv_loc_ext: u32,
        u_tex_loc_ext: Option<glow::UniformLocation>,
        /// Phase-1 state: the latest video texture id + its target.
        /// Updated by set_video_texture each iteration.
        video_tex_id: Option<u32>,
        video_target: Option<crate::gst_decode::TexTarget>,
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
                let a_pos_loc_2d = gl
                    .get_attrib_location(prog_2d, "a_pos")
                    .ok_or_else(|| anyhow!("attribute a_pos missing"))?;
                let a_uv_loc_2d = gl
                    .get_attrib_location(prog_2d, "a_uv")
                    .ok_or_else(|| anyhow!("attribute a_uv missing"))?;
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
                    vbo,
                    tex_checker: tex,
                    a_pos_loc_2d,
                    a_uv_loc_2d,
                    u_tex_loc_2d,
                    a_pos_loc_ext: 0,
                    a_uv_loc_ext: 0,
                    u_tex_loc_ext: None,
                    video_tex_id: None,
                    video_target: None,
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

        fn compile_ext_program(&mut self) -> Result<()> {
            unsafe {
                let vs = compile(&self.gl, glow::VERTEX_SHADER, VS)?;
                let fs =
                    compile(&self.gl, glow::FRAGMENT_SHADER, FS_EXTERNAL_OES)?;
                let prog = link(&self.gl, vs, fs)?;
                self.gl.delete_shader(vs);
                self.gl.delete_shader(fs);
                self.a_pos_loc_ext = self
                    .gl
                    .get_attrib_location(prog, "a_pos")
                    .ok_or_else(|| anyhow!("ext a_pos missing"))?;
                self.a_uv_loc_ext = self
                    .gl
                    .get_attrib_location(prog, "a_uv")
                    .ok_or_else(|| anyhow!("ext a_uv missing"))?;
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

        pub fn draw_frame(&mut self, frame_idx: u64) -> Result<()> {
            unsafe {
                self.gl.viewport(0, 0, self.w, self.h);
                match self.step {
                    Step::Solid => {
                        // Hue cycle: ~6 deg/frame -> 60s per loop @ 60fps.
                        let h = (frame_idx as f32 * 6.0 / 360.0).fract();
                        let (r, g, b) = hsv_to_rgb(h, 0.8, 0.9);
                        self.gl.clear_color(r, g, b, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                    }
                    Step::Checker => {
                        self.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        self.draw_quad_2d(self.tex_checker)?;
                    }
                    Step::Video => {
                        self.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                        self.gl.clear(glow::COLOR_BUFFER_BIT);
                        match (self.video_tex_id, self.video_target) {
                            (Some(tex_id), Some(crate::gst_decode::TexTarget::TwoD)) => {
                                self.draw_quad_2d_raw(tex_id)?;
                            }
                            (Some(tex_id), Some(crate::gst_decode::TexTarget::External)) => {
                                self.draw_quad_external(tex_id)?;
                            }
                            _ => {
                                // No video frame yet (first iteration
                                // before pull_first_texture completes,
                                // or a transient pull-miss). Just the
                                // black clear; render loop will catch up.
                                if frame_idx % 60 == 0 {
                                    log::debug!(
                                        "[gl] Step::Video draw with no tex; \
                                         showing black for frame {frame_idx}"
                                    );
                                }
                            }
                        }
                    }
                }
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
            }
            Ok(())
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
                self.gl.use_program(Some(self.prog_2d));
                self.gl.active_texture(glow::TEXTURE0);
                if let Some(t) = owned {
                    self.gl.bind_texture(glow::TEXTURE_2D, Some(t));
                } else {
                    // Bind by raw id: glow::NativeTexture wraps
                    // NonZeroU32; build one to satisfy the API.
                    let nz = std::num::NonZeroU32::new(raw_id)
                        .ok_or_else(|| anyhow!("video tex_id is 0"))?;
                    self.gl
                        .bind_texture(glow::TEXTURE_2D, Some(glow::NativeTexture(nz)));
                }
                self.gl.uniform_1_i32(Some(&self.u_tex_loc_2d), 0);
                self.bind_quad_attribs(self.a_pos_loc_2d, self.a_uv_loc_2d)?;
                self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                self.gl.disable_vertex_attrib_array(self.a_pos_loc_2d);
                self.gl.disable_vertex_attrib_array(self.a_uv_loc_2d);
                Ok(())
            }
        }

        unsafe fn draw_quad_external(&self, tex_id: u32) -> Result<()> {
            unsafe {
                let prog = self.prog_ext.ok_or_else(|| {
                    anyhow!("external-OES program not compiled yet")
                })?;
                let u_loc = self.u_tex_loc_ext.as_ref().ok_or_else(|| {
                    anyhow!("external-OES u_tex uniform missing")
                })?;
                self.gl.use_program(Some(prog));
                self.gl.active_texture(glow::TEXTURE0);
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
                self.gl.uniform_1_i32(Some(u_loc), 0);
                self.bind_quad_attribs(self.a_pos_loc_ext, self.a_uv_loc_ext)?;
                self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                self.gl.disable_vertex_attrib_array(self.a_pos_loc_ext);
                self.gl.disable_vertex_attrib_array(self.a_uv_loc_ext);
                Ok(())
            }
        }

        unsafe fn bind_quad_attribs(
            &self,
            a_pos: u32,
            a_uv: u32,
        ) -> Result<()> {
            unsafe {
                self.gl.bind_buffer(glow::ARRAY_BUFFER, Some(self.vbo));
                let stride = 4 * std::mem::size_of::<f32>() as i32;
                self.gl.enable_vertex_attrib_array(a_pos);
                self.gl
                    .vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, stride, 0);
                self.gl.enable_vertex_attrib_array(a_uv);
                self.gl.vertex_attrib_pointer_f32(
                    a_uv,
                    2,
                    glow::FLOAT,
                    false,
                    stride,
                    (2 * std::mem::size_of::<f32>()) as i32,
                );
                Ok(())
            }
        }
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
            }
        }
    }

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
