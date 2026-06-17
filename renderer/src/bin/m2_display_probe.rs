//! M2 — display-path probe: drive A(live) + B(warm-park-resume) THROUGH
//! a minimal `with_egl_session`-style EGL bring-up + the real NV12 DMABUF
//! EGLImage-import + external-OES blit, then run the shared pixel oracle
//! on `glReadPixels` of the back buffer (NOT the decoder Y-plane).
//!
//! ## Why this exists
//!
//! Per QA's M0/M1 dispatch (2026-06-17): M0 ruled out single-decoder
//! park-resume as broken; M1 ruled out two-decoder codec contention. The
//! `r76` "endpoint_b zero frames" freeze, if it still reproduces, must
//! therefore live in the **GL-bake / paint-thread / swap** layer that
//! M0+M1 don't exercise.
//!
//! M2 closes that gap: drives the SAME EGL setup + the SAME EGLImage
//! import + the SAME NV12 → external-OES → blit pipeline that
//! `paint_and_present_one_video_slide_frame` runs in prod, on REAL HDMI
//! output (KMS scanout), and asserts that B's PAINTED-AND-SWAPPED
//! pixels match its DECODED pixels.
//!
//! The bullseye is the divergence: B's decoder oracle PASSES (fresh,
//! distinct, non-black Y-plane) but the screen oracle FAILS (`glReadPixels`
//! returns black or stuck pixels at B's painted region). That is the r76
//! fingerprint, reproduced minimal+measured.
//!
//! ## Faithfulness — lifted verbatim from `hdmi.rs` 2026-06-17
//!
//! Per QA's "import-not-reinvent" Q1 confirmation + the faithfulness
//! guardrail: M2 must exercise the REAL GL environment (same EGL config /
//! same shaders / same FBO setup) — a thin reinvented bring-up could
//! accidentally dodge the very thing that breaks prod. So the following
//! were COPIED verbatim from `hdmi.rs` at the branch tip on 2026-06-17
//! (must stay in sync if prod evolves):
//!
//! - `with_egl_session`'s EGL bring-up body (hdmi.rs:619-754) — DRM open,
//!   GBM surface, EGL display+config+context, MakeCurrent, swap_interval.
//! - `commit_fb`'s modeset + page-flip path (hdmi.rs:1220-...) — set_crtc
//!   on first commit, async page_flip + event drain on subsequent.
//! - `dma_buf_egl_entry_points` (hdmi.rs:12830-...) — eglCreateImageKHR /
//!   eglDestroyImageKHR / glEGLImageTargetTexture2DOES resolution.
//! - `run_nv12_dmabuf_blit_pass` body (hdmi.rs:13109-...) — EGLImage
//!   attribs, external-OES texture create + bind, shader compile + draw.
//! - `FS_NV12_DMABUF_TO_RGB` shader (hdmi_logic.rs:3561) — the actual
//!   NV12-to-RGB sampler (Mesa fast-path on vc4).
//! - `VS_TEXTURED_QUAD` (hdmi_logic.rs:342) — interleaved [x,y,u,v]
//!   vertex shader.
//!
//! NO new EGL config attribs, NO new shader variants, NO new FBO/texture
//! lifecycle.
//!
//! ## Q3 dual-bake adjustment (per QA dispatch)
//!
//! During the resume window, BOTH A and B are baked per tick and
//! composited (split-screen vertically — left half = A, right half = B).
//! Reason: the r76 freeze was specifically dual-bake-per-tick on the
//! single paint thread; A-only-then-B-only would under-test that exact
//! stress. A loops past its sample-count via `next_sample_idx %= len`
//! to stay live through B's resume window (hygiene fix for M1's
//! "DEGRADED at park 5000/10000" artifact).
//!
//! ## Per-tick log + final VERDICT (M0/M1-style)
//!
//! Per-tick: `[m2] tick=N a_dec_ok=Y/N a_scr_ok=Y/N b_dec_ok=Y/N
//! b_scr_ok=Y/N` so a sub-agent can replay the trajectory.
//!
//! Final (matches M0/M1 grep-uniform sweep format):
//! `[m2] PARK_MS=N b_decoder_ok=NN/NN b_screen_ok=NN/NN a_screen_ok=NN/NN
//! screen_divergence=N egl_import_errno="..." paint_stall_us=N
//! a_black_on_b_bake=bool VERDICT=HEALTHY|DIVERGENT|WEDGED`
//!
//! ## Run recipe (fireplacesign, backend stopped, manual)
//!
//!   sudo systemctl stop openmarquee-backend
//!   for park_ms in 50 200 1000 5000 10000; do
//!     M0_PARK_MS=$park_ms \
//!     M1_VIDEO_A=/var/openmarquee/content/<uuidA>/asset.mp4 \
//!     M1_VIDEO_B=/var/openmarquee/content/<uuidB>/asset.mp4 \
//!     /usr/local/bin/m2-display-probe
//!   done
//!   sudo systemctl start openmarquee-backend  # restore
//!
//! Branch base: `task/frame-phase-instrument-2026-06-16` (fc8d8c2,
//! co-located with M0/M1).

#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_DEFAULT: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_FOR_PRELOAD: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_DEFAULT: usize = PRIME_WARMUP_DEFAULT + 1;
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_FOR_PRELOAD: usize = PRIME_WARMUP_FOR_PRELOAD + 2;

#[cfg(target_os = "linux")]
#[path = "../v4l2.rs"]
mod v4l2;
#[cfg(target_os = "linux")]
#[path = "../mp4_demux.rs"]
mod mp4_demux;
#[cfg(target_os = "linux")]
#[path = "../video_decode.rs"]
mod video_decode;
#[cfg(target_os = "linux")]
#[path = "../frame_pacing.rs"]
mod frame_pacing;
#[cfg(target_os = "linux")]
#[path = "../probe_oracle.rs"]
mod probe_oracle;

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("m2-display-probe: Linux-only (V4L2 + DRM/KMS + EGL).");
    std::process::exit(1);
}

