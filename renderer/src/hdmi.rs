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

use std::path::Path;
use std::rc::Rc;
use uuid::Uuid;

use crate::content::{
    load_playlist, resolve_reel_items, solid_bg_hex, TextSlide,
};
use crate::hdmi_logic::{
    box_to_ndc_quad, clamp_transition_ms, compute_motion_state, effective_font_size_px,
    effective_hold_ms, format_auto_text, fourcc_for_argb_family, fs_for_transition_kind,
    gradient_uniforms, hex_to_rgba, hsv_to_rgb, layout_text_to_alpha, motion_offset_to_px,
    parse_crtc_list_filter_bits, parse_h_align, parse_motion_kind, pick_largest_mode_index,
    prev_idx_for_reel, unix_to_calendar_utc, AlphaBitmap, FontCatalog, ModeSpec, MotionKind,
    MotionState, VAlign, FS_BLIT, FS_CUT, FS_FADE, FS_GLYPH, FS_GLYPH_OUTLINE,
    FS_GRADIENT, VS_FULLSCREEN_QUAD, VS_TEXTURED_QUAD,
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
/// Hold for `hold_ms` milliseconds. Cleanup runs unconditionally
/// (warn-on-Err) regardless of whether the closure succeeded —
/// matches the Phase 3 followups pattern.
///
/// v1-spec-delta #1 (2026-05-07): hold parameter is now ms, not
/// seconds. The FYS Panic flash slides at 130/350/500/800 ms
/// were previously snapping to a 1-second floor inside
/// `effective_hold_secs`'s `/1000` truncation.
///
/// Phase 4.1c — extracted from `render_solid_color` and the
/// (then-public) gradient-render path now that we have two callers.
/// Phase 4.1d+ bg-pattern shaders reuse this helper directly; Phase
/// 4.2b's `draw_*` helpers compose under the same closure too.
///
/// `draw` receives the GLES2 context and the viewport (mode_w,
/// mode_h) so the closure can `glViewport`, `glClear`, or
/// compile/link/draw a quad without re-deriving size.
fn render_one_frame_to_hdmi<F>(card: &Card, hold_ms: u64, draw: F) -> Result<()>
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
            "scanout active on {:?}; holding for {}ms",
            crtc_handle, hold_ms
        );
        std::thread::sleep(std::time::Duration::from_millis(hold_ms));
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

/// v1-spec-delta #2 (slice c-2) — per-frame animated render path
/// for a TextSlide containing one or more non-static layers.
///
/// Architecture mirrors `render_one_frame_to_hdmi`: GBM + EGL +
/// GLES2 bring-up, then a loop that paints, swaps, locks the
/// front BO, adds a DRM framebuffer, and pushes it to scanout via
/// legacy `drmModeSetCrtc`. The previous frame's (BO, FB) is held
/// until the next SetCrtc commits, then released — N-1 rotation
/// matches the dev Pi's vc4-double-buffered GBM surface.
///
/// Pacing: target `fps`, naive `Instant::now`-based sleep loop.
/// Frame-time is dominated by EGL bring-up (~500 ms one-shot) +
/// the per-frame `drmModeSetCrtc` (~16 ms) — at 30 fps the SetCrtc
/// cost alone is half the frame budget. Slice (e+) can refactor to
/// atomic page flips. For v1 functional motion this is sufficient.
///
/// `hold_ms` is the spec'd slide duration (ms-precision per item
/// #1); the loop runs until `start.elapsed() >= hold_ms` regardless
/// of how many frames actually rendered. A frame in flight when
/// the deadline hits is allowed to complete (no mid-frame abort).
#[allow(clippy::too_many_arguments)]
fn render_animated_slide(
    card: &Card,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    slide_id: Uuid,
    hold_ms: u64,
    fps: u32,
) -> Result<()> {
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
    egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
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

    // Hold (BO, FB) of the previous frame across the loop body so
    // the kernel is never asked to scan out a destroyed BO. After
    // the next `drmModeSetCrtc` commits, the previous FB is
    // detached and safe to destroy.
    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    // Frame deadline tracking.
    let frame_period_ns: u64 = 1_000_000_000_u64 / fps.max(1) as u64;
    let start = std::time::Instant::now();
    let mut frames: u32 = 0;
    // v1-spec-delta #3 (slice b QA followup): glyph rasterization
    // cache lives across the per-frame loop. layout_text_to_alpha
    // fires only when resolved_text changes (motion-only paths
    // never refresh; auto_mode=time refreshes 1x/sec instead of
    // 30x/sec). Without this, the per-frame render at 1080p hits
    // the fontdue ~50ms/layer bottleneck on every iteration.
    let mut glyph_cache: GlyphCache = Vec::with_capacity(text_layers.len());
    glyph_cache.resize_with(text_layers.len(), || None);

    let work: Result<()> = (|| {
        use glow::HasContext;
        loop {
            let elapsed = start.elapsed();
            let elapsed_ms = elapsed.as_millis() as u64;
            if elapsed_ms >= hold_ms {
                break;
            }
            let tick_seconds = elapsed.as_secs_f64();
            let motion_states =
                motion_states_for_layers(slide_id, text_layers, tick_seconds);
            let wall_clock_unix = current_unix_seconds();
            paint_slide(
                &gl,
                mode_w as u32,
                mode_h as u32,
                bg_kind,
                text_layers,
                Some(&motion_states),
                wall_clock_unix,
                Some(&mut glyph_cache),
            )?;
            unsafe { gl.flush(); }
            egl_lib
                .swap_buffers(display, egl_surface)
                .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
            let bo = unsafe {
                gbm_surface
                    .lock_front_buffer()
                    .context("gbm_surface_lock_front_buffer failed")?
            };
            let fb_buf = GbmBufferAdapter::new(&bo).context("read GBM bo metadata")?;
            let fb = card
                .add_framebuffer(&fb_buf, 32, 32)
                .map_err(|e| anyhow!("drmModeAddFB failed: {e}"))?;
            // QA F2 (slice c carry-over): on SetCrtc fail, the
            // just-added fb is a u32 with no Drop and would leak.
            // Explicitly rmFB on the unhappy path. The BO Drops
            // cleanly via gbm RAII either way.
            if let Err(e) = card.set_crtc(
                crtc_handle,
                Some(fb),
                (0, 0),
                &[connector_info.handle()],
                Some(mode),
            ) {
                if let Err(de) = card.destroy_framebuffer(fb) {
                    eprintln!(
                        "warn: cleanup destroy_framebuffer({fb:?}) on SetCrtc-fail: {de}"
                    );
                }
                drop(bo);
                return Err(anyhow!("drmModeSetCrtc failed: {e}"));
            }

            // Previous frame is no longer scanout — safe to release.
            if let Some(old_fb) = prev_fb.take() {
                if let Err(e) = card.destroy_framebuffer(old_fb) {
                    eprintln!("warn: destroy_framebuffer({old_fb:?}): {e}");
                }
            }
            if let Some(old_bo) = prev_bo.take() {
                drop(old_bo);
            }
            prev_bo = Some(bo);
            prev_fb = Some(fb);
            frames += 1;

            // Pace to fps. next-deadline math, not sleep-by-period
            // — accumulated drift would walk us off cadence after a
            // few seconds.
            let next_deadline_ns = (frames as u64).wrapping_mul(frame_period_ns);
            let now = start.elapsed().as_nanos() as u64;
            if next_deadline_ns > now {
                std::thread::sleep(std::time::Duration::from_nanos(
                    next_deadline_ns - now,
                ));
            }
        }
        eprintln!(
            "animated slide complete: {frames} frames in {}ms",
            start.elapsed().as_millis()
        );
        Ok(())
    })();

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
    if let Some(bo) = prev_bo {
        drop(bo);
    }
    if let Some(fb) = prev_fb {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
        }
    }

    work?;
    Ok(())
}

