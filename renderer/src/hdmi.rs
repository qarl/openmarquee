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

use std::collections::{HashMap, VecDeque};
use std::ffi::c_void;
use std::ptr;
use std::time::{Duration, Instant};

use anyhow::{anyhow, bail, Context, Result};
use drm::buffer::{Buffer as DrmBuffer, DrmFourcc, Handle as DrmHandle};
use drm::Device as DrmBaseDevice;
use drm::control::{
    atomic::AtomicModeReq,
    connector::{self, State as ConnectorState},
    crtc, framebuffer, plane,
    property::{self, Value as PropValue},
    AtomicCommitFlags, Device as ControlDevice, Event, Mode, PageFlipFlags,
};
use gbm::{AsRaw, BufferObject, BufferObjectFlags, Format as GbmFormat};
use khronos_egl as egl;

use std::path::{Path, PathBuf};
use std::rc::Rc;
use uuid::Uuid;

use crate::content::{
    image_slide_asset_path, load_playlist, resolve_reel_items, solid_bg_hex, ContentItem,
    ImageSlide, TextSlide,
};
use crate::hdmi_logic::{
    blend_mode_label, box_to_ndc_quad, bricks_uniforms, checker_uniforms,
    clamp_transition_ms, compute_motion_state, confetti_uniforms, dots_uniforms,
    effective_font_size_px, effective_hold_ms, format_auto_text, fourcc_for_argb_family,
    fs_for_transition_kind, gradient_uniforms, grid_uniforms, halftone_uniforms,
    hex_to_rgba, hsv_to_rgb, layout_text_to_alpha, motion_offset_to_px,
    parse_blend_mode, parse_crtc_list_filter_bits, parse_h_align, parse_motion_kind,
    parse_pattern_kind, pattern_kind_label, pick_largest_mode_index, prev_idx_for_reel,
    rays_uniforms, rings_uniforms, scanlines_uniforms, should_rerasterize,
    fs_transition_sp_source, is_transition_kind_single_pass, stripes_uniforms,
    unix_to_calendar_utc, AlphaBitmap, BlendMode, FontCatalog, ModeSpec, MotionKind,
    MotionState, PatternKind, VAlign, FS_BLIT,
    FS_CUT, FS_FADE, FS_GLYPH, FS_GLYPH_OUTLINE, FS_GRADIENT, FS_OVERLAY_BLEND,
    FS_PATTERN_BRICKS, FS_PATTERN_CHECKER, FS_PATTERN_CONFETTI, FS_PATTERN_DOTS,
    FS_PATTERN_GRID, FS_PATTERN_HALFTONE, FS_PATTERN_RAYS, FS_PATTERN_RINGS,
    FS_PATTERN_SCANLINES, FS_PATTERN_STRIPES, SINGLE_PASS_MAX_LAYERS_PER_SLIDE,
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

/// v1-spec-delta #5 (slice a) -- handles a render session can
/// reuse across multiple draws. Created by `with_egl_session`,
/// borrows refs to the bring-up scope (GBM + EGL handles owned
/// there). Slice (a) only migrates `render_one_frame_to_hdmi`;
/// slice (b)+ will let the reel driver acquire one session and
/// loop slides through it without re-paying the ~500 ms bring-up
/// cost per slide (closes spec-delta MAJOR #19's BLACK gaps).
/// v1-spec-delta #8 (F-image-bg-cache, 2026-05-08) -- per-session
/// cache of decoded + uploaded image-bg textures, keyed on the
/// asset PathBuf. Lives across the entire reel pass so a slide
/// referenced multiple times (or a single animated slide with
/// image bg painted at 30 fps) re-uploads exactly once. Freed
/// in with_egl_session's teardown via gl.delete_texture for
/// every entry.
///
/// QA flagged this as HIGH priority post-slice 8(b): per-frame
/// PNG decode at 1920×1080 is ~50 ms (over the 33 ms frame
/// budget), so animated text slides with image bg would tank
/// to ~13 fps without the cache. Today's exposure is zero
/// because FYS has no image-bg slides, but production demos
/// will trigger the regression the moment the editor wires
/// background_image_slide_id under a motion-bearing layer.
///
/// v1-spec-delta #12 (image-bg eviction, 2026-05-08): bounded LRU
/// per memory budget §4 (image-bg cache hard ceiling = 6 entries
/// = 48 MB CMA cap). Without eviction, a long-running renderer
/// with many distinct images grows CMA without bound until OOM.
/// Implementation lives in crate::lru as a generic LruMap so the
/// eviction policy is host-testable on Mac (hdmi.rs is Linux-only).
pub const IMAGE_BG_CACHE_CAPACITY: usize = 6;

pub type ImageBgCache = crate::lru::LruMap<PathBuf, (glow::NativeTexture, u32, u32)>;

pub struct EglSession<'a> {
    egl_lib: &'a egl::DynamicInstance<egl::EGL1_5>,
    display: egl::Display,
    egl_surface: egl::Surface,
    gbm_surface: &'a mut gbm::Surface<()>,
    gl: &'a glow::Context,
    crtc_handle: crtc::Handle,
    connector_handle: connector::Handle,
    mode: drm::control::Mode,
    mode_w: u16,
    mode_h: u16,
    /// v1-spec-delta #8 (F-image-bg-cache): per-session cache of
    /// decoded + uploaded image-bg textures. See ImageBgCache
    /// docs. The reel driver passes &mut self.image_bg_cache
    /// to paint_slide via render_*_in_session.
    image_bg_cache: ImageBgCache,
    /// v1-spec-delta #9 (slice d): per-session N-2 BO/FB
    /// rotation for IPC sidecar mode. The standalone render_*_
    /// in_session loops keep their own loop-local rotation;
    /// the IPC dispatcher's Advance op uses these so the
    /// rotation persists across stdin-driven Advance calls
    /// (which are independent function invocations from the
    /// renderer's perspective). Both paths must NOT use the
    /// other's rotation -- standalone callers reset modeset_
    /// done = false on exit, which would mid-stream the IPC
    /// flow.
    scanout_prev_bo: Option<BufferObject<()>>,
    scanout_prev_fb: Option<framebuffer::Handle>,
    scanout_current_bo: Option<BufferObject<()>>,
    scanout_current_fb: Option<framebuffer::Handle>,
    /// v1-spec-delta #10 (slice c): persistent scene FBO for
    /// the brightness/gamma post-pass. Lazy-allocated on first
    /// non-identity settings frame, freed on session teardown.
    /// When settings are identity, scene_fbo stays None and
    /// paint targets default fb directly (zero overhead).
    scene_fbo: Option<glow::NativeFramebuffer>,
    scene_tex: Option<glow::NativeTexture>,
    /// v1-spec-delta #10 (slice c): caller-applied settings.
    /// Default = identity (Settings::default); apply_settings
    /// updates. paint_and_present_one_frame uses
    /// is_identity() to decide route.
    current_settings: crate::content::Settings,
    /// qarl-direct perf-profile (2026-05-08, post-cache): per-
    /// slide CachedGlyph + TextureCache hoisted from per-call
    /// scope to session level. Closes the per-transition first-
    /// frame text-rasterization tax (~180 ms × 2 sides per
    /// transition setup) by sharing rasterized bitmaps and GL
    /// textures across all renders of the same slide_id within
    /// a session. With the FYS reel cycling 19 slides and each
    /// reel pass touching every slide, the second pass + onward
    /// hit cache for ALL bake operations.
    ///
    /// Keyed by slide_id (Uuid). HashMap (no LRU) — FYS reel is
    /// 19 slides × ~1 MB cached state per slide (small text
    /// bitmaps); 19 MB total fits trivially in CMA budget. If
    /// future workloads need eviction, swap to LruMap.
    ///
    /// Cleanup at with_egl_session teardown drains all entries
    /// + delete_textures while gl context is still bound.
    slide_caches: std::collections::HashMap<uuid::Uuid, SlideRenderCache>,
    /// v1-spec-delta #5 (slice d, refined slice e): tracks whether
    /// the kernel CRTC currently has an alive (set_crtc'd) FB
    /// attached. The first commit per session OR the first commit
    /// of a render call after a prior call destroyed its scanout
    /// FB takes the SetCrtc branch (re-establishes the FB on the
    /// CRTC); subsequent within-call commits use the cheaper
    /// page_flip path. Set true on successful commit; reset to
    /// false at end of each render call's per-call cleanup
    /// (because we destroyed the FB the kernel was scanning out,
    /// so the next call's page_flip would EBUSY if we lied about
    /// readiness). Slice e's pass_ms gate caught this regression
    /// when slice d was originally written as "set_crtc once per
    /// session" -- the kernel rejected page_flip across render-
    /// call boundaries because the prior FB had been rmFB'd.
    modeset_done: bool,
    /// v1-spec-delta #5 (slice d): tracks whether a page-flip is
    /// currently in flight. The kernel allows at most one
    /// outstanding flip per CRTC; the next commit must drain the
    /// pending event before issuing another flip. Drain-before-
    /// commit is the design (as opposed to drain-after-commit) so
    /// the natural blocking point is when we WANT to advance, not
    /// when we just told the kernel "go."
    flip_pending: bool,
}

/// v1-spec-delta #5 (slice a) -- bring up GBM + EGL + GLES2,
/// invoke the closure with a borrowed `EglSession`, tear down
/// unconditionally. Behavior matches the inline bring-up pattern
/// every existing render path uses today; slice (a) is pure
/// extraction so slice (b)+ can compose multiple draws under one
/// session. The cleanup is warn-on-Err so the original error
/// propagates via the closure's return.
fn with_egl_session<F, R>(card: &Card, work: F) -> Result<R>
where
    F: FnOnce(&mut EglSession) -> Result<R>,
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
    let mut gbm_surface = gbm_dev
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

    let mut session = EglSession {
        egl_lib: &egl_lib,
        display,
        egl_surface,
        gbm_surface: &mut gbm_surface,
        gl: &gl,
        crtc_handle,
        connector_handle: connector_info.handle(),
        mode,
        mode_w,
        mode_h,
        modeset_done: false,
        flip_pending: false,
        image_bg_cache: ImageBgCache::with_capacity(IMAGE_BG_CACHE_CAPACITY),
        scanout_prev_bo: None,
        scanout_prev_fb: None,
        scanout_current_bo: None,
        scanout_current_fb: None,
        scene_fbo: None,
        scene_tex: None,
        current_settings: crate::content::Settings::default(),
        slide_caches: std::collections::HashMap::new(),
    };
    let work_result = work(&mut session);

    // v1-spec-delta #8 (F-image-bg-cache): free per-session
    // image-bg textures while the GL context is still current.
    // After this point EGL teardown invalidates all textures
    // anyway, but explicit deletion keeps driver bookkeeping
    // clean and surfaces leaks via warn-on-Err pattern.
    {
        use glow::HasContext;
        for (path, (tex, _, _)) in session.image_bg_cache.drain() {
            unsafe { gl.delete_texture(tex); }
            // Trace-level diagnostic: cached image freed.
            // Comment-only -- production logs stay quiet.
            let _ = path;
        }
        // qarl-direct perf-profile (2026-05-08, post-cache hoist):
        // free per-slide cached GL textures from the session-
        // level slide_caches. Glyph alpha bitmaps are CPU heap
        // (drop on drain). Texture handles are kernel-side; need
        // explicit gl.delete_texture while context is bound.
        for (_slide_id, mut entry) in session.slide_caches.drain() {
            for slot in entry.tex.iter_mut() {
                if let Some(t) = slot.take() {
                    unsafe { gl.delete_texture(t); }
                }
            }
        }
    }
    // qarl-direct perf-profile (2026-05-08): free thread-local
    // cached glyph programs while the GL context is still bound.
    // The thread_local Cells live across function invocations
    // within the process; clearing here keeps them in sync with
    // the GL context lifecycle.
    clear_glyph_program_cache(&gl);
    clear_transition_program_cache(&gl);
    clear_transition_sp_program_cache(&gl);
    // v1-spec-delta #9 (slice d): drain pending flip + free
    // session-level scanout BO/FB rotation. Mirrors the
    // animated_slide end-of-call cleanup but at session
    // teardown for the IPC path (where each Advance is one
    // frame of a long-lived loop). drain_pending_flip
    // confirms kernel switched to current; then both prev
    // and current are safe to free.
    drain_pending_flip(&mut session, card);
    if let Some(fb) = session.scanout_current_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_current): {e}");
        }
    }
    if let Some(bo) = session.scanout_current_bo.take() {
        drop(bo);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev_bo.take() {
        drop(bo);
    }
    // v1-spec-delta #10 (slice c): free scene FBO + texture
    // (lazy-allocated by paint_*_one_frame when settings are
    // non-identity). Safe to call delete_framebuffer/texture
    // while GL context is still current.
    unsafe {
        use glow::HasContext;
        if let Some(fbo) = session.scene_fbo.take() {
            gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.scene_tex.take() {
            gl.delete_texture(tex);
        }
    }
    drop(session);

    // Cleanup — unconditional, warn-on-Err so the original cause
    // propagates via `work_result?`. gbm_surface and gbm_dev drop
    // via their RAII Drop impls when this scope exits.
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

    work_result
}

/// v1-spec-delta F1d (V1-GA-blocker, 2026-05-08): poll(2) the DRM
/// fd until POLLIN is set or `timeout_ms` elapses. drm-rs's
/// `receive_events` does a blocking read; without this gate, a
/// HW vblank miss / kernel hang / unplugged HDMI cable would
/// hang the renderer forever inside the drain. 500 ms is the
/// canonical timeout: well above the 16.7 ms vsync interval but
/// short enough that a stuck renderer surfaces in roughly one
/// human-noticeable interval.
///
/// EINTR is retried (signal-interrupt is transient). POLLERR /
/// POLLHUP / POLLNVAL surface as Err so the caller can decide
/// whether to escalate or recover. Spurious wake (no POLLIN, no
/// error, no timeout) loops back to poll.
#[cfg(target_os = "linux")]
fn poll_drm_fd_for_events(card: &Card, timeout_ms: i32) -> Result<()> {
    use std::os::fd::{AsFd, AsRawFd};
    let raw_fd = card.as_fd().as_raw_fd();
    let mut fds = [libc::pollfd {
        fd: raw_fd,
        events: libc::POLLIN,
        revents: 0,
    }];
    loop {
        let n = unsafe { libc::poll(fds.as_mut_ptr(), 1, timeout_ms) };
        if n < 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() == std::io::ErrorKind::Interrupted {
                continue;
            }
            return Err(anyhow!("poll on DRM fd failed: {err}"));
        }
        if n == 0 {
            return Err(anyhow!(
                "page-flip event timeout after {timeout_ms} ms (HW hang or vblank miss)"
            ));
        }
        let revents = fds[0].revents;
        if revents & libc::POLLIN != 0 {
            return Ok(());
        }
        if revents & (libc::POLLERR | libc::POLLHUP | libc::POLLNVAL) != 0 {
            return Err(anyhow!("DRM fd error: revents=0x{revents:x}"));
        }
        // Spurious wake: no POLLIN, no error, but n>0. Loop.
    }
}

/// v1-spec-delta #5 (slice d, 2026-05-08): commit a freshly-added
/// FB to scanout. First call on a fresh EglSession does the
/// SetCrtc modeset; subsequent calls use page_flip with EVENT
/// completion. This closes spec-delta #8b's transition wall-clock
/// perf gap (12.6 -> 30 fps target) by replacing the per-frame
/// SetCrtc (~32 ms cost on vc4) with the cheaper page_flip path.
///
/// Drain-before-commit: at most one page-flip can be in flight
/// per CRTC at the kernel boundary. If a flip is pending from a
/// prior call, drain its completion event first. This naturally
/// vsync-paces the per-frame loop -- the drain blocks until the
/// kernel has scanned out the previous FB.
///
/// On the unhappy path the caller is responsible for fb/bo
/// cleanup; this fn does NOT call destroy_framebuffer/drop on
/// error so the existing per-call cleanup pattern stays
/// consistent across both SetCrtc and page_flip dispatch.
fn commit_fb(
    session: &mut EglSession,
    card: &Card,
    fb: framebuffer::Handle,
) -> Result<()> {
    // QA-direct (2026-05-08): sub-phase profiling to characterize
    // the 8.2ms p50 of commit_fb. Goal is to identify whether
    // drain-wait (vblank gating), receive_events deserialize,
    // or the page_flip ioctl is the dominant cost.
    if session.flip_pending {
        // Drain. Kernel sends a single PageFlipEvent per requested
        // flip on this fd; loop in case multiple events arrive
        // (defensive — we only ever request one at a time).
        // F1d: poll-gate each receive_events so a HW vblank miss
        // doesn't hang the renderer forever.
        let t_drain = std::time::Instant::now();
        loop {
            let t_poll = std::time::Instant::now();
            poll_drm_fd_for_events(card, 500)
                .context("page-flip drain (commit_fb)")?;
            crate::profile::record_phase(
                "commit_drain_poll",
                t_poll.elapsed().as_nanos() as u64,
            );
            let t_recv = std::time::Instant::now();
            let events = card
                .receive_events()
                .context("drmHandleEvent (page-flip drain)")?;
            crate::profile::record_phase(
                "commit_drain_recv",
                t_recv.elapsed().as_nanos() as u64,
            );
            let mut got_flip = false;
            for ev in events {
                if matches!(ev, Event::PageFlip(_)) {
                    got_flip = true;
                }
            }
            if got_flip {
                break;
            }
        }
        session.flip_pending = false;
        crate::profile::record_phase(
            "commit_drain_total",
            t_drain.elapsed().as_nanos() as u64,
        );
    }

    if !session.modeset_done {
        let t_setcrtc = std::time::Instant::now();
        card.set_crtc(
            session.crtc_handle,
            Some(fb),
            (0, 0),
            &[session.connector_handle],
            Some(session.mode),
        )
        .context("drmModeSetCrtc failed")?;
        crate::profile::record_phase(
            "commit_setcrtc",
            t_setcrtc.elapsed().as_nanos() as u64,
        );
        session.modeset_done = true;
        return Ok(());
    }

    // QA-direct (2026-05-08): use DRM_MODE_PAGE_FLIP_ASYNC so the
    // kernel performs the flip immediately rather than waiting
    // for vblank. EVENT is still set so the page-flip event fires
    // (right after the flip, not at vblank) -- our drain reads it
    // promptly on the next commit_fb. Drops the per-frame
    // commit_drain_poll wait (~8ms p50 at 60Hz) to ~0 ms.
    //
    // Tradeoff: tearing during the half-vblank window between the
    // flip and the next vblank. Acceptable for the FYS reel
    // because (a) transitions are short and visually busy, (b)
    // static slides only flip once at scene-change, (c) vc4 vblank
    // period at 60Hz = 16.7 ms means worst-case tear width is one
    // half-screen for one frame.
    let t_pageflip = std::time::Instant::now();
    card.page_flip(
        session.crtc_handle,
        fb,
        PageFlipFlags::EVENT | PageFlipFlags::ASYNC,
        None,
    )
    .context("drmModePageFlip failed")?;
    crate::profile::record_phase(
        "commit_pageflip",
        t_pageflip.elapsed().as_nanos() as u64,
    );
    session.flip_pending = true;
    Ok(())
}