#[cfg(target_os = "linux")]
mod linux_main {
    use anyhow::{anyhow, bail, Context, Result};
    use std::ffi::c_void;
    use std::os::fd::{AsFd, BorrowedFd};
    use std::ptr;
    use std::time::{Duration, Instant};

    use drm::buffer::{Buffer as DrmBuffer, DrmFourcc, Handle as DrmHandle};
    use drm::control::{connector, framebuffer, Device as ControlDevice};
    use drm::Device as BaseDevice;
    use gbm::{AsRaw, BufferObject, BufferObjectFlags, Format as GbmFormat};
    use glow::HasContext;
    use khronos_egl as egl;

    use super::{frame_pacing, mp4_demux, probe_oracle, v4l2, video_decode};

    // ------------------------------------------------------------------
    // Lifted constants — verbatim from hdmi.rs 2026-06-17.
    // ------------------------------------------------------------------
    const EGL_LINUX_DMA_BUF_EXT: u32 = 0x3270;
    const EGL_LINUX_DRM_FOURCC_EXT: i32 = 0x3271;
    const EGL_DMA_BUF_PLANE0_FD_EXT: i32 = 0x3272;
    const EGL_DMA_BUF_PLANE0_OFFSET_EXT: i32 = 0x3273;
    const EGL_DMA_BUF_PLANE0_PITCH_EXT: i32 = 0x3274;
    const EGL_DMA_BUF_PLANE1_FD_EXT: i32 = 0x3275;
    const EGL_DMA_BUF_PLANE1_OFFSET_EXT: i32 = 0x3276;
    const EGL_DMA_BUF_PLANE1_PITCH_EXT: i32 = 0x3277;
    const EGL_NONE_ATTR: i32 = 0x3038;
    const DRM_FORMAT_NV12: i32 = 0x3231564E;
    const GL_TEXTURE_EXTERNAL_OES: u32 = 0x8D65;

    // ------------------------------------------------------------------
    // Card wrapper — same shape main.rs uses. Just a File + the
    // drm trait impls.
    // ------------------------------------------------------------------
    struct Card(std::fs::File);
    impl AsFd for Card {
        fn as_fd(&self) -> BorrowedFd<'_> { self.0.as_fd() }
    }
    impl BaseDevice for Card {}
    impl ControlDevice for Card {}
    impl Card {
        fn open(path: &str) -> Result<Self> {
            let f = std::fs::OpenOptions::new()
                .read(true).write(true).open(path)
                .with_context(|| format!("open {}", path))?;
            Ok(Card(f))
        }
    }

    /// Minimal GBM-buffer→DRM-framebuffer adapter, mirroring the
    /// shape of hdmi.rs::GbmBufferAdapter (lines 15873-15913) so
    /// `card.add_framebuffer` accepts the BO. Same fields, same
    /// fourcc translation pattern. Kept inline to avoid pulling in
    /// hdmi.rs's full module tree.
    struct GbmFb {
        width: u32, height: u32, format: DrmFourcc, pitch: u32, handle: DrmHandle,
    }
    impl GbmFb {
        fn new(bo: &BufferObject<()>) -> Result<Self> {
            let width = bo.width().context("gbm bo width")?;
            let height = bo.height().context("gbm bo height")?;
            let stride = bo.stride().context("gbm bo stride")?;
            let gbm_fmt = bo.format().context("gbm bo format")?;
            // Map gbm::Format → fourcc bytes → DrmFourcc. We only
            // need Argb8888 for the M2 probe; bail out clearly if
            // gbm decided on something else (shouldn't happen with
            // the BufferObjectFlags::SCANOUT we pass).
            let fourcc = match gbm_fmt {
                GbmFormat::Argb8888 => DrmFourcc::Argb8888,
                GbmFormat::Xrgb8888 => DrmFourcc::Xrgb8888,
                other => bail!("unexpected gbm format {:?} (only Argb/Xrgb8888 supported in probe)", other),
            };
            let bo_handle = bo.handle().context("gbm bo handle")?;
            let raw_handle = unsafe { bo_handle.u32_ };
            let handle = DrmHandle::from(
                std::num::NonZeroU32::new(raw_handle)
                    .ok_or_else(|| anyhow!("gbm bo handle was 0"))?,
            );
            Ok(Self { width, height, format: fourcc, pitch: stride, handle })
        }
    }
    impl DrmBuffer for GbmFb {
        fn size(&self) -> (u32, u32) { (self.width, self.height) }
        fn format(&self) -> DrmFourcc { self.format }
        fn pitch(&self) -> u32 { self.pitch }
        fn handle(&self) -> DrmHandle { self.handle }
    }

    // ------------------------------------------------------------------
    // Lifted: dma_buf EGL entry-point resolution. Verbatim from
    // hdmi.rs:12830-2900-ish — minus the thread_local cache (we
    // resolve once at session bring-up + thread the handle through).
    // ------------------------------------------------------------------
    #[derive(Copy, Clone)]
    struct DmaBufEglEps {
        create_image: unsafe extern "C" fn(
            dpy: *mut c_void,
            ctx: *mut c_void,
            target: u32,
            buffer: *mut c_void,
            attrib_list: *const i32,
        ) -> *mut c_void,
        destroy_image: unsafe extern "C" fn(dpy: *mut c_void, image: *mut c_void) -> u32,
        image_target_texture_2d: unsafe extern "C" fn(target: u32, image: *mut c_void),
    }

    fn resolve_dma_buf_egl_eps(
        egl_lib: &egl::DynamicInstance<egl::EGL1_5>,
        display: egl::Display,
        gl: &glow::Context,
    ) -> Option<DmaBufEglEps> {
        let egl_exts = egl_lib
            .query_string(Some(display), egl::EXTENSIONS)
            .ok()
            .and_then(|s| s.to_str().ok().map(str::to_string))
            .unwrap_or_default();
        if !egl_exts.contains("EGL_EXT_image_dma_buf_import") {
            eprintln!("[m2] WARN: EGL extension EGL_EXT_image_dma_buf_import missing");
            return None;
        }
        let gles_exts = unsafe { gl.get_parameter_string(glow::EXTENSIONS) };
        if !gles_exts.contains("GL_OES_EGL_image_external") {
            eprintln!("[m2] WARN: GLES extension GL_OES_EGL_image_external missing");
            return None;
        }
        let create_image = egl_lib.get_proc_address("eglCreateImageKHR")?;
        let destroy_image = egl_lib.get_proc_address("eglDestroyImageKHR")?;
        let image_target_tex = egl_lib.get_proc_address("glEGLImageTargetTexture2DOES")?;
        unsafe {
            Some(DmaBufEglEps {
                create_image: std::mem::transmute(create_image),
                destroy_image: std::mem::transmute(destroy_image),
                image_target_texture_2d: std::mem::transmute(image_target_tex),
            })
        }
    }