/// Draw a two-color linear gradient that fills the viewport. The
/// fragment shader matches Python's PIL reference (image-space y,
/// flipped from gl_FragCoord). Phase 4.2b extracted into a helper
/// so `render_slide` can compose it with the text pass in one
/// closure.
fn draw_gradient_pattern(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    color_a: [f32; 4],
    color_b: [f32; 4],
    density: f32,
) -> Result<()> {
    use glow::HasContext;
    let g = gradient_uniforms(mode_w, mode_h, density);
    unsafe {
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
    }
    Ok(())
}

/// Clear the viewport to a solid RGBA. Trivial helper extracted so
/// the bg dispatch in `render_slide` is purely structural — the
/// closure's match arm reads as "gradient or clear" without
/// inlined GLES.
fn draw_solid_clear(gl: &glow::Context, color: [f32; 4]) {
    use glow::HasContext;
    unsafe {
        gl.clear_color(color[0], color[1], color[2], color[3]);
        gl.clear(glow::COLOR_BUFFER_BIT);
    }
}

/// Phase 4.2 — rasterize and draw a single text layer's text on top
/// of whatever's already in the framebuffer. Premultiplied-alpha
/// blend so the glyph composites cleanly over the bg pass.
///
/// `text_color` is the layer's `text_color` already parsed from hex
/// (caller does it once outside the closure for early error
/// reporting). `opacity` is the layer's opacity in [0, 1] —
/// multiplied into the rgb channel so antialiased edges still
/// resolve via the glyph alpha.
///
/// Resource discipline matches `draw_gradient_pattern`'s pattern:
/// every early-return path frees the right subset (texture /
/// texture+program / texture+program+vbo); happy path frees all
/// three.
fn draw_text_layer(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    layer: &crate::content::TextLayer,
    text_color: [f32; 4],
    motion_kind: MotionKind,
    motion_state: MotionState,
    bm: &AlphaBitmap,
) -> Result<()> {
    use glow::HasContext;

    // Phase 4.2c: real Python-model semantics via the
    // host-tested `effective_font_size_px` helper. font_size_pct =
    // percent-of-box-WIDTH.
    let size_px = effective_font_size_px(
        layer.font_size_px,
        layer.font_size_pct,
        layer.r#box.w,
        mode_w,
    );

    // v1-spec-delta #3 (slice b cache, QA followup): the alpha
    // bitmap is pre-rasterized by paint_slide via the
    // (resolved_text -> bitmap) cache. draw_text_layer is now
    // GPU-only -- no fontdue calls per frame on cache hits.

    // v1-spec-delta #2 (slice c-1): apply per-layer motion.
    // alpha_mul folds in pulse / blink. A fully transparent layer
    // (e.g. blink off-half) skips the GPU work entirely.
    let opacity =
        (layer.opacity.clamp(0.0, 1.0) * motion_state.alpha_mul.clamp(0.0, 1.0))
            .clamp(0.0, 1.0);
    if opacity < 1e-3 {
        return Ok(());
    }
    let halign = parse_h_align(&layer.text_align);
    // The Python content model has no v-align field. Phase 4.2c
    // matches the Python auto_render reference behavior of vertical-
    // centering text inside the box. If a v-align field lands later,
    // route through `parse_v_align(layer.v_align)`.
    let valign = VAlign::Middle;

    unsafe {
        // -- Glyph atlas as a LUMINANCE texture. GLES2 doesn't
        // expose GL_RED; LUMINANCE is the analog for single-channel
        // grayscale and returns the value in r/g/b/a on sample
        // (FS_GLYPH reads `.r`).
        let tex = gl
            .create_texture()
            .map_err(|e| anyhow!("glGenTextures: {e}"))?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        // Tightly-packed 1-byte rows. R2: restore the default of 4
        // before returning so a future persistent context (4.3+)
        // can't accidentally inherit this state.
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
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);

        // -- Build the textured quad in NDC via the host-tested
        // `box_to_ndc_quad` helper. Scale-down-only (no upscaling),
        // aligned per `halign`/`valign` inside the box.
        let (mut ndc_l, mut ndc_r, mut ndc_t, mut ndc_b) = box_to_ndc_quad(
            layer.r#box.x,
            layer.r#box.y,
            layer.r#box.w,
            layer.r#box.h,
            bm.width,
            bm.height,
            mode_w,
            mode_h,
            halign,
            valign,
        );

        // v1-spec-delta #2 (slice c-1): breathe scales the rendered
        // quad around the box center (not the glyph bbox center —
        // see motion-spec.md §"breathe pivot"). Operator-authored
        // offset within the box is preserved because we scale the
        // already-aligned quad about the box center.
        let scale = motion_state.scale.max(0.05);
        if (scale - 1.0).abs() > 1e-4 {
            let box_cx_ndc = (layer.r#box.x + layer.r#box.w * 0.5) * 2.0 - 1.0;
            let box_cy_ndc = 1.0 - (layer.r#box.y + layer.r#box.h * 0.5) * 2.0;
            ndc_l = box_cx_ndc + scale * (ndc_l - box_cx_ndc);
            ndc_r = box_cx_ndc + scale * (ndc_r - box_cx_ndc);
            ndc_t = box_cy_ndc + scale * (ndc_t - box_cy_ndc);
            ndc_b = box_cy_ndc + scale * (ndc_b - box_cy_ndc);
        }

        // v1-spec-delta #2 (slice c-1): translation for ticker /
        // bounce / shake. motion_offset_to_px applies the spec's
        // per-effect unit convention (box-width / box-height /
        // glyph-height). Convert the resulting pixel offset into
        // NDC and shift the quad. Note: NDC y is up, screen y is
        // down, hence the negation.
        let box_w_px = (layer.r#box.w * mode_w as f32).max(1.0);
        let box_h_px = (layer.r#box.h * mode_h as f32).max(1.0);
        let (dx_px, dy_px) =
            motion_offset_to_px(motion_kind, motion_state, box_w_px, box_h_px, size_px);
        if dx_px.abs() > 1e-4 || dy_px.abs() > 1e-4 {
            let dx_ndc = (dx_px / mode_w as f32) * 2.0;
            let dy_ndc = -(dy_px / mode_h as f32) * 2.0;
            ndc_l += dx_ndc;
            ndc_r += dx_ndc;
            ndc_t += dy_ndc;
            ndc_b += dy_ndc;
        }
        // Verts: TRIANGLE_STRIP order BL, BR, TL, TR. Each vert is
        // [x, y, u, v]. UV (0,0) is top-left of the bitmap, which
        // matches our row-major top-down `data`.
        let verts: [f32; 16] = [
            ndc_l, ndc_b, 0.0, 1.0,
            ndc_r, ndc_b, 1.0, 1.0,
            ndc_l, ndc_t, 0.0, 0.0,
            ndc_r, ndc_t, 1.0, 0.0,
        ];

        // v1-spec-delta #4 (b): outline=true picks the dilated-
        // alpha shader; outline=false uses FS_GLYPH (cheap path).
        // Both write premultiplied-alpha output.
        let fs = if layer.outline { FS_GLYPH_OUTLINE } else { FS_GLYPH };
        let program = match link_program(gl, VS_TEXTURED_QUAD, fs) {
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
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        let u_atlas = gl.get_uniform_location(program, "u_atlas");
        gl.uniform_1_i32(u_atlas.as_ref(), 0);
        // v1-spec-delta #4: opacity goes through u_opacity uniform
        // so the shader multiplies BOTH RGB and the output alpha
        // by it. Pre-fix this code multiplied only the RGB
        // channels into u_text_color, leaving the output alpha at
        // `a` regardless of opacity -- which made an opacity=0.5
        // glyph fully cover the bg instead of letting half through.
        let u_text_color = gl.get_uniform_location(program, "u_text_color");
        gl.uniform_3_f32(u_text_color.as_ref(), text_color[0], text_color[1], text_color[2]);
        let u_opacity = gl.get_uniform_location(program, "u_opacity");
        gl.uniform_1_f32(u_opacity.as_ref(), opacity);
        if layer.outline {
            // v1-spec-delta #4 (b): hardcoded 1px black outline,
            // matching the Python convention at backend/openmarquee/
            // motion.py:341 ('outline_color = #000000'). The schema
            // is just `outline: bool`; future schema growth could
            // expose color + width through these uniforms without
            // a shader rewrite.
            let u_outline_color = gl.get_uniform_location(program, "u_outline_color");
            gl.uniform_3_f32(u_outline_color.as_ref(), 0.0, 0.0, 0.0);
            let u_pixel_size = gl.get_uniform_location(program, "u_pixel_size");
            gl.uniform_2_f32(
                u_pixel_size.as_ref(),
                1.0 / bm.width as f32,
                1.0 / bm.height as f32,
            );
        }

        // BLEND state is set by the caller (render_slide) once
        // around the layer loop — same blend func for every layer,
        // so toggling per-layer would just churn driver state.

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
        gl.delete_buffer(vbo);
        gl.delete_program(program);
        gl.delete_texture(tex);
    }
    Ok(())
}

/// Resolved background-pass kind. Pre-resolved before the render
/// closure so any hex-parse or pattern-name issues surface as a
/// clean Err before EGL bring-up.
enum BgKind {
    Gradient {
        color_a: [f32; 4],
        color_b: [f32; 4],
        density: f32,
    },
    Solid([f32; 4]),
}

fn resolve_slide_bg(slide: &TextSlide) -> Result<(BgKind, &'static str)> {
    if let Some(p) = &slide.background_pattern {
        if p.pattern == "gradient" {
            let color_a = hex_to_rgba(&p.color_a)
                .ok_or_else(|| anyhow!("invalid color_a {:?} for slide {}", p.color_a, slide.id))?;
            let color_b = hex_to_rgba(&p.color_b)
                .ok_or_else(|| anyhow!("invalid color_b {:?} for slide {}", p.color_b, slide.id))?;
            return Ok((
                BgKind::Gradient { color_a, color_b, density: p.density },
                "gradient",
            ));
        }
    }
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
    let hex = solid_bg_hex(slide).to_string();
    let color = hex_to_rgba(&hex)
        .ok_or_else(|| anyhow!("invalid hex color {hex:?} for slide {}", slide.id))?;
    let label = match pattern_label {
        "solid" => "solid",
        _ => "none",
    };
    Ok((BgKind::Solid(color), label))
}

/// Phase 4.2b — render a TextSlide as bg + first text layer in ONE
/// frame on the shared `render_one_frame_to_hdmi` harness. Pattern
/// dispatch:
///   - `gradient` → fragment-shader gradient (Phase 4.1b)
///   - `solid`    → color_a as solid fill (Phase 4.1a)
///   - `<other>`  → fall back to background_color + warn (4.1d
///                  fills these in)
///   - None       → background_color
///
/// When `font` is provided AND the slide has a visible non-empty
/// text_layer, the first such layer is rasterized + composited over
/// the bg via the glyph-shader path. Phase 4.2c iterates over ALL
/// visible non-empty text_layers (front-to-back per the model),
/// supports `text_align`, scale-to-fit, and font catalog lookup
/// per-layer via `layer.font_family`.
pub fn render_slide(
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    hold_ms: u64,
) -> Result<()> {
    let (bg_kind, pattern_label, text_layers) = resolve_slide_layers(slide, fonts)?;

    let bg_log = match &bg_kind {
        BgKind::Gradient { density, .. } => format!("pattern=gradient density={density:.3}"),
        BgKind::Solid(c) => format!(
            "pattern={pattern_label} bg=[{:.3},{:.3},{:.3}]",
            c[0], c[1], c[2]
        ),
    };
    eprintln!(
        "rendering slide {} ({:?}) {bg_log} text_layers={} for {}ms",
        slide.id,
        slide.name,
        text_layers.len(),
        hold_ms,
    );

    // v1-spec-delta #2 (slice c-2): dispatch on whether ANY layer
    // is animated. Static-only slides keep the cheap one-shot
    // bring-up + sleep path (no perf regression on FYS today).
    // Animated slides take the per-frame loop with the same legacy
    // SetCrtc bring-up. 30 fps is the target, picked to match
    // spec §11's frame-rate ask.
    // v1-spec-delta #3: auto_mode-set layers also force the
    // animated dispatch (text changes every second, so the slide
    // can't be one-shot). Layers with motion=static AND auto_mode
    // unset stay in the cheap one-shot path. FYS today has neither
    // motion nor auto_mode, so behavior is unchanged.
    let any_animated = text_layers.iter().any(|(layer, _, _)| {
        parse_motion_kind(&layer.motion) != MotionKind::Static
            || layer.auto_mode.is_some()
    });
    if any_animated {
        eprintln!("slide has animated/auto_mode layers — entering per-frame loop @ 30 fps");
        render_animated_slide(card, &bg_kind, &text_layers, slide.id, hold_ms, 30)?;
    } else {
        let motion_states = motion_states_for_layers(slide.id, &text_layers, 0.0);
        let wall_clock_unix = current_unix_seconds();
        render_one_frame_to_hdmi(card, hold_ms, |gl, mode_w, mode_h| {
            use glow::HasContext;
            paint_slide(
                gl,
                mode_w,
                mode_h,
                &bg_kind,
                &text_layers,
                Some(&motion_states),
                wall_clock_unix,
                None,
            )?;
            unsafe { gl.flush(); }
            Ok(())
        })?;
    }
    eprintln!("slide render complete");
    Ok(())
}

/// Resolve a stable u64 RNG seed for a text layer at `index` within
/// `slide_id`. The TextLayer schema has no `id` field, so the
/// renderer derives identity from (slide UUID, layer index). Stable
/// across reloads as long as the operator doesn't reorder layers
/// (which would re-seed shake — acceptable; reorder is a
/// deliberate edit, not an idle re-render).
/// v1-spec-delta #3 -- current Unix timestamp in seconds, for
/// auto_mode time/date/day substitution. Saturating cast on the
/// pre-1970 / post-2262 edges (both fall outside the dev Pi's
/// realistic operating range; the saturating behavior just avoids
/// a panic if the system clock is wedged).
fn current_unix_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn layer_id_seed(slide_id: Uuid, index: usize) -> u64 {
    let bytes = slide_id.as_bytes();
    let high = u64::from_le_bytes([
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
    ]);
    let low = u64::from_le_bytes([
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14],
        bytes[15],
    ]);
    high ^ low.rotate_left(13) ^ (index as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)
}

/// Build a motion state vector parallel to `text_layers` at the
/// given tick. Pure helper used by render_slide (and render_animated
/// _slide once slice c-2 lands) to avoid duplicating the per-layer
/// resolve loop.
fn motion_states_for_layers(
    slide_id: Uuid,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    tick_seconds: f64,
) -> Vec<MotionState> {
    text_layers
        .iter()
        .enumerate()
        .map(|(i, (layer, _, _))| {
            let kind = parse_motion_kind(&layer.motion);
            compute_motion_state(
                kind,
                layer.motion_intensity,
                layer.motion_phase,
                layer.motion_speed,
                layer_id_seed(slide_id, i),
                tick_seconds,
            )
        })
        .collect()
}

/// Phase 5-b — create an FBO + RGBA color texture sized to the
/// mode, paint the slide into it, then leave the binding on the
/// default FB. Returns `(fbo, color_tex)` on success — caller is
/// responsible for `delete_framebuffer` + `delete_texture` after
/// they're done sampling. On any failure, all created resources
/// are freed before propagating Err.
///
/// Used by render_fade_composite (Phase 5-b-1) to materialize
/// slide_a and slide_b textures that the fade shader samples.
unsafe fn make_slide_fbo(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(slide_fbo): {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::RGBA as i32,
        mode_w as i32,
        mode_h as i32,
        0,
        glow::RGBA,
        glow::UNSIGNED_BYTE,
        None,
    );
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
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
    let fbo = match gl.create_framebuffer() {
        Ok(f) => f,
        Err(e) => {
            gl.delete_texture(tex);
            return Err(anyhow!("glGenFramebuffers(slide_fbo): {e}"));
        }
    };
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
    gl.framebuffer_texture_2d(
        glow::FRAMEBUFFER,
        glow::COLOR_ATTACHMENT0,
        glow::TEXTURE_2D,
        Some(tex),
        0,
    );
    let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
    if status != glow::FRAMEBUFFER_COMPLETE {
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        return Err(anyhow!("framebuffer incomplete (slide_fbo): status=0x{status:x}"));
    }
    // v1-spec-delta #2 (slice c-1): FBO bake takes the static
    // snapshot path. Slice (d) — motion through transitions —
    // passes per-frame motion states inside render_transition_
    // animated; this make_slide_fbo path is the initial bake, so
    // None is correct.
    // v1-spec-delta #3: pass current wall-clock so any auto_mode
    // layer in the FBO bake renders the right time-of-day.
    let paint_result = paint_slide(
        gl,
        mode_w,
        mode_h,
        bg_kind,
        text_layers,
        None,
        current_unix_seconds(),
        None,
    );
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    if let Err(e) = paint_result {
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        return Err(e);
    }
    Ok((fbo, tex))
}

/// Resolve a slide's bg + visible non-empty text layers up-front,
/// shared by render_slide / render_slide_via_fbo /
/// render_fade_composite. Pre-EGL validation: malformed hex colors
/// error before we bring up the scanout pipeline.
///
/// Layers whose font fails to load OR whose text_color is malformed
/// are skipped with an `eprintln!` warn (NOT silently dropped) so
/// per-frame transition loops in Phase 5-b-2+ keep emitting a
/// diagnostic when a slide has a bad layer. The whole-slide bg
/// resolution still hard-errors on bad hex (unrecoverable).
fn resolve_slide_layers<'a>(
    slide: &'a TextSlide,
    fonts: Option<&FontCatalog>,
) -> Result<(
    BgKind,
    &'static str,
    Vec<(&'a crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)>,
)> {
    let (bg_kind, pattern_label) = resolve_slide_bg(slide)?;
    let text_layers: Vec<(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)> =
        if let Some(catalog) = fonts {
            slide
                .text_layers
                .iter()
                .filter(|l| l.visible && !l.text.is_empty())
                .filter_map(|l| {
                    let family = l
                        .font_family
                        .as_deref()
                        .unwrap_or_else(|| catalog.fallback_family());
                    let font = match catalog.get(family) {
                        Some(f) => f,
                        None => {
                            eprintln!(
                                "warn: no font available for family {family:?} \
                                 (and fallback also missing) — skipping layer {:?} \
                                 in slide {}",
                                l.text, slide.id,
                            );
                            return None;
                        }
                    };
                    let tc = match hex_to_rgba(&l.text_color) {
                        Some(c) => c,
                        None => {
                            eprintln!(
                                "warn: invalid text_color {:?} for slide {} — \
                                 skipping layer {:?}",
                                l.text_color, slide.id, l.text,
                            );
                            return None;
                        }
                    };
                    Some((l, tc, font))
                })
                .collect()
        } else {
            Vec::new()
        };
    Ok((bg_kind, pattern_label, text_layers))
}

/// Phase 5-b-1 — single-frame composite of two slides via the
/// fade transition shader at a fixed `t` ∈ [0, 1]. Renders each
/// slide into its own FBO once, then runs FS_FADE against both
/// textures at the given t and pushes one frame to scanout.
/// Holds for `hold_ms` milliseconds. Same one-shot legacy
/// SetCrtc path as render_slide_via_fbo.
///
/// At t=0 the screen shows slide_a unchanged. At t=1 the screen
/// shows slide_b unchanged. At t=0.5 a 50/50 cross-fade. Phase
/// 5-b-2 wraps this in a per-frame loop driving t from 0..1 over
/// `transition_ms`.
pub fn render_fade_composite(
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    t: f32,
    hold_ms: u64,
) -> Result<()> {
    let t = t.clamp(0.0, 1.0);
    let (bg_a, _, layers_a) = resolve_slide_layers(slide_a, fonts)?;
    let (bg_b, _, layers_b) = resolve_slide_layers(slide_b, fonts)?;

    eprintln!(
        "rendering fade composite slide_a={} slide_b={} t={:.3} for {}ms",
        slide_a.id, slide_b.id, t, hold_ms,
    );

    render_one_frame_to_hdmi(card, hold_ms, |gl, mode_w, mode_h| {
        use glow::HasContext;
        unsafe {
            // -- Render each slide into its own FBO.
            let (fbo_a, tex_a) = make_slide_fbo(gl, mode_w, mode_h, &bg_a, &layers_a)?;
            let (fbo_b, tex_b) = match make_slide_fbo(gl, mode_w, mode_h, &bg_b, &layers_b) {
                Ok(pair) => pair,
                Err(e) => {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    return Err(e);
                }
            };

            // -- Composite via FS_FADE on the default FB.
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);

            let program = match link_program(gl, VS_TEXTURED_QUAD, FS_FADE) {
                Ok(p) => p,
                Err(e) => {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                    return Err(e);
                }
            };
            // Fullscreen NDC quad with UVs (0,0)..(1,1). Same NDC↔UV
            // pairing as render_slide_via_fbo so image-top maps to
            // screen-top (see that function's comment for the trace).
            let verts: [f32; 16] = [
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0,
            ];
            let vbo = match gl.create_buffer() {
                Ok(b) => b,
                Err(e) => {
                    gl.delete_program(program);
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                    return Err(anyhow!("glGenBuffers(fade): {e}"));
                }
            };
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                verts.as_ptr() as *const u8,
                std::mem::size_of_val(&verts),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

            let cleanup = |gl: &glow::Context| unsafe {
                gl.delete_buffer(vbo);
                gl.delete_program(program);
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
                // Restore active texture unit back to TEXTURE0 so a
                // future per-frame loop (5-b-2 / 4.3+) doesn't
                // inherit selector=TEXTURE1 — paint_slide's glyph
                // bind happens to use explicit active_texture(TEXTURE0)
                // calls, but defensive restore is cheap.
                gl.active_texture(glow::TEXTURE0);
            };

            let a_pos = match gl.get_attrib_location(program, "a_pos") {
                Some(loc) => loc,
                None => {
                    cleanup(gl);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos (fade)"));
                }
            };
            let a_uv = match gl.get_attrib_location(program, "a_uv") {
                Some(loc) => loc,
                None => {
                    cleanup(gl);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_uv (fade)"));
                }
            };

            gl.use_program(Some(program));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(tex_a));
            gl.active_texture(glow::TEXTURE1);
            gl.bind_texture(glow::TEXTURE_2D, Some(tex_b));
            let u_src_a = gl.get_uniform_location(program, "u_src_a");
            let u_src_b = gl.get_uniform_location(program, "u_src_b");
            let u_t = gl.get_uniform_location(program, "u_t");
            gl.uniform_1_i32(u_src_a.as_ref(), 0);
            gl.uniform_1_i32(u_src_b.as_ref(), 1);
            gl.uniform_1_f32(u_t.as_ref(), t);

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

            cleanup(gl);
            gl.flush();
        }
        Ok(())
    })?;
    eprintln!("fade composite render complete");
    Ok(())
}

/// Phase 5-b-2/5-c — animate a transition between two slides over
/// `transition_ms` at `fps`. Renders slide_a + slide_b into FBOs
/// ONCE before the loop; per-frame runs the kind-selected
/// transition shader at `t = elapsed / transition_ms` clamped to
/// [0, 1] and pushes via legacy SetCrtc.
///
/// `kind` selects the shader via `fs_for_transition_kind`. Unknown
/// kinds fall back to `cut` (hard switch at t=0.5) with a warn so
/// the transition still completes rather than a black frame.
///
/// Single-buffered scanout — there's tearing at the swap boundary
/// for the brief transition duration. May switch to atomic +
/// double-buffered (see render_animated_atomic) once the
/// transition deck is complete; for now the simpler path keeps the
/// slice scope reviewable.
///
/// Returns the rendered frame count for smoke-script floor checks.
pub fn render_transition_animated(
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    kind: &str,
    transition_ms: u32,
    fps: u32,
) -> Result<u32> {
    if transition_ms == 0 {
        bail!("transition_ms must be > 0");
    }
    if fps == 0 {
        bail!("fps must be > 0");
    }

    let fs = match fs_for_transition_kind(kind) {
        Some(s) => s,
        None => {
            eprintln!(
                "warn: transition kind {kind:?} not yet implemented; \
                 falling back to cut"
            );
            FS_CUT
        }
    };
    let (bg_a, _, layers_a) = resolve_slide_layers(slide_a, fonts)?;
    let (bg_b, _, layers_b) = resolve_slide_layers(slide_b, fonts)?;

    eprintln!(
        "rendering animated transition kind={kind:?} slide_a={} slide_b={} \
         transition_ms={transition_ms} fps={fps}",
        slide_a.id, slide_b.id,
    );

    // -- DRM + GBM + EGL bring-up (same as render_one_frame_to_hdmi).
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
    egl_lib
        .initialize(display)
        .map_err(|e| anyhow!("eglInitialize failed: {e:?}"))?;
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

    // -- Animated render work + per-frame BO/FB tracking.
    let mode_w_u32 = mode_w as u32;
    let mode_h_u32 = mode_h as u32;
    let frame_budget = std::time::Duration::from_secs_f64(1.0 / fps as f64);
    let total_frames = ((transition_ms as f64) / 1000.0 * fps as f64).round().max(1.0) as u32;

    // Track previous-frame's BO/FB so we can drop them after the
    // next setCrtc takes effect (single-buffered legacy: we can't
    // drop the currently-scanning FB until the new one is in
    // scanout). Simplest pattern: keep N and N-1, drop N-1 after
    // frame N's setCrtc.
    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    let mut current_bo: Option<BufferObject<()>> = None;
    let mut current_fb: Option<framebuffer::Handle> = None;

    let work: Result<u32> = (|| {
        use glow::HasContext;

        // -- Build slide_a and slide_b FBOs once.
        let (fbo_a, tex_a) = unsafe { make_slide_fbo(&gl, mode_w_u32, mode_h_u32, &bg_a, &layers_a)? };
        let (fbo_b, tex_b) = unsafe {
            match make_slide_fbo(&gl, mode_w_u32, mode_h_u32, &bg_b, &layers_b) {
                Ok(pair) => pair,
                Err(e) => {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    return Err(e);
                }
            }
        };

        // -- Compile transition program + build VBO once.
        let program = unsafe {
            match link_program(&gl, VS_TEXTURED_QUAD, fs) {
                Ok(p) => p,
                Err(e) => {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                    return Err(e);
                }
            }
        };
        let cleanup_static = |gl: &glow::Context, vbo: Option<glow::NativeBuffer>| unsafe {
            if let Some(b) = vbo { gl.delete_buffer(b); }
            gl.delete_program(program);
            gl.delete_framebuffer(fbo_a);
            gl.delete_texture(tex_a);
            gl.delete_framebuffer(fbo_b);
            gl.delete_texture(tex_b);
        };
        let vbo = unsafe {
            match gl.create_buffer() {
                Ok(b) => b,
                Err(e) => {
                    cleanup_static(&gl, None);
                    return Err(anyhow!("glGenBuffers(animated fade): {e}"));
                }
            }
        };
        let verts: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        unsafe {
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                verts.as_ptr() as *const u8,
                std::mem::size_of_val(&verts),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
        }
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") };
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") };
        let (a_pos, a_uv) = match (a_pos, a_uv) {
            (Some(p), Some(u)) => (p, u),
            _ => {
                cleanup_static(&gl, Some(vbo));
                return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos / a_uv (animated fade)"));
            }
        };
        let u_src_a = unsafe { gl.get_uniform_location(program, "u_src_a") };
        let u_src_b = unsafe { gl.get_uniform_location(program, "u_src_b") };
        let u_t = unsafe { gl.get_uniform_location(program, "u_t") };

        // -- Per-frame loop. The loop body is wrapped in an IIFE so
        // the cleanup_static call below runs UNCONDITIONALLY even
        // if a frame errors mid-iteration. Without this, an
        // eglSwapBuffers / lock_front_buffer / setCrtc failure on
        // (say) frame 7 would leak program/vbo/fbo_a/tex_a/fbo_b/
        // tex_b until EGL teardown invalidated the context. Today
        // that's invisible (teardown happens immediately on Err);
        // 5-c may persistize the context across calls, where the
        // leak would compound.
        // v1-spec-delta #2 (slice d): motion through transitions.
        // If either slide has any animated layer, its FBO is
        // re-painted each frame so the motion math advances during
        // the transition. Static-only slides keep the one-shot bake
        // — no per-frame paint cost. Spec §11: motion advances
        // through transitions is a first-class render requirement.
        let any_animated_a = layers_a
            .iter()
            .any(|(l, _, _)| parse_motion_kind(&l.motion) != MotionKind::Static);
        let any_animated_b = layers_b
            .iter()
            .any(|(l, _, _)| parse_motion_kind(&l.motion) != MotionKind::Static);
        // v1-spec-delta #3: auto_mode-set layers also need
        // re-rasterization through transitions so the clock
        // doesn't freeze. Hoisted out of the per-frame loop --
        // immutable across frames.
        let any_auto_a = layers_a
            .iter()
            .any(|(l, _, _)| l.auto_mode.is_some());
        let any_auto_b = layers_b
            .iter()
            .any(|(l, _, _)| l.auto_mode.is_some());
        // v1-spec-delta #3 (slice b QA followup): per-slide glyph
        // caches across the transition loop. fontdue rasterization
        // skips when (resolved_text, font_size) is unchanged.
        let mut glyph_cache_a: GlyphCache = Vec::with_capacity(layers_a.len());
        glyph_cache_a.resize_with(layers_a.len(), || None);
        let mut glyph_cache_b: GlyphCache = Vec::with_capacity(layers_b.len());
        glyph_cache_b.resize_with(layers_b.len(), || None);
        let start = Instant::now();
        let mut rendered = 0_u32;
        let loop_result: Result<()> = (|| {
        for frame in 0..total_frames {
            let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
            // Each transition frame's tick_seconds is the elapsed
            // time inside the transition loop. Slide A "continues"
            // its motion across the transition; slide B starts
            // ticking from 0 (snaps to its render_slide tick=0 at
            // transition end, where render_slide(B) takes over).
            // The B-snap is sub-frame and below operator perception
            // — acceptable per spec line 277.
            let tick_seconds = start.elapsed().as_secs_f64();
            let wall_clock_unix = current_unix_seconds();
            unsafe {
                if any_animated_a || any_auto_a {
                    let states_a = motion_states_for_layers(
                        slide_a.id,
                        &layers_a,
                        tick_seconds,
                    );
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo_a));
                    paint_slide(
                        &gl,
                        mode_w_u32,
                        mode_h_u32,
                        &bg_a,
                        &layers_a,
                        Some(&states_a),
                        wall_clock_unix,
                        Some(&mut glyph_cache_a),
                    )?;
                }
                if any_animated_b || any_auto_b {
                    let states_b = motion_states_for_layers(
                        slide_b.id,
                        &layers_b,
                        tick_seconds,
                    );
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo_b));
                    paint_slide(
                        &gl,
                        mode_w_u32,
                        mode_h_u32,
                        &bg_b,
                        &layers_b,
                        Some(&states_b),
                        wall_clock_unix,
                        Some(&mut glyph_cache_b),
                    )?;
                }
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.clear_color(0.0, 0.0, 0.0, 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.use_program(Some(program));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_a));
                gl.active_texture(glow::TEXTURE1);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_b));
                gl.uniform_1_i32(u_src_a.as_ref(), 0);
                gl.uniform_1_i32(u_src_b.as_ref(), 1);
                gl.uniform_1_f32(u_t.as_ref(), t);

                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
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
                gl.flush();
            }

            // -- Push to scanout.
            egl_lib
                .swap_buffers(display, egl_surface)
                .map_err(|e| anyhow!("eglSwapBuffers (frame {frame}) failed: {e:?}"))?;
            let bo = unsafe {
                gbm_surface
                    .lock_front_buffer()
                    .with_context(|| format!("lock_front_buffer (frame {frame})"))?
            };
            let fb_buf = GbmBufferAdapter::new(&bo)
                .with_context(|| format!("read GBM bo metadata (frame {frame})"))?;
            let fb = card
                .add_framebuffer(&fb_buf, 32, 32)
                .with_context(|| format!("drmModeAddFB (frame {frame})"))?;
            // QA F2 (slice c carry-over): rmFB the just-added fb
            // on SetCrtc-fail unhappy path. Pre-existing leak in
            // this transition harness mirrored across the slice
            // (c) render_animated_slide. Both fixed in this commit.
            if let Err(e) = card.set_crtc(
                crtc_handle,
                Some(fb),
                (0, 0),
                &[connector_info.handle()],
                Some(mode),
            ) {
                if let Err(de) = card.destroy_framebuffer(fb) {
                    eprintln!(
                        "warn: cleanup destroy_framebuffer({fb:?}) on SetCrtc-fail (frame {frame}): {de}"
                    );
                }
                drop(bo);
                return Err(anyhow!("drmModeSetCrtc (frame {frame}) failed: {e}"));
            }

            // -- Rotate frames: free the frame from TWO iterations
            // ago — `prev` is no longer in scanout because
            // `current` (set last iter) is now the source. Up to
            // 3 BO/FB pairs alive transiently at the rotation
            // moment; 2 between iterations.
            if let Some(old_fb) = prev_fb.take() {
                if let Err(e) = card.destroy_framebuffer(old_fb) {
                    eprintln!("warn: destroy_framebuffer(prev): {e}");
                }
            }
            if let Some(old_bo) = prev_bo.take() {
                drop(old_bo);
            }
            prev_fb = current_fb.take();
            prev_bo = current_bo.take();
            current_fb = Some(fb);
            current_bo = Some(bo);

            rendered += 1;
            let target = start + frame_budget * (frame + 1);
            let now = Instant::now();
            if target > now {
                std::thread::sleep(target - now);
            }
        }
        Ok(())
        })();
        cleanup_static(&gl, Some(vbo));
        loop_result?;
        Ok(rendered)
    })();

    // Cleanup — unconditional. Free any remaining BO/FB pairs from
    // the loop (current + prev), then EGL state.
    for (fb_opt, bo_opt) in [
        (current_fb.take(), current_bo.take()),
        (prev_fb.take(), prev_bo.take()),
    ] {
        // Match the in-loop rotation order: destroy_framebuffer
        // first, then drop the BO. The kernel refcounts the
        // underlying buffer either way, but consistency aids
        // future readers.
        if let Some(fb) = fb_opt {
            if let Err(e) = card.destroy_framebuffer(fb) {
                eprintln!("warn: destroy_framebuffer(cleanup): {e}");
            }
        }
        if let Some(bo) = bo_opt {
            drop(bo);
        }
    }
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

    let frame_count = work?;
    eprintln!(
        "animated transition complete: kind={kind:?} rendered {frame_count} frames in {transition_ms}ms"
    );
    Ok(frame_count)
}