/// v1-spec-delta #5 (slice d, 2026-05-08): drain any pending
/// page-flip event so the caller can safely release its last-
/// frame BO/FB without racing the kernel scanout. Called at the
/// end of per-frame loops in render_animated_slide_in_session
/// and render_transition_animated_in_session.
///
/// Why drain at end-of-call (not just before next commit): the
/// gbm_surface BO pool is shared across render calls in the same
/// session. If we exit a call with a flip in flight, the kernel
/// is still scanning the last BO. The next call's first
/// swap_buffers may reuse that BO from the gbm pool -- racing
/// the kernel mid-scanout. Draining here ensures the kernel has
/// switched away before we drop the BufferObject (which marks
/// it as free for gbm to reuse).
fn drain_pending_flip(session: &mut EglSession, card: &Card) {
    if !session.flip_pending {
        return;
    }
    loop {
        // F1d: poll-gate so a vc4 driver stall doesn't hang the
        // teardown path forever. drain_pending_flip is a best-
        // effort cleanup -- on poll timeout we log + give up + clear
        // flip_pending so the next render call can proceed (the
        // kernel may have recovered, or the next set_crtc will
        // resync state).
        if let Err(e) = poll_drm_fd_for_events(card, 500) {
            eprintln!("warn: page-flip drain timeout (end-of-call): {e}; clearing flip_pending");
            break;
        }
        let events = match card.receive_events() {
            Ok(events) => events,
            Err(e) => {
                eprintln!("warn: drmHandleEvent (end-of-call drain): {e}");
                break;
            }
        };
        let mut got_flip = false;
        for ev in events {
            if matches!(ev, Event::PageFlip(_)) {
                got_flip = true;
            }
        }
        if got_flip {
            break;
        }
    }
    session.flip_pending = false;
}

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
/// v1-spec-delta #5 (slice a, 2026-05-08): the EGL/GBM bring-up
/// + teardown is now extracted into `with_egl_session`. This
/// function still does its own session per call (no behavior
/// change vs slice 0); slice (b)+ will let the reel driver hold
/// one session across the slide loop and skip the ~500 ms
/// bring-up cost per slide.
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
    with_egl_session(card, |session| render_one_frame_in_session(session, card, hold_ms, draw))
}

/// v1-spec-delta #5 (slice b, 2026-05-08): per-frame work given an
/// already-acquired EGL session. Runs the caller's `draw` closure,
/// `eglSwapBuffers`, locks the front BO, addFB, drmModeSetCrtc,
/// holds for `hold_ms` ms, then drops BO + destroy_framebuffer.
/// Cleanup unconditional (errors warn but don't shadow the
/// original cause via `work`).
///
/// Extracted from `render_one_frame_to_hdmi` so slice (c) can let
/// the reel driver call this multiple times under one
/// `with_egl_session` -- amortizing the ~500 ms bring-up cost
/// across the whole reel pass instead of paying it per slide
/// (closes spec-delta MAJOR #19's BLACK gaps). render_one_frame_to
/// _hdmi remains as the wrapper for one-shot callers (CLI
/// `--solid-color`, `--play-slide` static, `--fade-from/to`).
fn render_one_frame_in_session<F>(
    session: &mut EglSession,
    card: &Card,
    hold_ms: u64,
    draw: F,
) -> Result<()>
where
    F: FnOnce(&glow::Context, u32, u32) -> Result<()>,
{
    // Resources the work block creates (BO + FB) need cleanup
    // regardless of whether the work succeeds. Track via Options
    // populated mid-closure; cleanup walks them after.
    let mut bo_holder: Option<BufferObject<()>> = None;
    let mut fb_holder: Option<framebuffer::Handle> = None;

    let work: Result<()> = (|| {
        draw(session.gl, session.mode_w as u32, session.mode_h as u32)?;
        gl_error_sweep(session.gl, "user draw closure");
        session
            .egl_lib
            .swap_buffers(session.display, session.egl_surface)
            .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
        let bo = unsafe {
            session
                .gbm_surface
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
        // v1-spec-delta #5 (slice d): SetCrtc on first commit per
        // session, page_flip thereafter. The static path benefits
        // because slide N+1 inside a reel sees modeset_done=true
        // from slide N -- so a held static slide between two
        // animated slides commits via page_flip (no expensive
        // modeset).
        commit_fb(session, card, fb)?;
        eprintln!(
            "scanout active on {:?}; holding for {}ms",
            session.crtc_handle, hold_ms
        );
        std::thread::sleep(std::time::Duration::from_millis(hold_ms));
        Ok(())
    })();

    // v1-spec-delta #5 (slice d): drain pending page-flip event
    // before BO/FB cleanup. For the FIRST call on a fresh session
    // this is a no-op (commit_fb took the SetCrtc-synchronous
    // branch). For subsequent calls under the same reel session
    // it ensures the kernel has finished scanning out our BO
    // before gbm reuses it.
    drain_pending_flip(session, card);

    // BO/FB cleanup -- happens before with_egl_session's EGL
    // teardown so the FB-handle rmFB lands while DRM master is
    // still held cleanly.
    if let Some(bo) = bo_holder {
        drop(bo);
    }
    if let Some(fb) = fb_holder {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
        }
    }
    // v1-spec-delta #5 (slice e fix): the FB we just rmFB'd was
    // the kernel's scanout source. Mark the CRTC as "needs a
    // re-establishing SetCrtc on the next commit"; otherwise the
    // next call's page_flip EBUSYs because the kernel sees a
    // destroyed scanout FB. Caught by slice e's pass_ms gate.
    session.modeset_done = false;

    work
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
    with_egl_session(card, |session| {
        render_animated_slide_in_session(
            session, card, bg_kind, text_layers, slide_id, hold_ms, fps,
        )
    })
}

/// v1-spec-delta #5 (slice c, 2026-05-08): per-frame animated
/// slide work given an already-acquired EGL session. Extracted
/// from render_animated_slide so the reel driver can call this
/// under one shared with_egl_session, amortizing the ~500 ms
/// bring-up across all reel slides (closes spec-delta MAJOR #19).
///
/// BO/FB rotation is per-call: each render holds prev_bo+prev_fb
/// across its own frames, releases all of it on exit. The
/// session's gbm_surface is reused across calls but no BOs leak
/// between calls.
/// QA-direct (2026-05-08, post-Step-3): pace to a per-frame
/// deadline with a hybrid sleep + spin-wait tail. std::thread::
/// sleep on Linux at the default kernel HZ has 1-5 ms overshoot;
/// at 30 fps target (33.3 ms cadence), that overshoot pushes
/// per-frame to ~37 ms = ~27-28 fps aggregate (matches the §8.3
/// gap measured post-Step-3). Sleeping most of the way and
/// busy-spinning the last few ms lets us hit the deadline within
/// a single spin iteration (~10 us). Cost: <2% CPU at 30 fps.
fn pace_to_frame_deadline(start: Instant, frame_idx: u64, frame_period_ns: u64) {
    // 10 ms spin budget. Linux thread::sleep on this Pi at the
    // default kernel HZ has typical overshoot of 1-3 ms but tail
    // hits 5-8 ms; a 10 ms spin window guarantees the spin (not
    // the sleep) is what hits the deadline. CPU cost: ~30% of
    // one core during the spin window per paced frame, which at
    // 30 fps with a 33 ms cadence works out to ~9% of a core per
    // second of render. That's the price for closing the §8.3
    // gap from 28→30 fps STRICT instead of 29.6.
    const SPIN_BUDGET_NS: u64 = 10_000_000;
    let deadline_ns = frame_idx.wrapping_mul(frame_period_ns);
    let now = start.elapsed().as_nanos() as u64;
    if deadline_ns <= now {
        return;
    }
    let remaining = deadline_ns - now;
    if remaining > SPIN_BUDGET_NS {
        std::thread::sleep(std::time::Duration::from_nanos(
            remaining - SPIN_BUDGET_NS,
        ));
    }
    while (start.elapsed().as_nanos() as u64) < deadline_ns {
        std::hint::spin_loop();
    }
}

fn render_animated_slide_in_session(
    session: &mut EglSession,
    card: &Card,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    slide_id: Uuid,
    hold_ms: u64,
    fps: u32,
) -> Result<()> {
    // v1-spec-delta #5 (slice e F1e fix): N-2 BO/FB rotation. Pre-
    // slice-d under sync SetCrtc, N-1 was correct because the FB
    // was guaranteed-released by the time SetCrtc returned. Post-
    // slice-d the kernel scans fb_{K-1} until next vblank (async
    // page_flip), so dropping bo_{K-1} immediately returns the
    // BO to the gbm pool while still on scanout — kernel-level
    // use-after-free under min-pool / back-pressure (typically
    // hidden by libgbm's 3-4 BO rotation but not safe to rely
    // on). Mirrors the N-2 rotation in
    // render_transition_animated_in_session.
    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    let mut current_bo: Option<BufferObject<()>> = None;
    let mut current_fb: Option<framebuffer::Handle> = None;
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
    // qarl-direct perf-profile (2026-05-08, post-cache hoist):
    // session-level slide cache replaces the per-call locals.
    // Re-renders of the same slide_id (e.g. across a reel pass)
    // hit the cache; no per-call setup tax.
    {
        let needs_new = match session.slide_caches.get(&slide_id) {
            Some(c) => c.glyph.len() != text_layers.len(),
            None => true,
        };
        if needs_new {
            if let Some(old) = session.slide_caches.remove(&slide_id) {
                unsafe {
                    use glow::HasContext;
                    for slot in old.tex {
                        if let Some(t) = slot {
                            session.gl.delete_texture(t);
                        }
                    }
                }
            }
            session.slide_caches.insert(slide_id, SlideRenderCache::new(text_layers.len()));
        }
    }

    let work: Result<()> = (|| {
        use glow::HasContext;
        let profile_active = crate::profile::is_enabled();
        loop {
            let elapsed = start.elapsed();
            let elapsed_ms = elapsed.as_millis() as u64;
            if elapsed_ms >= hold_ms {
                break;
            }
            // qarl-direct perf-profile: stop after N captured
            // frames when the profile budget is set.
            if profile_active && crate::profile::frames_remaining() == Some(0) {
                break;
            }
            let frame_start = std::time::Instant::now();
            let tick_seconds = elapsed.as_secs_f64();
            let motion_states =
                motion_states_for_layers(slide_id, text_layers, tick_seconds);
            let wall_clock_unix = current_unix_seconds();
            let t_paint = std::time::Instant::now();
            // Borrow each disjoint EglSession field for paint_slide.
            // Compiler verifies they don't overlap (gl=&immut,
            // image_bg_cache=&mut, slide_caches[slide_id].glyph=&mut,
            // slide_caches[slide_id].tex=&mut).
            let cache = session.slide_caches.get_mut(&slide_id)
                .expect("slide_caches entry initialized above");
            paint_slide(
                session.gl,
                session.mode_w as u32,
                session.mode_h as u32,
                bg_kind,
                text_layers,
                Some(&motion_states),
                wall_clock_unix,
                Some(&mut cache.glyph),
                // v1-spec-delta #8 F-image-bg-cache: reuse the
                // session-wide cache so animated slides with
                // image bg upload exactly once. Closes the per-
                // frame re-decode regression QA flagged.
                Some(&mut session.image_bg_cache),
                Some(&mut cache.tex),
            )?;
            unsafe { session.gl.flush(); }
            crate::profile::record_phase("paint", t_paint.elapsed().as_nanos() as u64);
            let t_swap = std::time::Instant::now();
            session
                .egl_lib
                .swap_buffers(session.display, session.egl_surface)
                .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
            crate::profile::record_phase("swap", t_swap.elapsed().as_nanos() as u64);
            let t_lockfb = std::time::Instant::now();
            let bo = unsafe {
                session
                    .gbm_surface
                    .lock_front_buffer()
                    .context("gbm_surface_lock_front_buffer failed")?
            };
            let fb_buf = GbmBufferAdapter::new(&bo).context("read GBM bo metadata")?;
            let fb = card
                .add_framebuffer(&fb_buf, 32, 32)
                .map_err(|e| anyhow!("drmModeAddFB failed: {e}"))?;
            crate::profile::record_phase("lockfb", t_lockfb.elapsed().as_nanos() as u64);
            // QA F2 (slice c carry-over): on commit fail, the
            // just-added fb is a u32 with no Drop and would leak.
            // Explicitly rmFB on the unhappy path. The BO Drops
            // cleanly via gbm RAII either way.
            //
            // v1-spec-delta #5 (slice d): commit_fb dispatches
            // SetCrtc-on-first-call vs page_flip-thereafter, and
            // drains any pending flip event before issuing the
            // next one (natural vsync pacing).
            let t_commit = std::time::Instant::now();
            if let Err(e) = commit_fb(session, card, fb) {
                if let Err(de) = card.destroy_framebuffer(fb) {
                    eprintln!(
                        "warn: cleanup destroy_framebuffer({fb:?}) on commit-fail: {de}"
                    );
                }
                drop(bo);
                return Err(e);
            }
            crate::profile::record_phase("commit", t_commit.elapsed().as_nanos() as u64);

            // v1-spec-delta #5 (slice e F1e fix): rotate N-2.
            // After commit_fb returns, kernel still scans current
            // (page_flip queued, fires next vblank). prev was
            // scanned 2+ frames ago — safe to free.
            let t_rotate = std::time::Instant::now();
            if let Some(old_fb) = prev_fb.take() {
                if let Err(e) = card.destroy_framebuffer(old_fb) {
                    eprintln!("warn: destroy_framebuffer({old_fb:?}): {e}");
                }
            }
            if let Some(old_bo) = prev_bo.take() {
                drop(old_bo);
            }
            prev_fb = current_fb.take();
            prev_bo = current_bo.take();
            current_fb = Some(fb);
            current_bo = Some(bo);
            frames += 1;
            crate::profile::record_phase("rotate", t_rotate.elapsed().as_nanos() as u64);
            crate::profile::record_phase(
                "frame_total",
                frame_start.elapsed().as_nanos() as u64,
            );
            crate::profile::frame_complete();

            // Pace to fps. next-deadline math, not sleep-by-period
            // — accumulated drift would walk us off cadence after a
            // few seconds. SKIP when profiling so the histogram
            // captures real shader-bound cadence, not vsync-padded.
            // QA-direct (2026-05-08): pace_to_frame_deadline does a
            // hybrid sleep+spin to absorb the 1-5 ms kernel sleep
            // overshoot that was dragging per-frame to 37 ms (~27
            // fps aggregate) at 30 fps target.
            if !profile_active {
                pace_to_frame_deadline(start, frames as u64, frame_period_ns);
            }
        }
        eprintln!(
            "animated slide complete: {frames} frames in {}ms",
            start.elapsed().as_millis()
        );
        Ok(())
    })();

    // qarl-direct perf-profile (2026-05-08, post-cache hoist):
    // tex_cache is now session-owned via session.slide_caches;
    // cleanup deferred to with_egl_session teardown. The
    // previous per-call free is gone -- intentional, that's the
    // whole point of the hoist.

    // v1-spec-delta #5 (slice d): drain the last frame's pending
    // page-flip event before per-call BO/FB cleanup. Otherwise
    // the kernel may still be reading from the last frame's BO
    // when we drop it, racing with the next render call's
    // gbm_surface BO pool reuse.
    drain_pending_flip(session, card);

    // Per-call BO/FB cleanup. Drops the last two frames' holders
    // (current = last frame just-committed; prev = frame before).
    // Both are post-drain so the kernel is no longer reading from
    // either. drain_pending_flip above guaranteed that the kernel
    // switched away from current's predecessor, so prev is freeable.
    // For current: kernel just switched to it; rmFB pulls our user-
    // ref but kernel keeps internal ref until something replaces
    // current as scanout (next call's set_crtc).
    for (fb_opt, bo_opt) in [
        (current_fb.take(), current_bo.take()),
        (prev_fb.take(), prev_bo.take()),
    ] {
        if let Some(fb) = fb_opt {
            if let Err(e) = card.destroy_framebuffer(fb) {
                eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
            }
        }
        if let Some(bo) = bo_opt {
            drop(bo);
        }
    }
    // v1-spec-delta #5 (slice e fix): see render_one_frame_in_session.
    // The last frame's FB was the kernel scanout source; rmFB
    // means the next call's page_flip would EBUSY without a fresh
    // SetCrtc to re-establish.
    session.modeset_done = false;

    work
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

/// v1-spec-delta #8 (slice b + F-image-bg-cache) -- draw an
/// ImageSlide-referenced PNG as the slide background. When a
/// cache is provided AND already holds an entry for this asset
/// path, reuse the cached texture (~free per frame). Otherwise
/// decode + upload, blit, and (if cache provided) insert. When
/// no cache is provided (one-shot paths, transition FBO bake),
/// the texture is freed at end of call.
///
/// Cache hit cost: 1 texture-bind + run_blit_pass (one full-
/// screen draw). Cache miss cost: PNG decode (~50 ms at 1920×
/// 1080) + tex upload (~5 ms) + blit. Hits are the common path
/// for animated text slides with image bg (paint_slide called
/// at 30 fps).
///
/// On any failure (missing file, corrupt PNG, GL error), falls
/// back to a solid clear with `solid_fallback`. The fallback
/// path emits a `warn:` line tagged with the asset path so the
/// failure is visible in logs. With cache, the warn fires once
/// per slide-entry (the failed entry isn't inserted, so each
/// re-attempt re-warns -- still bounded by attempts-per-slide).
fn draw_image_bg(
    gl: &glow::Context,
    asset_path: &Path,
    solid_fallback: [f32; 4],
    mut image_bg_cache: Option<&mut ImageBgCache>,
) {
    use glow::HasContext;
    // Cache hit -- skip decode + upload, just bind + blit. Touches
    // the entry to back-of-LRU-order via cache.get's &mut self.
    if let Some(cache) = image_bg_cache.as_deref_mut() {
        if let Some((tex, _, _)) = cache.get(asset_path) {
            let tex = *tex;
            let blit_result = unsafe { run_blit_pass(gl, tex) };
            if let Err(e) = blit_result {
                eprintln!(
                    "warn: image-bg blit failed (cache-hit) for {}: {e:#}; result may be partial",
                    asset_path.display()
                );
            }
            return;
        }
    }
    let (rgba, w, h) = match load_png_rgba(asset_path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!(
                "warn: image-bg load failed for {}: {e:#}; falling back to solid",
                asset_path.display()
            );
            draw_solid_clear(gl, solid_fallback);
            return;
        }
    };
    unsafe {
        let tex = match gl.create_texture() {
            Ok(t) => t,
            Err(e) => {
                eprintln!(
                    "warn: image-bg glGenTextures failed for {}: {e}; falling back to solid",
                    asset_path.display()
                );
                draw_solid_clear(gl, solid_fallback);
                return;
            }
        };
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.tex_parameter_i32(
            glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(
            glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(
            glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(
            glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.tex_image_2d(
            glow::TEXTURE_2D,
            0,
            glow::RGBA as i32,
            w as i32,
            h as i32,
            0,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            Some(&rgba),
        );
        let blit_result = run_blit_pass(gl, tex);
        // Cache insertion (or free) decision: when a cache is
        // provided, transfer ownership of the texture into the
        // cache so the next call with the same asset_path skips
        // decode+upload. Otherwise free now. Bounded LRU: insert
        // returns evicted_lru when at capacity, replaced when the
        // key already existed (rare; only on retry-after-failure).
        // Both must be deleted via gl since the cache only owns
        // the *key*, not the GPU resource.
        match image_bg_cache {
            Some(cache) => {
                let outcome = cache.insert(asset_path.to_path_buf(), (tex, w, h));
                if let Some((evicted, _, _)) = outcome.evicted_lru {
                    gl.delete_texture(evicted);
                }
                if let Some((replaced, _, _)) = outcome.replaced {
                    gl.delete_texture(replaced);
                }
            }
            None => {
                gl.delete_texture(tex);
            }
        }
        if let Err(e) = blit_result {
            eprintln!(
                "warn: image-bg blit failed for {}: {e:#}; result may be partial",
                asset_path.display()
            );
        }
    }
}

/// v1-spec-delta #6 (slice a, 2026-05-08): dispatch table for the
/// 10 procedural patterns. Slice a wired the dispatch shape;
/// slices (b)/(c)/(d) fill in fragment shaders. Until a pattern's
/// shader lands, the dispatch warns + falls back to a solid
/// color_a clear so the schema can accept all 10 names without
/// blocking playlist authoring.
fn draw_pattern(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    kind: PatternKind,
    color_a: [f32; 4],
    color_b: [f32; 4],
    density: f32,
) -> Result<()> {
    match kind {
        PatternKind::Stripes => {
            let u = stripes_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_STRIPES, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                },
            )
        }
        PatternKind::Checker => {
            let u = checker_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_CHECKER, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                },
            )
        }
        PatternKind::Dots => {
            let u = dots_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_DOTS, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_radius = gl.get_uniform_location(program, "u_radius");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_radius.as_ref(), u.radius);
                },
            )
        }
        PatternKind::Halftone => {
            let u = halftone_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_HALFTONE, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_radius = gl.get_uniform_location(program, "u_radius");
                    let u_half = gl.get_uniform_location(program, "u_half");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_radius.as_ref(), u.radius);
                    gl.uniform_1_f32(u_half.as_ref(), u.half);
                },
            )
        }
        PatternKind::Scanlines => {
            let u = scanlines_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_SCANLINES, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                },
            )
        }
        PatternKind::Grid => {
            let u = grid_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_GRID, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                },
            )
        }
        PatternKind::Rings => {
            let u = rings_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_RINGS, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_threshold = gl.get_uniform_location(program, "u_threshold");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_threshold.as_ref(), u.threshold);
                },
            )
        }
        PatternKind::Rays => {
            let u = rays_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_RAYS, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_slices = gl.get_uniform_location(program, "u_slices");
                    gl.uniform_1_f32(u_slices.as_ref(), u.slices);
                },
            )
        }
        PatternKind::Bricks => {
            let u = bricks_uniforms(density);
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_BRICKS, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_bw = gl.get_uniform_location(program, "u_bw");
                    let u_bh = gl.get_uniform_location(program, "u_bh");
                    let u_half = gl.get_uniform_location(program, "u_half");
                    gl.uniform_1_f32(u_bw.as_ref(), u.bw);
                    gl.uniform_1_f32(u_bh.as_ref(), u.bh);
                    gl.uniform_1_f32(u_half.as_ref(), u.half);
                },
            )
        }
        PatternKind::Confetti => {
            let u = confetti_uniforms(density);
            // Scale cell_ref (sized at 1024x768 reference) to the
            // actual viewport: cell = cell_ref * sqrt(actual_area /
            // ref_area). Equivalently: cell = sqrt(actual_area /
            // count). Use the actual-area form to skip the ratio.
            let actual_area = (mode_w as f32) * (mode_h as f32);
            let cell = (actual_area / u.count).sqrt();
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_CONFETTI, color_a, color_b,
                move |gl, program| unsafe {
                    use glow::HasContext;
                    let u_cell = gl.get_uniform_location(program, "u_cell");
                    gl.uniform_1_f32(u_cell.as_ref(), cell);
                },
            )
        }
    }
}

