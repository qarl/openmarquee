//! Phase 2 — pixels on the HDMI display.
//!
//! Two paths:
//!
//! - `render_solid_color()` — Phase 2 milestone. Single frame via
//!   GBM + EGL + GLES2 + legacy `drmModeSetCrtc`. Smallest end-to-end
//!   test of the GLES → DRM scanout pipeline; not production-shaped.
//!
//! - `render_animated_atomic()` — Phase 2.1 / plan §4 Step 2. Atomic
//!   commit + double-buffered page-flip event loop, animating a hue
//!   rotation. This is the foundation every subsequent phase (slide
//!   bake, transitions, video decode) extends.
//!
//! The error model is intentionally chatty (`anyhow::Context`) so any
//! failure during bring-up tells you which step blew up.

use std::collections::VecDeque;
use std::ffi::c_void;
use std::ptr;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use drm::buffer::{Buffer as DrmBuffer, DrmFourcc, Handle as DrmHandle};
use drm::Device as DrmBaseDevice;
use drm::control::{
    atomic::AtomicModeReq,
    connector::{self, State as ConnectorState},
    framebuffer, plane,
    property::{self, Value as PropValue},
    AtomicCommitFlags, Device as ControlDevice, Mode,
};
use gbm::{AsRaw, BufferObject, BufferObjectFlags, Format as GbmFormat};
use khronos_egl as egl;

use crate::content::{solid_bg_hex, TextSlide};
use crate::hdmi_logic::{
    fourcc_for_argb_family, gradient_uniforms, hex_to_rgba, hsv_to_rgb, layout_text_to_alpha,
    parse_crtc_list_filter_bits, pick_largest_mode_index, ModeSpec, FS_GLYPH, FS_GRADIENT,
    VS_FULLSCREEN_QUAD, VS_TEXTURED_QUAD,
};
use crate::Card;

// =====================================================================
// Phase 4.1b — gradient pattern via fragment shader.
//
// Architectural decisions (per QA's "spend the cycles deliberately"
// note for the shader infrastructure that text glyphs + remaining
// patterns will build on):
//
//   * Shader sources: inline raw strings for now. Phase 4.1b ships
//     ONE fragment shader (gradient) so a `shaders/` dir +
//     include_str! is premature. Move to a directory when the
//     count grows past ~3.
//   * Uniform passing: individual glow `uniform_*` calls. UBOs are
//     GLES3-only; vc4 only exposes GLES2. No alternative.
//   * Vertex shader: ONE shared shader for all bg-pattern + future
//     compositor passes (a fullscreen NDC quad). Pulled out as
//     `VS_FULLSCREEN_QUAD` const and reused.
//   * Fragment compile errors: anyhow context with the GL info-log
//     attached. Matches the rest of the renderer's chatty-context
//     error model. Not a panic — operators see the log, not a stack
//     trace.
// =====================================================================


/// Compile a single shader stage, returning the GL handle on success
/// or an anyhow error with the compile log attached.
fn compile_shader(gl: &glow::Context, kind: u32, source: &str) -> Result<glow::NativeShader> {
    use glow::HasContext;
    unsafe {
        let sh = gl
            .create_shader(kind)
            .map_err(|e| anyhow!("glCreateShader: {e}"))?;
        gl.shader_source(sh, source);
        gl.compile_shader(sh);
        if !gl.get_shader_compile_status(sh) {
            let log = gl.get_shader_info_log(sh);
            gl.delete_shader(sh);
            return Err(anyhow!("shader compile failed:\n{log}\n--source--\n{source}"));
        }
        Ok(sh)
    }
}

/// Compile + link a vertex + fragment shader pair into a program,
/// returning the program handle. Both shader stages are deleted
/// after link (their objects are no longer referenced).
///
/// Cleanup is exhaustive: if the FRAGMENT compile fails, the
/// already-compiled VERTEX shader is deleted before the early-
/// return; if create_program fails, both stage shaders are
/// deleted; if link fails, the program plus both stage shaders
/// are deleted. Phase 4.2 (text glyphs) calls this repeatedly,
/// so leaks compound.
fn link_program(gl: &glow::Context, vs_src: &str, fs_src: &str) -> Result<glow::NativeProgram> {
    use glow::HasContext;
    let vs = compile_shader(gl, glow::VERTEX_SHADER, vs_src)?;
    let fs = match compile_shader(gl, glow::FRAGMENT_SHADER, fs_src) {
        Ok(fs) => fs,
        Err(e) => {
            unsafe { gl.delete_shader(vs) };
            return Err(e);
        }
    };
    unsafe {
        let prog = match gl.create_program() {
            Ok(p) => p,
            Err(e) => {
                gl.delete_shader(vs);
                gl.delete_shader(fs);
                return Err(anyhow!("glCreateProgram: {e}"));
            }
        };
        gl.attach_shader(prog, vs);
        gl.attach_shader(prog, fs);
        gl.link_program(prog);
        let linked = gl.get_program_link_status(prog);
        gl.detach_shader(prog, vs);
        gl.detach_shader(prog, fs);
        gl.delete_shader(vs);
        gl.delete_shader(fs);
        if !linked {
            let log = gl.get_program_info_log(prog);
            gl.delete_program(prog);
            return Err(anyhow!("program link failed: {log}"));
        }
        Ok(prog)
    }
}

/// Set up the two-triangle fullscreen quad: 4 vertices in NDC,
/// drawn as TRIANGLE_STRIP. Returns (VBO, attribute location). The
/// caller is responsible for binding the VBO + enabling the attrib
/// before drawing.
///
/// On `get_attrib_location` failure we delete the VBO before
/// returning Err so a misnamed-attribute build doesn't leak buffers.
fn create_fullscreen_quad(
    gl: &glow::Context,
    program: glow::NativeProgram,
) -> Result<(glow::NativeBuffer, u32)> {
    use glow::HasContext;
    unsafe {
        let vbo = gl
            .create_buffer()
            .map_err(|e| anyhow!("glGenBuffers: {e}"))?;
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        // Two triangles via TRIANGLE_STRIP: BL, BR, TL, TR.
        let verts: [f32; 8] = [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0];
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            verts.len() * std::mem::size_of::<f32>(),
        );
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
        match gl.get_attrib_location(program, "a_pos") {
            Some(loc) => Ok((vbo, loc)),
            None => {
                gl.delete_buffer(vbo);
                Err(anyhow!("vertex shader is missing the `a_pos` attribute"))
            }
        }
    }
}

/// Sweep `glGetError` and `eprintln!` any sticky errors with a
/// caller-supplied label. Debug-build-only — release builds skip the
/// sweep entirely so production hot loops don't pay for it.
///
/// Bad uniform-location lookups (an optimizer-stripped uniform's
/// `get_uniform_location` returning `None`) silently no-op via
/// glow's `uniform_*_f32(None, ...)` wrappers. The sweep is the
/// catch-all for those plus other "should never happen" GL errors
/// that would otherwise surface only as black/garbage frames.
#[cfg(debug_assertions)]
fn gl_error_sweep(gl: &glow::Context, label: &str) {
    use glow::HasContext;
    loop {
        let err = unsafe { gl.get_error() };
        if err == glow::NO_ERROR {
            break;
        }
        eprintln!("warn: GL error 0x{err:x} after {label}");
    }
}