/// Paint a slide (bg pass + text-layer passes) into the currently-
/// bound framebuffer. Phase 5-a — extracted from `render_slide`'s
/// closure so the same painting logic can target either the default
/// framebuffer (direct path) OR an offscreen FBO color texture
/// (transition path: render slide A and slide B into separate
/// textures, then blend them via a transition shader).
///
/// Caller is responsible for binding the target framebuffer BEFORE
/// the call. Caller flushes/swaps AFTER. We do set the viewport so
/// the caller doesn't have to re-derive size against the binding.
/// v1-spec-delta #3 (slice b cache, QA followup): per-layer
/// rasterized-bitmap cache. Each entry holds the
/// (resolved_text, AlphaBitmap) for one layer. When the resolved
/// text is unchanged across frames (motion-only animations or the
/// 29 frames between auto_mode second-bucket boundaries), the
/// expensive fontdue rasterization is skipped and the cached
/// bitmap is reused. Cache miss = text changed = re-rasterize.
///
/// Vec parallel to text_layers; len matches. Initialized to None
/// at slide-render entry; populated lazily on first paint.
pub type GlyphCache = Vec<Option<CachedGlyph>>;

#[derive(Debug)]
pub struct CachedGlyph {
    text: String,
    bitmap: AlphaBitmap,
}