/// v1-spec-delta #6 (slice b, 2026-05-08): generic full-screen-
/// quad pattern draw helper. Mirrors `draw_gradient_pattern`'s
/// resource discipline (link program -> create VBO -> set
/// uniforms -> draw -> tear down) but factors out the per-pattern
/// uniform setup into a closure. Each pattern slice wires its
/// shader + extra uniforms via this helper instead of duplicating
/// the GL plumbing 10 times.
///
/// Standard uniforms (set unconditionally before the closure):
///   u_viewport (vec2: w, h)
///   u_color_a  (vec3 RGB)
///   u_color_b  (vec3 RGB)
/// Per-pattern uniforms (set by the closure):
///   stripes:  u_tile
///   checker:  u_tile
///   dots:     u_tile, u_radius
///   ... (slice c+)
fn draw_full_screen_pattern<F>(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    fs_src: &str,
    color_a: [f32; 4],
    color_b: [f32; 4],
    set_extra_uniforms: F,
) -> Result<()>
where
    F: FnOnce(&glow::Context, glow::Program),
{
    use glow::HasContext;
    unsafe {
        let program = link_program(gl, VS_FULLSCREEN_QUAD, fs_src)?;
        let (vbo, attrib) = match create_fullscreen_quad(gl, program) {
            Ok(pair) => pair,
            Err(e) => {
                gl.delete_program(program);
                return Err(e);
            }
        };
        gl.use_program(Some(program));
        let u_viewport = gl.get_uniform_location(program, "u_viewport");
        let u_color_a = gl.get_uniform_location(program, "u_color_a");
        let u_color_b = gl.get_uniform_location(program, "u_color_b");
        gl.uniform_2_f32(u_viewport.as_ref(), mode_w as f32, mode_h as f32);
        gl.uniform_3_f32(u_color_a.as_ref(), color_a[0], color_a[1], color_a[2]);
        gl.uniform_3_f32(u_color_b.as_ref(), color_b[0], color_b[1], color_b[2]);
        set_extra_uniforms(gl, program);
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        gl.enable_vertex_attrib_array(attrib);
        gl.vertex_attrib_pointer_f32(attrib, 2, glow::FLOAT, false, 0, 0);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        gl.disable_vertex_attrib_array(attrib);
        gl.delete_buffer(vbo);
        gl.delete_program(program);
    }
    Ok(())
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
    tex_slot: Option<&mut Option<glow::NativeTexture>>,
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
        //
        // v1-spec-delta perf-profile (qarl-direct 2026-05-08): when
        // tex_slot has a cached texture, just bind + skip the
        // create+upload (~3.5 MB / 1080p alpha bitmap). Slot empty
        // = create + upload + (optionally) store back. Slot None
        // (caller didn't pass cache) = legacy create+upload+delete
        // path, freed at end of this function.
        let (tex, owns_tex) = match tex_slot.as_deref() {
            Some(Some(t)) => {
                crate::profile::record_phase("draw_tex_hit", 1);
                gl.bind_texture(glow::TEXTURE_2D, Some(*t));
                (*t, false)
            }
            _ => {
                crate::profile::record_phase("draw_tex_miss", 1);
                let t_upload = std::time::Instant::now();
                let t = gl
                    .create_texture()
                    .map_err(|e| anyhow!("glGenTextures: {e}"))?;
                gl.bind_texture(glow::TEXTURE_2D, Some(t));
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
                crate::profile::record_phase("tex_upload", t_upload.elapsed().as_nanos() as u64);
                (t, true)
            }
        };

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
        //
        // qarl-direct perf-profile (2026-05-08): glyph programs
        // are cached in thread-local Cells across paint_slide
        // calls within the same EGL session. Cuts ~5 ms / frame
        // / layer of GLSL compile cost — was the dominant per-
        // frame cost in motion-shake at 1080p (paint p50 7.4 ms,
        // of which link_program p50 was 5 ms).
        let t_link = std::time::Instant::now();
        let program = match cached_glyph_program(gl, layer.outline) {
            Ok(p) => p,
            Err(e) => {
                if owns_tex {
                    gl.delete_texture(tex);
                }
                return Err(e);
            }
        };
        crate::profile::record_phase("link_program", t_link.elapsed().as_nanos() as u64);
        // Programs come from the thread-local cache; never freed
        // here even on error. clear_glyph_program_cache handles
        // session-teardown cleanup.
        let owns_program = false;
        let t_vbo = std::time::Instant::now();
        let vbo = match gl.create_buffer() {
            Ok(b) => b,
            Err(e) => {
                if owns_tex {
                    gl.delete_texture(tex);
                }
                return Err(anyhow!("glGenBuffers: {e}"));
            }
        };
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        crate::profile::record_phase("create_vbo", t_vbo.elapsed().as_nanos() as u64);
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            std::mem::size_of_val(&verts),
        );
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

        let a_pos = match gl.get_attrib_location(program, "a_pos") {
            Some(loc) => loc,
            None => {
                gl.delete_buffer(vbo);
                if owns_tex {
                    gl.delete_texture(tex);
                }
                return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos attribute"));
            }
        };
        let a_uv = match gl.get_attrib_location(program, "a_uv") {
            Some(loc) => loc,
            None => {
                gl.delete_buffer(vbo);
                if owns_tex {
                    gl.delete_texture(tex);
                }
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
        if owns_program {
            gl.delete_program(program);
        }
        // Texture lifecycle: if caller passed a slot AND we created
        // the texture this call, store it back so the next draw
        // reuses it. If caller didn't pass a slot, the texture is
        // ours to free now (legacy one-shot paint path). If we
        // bound a pre-cached texture (owns_tex=false), the slot
        // already owns it; do nothing.
        if owns_tex {
            match tex_slot {
                Some(slot) => *slot = Some(tex),
                None => gl.delete_texture(tex),
            }
        }
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
    /// v1-spec-delta #6 (slice a): the 10 procedural patterns
    /// share a (color_a, color_b, density) signature. Each
    /// pattern dispatches to its own fragment shader from
    /// paint_slide's BgKind::Pattern arm. Slice a only adds the
    /// dispatch shape; subsequent slices add per-pattern shaders.
    /// Until a pattern's shader lands, the dispatch falls back
    /// to a solid `color_a` fill + a `warn:` line tagged with
    /// the pattern name.
    Pattern {
        kind: PatternKind,
        color_a: [f32; 4],
        color_b: [f32; 4],
        density: f32,
    },
    /// v1-spec-delta #8 (slice b): TextSlide bg via a referenced
    /// ImageSlide. Resolved at slide-entry time from
    /// background_image_slide_id + content_root. paint_slide's
    /// BgKind::Image arm loads the PNG, uploads it as a fullscreen-
    /// blit texture, and runs FS_BLIT before the text-layer pass.
    /// `solid_fallback` is the slide's `background_color` -- if
    /// the PNG fails to load, paint_slide falls back to a solid
    /// clear so the slide still renders something.
    Image {
        asset_path: PathBuf,
        solid_fallback: [f32; 4],
    },
    Solid([f32; 4]),
}

fn resolve_slide_bg(
    slide: &TextSlide,
    content_root: Option<&Path>,
) -> Result<(BgKind, &'static str)> {
    // v1-spec-delta #8 (slice b): image bg takes precedence over
    // background_pattern + background_color when the schema
    // references an ImageSlide AND the renderer was given a
    // content_root to resolve it. If image_slide_id is set but
    // content_root is None (one-shot CLI without --content-root),
    // warn-and-fall to the existing pattern/solid path. If
    // image_slide_id is set + content_root is Some, return
    // BgKind::Image with the resolved asset path; paint_slide
    // does the actual load + upload at draw time.
    if let Some(image_id) = slide.background_image_slide_id {
        match content_root {
            Some(root) => {
                let asset_path = crate::content::image_slide_asset_path(root, image_id);
                let hex = solid_bg_hex(slide).to_string();
                let solid_fallback = hex_to_rgba(&hex)
                    .ok_or_else(|| anyhow!("invalid hex color {hex:?} for slide {}", slide.id))?;
                if slide.background_pattern.is_some() {
                    eprintln!(
                        "warn: slide {} has both background_image_slide_id and background_pattern -- image wins",
                        slide.id
                    );
                }
                return Ok((BgKind::Image { asset_path, solid_fallback }, "image"));
            }
            None => {
                eprintln!(
                    "warn: slide {} has background_image_slide_id but no content_root provided; falling back to background_color",
                    slide.id
                );
            }
        }
    }
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
        // v1-spec-delta #6 (slice a): typed dispatch for the 10
        // procedural patterns. Even when the per-kind shader
        // hasn't landed yet, the typed dispatch unifies the
        // resolve path; paint_slide's BgKind::Pattern arm
        // handles the unimplemented-shader fallback to solid
        // color_a.
        if let Some(kind) = parse_pattern_kind(&p.pattern) {
            let color_a = hex_to_rgba(&p.color_a)
                .ok_or_else(|| anyhow!("invalid color_a {:?} for slide {}", p.color_a, slide.id))?;
            let color_b = hex_to_rgba(&p.color_b)
                .ok_or_else(|| anyhow!("invalid color_b {:?} for slide {}", p.color_b, slide.id))?;
            return Ok((
                BgKind::Pattern { kind, color_a, color_b, density: p.density },
                pattern_kind_label(kind),
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
            "warn: pattern {pattern_label:?} unrecognized; falling back to background_color"
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
    content_root: Option<&Path>,
    hold_ms: u64,
) -> Result<()> {
    with_egl_session(card, |session| {
        render_slide_in_session(session, card, slide, fonts, content_root, hold_ms)
    })
}

/// v1-spec-delta #8 (slice a) -- public wrapper for one-shot
/// ImageSlide rendering. Mirrors render_slide's shape: open an
/// EglSession, render the image asset, hold for hold_ms, tear
/// down. Used by the --play-image-slide CLI flag.
pub fn render_image_slide(
    card: &Card,
    asset_path: &Path,
    hold_ms: u64,
) -> Result<()> {
    with_egl_session(card, |session| {
        render_image_slide_in_session(session, card, asset_path, hold_ms)
    })
}

/// v1-spec-delta #8 (slice a, 2026-05-08) -- decode a PNG file
/// to RGBA8 bytes + dimensions. Handles the two PIL-default color
/// types we expect to see from the openMarquee browser pipeline:
/// RGB (3 bytes/px) and RGBA (4 bytes/px). RGB is expanded to
/// RGBA in-place with alpha=255. Other color types (greyscale,
/// indexed, 16-bit) bail with a context-rich error -- the
/// browser doesn't produce them, but the diagnostic surfaces if
/// an operator hand-edits an asset.
fn load_png_rgba(path: &Path) -> Result<(Vec<u8>, u32, u32)> {
    let file = std::fs::File::open(path)
        .with_context(|| format!("open png {}", path.display()))?;
    let decoder = png::Decoder::new(file);
    let mut reader = decoder
        .read_info()
        .with_context(|| format!("png read_info {}", path.display()))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader
        .next_frame(&mut buf)
        .with_context(|| format!("png next_frame {}", path.display()))?;
    if info.bit_depth != png::BitDepth::Eight {
        bail!(
            "png {}: bit depth {:?} not supported (need 8-bit)",
            path.display(),
            info.bit_depth,
        );
    }
    let (w, h) = (info.width, info.height);
    let rgba: Vec<u8> = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => {
            let mut out = Vec::with_capacity((w * h) as usize * 4);
            for px in buf.chunks_exact(3) {
                out.extend_from_slice(&[px[0], px[1], px[2], 0xFF]);
            }
            out
        }
        other => bail!(
            "png {}: color type {other:?} not supported (need RGB or RGBA)",
            path.display(),
        ),
    };
    Ok((rgba, w, h))
}

/// v1-spec-delta #8 (slice a, 2026-05-08) -- render an ImageSlide
/// for hold_ms milliseconds. Loads the PNG asset from
/// `<content_root>/<id>/asset.png`, uploads as an RGBA8 GLES2
/// texture, blits it via FS_BLIT to fill the viewport, and holds
/// the frame on scanout for the slide's duration.
///
/// The browser pre-scales operator uploads to the panel's native
/// resolution per the ImageSlide schema docstring, so the texture
/// matches the viewport without further scaling. If the asset
/// dims don't match the mode (e.g., dev playback at a different
/// panel), FS_BLIT samples the texture across the full quad
/// regardless -- visually correct stretch with linear filtering.
///
/// Slice (a) doesn't yet support image-side transitions; the
/// reel driver hard-cuts into image slides via skip-with-warn.
/// Slice (b) extends transitions to cover image inputs.
fn render_image_slide_in_session(
    session: &mut EglSession,
    card: &Card,
    asset_path: &Path,
    hold_ms: u64,
) -> Result<()> {
    let (rgba, img_w, img_h) = load_png_rgba(asset_path)?;
    eprintln!(
        "rendering image_slide from {} ({}x{} RGBA) for {hold_ms}ms",
        asset_path.display(),
        img_w,
        img_h,
    );
    render_one_frame_in_session(session, card, hold_ms, |gl, mode_w, mode_h| {
        use glow::HasContext;
        unsafe {
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            // Upload PNG bytes as a fresh GLES2 RGBA8 texture.
            let tex = gl
                .create_texture()
                .map_err(|e| anyhow!("glGenTextures(image_slide): {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
            gl.tex_image_2d(
                glow::TEXTURE_2D,
                0,
                glow::RGBA as i32,
                img_w as i32,
                img_h as i32,
                0,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                Some(&rgba),
            );
            // Blit via FS_BLIT (existing slice 7c helper).
            let blit_result = run_blit_pass(gl, tex);
            gl.delete_texture(tex);
            blit_result?;
        }
        Ok(())
    })
}

/// v1-spec-delta #9 (slice d, 2026-05-08) -- single-frame
/// paint + present helper for the IPC sidecar. Called once per
/// Advance op (PaintSlide branch). Holds NO sleep / loop --
/// the caller (IPC dispatcher) drives pacing via stdin. The
/// session's scanout_prev / scanout_current BO/FB pair holds
/// the N-2 rotation across Advance calls.
///
/// Pre-conditions:
///   * EglSession is bound (with_egl_session is the caller).
///   * slide layers + bg are pre-resolved by caller.
///   * t_in_slide_ms is the relative ms since slide entry; the
///     state machine produces this from advance() so the
///     render side stays purely a function of (slide,
///     t_in_slide).
///
/// Post-conditions:
///   * One frame painted to scanout (set_crtc on first call,
///     page_flip thereafter via commit_fb).
///   * scanout_prev / scanout_current rotated. Stale prev BO/
///     FB freed (kernel done with it via drain in commit_fb).
///   * No sleeps. The IPC caller paces via wall-clock advance.
pub fn paint_and_present_one_frame_for_slide(
    session: &mut EglSession,
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    t_in_slide_ms: u64,
) -> Result<()> {
    use glow::HasContext;
    let (bg_kind, _pattern_label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    let tick_seconds = t_in_slide_ms as f64 / 1000.0;
    let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
    let wall_clock_unix = current_unix_seconds();

    // v1-spec-delta #10 (slice c): when settings have non-
    // identity brightness/gamma, route paint_slide through a
    // session-cached scene FBO + post-pass blit. Identity
    // settings (brightness=100 + gamma=1.0) take the direct-
    // to-default-fb path with zero post-pass cost.
    let identity = session.current_settings.is_color_identity();
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    let scene_fbo_handle = if !identity {
        Some(unsafe { ensure_scene_fbo(session, mode_w, mode_h)? })
    } else {
        None
    };
    if let Some((fbo, _tex)) = scene_fbo_handle {
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        }
    }

    paint_slide(
        session.gl,
        mode_w,
        mode_h,
        &bg_kind,
        &text_layers,
        Some(&motion_states),
        wall_clock_unix,
        None,
        Some(&mut session.image_bg_cache),
        None,  // tex_cache: one-shot capture path, no caching needed
    )?;
    unsafe { session.gl.flush(); }

    // v1-spec-delta #10 (slice c): if non-identity, the scene
    // is in scene_fbo. Bind default fb + run FS_BRIGHT_GAMMA
    // from scene_tex. Brightness divides by 100 to turn
    // schema [0, 100] into shader [0, 1].
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            run_bright_gamma_pass(session.gl, tex, brightness, gamma)?;
            session.gl.flush();
        }
    }

    // swap_buffers → lock → addFB → commit_fb. Same primitive
    // sequence as render_animated_slide_in_session's per-frame
    // loop body, with the (BO, FB) holders coming off session
    // instead of loop locals.
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer failed")?
    };
    let fb_buf = GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        // Roll back: free the new FB + drop the new BO before
        // propagating. session's scanout_*_* holders untouched
        // on this error path.
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail: {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }

    // commit_fb's drain confirmed kernel switched to scanout_
    // current (the previous frame's commit). scanout_prev (the
    // frame before that) is now safe to free.
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev_bo.take() {
        drop(bo);
    }
    // Shift: current → prev. Then store new as current.
    session.scanout_prev_fb = session.scanout_current_fb.take();
    session.scanout_prev_bo = session.scanout_current_bo.take();
    session.scanout_current_bo = Some(new_bo);
    session.scanout_current_fb = Some(new_fb);
    Ok(())
}

/// v1-spec-delta #9 (slice d) -- one-frame transition paint
/// for the IPC dispatcher's Advance(PaintTransition) branch.
/// Bakes both slide_a and slide_b into FBOs (per-call, no
/// cache yet), runs the transition shader at `progress`,
/// presents one frame. Same scanout-rotation discipline as
/// paint_and_present_one_frame_for_slide.
///
/// SLICE-D SCOPE NOTE: the FBO bake happens every call.
/// Slice (e) or follow-up adds a session-level cache keyed
/// on (from, to, fps_bucket) so a transition's per-frame
/// Advance calls don't re-bake the inputs. Today's per-call
/// rebake costs ~30 ms on vc4 at 1080p -- borderline 30 fps;
/// acceptable for v1 demo posture, but flagged for follow-up.
pub fn paint_and_present_one_transition_frame(
    session: &mut EglSession,
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    kind: &str,
    progress: f32,
) -> Result<()> {
    use glow::HasContext;
    let fs = match fs_for_transition_kind(kind) {
        Some(s) => s,
        None => {
            eprintln!(
                "warn: transition kind {kind:?} not yet implemented; falling back to cut"
            );
            FS_CUT
        }
    };
    let (bg_a, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    let mode_w_u32 = session.mode_w as u32;
    let mode_h_u32 = session.mode_h as u32;

    let work: Result<()> = (|| unsafe {
        // Bake slide_a + slide_b into FBOs (same machinery as
        // render_transition_animated_in_session's bake).
        let (fbo_a, tex_a) = make_slide_fbo(session.gl, mode_w_u32, mode_h_u32, &bg_a, &layers_a)?;
        let (fbo_b, tex_b) = match make_slide_fbo(session.gl, mode_w_u32, mode_h_u32, &bg_b, &layers_b) {
            Ok(p) => p,
            Err(e) => {
                session.gl.delete_framebuffer(fbo_a);
                session.gl.delete_texture(tex_a);
                return Err(e);
            }
        };
        let cleanup_static = |gl: &glow::Context, vbo: Option<glow::Buffer>| {
            if let Some(vbo) = vbo { gl.delete_buffer(vbo); }
            gl.delete_framebuffer(fbo_a);
            gl.delete_texture(tex_a);
            gl.delete_framebuffer(fbo_b);
            gl.delete_texture(tex_b);
        };
        let program = match link_program(session.gl, VS_TEXTURED_QUAD, fs) {
            Ok(p) => p,
            Err(e) => {
                cleanup_static(session.gl, None);
                return Err(e);
            }
        };
        // Build the textured-quad VBO inline (mirrors
        // render_transition_animated_in_session). a_pos / a_uv
        // attribs at offsets 0 / 8 with stride 16.
        let vbo = match session.gl.create_buffer() {
            Ok(b) => b,
            Err(e) => {
                cleanup_static(session.gl, None);
                session.gl.delete_program(program);
                return Err(anyhow!("glGenBuffers(transition-frame): {e}"));
            }
        };
        let verts: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        session.gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            std::mem::size_of_val(&verts),
        );
        session.gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
        let a_pos = session.gl.get_attrib_location(program, "a_pos")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos"))?;
        let a_uv = session.gl.get_attrib_location(program, "a_uv")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv"))?;
        let u_src_a = session.gl.get_uniform_location(program, "u_src_a");
        let u_src_b = session.gl.get_uniform_location(program, "u_src_b");
        let u_t = session.gl.get_uniform_location(program, "u_t");

        // v1-spec-delta #10 (slice c-2): when settings have non-
        // identity brightness/gamma, route the transition shader
        // output through the session's scene FBO + FS_BRIGHT_GAMMA
        // post-pass before scanout. Identity skips the FBO bind +
        // post-pass.
        let identity = session.current_settings.is_color_identity();
        let scene_for_post_pass = if !identity {
            Some(ensure_scene_fbo(session, mode_w_u32, mode_h_u32)?)
        } else {
            None
        };
        // Bind transition target: scene FBO (non-identity) or
        // default fb (identity).
        let transition_target = scene_for_post_pass.map(|(fbo, _)| fbo);
        session.gl.bind_framebuffer(glow::FRAMEBUFFER, transition_target);
        session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
        session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
        session.gl.clear(glow::COLOR_BUFFER_BIT);
        session.gl.use_program(Some(program));
        session.gl.active_texture(glow::TEXTURE0);
        session.gl.bind_texture(glow::TEXTURE_2D, Some(tex_a));
        session.gl.uniform_1_i32(u_src_a.as_ref(), 0);
        session.gl.active_texture(glow::TEXTURE1);
        session.gl.bind_texture(glow::TEXTURE_2D, Some(tex_b));
        session.gl.uniform_1_i32(u_src_b.as_ref(), 1);
        session.gl.uniform_1_f32(u_t.as_ref(), progress);
        session.gl.enable_vertex_attrib_array(a_pos);
        session.gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, 16, 0);
        session.gl.enable_vertex_attrib_array(a_uv);
        session.gl.vertex_attrib_pointer_f32(a_uv, 2, glow::FLOAT, false, 16, 8);
        session.gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        session.gl.disable_vertex_attrib_array(a_pos);
        session.gl.disable_vertex_attrib_array(a_uv);

        // Cleanup static (per-call FBOs + program + VBO).
        cleanup_static(session.gl, Some(vbo));
        session.gl.delete_program(program);

        // v1-spec-delta #10 (slice c-2): post-pass blit from
        // scene FBO to default fb when non-identity. Mirrors
        // paint_and_present_one_frame_for_slide's slice-c
        // dispatch.
        if let Some((_fbo, tex)) = scene_for_post_pass {
            let brightness = (session.current_settings.brightness as f32) / 100.0;
            let gamma = session.current_settings.gamma;
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
            run_bright_gamma_pass(session.gl, tex, brightness, gamma)?;
        }

        session.gl.flush();
        Ok(())
    })();
    work?;

    // swap → lock → addFB → commit_fb same as paint_and_
    // present_one_frame_for_slide.
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer failed")?
    };
    let fb_buf = GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!("warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail: {de}");
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev_bo.take() {
        drop(bo);
    }
    session.scanout_prev_fb = session.scanout_current_fb.take();
    session.scanout_prev_bo = session.scanout_current_bo.take();
    session.scanout_current_bo = Some(new_bo);
    session.scanout_current_fb = Some(new_fb);
    Ok(())
}