#[cfg(not(debug_assertions))]
#[inline]
fn gl_error_sweep(_gl: &glow::Context, _label: &str) {}

/// Bring up GBM + EGL + GLES2 against the HDMI display, run the
/// caller's `draw` closure once with a live `glow::Context`, then
/// `eglSwapBuffers` + lock the front BO + register the DRM
/// framebuffer + legacy `drmModeSetCrtc` to push it to scanout.
/// Hold for `hold_secs` seconds. Cleanup runs unconditionally
/// (warn-on-Err) regardless of whether the closure succeeded —
/// matches the Phase 3 followups pattern.
///
/// Phase 4.1c — extracted from `render_solid_color` and
/// `render_slide_bg_gradient` now that we have two callers. Phase
/// 4.1d+ bg-pattern shaders reuse this helper directly.
///
/// `draw` receives the GLES2 context and the viewport (mode_w,
/// mode_h) so the closure can `glViewport`, `glClear`, or
/// compile/link/draw a quad without re-deriving size.
fn render_one_frame_to_hdmi<F>(card: &Card, hold_secs: u64, draw: F) -> Result<()>
where
    F: FnOnce(&glow::Context, u32, u32) -> Result<()>,
{
    let resources = card
        .resource_handles()
        .context("drmModeGetResources failed")?;
    let (connector_info, mode) = pick_connector_and_mode(card, &resources)
        .context("no connected HDMI connector with a usable mode")?;
    let (mode_w, mode_h) = mode.size();
    eprintln!(
        "selected connector {:?} {:?} at {}x{}@{}",
        connector_info.handle(),
        connector_info.interface(),
        mode_w,
        mode_h,
        mode.vrefresh(),
    );

    let encoder_handle = connector_info
        .current_encoder()
        .or_else(|| connector_info.encoders().first().copied())
        .ok_or_else(|| anyhow!("connector advertises no encoders"))?;
    let encoder_info = card
        .get_encoder(encoder_handle)
        .context("drmModeGetEncoder failed")?;
    let crtc_handle = encoder_info
        .crtc()
        .or_else(|| resources.crtcs().first().copied())
        .ok_or_else(|| anyhow!("no CRTC available for encoder {:?}", encoder_handle))?;
    eprintln!("using encoder {:?} crtc {:?}", encoder_handle, crtc_handle);

    let gbm_dev = gbm::Device::new(card.0.try_clone().context("clone DRM fd for GBM")?)
        .context("gbm_create_device failed")?;
    let gbm_dev_ptr: *mut c_void = gbm_dev.as_raw() as *mut c_void;
    if gbm_dev_ptr.is_null() {
        bail!("gbm_device raw pointer is null");
    }
    let gbm_surface = gbm_dev
        .create_surface::<()>(
            mode_w as u32,
            mode_h as u32,
            GbmFormat::Argb8888,
            BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
        )
        .context("gbm_surface_create failed")?;

    let egl_lib = unsafe {
        egl::DynamicInstance::<egl::EGL1_5>::load_required().map_err(|e| {
            anyhow!("eglDynamicInstance::<EGL1_5>::load_required failed: {e:?}")
        })?
    };
    let display = unsafe {
        egl_lib
            .get_display(gbm_dev_ptr as egl::NativeDisplayType)
            .ok_or_else(|| anyhow!("eglGetDisplay returned NO_DISPLAY"))?
    };
    let (egl_major, egl_minor) = egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
    eprintln!("EGL {}.{}", egl_major, egl_minor);

    egl_lib
        .bind_api(egl::OPENGL_ES_API)
        .map_err(|e| anyhow!("eglBindAPI(GLES) failed: {e:?}"))?;
    let cfg_attribs = [
        egl::SURFACE_TYPE, egl::WINDOW_BIT,
        egl::RED_SIZE, 8, egl::GREEN_SIZE, 8, egl::BLUE_SIZE, 8, egl::ALPHA_SIZE, 8,
        egl::RENDERABLE_TYPE, egl::OPENGL_ES2_BIT, egl::NONE,
    ];
    let configs = egl_lib
        .choose_first_config(display, &cfg_attribs)
        .map_err(|e| anyhow!("eglChooseConfig failed: {e:?}"))?
        .ok_or_else(|| anyhow!("no EGL config matched ARGB8888 + GLES2"))?;
    let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
    let context = egl_lib
        .create_context(display, configs, None, &ctx_attribs)
        .map_err(|e| anyhow!("eglCreateContext failed: {e:?}"))?;
    let egl_surface = unsafe {
        let raw_surface = gbm_surface.as_raw_mut() as *mut c_void;
        egl_lib
            .create_window_surface(display, configs, raw_surface, None)
            .map_err(|e| anyhow!("eglCreateWindowSurface failed: {e:?}"))?
    };
    egl_lib
        .make_current(display, Some(egl_surface), Some(egl_surface), Some(context))
        .map_err(|e| anyhow!("eglMakeCurrent failed: {e:?}"))?;

    let gl = unsafe {
        glow::Context::from_loader_function(|name| {
            egl_lib.get_proc_address(name).map(|fp| fp as *const _).unwrap_or(ptr::null())
        })
    };

    // Resources the work block creates (BO + FB) need cleanup
    // regardless of whether the work succeeds. Track via Options
    // populated mid-closure; cleanup walks them after.
    let mut bo_holder: Option<BufferObject<()>> = None;
    let mut fb_holder: Option<framebuffer::Handle> = None;

    let work: Result<()> = (|| {
        draw(&gl, mode_w as u32, mode_h as u32)?;
        gl_error_sweep(&gl, "user draw closure");
        egl_lib
            .swap_buffers(display, egl_surface)
            .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
        let bo = unsafe {
            gbm_surface
                .lock_front_buffer()
                .context("gbm_surface_lock_front_buffer failed")?
        };
        let fb_buf = GbmBufferAdapter::new(&bo).context("read GBM bo metadata")?;
        let fb = match card.add_framebuffer(&fb_buf, 32, 32) {
            Ok(fb) => fb,
            Err(e) => {
                drop(bo);
                return Err(anyhow!("drmModeAddFB failed: {e}"));
            }
        };
        bo_holder = Some(bo);
        fb_holder = Some(fb);
        eprintln!("registered fb {fb:?}");
        card.set_crtc(
            crtc_handle,
            Some(fb),
            (0, 0),
            &[connector_info.handle()],
            Some(mode),
        )
        .context("drmModeSetCrtc failed")?;
        eprintln!(
            "scanout active on {:?}; holding for {}s",
            crtc_handle, hold_secs
        );
        std::thread::sleep(std::time::Duration::from_secs(hold_secs));
        Ok(())
    })();

    // Cleanup — unconditional, warn-on-Err so the original cause
    // propagates via `work?`.
    if let Err(e) = egl_lib.make_current(display, None, None, None) {
        eprintln!("warn: eglMakeCurrent(unbind): {e:?}");
    }
    if let Err(e) = egl_lib.destroy_context(display, context) {
        eprintln!("warn: eglDestroyContext: {e:?}");
    }
    if let Err(e) = egl_lib.destroy_surface(display, egl_surface) {
        eprintln!("warn: eglDestroySurface: {e:?}");
    }
    if let Err(e) = egl_lib.terminate(display) {
        eprintln!("warn: eglTerminate: {e:?}");
    }
    if let Some(bo) = bo_holder {
        drop(bo);
    }
    if let Some(fb) = fb_holder {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
        }
    }

    work?;
    Ok(())
}