fn paint_slide(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: Option<&[MotionState]>,
    wall_clock_unix: i64,
    glyph_cache: Option<&mut GlyphCache>,
) -> Result<()> {
    use glow::HasContext;
    unsafe { gl.viewport(0, 0, mode_w as i32, mode_h as i32); }
    match *bg_kind {
        BgKind::Gradient { color_a, color_b, density } => {
            draw_gradient_pattern(gl, mode_w, mode_h, color_a, color_b, density)?;
        }
        BgKind::Solid(color) => {
            draw_solid_clear(gl, color);
        }
    }
    // BLEND toggle once around the layer loop (Phase 4.2c
    // optimization vs. per-layer enable/disable) — every text layer
    // uses the same premultiplied-alpha blend func and
    // disabling/re-enabling between layers is wasted state. The
    // IIFE guard ensures `gl.disable(BLEND)` always runs even when
    // a layer's draw errors mid-loop (4.3+ persistent-context
    // future-correctness).
    if !text_layers.is_empty() {
        // v1-spec-delta #2 (slice c-1): None = all-identity (no
        // animation). FBO bake / transition snapshots / static
        // slides take this path. Animated slides pass per-frame
        // motion states.
        if let Some(ms) = motion_states {
            if ms.len() != text_layers.len() {
                bail!(
                    "paint_slide: motion_states len {} != layers len {}",
                    ms.len(),
                    text_layers.len(),
                );
            }
        }
        unsafe {
            gl.enable(glow::BLEND);
            gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
        }
        // v1-spec-delta #3: resolve layer text up front. auto_mode
        // != None substitutes a formatted clock/date/day string; if
        // the substitution returns None (mode unset/unknown) the
        // layer's authored text falls through. Computed per frame
        // because per-frame is when the wall-clock advances.
        let cal = unix_to_calendar_utc(wall_clock_unix);
        let resolved_texts: Vec<String> = text_layers
            .iter()
            .map(|(layer, _, _)| {
                format_auto_text(
                    layer.auto_mode.as_deref(),
                    layer.auto_format.as_deref(),
                    cal,
                )
                .unwrap_or_else(|| layer.text.clone())
            })
            .collect();
        // v1-spec-delta #3 (slice b QA followup): rasterize through
        // the per-layer cache. On cache hit (text unchanged), skip
        // the fontdue call entirely -- this is what limits the
        // motion=ticker / auto_mode=time / etc. paths to one rast
        // per second-bucket instead of 30 per second. Without
        // glyph_cache (one-shot static path), allocate a local
        // throwaway cache so the layer loop has a uniform shape.
        let mut local_cache_storage: GlyphCache;
        let cache_ref: &mut GlyphCache = match glyph_cache {
            Some(c) => {
                if c.len() != text_layers.len() {
                    c.clear();
                    c.resize_with(text_layers.len(), || None);
                }
                c
            }
            None => {
                local_cache_storage = Vec::with_capacity(text_layers.len());
                local_cache_storage.resize_with(text_layers.len(), || None);
                &mut local_cache_storage
            }
        };
        // Stage 1: rasterize-or-reuse per layer. Bitmaps owned by
        // cache_ref entries; we'll borrow them in stage 2's GL draw.
        for (i, (layer, _, font)) in text_layers.iter().enumerate() {
            let resolved_text = &resolved_texts[i];
            let needs_raster = match &cache_ref[i] {
                Some(cached) => cached.text != *resolved_text,
                None => true,
            };
            if needs_raster {
                let size_px = effective_font_size_px(
                    layer.font_size_px,
                    layer.font_size_pct,
                    layer.r#box.w,
                    mode_w,
                );
                let bm = layout_text_to_alpha(font.as_ref(), resolved_text, size_px)
                    .ok_or_else(|| {
                        anyhow!(
                            "layout_text_to_alpha returned None for text={resolved_text:?} size={size_px}"
                        )
                    })?;
                eprintln!(
                    "rasterized text {resolved_text:?} @ {size_px:.1}px → {}x{} alpha bitmap",
                    bm.width, bm.height,
                );
                cache_ref[i] = Some(CachedGlyph {
                    text: resolved_text.clone(),
                    bitmap: bm,
                });
            }
        }
        let layer_loop_result: Result<()> = (|| {
            for (i, (layer, tc, _)) in text_layers.iter().enumerate() {
                let motion_state = motion_states
                    .map(|ms| ms[i])
                    .unwrap_or(MotionState::IDENTITY);
                let motion_kind = parse_motion_kind(&layer.motion);
                let cached = cache_ref[i]
                    .as_ref()
                    .expect("cache entry populated above");
                draw_text_layer(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    &cached.bitmap,
                )?;
            }
            Ok(())
        })();
        unsafe { gl.disable(glow::BLEND); }
        layer_loop_result?;
    }
    Ok(())
}