/// Public adapter: open a fresh EglSession and run the
/// supplied closure with it. The IPC sidecar's Open op uses
/// this so the inner loop runs inside a held session.
pub fn run_in_egl_session<F, R>(card: &Card, work: F) -> Result<R>
where
    F: FnOnce(&mut EglSession) -> Result<R>,
{
    with_egl_session(card, work)
}

/// v1-spec-delta #11 (slice a, 2026-05-08) -- read back the
/// pixels of a bound framebuffer as an RGBA8 buffer in image-
/// coord convention (y=0 at top). When `fbo` is None, reads
/// the default framebuffer (the EGL window surface). When
/// `fbo` is Some(handle), reads that FBO -- caller is
/// responsible for its lifecycle.
///
/// glReadPixels returns rows bottom-to-top in OpenGL
/// convention; this helper flips Y so the result matches
/// image-coord convention (the convention rgba_to_png_bytes +
/// the Python PIL reference both expect).
///
/// Buffer size: 4 * w * h bytes. Caller passes that buffer
/// pre-allocated to avoid a second alloc inside the hot
/// path.
pub fn capture_fbo_to_rgba(
    gl: &glow::Context,
    fbo: Option<glow::NativeFramebuffer>,
    w: u32,
    h: u32,
) -> Result<Vec<u8>> {
    use glow::HasContext;
    let stride = (w as usize) * 4;
    let total = stride * (h as usize);
    let mut gl_pixels = vec![0u8; total];
    unsafe {
        // Bind the requested FBO before glReadPixels. None ->
        // default framebuffer (FBO 0).
        gl.bind_framebuffer(glow::FRAMEBUFFER, fbo);
        gl.read_pixels(
            0,
            0,
            w as i32,
            h as i32,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelPackData::Slice(&mut gl_pixels),
        );
        let err = gl.get_error();
        if err != glow::NO_ERROR {
            return Err(anyhow!("glReadPixels: GL error 0x{err:x}"));
        }
    }
    // Flip Y. glReadPixels returns row 0 at the bottom; image-
    // coord convention (and PNG, and PIL) wants row 0 at the
    // top. In-place would need a swap-pair; allocating a new
    // buffer is simpler and the cost is one memcpy for the
    // capture path (not a hot loop).
    let mut flipped = vec![0u8; total];
    for y in 0..h as usize {
        let src_row = (h as usize - 1 - y) * stride;
        let dst_row = y * stride;
        flipped[dst_row..dst_row + stride]
            .copy_from_slice(&gl_pixels[src_row..src_row + stride]);
    }
    Ok(flipped)
}

/// v1-spec-delta #11 (slice c, 2026-05-08) -- snapshot capture
/// of a TextSlide to a PNG file. Composition over the slice-a
/// + slice-b primitives:
///   1. with_egl_session bring-up.
///   2. paint_slide into the EGL default framebuffer (no
///      scanout commit -- this is offscreen-only; the caller
///      doesn't see the slide on screen).
///   3. capture_fbo_to_rgba reads back as image-coord RGBA.
///   4. rgba_to_png_bytes encodes.
///   5. write to png_path.
///
/// Per spec §7.3 the snapshot PNG dimensions match the
/// negotiated CRTC mode (the operator's panel resolution).
pub fn capture_slide_to_png(
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    png_path: &Path,
) -> Result<()> {
    use crate::hdmi_logic::rgba_to_png_bytes;
    let (bg_kind, _label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    let motion_states = motion_states_for_layers(slide.id, &text_layers, 0.0);
    let wall_clock_unix = current_unix_seconds();
    with_egl_session(card, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;
        paint_slide(
            session.gl,
            mode_w,
            mode_h,
            &bg_kind,
            &text_layers,
            Some(&motion_states),
            wall_clock_unix,
            None,
            Some(&mut session.image_bg_cache),
            None,  // tex_cache: one-shot path, no caching needed
        )?;
        unsafe {
            use glow::HasContext;
            session.gl.flush();
        }
        let rgba = capture_fbo_to_rgba(session.gl, None, mode_w, mode_h)?;
        let png_bytes = rgba_to_png_bytes(&rgba, mode_w, mode_h)?;
        std::fs::write(png_path, &png_bytes)
            .with_context(|| format!("write png {}", png_path.display()))?;
        eprintln!(
            "captured slide {} to {} ({}x{} RGBA, {} bytes)",
            slide.id,
            png_path.display(),
            mode_w,
            mode_h,
            png_bytes.len()
        );
        Ok(())
    })
}

/// Public accessor for IPC sidecar Open op: the negotiated
/// mode (w, h) of the EglSession's CRTC.
pub fn egl_session_mode_size(session: &EglSession) -> (u32, u32) {
    (session.mode_w as u32, session.mode_h as u32)
}

impl<'a> EglSession<'a> {
    /// Public accessor for the GL context. Used by the IPC
    /// sidecar's Capture op which calls capture_fbo_to_rgba
    /// directly (no paint_and_present round-trip).
    pub fn gl(&self) -> &glow::Context {
        self.gl
    }

    /// v1-spec-delta #10 (slice c) -- update cached settings.
    /// paint_and_present_one_frame_for_slide consults
    /// current_settings.is_color_identity() to decide whether
    /// to route through the FBO post-pass.
    pub fn apply_settings(&mut self, settings: crate::content::Settings) {
        self.current_settings = settings;
    }

    /// v1-spec-delta #10 (slice c) accessor for the cached
    /// settings. Used by tests + the IPC dispatcher's
    /// Reconfigure op (slice d) to read the active state.
    pub fn current_settings(&self) -> &crate::content::Settings {
        &self.current_settings
    }

    /// v1-spec-delta #12 (slice b-2): GPU counters derived from
    /// session state. Cheap (no GL calls); inspects Option fields
    /// + image_bg_cache.len. Transient FBOs allocated inside
    /// render_transition_animated_in_session are NOT counted (they
    /// only live across the transition function's stack); the
    /// session-persistent scene FBO + scanout chain ARE. The
    /// glyph atlas is also not counted -- FontCatalog is held by
    /// callers (not the session) so the count would need a
    /// separate plumbing axis. Tracked as a slice (c) followup.
    pub fn gpu_counters(&self) -> crate::mem::GpuCounters {
        let bo = (self.scanout_prev_bo.is_some() as u32)
               + (self.scanout_current_bo.is_some() as u32);
        let fb = (self.scanout_prev_fb.is_some() as u32)
               + (self.scanout_current_fb.is_some() as u32);
        let fbo = self.scene_fbo.is_some() as u32;
        let textures = (self.scene_tex.is_some() as u32)
                     + self.image_bg_cache.len() as u32;
        crate::mem::GpuCounters { bo, fb, fbo, textures }
    }
}

/// v1-spec-delta #10 (slice c) -- lazy-allocate the per-
/// session scene FBO + texture used as the brightness/gamma
/// post-pass source. Idempotent on success: calls after the
/// first return Ok without allocating. On framebuffer-
/// incomplete, frees both before propagating Err.
unsafe fn ensure_scene_fbo(session: &mut EglSession, w: u32, h: u32) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    if let (Some(fbo), Some(tex)) = (session.scene_fbo, session.scene_tex) {
        return Ok((fbo, tex));
    }
    let (fbo, tex) = create_color_fbo(session.gl, w, h)?;
    session.scene_fbo = Some(fbo);
    session.scene_tex = Some(tex);
    Ok((fbo, tex))
}

/// v1-spec-delta #10 (slice c) -- final blit from scene FBO
/// to the EGL window surface (default fb) via FS_BRIGHT_GAMMA
/// using the session's current_settings. Caller is responsible
/// for binding the default framebuffer + setting viewport
/// before this call.
unsafe fn run_bright_gamma_pass(
    gl: &glow::Context,
    src_tex: glow::NativeTexture,
    brightness: f32,
    gamma: f32,
) -> Result<()> {
    use glow::HasContext;
    let program = link_program(gl, VS_TEXTURED_QUAD, crate::hdmi_logic::FS_BRIGHT_GAMMA)?;
    let (vbo, a_pos, a_uv) = match create_textured_quad(gl, program) {
        Ok(t) => t,
        Err(e) => {
            gl.delete_program(program);
            return Err(e);
        }
    };
    gl.use_program(Some(program));
    let u_src = gl.get_uniform_location(program, "u_src");
    let u_brightness = gl.get_uniform_location(program, "u_brightness");
    let u_gamma = gl.get_uniform_location(program, "u_gamma");
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(src_tex));
    gl.uniform_1_i32(u_src.as_ref(), 0);
    gl.uniform_1_f32(u_brightness.as_ref(), brightness);
    gl.uniform_1_f32(u_gamma.as_ref(), gamma);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(a_pos);
    gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(a_uv);
    gl.vertex_attrib_pointer_f32(a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(a_pos);
    gl.disable_vertex_attrib_array(a_uv);
    gl.delete_buffer(vbo);
    gl.delete_program(program);
    gl.bind_texture(glow::TEXTURE_2D, None);
    Ok(())
}