/// Phase 4.1b — render a two-color linear gradient via fragment
/// shader, push one frame to the HDMI display via legacy
/// `drmModeSetCrtc`, hold for `hold_secs` seconds, clean up.
///
/// Phase 4.1c factored the GBM/EGL/DRM bring-up onto
/// `render_one_frame_to_hdmi`; this function only owns the GLES
/// draw work.
pub fn render_slide_bg_gradient(
    card: &Card,
    color_a: [f32; 4],
    color_b: [f32; 4],
    density: f32,
    hold_secs: u64,
) -> Result<()> {
    // Phase 4.1c: closure body owns just the GLES draw work
    // (compile + draw the gradient, or fall back to clear_color when
    // the gradient degenerates). `render_one_frame_to_hdmi` handles
    // the GBM/EGL/DRM bring-up + swap/addFB/SetCrtc/hold/teardown.
    render_one_frame_to_hdmi(card, hold_secs, |gl, mode_w, mode_h| {
        use glow::HasContext;
        let g = gradient_uniforms(mode_w, mode_h, density);
        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            if let Some(g) = g {
                let program = link_program(gl, VS_FULLSCREEN_QUAD, FS_GRADIENT)?;
                let (vbo, attrib) = match create_fullscreen_quad(gl, program) {
                    Ok(pair) => pair,
                    Err(e) => {
                        gl.delete_program(program);
                        return Err(e);
                    }
                };
                gl.use_program(Some(program));
                let u_viewport = gl.get_uniform_location(program, "u_viewport");
                let u_dir = gl.get_uniform_location(program, "u_dir");
                let u_proj_bounds = gl.get_uniform_location(program, "u_proj_bounds");
                let u_color_a = gl.get_uniform_location(program, "u_color_a");
                let u_color_b = gl.get_uniform_location(program, "u_color_b");
                gl.uniform_2_f32(u_viewport.as_ref(), mode_w as f32, mode_h as f32);
                gl.uniform_2_f32(u_dir.as_ref(), g.dx, g.dy);
                gl.uniform_2_f32(u_proj_bounds.as_ref(), g.proj_min, g.span);
                gl.uniform_3_f32(u_color_a.as_ref(), color_a[0], color_a[1], color_a[2]);
                gl.uniform_3_f32(u_color_b.as_ref(), color_b[0], color_b[1], color_b[2]);
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                gl.enable_vertex_attrib_array(attrib);
                gl.vertex_attrib_pointer_f32(attrib, 2, glow::FLOAT, false, 0, 0);
                gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
                gl.disable_vertex_attrib_array(attrib);
                gl.delete_buffer(vbo);
                gl.delete_program(program);
            } else {
                // Degenerate gradient (1×1 viewport): solid color_a.
                gl.clear_color(color_a[0], color_a[1], color_a[2], 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
            }
            gl.flush();
        }
        Ok(())
    })?;
    eprintln!("gradient render complete");
    Ok(())
}

/// Phase 4 entry — render a TextSlide's `background_color` (a
/// `#RRGGBB` hex string) for `hold_secs` seconds. Procedural
/// `background_pattern` (12 patterns) lands in a follow-up commit;
/// for now any pattern-only slide falls back to the slide's
/// `background_color` (which the model defaults to `#000000`).
///
/// Reuses `render_solid_color`'s legacy SetCrtc path — the parsed
/// hex is just an `[f32; 4]`, identical to what --solid-color takes.
/// When the procedural-pattern shader path lands we'll route based
/// on whether `slide.background_pattern.is_some()`.
pub fn render_slide_bg(card: &Card, slide: &TextSlide, hold_secs: u64) -> Result<()> {
    // Dispatch:
    //   pattern: gradient → fragment-shader gradient (Phase 4.1b)
    //   pattern: solid    → color_a as solid fill (Phase 4.1a)
    //   pattern: <other>  → fall back to background_color + warn
    //   pattern: None     → background_color
    if let Some(p) = &slide.background_pattern {
        if p.pattern == "gradient" {
            let color_a = hex_to_rgba(&p.color_a)
                .ok_or_else(|| anyhow!("invalid color_a {:?} for slide {}", p.color_a, slide.id))?;
            let color_b = hex_to_rgba(&p.color_b)
                .ok_or_else(|| anyhow!("invalid color_b {:?} for slide {}", p.color_b, slide.id))?;
            eprintln!(
                "rendering slide {} ({:?}) pattern=gradient density={:.3} a={} b={} for {}s",
                slide.id, slide.name, p.density, p.color_a, p.color_b, hold_secs,
            );
            return render_slide_bg_gradient(card, color_a, color_b, p.density, hold_secs);
        }
    }
    // Pure dispatch in `content::solid_bg_hex` for unit testability.
    let hex = solid_bg_hex(slide).to_string();
    let color = hex_to_rgba(&hex)
        .ok_or_else(|| anyhow!("invalid hex color {hex:?} for slide {}", slide.id))?;
    let pattern_label = slide
        .background_pattern
        .as_ref()
        .map(|p| p.pattern.as_str())
        .unwrap_or("none");
    if pattern_label != "none" && pattern_label != "solid" {
        eprintln!(
            "warn: pattern {pattern_label:?} not yet implemented; falling back to background_color"
        );
    }
    eprintln!(
        "rendering slide {} ({:?}) pattern={} bg={} for {}s",
        slide.id, slide.name, pattern_label, hex, hold_secs,
    );
    render_solid_color(card, color, hold_secs)
}