/// Phase 5-a — render a slide into an offscreen color texture
/// attached to a fresh FBO, then blit that texture to the default
/// framebuffer via a textured-quad pass. End-to-end visual output
/// is identical to `render_slide`, but the intermediate texture is
/// the foundation Phase 5 transitions need (render slide A and
/// slide B into separate textures, then blend via a transition
/// shader instead of the simple FS_BLIT).
///
/// At Phase 5-a this is one extra textured-quad blit per frame
/// vs. the direct path — fine for a one-shot render at hold-secs.
/// Phase 5-b's transition path will run per-frame at 30fps with
/// TWO source textures + a fragment shader composite, which is the
/// architectural shape this function bootstraps.
pub fn render_slide_via_fbo(
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    hold_ms: u64,
) -> Result<()> {
    let (bg_kind, pattern_label, text_layers) = resolve_slide_layers(slide, fonts)?;

    let bg_log = match &bg_kind {
        BgKind::Gradient { density, .. } => format!("pattern=gradient density={density:.3}"),
        BgKind::Solid(c) => format!(
            "pattern={pattern_label} bg=[{:.3},{:.3},{:.3}]",
            c[0], c[1], c[2]
        ),
    };
    eprintln!(
        "rendering slide via FBO {} ({:?}) {bg_log} text_layers={} for {}ms",
        slide.id,
        slide.name,
        text_layers.len(),
        hold_ms,
    );

    render_one_frame_to_hdmi(card, hold_ms, |gl, mode_w, mode_h| {
        use glow::HasContext;
        unsafe {
            // -- Build offscreen color texture sized to the mode.
            let color_tex = gl
                .create_texture()
                .map_err(|e| anyhow!("glGenTextures(color_tex): {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(color_tex));
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGBA as i32,
                mode_w as i32,
                mode_h as i32,
                0,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                None,
            );
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
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

            // -- Build FBO and attach the color texture.
            let fbo = match gl.create_framebuffer() {
                Ok(f) => f,
                Err(e) => {
                    gl.delete_texture(color_tex);
                    return Err(anyhow!("glGenFramebuffers: {e}"));
                }
            };
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER,
                glow::COLOR_ATTACHMENT0,
                glow::TEXTURE_2D,
                Some(color_tex),
                0,
            );
            let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            if status != glow::FRAMEBUFFER_COMPLETE {
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                gl.delete_framebuffer(fbo);
                gl.delete_texture(color_tex);
                return Err(anyhow!(
                    "framebuffer incomplete: status=0x{status:x} (FRAMEBUFFER_COMPLETE=0x{:x})",
                    glow::FRAMEBUFFER_COMPLETE,
                ));
            }

            // -- Paint the slide into the FBO.
            // v1-spec-delta #2 (slice c-1): debug FBO-parity path
            // takes the static snapshot. Slice (d) wires per-frame
            // motion through here when the test path needs it; for
            // now this is a deliberate freeze for visual diff
            // against render_slide.
            let paint_result = paint_slide(
                gl,
                mode_w,
                mode_h,
                &bg_kind,
                &text_layers,
                None,
                current_unix_seconds(),
                None,
            );
            // Always rebind default FBO before propagating Err so
            // cleanup/teardown doesn't operate on the offscreen one.
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            // R5-a/F1: free fbo+color_tex on the paint_slide-Err
            // path. Today the harness tears down EGL on Err so this
            // is invisible, but Phase 5-b runs this code per-frame
            // and Phase 4.3+ persistent-context inherits state —
            // leaks compound under both.
            if let Err(e) = paint_result {
                gl.delete_framebuffer(fbo);
                gl.delete_texture(color_tex);
                return Err(e);
            }

            // -- Blit the color texture to the default framebuffer
            // via a fullscreen textured quad. FS_BLIT is the
            // identity sampler; Phase 5-b swaps in a transition
            // shader sampling TWO textures + a `t` uniform.
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);

            let program = match link_program(gl, VS_TEXTURED_QUAD, FS_BLIT) {
                Ok(p) => p,
                Err(e) => {
                    gl.delete_framebuffer(fbo);
                    gl.delete_texture(color_tex);
                    return Err(e);
                }
            };
            // Fullscreen quad in NDC, TRIANGLE_STRIP order BL, BR,
            // TL, TR with UV (0,0)..(1,1). End-to-end orientation
            // trace (image-top stays at screen-top, no mirror):
            //
            //   1. paint_slide's `box_to_ndc_quad` maps image-y=0
            //      (top-of-slide) to NDC y=+1.
            //   2. Render-to-texture writes NDC y=+1 to texture
            //      v=1 (the FBO's UV-up convention).
            //   3. Blit verts pair NDC (+1, +1) ↔ UV (1, 1) and
            //      NDC (-1, -1) ↔ UV (0, 0).
            //   4. So sampling the FBO with this UV layout puts
            //      image-top at screen-top — same NDC↔UV pairing
            //      on both write and read. No flip needed.
            //
            // If a future blend/transition shader changes either
            // the write UV convention or the verts, recheck steps
            // 2-3 against the new ones.
            let verts: [f32; 16] = [
                -1.0, -1.0, 0.0, 0.0,
                 1.0, -1.0, 1.0, 0.0,
                -1.0,  1.0, 0.0, 1.0,
                 1.0,  1.0, 1.0, 1.0,
            ];
            let vbo = match gl.create_buffer() {
                Ok(b) => b,
                Err(e) => {
                    gl.delete_program(program);
                    gl.delete_framebuffer(fbo);
                    gl.delete_texture(color_tex);
                    return Err(anyhow!("glGenBuffers(blit): {e}"));
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
                    gl.delete_framebuffer(fbo);
                    gl.delete_texture(color_tex);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos (blit path)"));
                }
            };
            let a_uv = match gl.get_attrib_location(program, "a_uv") {
                Some(loc) => loc,
                None => {
                    gl.delete_buffer(vbo);
                    gl.delete_program(program);
                    gl.delete_framebuffer(fbo);
                    gl.delete_texture(color_tex);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_uv (blit path)"));
                }
            };
            gl.use_program(Some(program));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(color_tex));
            let u_src = gl.get_uniform_location(program, "u_src");
            gl.uniform_1_i32(u_src.as_ref(), 0);

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

            gl.delete_buffer(vbo);
            gl.delete_program(program);
            gl.delete_framebuffer(fbo);
            gl.delete_texture(color_tex);
            gl.flush();
        }
        Ok(())
    })?;
    eprintln!("slide render complete (via FBO)");
    Ok(())
}