/// v1-spec-delta #9 (slice e -- Capture) + #10 (slice d) --
/// paint a slide into the EGL window surface for capture.
/// No swap_buffers, no commit_fb, no scanout.
///
/// v1-spec-delta #10 (slice d): when settings have non-
/// identity brightness/gamma, route paint through the
/// session-cached scene FBO + FS_BRIGHT_GAMMA post-pass so
/// the captured PNG reflects the same tonemapping as live
/// scanout. Caller's subsequent capture_fbo_to_rgba on the
/// default framebuffer reads the post-pass output.
pub fn paint_one_for_capture(
    session: &mut EglSession,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    t_in_slide_ms: u64,
) -> Result<()> {
    use glow::HasContext;
    let (bg_kind, _label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    let tick_seconds = t_in_slide_ms as f64 / 1000.0;
    let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
    let wall_clock_unix = current_unix_seconds();

    let identity = session.current_settings.is_color_identity();
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    let scene_fbo_handle = if !identity {
        Some(unsafe { ensure_scene_fbo(session, mode_w, mode_h)? })
    } else {
        None
    };
    if let Some((fbo, _tex)) = scene_fbo_handle {
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        }
    }

    paint_slide(
        session.gl,
        mode_w,
        mode_h,
        &bg_kind,
        &text_layers,
        Some(&motion_states),
        wall_clock_unix,
        None,
        Some(&mut session.image_bg_cache),
        None,  // tex_cache: one-shot path, no caching needed
    )?;
    unsafe { session.gl.flush(); }

    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            run_bright_gamma_pass(session.gl, tex, brightness, gamma)?;
            session.gl.flush();
        }
    }
    Ok(())
}