/// Phase 4.2a — render a TextSlide's first visible text_layer over
/// its background color. Single-pass GLES2 composite:
///   1. clear_color → bg (slide.background_color, or pattern color_a
///      when pattern == "solid"; gradient/other patterns fall back
///      to the same `solid_bg_hex` dispatch as `render_slide_bg`).
///   2. rasterize the layer's text via fontdue + `layout_text_to_alpha`.
///   3. upload as a single-channel `LUMINANCE` texture.
///   4. draw a textured quad sized + positioned by the layer's
///      slide-relative `box`.
///
/// Phase 4.2a constraints (intentional, narrowed for the 4.2a slice):
///   - First visible layer only (multi-layer composite is 4.2c).
///   - Bg pattern beyond solid falls back to `background_color`.
///     Gradient bg + text on top is 4.2b's slice.
///   - Layer alignment: top-left placement of the rasterized bitmap
///     inside the box. text_align/scale-to-fit/center is 4.2c.
///   - Single-line layout from `layout_text_to_alpha`.
///
/// Holds for `hold_secs` and tears down (same harness as the bg
/// paths via `render_one_frame_to_hdmi`).
pub fn render_slide_text(
    card: &Card,
    slide: &TextSlide,
    font: &fontdue::Font,
    hold_secs: u64,
) -> Result<()> {
    // First visible layer with non-empty `text`. Empty-text layers
    // exist in the model (operator dragged a text widget but never
    // typed anything) and would fail layout — skip them silently
    // here so the slide still renders.
    let layer = slide
        .text_layers
        .iter()
        .find(|l| l.visible && !l.text.is_empty())
        .ok_or_else(|| anyhow!("slide {} has no visible non-empty text_layers", slide.id))?;

    // Bg color (Phase 4.2a: solid fill underneath. Gradient + text
    // composite is 4.2b.)
    let bg_hex = solid_bg_hex(slide).to_string();
    let bg = hex_to_rgba(&bg_hex)
        .ok_or_else(|| anyhow!("invalid bg hex {bg_hex:?} for slide {}", slide.id))?;

    // Layer-side state copied out before the closure so we don't
    // borrow `slide`/`layer` for `'static`-ish lifetimes through the
    // FnOnce.
    let text_color = hex_to_rgba(&layer.text_color)
        .ok_or_else(|| anyhow!("invalid text_color {:?} for slide {}", layer.text_color, slide.id))?;
    let text = layer.text.clone();
    let box_x = layer.r#box.x;
    let box_y = layer.r#box.y;
    // box.w is intentionally unused at Phase 4.2a — the bitmap is
    // placed at its rasterized pixel size, not fit to the box.
    // 4.2c uses box.w when scale-to-fit lands.
    let box_h = layer.r#box.h;
    let font_size_px = layer.font_size_px;
    let font_size_pct = layer.font_size_pct;
    let opacity = layer.opacity.clamp(0.0, 1.0);

    eprintln!(
        "rendering slide {} text_layer text={:?} box=({:.3},{:.3},{:.3},{:.3}) text_color={} bg={} for {}s",
        slide.id,
        text,
        box_x,
        box_y,
        layer.r#box.w,
        box_h,
        layer.text_color,
        bg_hex,
        hold_secs,
    );

    render_one_frame_to_hdmi(card, hold_secs, move |gl, mode_w, mode_h| {
        use glow::HasContext;

        // Resolve effective pixel size.
        // Phase 4.2a heuristic (NOT the Python model semantics —
        // 4.2c lands a real fit-to-box pass):
        //   - font_size_px wins when set.
        //   - font_size_pct treated as percent-of-box-HEIGHT (the
        //     Python reference is percent-of-box-WIDTH; we deviate
        //     here so FYS canonical slides at font_size_pct=80 and
        //     box-height=0.8 produce a sensibly-sized atlas without
        //     any fit pass. Replaced wholesale in 4.2c).
        //   - default 64px when neither set.
        let box_h_px = (box_h * mode_h as f32).max(1.0);
        let size_px = font_size_px
            .or(font_size_pct.map(|p| (p / 100.0) * box_h_px))
            .unwrap_or(64.0)
            .max(8.0);

        let bm = layout_text_to_alpha(font, &text, size_px).ok_or_else(|| {
            anyhow!("layout_text_to_alpha returned None for text={text:?} size={size_px}")
        })?;
        eprintln!(
            "rasterized text {:?} @ {:.1}px → {}x{} alpha bitmap",
            text, size_px, bm.width, bm.height,
        );

        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(bg[0], bg[1], bg[2], bg[3]);
            gl.clear(glow::COLOR_BUFFER_BIT);

            // -- Glyph atlas (single-line bitmap) as a LUMINANCE
            // texture. GLES2 doesn't expose GL_RED; LUMINANCE is the
            // analog for single-channel grayscale and returns the
            // value in r/g/b/a on sample (FS_GLYPH reads `.r`).
            let tex = gl
                .create_texture()
                .map_err(|e| anyhow!("glGenTextures: {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            // Tightly-packed 1-byte rows.
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::LUMINANCE as i32,
                bm.width as i32,
                bm.height as i32,
                0,
                glow::LUMINANCE,
                glow::UNSIGNED_BYTE,
                Some(&bm.data),
            );
            // Linear filter for upsampling, clamp-to-edge so the
            // tightly-cropped atlas doesn't wrap-bleed at quad edges.
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MIN_FILTER,
                glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D,
                glow::TEXTURE_MAG_FILTER,
                glow::LINEAR as i32,
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

            // -- Build the textured quad in NDC.
            // Box position in image-pixel coords (y=0 at top):
            let box_left_px = box_x * mode_w as f32;
            let box_top_px = box_y * mode_h as f32;
            // Place the bitmap at the box top-left at its rasterized
            // pixel size. 4.2c will fit + center; 4.2a draws it as-is
            // so we can verify glyph correctness without confounding
            // it with a layout-fit pass.
            let dst_left = box_left_px;
            let dst_top = box_top_px;
            let dst_right = dst_left + bm.width as f32;
            let dst_bottom = dst_top + bm.height as f32;
            let to_ndc_x = |px: f32| (px / mode_w as f32) * 2.0 - 1.0;
            let to_ndc_y = |px: f32| 1.0 - (px / mode_h as f32) * 2.0;
            let ndc_l = to_ndc_x(dst_left);
            let ndc_r = to_ndc_x(dst_right);
            let ndc_t = to_ndc_y(dst_top);
            let ndc_b = to_ndc_y(dst_bottom);
            // Verts: TRIANGLE_STRIP order BL, BR, TL, TR. Each vert is
            // [x, y, u, v]. UV (0,0) is top-left of the bitmap, which
            // matches our row-major top-down `data`.
            let verts: [f32; 16] = [
                ndc_l, ndc_b, 0.0, 1.0,
                ndc_r, ndc_b, 1.0, 1.0,
                ndc_l, ndc_t, 0.0, 0.0,
                ndc_r, ndc_t, 1.0, 0.0,
            ];

            let program = match link_program(gl, VS_TEXTURED_QUAD, FS_GLYPH) {
                Ok(p) => p,
                Err(e) => {
                    gl.delete_texture(tex);
                    return Err(e);
                }
            };
            let vbo = match gl.create_buffer() {
                Ok(b) => b,
                Err(e) => {
                    gl.delete_program(program);
                    gl.delete_texture(tex);
                    return Err(anyhow!("glGenBuffers: {e}"));
                }
            };
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                verts.as_ptr() as *const u8,
                std::mem::size_of_val(&verts),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

            let a_pos = match gl.get_attrib_location(program, "a_pos") {
                Some(loc) => loc,
                None => {
                    gl.delete_buffer(vbo);
                    gl.delete_program(program);
                    gl.delete_texture(tex);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos attribute"));
                }
            };
            let a_uv = match gl.get_attrib_location(program, "a_uv") {
                Some(loc) => loc,
                None => {
                    gl.delete_buffer(vbo);
                    gl.delete_program(program);
                    gl.delete_texture(tex);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_uv attribute"));
                }
            };

            gl.use_program(Some(program));
            // Sampler unit 0 holds the atlas.
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            let u_atlas = gl.get_uniform_location(program, "u_atlas");
            gl.uniform_1_i32(u_atlas.as_ref(), 0);
            // Premultiplied text color (FS emits `vec4(color * a, a)`).
            // Layer opacity multiplies the rgb path; alpha stays driven
            // by the glyph atlas so partial-glyph edges still antialias.
            let r = text_color[0] * opacity;
            let g = text_color[1] * opacity;
            let b = text_color[2] * opacity;
            let u_text_color = gl.get_uniform_location(program, "u_text_color");
            gl.uniform_3_f32(u_text_color.as_ref(), r, g, b);

            // Premultiplied alpha blend so the glyph composite over the
            // already-cleared bg looks right.
            gl.enable(glow::BLEND);
            gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);

            let stride = (4 * std::mem::size_of::<f32>()) as i32;
            gl.enable_vertex_attrib_array(a_pos);
            gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, stride, 0);
            gl.enable_vertex_attrib_array(a_uv);
            gl.vertex_attrib_pointer_f32(
                a_uv,
                2,
                glow::FLOAT,
                false,
                stride,
                (2 * std::mem::size_of::<f32>()) as i32,
            );
            gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
            gl.disable_vertex_attrib_array(a_pos);
            gl.disable_vertex_attrib_array(a_uv);
            gl.disable(glow::BLEND);
            gl.delete_buffer(vbo);
            gl.delete_program(program);
            gl.delete_texture(tex);
            gl.flush();
        }
        Ok(())
    })?;
    eprintln!("text-layer render complete");
    Ok(())
}

