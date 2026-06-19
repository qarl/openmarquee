//! GLES2 presenter: compiles a tiny full-screen-quad shader,
//! uploads a static 256x256 checkerboard texture, and draws either
//! a hue-cycling solid color (Step A) or the texture (Step B) per
//! frame.

use anyhow::Result;

#[derive(Clone, Copy, Debug, clap::ValueEnum)]
pub enum Step {
    Solid,
    Checker,
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

    const FS: &str = r#"
        precision mediump float;
        varying vec2 v_uv;
        uniform sampler2D u_tex;
        void main() {
            gl_FragColor = texture2D(u_tex, v_uv);
        }
    "#;

    pub struct Presenter {
        gl: glow::Context,
        w: i32,
        h: i32,
        step: Step,
        prog: glow::Program,
        vbo: glow::Buffer,
        tex: glow::Texture,
        a_pos_loc: u32,
        a_uv_loc: u32,
        u_tex_loc: glow::UniformLocation,
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
                let fs = compile(&gl, glow::FRAGMENT_SHADER, FS)?;
                let prog = link(&gl, vs, fs)?;
                gl.delete_shader(vs);
                gl.delete_shader(fs);

                gl.use_program(Some(prog));
                let a_pos_loc = gl
                    .get_attrib_location(prog, "a_pos")
                    .ok_or_else(|| anyhow!("attribute a_pos missing"))?;
                let a_uv_loc = gl
                    .get_attrib_location(prog, "a_uv")
                    .ok_or_else(|| anyhow!("attribute a_uv missing"))?;
                let u_tex_loc = gl
                    .get_uniform_location(prog, "u_tex")
                    .ok_or_else(|| anyhow!("uniform u_tex missing"))?;
                gl.uniform_1_i32(Some(&u_tex_loc), 0);

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
                    gl, w: w as i32, h: h as i32, step,
                    prog, vbo, tex, a_pos_loc, a_uv_loc, u_tex_loc,
                })
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
                        self.gl.use_program(Some(self.prog));
                        self.gl.active_texture(glow::TEXTURE0);
                        self.gl.bind_texture(
                            glow::TEXTURE_2D,
                            Some(self.tex),
                        );
                        self.gl.uniform_1_i32(Some(&self.u_tex_loc), 0);
                        self.gl.bind_buffer(
                            glow::ARRAY_BUFFER,
                            Some(self.vbo),
                        );
                        let stride = 4 * std::mem::size_of::<f32>() as i32;
                        self.gl.enable_vertex_attrib_array(self.a_pos_loc);
                        self.gl.vertex_attrib_pointer_f32(
                            self.a_pos_loc, 2, glow::FLOAT,
                            false, stride, 0,
                        );
                        self.gl.enable_vertex_attrib_array(self.a_uv_loc);
                        self.gl.vertex_attrib_pointer_f32(
                            self.a_uv_loc, 2, glow::FLOAT,
                            false, stride,
                            (2 * std::mem::size_of::<f32>()) as i32,
                        );
                        self.gl.draw_arrays(glow::TRIANGLES, 0, 6);
                        self.gl.disable_vertex_attrib_array(self.a_pos_loc);
                        self.gl.disable_vertex_attrib_array(self.a_uv_loc);
                    }
                }
                let err = self.gl.get_error();
                if err != glow::NO_ERROR {
                    return Err(anyhow!("glGetError={err:#x}"));
                }
            }
            Ok(())
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
                self.gl.delete_texture(self.tex);
                self.gl.delete_buffer(self.vbo);
                self.gl.delete_program(self.prog);
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