    // ------------------------------------------------------------------
    // Lifted: NV12 DMABUF → RGB shader. Verbatim from hdmi_logic.rs.
    // ------------------------------------------------------------------
    const VS_TEXTURED_QUAD: &str = r#"#version 100
attribute vec2 a_pos;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() {
    v_uv = a_uv;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"#;
    const FS_NV12_DMABUF_TO_RGB: &str = r#"#version 100
#extension GL_OES_EGL_image_external : require
precision mediump float;
uniform samplerExternalOES u_tex_external;
uniform float u_y_crop_max;
varying vec2 v_uv;
void main() {
    vec2 uv_t = vec2(v_uv.x, (1.0 - v_uv.y) * u_y_crop_max);
    vec3 rgb = texture2D(u_tex_external, uv_t).rgb;
    gl_FragColor = vec4(rgb, 1.0);
}
"#;

    struct BlitProgram {
        program: glow::NativeProgram,
        u_tex_external: glow::NativeUniformLocation,
        u_y_crop_max: glow::NativeUniformLocation,
        a_pos: u32,
        a_uv: u32,
    }

    fn compile_shader(gl: &glow::Context, kind: u32, source: &str) -> Result<glow::NativeShader> {
        unsafe {
            let shader = gl.create_shader(kind).map_err(|e| anyhow!("glCreateShader: {e}"))?;
            gl.shader_source(shader, source);
            gl.compile_shader(shader);
            if !gl.get_shader_compile_status(shader) {
                let log = gl.get_shader_info_log(shader);
                gl.delete_shader(shader);
                bail!("shader compile failed: {log}");
            }
            Ok(shader)
        }
    }

    fn build_nv12_blit_program(gl: &glow::Context) -> Result<BlitProgram> {
        unsafe {
            let vs = compile_shader(gl, glow::VERTEX_SHADER, VS_TEXTURED_QUAD)?;
            let fs = compile_shader(gl, glow::FRAGMENT_SHADER, FS_NV12_DMABUF_TO_RGB)?;
            let program = gl.create_program().map_err(|e| anyhow!("glCreateProgram: {e}"))?;
            gl.attach_shader(program, vs);
            gl.attach_shader(program, fs);
            gl.bind_attrib_location(program, 0, "a_pos");
            gl.bind_attrib_location(program, 1, "a_uv");
            gl.link_program(program);
            if !gl.get_program_link_status(program) {
                let log = gl.get_program_info_log(program);
                bail!("program link failed: {log}");
            }
            let u_tex_external = gl
                .get_uniform_location(program, "u_tex_external")
                .ok_or_else(|| anyhow!("missing uniform u_tex_external"))?;
            let u_y_crop_max = gl
                .get_uniform_location(program, "u_y_crop_max")
                .ok_or_else(|| anyhow!("missing uniform u_y_crop_max"))?;
            gl.delete_shader(vs);
            gl.delete_shader(fs);
            Ok(BlitProgram {
                program,
                u_tex_external,
                u_y_crop_max,
                a_pos: 0,
                a_uv: 1,
            })
        }
    }

    /// Build the full-screen-quad VBO. 4 verts of interleaved [x,y,u,v]
    /// in TRIANGLE_STRIP order. Same shape as hdmi_logic::cover_quad_vbo
    /// but without the cover-fit math (M2's split-screen viewport
    /// handles the panel-aspect half assignment).
    fn full_quad_vbo(gl: &glow::Context) -> Result<glow::NativeBuffer> {
        // TRIANGLE_STRIP: (bl, br, tl, tr) — pos then uv per vert.
        // Note: FS flips v internally to handle V4L2 bottom-up, so we
        // use the standard 0..1 UV range here. Same convention as the
        // hdmi.rs cover_quad_vbo path.
        let verts: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        unsafe {
            let vbo = gl.create_buffer().map_err(|e| anyhow!("glGenBuffers: {e}"))?;
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                verts.as_ptr() as *const u8,
                std::mem::size_of_val(&verts),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
            Ok(vbo)
        }
    }

    /// Lifted from `run_nv12_dmabuf_blit_pass` (hdmi.rs:13109+) — minus
    /// the cached_texture spike-kill (M2 uses per-frame create+delete
    /// since we're not optimizing for steady-state perf) and the
    /// EGLImage cache (M2 imports per-frame; matches the pre-r101
    /// kill-switch path). The VIEWPORT is set by the caller; this
    /// function just runs the blit pipeline.
    ///
    /// SAFETY: caller guarantees EGL context is current + the bound
    /// GL framebuffer is appropriate (default fb in M2).
    unsafe fn blit_nv12_dmabuf_to_viewport(
        gl: &glow::Context,
        egl_lib: &egl::DynamicInstance<egl::EGL1_5>,
        display: egl::Display,
        eps: DmaBufEglEps,
        program: &BlitProgram,
        vbo: glow::NativeBuffer,
        fd: std::os::fd::RawFd,
        width: u32,
        height: u32,
        stride: u32,
        y_crop_max: f32,
        site: &str,
    ) -> Result<()> {
        // Helper to check + report glGetError immediately after a
        // suspected source. Returns the error code so caller can
        // log + decide.
        let check_gl_err = |label: &str| -> u32 {
            let err = gl.get_error();
            if err != 0 {
                eprintln!("[m2] WARN gl_err=0x{err:x} at {site}/{label}");
            }
            err
        };
        let y_size: i32 = (stride as i32) * (height as i32);
        // EGL attribute list — verbatim from hdmi.rs:13186.
        let attribs: [i32; 20] = [
            0x3057, width as i32,   // EGL_WIDTH
            0x3056, height as i32,  // EGL_HEIGHT
            EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_NV12,
            EGL_DMA_BUF_PLANE0_FD_EXT, fd,
            EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
            EGL_DMA_BUF_PLANE0_PITCH_EXT, stride as i32,
            EGL_DMA_BUF_PLANE1_FD_EXT, fd,
            EGL_DMA_BUF_PLANE1_OFFSET_EXT, y_size,
            EGL_DMA_BUF_PLANE1_PITCH_EXT, stride as i32,
            EGL_NONE_ATTR,
            0,
        ];
        let egl_image = (eps.create_image)(
            display.as_ptr(),
            ptr::null_mut(),
            EGL_LINUX_DMA_BUF_EXT,
            ptr::null_mut(),
            attribs.as_ptr(),
        );
        if egl_image.is_null() {
            // Capture the EGL error per QA's "house rule": grep the EGL
            // error code so the sub-agent has the exact errno.
            let err = egl_lib.get_error().map(|e| format!("{e:?}")).unwrap_or_else(|| "no-error".into());
            bail!(
                "eglCreateImageKHR(LINUX_DMA_BUF, fd={fd}, w={width}, h={height}, stride={stride}) -> EGL_NO_IMAGE (egl_err={err})"
            );
        }
        let tex = gl.create_texture().map_err(|e| {
            (eps.destroy_image)(display.as_ptr(), egl_image);
            anyhow!("glGenTextures(external-OES): {e}")
        })?;
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, Some(tex));
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        (eps.image_target_texture_2d)(GL_TEXTURE_EXTERNAL_OES, egl_image);
        check_gl_err("after_image_target_texture_2d");
        // Shader + draw.
        gl.use_program(Some(program.program));
        gl.uniform_1_i32(Some(&program.u_tex_external), 0);
        gl.uniform_1_f32(Some(&program.u_y_crop_max), y_crop_max);
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        gl.enable_vertex_attrib_array(program.a_pos);
        gl.vertex_attrib_pointer_f32(program.a_pos, 2, glow::FLOAT, false, 16, 0);
        gl.enable_vertex_attrib_array(program.a_uv);
        gl.vertex_attrib_pointer_f32(program.a_uv, 2, glow::FLOAT, false, 16, 8);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        check_gl_err("after_draw_arrays");
        // M2 wiring-fix (2026-06-17 post first-bench DIVERGENT-all-black
        // diagnosis): force the GPU to actually CONSUME the draw before
        // we tear down the texture + EGLImage. Prod's hot path keeps
        // both cached across frames (c06340b spike-kill + r101 cache),
        // so the per-frame create/destroy path historically deferred
        // texture+image cleanup via cache eviction. M2 lacks the cache,
        // so the draw can be queued while the texture+image get deleted
        // BEFORE the GPU touches them — vc4 then renders BLACK because
        // its draw-side reference is gone. gl.finish() blocks until the
        // GPU completes the queued draws, making the per-frame
        // create/destroy safe (slower per blit by ~5-15 ms but
        // correctness > throughput for a probe).
        gl.finish();
        gl.disable_vertex_attrib_array(program.a_pos);
        gl.disable_vertex_attrib_array(program.a_uv);
        // Teardown — order matters (texture → EGLImage).
        gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
        gl.delete_texture(tex);
        let destroyed = (eps.destroy_image)(display.as_ptr(), egl_image);
        if destroyed == 0 {
            eprintln!("[m2] warn: eglDestroyImageKHR returned EGL_FALSE for fd={fd}");
        }
        Ok(())
    }