/// Render a single solid-color frame, push it to the HDMI display via
/// legacy `drmModeSetCrtc`, and hold for `duration_secs` seconds.
///
/// `color` is RGBA in [0.0, 1.0] linear space. The vc4 HVS handles
/// gamma at scanout per the connector's Colorspace property — we just
/// hand it premultiplied float color and let the hardware do the rest.
pub fn render_solid_color(card: &Card, color: [f32; 4], duration_secs: u64) -> Result<()> {
    // Phase 4.1c: thin wrapper over `render_one_frame_to_hdmi`. The
    // GLES draw work is just `glClearColor` + `glClear`; everything
    // else (GBM bring-up, EGL context, swap, addFB, SetCrtc, hold,
    // teardown) is shared with `render_slide_bg_gradient` and the
    // upcoming pattern shaders.
    render_one_frame_to_hdmi(card, duration_secs, |gl, mode_w, mode_h| {
        use glow::HasContext;
        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(color[0], color[1], color[2], color[3]);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.flush();
        }
        Ok(())
    })?;
    eprintln!("solid-color render complete");
    Ok(())
}

/// Bridge between gbm::BufferObject and drm::buffer::Buffer. The two
/// crates are independent; gbm's BufferObject doesn't impl drm-rs's
/// Buffer trait. This newtype reads the four fields drm-rs's
/// `add_framebuffer` needs (size, format, pitch, handle) at construction
/// time so we can hand it across.
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
        // gbm::Format and drm-fourcc::DrmFourcc both wrap a fourcc u32.
        // The values match per the DRM_FORMAT_* spec; the enum names
        // match too (Argb8888, Xrgb8888, etc.). Safest path: get the
        // raw fourcc and rebuild on the drm side.
        let fourcc_bytes = gbm_fourcc_bytes(gbm_fmt);
        let format = DrmFourcc::try_from(u32::from_le_bytes(fourcc_bytes))
            .map_err(|e| anyhow!("unsupported drm fourcc from gbm format: {e}"))?;
        // gbm 0.15's BufferObject::handle returns a u32 wrapped; the
        // raw value is what drm-rs's Handle is built from.
        // gbm_bo_handle is a C union (u32_/s32/u64_/s64). For DRM
        // handles we always read u32_. Reading a union field is
        // unsafe in Rust regardless of the variants' layouts.
        let bo_handle = bo.handle().context("gbm bo handle")?;
        let raw_handle = unsafe { bo_handle.u32_ };
        let handle = DrmHandle::from(
            std::num::NonZeroU32::new(raw_handle)
                .ok_or_else(|| anyhow!("gbm bo handle was 0"))?,
        );
        Ok(Self {
            width,
            height,
            format,
            pitch: stride,
            handle,
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

/// gbm 0.15's Format enum doesn't expose `.bits()` or `Into<u32>`;
/// match on the variants we care about and delegate to `hdmi_logic`'s
/// shared lookup table so the bytes are tested against the DRM spec
/// in a host-runnable test.
fn gbm_fourcc_bytes(fmt: GbmFormat) -> [u8; 4] {
    let name = match fmt {
        GbmFormat::Argb8888 => "Argb8888",
        GbmFormat::Xrgb8888 => "Xrgb8888",
        GbmFormat::Abgr8888 => "Abgr8888",
        GbmFormat::Xbgr8888 => "Xbgr8888",
        GbmFormat::Rgba8888 => "Rgba8888",
        GbmFormat::Rgbx8888 => "Rgbx8888",
        _ => return [0, 0, 0, 0],
    };
    fourcc_for_argb_family(name).unwrap_or([0, 0, 0, 0])
}

/// Find the first connected connector and its largest mode. Mode
/// selection delegates to `hdmi_logic::pick_largest_mode_index` so
/// the tie-breaking + max-area logic is testable without a real DRM
/// connector.
fn pick_connector_and_mode(
    card: &Card,
    resources: &drm::control::ResourceHandles,
) -> Result<(connector::Info, Mode)> {
    for &handle in resources.connectors() {
        let info = card
            .get_connector(handle, false)
            .with_context(|| format!("get_connector({handle:?})"))?;
        if info.state() != ConnectorState::Connected {
            continue;
        }
        let specs: Vec<ModeSpec> = info
            .modes()
            .iter()
            .map(|m| {
                let (w, h) = m.size();
                ModeSpec {
                    width: w,
                    height: h,
                    vrefresh: m.vrefresh(),
                }
            })
            .collect();
        if let Some(idx) = pick_largest_mode_index(&specs) {
            // Copy the chosen Mode out of the borrow before moving info.
            let chosen = info.modes()[idx];
            return Ok((info, chosen));
        }
    }
    bail!("no connected connector with any modes")
}

// =====================================================================
// Phase 2.1 — atomic commit + double-buffered animated scanout.
// =====================================================================

/// Run a hue-rotating animation for `duration_secs` seconds via DRM
/// atomic commit. Each frame: render the next color, swap, lock the
/// new GBM front buffer, register it as a DRM framebuffer, atomic-
/// commit it as the primary plane's `FB_ID`, wait for the page-flip
/// event before queuing the next frame, release the previous frame's
/// BO + FB.
///
/// `fps` sets the animation speed (one full hue rotation per 6/fps×30
/// seconds). The page-flip event loop caps actual presentation to
/// display vrefresh regardless.
pub fn render_animated_atomic(card: &Card, duration_secs: u64, fps: u32) -> Result<()> {
    // DRM hides primary + cursor planes from the plane API by default
    // and rejects atomic commits unless the client opts in. These two
    // capabilities are sticky to this fd; set them before any
    // resource enumeration.
    card.set_client_capability(drm::ClientCapability::UniversalPlanes, true)
        .context("set_client_capability(UniversalPlanes) failed")?;
    card.set_client_capability(drm::ClientCapability::Atomic, true)
        .context("set_client_capability(Atomic) failed")?;

    let resources = card
        .resource_handles()
        .context("drmModeGetResources failed")?;
    let (connector_info, mode) = pick_connector_and_mode(card, &resources)
        .context("no connected HDMI connector with a usable mode")?;
    let (mode_w, mode_h) = mode.size();
    eprintln!(
        "selected connector {:?} {:?} at {}x{}@{}",
        connector_info.handle(),
        connector_info.interface(),
        mode_w,
        mode_h,
        mode.vrefresh(),
    );

    let encoder_handle = connector_info
        .current_encoder()
        .or_else(|| connector_info.encoders().first().copied())
        .ok_or_else(|| anyhow!("connector advertises no encoders"))?;
    let encoder_info = card
        .get_encoder(encoder_handle)
        .context("drmModeGetEncoder failed")?;
    let crtc_handle = encoder_info
        .crtc()
        .or_else(|| resources.crtcs().first().copied())
        .ok_or_else(|| anyhow!("no CRTC available for encoder {:?}", encoder_handle))?;

    // -----------------------------------------------------------------
    // Find the primary plane bound to (or compatible with) this CRTC.
    //
    // Atomic commit needs us to set FB_ID on a specific plane, not on
    // the CRTC. The DRM stack assigns a "type" property to each plane
    // — PRIMARY / OVERLAY / CURSOR — and we want the primary one.
    // -----------------------------------------------------------------
    let primary_plane = find_primary_plane(card, crtc_handle)
        .context("locate primary plane for CRTC")?;
    eprintln!(
        "using encoder {:?} crtc {:?} primary plane {:?}",
        encoder_handle, crtc_handle, primary_plane
    );

    // -----------------------------------------------------------------
    // Resolve the property IDs we need on each object once. drm-rs
    // makes you walk the property table to find a property by name;
    // doing it per-frame would be silly.
    // -----------------------------------------------------------------
    let crtc_props = ObjectProps::for_crtc(card, crtc_handle)
        .context("read CRTC properties")?;
    let conn_props = ObjectProps::for_connector(card, connector_info.handle())
        .context("read connector properties")?;
    let plane_props = ObjectProps::for_plane(card, primary_plane)
        .context("read primary-plane properties")?;

    let crtc_mode_id = crtc_props.find("MODE_ID")?;
    let crtc_active = crtc_props.find("ACTIVE")?;
    let conn_crtc_id = conn_props.find("CRTC_ID")?;
    let plane_crtc_id = plane_props.find("CRTC_ID")?;
    let plane_fb_id = plane_props.find("FB_ID")?;
    let plane_src_x = plane_props.find("SRC_X")?;
    let plane_src_y = plane_props.find("SRC_Y")?;
    let plane_src_w = plane_props.find("SRC_W")?;
    let plane_src_h = plane_props.find("SRC_H")?;
    let plane_crtc_x = plane_props.find("CRTC_X")?;
    let plane_crtc_y = plane_props.find("CRTC_Y")?;
    let plane_crtc_w = plane_props.find("CRTC_W")?;
    let plane_crtc_h = plane_props.find("CRTC_H")?;

    // -----------------------------------------------------------------
    // GBM + EGL + GLES2 setup (same as render_solid_color).
    // -----------------------------------------------------------------
    let gbm_dev = gbm::Device::new(card.0.try_clone().context("clone DRM fd for GBM")?)
        .context("gbm_create_device failed")?;
    let gbm_surface = gbm_dev
        .create_surface::<()>(
            mode_w as u32,
            mode_h as u32,
            GbmFormat::Argb8888,
            BufferObjectFlags::SCANOUT | BufferObjectFlags::RENDERING,
        )
        .context("gbm_surface_create failed")?;

    let egl_lib = unsafe {
        egl::DynamicInstance::<egl::EGL1_5>::load_required().map_err(|e| {
            anyhow!("eglDynamicInstance::<EGL1_5>::load_required failed: {e:?}")
        })?
    };
    let gbm_dev_ptr: *mut c_void = gbm_dev.as_raw() as *mut c_void;
    let native_display = gbm_dev_ptr as egl::NativeDisplayType;
    let display = unsafe {
        egl_lib
            .get_display(native_display)
            .ok_or_else(|| anyhow!("eglGetDisplay returned NO_DISPLAY"))?
    };
    egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
    egl_lib
        .bind_api(egl::OPENGL_ES_API)
        .map_err(|e| anyhow!("eglBindAPI(GLES) failed: {e:?}"))?;
    let cfg_attribs = [
        egl::SURFACE_TYPE,
        egl::WINDOW_BIT,
        egl::RED_SIZE,
        8,
        egl::GREEN_SIZE,
        8,
        egl::BLUE_SIZE,
        8,
        egl::ALPHA_SIZE,
        8,
        egl::RENDERABLE_TYPE,
        egl::OPENGL_ES2_BIT,
        egl::NONE,
    ];
    let configs = egl_lib
        .choose_first_config(display, &cfg_attribs)
        .map_err(|e| anyhow!("eglChooseConfig failed: {e:?}"))?
        .ok_or_else(|| anyhow!("no EGL config matched ARGB8888 + GLES2"))?;
    let ctx_attribs = [egl::CONTEXT_CLIENT_VERSION, 2, egl::NONE];
    let context = egl_lib
        .create_context(display, configs, None, &ctx_attribs)
        .map_err(|e| anyhow!("eglCreateContext failed: {e:?}"))?;
    let egl_surface = unsafe {
        let raw_surface = gbm_surface.as_raw_mut() as *mut c_void;
        egl_lib
            .create_window_surface(display, configs, raw_surface, None)
            .map_err(|e| anyhow!("eglCreateWindowSurface failed: {e:?}"))?
    };
    egl_lib
        .make_current(display, Some(egl_surface), Some(egl_surface), Some(context))
        .map_err(|e| anyhow!("eglMakeCurrent failed: {e:?}"))?;

    let gl = unsafe {
        glow::Context::from_loader_function(|name| {
            egl_lib
                .get_proc_address(name)
                .map(|fp| fp as *const _)
                .unwrap_or(ptr::null())
        })
    };

    // -----------------------------------------------------------------
    // Upload the current mode as a property blob — atomic commit
    // wants `MODE_ID` to point at a kernel-side blob, not at a Mode
    // value directly.
    // -----------------------------------------------------------------
    let mode_blob = card
        .create_property_blob(&mode)
        .context("create_property_blob(mode) failed")?;
    // create_property_blob returns a typed Value; we need the raw blob
    // id (a u64) to plumb through both the atomic-commit add_property
    // and the eventual destroy_property_blob.
    let mode_blob_id = match mode_blob {
        PropValue::Blob(id) => id,
        other => {
            // No resources held yet; safe to bail directly.
            bail!("create_property_blob returned unexpected variant: {other:?}")
        }
    };

    // -----------------------------------------------------------------
    // From here we have kernel-side resources (mode blob, EGL state,
    // future BOs+FBs) that leak if we early-return on error. Wrap the
    // animation work in an inner closure so cleanup runs unconditionally
    // regardless of whether the work succeeded or `?`-bailed.
    // -----------------------------------------------------------------
    use glow::HasContext;

    let start = Instant::now();
    let end = start + Duration::from_secs(duration_secs);
    let hue_period_secs = 6.0_f32 * 30.0_f32 / fps.max(1) as f32;

    let render_frame = |gl: &glow::Context, t: f32| {
        let hue = (t * 360.0 / hue_period_secs) % 360.0;
        let (r, g, b) = hsv_to_rgb(hue, 1.0, 1.0);
        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(r, g, b, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.flush();
        }
    };

    let src_w_fp16 = (mode_w as u32) << 16;
    let src_h_fp16 = (mode_h as u32) << 16;

    let mut bos: VecDeque<(BufferObject<()>, framebuffer::Handle)> = VecDeque::with_capacity(3);
    let mut frame_count: u64 = 1;

    let work: Result<()> = (|| {
        // Render frame 0 + ALLOW_MODESET commit that binds connector
        // → CRTC and primary plane → FB.
        render_frame(&gl, 0.0);
        egl_lib
            .swap_buffers(display, egl_surface)
            .map_err(|e| anyhow!("eglSwapBuffers (frame 0) failed: {e:?}"))?;
        let first_bo = unsafe {
            gbm_surface
                .lock_front_buffer()
                .context("gbm_surface_lock_front_buffer (frame 0) failed")?
        };
        let first_fb_buf = GbmBufferAdapter::new(&first_bo).context("first frame fb adapter")?;
        let first_fb = match card.add_framebuffer(&first_fb_buf, 32, 32) {
            Ok(fb) => fb,
            Err(e) => {
                drop(first_bo);
                return Err(anyhow!("drmModeAddFB (frame 0) failed: {e}"));
            }
        };

        let mut req = AtomicModeReq::new();
        req.add_property(crtc_handle, crtc_mode_id, PropValue::Blob(mode_blob_id));
        req.add_property(crtc_handle, crtc_active, PropValue::Boolean(true));
        req.add_property(connector_info.handle(), conn_crtc_id, PropValue::CRTC(Some(crtc_handle)));
        req.add_property(primary_plane, plane_crtc_id, PropValue::CRTC(Some(crtc_handle)));
        req.add_property(primary_plane, plane_fb_id, PropValue::Framebuffer(Some(first_fb)));
        req.add_property(primary_plane, plane_src_x, PropValue::UnsignedRange(0));
        req.add_property(primary_plane, plane_src_y, PropValue::UnsignedRange(0));
        req.add_property(primary_plane, plane_src_w, PropValue::UnsignedRange(src_w_fp16 as u64));
        req.add_property(primary_plane, plane_src_h, PropValue::UnsignedRange(src_h_fp16 as u64));
        req.add_property(primary_plane, plane_crtc_x, PropValue::SignedRange(0));
        req.add_property(primary_plane, plane_crtc_y, PropValue::SignedRange(0));
        req.add_property(primary_plane, plane_crtc_w, PropValue::UnsignedRange(mode_w as u64));
        req.add_property(primary_plane, plane_crtc_h, PropValue::UnsignedRange(mode_h as u64));
        if let Err(e) = card.atomic_commit(AtomicCommitFlags::ALLOW_MODESET, req) {
            // Initial commit failed; we own first_bo + first_fb but
            // they're not on scanout. Release before bailing — the
            // outer cleanup only handles bos[].
            let _ = card.destroy_framebuffer(first_fb);
            drop(first_bo);
            return Err(anyhow!("initial atomic_commit (mode-set) failed: {e}"));
        }
        bos.push_back((first_bo, first_fb));
        eprintln!(
            "scanout active via atomic commit; animating for {}s at target {} fps",
            duration_secs, fps
        );

        // Per-frame loop: render → swap → lock new BO → addFB → atomic
        // page-flip → wait for event → release the prior BO+FB.
        while Instant::now() < end {
            let t = start.elapsed().as_secs_f32();
            render_frame(&gl, t);
            egl_lib
                .swap_buffers(display, egl_surface)
                .map_err(|e| anyhow!("eglSwapBuffers (frame {frame_count}) failed: {e:?}"))?;
            let bo = unsafe {
                gbm_surface
                    .lock_front_buffer()
                    .with_context(|| format!("lock_front_buffer (frame {frame_count})"))?
            };
            let fb_buf = GbmBufferAdapter::new(&bo)
                .with_context(|| format!("fb adapter (frame {frame_count})"))?;
            let fb = match card.add_framebuffer(&fb_buf, 32, 32) {
                Ok(fb) => fb,
                Err(e) => {
                    drop(bo);
                    return Err(anyhow!("add_framebuffer (frame {frame_count}) failed: {e}"));
                }
            };

            let mut req = AtomicModeReq::new();
            req.add_property(primary_plane, plane_fb_id, PropValue::Framebuffer(Some(fb)));
            // PAGE_FLIP_EVENT asks the kernel to deliver an event on
            // the DRM fd when this commit reaches scanout. NONBLOCK
            // lets the commit return immediately; we drain the event
            // below.
            let flags = AtomicCommitFlags::PAGE_FLIP_EVENT | AtomicCommitFlags::NONBLOCK;
            if let Err(e) = card.atomic_commit(flags, req) {
                let _ = card.destroy_framebuffer(fb);
                drop(bo);
                return Err(anyhow!("atomic_commit (page-flip frame {frame_count}) failed: {e}"));
            }

            // Drain the page-flip event the atomic commit just queued.
            // drm-rs's `receive_events` is *non-blocking* — it returns
            // whatever's currently pending in the fd's event queue.
            //
            // Probed 2026-05-06 (--animate 3s @ 30 fps target, vc4
            // 1024×768@60): events=1 on 179/179 frames, 59.3 fps avg
            // matching display vrefresh. Every event was already
            // queued by the time we got here. The actual vsync gate
            // is `eglSwapBuffers` — vc4's Mesa EGL is vsync-locked by
            // default, so swap blocks ~16.7 ms. receive_events just
            // drains the resulting page-flip event; it's not the
            // gate.
            //
            // Implication for Phase 4: if we ever render off-screen
            // (no EGL swap to gate us), we have to block on the DRM
            // fd ourselves via poll(2) — receive_events alone won't
            // wait. Until then, EGL-swap + drain is correct and
            // tear-free.
            let _events = card
                .receive_events()
                .context("receive_events after atomic commit")?;

            bos.push_back((bo, fb));
            // Keep last 2 BOs/FBs alive: the one currently scanning
            // out and the one we just queued. The older one is now
            // safely off-screen.
            while bos.len() > 2 {
                let (old_bo, old_fb) = bos.pop_front().unwrap();
                if let Err(e) = card.destroy_framebuffer(old_fb) {
                    eprintln!("warn: destroy_framebuffer(old_fb) on hot loop: {e}");
                }
                drop(old_bo);
            }
            frame_count += 1;
        }
        eprintln!(
            "rendered {} frames in {:.2}s ({:.1} fps avg)",
            frame_count,
            start.elapsed().as_secs_f32(),
            frame_count as f32 / start.elapsed().as_secs_f32(),
        );
        Ok(())
    })();

    // -----------------------------------------------------------------
    // Cleanup runs unconditionally — both the success and error paths
    // pass through here. We log but don't propagate cleanup errors,
    // since they'd hide the original cause.
    //
    // Order matters:
    //   1. Unbind the EGL context (so destroys are valid).
    //   2. Destroy EGL context + surface, terminate display.
    //   3. drmModeRmFB on every queued framebuffer.
    //   4. Drop all GBM BOs.
    //   5. drmModeDestroyPropertyBlob on the mode blob.
    //
    // gbm_surface and gbm_dev fall out via Drop on scope exit.
    //
    // drmDropMaster is NOT called explicitly here. We never call
    // drmSetMaster — the kernel drops master on fd close (Card's
    // File field) when the renderer exits. A long-running renderer
    // process holding master across requests would need an explicit
    // Drop on Card to cover crash-mid-run; deferring that to the
    // sidecar IPC slice (plan §5) where Card outlives a single render.
    // -----------------------------------------------------------------
    if let Err(e) = egl_lib.make_current(display, None, None, None) {
        eprintln!("warn: eglMakeCurrent(unbind): {e:?}");
    }
    if let Err(e) = egl_lib.destroy_context(display, context) {
        eprintln!("warn: eglDestroyContext: {e:?}");
    }
    if let Err(e) = egl_lib.destroy_surface(display, egl_surface) {
        eprintln!("warn: eglDestroySurface: {e:?}");
    }
    if let Err(e) = egl_lib.terminate(display) {
        eprintln!("warn: eglTerminate: {e:?}");
    }
    for (bo, fb) in bos.drain(..) {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
        }
        drop(bo);
    }
    if let Err(e) = card.destroy_property_blob(mode_blob_id) {
        eprintln!("warn: destroy_property_blob({mode_blob_id}): {e}");
    }

    work?;

    eprintln!("animated atomic render complete");
    Ok(())
}

/// Find a PRIMARY-type plane that the given CRTC can drive. drm-rs
/// exposes plane info but not the plane's TYPE property directly —
/// we walk the plane's properties looking for `type` = PRIMARY.
fn find_primary_plane(card: &Card, crtc_handle: drm::control::crtc::Handle) -> Result<plane::Handle> {
    let plane_handles = card.plane_handles().context("plane_handles failed")?;
    let resources = card.resource_handles().context("resource_handles failed")?;
    // Find which bit in possible_crtcs corresponds to our chosen CRTC.
    let crtc_bit_index = resources
        .crtcs()
        .iter()
        .position(|&c| c == crtc_handle)
        .ok_or_else(|| anyhow!("CRTC {crtc_handle:?} not in resource list"))?;
    let crtc_mask: u32 = 1 << crtc_bit_index;

    for &handle in plane_handles.iter() {
        let plane_info = match card.get_plane(handle) {
            Ok(p) => p,
            Err(_) => continue,
        };
        // possible_crtcs's bits map onto resources.crtcs(). We can't
        // read the wrapper's bits directly — drm 0.12 keeps the u32
        // pub(crate). Fall back to formatting the Debug repr and
        // parsing it; lifted to hdmi_logic::parse_crtc_list_filter_bits
        // so a drm-rs Debug-derive change is caught by the host
        // test gate, not by a runtime regression.
        let possible_dbg = format!("{:?}", plane_info.possible_crtcs());
        let possible_bits = parse_crtc_list_filter_bits(&possible_dbg).unwrap_or(0);
        if (possible_bits & crtc_mask) == 0 {
            continue;
        }
        // Walk this plane's properties looking for "type" = PRIMARY.
        let plane_props = match card.get_properties(handle) {
            Ok(p) => p,
            Err(_) => continue,
        };
        let (prop_ids, prop_vals) = plane_props.as_props_and_values();
        for (prop_id, val) in prop_ids.iter().zip(prop_vals.iter()) {
            let info = match card.get_property(*prop_id) {
                Ok(info) => info,
                Err(_) => continue,
            };
            if info.name().to_string_lossy() != "type" {
                continue;
            }
            // Plane type values: 0 = OVERLAY, 1 = PRIMARY, 2 = CURSOR.
            // (DRM_PLANE_TYPE_PRIMARY = 1.)
            if *val == 1 {
                return Ok(handle);
            }
        }
    }
    bail!("no PRIMARY plane found for CRTC {crtc_handle:?}");
}

/// Per-object property table — name → property ID lookup, built once
/// per object and reused per frame.
struct ObjectProps {
    entries: Vec<(String, property::Handle)>,
}

impl ObjectProps {
    fn for_crtc(card: &Card, h: drm::control::crtc::Handle) -> Result<Self> {
        Self::collect(card, card.get_properties(h)?)
    }
    fn for_connector(card: &Card, h: connector::Handle) -> Result<Self> {
        Self::collect(card, card.get_properties(h)?)
    }
    fn for_plane(card: &Card, h: plane::Handle) -> Result<Self> {
        Self::collect(card, card.get_properties(h)?)
    }
    fn collect(card: &Card, props: drm::control::PropertyValueSet) -> Result<Self> {
        let (ids, _vals) = props.as_props_and_values();
        let mut entries = Vec::with_capacity(ids.len());
        for id in ids {
            if let Ok(info) = card.get_property(*id) {
                let name = info.name().to_string_lossy().into_owned();
                entries.push((name, *id));
            }
        }
        Ok(Self { entries })
    }
    fn find(&self, name: &str) -> Result<property::Handle> {
        self.entries
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, id)| *id)
            .ok_or_else(|| anyhow!("property {name:?} not found on object"))
    }
}