/// Phase 6 — playlist-driven playback loop. Walks `playlist.json`
/// in order, and for each text-slide item:
///   1. Renders the previous slide → this slide via the entry
///      transition (kind + duration from the playlist item's
///      `transition` / `transition_ms` fields). The first item
///      has no predecessor so its entry transition is skipped.
///   2. Holds the slide for `slide.duration_ms` milliseconds
///      verbatim (v1-spec-delta #1, 2026-05-07 — was previously
///      `/1000` truncated to seconds, which collapsed the FYS
///      Panic flash slides at 130/350/500/800 ms onto a 1s
///      floor). Operator's `--hold-secs N` override stays
///      seconds-semantic at the CLI for ergonomics; the helper
///      internally ×1000's it.
///
/// Make-best-guess decisions logged inline:
///   * **Loop semantics** — single-pass for now. `loop_forever`
///     wraps back to the first item indefinitely; first slice
///     just exposes it as a flag for testing the wraparound
///     code path. Production playback chooses behavior.
///   * **Item filter** — non-text-slide items (image / video) get
///     skipped with a warn. Image/video playback is post-Phase-6.
///   * **Bad-hex / missing-slide policy** — skip with warn +
///     continue, mirroring the per-layer skip-with-warn policy
///     resolve_slide_layers established. The reel doesn't bail
///     on a malformed item.
///   * **Transition association** — `transition` field is the
///     ENTRY transition (i.e. how slide N appears). First slide
///     has no entry; cut implicitly.
///   * **EGL bring-up cost** — each call to render_slide /
///     render_transition_animated does its own GBM+EGL+GLES2
///     bring-up + teardown. For an N-slide reel that's ~2N
///     bring-ups per pass. ~500ms each on the dev Pi. Acceptable
///     overhead at this slice; FBO + harness recycling is post-
///     Phase-6 optimization.
pub fn render_playlist_reel(
    card: &Card,
    playlist_path: &Path,
    content_root: &Path,
    fonts: Option<&FontCatalog>,
    fps: u32,
    loop_forever: bool,
    hold_secs_override: Option<u64>,
) -> Result<()> {
    let envelope = load_playlist(playlist_path)?;
    if envelope.playlists.is_empty() {
        bail!("playlist {} has no playlists", playlist_path.display());
    }
    // Phase 6 first slice: take playlist[0]; multi-playlist
    // routing is a backend-side concern, not Phase 6's job.
    let playlist = &envelope.playlists[0];
    eprintln!(
        "reel: playlist {:?} ({}) {} items",
        playlist.name,
        playlist.id,
        playlist.items.len(),
    );

    // Pre-resolve via content::resolve_reel_items — host-tested
    // with the tempdir fixture matrix (text-only / image-skip /
    // missing-skip / empty / order-preserved). Reel logs a count
    // here; any per-item warns came out of the helper.
    let resolved = resolve_reel_items(content_root, playlist);
    if resolved.is_empty() {
        bail!("reel: no playable text-slide items in playlist");
    }
    eprintln!("reel: resolved {} playable text-slide items", resolved.len());

    let mut pass = 0_u32;
    loop {
        eprintln!(
            "reel: starting pass #{pass} ({} items, hold_override={:?}, fps={fps})",
            resolved.len(),
            hold_secs_override,
        );
        for (i, (slide, _, _)) in resolved.iter().enumerate() {
            // Entry transition (skip when no predecessor). The
            // wraparound math + first-pass semantics is in the
            // host-tested `prev_idx_for_reel`.
            //
            // Defensive guard: if the predecessor IS the current
            // item (1-item reel + --reel-loop), there's nothing
            // visually meaningful to transition — slide_b ≡
            // slide_a. Skip the per-frame loop entirely so we
            // don't burn compute on a no-op.
            if let Some(p) = prev_idx_for_reel(i, pass, resolved.len()) {
                if p != i {
                    let (prev_slide, _, _) = &resolved[p];
                    let (_, kind, transition_ms) = &resolved[i];
                    let transition_ms = clamp_transition_ms(*transition_ms);
                    eprintln!(
                        "reel: transition into item {i}/{} kind={kind:?} ms={transition_ms}",
                        resolved.len() - 1,
                    );
                    if let Err(e) = render_transition_animated(
                        card,
                        prev_slide,
                        slide,
                        fonts,
                        kind,
                        transition_ms,
                        fps,
                    ) {
                        // Skip-with-warn (no replay frame): the
                        // next render_slide call below will paint
                        // the new slide, which functions as a
                        // hard cut.
                        eprintln!(
                            "reel: warn — transition into item {i} failed: {e:#}; \
                             skipping to slide hold (acts as hard cut)"
                        );
                    }
                }
            }

            // v1-spec-delta #1: ms precision. slide.duration_ms is
            // in ms verbatim; the override (operator's --hold-secs)
            // is in seconds and gets ×1000'd inside effective_hold_ms.
            // FYS Panic flash slides at 130/350/500/800 ms now hold
            // for the actual specified duration instead of snapping
            // to a 1-second floor.
            let hold_ms = effective_hold_ms(slide.duration_ms, hold_secs_override);
            eprintln!(
                "reel: holding item {i}/{} ({:?}) for {hold_ms}ms",
                resolved.len() - 1,
                slide.name,
            );
            if let Err(e) = render_slide(card, slide, fonts, hold_ms) {
                eprintln!(
                    "reel: warn — render_slide failed for item {i}: {e:#}; \
                     skipping"
                );
            }
        }

        pass += 1;
        if !loop_forever {
            break;
        }
    }

    eprintln!("reel: complete after {pass} pass(es)");
    Ok(())
}

/// Render a single solid-color frame, push it to the HDMI display via
/// legacy `drmModeSetCrtc`, and hold for `duration_ms` milliseconds.
///
/// `color` is RGBA in [0.0, 1.0] linear space. The vc4 HVS handles
/// gamma at scanout per the connector's Colorspace property — we just
/// hand it premultiplied float color and let the hardware do the rest.
pub fn render_solid_color(card: &Card, color: [f32; 4], duration_ms: u64) -> Result<()> {
    // Phase 4.1c: thin wrapper over `render_one_frame_to_hdmi`. The
    // GLES draw work is just `glClearColor` + `glClear`; everything
    // else (GBM bring-up, EGL context, swap, addFB, SetCrtc, hold,
    // teardown) is shared with the slide-render path through the
    // same harness.
    render_one_frame_to_hdmi(card, duration_ms, |gl, mode_w, mode_h| {
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