/// v1-spec-delta #5 (slice c, 2026-05-08): render a slide given
/// an already-acquired EGL session. Static dispatch goes through
/// render_one_frame_in_session; animated/auto_mode dispatch goes
/// through render_animated_slide_in_session. Reused by
/// render_playlist_reel which acquires one session for the entire
/// reel pass instead of paying ~500 ms bring-up per slide
/// (closes spec-delta MAJOR #19's BLACK gaps).
fn render_slide_in_session(
    session: &mut EglSession,
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    hold_ms: u64,
) -> Result<()> {
    let (bg_kind, pattern_label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;

    let bg_log = match &bg_kind {
        BgKind::Gradient { density, .. } => format!("pattern=gradient density={density:.3}"),
        BgKind::Pattern { kind, density, .. } => format!(
            "pattern={} density={density:.3}",
            pattern_kind_label(*kind)
        ),
        BgKind::Image { asset_path, .. } => {
            format!("pattern=image asset={}", asset_path.display())
        }
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
    // sleep path (no perf regression on FYS today).
    // Animated slides take the per-frame loop with the same legacy
    // SetCrtc per-frame. 30 fps is the target, picked to match
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
        render_animated_slide_in_session(
            session, card, &bg_kind, &text_layers, slide.id, hold_ms, 30,
        )?;
    } else {
        let motion_states = motion_states_for_layers(slide.id, &text_layers, 0.0);
        let wall_clock_unix = current_unix_seconds();
        render_one_frame_in_session(session, card, hold_ms, |gl, mode_w, mode_h| {
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
                None,  // image_bg_cache: closure-captured, no session access
                None,  // tex_cache: one-shot path, no caching needed
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
        None,  // image_bg_cache: standalone bake, no session
        None,  // tex_cache: standalone bake, no caching needed
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
    content_root: Option<&Path>,
) -> Result<(
    BgKind,
    &'static str,
    Vec<(&'a crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)>,
)> {
    let (bg_kind, pattern_label) = resolve_slide_bg(slide, content_root)?;
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
    content_root: Option<&Path>,
    t: f32,
    hold_ms: u64,
) -> Result<()> {
    let t = t.clamp(0.0, 1.0);
    let (bg_a, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;

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
    content_root: Option<&Path>,
    kind: &str,
    transition_ms: u32,
    fps: u32,
) -> Result<u32> {
    with_egl_session(card, |session| {
        render_transition_animated_in_session(
            session, card, slide_a, slide_b, fonts, content_root, kind, transition_ms, fps,
        )
    })
}

/// v1-spec-delta #5 (slice c, 2026-05-08): per-frame transition
/// work given an already-acquired EGL session. Extracted from
/// render_transition_animated so the reel driver can call this
/// under one shared with_egl_session, amortizing the ~500 ms
/// bring-up across all reel transitions (closes spec-delta
/// MAJOR #19's BLACK gaps + #8b transition wall-clock perf gap).
///
/// FBO bake + transition program + VBO + per-frame BO/FB rotation
/// are all per-call: each transition holds its own GL resources,
/// releases all of them on exit. The session's gbm_surface is
/// reused across calls but no GL state leaks between calls
/// (cleanup_static at end of work + per-call BO/FB rotation
/// cleanup).
fn render_transition_animated_in_session(
    session: &mut EglSession,
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
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
    let (bg_a, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;

    // QA-mandated single-pass transition (2026-05-08): when the
    // transition kind + slide composition fits a single fragment
    // shader (FS_FADE_SP), delegate. Eliminates the bake_a + bake_b
    // + composite three-pass structure that was the §8.3 wall-clock
    // bottleneck (1080p×3 fragment fill exceeded the 33ms vsync
    // budget at 30Hz). The eligibility check is conservative -- any
    // slide that doesn't fit (image bg, pattern bg, >4 layers,
    // outline, non-normal blend) falls through to the legacy path.
    if transition_eligible_for_single_pass(kind, &bg_a, &bg_b, &layers_a, &layers_b) {
        return render_transition_single_pass_in_session(
            session, card, slide_a, slide_b, fonts, content_root, kind, transition_ms, fps,
        );
    }

    eprintln!(
        "rendering animated transition kind={kind:?} slide_a={} slide_b={} \
         transition_ms={transition_ms} fps={fps}",
        slide_a.id, slide_b.id,
    );

    // -- Animated render work + per-frame BO/FB tracking.
    let mode_w_u32 = session.mode_w as u32;
    let mode_h_u32 = session.mode_h as u32;
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

    // qarl-direct (2026-05-08): wall-clock around the work
    // closure for the §8.3 fps log line below. Captures
    // FBO bring-up + per-frame loop + cleanup; matches the
    // "real elapsed" semantic of render_animated_slide's
    // start.elapsed log.
    let work_start_t = Instant::now();
    let work: Result<u32> = (|| {
        use glow::HasContext;
        let gl = session.gl;

        // -- Build slide_a and slide_b FBOs once.
        let (fbo_a, tex_a) = unsafe { make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_a, &layers_a)? };
        let (fbo_b, tex_b) = unsafe {
            match make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_b, &layers_b) {
                Ok(pair) => pair,
                Err(e) => {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    return Err(e);
                }
            }
        };

        // -- Get/compile transition program (cached) + build VBO.
        // qarl-direct perf-profile (2026-05-08): cached_transition_
        // program shares the FS_<KIND> compile cost across all
        // calls in the session. Cleanup at session teardown via
        // clear_transition_program_cache.
        let program = match cached_transition_program(gl, fs) {
            Ok(p) => p,
            Err(e) => {
                unsafe {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                }
                return Err(e);
            }
        };
        let cleanup_static = |gl: &glow::Context, vbo: Option<glow::NativeBuffer>| unsafe {
            if let Some(b) = vbo { gl.delete_buffer(b); }
            // Don't delete program -- it's owned by the thread-
            // local TRANSITION_PROGRAMS cache. clear_transition_
            // program_cache handles it at session teardown.
            gl.delete_framebuffer(fbo_a);
            gl.delete_texture(tex_a);
            gl.delete_framebuffer(fbo_b);
            gl.delete_texture(tex_b);
        };
        let vbo = unsafe {
            match gl.create_buffer() {
                Ok(b) => b,
                Err(e) => {
                    cleanup_static(gl, None);
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
                cleanup_static(gl, Some(vbo));
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
        // qarl-direct perf-profile (2026-05-08, post-cache hoist):
        // session-level slide cache by slide_id. Both slide_a and
        // slide_b's caches live in session.slide_caches and
        // persist across transition calls. Re-render of same slide
        // (e.g. slide N becomes slide_a in transition N→N+1, and
        // slide_b in transition N-1→N) hits cache.
        let slide_a_id = slide_a.id;
        let slide_b_id = slide_b.id;
        let layers_a_len = layers_a.len();
        let layers_b_len = layers_b.len();
        // Ensure both entries exist + are correctly sized. Free
        // any stale textures if layer count changed.
        for (sid, n) in [(slide_a_id, layers_a_len), (slide_b_id, layers_b_len)] {
            let needs_new = match session.slide_caches.get(&sid) {
                Some(c) => c.glyph.len() != n,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&sid) {
                    unsafe {
                        for slot in old.tex {
                            if let Some(t) = slot {
                                gl.delete_texture(t);
                            }
                        }
                    }
                }
                session.slide_caches.insert(sid, SlideRenderCache::new(n));
            }
        }
        let start = Instant::now();
        let mut rendered = 0_u32;
        let profile_active_t = crate::profile::is_enabled();
        let loop_result: Result<()> = (|| {
        for frame in 0..total_frames {
            if profile_active_t && crate::profile::frames_remaining() == Some(0) {
                break;
            }
            let frame_start_t = std::time::Instant::now();
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
                let t_bake_a = std::time::Instant::now();
                if any_animated_a || any_auto_a {
                    let states_a = motion_states_for_layers(
                        slide_a.id,
                        &layers_a,
                        tick_seconds,
                    );
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo_a));
                    let cache_a = session.slide_caches.get_mut(&slide_a_id)
                        .expect("slide_caches[slide_a] initialized above");
                    paint_slide(
                        &gl,
                        mode_w_u32,
                        mode_h_u32,
                        &bg_a,
                        &layers_a,
                        Some(&states_a),
                        wall_clock_unix,
                        Some(&mut cache_a.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache_a.tex),
                    )?;
                }
                crate::profile::record_phase("bake_a", t_bake_a.elapsed().as_nanos() as u64);
                let t_bake_b = std::time::Instant::now();
                if any_animated_b || any_auto_b {
                    let states_b = motion_states_for_layers(
                        slide_b.id,
                        &layers_b,
                        tick_seconds,
                    );
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo_b));
                    let cache_b = session.slide_caches.get_mut(&slide_b_id)
                        .expect("slide_caches[slide_b] initialized above");
                    paint_slide(
                        &gl,
                        mode_w_u32,
                        mode_h_u32,
                        &bg_b,
                        &layers_b,
                        Some(&states_b),
                        wall_clock_unix,
                        Some(&mut cache_b.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache_b.tex),
                    )?;
                }
                crate::profile::record_phase("bake_b", t_bake_b.elapsed().as_nanos() as u64);
                let t_composite = std::time::Instant::now();
                gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
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
                crate::profile::record_phase("composite", t_composite.elapsed().as_nanos() as u64);
            }

            // -- Push to scanout.
            let t_swap_t = std::time::Instant::now();
            session
                .egl_lib
                .swap_buffers(session.display, session.egl_surface)
                .map_err(|e| anyhow!("eglSwapBuffers (frame {frame}) failed: {e:?}"))?;
            crate::profile::record_phase("swap", t_swap_t.elapsed().as_nanos() as u64);
            let t_lockfb_t = std::time::Instant::now();
            let bo = unsafe {
                session
                    .gbm_surface
                    .lock_front_buffer()
                    .with_context(|| format!("lock_front_buffer (frame {frame})"))?
            };
            let fb_buf = GbmBufferAdapter::new(&bo)
                .with_context(|| format!("read GBM bo metadata (frame {frame})"))?;
            let fb = card
                .add_framebuffer(&fb_buf, 32, 32)
                .with_context(|| format!("drmModeAddFB (frame {frame})"))?;
            crate::profile::record_phase("lockfb", t_lockfb_t.elapsed().as_nanos() as u64);
            // QA F2 (slice c carry-over): rmFB the just-added fb
            // on commit-fail unhappy path. Pre-existing leak in
            // this transition harness mirrored across the slice
            // (c) render_animated_slide. Both fixed in this commit.
            //
            // v1-spec-delta #5 (slice d): commit_fb dispatches
            // SetCrtc-on-first-call vs page_flip-thereafter and
            // drains the prior flip event so the kernel is no
            // longer reading from the prev BO when we rotate.
            // This is the critical change for #8b -- transitions
            // were 12.6 fps with set_crtc-per-frame; page_flip
            // moves them to vsync-paced (60Hz hw vsync, target
            // 30 fps via the deadline sleep below).
            let t_commit_t = std::time::Instant::now();
            if let Err(e) = commit_fb(session, card, fb) {
                if let Err(de) = card.destroy_framebuffer(fb) {
                    eprintln!(
                        "warn: cleanup destroy_framebuffer({fb:?}) on commit-fail (frame {frame}): {de}"
                    );
                }
                drop(bo);
                return Err(e.context(format!("commit_fb (frame {frame})")));
            }
            crate::profile::record_phase("commit", t_commit_t.elapsed().as_nanos() as u64);

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
            crate::profile::record_phase("frame_total", frame_start_t.elapsed().as_nanos() as u64);
            crate::profile::frame_complete();
            // Skip pace-sleep when profiling so the histogram
            // captures real shader-bound cadence.
            // QA-direct (2026-05-08): pace_to_frame_deadline
            // hybrid-sleeps to absorb kernel overshoot.
            if !profile_active_t {
                pace_to_frame_deadline(
                    start,
                    (frame + 1) as u64,
                    frame_budget.as_nanos() as u64,
                );
            }
        }
        Ok(())
        })();
        cleanup_static(gl, Some(vbo));
        // qarl-direct perf-profile (2026-05-08, post-cache hoist):
        // tex_cache_a / tex_cache_b are now session-owned via
        // session.slide_caches; cleanup deferred to with_egl_
        // session teardown. No per-call texture free here -- the
        // whole point of the hoist is that subsequent transition
        // calls reuse these textures.
        loop_result?;
        Ok(rendered)
    })();

    // v1-spec-delta #5 (slice d): drain the last frame's pending
    // page-flip event before per-call BO/FB cleanup. Otherwise
    // the kernel may still be reading from the last frame's BO
    // when we drop it, racing the next render call's gbm_surface
    // BO pool reuse.
    drain_pending_flip(session, card);

    // Per-call cleanup. Free any remaining BO/FB pairs from the
    // loop (current + prev). The session's gbm_surface is reused
    // across calls; only the render-call-scoped GL resources go
    // here.
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
    // v1-spec-delta #5 (slice e fix): see render_one_frame_in_session.
    // Reset modeset_done after destroying scanout FB so next call
    // re-establishes via SetCrtc instead of EBUSY-ing on page_flip.
    session.modeset_done = false;

    let frame_count = work?;
    // qarl-direct (2026-05-08): the {transition_ms} field above
    // was previously a literal of the SCHEDULED parameter (e.g.
    // 800ms target), not the actual wall-clock elapsed. That's
    // useless for §8.3 fps verification because a 24-frame
    // transition that ran 1.5x over budget would still log "in
    // 800ms" — silently passing under spec. Now logs both the
    // scheduled target AND the actual elapsed_ms so the soak
    // gate can grep effective fps from any transition. Keep the
    // existing token shape ("rendered N frames in Mms") at the
    // start for backward-compat with parsers that already key on
    // it; append "(target Tms)" so the new field is unambiguous.
    let elapsed_ms = work_start_t.elapsed().as_millis();
    let effective_fps = if elapsed_ms > 0 {
        (frame_count as f64) * 1000.0 / (elapsed_ms as f64)
    } else {
        0.0
    };
    eprintln!(
        "animated transition complete: kind={kind:?} rendered {frame_count} frames in {elapsed_ms}ms (target {transition_ms}ms; effective {effective_fps:.1} fps)"
    );
    Ok(frame_count)
}

/// QA-mandated single-pass transition (2026-05-08, step 3): per-
/// transition eligibility gate. The single-pass shader can express
/// any kind for which `is_transition_kind_single_pass` returns
/// true PLUS the slide composition fits the FS layout:
///   - solid bg on both sides (no pattern/image)
///   - <= 4 visible text layers per slide (texture-unit budget)
///   - all layers blend=normal, outline=false
/// Anything else falls through to the legacy 3-pass bake+composite.
fn transition_eligible_for_single_pass(
    kind: &str,
    bg_a: &BgKind,
    bg_b: &BgKind,
    layers_a: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    layers_b: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
) -> bool {
    if !is_transition_kind_single_pass(kind) {
        return false;
    }
    if effective_solid_bg(bg_a).is_none() {
        return false;
    }
    if effective_solid_bg(bg_b).is_none() {
        return false;
    }
    if layers_a.len() > SINGLE_PASS_MAX_LAYERS_PER_SLIDE {
        return false;
    }
    if layers_b.len() > SINGLE_PASS_MAX_LAYERS_PER_SLIDE {
        return false;
    }
    for (l, _, _) in layers_a.iter().chain(layers_b.iter()) {
        if l.outline {
            return false;
        }
        if !matches!(parse_blend_mode(&l.blend), BlendMode::Normal) {
            return false;
        }
    }
    true
}

/// QA-mandated single-pass transition (2026-05-08, batch B fix):
/// returns the effective uniform-fill color for this BgKind if it's
/// equivalent to a solid color. Resolves:
///   - BgKind::Solid(c)                              -> Some(c)
///   - BgKind::Gradient with density ≈ 0             -> Some(color_a)
///     (FS_GRADIENT at density=0 outputs color_a uniformly; the
///     authored "gradient" is visually solid. Several FYS slides
///     ride this shape -- without the relaxation 2/19 slides fall
///     through to legacy.)
/// Returns None for genuine gradients (density > 0), patterns, and
/// images -- those need a non-uniform bg the SP shader doesn't
/// model and stay on the legacy 3-pass path.
fn effective_solid_bg(bg: &BgKind) -> Option<[f32; 4]> {
    match bg {
        BgKind::Solid(c) => Some(*c),
        BgKind::Gradient { color_a, density, .. } if density.abs() < 1e-4 => Some(*color_a),
        _ => None,
    }
}

/// QA-mandated single-pass transition (2026-05-08): compute a
/// layer's destination rect in v_uv space ([0,1] bottom-up) after
/// applying halign/valign + scale-around-box-center + motion-
/// translate. CPU-side; the FS just does a per-fragment in-rect
/// test + alpha sample. Mirrors the geometry math in
/// draw_text_layer so the visual result is identical to the
/// legacy bake path.
fn compute_layer_uv_rect(
    layer: &crate::content::TextLayer,
    motion_kind: MotionKind,
    motion_state: MotionState,
    bm: &AlphaBitmap,
    mode_w: u32,
    mode_h: u32,
) -> [f32; 4] {
    let halign = parse_h_align(&layer.text_align);
    let valign = VAlign::Middle;
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
    let scale = motion_state.scale.max(0.05);
    if (scale - 1.0).abs() > 1e-4 {
        let box_cx_ndc = (layer.r#box.x + layer.r#box.w * 0.5) * 2.0 - 1.0;
        let box_cy_ndc = 1.0 - (layer.r#box.y + layer.r#box.h * 0.5) * 2.0;
        ndc_l = box_cx_ndc + scale * (ndc_l - box_cx_ndc);
        ndc_r = box_cx_ndc + scale * (ndc_r - box_cx_ndc);
        ndc_t = box_cy_ndc + scale * (ndc_t - box_cy_ndc);
        ndc_b = box_cy_ndc + scale * (ndc_b - box_cy_ndc);
    }
    let box_w_px = (layer.r#box.w * mode_w as f32).max(1.0);
    let box_h_px = (layer.r#box.h * mode_h as f32).max(1.0);
    let size_px = effective_font_size_px(
        layer.font_size_px,
        layer.font_size_pct,
        layer.r#box.w,
        mode_w,
    );
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
    let to_uv = |c: f32| (c + 1.0) * 0.5;
    [to_uv(ndc_l), to_uv(ndc_b), to_uv(ndc_r), to_uv(ndc_t)]
}

/// QA-mandated single-pass transition (2026-05-08): rasterize +
/// upload + pack uniforms for one slide's text layers. Mirrors
/// paint_slide's stage-1 (rasterize-or-reuse) and stage-2 (texture
/// upload) loops, but instead of issuing per-layer GL draws it
/// returns the per-layer rect/rgba/tex tuples so the caller can
/// drive a single FS_FADE_SP draw.
///
/// The glyph_cache + tex_cache are session-owned (via
/// SlideRenderCache) and survive across transitions; cache hits
/// skip both rasterization and GL upload.
fn prepare_layers_for_single_pass(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: &[MotionState],
    wall_clock_unix: i64,
    glyph_cache: &mut GlyphCache,
    tex_cache: &mut TextureCache,
) -> Result<(Vec<[f32; 4]>, Vec<[f32; 4]>, Vec<glow::NativeTexture>)> {
    use glow::HasContext;
    if motion_states.len() != text_layers.len() {
        bail!(
            "prepare_layers_for_single_pass: motion_states len {} != layers len {}",
            motion_states.len(),
            text_layers.len(),
        );
    }
    let cal = unix_to_calendar_utc(wall_clock_unix);
    let resolved_texts: Vec<String> = text_layers
        .iter()
        .map(|(layer, _, _)| {
            format_auto_text(layer.auto_mode.as_deref(), layer.auto_format.as_deref(), cal)
                .unwrap_or_else(|| layer.text.clone())
        })
        .collect();
    if glyph_cache.len() != text_layers.len() {
        glyph_cache.clear();
        glyph_cache.resize_with(text_layers.len(), || None);
    }
    if tex_cache.len() != text_layers.len() {
        // Free any existing textures before resizing -- the slot
        // count is changing so old slot mapping is invalid.
        for slot in tex_cache.drain(..) {
            if let Some(t) = slot {
                unsafe { gl.delete_texture(t); }
            }
        }
        tex_cache.resize_with(text_layers.len(), || None);
    }
    // Stage 1: rasterize-or-reuse.
    for (i, (layer, _, font)) in text_layers.iter().enumerate() {
        let resolved_text = &resolved_texts[i];
        let size_px = effective_font_size_px(
            layer.font_size_px,
            layer.font_size_pct,
            layer.r#box.w,
            mode_w,
        );
        if should_rerasterize(glyph_cache[i].as_ref(), resolved_text, size_px) {
            if let Some(old_tex) = tex_cache[i].take() {
                unsafe { gl.delete_texture(old_tex); }
            }
            let bm = layout_text_to_alpha(font.as_ref(), resolved_text, size_px)
                .ok_or_else(|| {
                    anyhow!(
                        "layout_text_to_alpha returned None for text={resolved_text:?} size={size_px}"
                    )
                })?;
            glyph_cache[i] = Some(CachedGlyph {
                text: resolved_text.clone(),
                size_px,
                bitmap: bm,
            });
        }
    }
    // Stage 2: upload-or-reuse + pack rect/rgba.
    let mut rects: Vec<[f32; 4]> = Vec::with_capacity(text_layers.len());
    let mut rgbas: Vec<[f32; 4]> = Vec::with_capacity(text_layers.len());
    let mut texs: Vec<glow::NativeTexture> = Vec::with_capacity(text_layers.len());
    for (i, (layer, color, _)) in text_layers.iter().enumerate() {
        let cached = glyph_cache[i].as_ref().expect("cache populated above");
        let bm = &cached.bitmap;
        let tex = if let Some(t) = tex_cache[i] {
            t
        } else {
            let t = unsafe {
                let t = gl
                    .create_texture()
                    .map_err(|e| anyhow!("glGenTextures(single_pass_layer): {e}"))?;
                gl.bind_texture(glow::TEXTURE_2D, Some(t));
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
                t
            };
            tex_cache[i] = Some(t);
            t
        };
        let motion_state = motion_states[i];
        let motion_kind = parse_motion_kind(&layer.motion);
        let rect = compute_layer_uv_rect(layer, motion_kind, motion_state, bm, mode_w, mode_h);
        let opacity = (layer.opacity.clamp(0.0, 1.0)
            * motion_state.alpha_mul.clamp(0.0, 1.0))
        .clamp(0.0, 1.0);
        let rgba = [color[0], color[1], color[2], opacity];
        rects.push(rect);
        rgbas.push(rgba);
        texs.push(tex);
    }
    Ok((rects, rgbas, texs))
}

/// QA-mandated single-pass transition (2026-05-08, step 3): per-
/// frame transition that composites both slides + the per-kind
/// transition mix in ONE fragment shader pass to the default
/// framebuffer. Replaces the legacy bake_a + bake_b + composite
/// three-pass structure for transitions that satisfy
/// transition_eligible_for_single_pass.
///
/// `kind` selects the FS via fs_transition_sp_source. The slice-1
/// implementation supported only "fade"; step 3 expands to all
/// non-glitch kinds.
///
/// The fragment-fill cost drops from 3× 1080p (bake_a + bake_b +
/// composite) to 1× 1080p, matching the slide-render path's per-
/// frame budget.
///
/// Resource lifecycle mirrors render_transition_animated_in_session:
/// VBO + page-flip pacing + N-2 BO/FB rotation. Per-layer alpha-
/// bitmap textures are session-cached via slide_caches.
fn render_transition_single_pass_in_session(
    session: &mut EglSession,
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
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
    if !is_transition_kind_single_pass(kind) {
        bail!("single-pass transition: kind {kind:?} has no SP generator");
    }
    let (bg_a_kind, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b_kind, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    let bg_a_color: [f32; 3] = match effective_solid_bg(&bg_a_kind) {
        Some(c) => [c[0], c[1], c[2]],
        None => bail!("single-pass transition: bg_a not equivalent to a solid color"),
    };
    let bg_b_color: [f32; 3] = match effective_solid_bg(&bg_b_kind) {
        Some(c) => [c[0], c[1], c[2]],
        None => bail!("single-pass transition: bg_b not equivalent to a solid color"),
    };
    if layers_a.len() > SINGLE_PASS_MAX_LAYERS_PER_SLIDE
        || layers_b.len() > SINGLE_PASS_MAX_LAYERS_PER_SLIDE
    {
        bail!(
            "single-pass transition: layer count exceeds {} per slide",
            SINGLE_PASS_MAX_LAYERS_PER_SLIDE
        );
    }

    eprintln!(
        "rendering single-pass {kind} transition slide_a={} slide_b={} \
         transition_ms={transition_ms} fps={fps} layers_a={} layers_b={}",
        slide_a.id,
        slide_b.id,
        layers_a.len(),
        layers_b.len(),
    );

    let mode_w_u32 = session.mode_w as u32;
    let mode_h_u32 = session.mode_h as u32;
    let total_frames =
        ((transition_ms as f64) / 1000.0 * fps as f64).round().max(1.0) as u32;
    let frame_period_ns: u64 = 1_000_000_000_u64 / fps.max(1) as u64;

    // Ensure session caches exist + match layer counts. Stale
    // caches (layer count changed) are dropped + re-allocated;
    // their textures are freed while the GL context is bound.
    let slide_a_id = slide_a.id;
    let slide_b_id = slide_b.id;
    let layers_a_len = layers_a.len();
    let layers_b_len = layers_b.len();
    {
        use glow::HasContext;
        for (sid, n) in [(slide_a_id, layers_a_len), (slide_b_id, layers_b_len)] {
            let needs_new = match session.slide_caches.get(&sid) {
                Some(c) => c.glyph.len() != n,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&sid) {
                    unsafe {
                        for slot in old.tex {
                            if let Some(t) = slot {
                                session.gl.delete_texture(t);
                            }
                        }
                    }
                }
                session.slide_caches.insert(sid, SlideRenderCache::new(n));
            }
        }
    }
    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    let mut current_bo: Option<BufferObject<()>> = None;
    let mut current_fb: Option<framebuffer::Handle> = None;

    let work_start_t = Instant::now();
    let work: Result<u32> = (|| {
        use glow::HasContext;
        let gl = session.gl;
        let program = cached_transition_sp_program(gl, kind, layers_a_len, layers_b_len)?;

        let vbo = unsafe {
            gl.create_buffer()
                .map_err(|e| anyhow!("glGenBuffers(single_pass_fade): {e}"))?
        };
        let cleanup_static = |gl: &glow::Context, vbo: glow::NativeBuffer| unsafe {
            gl.delete_buffer(vbo);
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
                cleanup_static(gl, vbo);
                return Err(anyhow!(
                    "VS_TEXTURED_QUAD missing a_pos / a_uv (single_pass_fade)"
                ));
            }
        };
        let u_t_loc = unsafe { gl.get_uniform_location(program, "u_t") };
        let u_a_bg_loc = unsafe { gl.get_uniform_location(program, "u_a_bg") };
        let u_b_bg_loc = unsafe { gl.get_uniform_location(program, "u_b_bg") };
        // Specialized shader: only resolve uniforms for the slots
        // the shader actually emits (0..n). Returns None for slots
        // beyond the count -- those are never bound.
        let resolve_slot_locs = |prefix: &str, n: usize| -> [Option<glow::UniformLocation>; 4] {
            let mut out: [Option<glow::UniformLocation>; 4] = [None, None, None, None];
            for slot in 0..n {
                let name = format!("{prefix}{slot}");
                out[slot] = unsafe { gl.get_uniform_location(program, &name) };
            }
            out
        };
        let u_a_tex_locs = resolve_slot_locs("u_a_tex", layers_a_len);
        let u_b_tex_locs = resolve_slot_locs("u_b_tex", layers_b_len);
        let u_a_rect_locs = resolve_slot_locs("u_a_rect", layers_a_len);
        let u_b_rect_locs = resolve_slot_locs("u_b_rect", layers_b_len);
        let u_a_rgba_locs = resolve_slot_locs("u_a_rgba", layers_a_len);
        let u_b_rgba_locs = resolve_slot_locs("u_b_rgba", layers_b_len);

        let start = Instant::now();
        let mut rendered = 0_u32;
        let profile_active_t = crate::profile::is_enabled();
        let loop_result: Result<()> = (|| {
            for frame in 0..total_frames {
                if profile_active_t && crate::profile::frames_remaining() == Some(0) {
                    break;
                }
                let frame_start_t = Instant::now();
                let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
                let tick_seconds = start.elapsed().as_secs_f64();
                let wall_clock_unix = current_unix_seconds();

                let states_a =
                    motion_states_for_layers(slide_a.id, &layers_a, tick_seconds);
                let states_b =
                    motion_states_for_layers(slide_b.id, &layers_b, tick_seconds);

                let t_prep_a = Instant::now();
                let (rects_a, rgbas_a, texs_a) = {
                    let cache_a = session
                        .slide_caches
                        .get_mut(&slide_a_id)
                        .expect("slide_caches[slide_a] init above");
                    prepare_layers_for_single_pass(
                        gl,
                        mode_w_u32,
                        mode_h_u32,
                        &layers_a,
                        &states_a,
                        wall_clock_unix,
                        &mut cache_a.glyph,
                        &mut cache_a.tex,
                    )?
                };
                crate::profile::record_phase(
                    "sp_prep_a",
                    t_prep_a.elapsed().as_nanos() as u64,
                );
                let t_prep_b = Instant::now();
                let (rects_b, rgbas_b, texs_b) = {
                    let cache_b = session
                        .slide_caches
                        .get_mut(&slide_b_id)
                        .expect("slide_caches[slide_b] init above");
                    prepare_layers_for_single_pass(
                        gl,
                        mode_w_u32,
                        mode_h_u32,
                        &layers_b,
                        &states_b,
                        wall_clock_unix,
                        &mut cache_b.glyph,
                        &mut cache_b.tex,
                    )?
                };
                crate::profile::record_phase(
                    "sp_prep_b",
                    t_prep_b.elapsed().as_nanos() as u64,
                );

                let t_draw = Instant::now();
                unsafe {
                    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                    gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
                    gl.disable(glow::BLEND);
                    gl.clear_color(0.0, 0.0, 0.0, 1.0);
                    gl.clear(glow::COLOR_BUFFER_BIT);
                    gl.use_program(Some(program));
                    gl.uniform_1_f32(u_t_loc.as_ref(), t);
                    gl.uniform_3_f32(
                        u_a_bg_loc.as_ref(),
                        bg_a_color[0],
                        bg_a_color[1],
                        bg_a_color[2],
                    );
                    gl.uniform_3_f32(
                        u_b_bg_loc.as_ref(),
                        bg_b_color[0],
                        bg_b_color[1],
                        bg_b_color[2],
                    );
                    // Specialized shader: bind ONLY the slots the
                    // FS uses (0..layers_a_len for slide A, then
                    // 0..layers_b_len for slide B). No dummy
                    // textures, no unused branches in shader.
                    for slot in 0..layers_a_len {
                        let unit = slot as u32;
                        gl.active_texture(glow::TEXTURE0 + unit);
                        gl.bind_texture(glow::TEXTURE_2D, Some(texs_a[slot]));
                        gl.uniform_1_i32(u_a_tex_locs[slot].as_ref(), unit as i32);
                        let rect = rects_a[slot];
                        let rgba = rgbas_a[slot];
                        gl.uniform_4_f32(
                            u_a_rect_locs[slot].as_ref(),
                            rect[0], rect[1], rect[2], rect[3],
                        );
                        gl.uniform_4_f32(
                            u_a_rgba_locs[slot].as_ref(),
                            rgba[0], rgba[1], rgba[2], rgba[3],
                        );
                    }
                    for slot in 0..layers_b_len {
                        let unit = (layers_a_len + slot) as u32;
                        gl.active_texture(glow::TEXTURE0 + unit);
                        gl.bind_texture(glow::TEXTURE_2D, Some(texs_b[slot]));
                        gl.uniform_1_i32(u_b_tex_locs[slot].as_ref(), unit as i32);
                        let rect = rects_b[slot];
                        let rgba = rgbas_b[slot];
                        gl.uniform_4_f32(
                            u_b_rect_locs[slot].as_ref(),
                            rect[0], rect[1], rect[2], rect[3],
                        );
                        gl.uniform_4_f32(
                            u_b_rgba_locs[slot].as_ref(),
                            rgba[0], rgba[1], rgba[2], rgba[3],
                        );
                    }
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
                crate::profile::record_phase(
                    "sp_draw",
                    t_draw.elapsed().as_nanos() as u64,
                );

                let t_swap_t = Instant::now();
                session
                    .egl_lib
                    .swap_buffers(session.display, session.egl_surface)
                    .map_err(|e| anyhow!("eglSwapBuffers (frame {frame}) failed: {e:?}"))?;
                crate::profile::record_phase("swap", t_swap_t.elapsed().as_nanos() as u64);
                let t_lockfb_t = Instant::now();
                let bo = unsafe {
                    session
                        .gbm_surface
                        .lock_front_buffer()
                        .with_context(|| format!("lock_front_buffer (frame {frame})"))?
                };
                let fb_buf = GbmBufferAdapter::new(&bo)
                    .with_context(|| format!("read GBM bo metadata (frame {frame})"))?;
                let fb = card
                    .add_framebuffer(&fb_buf, 32, 32)
                    .with_context(|| format!("drmModeAddFB (frame {frame})"))?;
                crate::profile::record_phase(
                    "lockfb",
                    t_lockfb_t.elapsed().as_nanos() as u64,
                );
                let t_commit_t = Instant::now();
                if let Err(e) = commit_fb(session, card, fb) {
                    if let Err(de) = card.destroy_framebuffer(fb) {
                        eprintln!(
                            "warn: cleanup destroy_framebuffer({fb:?}) on commit-fail (frame {frame}): {de}"
                        );
                    }
                    drop(bo);
                    return Err(e.context(format!("commit_fb (frame {frame})")));
                }
                crate::profile::record_phase(
                    "commit",
                    t_commit_t.elapsed().as_nanos() as u64,
                );

                let t_rotate = Instant::now();
                if let Some(old_fb) = prev_fb.take() {
                    if let Err(e) = card.destroy_framebuffer(old_fb) {
                        eprintln!("warn: destroy_framebuffer({old_fb:?}): {e}");
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
                crate::profile::record_phase(
                    "rotate",
                    t_rotate.elapsed().as_nanos() as u64,
                );
                crate::profile::record_phase(
                    "frame_total",
                    frame_start_t.elapsed().as_nanos() as u64,
                );
                crate::profile::frame_complete();

                if !profile_active_t {
                    pace_to_frame_deadline(start, rendered as u64, frame_period_ns);
                }
            }
            Ok(())
        })();
        cleanup_static(gl, vbo);
        loop_result?;
        Ok(rendered)
    })();

    drain_pending_flip(session, card);
    for (fb_opt, bo_opt) in [
        (current_fb.take(), current_bo.take()),
        (prev_fb.take(), prev_bo.take()),
    ] {
        if let Some(fb) = fb_opt {
            if let Err(e) = card.destroy_framebuffer(fb) {
                eprintln!("warn: destroy_framebuffer({fb:?}): {e}");
            }
        }
        if let Some(bo) = bo_opt {
            drop(bo);
        }
    }
    session.modeset_done = false;

    let frame_count = work?;
    let elapsed_ms = work_start_t.elapsed().as_millis();
    let effective_fps = if elapsed_ms > 0 {
        (frame_count as f64) * 1000.0 / (elapsed_ms as f64)
    } else {
        0.0
    };
    eprintln!(
        "animated transition complete: kind={kind:?} rendered {frame_count} frames in {elapsed_ms}ms (target {transition_ms}ms; effective {effective_fps:.1} fps) [single-pass]"
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
// v1-spec-delta #3 (slice b cache): GlyphCache + CachedGlyph
// types live in hdmi_logic.rs (host-testable surface). Re-export
// here for the existing render_*_slide signatures that take
// `Option<&mut GlyphCache>`.
pub use crate::hdmi_logic::{CachedGlyph, GlyphCache};

/// qarl-direct perf-profile (2026-05-08): thread-local cache of
/// compiled glyph programs. Renderer is single-threaded, so
/// thread_local + Cell is mutex-free. EglSession teardown calls
/// clear_glyph_program_cache to delete the programs while the GL
/// context is still bound; without that they'd outlive the
/// context as dangling driver handles.
std::thread_local! {
    static FS_GLYPH_PROGRAM: std::cell::Cell<Option<glow::NativeProgram>> =
        const { std::cell::Cell::new(None) };
    static FS_GLYPH_OUTLINE_PROGRAM: std::cell::Cell<Option<glow::NativeProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_glyph_program(gl: &glow::Context, outline: bool) -> Result<glow::NativeProgram> {
    let cell = if outline { &FS_GLYPH_OUTLINE_PROGRAM } else { &FS_GLYPH_PROGRAM };
    cell.with(|c| {
        if let Some(p) = c.get() {
            return Ok(p);
        }
        let fs = if outline { FS_GLYPH_OUTLINE } else { FS_GLYPH };
        let p = link_program(gl, VS_TEXTURED_QUAD, fs)?;
        c.set(Some(p));
        Ok(p)
    })
}

/// Delete the cached programs while the GL context is still bound.
/// Called from with_egl_session teardown.
fn clear_glyph_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    FS_GLYPH_PROGRAM.with(|c| {
        if let Some(p) = c.replace(None) {
            unsafe { gl.delete_program(p); }
        }
    });
    FS_GLYPH_OUTLINE_PROGRAM.with(|c| {
        if let Some(p) = c.replace(None) {
            unsafe { gl.delete_program(p); }
        }
    });
}

/// qarl-direct perf-profile (2026-05-08): transition shader cache.
/// Each render_transition_animated_in_session invocation was
/// link_program-ing its FS source per call (~5 ms on warm cache,
/// ~165 ms on the very first compile). With 18 transitions/pass
/// in the FYS reel that's 90 ms+ of repeat compile per pass.
/// Caching by &'static str pointer (the FS source is a constant)
/// lets all 16 transition kinds share their compile cost across
/// the session.
std::thread_local! {
    static TRANSITION_PROGRAMS: std::cell::RefCell<std::collections::HashMap<*const u8, glow::NativeProgram>> =
        std::cell::RefCell::new(std::collections::HashMap::new());
}

fn cached_transition_program(gl: &glow::Context, fs: &'static str) -> Result<glow::NativeProgram> {
    TRANSITION_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        let key = fs.as_ptr();
        if let Some(&p) = cache.get(&key) {
            return Ok(p);
        }
        let p = link_program(gl, VS_TEXTURED_QUAD, fs)?;
        cache.insert(key, p);
        Ok(p)
    })
}

fn clear_transition_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    TRANSITION_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        for (_, p) in cache.drain() {
            unsafe { gl.delete_program(p); }
        }
    });
}

/// QA-mandated single-pass transition (2026-05-08, step 3
/// generalization): per-(kind, n_a, n_b) shader cache. The slice-2
/// fade-only cache became inadequate once additional transition
/// kinds gained their own specialized shaders. Keyed by
/// (kind: &'static str, n_a, n_b) tuple. The kind string is a
/// kind literal (e.g. "fade", "wipe") so the HashMap key is cheap.
/// FYS reel cycles through ~5-15 unique (kind, n_a, n_b) pairs;
/// each compiles ONCE per session.
std::thread_local! {
    static TRANSITION_SP_PROGRAMS: std::cell::RefCell<
        std::collections::HashMap<(&'static str, usize, usize), glow::NativeProgram>,
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

/// Resolve `kind` to a 'static string slice if and only if it has a
/// single-pass generator. Required because HashMap keys borrow
/// 'static; a runtime `&str` would need ownership. Mirrors the
/// match in is_transition_kind_single_pass; grows as batches port.
fn sp_kind_static(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "cut" => "cut",
        "fade" => "fade",
        "wipe" => "wipe",
        "iris" => "iris",
        "dissolve" => "dissolve",
        "scanline" => "scanline",
        "halftone" => "halftone",
        "blinds" => "blinds",
        "shutter" => "shutter",
        "slide" => "slide",
        "push" => "push",
        "scroll" => "scroll",
        "flip" => "flip",
        "marquee" => "marquee",
        "pixelate" => "pixelate",
        _ => return None,
    })
}

fn cached_transition_sp_program(
    gl: &glow::Context,
    kind: &str,
    n_a: usize,
    n_b: usize,
) -> Result<glow::NativeProgram> {
    let kind_static =
        sp_kind_static(kind).ok_or_else(|| anyhow!("kind {kind:?} has no SP generator"))?;
    TRANSITION_SP_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        if let Some(&p) = cache.get(&(kind_static, n_a, n_b)) {
            return Ok(p);
        }
        let fs = fs_transition_sp_source(kind, n_a, n_b)
            .ok_or_else(|| anyhow!("fs_transition_sp_source returned None for {kind:?}"))?;
        let p = link_program(gl, VS_TEXTURED_QUAD, &fs)
            .with_context(|| format!("link FS_{}_SP({n_a}, {n_b})", kind.to_uppercase()))?;
        cache.insert((kind_static, n_a, n_b), p);
        Ok(p)
    })
}

fn clear_transition_sp_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    TRANSITION_SP_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        for (_, p) in cache.drain() {
            unsafe { gl.delete_program(p); }
        }
    });
}