    /// `glReadPixels` an RGBA8 patch from the back buffer at the
    /// given viewport region. Returns a Y-plane-like byte vector for
    /// the oracle: average each pixel's R+G+B as a luma proxy.
    /// Reading BEFORE eglSwapBuffers per hdmi.rs:5589's
    /// transition_tex_probe convention.
    unsafe fn read_back_buffer_luma(
        gl: &glow::Context,
        x: u32, y: u32, w: u32, h: u32,
    ) -> Vec<u8> {
        let pixel_count = (w as usize) * (h as usize);
        let mut buf = vec![0u8; pixel_count * 4];
        gl.read_pixels(
            x as i32, y as i32,
            w as i32, h as i32,
            glow::RGBA, glow::UNSIGNED_BYTE,
            glow::PixelPackData::Slice(&mut buf[..]),
        );
        // Pack to a luma-proxy plane (sum of R+G+B / 3 → u8). The
        // oracle's FNV-1a hash + all-constant check works on any byte
        // sequence; we don't need actual BT.601 luma here.
        let mut luma = Vec::with_capacity(pixel_count);
        for px in buf.chunks_exact(4) {
            let r = px[0] as u32;
            let g = px[1] as u32;
            let b = px[2] as u32;
            luma.push(((r + g + b) / 3) as u8);
        }
        luma
    }

    pub fn main() -> Result<()> {
        frame_pacing::mark_renderer_startup();

        // ---------- Env config -----------------------------------------
        let park_ms: u64 = std::env::var("M0_PARK_MS")
            .or_else(|_| std::env::var("M2_PARK_MS"))
            .ok().and_then(|s| s.parse().ok()).unwrap_or(200);
        let b_open_ms: u64 = std::env::var("M1_B_OPEN_MS")
            .ok().and_then(|s| s.parse().ok()).unwrap_or(2000);
        let rotation_deg: i32 = std::env::var("M2_ROTATION")
            .ok().and_then(|s| s.parse().ok()).unwrap_or(0);
        let video_a_path = std::env::var("M1_VIDEO_A").map_err(|_| {
            anyhow!("M1_VIDEO_A env var required (outgoing asset.mp4 path)")
        })?;
        let video_b_path = std::env::var("M1_VIDEO_B").map_err(|_| {
            anyhow!("M1_VIDEO_B env var required (incoming asset.mp4 path)")
        })?;
        let video_a_path = std::path::PathBuf::from(video_a_path);
        let video_b_path = std::path::PathBuf::from(video_b_path);
        if !video_a_path.is_file() { bail!("M1_VIDEO_A not found at {}", video_a_path.display()); }
        if !video_b_path.is_file() { bail!("M1_VIDEO_B not found at {}", video_b_path.display()); }
        eprintln!(
            "[m2] start A={} B={} park_ms={} b_open_ms={} rotation={}",
            video_a_path.display(), video_b_path.display(),
            park_ms, b_open_ms, rotation_deg,
        );

        // ---------- DRM + GBM + EGL bring-up ---------------------------
        // Lifted from hdmi.rs::with_egl_session 619-754. KEEP IN SYNC.
        let card_path = if std::path::Path::new("/dev/dri/card1").exists() {
            "/dev/dri/card1"
        } else {
            "/dev/dri/card0"
        };
        let card = Card::open(card_path)?;
        let resources = card.resource_handles().context("drmModeGetResources failed")?;
        let (connector_info, mode) = {
            let mut chosen = None;
            for conn_h in resources.connectors() {
                let info = card.get_connector(*conn_h, false).context("get_connector")?;
                if info.state() == connector::State::Connected && !info.modes().is_empty() {
                    let mode = info.modes()[0];
                    chosen = Some((info, mode));
                    break;
                }
            }
            chosen.ok_or_else(|| anyhow!("no connected HDMI connector with a usable mode"))?
        };
        // mode.size() returns (u16, u16); widen to u32 for arithmetic.
        let (phys_w_u16, phys_h_u16) = mode.size();
        let phys_w: u32 = phys_w_u16 as u32;
        let phys_h: u32 = phys_h_u16 as u32;
        let (mode_w, mode_h) = if rotation_deg == 90 || rotation_deg == 270 {
            (phys_h, phys_w)
        } else {
            (phys_w, phys_h)
        };
        eprintln!(
            "[m2] selected connector {:?} at {}x{}@{} (logical {}x{})",
            connector_info.handle(), phys_w, phys_h, mode.vrefresh(), mode_w, mode_h,
        );
        let encoder_handle = connector_info.current_encoder()
            .or_else(|| connector_info.encoders().first().copied())
            .ok_or_else(|| anyhow!("connector advertises no encoders"))?;
        let encoder_info = card.get_encoder(encoder_handle).context("get_encoder")?;
        let crtc_handle = encoder_info.crtc()
            .or_else(|| resources.crtcs().first().copied())
            .ok_or_else(|| anyhow!("no CRTC available"))?;
        let gbm_dev = gbm::Device::new(card.0.try_clone().context("clone DRM fd for GBM")?)
            .context("gbm_create_device failed")?;
        let gbm_dev_ptr: *mut c_void = gbm_dev.as_raw() as *mut c_void;
        if gbm_dev_ptr.is_null() { bail!("gbm_device raw pointer is null"); }
        let mut gbm_surface = gbm_dev.create_surface::<()>(
            phys_w, phys_h,
            GbmFormat::Argb8888,
            BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
        ).context("gbm_surface_create failed")?;

        let egl_lib = unsafe {
            egl::DynamicInstance::<egl::EGL1_5>::load_required()
                .map_err(|e| anyhow!("eglDynamicInstance load: {e:?}"))?
        };
        let display = unsafe {
            egl_lib.get_display(gbm_dev_ptr as egl::NativeDisplayType)
                .ok_or_else(|| anyhow!("eglGetDisplay returned NO_DISPLAY"))?
        };
        let (major, minor) = egl_lib.initialize(display)
            .map_err(|e| anyhow!("eglInitialize: {e:?}"))?;
        eprintln!("[m2] EGL {}.{}", major, minor);
        egl_lib.bind_api(egl::OPENGL_ES_API)
            .map_err(|e| anyhow!("eglBindAPI(GLES): {e:?}"))?;
        let cfg_attribs = [
            egl::SURFACE_TYPE, egl::WINDOW_BIT,
            egl::RED_SIZE, 8, egl::GREEN_SIZE, 8, egl::BLUE_SIZE, 8, egl::ALPHA_SIZE, 8,
            egl::RENDERABLE_TYPE, egl::OPENGL_ES2_BIT, egl::NONE,
        ];
        let configs = egl_lib.choose_first_config(display, &cfg_attribs)
            .map_err(|e| anyhow!("eglChooseConfig: {e:?}"))?
            .ok_or_else(|| anyhow!("no EGL config matched ARGB8888 + GLES2"))?;
        let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
        let context = egl_lib.create_context(display, configs, None, &ctx_attribs)
            .map_err(|e| anyhow!("eglCreateContext: {e:?}"))?;
        let egl_surface = unsafe {
            let raw_surface = gbm_surface.as_raw_mut() as *mut c_void;
            egl_lib.create_window_surface(display, configs, raw_surface, None)
                .map_err(|e| anyhow!("eglCreateWindowSurface: {e:?}"))?
        };
        egl_lib.make_current(display, Some(egl_surface), Some(egl_surface), Some(context))
            .map_err(|e| anyhow!("eglMakeCurrent: {e:?}"))?;
        if let Err(e) = egl_lib.swap_interval(display, 0) {
            eprintln!("[m2] warn: eglSwapInterval(0): {e:?}");
        }
        let gl = unsafe {
            glow::Context::from_loader_function(|name| {
                egl_lib.get_proc_address(name).map(|fp| fp as *const _).unwrap_or(ptr::null())
            })
        };
        let dma_eps = resolve_dma_buf_egl_eps(&egl_lib, display, &gl)
            .ok_or_else(|| anyhow!("dma_buf EGL extensions missing — cannot proceed"))?;
        let blit_program = build_nv12_blit_program(&gl)?;
        let vbo = full_quad_vbo(&gl)?;
        eprintln!("[m2] GL bring-up complete; NV12 blit program linked");

        // ---------- Decoder A: open + prime ----------------------------
        let t_a_warm = Instant::now();
        let dem_a = mp4_demux::Mp4Demuxer::open(&video_a_path)
            .map_err(|e| anyhow!("Mp4Demuxer::open A: {e:#}"))?;
        let slide_a_id = uuid::Uuid::from_bytes([
            0x4d,0x32,0xaa,0xaa, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        let mut state_a = video_decode::prime_video_decoder_for_preload(&dem_a, slide_a_id)
            .map_err(|e| anyhow!("prime A: {e:#}"))?;
        let a_warm_us = t_a_warm.elapsed().as_micros();
        eprintln!(
            "[m2] A primed warm_us={} samples={} w={} h={}",
            a_warm_us, dem_a.samples.len(), dem_a.width, dem_a.height,
        );

        // ---------- Decoder B: demux parsed; open deferred --------------
        let dem_b = mp4_demux::Mp4Demuxer::open(&video_b_path)
            .map_err(|e| anyhow!("Mp4Demuxer::open B: {e:#}"))?;
        let slide_b_id = uuid::Uuid::from_bytes([
            0x4d,0x32,0xbb,0xbb, 0,0,0,0, 0,0,0,0, 0,0,0,0,
        ]);
        eprintln!(
            "[m2] B mp4 parsed samples={} w={} h={}",
            dem_b.samples.len(), dem_b.width, dem_b.height,
        );

        // ---------- State machine -------------------------------------
        enum Phase {
            ASoloPreOpen,
            BOpening,
            BParked,
            BResuming,
            Drain,
        }
        let mut phase = Phase::ASoloPreOpen;

        // Per-side counters + oracles.
        let mut a_decoder_oracle = probe_oracle::PixelOracle::new();
        let mut a_screen_oracle = probe_oracle::PixelOracle::new();
        let mut b_decoder_oracle = probe_oracle::PixelOracle::new();
        let mut b_screen_oracle = probe_oracle::PixelOracle::new();
        let mut a_fresh: u32 = 0;
        let mut a_screen_samples: u32 = 0;
        let mut b_fresh: u32 = 0;
        let mut b_screen_samples: u32 = 0;
        let mut a_samples_fed: usize = 0;
        let mut a_other_errs: u32 = 0;
        let mut b_samples_fed: usize = 0;
        let mut b_other_errs: u32 = 0;
        let mut b_egl_import_errno: String = String::new();
        let mut b_first_resume_to_screen_us: u128 = 0;
        let mut b_warm_us: u128 = 0;
        let mut b_resume_us: u128 = 0;
        let mut b_open_errno: String = String::new();
        let mut a_black_count_during_b_bake: u32 = 0;
        let mut a_fresh_at_b_resume_start: u32 = 0;
        let mut a_fresh_at_b_resume_end: u32 = 0;
        let mut max_paint_stall_us: u128 = 0;

        let t_start = Instant::now();
        let b_open_deadline = t_start + Duration::from_millis(b_open_ms);
        let mut park_until: Option<Instant> = None;
        let mut b_resume_start: Option<Instant> = None;
        let mut state_b: Option<video_decode::VideoDecoderState> = None;

        const TARGET_B_FRAMES: u32 = 30;
        const A_MIN_FRAMES_POST_B_RESUME: u32 = 30;
        const TICK_NS: u64 = 33_333_333;
        const TOTAL_DEADLINE_MS: u64 = 25_000;
        let total_deadline = t_start + Duration::from_millis(TOTAL_DEADLINE_MS);

        // Track whether the first set_crtc has run (replaces session.modeset_done).
        let mut modeset_done = false;
        let mut prev_bo: Option<gbm::BufferObject<()>> = None;
        let mut prev_fb: Option<framebuffer::Handle> = None;

        let mut tick: u32 = 0;
        while Instant::now() < total_deadline {
            let tick_start = Instant::now();
            tick += 1;

            // A: feed + DQBUF every tick. M1-hygiene fix: wrap
            // next_sample_idx so A stays live past its sample count
            // through B's resume window.
            let n_a = dem_a.samples.len();
            let idx_a = state_a.next_sample_idx % n_a;
            match state_a.decoder.feed(&dem_a.samples[idx_a]) {
                Ok(()) => {
                    state_a.next_sample_idx += 1;
                    a_samples_fed += 1;
                }
                Err(e) => {
                    let s = format!("{e:#}");
                    if !s.contains("EAGAIN") {
                        a_other_errs += 1;
                        eprintln!("[m2] A feed err: {s}");
                    }
                }
            }
            let mut a_frame: Option<v4l2::Frame> = None;
            match state_a.decoder.next_frame() {
                Ok(Some(f)) => {
                    a_fresh += 1;
                    state_a.frames_decoded += 1;
                    a_decoder_oracle.check(f.y_plane());
                    a_frame = Some(f);
                }
                Ok(None) => {}
                Err(e) => {
                    let s = format!("{e:#}");
                    if !s.contains("EAGAIN") {
                        a_other_errs += 1;
                        eprintln!("[m2] A dqbuf err: {s}");
                    }
                }
            }

            // Phase machine.
            let mut b_frame: Option<v4l2::Frame> = None;
            match phase {
                Phase::ASoloPreOpen => {
                    if Instant::now() >= b_open_deadline {
                        phase = Phase::BOpening;
                    }
                }
                Phase::BOpening => {
                    eprintln!(
                        "[m2] OPENING B at t={} ms a_fresh_so_far={}",
                        t_start.elapsed().as_millis(), a_fresh,
                    );
                    let t_b_open = Instant::now();
                    match video_decode::prime_video_decoder_for_preload(&dem_b, slide_b_id) {
                        Ok(sb) => {
                            state_b = Some(sb);
                            b_warm_us = t_b_open.elapsed().as_micros();
                            eprintln!("[m2] B primed warm_us={} parking for {} ms",
                                b_warm_us, park_ms);
                            park_until = Some(Instant::now() + Duration::from_millis(park_ms));
                            phase = Phase::BParked;
                        }
                        Err(e) => {
                            let s = format!("{e:#}");
                            b_open_errno = s.clone();
                            eprintln!("[m2] B prime FAILED: {s}");
                            b_other_errs += 1;
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::BParked => {
                    if let Some(p_until) = park_until {
                        if Instant::now() >= p_until {
                            eprintln!(
                                "[m2] B PARK end → RESUME at t={} ms",
                                t_start.elapsed().as_millis(),
                            );
                            b_resume_start = Some(Instant::now());
                            a_fresh_at_b_resume_start = a_fresh;
                            phase = Phase::BResuming;
                        }
                    }
                }
                Phase::BResuming => {
                    if let Some(state) = state_b.as_mut() {
                        let n_b = dem_b.samples.len();
                        if state.next_sample_idx < n_b {
                            match state.decoder.feed(&dem_b.samples[state.next_sample_idx]) {
                                Ok(()) => {
                                    state.next_sample_idx += 1;
                                    b_samples_fed += 1;
                                }
                                Err(e) => {
                                    let s = format!("{e:#}");
                                    if !s.contains("EAGAIN") {
                                        b_other_errs += 1;
                                        eprintln!("[m2] B feed err: {s}");
                                    }
                                }
                            }
                        }
                        match state.decoder.next_frame() {
                            Ok(Some(f)) => {
                                if b_fresh == 0 {
                                    b_resume_us = b_resume_start
                                        .map(|t| t.elapsed().as_micros()).unwrap_or(0);
                                }
                                b_fresh += 1;
                                state.frames_decoded += 1;
                                b_decoder_oracle.check(f.y_plane());
                                b_frame = Some(f);
                            }
                            Ok(None) => {
                                eprintln!("[m2] B EOS at b_fresh={b_fresh}");
                                a_fresh_at_b_resume_end = a_fresh;
                                phase = Phase::Drain;
                            }
                            Err(e) => {
                                let s = format!("{e:#}");
                                if !s.contains("EAGAIN") {
                                    b_other_errs += 1;
                                    eprintln!("[m2] B dqbuf err: {s}");
                                }
                            }
                        }
                        if b_fresh >= TARGET_B_FRAMES {
                            a_fresh_at_b_resume_end = a_fresh;
                            phase = Phase::Drain;
                        }
                    }
                }
                Phase::Drain => {
                    let post = a_fresh.saturating_sub(a_fresh_at_b_resume_end);
                    if post >= A_MIN_FRAMES_POST_B_RESUME { break; }
                }
            }

            // Paint pass — clear + dual-bake into split viewport when B
            // is also live, else A-only full screen.
            let t_paint = Instant::now();
            unsafe {
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                gl.viewport(0, 0, phys_w as i32, phys_h as i32);
                gl.clear_color(0.0, 0.0, 0.0, 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.disable(glow::DEPTH_TEST);
                gl.disable(glow::BLEND);
                let half_w = (phys_w / 2) as i32;
                let h_i = phys_h as i32;
                // A paints full-screen when B not yet live; else left half.
                let a_viewport = if b_frame.is_some() {
                    (0, 0, half_w, h_i)
                } else {
                    (0, 0, phys_w as i32, h_i)
                };
                if let Some(f) = a_frame.as_ref() {
                    if let Some(fd) = f.dma_buf_fd() {
                        gl.viewport(a_viewport.0, a_viewport.1, a_viewport.2, a_viewport.3);
                        match blit_nv12_dmabuf_to_viewport(
                            &gl, &egl_lib, display, dma_eps,
                            &blit_program, vbo, fd,
                            f.width(), f.height(), f.stride(), 1.0,
                            "a_blit",
                        ) {
                            Ok(()) => {}
                            Err(e) => eprintln!("[m2] A blit err: {e:#}"),
                        }
                    }
                }
                if let Some(f) = b_frame.as_ref() {
                    if let Some(fd) = f.dma_buf_fd() {
                        gl.viewport(half_w, 0, half_w, h_i);
                        let t_b_blit = Instant::now();
                        match blit_nv12_dmabuf_to_viewport(
                            &gl, &egl_lib, display, dma_eps,
                            &blit_program, vbo, fd,
                            f.width(), f.height(), f.stride(), 1.0,
                            "b_blit",
                        ) {
                            Ok(()) => {
                                if b_first_resume_to_screen_us == 0 {
                                    b_first_resume_to_screen_us = b_resume_start
                                        .map(|t| t.elapsed().as_micros()).unwrap_or(0);
                                }
                            }
                            Err(e) => {
                                let s = format!("{e:#}");
                                if b_egl_import_errno.is_empty() {
                                    b_egl_import_errno = s.clone();
                                }
                                eprintln!("[m2] B blit err: {s}");
                            }
                        }
                        let blit_us = t_b_blit.elapsed().as_micros();
                        if blit_us > max_paint_stall_us {
                            max_paint_stall_us = blit_us;
                        }
                    }
                }
                gl.finish();
                // Screen oracle — read BACK buffer BEFORE swap.
                let probe_w = 64u32.min(phys_w / 4);
                let probe_h = 64u32.min(phys_h / 4);
                // A region center.
                let (a_px, a_py) = if b_frame.is_some() {
                    ((phys_w / 4) - probe_w / 2, (phys_h / 2) - probe_h / 2)
                } else {
                    ((phys_w / 2) - probe_w / 2, (phys_h / 2) - probe_h / 2)
                };
                if a_frame.is_some() {
                    let luma = read_back_buffer_luma(&gl, a_px, a_py, probe_w, probe_h);
                    let v = a_screen_oracle.check(&luma);
                    a_screen_samples += 1;
                    // r76 secondary signal: A going black when B's bake starts.
                    if b_frame.is_some() && v.all_constant {
                        a_black_count_during_b_bake += 1;
                    }
                }
                if b_frame.is_some() {
                    let bx = (3 * phys_w / 4) - probe_w / 2;
                    let by = (phys_h / 2) - probe_h / 2;
                    let luma = read_back_buffer_luma(&gl, bx, by, probe_w, probe_h);
                    b_screen_oracle.check(&luma);
                    b_screen_samples += 1;
                }
                egl_lib.swap_buffers(display, egl_surface)
                    .map_err(|e| anyhow!("eglSwapBuffers: {e:?}"))?;
                // Get the freshly-presented BO + add a framebuffer + commit
                // (set_crtc on first, page_flip on subsequent — set_crtc
                // path used to keep the probe simple; no event drain).
                let new_bo = gbm_surface.lock_front_buffer()
                    .context("gbm_surface_lock_front_buffer")?;
                let fb_buf = GbmFb::new(&new_bo).context("GbmFb::new")?;
                let new_fb = card.add_framebuffer(&fb_buf, 24, 32)
                    .map_err(|e| anyhow!("drmModeAddFB: {e}"))?;
                if !modeset_done {
                    card.set_crtc(
                        crtc_handle, Some(new_fb), (0, 0),
                        &[connector_info.handle()], Some(mode),
                    ).context("drmModeSetCrtc")?;
                    modeset_done = true;
                } else {
                    card.set_crtc(
                        crtc_handle, Some(new_fb), (0, 0),
                        &[connector_info.handle()], Some(mode),
                    ).context("drmModeSetCrtc (subsequent)")?;
                }
                // Release prev frame's resources.
                if let Some(pfb) = prev_fb.take() {
                    let _ = card.destroy_framebuffer(pfb);
                }
                if let Some(pbo) = prev_bo.take() {
                    drop(pbo);
                }
                prev_bo = Some(new_bo);
                prev_fb = Some(new_fb);
            }
            let paint_us = t_paint.elapsed().as_micros();
            if paint_us > max_paint_stall_us { max_paint_stall_us = paint_us; }

            // Per-tick log line (compact — sub-agent parses).
            let a_dec_ok = a_frame.is_some() as u8;
            let b_dec_ok = b_frame.is_some() as u8;
            println!(
                "[m2] tick={tick} phase={} a_dec={} b_dec={} a_fresh={} b_fresh={} paint_us={}",
                match phase {
                    Phase::ASoloPreOpen => "preopen",
                    Phase::BOpening => "opening",
                    Phase::BParked => "parked",
                    Phase::BResuming => "resuming",
                    Phase::Drain => "drain",
                },
                a_dec_ok, b_dec_ok, a_fresh, b_fresh, paint_us,
            );

            // 30 fps cadence.
            let elapsed = tick_start.elapsed().as_nanos() as u64;
            if elapsed < TICK_NS {
                std::thread::sleep(Duration::from_nanos(TICK_NS - elapsed));
            }
        }

        // ---------- VERDICT --------------------------------------------
        let a_during_b_resume = a_fresh_at_b_resume_end.saturating_sub(a_fresh_at_b_resume_start);
        let b_decoder_ok = b_decoder_oracle.pixel_ok;
        let b_screen_ok = b_screen_oracle.pixel_ok;
        let a_screen_ok = a_screen_oracle.pixel_ok;
        // Divergence count: ticks where B decoder produced a fresh frame
        // AND we sampled the screen but the screen oracle marked the
        // pixels as non-distinct/black. Approximate as (decoder_ok -
        // screen_ok) clamped at 0.
        let screen_divergence = b_decoder_ok.saturating_sub(b_screen_ok);
        let verdict = if !b_open_errno.is_empty() {
            "WEDGED"
        } else if screen_divergence >= 3 {
            // DIVERGENT = the r76 fingerprint: decoder produced but
            // screen didn't refresh / went black on B's region.
            "DIVERGENT"
        } else if b_decoder_oracle.total >= TARGET_B_FRAMES
            && b_screen_ok >= b_screen_samples.saturating_sub(2)
            && a_during_b_resume >= 10
            && a_other_errs == 0
            && b_other_errs == 0
        {
            "HEALTHY"
        } else if b_decoder_oracle.total >= 15
            && b_screen_ok >= 10
        {
            "DEGRADED"
        } else {
            "WEDGED"
        };

        println!(
            "[m2] PARK_MS={park_ms} B_OPEN_MS={b_open_ms} \
             a_warm_us={a_warm_us} a_fresh={a_fresh} a_samples_fed={a_samples_fed} a_other_errs={a_other_errs} \
             {a_dec_report} {a_scr_report} \
             b_warm_us={b_warm_us} b_resume_us={b_resume_us} \
             b_first_resume_to_screen_us={b_first_resume_to_screen_us} \
             b_fresh={b_fresh}/{target} b_samples_fed={b_samples_fed} \
             b_other_errs={b_other_errs} \
             {b_dec_report} {b_scr_report} \
             a_during_b_resume={a_during_b_resume} \
             b_open_errno=\"{b_open_errno}\" \
             egl_import_errno=\"{egl_import_errno}\" \
             paint_stall_us={paint_stall} \
             a_black_on_b_bake={a_black_on_b_bake} \
             screen_divergence={screen_divergence} \
             VERDICT={verdict}",
            park_ms = park_ms,
            b_open_ms = b_open_ms,
            a_warm_us = a_warm_us,
            a_fresh = a_fresh,
            a_samples_fed = a_samples_fed,
            a_other_errs = a_other_errs,
            a_dec_report = a_decoder_oracle.report("a_decoder"),
            a_scr_report = a_screen_oracle.report("a_screen"),
            b_warm_us = b_warm_us,
            b_resume_us = b_resume_us,
            b_first_resume_to_screen_us = b_first_resume_to_screen_us,
            b_fresh = b_fresh,
            target = TARGET_B_FRAMES,
            b_samples_fed = b_samples_fed,
            b_other_errs = b_other_errs,
            b_dec_report = b_decoder_oracle.report("b_decoder"),
            b_scr_report = b_screen_oracle.report("b_screen"),
            a_during_b_resume = a_during_b_resume,
            b_open_errno = b_open_errno,
            egl_import_errno = b_egl_import_errno,
            paint_stall = max_paint_stall_us,
            a_black_on_b_bake = a_black_count_during_b_bake > 0,
            screen_divergence = screen_divergence,
            verdict = verdict,
        );

        // Cleanup. Leak some GL resources on exit — process death
        // reclaims everything.
        drop(prev_bo);
        if let Some(pfb) = prev_fb { let _ = card.destroy_framebuffer(pfb); }
        Ok(())
    }
}

#[cfg(target_os = "linux")]
fn main() -> anyhow::Result<()> {
    linux_main::main()
}