/// qarl-direct perf-profile (2026-05-08, post-cache hoist):
/// per-slide cache state stored at session level. Bundles
/// GlyphCache (alpha-bitmap rasterization) + TextureCache (GL
/// luminance texture upload) for one slide's text layers.
/// Caller (paint_slide) borrows the inner Vecs to feed the
/// existing per-call API.
pub struct SlideRenderCache {
    pub glyph: GlyphCache,
    pub tex: TextureCache,
}

impl SlideRenderCache {
    pub fn new(layer_count: usize) -> Self {
        let mut glyph: GlyphCache = Vec::with_capacity(layer_count);
        glyph.resize_with(layer_count, || None);
        let mut tex: TextureCache = Vec::with_capacity(layer_count);
        tex.resize_with(layer_count, || None);
        Self { glyph, tex }
    }
}

/// v1-spec-delta perf-profile (qarl-direct 2026-05-08): per-layer
/// GL texture cache parallel to glyph_cache. Same indexing (Vec
/// position = layer index). When a layer's bitmap is re-rasterized
/// (text/size change), paint_slide deletes the stale texture so
/// draw_text_layer re-uploads. When the bitmap is unchanged, the
/// cached texture is reused — saving ~3.5 MB / layer / frame of
/// glTexImage2D upload at 1080p text sizes.
pub type TextureCache = Vec<Option<glow::NativeTexture>>;

fn paint_slide(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: Option<&[MotionState]>,
    wall_clock_unix: i64,
    glyph_cache: Option<&mut GlyphCache>,
    mut image_bg_cache: Option<&mut ImageBgCache>,
    mut tex_cache: Option<&mut TextureCache>,
) -> Result<()> {
    use glow::HasContext;
    unsafe { gl.viewport(0, 0, mode_w as i32, mode_h as i32); }
    match bg_kind {
        BgKind::Gradient { color_a, color_b, density } => {
            draw_gradient_pattern(gl, mode_w, mode_h, *color_a, *color_b, *density)?;
        }
        BgKind::Pattern { kind, color_a, color_b, density } => {
            draw_pattern(gl, mode_w, mode_h, *kind, *color_a, *color_b, *density)?;
        }
        BgKind::Image { asset_path, solid_fallback } => {
            // Reborrow so we can hand the cache to the overlay-
            // route below if any_overlay fires for a later layer.
            draw_image_bg(gl, asset_path, *solid_fallback, image_bg_cache.as_deref_mut());
        }
        BgKind::Solid(color) => {
            draw_solid_clear(gl, *color);
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
            // BLEND once-per-paint; per-layer the blend FUNC is
            // tweaked below based on layer.blend (slice 7b).
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
        // v1-spec-delta perf-profile: when raster fires, also
        // invalidate the parallel tex_cache slot — the new bitmap
        // needs a fresh GL texture upload. Cache hit = bitmap
        // unchanged = tex stays.
        for (i, (layer, _, font)) in text_layers.iter().enumerate() {
            let resolved_text = &resolved_texts[i];
            // Compute size_px first so should_rerasterize can key
            // on (text, size_px). Pre-fix the cache keyed only on
            // text — a layout-changing edit (box.w / mode_w shrink)
            // silently kept the stale bitmap + texture.
            let size_px = effective_font_size_px(
                layer.font_size_px,
                layer.font_size_pct,
                layer.r#box.w,
                mode_w,
            );
            let needs_raster = should_rerasterize(cache_ref[i].as_ref(), resolved_text, size_px);
            if needs_raster {
                if let Some(tc) = tex_cache.as_deref_mut() {
                    if i < tc.len() {
                        if let Some(old_tex) = tc[i].take() {
                            unsafe { gl.delete_texture(old_tex); }
                        }
                    }
                }
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
                    size_px,
                    bitmap: bm,
                });
            }
        }
        // v1-spec-delta #7 (slice c): if any layer has blend=
        // overlay, take the FBO ping-pong route. Overlay's per-
        // pixel formula `mix(2·src·dst, 1-2·(1-src)·(1-dst),
        // step(0.5, dst))` needs to read dst, which fixed-function
        // blend can't express on vc4 (no GL_EXT_shader_framebuffer_
        // fetch). The FBO route renders bg + non-overlay layers
        // into a scene FBO, processes overlay layers via a
        // separate layer FBO + overlay shader pass to a scratch
        // FBO, swaps scene/scratch ping-pong, and finally blits
        // the scene FBO to the default framebuffer.
        let any_overlay = text_layers
            .iter()
            .any(|(l, _, _)| matches!(parse_blend_mode(&l.blend), BlendMode::Overlay));
        if any_overlay {
            paint_layers_via_overlay_route(
                gl,
                mode_w,
                mode_h,
                bg_kind,
                text_layers,
                motion_states,
                cache_ref,
                image_bg_cache,
            )?;
            return Ok(());
        }
        let layer_loop_result: Result<()> = (|| {
            for (i, (layer, tc, _)) in text_layers.iter().enumerate() {
                let motion_state = motion_states
                    .map(|ms| ms[i])
                    .unwrap_or(MotionState::IDENTITY);
                let motion_kind = parse_motion_kind(&layer.motion);
                // v1-spec-delta #7 (slice b): per-layer blend func
                // dispatch. The FS_GLYPH/FS_GLYPH_OUTLINE shaders
                // emit premultiplied src (text_color * alpha,
                // alpha); the blend func choice translates that
                // emit into source-over normal / multiply / screen
                // formulas without any shader change.
                //   Normal:   src_factor = ONE,                   dst_factor = ONE_MINUS_SRC_ALPHA
                //             dst' = src + (1-α) dst                       = source-over
                //   Multiply: src_factor = DST_COLOR,             dst_factor = ONE_MINUS_SRC_ALPHA
                //             dst' = (text·α) · dst + (1-α) dst   = source-over multiply
                //   Screen:   src_factor = ONE_MINUS_DST_COLOR,   dst_factor = ONE
                //             dst' = (text·α)·(1-dst) + dst        = source-over screen
                //   Overlay:  handled via the FBO route above.
                let blend_mode = parse_blend_mode(&layer.blend);
                unsafe {
                    match blend_mode {
                        BlendMode::Normal => {
                            gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
                        }
                        BlendMode::Multiply => {
                            gl.blend_func(glow::DST_COLOR, glow::ONE_MINUS_SRC_ALPHA);
                        }
                        BlendMode::Screen => {
                            gl.blend_func(glow::ONE_MINUS_DST_COLOR, glow::ONE);
                        }
                        BlendMode::Overlay => {
                            // Unreachable: any_overlay above
                            // diverted to paint_layers_via_overlay_
                            // route. Defensive in case the early
                            // return is removed.
                            unreachable!("overlay layer reached non-overlay loop");
                        }
                    }
                }
                let cached = cache_ref[i]
                    .as_ref()
                    .expect("cache entry populated above");
                let tex_slot = tex_cache.as_deref_mut().and_then(|tc| {
                    if i < tc.len() { Some(&mut tc[i]) } else { None }
                });
                draw_text_layer(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    &cached.bitmap,
                    tex_slot,
                )?;
            }
            Ok(())
        })();
        unsafe { gl.disable(glow::BLEND); }
        layer_loop_result?;
    }
    Ok(())
}

/// v1-spec-delta #7 (slice c, 2026-05-08) -- overlay-route layer
/// composite. Allocates a scene FBO + scratch FBO (ping-pong) +
/// layer FBO, renders the bg into scene_fbo, then walks the layer
/// list:
///   - normal/multiply/screen layers draw directly into the current
///     scene FBO with the slice (b) blend-func dispatch.
///   - overlay layers render their text into the layer FBO, then
///     run FS_OVERLAY_BLEND with scene_tex + layer_tex as inputs,
///     writing the composite to the scratch FBO. Scene/scratch swap.
/// At the end, the scene FBO is blitted to the default framebuffer
/// via FS_BLIT.
///
/// Resources are allocated unconditionally on entry (one each of
/// scene/scratch/layer FBO+texture) and freed unconditionally on
/// exit, including all early-return error paths. Cleanup ordering:
/// programs/VBOs first (no kernel scanout dependency), then FBOs +
/// textures.
fn paint_layers_via_overlay_route(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: Option<&[MotionState]>,
    cache_ref: &mut GlyphCache,
    image_bg_cache: Option<&mut ImageBgCache>,
) -> Result<()> {
    use glow::HasContext;
    let (scene_fbo_a, scene_tex_a) = unsafe { create_color_fbo(gl, mode_w, mode_h)? };
    let (scene_fbo_b, scene_tex_b) = unsafe {
        match create_color_fbo(gl, mode_w, mode_h) {
            Ok(p) => p,
            Err(e) => {
                gl.delete_framebuffer(scene_fbo_a);
                gl.delete_texture(scene_tex_a);
                return Err(e);
            }
        }
    };
    let (layer_fbo, layer_tex) = unsafe {
        match create_color_fbo(gl, mode_w, mode_h) {
            Ok(p) => p,
            Err(e) => {
                gl.delete_framebuffer(scene_fbo_a);
                gl.delete_texture(scene_tex_a);
                gl.delete_framebuffer(scene_fbo_b);
                gl.delete_texture(scene_tex_b);
                return Err(e);
            }
        }
    };

    let work: Result<glow::NativeTexture> = (|| unsafe {
        let mut current_scene_fbo = scene_fbo_a;
        let mut current_scene_tex = scene_tex_a;
        let mut other_scene_fbo = scene_fbo_b;
        let mut other_scene_tex = scene_tex_b;

        // Render bg into the initial scene FBO.
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(current_scene_fbo));
        gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        match bg_kind {
            BgKind::Gradient { color_a, color_b, density } => {
                draw_gradient_pattern(gl, mode_w, mode_h, *color_a, *color_b, *density)?;
            }
            BgKind::Pattern { kind, color_a, color_b, density } => {
                draw_pattern(gl, mode_w, mode_h, *kind, *color_a, *color_b, *density)?;
            }
            BgKind::Image { asset_path, solid_fallback } => {
                draw_image_bg(gl, asset_path, *solid_fallback, image_bg_cache);
            }
            BgKind::Solid(color) => {
                draw_solid_clear(gl, *color);
            }
        }

        gl.enable(glow::BLEND);
        for (i, (layer, tc, _)) in text_layers.iter().enumerate() {
            let motion_state = motion_states
                .map(|ms| ms[i])
                .unwrap_or(MotionState::IDENTITY);
            let motion_kind = parse_motion_kind(&layer.motion);
            let blend_mode = parse_blend_mode(&layer.blend);
            let cached = cache_ref[i]
                .as_ref()
                .expect("cache entry populated above");

            if !matches!(blend_mode, BlendMode::Overlay) {
                // Direct-draw into current_scene_fbo with the slice
                // (b) blend-func dispatch. Same as the non-overlay
                // path in paint_slide; just bound to an FBO instead
                // of the default framebuffer.
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(current_scene_fbo));
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                match blend_mode {
                    BlendMode::Normal => {
                        gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
                    }
                    BlendMode::Multiply => {
                        gl.blend_func(glow::DST_COLOR, glow::ONE_MINUS_SRC_ALPHA);
                    }
                    BlendMode::Screen => {
                        gl.blend_func(glow::ONE_MINUS_DST_COLOR, glow::ONE);
                    }
                    BlendMode::Overlay => unreachable!(),
                }
                draw_text_layer(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    &cached.bitmap,
                    None,  // tex_slot: overlay route is not hot path
                )?;
            } else {
                // Overlay: render text to layer_fbo (premultiplied
                // source-over to a transparent clear), then run
                // FS_OVERLAY_BLEND from current_scene_tex + layer_tex
                // into other_scene_fbo. Swap scene FBOs at end.
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(layer_fbo));
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.clear_color(0.0, 0.0, 0.0, 0.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.blend_func(glow::ONE, glow::ONE_MINUS_SRC_ALPHA);
                draw_text_layer(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    &cached.bitmap,
                    None,  // tex_slot: overlay route is not hot path
                )?;

                // Composite layer_tex over current_scene_tex into
                // other_scene_fbo.
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(other_scene_fbo));
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.disable(glow::BLEND);
                run_overlay_blend_pass(gl, current_scene_tex, layer_tex)?;
                gl.enable(glow::BLEND);

                // Swap.
                std::mem::swap(&mut current_scene_fbo, &mut other_scene_fbo);
                std::mem::swap(&mut current_scene_tex, &mut other_scene_tex);
            }
        }
        gl.disable(glow::BLEND);

        // Final blit: current_scene_tex -> default framebuffer.
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        run_blit_pass(gl, current_scene_tex)?;
        Ok(current_scene_tex)
    })();

    // Cleanup unconditional. Delete all FBOs + textures regardless
    // of which one was "current" at error time.
    unsafe {
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(scene_fbo_a);
        gl.delete_texture(scene_tex_a);
        gl.delete_framebuffer(scene_fbo_b);
        gl.delete_texture(scene_tex_b);
        gl.delete_framebuffer(layer_fbo);
        gl.delete_texture(layer_tex);
    }
    work.map(|_| ())
}

/// v1-spec-delta #7 (slice c) helper -- build a fullscreen
/// textured quad (NDC -1..1 × -1..1, UV 0..1 × 0..1) for a shader
/// that takes `a_pos: vec2` + `a_uv: vec2`. Returns the (VBO,
/// a_pos location, a_uv location) tuple. On any setup error,
/// frees the program before propagating.
unsafe fn create_textured_quad(
    gl: &glow::Context,
    program: glow::Program,
) -> Result<(glow::Buffer, u32, u32)> {
    use glow::HasContext;
    // Fullscreen quad with UV (0,0) at top-left -> bottom in NDC
    // because gl_FragCoord origin is bottom-left. We sample
    // textures that were rendered in the same convention so the
    // composite is identity-aligned.
    let verts: [f32; 16] = [
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ];
    let vbo = gl
        .create_buffer()
        .map_err(|e| anyhow!("glGenBuffers(textured-quad): {e}"))?;
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
            return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos"));
        }
    };
    let a_uv = match gl.get_attrib_location(program, "a_uv") {
        Some(loc) => loc,
        None => {
            gl.delete_buffer(vbo);
            return Err(anyhow!("VS_TEXTURED_QUAD missing a_uv"));
        }
    };
    Ok((vbo, a_pos, a_uv))
}

/// v1-spec-delta #7 (slice c) helper -- create an RGBA8 color FBO
/// + bound texture sized to (w, h). Returns the (FBO, texture)
/// pair. On framebuffer-incomplete, frees both before propagating.
unsafe fn create_color_fbo(
    gl: &glow::Context,
    w: u32,
    h: u32,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(overlay-route): {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::RGBA as i32,
        w as i32,
        h as i32,
        0,
        glow::RGBA,
        glow::UNSIGNED_BYTE,
        None,
    );
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    let fbo = match gl.create_framebuffer() {
        Ok(f) => f,
        Err(e) => {
            gl.delete_texture(tex);
            return Err(anyhow!("glGenFramebuffers(overlay-route): {e}"));
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
        return Err(anyhow!("framebuffer incomplete (overlay-route): status=0x{status:x}"));
    }
    Ok((fbo, tex))
}

/// v1-spec-delta #7 (slice c) helper -- run the FS_OVERLAY_BLEND
/// shader with `scene_tex` (current dst) + `layer_tex` (layer src,
/// premultiplied alpha) bound. Caller must have bound the target
/// FBO and disabled BLEND. The shader writes opaque alpha=1 output.
unsafe fn run_overlay_blend_pass(
    gl: &glow::Context,
    scene_tex: glow::NativeTexture,
    layer_tex: glow::NativeTexture,
) -> Result<()> {
    use glow::HasContext;
    let program = link_program(gl, VS_TEXTURED_QUAD, FS_OVERLAY_BLEND)?;
    let (vbo, a_pos, a_uv) = match create_textured_quad(gl, program) {
        Ok(t) => t,
        Err(e) => {
            gl.delete_program(program);
            return Err(e);
        }
    };
    gl.use_program(Some(program));
    let u_layer_tex = gl.get_uniform_location(program, "u_layer_tex");
    let u_slide_tex = gl.get_uniform_location(program, "u_slide_tex");
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(layer_tex));
    gl.uniform_1_i32(u_layer_tex.as_ref(), 0);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(scene_tex));
    gl.uniform_1_i32(u_slide_tex.as_ref(), 1);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(a_pos);
    gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(a_uv);
    gl.vertex_attrib_pointer_f32(a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(a_pos);
    gl.disable_vertex_attrib_array(a_uv);
    gl.delete_buffer(vbo);
    gl.delete_program(program);
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE0);
    Ok(())
}

/// v1-spec-delta #7 (slice c) helper -- blit a texture to the
/// currently-bound framebuffer via FS_BLIT. Used at end of the
/// overlay route to copy the final scene texture to the default
/// framebuffer. Caller must have bound the target FBO and set
/// the viewport.
unsafe fn run_blit_pass(
    gl: &glow::Context,
    src_tex: glow::NativeTexture,
) -> Result<()> {
    use glow::HasContext;
    let program = link_program(gl, VS_TEXTURED_QUAD, FS_BLIT)?;
    let (vbo, a_pos, a_uv) = match create_textured_quad(gl, program) {
        Ok(t) => t,
        Err(e) => {
            gl.delete_program(program);
            return Err(e);
        }
    };
    gl.use_program(Some(program));
    let u_src = gl.get_uniform_location(program, "u_src");
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(src_tex));
    gl.uniform_1_i32(u_src.as_ref(), 0);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(a_pos);
    gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(a_uv);
    gl.vertex_attrib_pointer_f32(a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(a_pos);
    gl.disable_vertex_attrib_array(a_uv);
    gl.delete_buffer(vbo);
    gl.delete_program(program);
    gl.bind_texture(glow::TEXTURE_2D, None);
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
    content_root: Option<&Path>,
    hold_ms: u64,
) -> Result<()> {
    let (bg_kind, pattern_label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;

    let bg_log = match &bg_kind {
        BgKind::Gradient { density, .. } => format!("pattern=gradient density={density:.3}"),
        BgKind::Pattern { kind, density, .. } => format!(
            "pattern={} density={density:.3}",
            pattern_kind_label(*kind)
        ),
        BgKind::Image { asset_path, .. } => {
            format!("pattern=image asset={}", asset_path.display())
        }
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
                None,  // image_bg_cache: standalone debug bake, no session
                None,  // tex_cache: standalone debug bake, no caching
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
    settings_path: Option<&Path>,
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
        bail!("reel: no playable items in playlist");
    }
    eprintln!("reel: resolved {} playable items", resolved.len());

    // v1-spec-delta #5 (slice c, 2026-05-08): one with_egl_session
    // wraps the entire reel pass. Per-slide and per-transition
    // calls reuse the shared GBM/EGL/GLES2 context, eliminating
    // the ~500 ms bring-up cost that previously sat between every
    // slide and transition (closes spec-delta MAJOR #19's BLACK
    // gaps + unblocks #8b transition wall-clock perf gap).
    //
    // Lifetime axis introduced here: EglSession outlives single
    // render_*_in_session calls. The session's gbm_surface is
    // reused across calls. Each render_*_in_session holds its own
    // (BO, FB) rotation across its own frames and releases all of
    // it on exit -- no BO/FB state leaks between calls.
    with_egl_session(card, |session| {
        // v1-spec-delta #10 (slice c-2-b): SettingsWatcher in
        // standalone reel. When --settings is provided, poll
        // between slides and apply changes to the session;
        // matches the IPC sidecar pattern but with per-slide
        // cadence (vs. per-Advance) since the reel driver
        // sleeps inside slide hold rather than yielding to a
        // tick loop. ≤2s apply per spec §8.5 holds at typical
        // FYS slide durations (1-5s).
        let mut settings_watcher = settings_path.map(|p|
            crate::content::SettingsWatcher::new(p.to_path_buf())
        );
        if let Some(w) = settings_watcher.as_mut() {
            if let Some(initial) = w.check() {
                session.apply_settings(initial);
            }
        }
        // v1-spec-delta #12 (slice b-1): baseline memory
        // snapshot at session open. The soak gate (slice c)
        // diffs per-pass values against this to compute the
        // monotonic-growth slope per §8.2. Slice (b-2) adds
        // the bo/fb/fbo/textures counters on the right.
        crate::mem::log_mem_snapshot("session=open", Some(session.gpu_counters()));
        let mut pass = 0_u32;
        loop {
            let pass_start = std::time::Instant::now();
            eprintln!(
                "reel: starting pass #{pass} ({} items, hold_override={:?}, fps={fps})",
                resolved.len(),
                hold_secs_override,
            );
            // v1-spec-delta #5 (slice e, 2026-05-08): emit
            // per-pass cumulative wall-clock so smoke can assert
            // a perf floor. Catches regressions where slice (c)
            // (single-EGL-session) or slice (d) (page_flip) are
            // silently undone -- the BLACK-gap stutter doesn't
            // re-appear on the visual side, but cumulative pass
            // time would balloon.
            let mut transitions_run = 0_u32;
            let mut slides_held = 0_u32;
            for (i, (item, _, _)) in resolved.iter().enumerate() {
                // v1-spec-delta #10 (slice c-2-b): poll settings
                // between slides. ≤2s apply at typical 1-5s
                // slide durations. Best-effort: parse failures
                // absorbed silently (last-known kept).
                if let Some(w) = settings_watcher.as_mut() {
                    if let Some(updated) = w.check() {
                        eprintln!(
                            "reel: settings.json changed (brightness={} gamma={:.2}); applying",
                            updated.brightness,
                            updated.gamma,
                        );
                        session.apply_settings(updated);
                    }
                }
                // Entry transition (skip when no predecessor).
                // v1-spec-delta #8 (slice a): image-involving
                // transitions are not yet implemented. The
                // animated-transition harness expects two
                // TextSlides for the FBO bake. When EITHER side
                // is an image, hard-cut into the new item by
                // skipping the transition with a warn line.
                if let Some(p) = prev_idx_for_reel(i, pass, resolved.len()) {
                    if p != i {
                        let (prev_item, _, _) = &resolved[p];
                        let (_, kind, transition_ms) = &resolved[i];
                        let transition_ms = clamp_transition_ms(*transition_ms);
                        match (prev_item, item) {
                            (ContentItem::Text(prev_slide), ContentItem::Text(slide)) => {
                                eprintln!(
                                    "reel: transition into item {i}/{} kind={kind:?} ms={transition_ms}",
                                    resolved.len() - 1,
                                );
                                if let Err(e) = render_transition_animated_in_session(
                                    session,
                                    card,
                                    prev_slide,
                                    slide,
                                    fonts,
                                    Some(content_root),
                                    kind,
                                    transition_ms,
                                    fps,
                                ) {
                                    eprintln!(
                                        "reel: warn — transition into item {i} failed: {e:#}; \
                                         skipping to slide hold (acts as hard cut)"
                                    );
                                } else {
                                    transitions_run += 1;
                                }
                            }
                            _ => {
                                // Image-involving transition not
                                // yet supported -- slice (b)
                                // bundles image transition support
                                // with the FBO-bake refactor.
                                eprintln!(
                                    "reel: image-involving transition into item {i} ({} -> {}) not yet implemented; using hard cut",
                                    prev_item.type_label(),
                                    item.type_label(),
                                );
                            }
                        }
                    }
                }

                // v1-spec-delta #1: ms precision. duration_ms is in
                // ms verbatim; the operator's --hold-secs override is
                // in seconds and gets ×1000'd inside
                // effective_hold_ms. FYS Panic flash slides at
                // 130/350/500/800 ms now hold for the actual
                // specified duration instead of snapping to a
                // 1-second floor.
                let hold_ms = effective_hold_ms(item.duration_ms(), hold_secs_override);
                eprintln!(
                    "reel: holding item {i}/{} ({:?} type={}) for {hold_ms}ms",
                    resolved.len() - 1,
                    item.name(),
                    item.type_label(),
                );
                let render_result = match item {
                    ContentItem::Text(slide) => {
                        render_slide_in_session(
                            session, card, slide, fonts, Some(content_root), hold_ms,
                        )
                    }
                    ContentItem::Image(slide) => {
                        let asset = image_slide_asset_path(content_root, slide.id);
                        render_image_slide_in_session(session, card, &asset, hold_ms)
                    }
                    ContentItem::Video(_slide) => {
                        // v1-spec-delta #8 (slice c, infra-only):
                        // VideoSlide schema is mirrored + dispatched
                        // here, but the actual H.264 decode pipeline
                        // doesn't ship in this slice -- approach
                        // selection (gstreamer subprocess vs ffmpeg
                        // vs raw V4L2 M2M) is qarl-direct review per
                        // QA's slicing read. Today: warn-and-fall to
                        // a hard cut so the renderer doesn't choke
                        // on video envelopes in playlists. The slot
                        // is held for the spec'd hold_ms duration
                        // (black screen) so the reel pacing is
                        // preserved.
                        eprintln!(
                            "reel: video item {i} ({:?}) decode pipeline not yet implemented; holding {}ms with black",
                            _slide.name,
                            hold_ms,
                        );
                        // Hold the slot. Use a solid-black
                        // render_solid_color call so the panel shows
                        // something deterministic for hold_ms; the
                        // slice-d follow-up replaces this with the
                        // real decode-frame path.
                        std::thread::sleep(std::time::Duration::from_millis(hold_ms));
                        Ok(())
                    }
                };
                if let Err(e) = render_result {
                    eprintln!(
                        "reel: warn — render_{} failed for item {i}: {e:#}; \
                         skipping",
                        item.type_label(),
                    );
                } else {
                    slides_held += 1;
                }
            }

            // v1-spec-delta #5 (slice e): emit per-pass wall-clock
            // for smoke assertion. The line shape is stable so the
            // smoke parser can grep+regex it ("pass=N" anchors).
            let pass_ms = pass_start.elapsed().as_millis();
            eprintln!(
                "reel: pass #{pass} complete pass_ms={pass_ms} slides_held={slides_held} \
                 transitions_run={transitions_run}",
            );
            crate::mem::log_mem_snapshot(&format!("pass={pass}"), Some(session.gpu_counters()));

            pass += 1;
            if !loop_forever {
                break;
            }
        }

        crate::mem::log_mem_snapshot("session=close", Some(session.gpu_counters()));
        eprintln!("reel: complete after {pass} pass(es)");
        Ok(())
    })
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

/// v1-spec-delta #17 (slice c, 2026-05-08): process-wide
/// `--force-mode` setting. main.rs calls set_forced_mode at
/// startup; pick_connector_and_mode reads it. OnceLock first-call-
/// wins semantics: re-calls are silently ignored, which matches
/// the CLI-flag-set-once contract. Tests don't hit hdmi so the
/// OnceLock global doesn't leak across host test runs.
static FORCED_MODE: std::sync::OnceLock<Option<crate::ForcedMode>> = std::sync::OnceLock::new();

pub fn set_forced_mode(forced: Option<crate::ForcedMode>) {
    let _ = FORCED_MODE.set(forced);
}

fn forced_mode() -> Option<crate::ForcedMode> {
    FORCED_MODE.get().copied().flatten()
}

/// v1-spec-delta #17 (slice b, 2026-05-08): synthesize a CEA-861
/// drm::Mode from a `--force-mode` request. Used when the
/// connector's EDID is missing/invalid and the safe-mode list
/// doesn't include the operator's wanted resolution. The kernel
/// still validates the mode against the driver's capabilities at
/// SetCrtc time -- an unsupported timing surfaces as
/// drmModeSetCrtc Err which the caller logs + bails.
///
/// Field-for-field copy from cea861::Cea861Timings into a
/// drm_ffi::drm_mode_modeinfo, then .into() converts to drm-rs's
/// Mode (the From impl just wraps the inner ffi struct).
pub fn synthesize_drm_mode(forced: crate::ForcedMode) -> Result<Mode> {
    use drm::control::{ModeFlags, ModeTypeFlags};
    let t = crate::cea861::lookup(forced.width, forced.height, forced.vrefresh_hz)
        .with_context(|| format!("synthesize_drm_mode({}x{}@{})",
            forced.width, forced.height, forced.vrefresh_hz))?;
    // drm_mode_modeinfo.name is c_char[32] (sign varies by arch).
    // Build a NUL-terminated label that fits.
    let label = format!("{}x{}", forced.width, forced.height);
    let mut name: [core::ffi::c_char; 32] = [0; 32];
    for (i, b) in label.bytes().take(31).enumerate() {
        name[i] = b as core::ffi::c_char;
    }
    // PHSYNC | PVSYNC matches all four entries in the cea861 table.
    // Mode type DRIVER + USERDEF tells the kernel "userspace-supplied,
    // not from EDID parsing."
    let flags = (ModeFlags::PHSYNC | ModeFlags::PVSYNC).bits();
    let type_ = (ModeTypeFlags::DRIVER | ModeTypeFlags::USERDEF).bits();
    let modeinfo = drm_ffi::drm_mode_modeinfo {
        clock: t.clock,
        hdisplay: t.hdisplay,
        hsync_start: t.hsync_start,
        hsync_end: t.hsync_end,
        htotal: t.htotal,
        hskew: 0,
        vdisplay: t.vdisplay,
        vsync_start: t.vsync_start,
        vsync_end: t.vsync_end,
        vtotal: t.vtotal,
        vscan: 0,
        vrefresh: t.vrefresh_hz,
        flags,
        type_,
        name,
    };
    Ok(modeinfo.into())
}

/// Find the first connected connector and its largest mode. Mode
/// selection delegates to `hdmi_logic::pick_largest_mode_index` so
/// the tie-breaking + max-area logic is testable without a real DRM
/// connector.
fn pick_connector_and_mode(
    card: &Card,
    resources: &drm::control::ResourceHandles,
) -> Result<(connector::Info, Mode)> {
    // v1-spec-delta #17 (slice c): when --force-mode is set, find
    // the first connected connector but synthesize the mode from
    // the CEA-861 table instead of picking from info.modes(). The
    // kernel still validates at SetCrtc time -- an unsupported
    // timing surfaces as an error which the caller (with_egl_
    // session bring-up) propagates.
    if let Some(forced) = forced_mode() {
        for &handle in resources.connectors() {
            let info = card
                .get_connector(handle, false)
                .with_context(|| format!("get_connector({handle:?})"))?;
            if info.state() != ConnectorState::Connected {
                continue;
            }
            let mode = synthesize_drm_mode(forced)
                .context("--force-mode synthesize_drm_mode")?;
            eprintln!(
                "--force-mode: synthesized {}x{}@{} bypassing connector's {} reported modes",
                forced.width, forced.height, forced.vrefresh_hz,
                info.modes().len(),
            );
            return Ok((info, mode));
        }
        bail!("--force-mode: no connected connector found");
    }
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

            // Drain the page-flip event the atomic commit just
            // queued. F1d (V1-GA-blocker) landed: poll(2) gate with
            // 500 ms timeout escapes a HW hang / missed-vblank
            // cleanly; without the gate, drm-rs's read-based
            // receive_events blocks indefinitely.
            poll_drm_fd_for_events(&card, 500)
                .context("page-flip drain (atomic commit)")?;
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

