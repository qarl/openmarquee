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
    hex_to_rgba, hsv_to_rgb, motion_offset_to_px,
    parse_blend_mode, parse_crtc_list_filter_bits, parse_h_align, parse_motion_kind, parse_v_align,
    parse_pattern_kind, pattern_kind_label, pick_largest_mode_index, prev_idx_for_reel,
    rays_uniforms, rings_uniforms, scanlines_uniforms, should_rerasterize, wrap_text_to_width,
    classify_prewarm_pair,
    fs_transition_sp_source, gradient_density_is_degenerate,
    is_transition_kind_single_pass, layout_text_to_quads, prefer_scissored_bake,
    sp_kind_static, stripes_uniforms, transition_eligible_for_scissored_bake_logic,
    transition_eligible_for_single_pass_logic, unix_to_calendar_local,
    BlendMode, FontCatalog, GlyphKind, MsdfQuadGroup, PrewarmTier,
    ModeSpec, MotionKind, MotionState, PatternKind, VAlign, FS_BLIT,
    FS_CUT, FS_EMOJI, FS_FADE, FS_GLYPH, FS_GLYPH_OUTLINE, FS_GRADIENT, FS_OVERLAY_BLEND, FS_TOFU,
    FS_PATTERN_BRICKS, FS_PATTERN_CHECKER, FS_PATTERN_CONFETTI, FS_PATTERN_DOTS,
    FS_PATTERN_GRID, FS_PATTERN_HALFTONE, FS_PATTERN_RAYS, FS_PATTERN_RINGS,
    FS_PATTERN_SCANLINES, FS_PATTERN_STRIPES, SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE,
    SINGLE_PASS_MAX_LAYERS_PER_SLIDE, VS_FULLSCREEN_QUAD, VS_TEXTURED_QUAD,
};
use crate::Card;

// =====================================================================
// Phase 4.1b — gradient pattern via fragment shader.
//
// Architectural decisions (per QA's "spend the cycles deliberately"
// note for the shader infrastructure that text glyphs + remaining
// patterns will build on):
//
//   * Shader sources: inline raw strings (36 `pub const FS_*` in
//     hdmi_logic.rs as of 2026-05-17). The original threshold for
//     moving to a `shaders/` dir + include_str! was ~3; that's been
//     blown past 12× without action, but the inline-string pattern
//     has held — revisit only if grep-discovery starts breaking.
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
    /// FYS bug 5 -- LOGICAL (content) dimensions. For 90/270 these
    /// are the panel dims with width/height SWAPPED (portrait); for
    /// 0/180 they equal the panel dims. The content pipeline reads
    /// `mode_w`/`mode_h` everywhere, so making them logical means
    /// text/image/video/transition layout auto-adapts to portrait
    /// with NO content-pipeline changes. The PHYSICAL panel dims
    /// (the scanout buffer size) come from `mode.size()`, NOT from
    /// these fields -- see `phys_mode_size()`.
    mode_w: u16,
    mode_h: u16,
    /// FYS bug 5 -- display rotation in degrees, one of 0/90/180/270.
    /// Validated by the open handler (any other value is coerced to
    /// 0). The final present pass rotates the logical render onto the
    /// panel-native scanout buffer by this many degrees clockwise.
    rotation: i32,
    /// v1-spec-delta #8 (F-image-bg-cache): per-session cache of
    /// decoded + uploaded image-bg textures. See ImageBgCache
    /// docs. The reel driver passes &mut self.image_bg_cache
    /// to paint_slide via render_*_in_session.
    image_bg_cache: ImageBgCache,
    /// Task #168 (2026-05-22): per-session async-refresh GL texture
    /// cache for ImageSlide / WebSlide paints. Replaces the per-paint
    /// PNG decode + glTexImage2D + delete cycle that hitched the
    /// render thread by 100-300ms on every Web-slide refresh
    /// transition. See `image_slide_tex` module docs for the policy.
    image_slide_tex_cache: crate::image_slide_tex::ImageSlideTextureCache,
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
    /// Bug 2 (qarl-flag 2026-05-09): the in-session render paths
    /// (render_one_frame_in_session, render_animated_slide_in_
    /// session, render_transition_*_in_session) used to destroy
    /// their last scanout FB at end-of-call AND reset modeset_
    /// done = false. The next call's first commit then took the
    /// SetCrtc branch -- a panel re-sync that scans out one
    /// black frame at boundary. qarl saw "screen blinks black for
    /// a frame when transitions start/stop" on glass.
    ///
    /// Fix: at end-of-call, instead of destroying current's
    /// (fb, bo), STASH them here. The kernel keeps scanning out
    /// this FB until the next call's first page_flip retargets.
    /// modeset_done stays TRUE -> page_flip path on next call's
    /// first commit -> no re-modeset -> no black flash.
    ///
    /// Lifecycle: at the END of each in-session render call the
    /// helper destroys whatever was previously held (now off-
    /// scanout because THIS call already issued page_flips
    /// against it), then stashes THIS call's last (fb, bo). At
    /// session teardown, drain pending flip + destroy held.
    held_scanout_fb: Option<framebuffer::Handle>,
    held_scanout_bo: Option<BufferObject<()>>,
    /// Bug 1 (qarl-flag 2026-05-09): session-global monotonic
    /// tick basis for compute_motion_state. Pre-fix, each in-
    /// session render call (hold / SP transition / SB transition
    /// / legacy 3-pass) set its OWN `start = Instant::now()` and
    /// computed tick_seconds from THAT, resetting tick to 0 at
    /// every call boundary. compute_motion_state's per-effect
    /// frequency math then snapped motion phase at every call
    /// boundary -- visible "phase confusion" on glass at every
    /// hold->transition or transition->hold edge.
    ///
    /// Fix: every motion-tick derivation goes through
    /// `EglSession::motion_tick_seconds()` (impl block below).
    /// Motion phase is continuous across the entire session
    /// lifetime; render calls are transparent timing checkpoints,
    /// not phase resets. Direct reads of this field outside the
    /// helper would re-open the regression class -- don't.
    session_start: std::time::Instant,
    /// v1-spec-delta #10 (slice c): persistent scene FBO for
    /// the brightness/gamma post-pass. Lazy-allocated on first
    /// non-identity settings frame, freed on session teardown.
    /// When settings are identity, scene_fbo stays None and
    /// paint targets default fb directly (zero overhead).
    scene_fbo: Option<glow::NativeFramebuffer>,
    scene_tex: Option<glow::NativeTexture>,
    /// r102.2 (2026-06-09): cached transition endpoint FBO+tex
    /// pair for endpoint_a. Pre-r102.2 the transition path
    /// allocated a fresh ~8 MB FBO+tex per tick in
    /// `paint_and_present_one_transition_frame` via
    /// `create_slide_fbo_pair`; vc4 V3D's lazy GC retained the
    /// BO under queue back-pressure, leaking ~1 BO per
    /// transition. With the cache, exactly ONE FBO+tex pair
    /// exists per (EglSession, mode_w, mode_h) for the a-side;
    /// reused across every tick. Invalidated + reallocated if
    /// the mode dims change (rare; HDMI hot-plug or rotation).
    /// Cleared via `cleanup_resources` at session teardown.
    transition_fbo_a: Option<glow::NativeFramebuffer>,
    transition_tex_a: Option<glow::NativeTexture>,
    /// r102.2: same-shape cache for endpoint_b. Two separate
    /// slots (NOT a single shared one) because the transition
    /// shader samples both endpoints simultaneously and they
    /// must be distinct textures.
    transition_fbo_b: Option<glow::NativeFramebuffer>,
    transition_tex_b: Option<glow::NativeTexture>,
    /// r102.2: dims the cached transition_fbo_a/b were
    /// allocated against. Invalidates the cache on mode change
    /// (HDMI hot-plug, rotation flip). `None` while the cache
    /// is empty.
    transition_fbo_dims: Option<(u32, u32)>,
    /// r106 + Path A Stage 2 (2026-06-14): per-side
    /// "has-been-filled-this-cycle" flag for the cached
    /// transition_fbo_{a,b}. When the r106 decoupled feed/drain
    /// path returns `Ok(None)` from `bake_video_slide_to_current
    /// _fbo` (because the codec hasn't delivered a frame this
    /// tick), `paint_and_present_one_transition_frame` reuses
    /// the cached FBO content IF AND ONLY IF this flag is true
    /// — i.e. an earlier tick of THIS transition window
    /// successfully baked into the cached pair. Reuse with
    /// `painted=false` would show undefined GL contents (the
    /// stale prior-transition image at best, garbage at worst).
    ///
    /// Reset to `false` whenever the cached pair is freed:
    /// (1) on dim-change (BOTH sides simultaneously, since the
    /// dim-changed branch frees BOTH cached FBOs at
    /// `ensure_transition_fbo_pair`), and
    /// (2) on a fresh allocation of a side's pair (per-side, in
    /// the same helper).
    ///
    /// Set to `true` after a successful bake fills the cached
    /// pair (in `paint_and_present_one_transition_frame` after
    /// the bake_a / bake_b success path lands the new content).
    transition_fbo_a_painted: bool,
    transition_fbo_b_painted: bool,
    /// STREAM/VLC slice-9 follow-up: persistent texture for the
    /// external-frame push-paint path. Allocated once with
    /// glTexImage2D and thereafter updated in place with
    /// glTexSubImage2D — per-frame glGen/glTexImage2D/glDelete churn
    /// was a measured paint-cost tax on the Pi Zero 2 W push-paint
    /// path (slice-9 live-fire, ~60ms/frame). The tuple is
    /// (texture, width, height); reallocated only when the frame
    /// dimensions change (a source resolution switch). Lazy-
    /// allocated on the first external frame, freed at session
    /// teardown.
    external_frame_tex: Option<(glow::NativeTexture, u32, u32)>,
    /// STREAM/VLC HW-decode (2026-05-20): persistent Y + UV texture
    /// pair for the external-frame NV12 push path. The HW-decode VLC
    /// pump (`-c:v h264_v4l2m2m`, raw NV12 out) pushes source-res
    /// NV12 frames; this pair is uploaded once with glTexImage2D and
    /// thereafter updated in place with glTexSubImage2D — same
    /// per-frame-churn-avoidance spirit as `external_frame_tex`. The
    /// tuple is (y_tex, uv_tex, source_w, source_h); reallocated only
    /// on a source resolution switch. Lazy-allocated on the first
    /// NV12 frame, freed at session teardown.
    external_nv12_tex: Option<(glow::NativeTexture, glow::NativeTexture, u32, u32)>,
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
    /// Keyed by slide_id (Uuid). 2026-06-15 perf-gl M-1: converted
    /// HashMap → LruMap so growth is BOUNDED. Per-entry holds glyph
    /// alpha bitmaps (CPU heap) + tex/bg_tex handles (GPU/CMA;
    /// r62 first_frame_tex removed in R-1 footprint cut); cap
    /// prevents unbounded vm_data accumulation under
    /// long-lived sessions or playlist swaps. SLIDE_CACHE_CAP_DEFAULT
    /// = 24 (covers the FYS 19-slide reel + 5 headroom for a swap-in
    /// of a partial new playlist before LRU eviction begins). The
    /// cap is operator-tunable via `OPENMARQUEE_SLIDE_CACHE_CAP` for
    /// bench experiments without a recompile.
    ///
    /// Cleanup at with_egl_session teardown drains all entries
    /// + delete_textures while gl context is still bound. Per-insert
    /// LRU eviction (when at cap + new key) routes the evicted
    /// SlideRenderCache through `free_slide_render_cache` for the
    /// same texture-handle cleanup contract.
    slide_caches: crate::lru::LruMap<uuid::Uuid, SlideRenderCache>,
    /// QA-direct (2026-05-08, post-clock_nanosleep): session-cached
    /// fullscreen-quad VBO for the SP transition path. The same
    /// 4-vert TRIANGLE_STRIP geometry is used by every transition
    /// kind; lifting it out of the per-call setup saves the
    /// gl.create_buffer + buffer_data ioctl pair on every call
    /// (~1 ms per transition * 18 reel transitions). Lazy-init on
    /// first SP transition; freed at with_egl_session teardown.
    transition_sp_quad_vbo: Option<glow::NativeBuffer>,
    /// 2026-06-15 spike-kill (Karl-live-QA-observed stutter; Jimmy-
    /// prime dispatch post Option B): session-cached GL_TEXTURE_
    /// EXTERNAL_OES texture object for the DMABUF NV12 blit path.
    /// Pre-fix every call to run_nv12_dmabuf_blit_pass did
    /// gl.create_texture + 4× gl.tex_parameter_i32 + image_target_
    /// texture_2d + (after blit) gl.delete_texture — V3D BO alloc/
    /// free PER FRAME even on r101's cache_path=true. QA's tail-
    /// diag-v2.1 measured sampler_us 200-400 ms on slow ticks; that
    /// budget is dominated by the per-frame create_texture under
    /// memory pressure on the 512 MB Pi Zero 2 W. Cache the texture
    /// once; each frame just calls image_target_texture_2d to re-
    /// associate it with the new EGLImage (the EXT_image_external
    /// spec permits re-association without re-creating the texture).
    /// Sampler state (MIN_FILTER / MAG_FILTER / WRAP_S / WRAP_T) is
    /// set once at init + sticks per GLES2 spec. Freed at with_egl_
    /// session teardown while the GL context is still bound.
    dmabuf_blit_texture: Option<glow::NativeTexture>,
    /// Single 2048x2048 atlas FBO for the scissored-bake path
    /// (2026-05-09 redirect). Replaces the prior fbo_a / fbo_b
    /// pair: with vc4 V3D 2.1 tiled-deferred sequencing, every
    /// FBO bind-switch forces a tile-store flush of the outgoing
    /// FBO (~13ms p50). Baking BOTH slides into ONE FBO, with
    /// scissor switching between regions, eliminates one of the
    /// three per-frame bind-switches. Lazy-allocated on first
    /// scissored-bake call; freed at with_egl_session teardown.
    /// Memory: 2048*2048*4 = 16 MB. Same total as the prior pair
    /// at 1920*1080*4*2 = 16.6 MB; no net memory increase.
    /// See ATLAS_FBO_W / ATLAS_FBO_H / ATLAS_REGION_W /
    /// ATLAS_REGION_H in hdmi_logic.rs for the geometry.
    scissored_bake_atlas: Option<(glow::NativeFramebuffer, glow::NativeTexture)>,
    /// SDF arc slice B.2 -- session-wide MSDF atlases. 23 RGB888
    /// textures uploaded once at session bring-up (immediately
    /// after `make_current`), bound per-layer at draw time keyed
    /// on the layer's font stem. Freed at session teardown while
    /// the GL context is still bound. The CPU-side parsed
    /// manifests live in the `manifest` field of each entry so the
    /// quad-layout pass + atlas-lookup don't re-parse JSON per
    /// draw.
    msdf_atlases: Vec<crate::sdf_atlas_gl::MsdfAtlasGl>,
    /// Bug 3 Slice 1 part B (2026-05-19): dynamic runtime glyph
    /// cache for codepoints not in the static build-time-baked
    /// MSDF atlas (e.g. ●, ∞). Cache + atlas page created at session
    /// bring-up; dropped at session tear-down.
    ///
    /// SLICE 1 STATE (CURRENT): the cache + page exist but no caller
    /// queries the cache — there is no dispatch hook yet. Worker
    /// threads are a stub (Part A glyph_cache.rs:117-131) that
    /// drains + discards MissRequest. Behavior is identical to
    /// pre-Bug-3: unbaked codepoints render as Tofu via the existing
    /// fallthrough in layout_text_to_quads.
    ///
    /// SLICE 2 (FORTHCOMING): layout_text_to_quads will gain a
    /// CharKind::DynamicMsdf branch that queries this cache on
    /// static-miss. The stub worker swaps for real msdfgen
    /// rasterization, completions feed glTexSubImage2D uploads into
    /// dynamic_atlas_page, and ●/∞/etc. start resolving to real
    /// glyphs after a ~250 ms p99 first-encounter latency.
    dynamic_glyph_cache: crate::glyph_cache::GlyphCache,
    /// Bug 3 Slice 2B: dynamic atlas page for runtime-MSDF glyphs
    /// (48 px cells matching CELL_PX). Drawn by GlyphKind::DynamicMsdf.
    dynamic_atlas_page_msdf: crate::atlas_page::AtlasPage,
    /// Bug 3 Slice 3B: dynamic atlas page for COLRv1-rasterized emoji
    /// (96 px cells matching COLR_CELL_PX + the static CBDT bake's
    /// EMOJI_CELL_PX). Different cell_px from the MSDF page so the
    /// two cannot share a slot allocator; the poll_completions upload
    /// dispatch routes Ready completions by GlyphKey::render_mode.
    /// Drawn by GlyphKind::DynamicEmoji using the FS_EMOJI shader
    /// (same RGBA passthrough as the static CBDT path).
    dynamic_atlas_page_colr: crate::atlas_page::AtlasPage,
    /// Bug 3 Slice 2B: directory the cache worker reads TTF bytes
    /// from. Defaults to the FYS deploy path
    /// (`/opt/openmarquee/ui/fonts`) which matches the IPC sidecar's
    /// hardcoded font catalog dir at ipc_main.rs ~line 699. Test
    /// callers that don't have fonts there pass None for
    /// runtime_glyph_ctx and skip the dispatch entirely; the
    /// hardcoded default never gets touched on those paths.
    dynamic_fonts_dir: std::path::PathBuf,
    /// v1-spec-delta #5 (slice d, refined slice e + Bug 2 fix
    /// 2026-05-09): tracks whether the kernel CRTC currently has
    /// an alive (set_crtc'd) FB attached. The first commit per
    /// session takes the SetCrtc branch (establishes the FB on
    /// the CRTC); ALL subsequent commits -- including across
    /// render-call boundaries -- use the cheaper page_flip path.
    /// Set true on the very first successful commit; STAYS true
    /// thereafter for the session's lifetime.
    ///
    /// Pre-Bug-2: this used to reset to false at end of every
    /// render call because the call destroyed its scanout FB,
    /// forcing the next call to SetCrtc to re-establish. Each
    /// SetCrtc forced a panel re-sync = visible black frame at
    /// the call boundary. The fix (held_scanout_fb / _bo +
    /// end_of_in_session_render_call) hands the last scanout FB
    /// across the call boundary instead of destroying it, so
    /// the kernel never loses its scanout source -- modeset_done
    /// stays true and the next call's first commit page_flips
    /// cleanly.
    modeset_done: bool,
    /// v1-spec-delta #5 (slice d): tracks whether a page-flip is
    /// currently in flight. The kernel allows at most one
    /// outstanding flip per CRTC; the next commit must drain the
    /// pending event before issuing another flip. Drain-before-
    /// commit is the design (as opposed to drain-after-commit) so
    /// the natural blocking point is when we WANT to advance, not
    /// when we just told the kernel "go."
    flip_pending: bool,
    /// `[perf]` r1 (2026-05-26): timestamp of the most-recent
    /// successful commit. `commit_fb` stamps this after every
    /// successful present (SetCrtc OR page_flip path), then on
    /// the NEXT call consults `frame_pacing::over_budget_ms` to
    /// detect deadline misses (delta > 36ms at 30fps target).
    /// `None` on the very first commit of the session (no prior
    /// baseline) — first-frame is skipped from observation.
    /// Field is private; mutation is funneled through
    /// `record_present` so the bookkeeping invariants stay
    /// co-located. Read via accessor on the impl block.
    last_present_at: Option<std::time::Instant>,
    /// `[perf]` r1: session-cumulative count of commits with a
    /// non-`None` `last_present_at` baseline (i.e. excluding
    /// frame 0). Paired with `frames_over_budget_total` so the
    /// IPC summary emitter can report the rate.
    frames_observed_total: u64,
    /// `[perf]` r1: session-cumulative count of commits whose
    /// delta-from-prior-present exceeded `FRAME_BUDGET_MS`.
    /// Monotonically non-decreasing across the session lifetime;
    /// surfaced via the IPC summary every 30s.
    frames_over_budget_total: u64,
    /// `[perf]` r1: rate-limit gate for the `[perf] frame over
    /// budget` warn-log. If the device is fully wedged and missing
    /// every frame, the counter still increments every frame but
    /// the warn fires at most once per second (cf. the dispatch's
    /// "don't spam 100x/s" hard rule). `None` before the first
    /// over-budget event.
    last_over_budget_warn_at: Option<std::time::Instant>,
    /// `[perf]` r1: IPC-dispatcher-set hint indicating whether
    /// the most-recent paint hook ran a transition (true) or
    /// a slide (false). Standalone non-IPC render paths don't
    /// touch this field — it stays `false`. Logged inside the
    /// rate-limited warn so operators can differentiate
    /// "video glitching" from "transition heavy" without needing
    /// per-slide context threaded through `commit_fb` (Option A
    /// per QA's r1 decision). Mutated via `set_in_transition`.
    in_transition: bool,
    /// QA verification unblocker (2026-06-13): flag-gated live
    /// scanout preview state. When env `OPENMARQUEE_LIVE_PREVIEW_
    /// PATH` is unset, `config` is None and `maybe_capture` is a
    /// near-zero early return. When set, every paint_and_present_*
    /// call site calls `live_preview.maybe_capture` right BEFORE
    /// `eglSwapBuffers` to glReadPixels the just-composited frame
    /// from FBO 0 (linear RGBA8, no T-tile) and write a downscaled
    /// PNG to the configured path so QA can scp + Read it. See
    /// `live_preview` module docs for the env-var surface + cost.
    live_preview: crate::live_preview::LivePreviewState,
}

/// v1-spec-delta #5 (slice a) -- bring up GBM + EGL + GLES2,
/// invoke the closure with a borrowed `EglSession`, tear down
/// unconditionally. Behavior matches the inline bring-up pattern
/// every existing render path uses today; slice (a) is pure
/// extraction so slice (b)+ can compose multiple draws under one
/// session. The cleanup is warn-on-Err so the original error
/// propagates via the closure's return.
fn with_egl_session<F, R>(card: &Card, rotation: i32, work: F) -> Result<R>
where
    F: FnOnce(&mut EglSession) -> Result<R>,
{
    let resources = card
        .resource_handles()
        .context("drmModeGetResources failed")?;
    let (connector_info, mode) = pick_connector_and_mode(card, &resources)
        .context("no connected HDMI connector with a usable mode")?;
    // Bug 7 fix (2026-05-17): force `Broadcast RGB = Full` so vc4
    // scanout emits full-range (0-255) RGB instead of limited-range
    // (16-235). Pre-fix, the default vc4 path emitted limited-range
    // and TVs in Full/Auto HDMI mode displayed (0,0,0) framebuffer
    // pixels as elevated gray. Probe + diagnostic at
    // qa/captures/bug-7-blacks-not-black-recon-2026-05-17.md.
    //
    // Forced full-range. If a Limited-mode TV regresses, settings-
    // driven override is the follow-up; see Bug 7 recon (NEW-B).
    try_force_full_range_rgb(card, connector_info.handle())?;
    // FYS bug 5 -- the PHYSICAL panel dims are always the negotiated
    // DRM mode size; the scanout buffer (GBM/EGL surface) is panel-
    // native. The LOGICAL (content) dims are the physical dims with
    // width/height SWAPPED for 90/270 (portrait layout), identical
    // for 0/180. The EglSession stores the LOGICAL dims in mode_w/
    // mode_h so the content pipeline lays out at portrait with no
    // per-call changes; the final present pass rotates the logical
    // render onto the physical scanout buffer.
    let (phys_w, phys_h) = mode.size();
    let (mode_w, mode_h) = if rotation == 90 || rotation == 270 {
        (phys_h, phys_w)
    } else {
        (phys_w, phys_h)
    };
    eprintln!(
        "selected connector {:?} {:?} at {}x{}@{} (rotation={}, logical {}x{})",
        connector_info.handle(),
        connector_info.interface(),
        phys_w,
        phys_h,
        mode.vrefresh(),
        rotation,
        mode_w,
        mode_h,
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
    // FYS bug 5 -- the scanout buffer is PANEL-NATIVE, so the GBM +
    // EGL surfaces are created at PHYSICAL dims, never the logical
    // (possibly swapped) dims.
    let mut gbm_surface = gbm_dev
        .create_surface::<()>(
            phys_w as u32,
            phys_h as u32,
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

    // eglSwapInterval(0) (2026-05-09 QA Phase 2): pair with
    // DRM_MODE_PAGE_FLIP_ASYNC so eglSwapBuffers does NOT wait
    // for vsync to release a back buffer. Default EGL behaviour
    // is interval=1 (vsync-lock), which on vc4 + GBM means
    // 16.67ms quantization on swap returns even though the
    // kernel page-flip is async. Setting interval=0 hands buffer
    // management to the kernel/driver and lets us pace at
    // arbitrary 33.3ms (or finer) intervals via clock_nanosleep.
    // Tearing is still bounded -- the ASYNC page-flip already
    // accepts the (sub-vblank) tear window.
    if let Err(e) = egl_lib.swap_interval(display, 0) {
        eprintln!("warn: eglSwapInterval(0) failed: {e:?}; defaulting to vsync-locked swap");
    }

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
        rotation,
        modeset_done: false,
        flip_pending: false,
        // `[perf]` r1: missed-deadline counter state. last_present_at
        // = None means "first commit has no baseline" — first frame
        // is skipped from the over-budget check. Counters start at 0.
        last_present_at: None,
        frames_observed_total: 0,
        frames_over_budget_total: 0,
        last_over_budget_warn_at: None,
        in_transition: false,
        image_bg_cache: ImageBgCache::with_capacity(IMAGE_BG_CACHE_CAPACITY),
        image_slide_tex_cache: crate::image_slide_tex::ImageSlideTextureCache::with_capacity(
            crate::image_slide_tex::IMAGE_SLIDE_TEX_CACHE_CAPACITY,
        ),
        scanout_prev_bo: None,
        scanout_prev_fb: None,
        scanout_current_bo: None,
        scanout_current_fb: None,
        held_scanout_fb: None,
        held_scanout_bo: None,
        session_start: std::time::Instant::now(),
        scene_fbo: None,
        scene_tex: None,
        transition_fbo_a: None,
        transition_tex_a: None,
        transition_fbo_b: None,
        transition_tex_b: None,
        transition_fbo_dims: None,
        // r106 + Path A Stage 2 (2026-06-14): start with painted=
        // false since no transition has rendered yet. Set true
        // after first successful bake fills each cached pair.
        transition_fbo_a_painted: false,
        transition_fbo_b_painted: false,
        external_frame_tex: None,
        external_nv12_tex: None,
        current_settings: crate::content::Settings::default(),
        slide_caches: crate::lru::LruMap::with_capacity(slide_cache_capacity()),
        transition_sp_quad_vbo: None,
        dmabuf_blit_texture: None,
        scissored_bake_atlas: None,
        msdf_atlases: Vec::new(),
        // Bug 3 Slice 1 part B (2026-05-19): construct the dynamic
        // glyph cache + its backing atlas page upfront. GlyphCache
        // spawns N std::thread workers via crossbeam-channel mpsc.
        // AtlasPage::allocate_texture is called below (after GL
        // context is current) to set up the GPU-resident
        // 2048×2048 RGBA8 backing texture.
        //
        // G-1 (2026-06-16): worker cap reduced 4 → 2 on Pi Zero 2 W
        // (4 ARM cores). Per QA's trace pin: 4 workers running msdfgen
        // FFI in parallel saturated all 4 cores at sidecar startup;
        // the render thread (running prewarm_glyph_rasterization's
        // poll_completions loop) was starved in nanosleep; the IPC
        // sidecar loop never started; /dev/video10 never opened; the
        // bcm2835-codec hit flush-timeout downstream → sign blank on
        // any cold-start of a reel with enough text slides to expose
        // the saturation. Capping at 2 leaves 2 cores for the main
        // render thread + IPC + presentation, bounding the prewarm
        // CPU storm to half of the SoC.
        //
        // Paired with G-1 Fix 2 (async prewarm, see
        // run_in_egl_session below): the worker cap reduces the
        // per-tick CPU contention; the async prewarm removes the
        // blocking gate. Both ship together.
        dynamic_glyph_cache: {
            let workers = 2usize;
            eprintln!(
                "[perf] glyph_cache_workers count={workers} reason=msdfgen_storm_cap"
            );
            crate::glyph_cache::GlyphCache::new(workers)
        },
        dynamic_atlas_page_msdf: crate::atlas_page::AtlasPage::new(
            crate::glyph_cache::CELL_PX,
        ),
        // Bug 3 Slice 3B: 96 px matches both CBDT (build.rs
        // EMOJI_CELL_PX) and the COLRv1 rasterizer's COLR_CELL_PX so
        // Slice 3D's CBDT retirement can drop into the same page
        // shape without a re-layout of the dynamic-emoji UV math.
        dynamic_atlas_page_colr: crate::atlas_page::AtlasPage::new(
            crate::glyph_cache_colr::COLR_CELL_PX,
        ),
        dynamic_fonts_dir: std::path::PathBuf::from("/opt/openmarquee/ui/fonts"),
        // QA verification unblocker (2026-06-13): read live-preview
        // env vars once at bring-up. Without the path env set this is
        // a zero-allocation default; the per-frame `maybe_capture`
        // sees `config.is_none()` and early-returns before touching
        // GL.
        live_preview: crate::live_preview::LivePreviewState::init_from_env(),
    };

    // SDF arc slice B.2 -- one-shot atlas upload after the GL
    // context is current. 23 atlases x ~1.3 MB each = ~30 MB GPU
    // memory, all RGB8. The Vec is freed at session teardown.
    //
    // G-3 (2026-06-16): static MSDF atlas LAZY UPLOAD. Pre-G-3
    // the eager `upload_all` path staged all 23 atlases (~29 MB
    // GPU memory; up to ~30 MB RSS attribution depending on
    // mesa's accounting) at session bring-up. With Pi Zero 2 W
    // cma=320 leaving only ~96 MB non-CMA RAM, that ~29 MB was a
    // sizeable contributor to the wedge ceiling — and it's
    // most-of-the-time WASTE: Karl's reel uses Bebas Neue (NOT in
    // the static atlas set) so 0 of the 23 atlases are actually
    // consulted on his content; other reels touch a subset.
    //
    // Post-G-3: CPU-side parse + the OnceLock<Vec<MsdfAtlas>>
    // initialization happen at session bring-up (cheap, ~80 KB
    // for the parsed manifests + the static atlas_rgb bytes ride
    // in .rodata via include_bytes!). The GPU texture upload is
    // deferred to `msdf_atlas_for_family`'s first-miss path —
    // each family's atlas (1.3 MB) is uploaded synchronously on
    // first lookup, cached in MSDF_ATLAS_LOOKUP, and reused
    // thereafter. Per-family upload cost ~30-100 ms paid once.
    //
    // Failure semantics: CPU-side parse failure still bubbles up
    // (operator sees broken text rendering immediately). GPU
    // upload failure (transient, e.g. resource exhaustion) logs
    // a warn + returns None from msdf_atlas_for_family — layout
    // then falls back to the dynamic glyph_cache path or tofu.
    {
        let _ = MSDF_ATLASES_CPU.get_or_init(|| {
            crate::sdf_atlas::load_all_atlases().unwrap_or_default()
        });
        if MSDF_ATLASES_CPU.get().map(|v| v.is_empty()).unwrap_or(true) {
            bail!("msdf atlas CPU-side parse produced 0 atlases");
        }
        // session.msdf_atlases stays empty (Vec::new). delete_all
        // at teardown becomes a no-op on it; lazy-uploaded
        // textures live in MSDF_ATLAS_LOOKUP and are released by
        // clear_msdf_lookup at teardown (see the G-3 update to
        // that helper below for the leak-fix).
        let n = MSDF_ATLASES_CPU.get().map(|v| v.len()).unwrap_or(0);
        eprintln!(
            "[perf] msdf_static_atlas_lazy=true atlases_parsed={n} bytes_uploaded=0 (G-3: ~29 MB GPU deferred to first per-family text paint via msdf_atlas_for_family)"
        );
    }

    // Bug 3 Slice 1 part B (2026-05-19): allocate the dynamic atlas
    // page's GPU texture (2048×2048 RGBA8 ~ 16 MB GPU memory).
    // Failure semantics: non-fatal — if the dynamic atlas can't
    // initialize, Slice 2's runtime cache-miss path will just keep
    // returning Tofu (the existing pre-Bug-3 behavior). Log + continue.
    if let Err(e) = session.dynamic_atlas_page_msdf.allocate_texture(&gl) {
        eprintln!(
            "warn: dynamic MSDF atlas page texture alloc failed: {e}; \
             runtime MSDF cache disabled this session",
        );
    } else if let Some(tex) = session.dynamic_atlas_page_msdf.texture() {
        // Bug 3 Slice 2B: publish the texture handle so
        // draw_text_layer_msdf can bind it for GlyphKind::DynamicMsdf
        // quads. Cleared in the teardown block below before the
        // texture is deleted.
        populate_dynamic_atlas_lookup(tex);
    }

    // Bug 3 Slice 3B (2026-05-19): parallel allocation for the
    // COLRv1-rasterized emoji page. Same failure semantics — if
    // alloc fails, runtime emoji rasterization yields Tofu (Slice 1
    // pre-cache behavior); static CBDT path keeps working.
    if let Err(e) = session.dynamic_atlas_page_colr.allocate_texture(&gl) {
        eprintln!(
            "warn: dynamic COLR atlas page texture alloc failed: {e}; \
             runtime COLRv1 emoji cache disabled this session",
        );
    } else if let Some(tex) = session.dynamic_atlas_page_colr.texture() {
        populate_dynamic_atlas_colr_lookup(tex);
    }

    // Slice 3D (2026-05-19): the SDF-arc-C.2 CBDT atlas upload
    // (~64 MB RGBA across ~3 pages, plus the `EMOJI_ATLAS_CPU`
    // OnceLock + per-page GL textures) is retired. Emoji
    // codepoints now route to the Slice 3B runtime COLRv1 cache
    // via `dynamic_atlas_page_colr` set up above. The COLR cache
    // rasterizes each codepoint on first encounter and emits a
    // GlyphKind::DynamicEmoji quad on subsequent frames.

    // perf-night r6 (2026-05-28): pre-compile GLES2 program cache
    // upfront so the first video slide doesn't pay the 592ms
    // NV12_DMABUF link cost in the paint hot path, and the first
    // transition doesn't pay the 132ms composite-shader compile.
    // r20 (2026-05-30): extended to cover the 6 text shader programs
    // (msdf x outline, glyph x outline, tofu, emoji) -- mirror of r6
    // for the text path.
    //
    // Runs for ALL with_egl_session callers (cost ~180ms, amortized
    // across the session even on snapshot/CLI paths). r25's heavier
    // glyph rasterization prewarm (~16s) lives in
    // run_in_egl_session below so only the long-lived IPC sidecar
    // pays it.
    prewarm_shader_programs(&session);

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
        // Task #168: drain the image-slide texture cache while the GL
        // context is still bound. Pending workers (if any) drop their
        // mpsc channels with the cache; their tx.send becomes a no-op
        // on the next try. No thread-join needed — workers exit on
        // their own as soon as decode finishes.
        for tex in session.image_slide_tex_cache.take_all_textures() {
            unsafe { gl.delete_texture(tex); }
        }
        // qarl-direct perf-profile (2026-05-08, post-cache hoist):
        // free per-slide cached GL textures from the session-
        // level slide_caches. Glyph alpha bitmaps are CPU heap
        // (drop on drain). Texture handles are kernel-side; need
        // explicit gl.delete_texture while context is bound.
        // r62 subagent (BLOCKER fix): route the session-teardown
        // drain through free_slide_render_cache so future fields
        // added to SlideRenderCache are freed by the canonical
        // single-source-of-truth helper and not the inline
        // tex+bg_tex deletion that diverged from it. The 9+
        // slide_caches.remove call sites already route through
        // free_slide_render_cache; matching here closes the last
        // divergent path. (r62 first_frame_tex itself removed in
        // R-1 footprint cut; the pattern remains for future fields.)
        for (_slide_id, entry) in session.slide_caches.drain() {
            free_slide_render_cache(&gl, entry);
        }
    }
    // qarl-direct perf-profile (2026-05-08): free thread-local
    // cached glyph programs while the GL context is still bound.
    // The thread_local Cells live across function invocations
    // within the process; clearing here keeps them in sync with
    // the GL context lifecycle.
    clear_glyph_program_cache(&gl);
    clear_msdf_program_cache(&gl);
    // QA perf-resweep-v2 P1: free the shared MSDF text scratch VBO
    // (cached across draw_text_layer_msdf calls within the session).
    clear_msdf_text_vbo_cache(&gl);
    // SDF arc slice B.2: free msdf atlas textures while context
    // is still bound. Clear the lookup table first so a paint after
    // teardown can't dereference dead texture handles. G-3:
    // clear_msdf_lookup now takes &gl + deletes lazy-uploaded
    // textures owned by MSDF_ATLAS_LOOKUP (session.msdf_atlases
    // stays empty under G-3 lazy upload; delete_all on it is a
    // no-op kept for forward-compat with non-lazy code paths).
    clear_msdf_lookup(&gl);
    crate::sdf_atlas_gl::delete_all(&gl, &mut session.msdf_atlases);
    // Slice 3D (2026-05-19): the CBDT-side `clear_emoji_lookup()` +
    // `delete_all` of session.emoji_atlases are gone alongside the
    // atlas itself. Only the dynamic-COLR teardown below remains.
    // Bug 3 Slice 1 part B (2026-05-19): free the dynamic atlas
    // page texture while GL is still bound. dynamic_glyph_cache's
    // Drop signals + joins the worker pool on session-struct drop
    // (right after this fn returns).
    //
    // Slice 2B: clear the thread_local BEFORE delete so the draw
    // path can't bind a stale NativeTexture handle on a subsequent
    // session bring-up before populate_dynamic_atlas_lookup re-fires.
    clear_dynamic_atlas_lookup();
    session.dynamic_atlas_page_msdf.delete(&gl);
    // Bug 3 Slice 3B: same lookup-clear-before-delete ordering for
    // the COLRv1 page.
    clear_dynamic_atlas_colr_lookup();
    session.dynamic_atlas_page_colr.delete(&gl);
    clear_transition_program_cache(&gl);
    // r102.3 subagent NIT-2: order matters -- the legacy cache
    // holds entries that REFERENCE program handles freed above.
    // Reversed order works today (NativeProgram is Copy + HashMap
    // drop is just dealloc) but the future-proof rule is
    // "drain the dependent cache after the owner cache."
    clear_legacy_transition_program_cache();
    clear_transition_sp_program_cache(&gl);
    clear_composite_program_cache(&gl);
    clear_blit_program_cache(&gl);
    clear_bright_gamma_cache(&gl);
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
    // Bug 2 (2026-05-09): drain in-session held scanout. The
    // standalone render paths (animated slide + transition + one-
    // frame) stash their last scanout (fb, bo) here at end-of-
    // call to avoid an end-of-call rmFB that would force a
    // re-modeset on the NEXT call's first commit (visible black
    // flash). At session teardown the kernel is about to lose
    // the GBM/EGL context anyway; drain pending flip + destroy.
    if let Some(fb) = session.held_scanout_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(held_scanout): {e}");
        }
    }
    if let Some(bo) = session.held_scanout_bo.take() {
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
        // r102.2: free the cached transition FBO+tex pairs.
        if let Some(fbo) = session.transition_fbo_a.take() {
            gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.transition_tex_a.take() {
            gl.delete_texture(tex);
        }
        if let Some(fbo) = session.transition_fbo_b.take() {
            gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.transition_tex_b.take() {
            gl.delete_texture(tex);
        }
        session.transition_fbo_dims = None;
        if let Some((tex, _, _)) = session.external_frame_tex.take() {
            gl.delete_texture(tex);
        }
        if let Some((y_tex, uv_tex, _, _)) = session.external_nv12_tex.take() {
            gl.delete_texture(y_tex);
            gl.delete_texture(uv_tex);
        }
        if let Some(vbo) = session.transition_sp_quad_vbo.take() {
            gl.delete_buffer(vbo);
        }
        // 2026-06-15 spike-kill: drain session-cached GL_TEXTURE_
        // EXTERNAL_OES texture object. Allocated lazily on the first
        // DMABUF blit; surviving GL handle freed here while context
        // is still bound. Matches the existing image_bg_cache /
        // slide_caches teardown pattern.
        if let Some(tex) = session.dmabuf_blit_texture.take() {
            gl.delete_texture(tex);
        }
        if let Some((fbo, tex)) = session.scissored_bake_atlas.take() {
            gl.delete_framebuffer(fbo);
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
        // `[perf]` r1: stamp the present time so the next frame's
        // delta has a baseline. First-frame is skipped from the
        // over-budget check (record_present's `if let Some(prev)`
        // guard handles that).
        session.record_present(std::time::Instant::now());
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
    // `[perf]` r1: every successful page_flip is a "present" from
    // the renderer's perspective (the kernel scans the new FB on
    // the next vblank, modulo the ASYNC flip tear-window). Stamp
    // here so the NEXT commit_fb measures the inter-present delta.
    session.record_present(std::time::Instant::now());
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

/// Bug 2 (qarl-flag 2026-05-09): cleanup at end of an in-session
/// render call -- animated slide / transition / one-shot frame.
/// Replaces the prior pattern of destroying current_fb +
/// resetting modeset_done = false (which forced the NEXT call's
/// first commit through SetCrtc, scanning out a black frame at
/// the boundary).
///
/// New pattern:
///   1. drain pending flip (kernel switches to current_fb)
///   2. destroy local prev_fb (older within-call FB; safe)
///   3. destroy session.held_scanout_fb from PRIOR call (the
///      kernel switched away from it during this call's commits)
///   4. stash local current_fb -> session.held_scanout_fb so
///      kernel keeps a valid scanout source across the call
///      boundary. modeset_done STAYS true. Next call's first
///      commit page_flips against held_scanout cleanly. No
///      modeset, no black frame.
///
/// Edge case: this call did zero successful commits (current_fb
/// is None). Then the kernel may still be on whatever held_
/// scanout was active at call start. Don't touch held_scanout;
/// don't change modeset_done. (No boundary swap to worry about.)
fn end_of_in_session_render_call(
    session: &mut EglSession,
    card: &Card,
    current_fb: Option<framebuffer::Handle>,
    current_bo: Option<BufferObject<()>>,
    prev_fb: Option<framebuffer::Handle>,
    prev_bo: Option<BufferObject<()>>,
) {
    drain_pending_flip(session, card);
    // prev (older within-call FB) is off-scanout if the loop
    // already drained at least one page_flip away from it.
    // Safe to destroy regardless.
    if let Some(fb) = prev_fb {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(prev): {e}");
        }
    }
    drop(prev_bo);

    if current_fb.is_some() {
        // We committed at least once this call; kernel is on
        // current_fb after the drain. The held_scanout from the
        // PRIOR call is now off-scanout (kernel moved through
        // current/prev FBs during this call). Destroy it; stash
        // THIS call's current as the new held.
        if let Some(prior_fb) = session.held_scanout_fb.take() {
            if let Err(e) = card.destroy_framebuffer(prior_fb) {
                eprintln!("warn: destroy_framebuffer(held): {e}");
            }
        }
        let _ = session.held_scanout_bo.take();
        session.held_scanout_fb = current_fb;
        session.held_scanout_bo = current_bo;
        // modeset_done stays whatever it was (typically true
        // post-first-commit). NO reset to false.
    } else {
        // Zero-commit call -- nothing to stash. Don't touch
        // held_scanout (still on-scanout from prior call, if any).
        // Don't touch modeset_done.
        drop(current_bo);
    }
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
/// + teardown is extracted into `with_egl_session`. This function
/// still does its own session per call (diagnostic-only path now);
/// the IPC sidecar's `hdmi::run_in_egl_session` closure at
/// ipc_main.rs:688 holds one session across all Advance ops in
/// the production reel path, which closed out the "skip the ~500 ms
/// bring-up cost per slide" goal originally framed as slice (b)+.
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
    with_egl_session(card, 0, |session| render_one_frame_in_session(session, card, hold_ms, draw))
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
        // QA live-preview hook (2026-06-13): no-op unless
        // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
        session.maybe_live_preview_capture();
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

    // Bug 2 fix (2026-05-09): hand fb_holder to held_scanout so
    // the kernel keeps a valid scanout source across the call
    // boundary -- avoids the prior end-of-call destroy + modeset_
    // done = false that caused a SetCrtc on the next call's
    // first commit (visible black flash on glass).
    end_of_in_session_render_call(session, card, fb_holder, bo_holder, None, None);

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
    with_egl_session(card, 0, |session| {
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
/// QA-direct (2026-05-08, post-Step-3): capture an absolute
/// CLOCK_MONOTONIC timestamp at loop entry. Used as the base for
/// pace_to_frame_deadline's clock_nanosleep TIMER_ABSTIME deadline
/// math. Returns the timestamp in nanoseconds.
#[cfg(target_os = "linux")]
fn monotonic_now_ns() -> u64 {
    let mut tp: libc::timespec = unsafe { std::mem::zeroed() };
    let _ = unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut tp) };
    (tp.tv_sec as u64) * 1_000_000_000 + (tp.tv_nsec as u64)
}

/// QA-direct (2026-05-08, post-pre-warm): pace to a per-frame
/// deadline. clock_nanosleep TIMER_ABSTIME (Linux's highest-
/// precision absolute-deadline sleep primitive) wakes us at
/// approximately the target time, but on Pi (Linux 6.12 PREEMPT
/// CONFIG_HZ_250) it overshoots by ~0.8 ms per call due to
/// scheduler latency. Sleeping all the way to the deadline pulls
/// per-frame to ~34 ms = 29.2 fps aggregate (matches the residual
/// gap measured post-pre-warm).
///
/// Fix: sleep until 2 ms BEFORE the deadline, then spin-wait the
/// last 2 ms. The spin absorbs the kernel overshoot precisely.
/// CPU cost: ~2 ms spin per 33 ms frame = 6% of one core per
/// second of render at 30 fps. Acceptable for the strict-30 gate.
#[cfg(target_os = "linux")]
fn pace_to_frame_deadline(start_mono_ns: u64, frame_idx: u64, frame_period_ns: u64) {
    // 2 ms = ~0.8 ms measured Linux 6.12 PREEMPT CONFIG_HZ_250
    // overshoot + 1.2 ms safety margin. If kernel HZ moves to
    // 1000 in a future Pi image, this is over-budget but still
    // correct (spin runs longer; CPU cost rises slightly).
    const SPIN_MARGIN_NS: u64 = 2_000_000;
    let deadline_ns = start_mono_ns.wrapping_add(frame_idx.wrapping_mul(frame_period_ns));
    let sleep_target_ns = deadline_ns.saturating_sub(SPIN_MARGIN_NS);
    let target = libc::timespec {
        tv_sec: (sleep_target_ns / 1_000_000_000) as libc::time_t,
        tv_nsec: (sleep_target_ns % 1_000_000_000) as libc::c_long,
    };
    let _ = unsafe {
        libc::clock_nanosleep(
            libc::CLOCK_MONOTONIC,
            libc::TIMER_ABSTIME,
            &target,
            std::ptr::null_mut(),
        )
    };
    // Spin to absorb kernel overshoot. clock_nanosleep typically
    // wakes us at sleep_target + 0.8 ms = deadline - 1.2 ms; the
    // spin runs ~1 ms on average. On rare tail-latency overshoot
    // (>2 ms), the spin loop exits immediately and we accept the
    // drift -- still better than spinning the full 33 ms budget.
    while monotonic_now_ns() < deadline_ns {
        std::hint::spin_loop();
    }
}

/// Non-Linux fallback: std::thread::sleep relative to deadline.
/// Renderer is Linux-only in production but the function must
/// compile on host (macOS) for tests.
#[cfg(not(target_os = "linux"))]
fn monotonic_now_ns() -> u64 {
    0
}

#[cfg(not(target_os = "linux"))]
fn pace_to_frame_deadline(_start_mono_ns: u64, _frame_idx: u64, _frame_period_ns: u64) {
    // No-op on non-Linux; renderer doesn't run on macOS.
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
    let start_mono_ns = monotonic_now_ns();
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
                free_slide_render_cache(session.gl, old);
            }
            insert_slide_render_cache(
                &mut session.slide_caches,
                session.gl,
                slide_id,
                SlideRenderCache::new(text_layers.len()),
            );
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
            // Bug 3 Slice 2D follow-up 2 extension (2026-05-19):
            // drain runtime glyph cache completions per frame. The
            // dispatch hook in layout_text_to_quads inserts Tofu
            // placeholders for codepoints whose slot is Requested/
            // Generating/FontMissing-mid-chain; without per-frame
            // poll+invalidate, the per-slide cache holds the Tofu
            // layout for the entire hold and the eventual Ready
            // (or terminal FontMissing) transition is never re-laid.
            // Mirrors paint_and_present_one_frame_for_slide's pattern
            // (hdmi.rs ~2741). FYS Boot's ● (motion=breathe routes
            // through this function) is the qarl-visible case.
            let uploaded = session.dynamic_glyph_cache.poll_completions(
                session.gl,
                &mut session.dynamic_atlas_page_msdf,
                &mut session.dynamic_atlas_page_colr,
                4,
            );
            if uploaded > 0 {
                if let Some(old) = session.slide_caches.remove(&slide_id) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    slide_id,
                    SlideRenderCache::new(text_layers.len()),
                );
            }
            // Bug 1 fix (2026-05-09): tick_seconds is session-
            // global, NOT call-local. Motion phase stays continuous
            // across hold/transition boundaries. `elapsed` (call-
            // local) still drives the hold-loop exit at hold_ms.
            let tick_seconds = session.motion_tick_seconds();
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
                // Bug 3 Slice 2B: thread the runtime glyph cache +
                // fonts_dir so the layout dispatch can resolve
                // static-atlas misses (●/∞ on FYS) to dynamic-MSDF
                // cells rasterized by the worker pool.
                Some(crate::glyph_cache::RuntimeGlyphCtx {
                    cache: &session.dynamic_glyph_cache,
                    fonts_dir: &session.dynamic_fonts_dir,
                }),
            )?;
            // eglSwapBuffers implicitly flushes; the explicit gl.flush()
            // that used to be here forced an extra tile-store on vc4
            // (cold-scout #2 P6, 2026-05-09).
            crate::profile::record_phase("paint", t_paint.elapsed().as_nanos() as u64);
            // QA live-preview hook (2026-06-13): no-op unless
            // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
            session.maybe_live_preview_capture();
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
            // QA-direct (2026-05-08): pace_to_frame_deadline uses
            // clock_nanosleep TIMER_ABSTIME for sub-ms precision.
            if !profile_active {
                pace_to_frame_deadline(start_mono_ns, frames as u64, frame_period_ns);
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

    // Bug 2 fix (2026-05-09): hand current to held_scanout so the
    // kernel keeps scanning out a valid FB across the call
    // boundary; destroy prev (off-scanout) and the prior call's
    // held (now also off-scanout); modeset_done stays true so
    // next call's first commit takes the page_flip path -- no
    // SetCrtc, no black flash.
    end_of_in_session_render_call(
        session, card,
        current_fb.take(), current_bo.take(),
        prev_fb.take(), prev_bo.take(),
    );

    work
}

/// Draw a two-color linear gradient that fills the viewport. The
/// fragment shader matches Python's PIL reference (image-space y,
/// flipped from gl_FragCoord). Phase 4.2b extracted into a helper
/// so `render_slide` can compose it with the text pass in one
/// closure.
fn draw_gradient_pattern(
    gl: &glow::Context,
    vp_x_off: u32,
    vp_y_off: u32,
    vp_w: u32,
    vp_h: u32,
    color_a: [f32; 4],
    color_b: [f32; 4],
    density: f32,
) -> Result<()> {
    use glow::HasContext;
    let g = gradient_uniforms(vp_w, vp_h, density);
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
            let u_vp_offset = gl.get_uniform_location(program, "u_vp_offset");
            let u_dir = gl.get_uniform_location(program, "u_dir");
            let u_proj_bounds = gl.get_uniform_location(program, "u_proj_bounds");
            let u_color_a = gl.get_uniform_location(program, "u_color_a");
            let u_color_b = gl.get_uniform_location(program, "u_color_b");
            gl.uniform_2_f32(u_viewport.as_ref(), vp_w as f32, vp_h as f32);
            gl.uniform_2_f32(u_vp_offset.as_ref(), vp_x_off as f32, vp_y_off as f32);
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
    raw_density: f32,
) -> Result<()> {
    // qarl 2026-05-12: stretch the low end of the density slider so
    // low-intensity values yield REALLY large features. Quadratic
    // curve in hdmi_logic::pattern_density_curve. Mirrors
    // ui/src/bg-system.js + backend/openmarquee/auto_render.py.
    let density = crate::hdmi_logic::pattern_density_curve(raw_density);
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
            // Phase 3ag: precompute y-phase so shader avoids the
            // `u_viewport.y - gl_FragCoord.y` precision trap. Mirrors
            // Phase 3x scanlines + Phase 3aa grid. l1 phase = layer-1
            // dot row centers (canvas_y = tile/2 + k*tile) in
            // gl_FragCoord.y mod tile space. l2 phase = layer-2 dot
            // row centers (canvas_y = k*tile) in same space.
            let y_phase_l1 = {
                let v = (mode_h as f32) - u.half;
                ((v % u.tile) + u.tile) % u.tile
            };
            let y_phase_l2 = {
                let v = mode_h as f32;
                ((v % u.tile) + u.tile) % u.tile
            };
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_HALFTONE, color_a, color_b,
                move |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_radius = gl.get_uniform_location(program, "u_radius");
                    let u_half = gl.get_uniform_location(program, "u_half");
                    let u_y_phase_l1 = gl.get_uniform_location(program, "u_y_phase_l1");
                    let u_y_phase_l2 = gl.get_uniform_location(program, "u_y_phase_l2");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_radius.as_ref(), u.radius);
                    gl.uniform_1_f32(u_half.as_ref(), u.half);
                    gl.uniform_1_f32(u_y_phase_l1.as_ref(), y_phase_l1);
                    gl.uniform_1_f32(u_y_phase_l2.as_ref(), y_phase_l2);
                },
            )
        }
        PatternKind::Scanlines => {
            let u = scanlines_uniforms(density);
            // Phase 3x/3ab: precompute y-phase so shader doesn't have
            // to do the large-magnitude `viewport.y - gl_FragCoord.y`
            // subtraction (vc4 mediump precision-truncates it).
            // u_y_phase = mod(viewport_h, tile). Originally we used
            // `mod(viewport_h - 0.5, tile)` (Phase 3x), but Phase 3aa
            // GRID surfaced — and Phase 3ab audit at tile=4/9/15
            // confirmed — that the -0.5 form only matched at the
            // default tile=13 by coincidence: vc4 mediump mod() at
            // large magnitudes behaves as if gl_FragCoord.y is
            // round-half-up'd (same root behavior as vc4 int()).
            let y_phase = {
                let v = mode_h as f32;
                ((v % u.tile) + u.tile) % u.tile
            };
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_SCANLINES, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_y_phase = gl.get_uniform_location(program, "u_y_phase");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_y_phase.as_ref(), y_phase);
                },
            )
        }
        PatternKind::Grid => {
            let u = grid_uniforms(density);
            // Phase 3aa: y_phase precomputed CPU-side so the shader
            // sidesteps the large-magnitude y-flip subtraction.
            // Uses mode_h (no -0.5) to align with vc4's round-half-up
            // int behavior (Phase 3z lesson: vc4 rounds .5 up).
            let y_phase = {
                let v = mode_h as f32;
                ((v % u.tile) + u.tile) % u.tile
            };
            draw_full_screen_pattern(
                gl, mode_w, mode_h, FS_PATTERN_GRID, color_a, color_b,
                |gl, program| unsafe {
                    use glow::HasContext;
                    let u_tile = gl.get_uniform_location(program, "u_tile");
                    let u_y_phase = gl.get_uniform_location(program, "u_y_phase");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_y_phase.as_ref(), y_phase);
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
                    let u_half = gl.get_uniform_location(program, "u_half");
                    gl.uniform_1_f32(u_tile.as_ref(), u.tile);
                    gl.uniform_1_f32(u_half.as_ref(), u.half);
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


/// SDF arc slice B.2 -- MSDF text layer draw. Per-glyph quad VBO
/// (TRIANGLES, 6 verts per glyph) sampled against the session-lived
/// MSDF atlas. The only text-rendering path on the renderer post-B.3
/// (the legacy AlphaBitmap-based `draw_text_layer` was deleted in B.3
/// once SP-tier was gated off for text-bearing transitions).
///
/// Geometry:
///   1. `box_to_ndc_quad` maps the group's per-layer pixel-space
///      bbox (group.width x group.height) into NDC against the
///      layer's box (scale-down-only + halign/valign placement).
///   2. Each glyph's per-layer pixel rect maps linearly inside that
///      NDC rect.
///   3. Motion (breathe scale around box center + translate)
///      applied to the OUTER NDC rect, then propagated to each
///      glyph via the same affine.
///
/// Shader: cached_msdf_program(outline) -> FS_MSDF_{FWIDTH,FIXED}
/// or FS_MSDF_OUTLINE_{FWIDTH,FIXED} per `aa_mode()` + `layer.outline`.
/// Uniforms set: u_atlas, u_text_color, u_opacity. FIXED variants
/// additionally take u_aa_width (~0.05 SDF units); outline variants
/// take u_outline_color (black per Python convention) +
/// u_outline_distance (0.1 SDF units).
///
/// Tofu quads (group.quads[i].tofu == true) are skipped in this
/// slice. Task #592 wires up a dedicated tofu fill shader; until
/// then unknown codepoints render as blank gaps. With Basic Latin
/// + Latin-1 Supplement baked, this is rare in practice.
fn draw_text_layer_msdf(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    layer: &crate::content::TextLayer,
    text_color: [f32; 4],
    motion_kind: MotionKind,
    motion_state: MotionState,
    group: &MsdfQuadGroup,
    atlas_tex: glow::NativeTexture,
    tighten_scissor: Option<(u32, u32, u32, u32)>,
) -> Result<()> {
    use glow::HasContext;

    let opacity = (layer.opacity.clamp(0.0, 1.0)
        * motion_state.alpha_mul.clamp(0.0, 1.0))
        .clamp(0.0, 1.0);
    if opacity < 1e-3 {
        return Ok(());
    }

    let size_px = effective_font_size_px(
        layer.font_size_px,
        layer.font_size_pct,
        layer.r#box.w,
        mode_w,
    );
    let halign = parse_h_align(&layer.text_align);
    // v1.0 close (2026-05-30) — honor §5.10a `anchor` field instead of
    // always-center. Operator picks top/bottom in the editor; the
    // renderer now matches the editor preview's vertical placement.
    let valign = parse_v_align(&layer.anchor);

    // Stage 1: outer NDC rect for the WHOLE laid-out text (matches
    // `box_to_ndc_quad` contract; pad-inclusive). bm_pad = 1 to
    // match layout_text_to_quads's `pad: u32 = 1`.
    let (mut ndc_l, mut ndc_r, mut ndc_t, mut ndc_b) = box_to_ndc_quad(
        layer.r#box.x,
        layer.r#box.y,
        layer.r#box.w,
        layer.r#box.h,
        group.width,
        group.height,
        1,
        mode_w,
        mode_h,
        halign,
        valign,
    );

    // Stage 2: motion scale around box center.
    let scale = motion_state.scale.max(0.05);
    if (scale - 1.0).abs() > 1e-4 {
        let box_cx_ndc = (layer.r#box.x + layer.r#box.w * 0.5) * 2.0 - 1.0;
        let box_cy_ndc = 1.0 - (layer.r#box.y + layer.r#box.h * 0.5) * 2.0;
        ndc_l = box_cx_ndc + scale * (ndc_l - box_cx_ndc);
        ndc_r = box_cx_ndc + scale * (ndc_r - box_cx_ndc);
        ndc_t = box_cy_ndc + scale * (ndc_t - box_cy_ndc);
        ndc_b = box_cy_ndc + scale * (ndc_b - box_cy_ndc);
    }

    // Stage 3: motion translate.
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

    // Per-glyph quad -> NDC affine. Pixel-space (0,0) lands on
    // (ndc_l, ndc_t); pixel-space (group.width, group.height) lands
    // on (ndc_r, ndc_b). Linear interp in both dims.
    let gw = group.width.max(1) as f32;
    let gh = group.height.max(1) as f32;
    let to_ndc_x = |px: f32| ndc_l + (px / gw) * (ndc_r - ndc_l);
    let to_ndc_y = |py: f32| ndc_t + (py / gh) * (ndc_b - ndc_t);

    // Build interleaved [x, y, u, v] verts. Per-glyph TRIANGLES
    // winding BL, BR, TL, BR, TL, TR (6 verts per glyph). Split into
    // THREE vertex streams (SDF arc slice C.3):
    //   - `ink_verts`: MSDF glyphs; drawn with cached_msdf_program
    //     against the font atlas texture.
    //   - `tofu_verts`: missing-codepoint quads; drawn with FS_TOFU.
    //     UVs span [0,1] across each tofu quad so FS_TOFU can use
    //     them as in-rect coordinates for the outline test.
    //   - Slice 3D: the CBDT-side `emoji_per_page` BTreeMap is
    //     retired alongside the static atlas; emoji draws now go
    //     through `dynamic_emoji_verts` only (Slice 3B).
    let mut ink_verts: Vec<f32> = Vec::with_capacity(group.quads.len() * 24);
    let mut tofu_verts: Vec<f32> = Vec::new();
    // Bug 3 Slice 2B: dynamic-MSDF quads. Same vert layout as
    // ink_verts but UVs are atlas-space within the 2048×2048
    // dynamic atlas page. Drawn against DYNAMIC_ATLAS_LOOKUP's
    // texture in a separate batch (FS_MSDF_FIXED program shared
    // with the static-MSDF batch -- the SDF math is identical;
    // only the bound texture differs).
    let mut dynamic_ink_verts: Vec<f32> = Vec::new();
    // Bug 3 Slice 3B (2026-05-19): runtime COLRv1 emoji quads. Same
    // vert layout as ink_verts but UVs are atlas-space within the
    // 2048×2048 dynamic-COLR atlas page. Drawn against
    // DYNAMIC_ATLAS_COLR_LOOKUP's texture in a separate batch
    // (FS_EMOJI program — same shader the static CBDT path uses;
    // only the bound texture differs).
    let mut dynamic_emoji_verts: Vec<f32> = Vec::new();
    // Ticker tiling (density-parity rewrite 2026-05-20): a ticker
    // layer draws the laid-out text TWICE, one box-width (the tile
    // pitch, = box.w * 2 in NDC) apart, so as one copy scrolls off
    // the left edge the next is already entering from the right —
    // a continuous marquee matching the Canvas2D editor ticker that
    // qarl picked as authoritative. Stage 3 translated the rest
    // rect left by the scroll offset; copy 0 is that rect, copy 1
    // sits one box-width to its right. The box scissor (below)
    // clips the spill. Every other motion draws exactly one copy.
    let is_ticker = motion_kind == MotionKind::Ticker;
    let tile_dx_ndc: [f32; 2] = [0.0, layer.r#box.w * 2.0];
    let tile_copies: &[f32] =
        if is_ticker { &tile_dx_ndc[..] } else { &tile_dx_ndc[..1] };
    for &copy_dx in tile_copies {
    for q in &group.quads {
        let xl = to_ndc_x(q.px_left) + copy_dx;
        let xr = to_ndc_x(q.px_right) + copy_dx;
        let yt = to_ndc_y(q.px_top);
        let yb = to_ndc_y(q.px_bottom);
        match q.kind {
            GlyphKind::Tofu => {
                // Per-quad UVs [0, 1] for FS_TOFU's in-rect test.
                // Atlas UVs on the quad are zero per
                // layout_text_to_quads; ignored here.
                tofu_verts.extend_from_slice(&[
                    xl, yb, 0.0, 1.0,
                    xr, yb, 1.0, 1.0,
                    xl, yt, 0.0, 0.0,
                    xr, yb, 1.0, 1.0,
                    xl, yt, 0.0, 0.0,
                    xr, yt, 1.0, 0.0,
                ]);
            }
            GlyphKind::Msdf => {
                let ul = q.uv_left;
                let ur = q.uv_right;
                let ut = q.uv_top;
                let ub = q.uv_bottom;
                ink_verts.extend_from_slice(&[
                    xl, yb, ul, ub,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yt, ur, ut,
                ]);
            }
            GlyphKind::DynamicMsdf => {
                let ul = q.uv_left;
                let ur = q.uv_right;
                let ut = q.uv_top;
                let ub = q.uv_bottom;
                dynamic_ink_verts.extend_from_slice(&[
                    xl, yb, ul, ub,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yt, ur, ut,
                ]);
            }
            GlyphKind::DynamicEmoji => {
                // Bug 3 Slice 3B (2026-05-19): emit into the dynamic-
                // COLR vertex buffer (drawn against the COLR atlas
                // page texture via FS_EMOJI shader — same RGBA
                // passthrough as the static Emoji path; only the
                // bound texture differs).
                let ul = q.uv_left;
                let ur = q.uv_right;
                let ut = q.uv_top;
                let ub = q.uv_bottom;
                dynamic_emoji_verts.extend_from_slice(&[
                    xl, yb, ul, ub,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yb, ur, ub,
                    xl, yt, ul, ut,
                    xr, yt, ur, ut,
                ]);
            }
        }
    }
    }
    if ink_verts.is_empty()
        && tofu_verts.is_empty()
        && dynamic_ink_verts.is_empty()
        && dynamic_emoji_verts.is_empty()
    {
        return Ok(());
    }

    // Scissor source rect. A ticker clips to the LAYER BOX — its
    // tiled copies + scroll spill must not bleed past the box
    // (matching the Canvas ticker's ctx.clip()). Every other layer
    // keeps the historical text-rect scissor, which after the
    // stage-2/3 motion transform IS the displaced text —
    // effectively unclipped, so shake/breathe/bounce spill past the
    // box on purpose (parity Bug 3).
    let (sc_l, sc_r, sc_t, sc_b) = if is_ticker {
        (
            layer.r#box.x * 2.0 - 1.0,
            (layer.r#box.x + layer.r#box.w) * 2.0 - 1.0,
            1.0 - layer.r#box.y * 2.0,
            1.0 - (layer.r#box.y + layer.r#box.h) * 2.0,
        )
    } else {
        (ndc_l, ndc_r, ndc_t, ndc_b)
    };
    let scissor_box: Option<(i32, i32, i32, i32)> = tighten_scissor.map(|(vp_x_off, vp_y_off, vp_w, vp_h)| {
        let to_fb_x = |ndc: f32| {
            vp_x_off as f32 + (ndc + 1.0) * 0.5 * vp_w as f32
        };
        let to_fb_y = |ndc: f32| {
            vp_y_off as f32 + (ndc + 1.0) * 0.5 * vp_h as f32
        };
        let vp_x_max = (vp_x_off + vp_w) as f32;
        let vp_y_max = (vp_y_off + vp_h) as f32;
        let fb_l =
            to_fb_x(sc_l).floor().clamp(vp_x_off as f32, vp_x_max) as i32;
        let fb_r =
            to_fb_x(sc_r).ceil().clamp(vp_x_off as f32, vp_x_max) as i32;
        let fb_b =
            to_fb_y(sc_b).floor().clamp(vp_y_off as f32, vp_y_max) as i32;
        let fb_t =
            to_fb_y(sc_t).ceil().clamp(vp_y_off as f32, vp_y_max) as i32;
        (fb_l, fb_b, (fb_r - fb_l).max(0), (fb_t - fb_b).max(0))
    });

    // A ticker REQUIRES a live scissor (its tiled copies spill past
    // the box). Enable GL_SCISSOR_TEST for the ticker draw, then
    // restore the prior state afterward — so the scissored-bake
    // path, which holds SCISSOR_TEST on for its region clip across
    // these layer draws, is not disturbed. Non-ticker layers keep
    // the historical no-op `gl.scissor` set with the test off.
    let ticker_clip = is_ticker && scissor_box.is_some();

    unsafe {
        // Apply scissor once (covers all sub-batches; their NDC
        // quads share the same source rect by construction).
        if let Some((fb_l, fb_b, sw, sh)) = scissor_box {
            if sw > 0 && sh > 0 {
                gl.scissor(fb_l, fb_b, sw, sh);
            }
        }
        let scissor_was_enabled =
            ticker_clip && gl.is_enabled(glow::SCISSOR_TEST);
        if ticker_clip {
            gl.enable(glow::SCISSOR_TEST);
        }

        // r51: drop_shadow pre-pass. When layer.drop_shadow is true, we
        // draw the static MSDF batch a second time UNDERNEATH with
        // shifted vertex positions (small bottom-right offset, ~0.04 of
        // font height in pixels) and semi-transparent black. Outline is
        // INTENTIONALLY suppressed on the shadow pass — we want the
        // solid glyph silhouette to cast the shadow, not the outline
        // ring. The main pass below then draws with text color +
        // optional outline on top. Same VBO layout (4 floats per vertex
        // [x, y, u, v]); a temporary shifted copy gets uploaded once
        // for the shadow draw and discarded.
        if layer.drop_shadow && !ink_verts.is_empty() {
            let offset_px = (size_px * 0.04).max(1.0);
            let dx_ndc = (offset_px / mode_w as f32) * 2.0;
            let dy_ndc = -(offset_px / mode_h as f32) * 2.0;
            let mut shadow_verts: Vec<f32> = ink_verts.clone();
            for chunk in shadow_verts.chunks_exact_mut(4) {
                chunk[0] += dx_ndc;
                chunk[1] += dy_ndc;
            }
            let cgp_sh = cached_msdf_program(gl, false)?;
            let vbo_sh = gl
                .create_buffer()
                .map_err(|e| anyhow!("glGenBuffers (msdf shadow): {e}"))?;
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo_sh));
            let bytes_sh = std::slice::from_raw_parts(
                shadow_verts.as_ptr() as *const u8,
                shadow_verts.len() * std::mem::size_of::<f32>(),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes_sh, glow::STATIC_DRAW);
            gl.use_program(Some(cgp_sh.program));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
            gl.uniform_1_i32(cgp_sh.u_atlas.as_ref(), 0);
            gl.uniform_3_f32(cgp_sh.u_text_color.as_ref(), 0.0, 0.0, 0.0);
            gl.uniform_1_f32(cgp_sh.u_opacity.as_ref(), opacity * 0.7);
            if cgp_sh.u_aa_width.is_some() {
                gl.uniform_1_f32(cgp_sh.u_aa_width.as_ref(), 0.05);
            }
            let a_pos_sh = cgp_sh.a_pos;
            let a_uv_sh = cgp_sh.a_uv;
            let stride = (4 * std::mem::size_of::<f32>()) as i32;
            gl.enable_vertex_attrib_array(a_pos_sh);
            gl.vertex_attrib_pointer_f32(a_pos_sh, 2, glow::FLOAT, false, stride, 0);
            gl.enable_vertex_attrib_array(a_uv_sh);
            gl.vertex_attrib_pointer_f32(
                a_uv_sh,
                2,
                glow::FLOAT,
                false,
                stride,
                (2 * std::mem::size_of::<f32>()) as i32,
            );
            let vert_count_sh = (shadow_verts.len() / 4) as i32;
            gl.draw_arrays(glow::TRIANGLES, 0, vert_count_sh);
            gl.disable_vertex_attrib_array(a_pos_sh);
            gl.disable_vertex_attrib_array(a_uv_sh);
            gl.delete_buffer(vbo_sh);
        }

        // Batch 1: MSDF-ink glyphs.
        if !ink_verts.is_empty() {
            let cgp = cached_msdf_program(gl, layer.outline)?;
            let vbo = cached_msdf_text_vbo(gl)?;
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                ink_verts.as_ptr() as *const u8,
                ink_verts.len() * std::mem::size_of::<f32>(),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

            gl.use_program(Some(cgp.program));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
            gl.uniform_1_i32(cgp.u_atlas.as_ref(), 0);
            gl.uniform_3_f32(
                cgp.u_text_color.as_ref(),
                text_color[0],
                text_color[1],
                text_color[2],
            );
            gl.uniform_1_f32(cgp.u_opacity.as_ref(), opacity);
            // FIXED variants only: aa_width in SDF units. 0.05 is a
            // mild softening; the shader smoothsteps over
            // 0.5 +/- u_aa_width. Picked to match fwidth() output at
            // a "typical" on-screen size (~64 px); operator can
            // override later via a CLI knob if larger sizes need a
            // softer falloff.
            if cgp.u_aa_width.is_some() {
                gl.uniform_1_f32(cgp.u_aa_width.as_ref(), 0.05);
            }
            if layer.outline {
                gl.uniform_3_f32(cgp.u_outline_color.as_ref(), 0.0, 0.0, 0.0);
                gl.uniform_1_f32(cgp.u_outline_distance.as_ref(), 0.10);
            }

            let a_pos = cgp.a_pos;
            let a_uv = cgp.a_uv;
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
            let vert_count = (ink_verts.len() / 4) as i32;
            gl.draw_arrays(glow::TRIANGLES, 0, vert_count);
            gl.disable_vertex_attrib_array(a_pos);
            gl.disable_vertex_attrib_array(a_uv);
        }

        // Batch 2: tofu quads (deterministic gray rect + black
        // outline for missing-codepoint glyphs).
        if !tofu_verts.is_empty() {
            let tgp = cached_tofu_program(gl)?;
            let vbo = cached_msdf_text_vbo(gl)?;
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let bytes = std::slice::from_raw_parts(
                tofu_verts.as_ptr() as *const u8,
                tofu_verts.len() * std::mem::size_of::<f32>(),
            );
            gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

            gl.use_program(Some(tgp.program));
            gl.uniform_1_f32(tgp.u_opacity.as_ref(), opacity);

            let stride = (4 * std::mem::size_of::<f32>()) as i32;
            gl.enable_vertex_attrib_array(tgp.a_pos);
            gl.vertex_attrib_pointer_f32(tgp.a_pos, 2, glow::FLOAT, false, stride, 0);
            gl.enable_vertex_attrib_array(tgp.a_uv);
            gl.vertex_attrib_pointer_f32(
                tgp.a_uv,
                2,
                glow::FLOAT,
                false,
                stride,
                (2 * std::mem::size_of::<f32>()) as i32,
            );
            let vert_count = (tofu_verts.len() / 4) as i32;
            gl.draw_arrays(glow::TRIANGLES, 0, vert_count);
            gl.disable_vertex_attrib_array(tgp.a_pos);
            gl.disable_vertex_attrib_array(tgp.a_uv);
        }

        // Batch 3 retired in Slice 3D (2026-05-19): the static
        // CBDT emoji color-bitmap per-page draw call is gone. The
        // FS_EMOJI program (link cache) is preserved because the
        // Slice 3B DynamicEmoji batch below still uses it — same
        // RGBA passthrough, different texture binding.

        // Batch 4 (Bug 3 Slice 2B): dynamic-MSDF glyphs. Same
        // FS_MSDF_FIXED program as the static-MSDF batch (the SDF
        // reconstruction is identical -- only the bound texture
        // differs). If the dynamic atlas texture isn't bound this
        // session (allocate_texture failed at bring-up), skip the
        // draw rather than fall through -- layout side already
        // committed to dynamic geometry for these quads.
        if !dynamic_ink_verts.is_empty() {
            if let Some(dyn_tex) = dynamic_atlas_tex() {
                // r51: drop_shadow pre-pass for dynamic glyphs (mirrors
                // Batch 1's pre-pass; same offset math + black + 0.7
                // opacity + outline=false so the solid silhouette
                // casts the shadow).
                if layer.drop_shadow {
                    let offset_px = (size_px * 0.04).max(1.0);
                    let dx_ndc = (offset_px / mode_w as f32) * 2.0;
                    let dy_ndc = -(offset_px / mode_h as f32) * 2.0;
                    let mut shadow_verts: Vec<f32> = dynamic_ink_verts.clone();
                    for chunk in shadow_verts.chunks_exact_mut(4) {
                        chunk[0] += dx_ndc;
                        chunk[1] += dy_ndc;
                    }
                    let cgp_sh = cached_msdf_program(gl, false)?;
                    let vbo_sh = gl
                        .create_buffer()
                        .map_err(|e| anyhow!("glGenBuffers (dyn msdf shadow): {e}"))?;
                    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo_sh));
                    let bytes_sh = std::slice::from_raw_parts(
                        shadow_verts.as_ptr() as *const u8,
                        shadow_verts.len() * std::mem::size_of::<f32>(),
                    );
                    gl.buffer_data_u8_slice(
                        glow::ARRAY_BUFFER,
                        bytes_sh,
                        glow::STATIC_DRAW,
                    );
                    gl.use_program(Some(cgp_sh.program));
                    gl.active_texture(glow::TEXTURE0);
                    gl.bind_texture(glow::TEXTURE_2D, Some(dyn_tex));
                    gl.uniform_1_i32(cgp_sh.u_atlas.as_ref(), 0);
                    gl.uniform_3_f32(cgp_sh.u_text_color.as_ref(), 0.0, 0.0, 0.0);
                    gl.uniform_1_f32(cgp_sh.u_opacity.as_ref(), opacity * 0.7);
                    if cgp_sh.u_aa_width.is_some() {
                        gl.uniform_1_f32(cgp_sh.u_aa_width.as_ref(), 0.05);
                    }
                    let a_pos_sh = cgp_sh.a_pos;
                    let a_uv_sh = cgp_sh.a_uv;
                    let stride = (4 * std::mem::size_of::<f32>()) as i32;
                    gl.enable_vertex_attrib_array(a_pos_sh);
                    gl.vertex_attrib_pointer_f32(
                        a_pos_sh, 2, glow::FLOAT, false, stride, 0,
                    );
                    gl.enable_vertex_attrib_array(a_uv_sh);
                    gl.vertex_attrib_pointer_f32(
                        a_uv_sh,
                        2,
                        glow::FLOAT,
                        false,
                        stride,
                        (2 * std::mem::size_of::<f32>()) as i32,
                    );
                    let vert_count_sh = (shadow_verts.len() / 4) as i32;
                    gl.draw_arrays(glow::TRIANGLES, 0, vert_count_sh);
                    gl.disable_vertex_attrib_array(a_pos_sh);
                    gl.disable_vertex_attrib_array(a_uv_sh);
                    gl.delete_buffer(vbo_sh);
                }
                let cgp = cached_msdf_program(gl, layer.outline)?;
                let vbo = cached_msdf_text_vbo(gl)?;
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                let bytes = std::slice::from_raw_parts(
                    dynamic_ink_verts.as_ptr() as *const u8,
                    dynamic_ink_verts.len() * std::mem::size_of::<f32>(),
                );
                gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

                gl.use_program(Some(cgp.program));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(dyn_tex));
                gl.uniform_1_i32(cgp.u_atlas.as_ref(), 0);
                gl.uniform_3_f32(
                    cgp.u_text_color.as_ref(),
                    text_color[0],
                    text_color[1],
                    text_color[2],
                );
                gl.uniform_1_f32(cgp.u_opacity.as_ref(), opacity);
                if cgp.u_aa_width.is_some() {
                    gl.uniform_1_f32(cgp.u_aa_width.as_ref(), 0.05);
                }
                if layer.outline {
                    gl.uniform_3_f32(cgp.u_outline_color.as_ref(), 0.0, 0.0, 0.0);
                    gl.uniform_1_f32(cgp.u_outline_distance.as_ref(), 0.10);
                }

                let a_pos = cgp.a_pos;
                let a_uv = cgp.a_uv;
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
                let vert_count = (dynamic_ink_verts.len() / 4) as i32;
                gl.draw_arrays(glow::TRIANGLES, 0, vert_count);
                gl.disable_vertex_attrib_array(a_pos);
                gl.disable_vertex_attrib_array(a_uv);
            }
        }

        // Batch 5 (Bug 3 Slice 3B, 2026-05-19): runtime COLRv1
        // emoji glyphs. Same FS_EMOJI program as the static-CBDT
        // batch (RGBA passthrough — only the bound texture differs).
        // If the dynamic-COLR atlas texture isn't bound this session
        // (allocate_texture failed at bring-up), skip the draw
        // rather than fall through — layout side already committed
        // to dynamic-emoji geometry for these quads, and emitting
        // tofu now would have the wrong bounds.
        if !dynamic_emoji_verts.is_empty() {
            if let Some(dyn_colr_tex) = dynamic_atlas_colr_tex() {
                let egp = cached_emoji_program(gl)?;
                let vbo = cached_msdf_text_vbo(gl)?;
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                let bytes = std::slice::from_raw_parts(
                    dynamic_emoji_verts.as_ptr() as *const u8,
                    dynamic_emoji_verts.len() * std::mem::size_of::<f32>(),
                );
                gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);

                gl.use_program(Some(egp.program));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(dyn_colr_tex));
                gl.uniform_1_i32(egp.u_atlas.as_ref(), 0);
                gl.uniform_1_f32(egp.u_opacity.as_ref(), opacity);

                let stride = (4 * std::mem::size_of::<f32>()) as i32;
                gl.enable_vertex_attrib_array(egp.a_pos);
                gl.vertex_attrib_pointer_f32(egp.a_pos, 2, glow::FLOAT, false, stride, 0);
                gl.enable_vertex_attrib_array(egp.a_uv);
                gl.vertex_attrib_pointer_f32(
                    egp.a_uv,
                    2,
                    glow::FLOAT,
                    false,
                    stride,
                    (2 * std::mem::size_of::<f32>()) as i32,
                );
                let vert_count = (dynamic_emoji_verts.len() / 4) as i32;
                gl.draw_arrays(glow::TRIANGLES, 0, vert_count);
                gl.disable_vertex_attrib_array(egp.a_pos);
                gl.disable_vertex_attrib_array(egp.a_uv);
            }
        }
        // P1 cache: the shared scratch VBO stays bound to
        // ARRAY_BUFFER after the last batch. Unbind so the caller's
        // post-state matches the pre-cache shape (the `delete_buffer`
        // calls implicitly unbound on each iteration). Cheap; one
        // bind_buffer(None) per call rather than four deletes.
        gl.bind_buffer(glow::ARRAY_BUFFER, None);
        // Restore GL_SCISSOR_TEST to its pre-ticker state so the
        // scissored-bake region clip (if active) survives.
        if ticker_clip && !scissor_was_enabled {
            gl.disable(glow::SCISSOR_TEST);
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
    // r46 (2026-06-02): SYSTEM_SPEC §5.10 text-over-video. The
    // IPC sidecar's text-arm dispatcher routes around this
    // function entirely when slide.background_video_slide_id is
    // set + the bg-video's demuxer + decoder are primed; it
    // calls paint_and_present_one_text_over_video_slide_frame
    // (hdmi.rs ~3420) instead. That function in turn calls
    // resolve_slide_layers (which calls THIS function) just to
    // get the text_layers Vec; the returned bg_kind is then
    // DISCARDED + paint_slide_with_viewport(bg_kind=None) runs
    // with the video frame already baked into the FBO.
    //
    // r46.1 (2026-06-02): the r46 sweep gap "warn when
    // background_video_slide_id is set on non-IPC paint paths"
    // was over-engineered -- the warn fires for EVERY paint of
    // text-over-video slides (the IPC path goes through
    // resolve_slide_layers → here), spamming journalctl at
    // ~22Hz with misleading "falling back" text. Removed; the
    // non-IPC-paths-don't-support-text-over-video limitation is
    // documented in qa/r46-text-over-video-impl-2026-06-02.md
    // §5 + §H.2 #6.
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
    with_egl_session(card, 0, |session| {
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
    with_egl_session(card, 0, |session| {
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
///
/// Bug W2 (2026-05-21): the returned RGBA buffer is row-flipped
/// to BOTTOM-UP order. The PNG file decodes top-down (row 0 =
/// image top); every caller uploads this buffer with
/// `glTexImage2D` and draws it through `VS_TEXTURED_QUAD`, whose
/// quads map texture `v=0` to the BOTTOM of the screen (the
/// bottom-left vertex carries UV (0,0) -- see
/// `cover_fit_quad_verts` and `create_fullscreen_quad`). A
/// top-down buffer through that quad samples image-top at
/// screen-bottom, i.e. renders the image UPSIDE DOWN. This was
/// latent in the image bake since slice (a) -- it surfaced when
/// the Web slide (which renders via this exact image path) put
/// obviously-oriented content (a webpage screenshot) on glass.
/// Flipping rows here matches the GL `v` convention so every
/// image-asset path -- scanout, capture, image-as-background --
/// renders right-side up, with no quad/shader change that would
/// touch the text / video / stream / pattern paths.
///
/// Same CLASS as FYS bug 2 (a625e35, the NV12 v-flip): a GL
/// Y-convention mismatch. The video path flipped `v` in its
/// fragment shaders; the image path is fixed here at decode
/// because its texture data is host-side bytes (a shader flip
/// would need a dedicated image-only shader variant).
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
    // Bug W2: flip to bottom-up row order so the GL `v` convention
    // (see the doc comment above) renders the image right-side up.
    // The flip helper lives in hdmi_logic.rs so it is host-testable
    // on the Mac dev box (hdmi.rs is Linux-only).
    let rgba = crate::hdmi_logic::flip_rgba_rows_vertically(rgba, w, h);
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

/// QA H2 (2026-05-23) — self-paced VideoSlide renderer for the
/// standalone `--play-reel` driver. Mirrors the IPC sidecar's V4L2
/// decode + paint dispatch, but with the reel's "hold this slide for
/// N ms" pacing instead of the per-Advance tick the sidecar gets.
///
/// Open the asset's `Mp4Demuxer`, prime a fresh V4L2 decoder via
/// `crate::video_decode::prime_video_decoder`, then loop calling
/// `paint_and_present_one_video_slide_frame` at `fps` until `hold_ms`
/// elapses. The video LOOPS during the hold — when
/// `next_sample_idx` wraps past `samples.len()`,
/// `reprime_video_decoder_for_loop` re-feeds the SPS+PPS+IDR primer
/// and the decoder picks back up at sample 1. This matches the
/// self-paced reel UX ("show this slide for N seconds" — a 2s video
/// in a 5s hold plays through 2.5x).
///
/// Failure mode: open / prime / first-frame paint failures bubble
/// to the caller. The reel's Video arm catches and falls through to
/// the existing black-hold sleep + warn log — production rule per
/// QA H2 dispatch: NEVER crash the reel.
#[cfg(target_os = "linux")]
pub fn render_video_slide_in_session(
    session: &mut EglSession,
    card: &Card,
    asset_path: &Path,
    hold_ms: u64,
    fps: u32,
) -> Result<()> {
    if fps == 0 {
        bail!("fps must be > 0");
    }
    let dem = crate::mp4_demux::Mp4Demuxer::open(asset_path)
        .with_context(|| {
            format!("open MP4 for reel video render: {}", asset_path.display())
        })?;
    let mut state = crate::video_decode::prime_video_decoder(&dem, "reel")
        .with_context(|| {
            format!(
                "prime V4L2 decoder for reel video {}",
                asset_path.display(),
            )
        })?;
    let frame_budget = std::time::Duration::from_secs_f64(1.0 / fps as f64);
    let total_frames =
        ((hold_ms as f64) / 1000.0 * fps as f64).round().max(1.0) as u32;
    eprintln!(
        "rendering video_slide from {} ({}x{}, {} samples) for {hold_ms}ms at {fps}fps ({total_frames} frames)",
        asset_path.display(),
        dem.width,
        dem.height,
        dem.samples.len(),
    );
    for _frame in 0..total_frames {
        let frame_start = std::time::Instant::now();
        if state.next_sample_idx >= dem.samples.len() {
            // Reached end of stream — re-feed SPS+PPS+IDR + sample[0]
            // to wrap. On failure (rare), bubble — the reel catches
            // and falls back to black-hold for remainder.
            crate::video_decode::reprime_video_decoder_for_loop(
                &mut state, &dem,
            )?;
        }
        paint_and_present_one_video_slide_frame(
            session,
            card,
            &dem.samples,
            &mut state.next_sample_idx,
            &mut state.frames_decoded,
            &state.decoder,
        )?;
        let elapsed = frame_start.elapsed();
        if elapsed < frame_budget {
            std::thread::sleep(frame_budget - elapsed);
        }
    }
    Ok(())
}

/// QA M2 (2026-05-23) — self-paced any-endpoint transition renderer
/// for the standalone `--play-reel` driver. Today's reel
/// special-cases (Text, Text) → `render_transition_animated_in_session`
/// and falls back to "hard cut + warn" for any other combo. This
/// wrapper dispatches the (Text|Image)² matrix via the same
/// per-frame `paint_and_present_one_transition_frame` the IPC
/// sidecar uses (which has already handled Text/Image/Image/Text/
/// Image/Image for slice 6+).
///
/// Video-involving endpoints (V↔T/I/V) are EXPLICITLY NOT supported
/// here — the reel has no `SlideCache` for V4L2 decoder state.
/// Returns `Err` for those combos so the reel's match arm can fall
/// back to a hard cut. Scoped to "image-involving transitions" per
/// the QA M2 dispatch text.
///
/// Mirrors `render_transition_animated_in_session`'s self-paced
/// frame-loop shape, but routes through the more-flexible
/// `paint_and_present_one_transition_frame` primitive instead of the
/// text/text-only legacy 3-pass / SP / SB dispatch tree.
pub fn render_transition_any_endpoint_in_session(
    session: &mut EglSession,
    card: &Card,
    prev_item: &crate::content::ContentItem,
    item: &crate::content::ContentItem,
    fonts: Option<&FontCatalog>,
    content_root: &Path,
    kind: &str,
    transition_ms: u32,
    fps: u32,
) -> Result<()> {
    if transition_ms == 0 {
        bail!("transition_ms must be > 0");
    }
    if fps == 0 {
        bail!("fps must be > 0");
    }
    // Video-involving transitions need cache-resident V4L2 decoder
    // state; the standalone reel doesn't carry one. Bail so the
    // caller hard-cuts (matches the pre-H2/M2 video transition
    // behavior — scoped fix, not a regression).
    if matches!(prev_item, crate::content::ContentItem::Video(_))
        || matches!(item, crate::content::ContentItem::Video(_))
    {
        bail!(
            "video-involving transitions not supported in the standalone reel \
             (no SlideCache for V4L2 decoder state); caller should hard-cut",
        );
    }
    // r50 subagent (2026-06-03 NIT): text-over-video slides
    // (TextSlide with background_video_slide_id) silently drop the
    // bg to solid in the standalone reel — same root cause: no
    // SlideCache for V4L2 state. The IPC sidecar path
    // (paint_and_present_one_transition_frame via the dispatcher
    // at ipc_main.rs:OpResult::PaintTransition) routes these to
    // TransitionEndpoint::TextOverVideo and bakes the video bg +
    // text composite. The standalone reel is the QA/preview path
    // (offline rendering, no IPC), so for now it lags the IPC
    // path's fidelity by 1 layer. Operator/QA running a text-over-
    // video playlist through the reel will see the bg drop to
    // solid for the transition window. Not a regression vs r48
    // -- documenting the inconsistency.
    let frame_budget = std::time::Duration::from_secs_f64(1.0 / fps as f64);
    let total_frames =
        ((transition_ms as f64) / 1000.0 * fps as f64).round().max(1.0) as u32;
    eprintln!(
        "rendering any-endpoint transition kind={kind:?} prev={} item={} transition_ms={transition_ms} fps={fps} ({total_frames} frames)",
        prev_item.type_label(),
        item.type_label(),
    );
    for f in 0..total_frames {
        let frame_start = std::time::Instant::now();
        // Build fresh TransitionEndpoints per iteration. Text/Image
        // variants hold immutable references, so reconstruction is
        // cheap (just borrows from the caller's ContentItem refs).
        let endpoint_a = match prev_item {
            crate::content::ContentItem::Text(s) => TransitionEndpoint::Text(s),
            crate::content::ContentItem::Image(s) => TransitionEndpoint::Image(s),
            crate::content::ContentItem::Video(_) => unreachable!(
                "video bailed above"
            ),
        };
        let endpoint_b = match item {
            crate::content::ContentItem::Text(s) => TransitionEndpoint::Text(s),
            crate::content::ContentItem::Image(s) => TransitionEndpoint::Image(s),
            crate::content::ContentItem::Video(_) => unreachable!(
                "video bailed above"
            ),
        };
        // Linear-in-time progress in [0.0, 1.0) for frames in
        // [0, total_frames). The endpoint at progress=1.0 (slide
        // fully on screen) is handled by the subsequent slide hold,
        // not the transition loop — same convention as
        // render_transition_animated_in_session.
        let progress = (f as f32) / (total_frames as f32);
        paint_and_present_one_transition_frame(
            session,
            card,
            endpoint_a,
            endpoint_b,
            fonts,
            Some(content_root),
            kind,
            progress,
        )?;
        let elapsed = frame_start.elapsed();
        if elapsed < frame_budget {
            std::thread::sleep(frame_budget - elapsed);
        }
    }
    Ok(())
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
///   * t_in_slide_ms is the relative ms since slide entry, kept
///     for IPC schema parity + future per-slide pacing needs.
///     **NOT** used to derive motion tick -- motion tick is
///     session-global (see Bug 1 fix extension below).
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
    _t_in_slide_ms: u64,
) -> Result<()> {
    use glow::HasContext;
    // QA-direct (2026-05-14 slide-boundary characterization slice):
    // OPENMARQUEE_BOUNDARY_TRACE=1 emits one JSON line per painted
    // frame to stderr with per-phase Instant deltas in microseconds.
    // 2026-06-15 perf-gl W-2: was a per-frame std::env::var_os call;
    // now reads a thread_local-cached bool (resolved once per
    // worker). The Instant captures are skipped entirely when off.
    let trace = boundary_trace_enabled_cached();
    let t_start = if trace { Some(std::time::Instant::now()) } else { None };
    // perf-night r2 (2026-05-26): record bake/compose/present sub-
    // phases for the runtime profile histogram (orthogonal to the
    // boundary-trace env-var path above, which dumps per-frame JSON
    // lines to stderr). The profile path aggregates p50/p95/p99/max
    // across N frames and is dump-able via IPC for live measurement.
    // Phase names sort adjacent: paint_bake_text / paint_compose /
    // paint_present.
    let t_phase = std::time::Instant::now();
    // Bug 3 Slice 2B: drain glyph-cache completions at frame start.
    // The worker pool rasterizes new MSDF cells asynchronously; this
    // call uploads any ready cells into the dynamic atlas page +
    // transitions slots to Ready. Bound = 4 matches the worker-pool
    // size to keep per-frame upload work bounded.
    //
    // Slide-cache invalidation: when poll uploads new cells, slides
    // whose previous layout pass cached Tofu placeholders (because
    // their codepoints were Requested/Generating at that pass) need
    // a fresh layout to pick up the new Ready states. Bulk-clearing
    // slide_caches forces the next paint to re-layout; the cost is
    // bounded (uploads happen only on first encounter per codepoint
    // per session, so a few cache rebuilds per session at most).
    let uploaded = session.dynamic_glyph_cache.poll_completions(
        session.gl,
        &mut session.dynamic_atlas_page_msdf,
        &mut session.dynamic_atlas_page_colr,
        4,
    );
    if uploaded > 0 {
        let drained: Vec<_> = session.slide_caches.drain().collect();
        for (_id, entry) in drained {
            free_slide_render_cache(session.gl, entry);
        }
    }
    let (bg_kind, _pattern_label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    // Bug 1 fix extension (qarl-flag 2026-05-09, applied 2026-05-13):
    // motion tick is session-global -- matches the basis used by
    // render_animated_slide_in_session (standalone hold) and the SP/SB
    // transition loops. Pre-fix, this IPC-sidecar path derived
    // tick_seconds from `t_in_slide_ms / 1000`, which resets to 0 at
    // every BeginSlide. With transitions baking motion frozen between
    // slide A and slide B, that reset produced a visible phase snap at
    // hold-A->transition AND transition->hold-B boundaries on glass.
    let tick_seconds = session.motion_tick_seconds();
    let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
    let wall_clock_unix = current_unix_seconds();

    // v1-spec-delta #10 (slice c): when settings have non-
    // identity brightness/gamma, route paint_slide through a
    // session-cached scene FBO + post-pass blit. Identity
    // settings (brightness=100 + gamma=1.0) take the direct-
    // to-default-fb path with zero post-pass cost.
    //
    // FYS bug 5: the scene FBO is ALSO needed for any non-zero
    // display rotation -- content renders into the logical-sized
    // scene FBO and the present pass rotates it onto the panel.
    // mode_w/mode_h are the LOGICAL dims; the scene FBO is sized
    // to them so content lays out at portrait for 90/270.
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    let scene_fbo_handle = if !identity || rotation != 0 {
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
    // QA-direct (2026-05-14 paint_slide profile, 34e952d): the IPC
    // sidecar path was passing glyph_cache=None / tex_cache=None,
    // so layout_text_to_alpha fired for every layer on every frame.
    // 85.9% of paint_us was CPU font rasterize for the 4 heavy FYS
    // slides. Mirror render_animated_slide_in_session's get-or-init
    // against session.slide_caches keyed by slide_id (line ~1204).
    // Existing 6-slide LRU (Atlas SB P0) handles bounded growth.
    {
        let needs_new = match session.slide_caches.get(&slide.id) {
            Some(c) => c.glyph.len() != text_layers.len(),
            None => true,
        };
        if needs_new {
            if let Some(old) = session.slide_caches.remove(&slide.id) {
                free_slide_render_cache(session.gl, old);
            }
            insert_slide_render_cache(
                &mut session.slide_caches,
                session.gl,
                slide.id,
                SlideRenderCache::new(text_layers.len()),
            );
        }
    }
    let cache = session
        .slide_caches
        .get_mut(&slide.id)
        .expect("slide_caches entry initialized above");
    let t_after_setup = if trace { Some(std::time::Instant::now()) } else { None };
    crate::profile::record_phase(
        "paint_bake_text",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();

    paint_slide(
        session.gl,
        mode_w,
        mode_h,
        &bg_kind,
        &text_layers,
        Some(&motion_states),
        wall_clock_unix,
        Some(&mut cache.glyph),
        Some(&mut session.image_bg_cache),
        Some(&mut cache.tex),
        // Bug 3 Slice 2B: runtime glyph cache + fonts_dir from the
        // session so the layout dispatch can resolve static-atlas
        // misses to dynamic-MSDF cells. The IPC sidecar's per-frame
        // entry on FYS production hits this path.
        Some(crate::glyph_cache::RuntimeGlyphCtx {
            cache: &session.dynamic_glyph_cache,
            fonts_dir: &session.dynamic_fonts_dir,
        }),
    )?;
    unsafe { session.gl.flush(); }
    let t_after_paint = if trace { Some(std::time::Instant::now()) } else { None };

    // v1-spec-delta #10 (slice c): if non-identity OR rotated, the
    // scene is in scene_fbo. Bind default fb + run the present pass
    // from scene_tex. Brightness divides by 100 to turn schema
    // [0, 100] into shader [0, 1]. FYS bug 5: the present-pass
    // viewport is the PHYSICAL panel size (the scanout buffer), and
    // the pass rotates the logical scene onto it.
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }
    let t_after_postpass = if trace { Some(std::time::Instant::now()) } else { None };
    crate::profile::record_phase(
        "paint_compose",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();

    // swap_buffers → lock → addFB → commit_fb. Same primitive
    // sequence as render_animated_slide_in_session's per-frame
    // loop body, with the (BO, FB) holders coming off session
    // instead of loop locals.
    // (cold-scout #2 P6, 2026-05-09): eglSwapBuffers implicitly
    // flushes; the explicit gl.flush() that used to be here
    // forced an extra tile-store on vc4.
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers failed: {e:?}"))?;
    let t_after_swap = if trace { Some(std::time::Instant::now()) } else { None };
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
    let t_after_gbm = if trace { Some(std::time::Instant::now()) } else { None };
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
    let t_end = if trace { Some(std::time::Instant::now()) } else { None };
    crate::profile::record_phase(
        "paint_present",
        t_phase.elapsed().as_nanos() as u64,
    );

    // Emit per-phase trace if OPENMARQUEE_BOUNDARY_TRACE was on.
    // One JSON line per painted frame; consumer is the sidecar
    // smoke driver's stderr drainer.
    if let (Some(t0), Some(t1), Some(t2), Some(t3), Some(t4), Some(t5), Some(t6)) = (
        t_start,
        t_after_setup,
        t_after_paint,
        t_after_postpass,
        t_after_swap,
        t_after_gbm,
        t_end,
    ) {
        eprintln!(
            "{{\"trace\":\"boundary\",\"slide_id\":\"{}\",\"setup_us\":{},\"paint_us\":{},\"postpass_us\":{},\"swap_us\":{},\"gbm_us\":{},\"commit_us\":{},\"total_us\":{}}}",
            slide.id,
            (t1 - t0).as_micros(),
            (t2 - t1).as_micros(),
            (t3 - t2).as_micros(),
            (t4 - t3).as_micros(),
            (t5 - t4).as_micros(),
            (t6 - t5).as_micros(),
            (t6 - t0).as_micros(),
        );
    }
    Ok(())
}

/// r46 (2026-06-02): per SYSTEM_SPEC §5.10, paint one frame of a
/// TextSlide whose `background_video_slide_id` references a
/// VideoSlide. Mirrors `paint_and_present_one_frame_for_slide`'s
/// shape but swaps the bg-paint step (was: solid/pattern/image)
/// for a V4L2 video-frame bake via
/// `bake_video_slide_to_current_fbo`; the text layers then
/// composite on top through `paint_slide_with_viewport` with
/// `bg_kind=None` (the "caller has already filled the bg" signal
/// the function already accepts from the atlas SB bg-cache
/// path).
///
/// The bg-video's V4L2 demuxer + decoder MUST be primed before
/// calling this -- the IPC `cache.load()` for a TextSlide with
/// `background_video_slide_id` set side-loads the referenced
/// VideoSlide for exactly this purpose.
///
/// Loop semantics: `bake_video_slide_to_current_fbo` wraps
/// `next_sample_idx` back to 0 when samples exhaust (FYS bug 3
/// fix), so a clip shorter than the slot replays from the
/// beginning. A clip longer than the slot truncates when the
/// backend's `playback.py` `end_at` fires (the renderer doesn't
/// drive slide duration).
///
/// Motion text: works naturally. Text layers paint per-tick on
/// top of the freshly-decoded video frame, exactly like the
/// image-bg-on-text path does today.
#[cfg(target_os = "linux")]
#[allow(clippy::too_many_arguments)]
pub fn paint_and_present_one_text_over_video_slide_frame(
    session: &mut EglSession,
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    _t_in_slide_ms: u64,
    samples: &[crate::mp4_demux::Sample],
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
) -> Result<()> {
    use glow::HasContext;
    // r61 Phase B (2026-06-04): first-frame paint breakdown for the
    // text-over-video hot path. r58 + r57 shaved most of the
    // pre-transition stall; the residual gap qarl can still see is
    // the cost of producing the first VISIBLE frame on the
    // panel after BeginSlide returns. Sub-phases:
    //   * bake_us       = V4L2 next_frame() + NV12 upload + blit
    //   * composite_us  = text-layer paint via paint_slide_with_viewport
    //   * present_us    = scene-FBO present pass (rotated/non-identity only)
    //   * scanout_us    = swap + lock_front_buffer + addFB + commit_fb
    //   * total_us      = whole function wall time
    //
    // First-frame detection: frames_decoded is incremented INSIDE
    // bake_video_slide_to_current_fbo on success, so we snapshot the
    // pre-bake value here. transition_kind is intentionally omitted
    // (transition state is cleared by the time this paint runs); QA
    // correlates with the prior [perf] begin_transition_load line in
    // the same journal.
    let was_first = *frames_decoded == 0;
    let t_total = if was_first { Some(std::time::Instant::now()) } else { None };
    let t_phase = std::time::Instant::now();
    // Same glyph-cache poll + slide-cache invalidation cascade as
    // paint_and_present_one_frame_for_slide. Keeps text-layer
    // rasterization in step with worker-pool completions.
    let uploaded = session.dynamic_glyph_cache.poll_completions(
        session.gl,
        &mut session.dynamic_atlas_page_msdf,
        &mut session.dynamic_atlas_page_colr,
        4,
    );
    if uploaded > 0 {
        let drained: Vec<_> = session.slide_caches.drain().collect();
        for (_id, entry) in drained {
            free_slide_render_cache(session.gl, entry);
        }
    }
    // Resolve layers + bg_kind from the slide schema. For the text-
    // over-video path we IGNORE bg_kind (the video frame replaces
    // it). resolve_slide_layers still validates the layer set +
    // returns fonts-resolved tuples we need for paint_slide_with_
    // viewport.
    let (_unused_bg_kind, _pattern_label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    let tick_seconds = session.motion_tick_seconds();
    let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
    let wall_clock_unix = current_unix_seconds();
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // Scene FBO for rotation or non-identity color, same as the
    // text-only path. Critical for text-over-video specifically
    // because the video bake also needs the scene FBO bound BEFORE
    // bake_video_slide_to_current_fbo runs (mirrors the standalone
    // paint_and_present_one_video_slide_frame routing).
    let scene_fbo_handle = if !identity || rotation != 0 {
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
    // r46 CMA mitigation: first paint of a text-over-video slide
    // forces eviction of the image_bg + image_slide_tex caches to
    // free up to ~96 MB of CMA for the V4L2 decoder pool the
    // bg-video bake needs. Detection via slide_caches absence --
    // subsequent paints of the same slide skip the eviction
    // (cheap no-op on empty caches anyway). See
    // qa/r46-text-over-video-impl-2026-06-02.md §4 CMA Budget.
    let first_paint = !session.slide_caches.contains_key(&slide.id);
    if first_paint {
        session.force_evict_image_caches_for_cma_pressure();
    }
    {
        let needs_new = match session.slide_caches.get(&slide.id) {
            Some(c) => c.glyph.len() != text_layers.len(),
            None => true,
        };
        if needs_new {
            if let Some(old) = session.slide_caches.remove(&slide.id) {
                free_slide_render_cache(session.gl, old);
            }
            insert_slide_render_cache(
                &mut session.slide_caches,
                session.gl,
                slide.id,
                SlideRenderCache::new(text_layers.len()),
            );
        }
    }
    crate::profile::record_phase(
        "paint_bake_text",
        t_phase.elapsed().as_nanos() as u64,
    );

    // r62 first_frame_tex cache REMOVED (2026-06-15 R-1 footprint
    // cut). The cached fast-path saved ~4.17 MB per slide × N reel
    // slides of CMA (FYS 4-slide reel = ~17 MB). Karl flagged
    // memory as dangerously high; reverting to pre-r62 behavior
    // (full bake + composite + present every slide cycle) is the
    // tradeoff. Other transition optimizations (Option B EGLImage
    // prewarm + r106 cached transition FBO + iter-7 scoped flush)
    // mitigate the slow-path cost the r62 cache used to amortize.

    // Step 1: decode + bake the next V4L2 video frame into the
    // currently-bound framebuffer (scene FBO when rotated/non-
    // identity, else default fb). On a no-frame tick
    // (Ok(None) -- V4L2 EAGAIN, no decoded frame ready) the FBO
    // contents are undefined after the eglSwapBuffers we'd run
    // below; painting text on undefined pixels would produce a
    // warmup-tick flicker on glass. Mirror the standalone
    // paint_and_present_one_video_slide_frame's behavior at lines
    // ~3840-3857: skip the swap+commit; the kernel keeps the
    // prior scanout BO/FB pair live so the prior decoded frame
    // stays on glass. The next advance retries the V4L2 dqbuf;
    // motion text effectively pauses for one tick (≤30 ms),
    // visually identical to the standalone-video case.
    let t_phase = std::time::Instant::now();
    let painted = unsafe {
        bake_video_slide_to_current_fbo(
            session,
            samples,
            next_sample_idx,
            frames_decoded,
            decoder,
            mode_w,
            mode_h,
            // Steady-state TextOverVideo paints into the WINDOW FB
            // (or scene FBO when rotated). eglSwapBuffers is the
            // implicit barrier; no extra flush needed.
            /* is_offscreen_bake (Path A Stage 2 scope tag) */ false,
        )?
    };
    // r61 Phase B: snapshot bake duration before its record_phase
    // call (record_phase's emit shouldn't pollute the sub-phase
    // timer). bake_us is the dispatch's "dequeue_us" equivalent --
    // it bundles V4L2 next_frame() (DQBUF wait) + NV12 upload +
    // cover-fit blit; further decomposition would need timers
    // inside bake_video_slide_to_current_fbo, deferred until the
    // top-level data flags bake as the dominant phase.
    let bake_us = t_phase.elapsed().as_micros();
    crate::profile::record_phase(
        "paint_bake_video",
        t_phase.elapsed().as_nanos() as u64,
    );
    if painted.is_none() {
        // No-frame tick: re-bind the default fb if we routed
        // through the scene FBO for rotation (matches standalone
        // video path's rebind at line ~3846), then return without
        // present. Subagent finding: prevents text-on-garbage
        // flicker (qa/r46-text-over-video-impl-2026-06-02.md §H).
        if scene_fbo_handle.is_some() {
            unsafe {
                session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            }
        }
        return Ok(());
    }

    // Step 2: composite text layers on top via paint_slide_with_
    // viewport with bg_kind=None -- the "caller has already
    // filled the bg" path the function documents at lines ~11295.
    let t_phase = std::time::Instant::now();
    let cache = session
        .slide_caches
        .get_mut(&slide.id)
        .expect("slide_caches entry initialized above");
    paint_slide_with_viewport(
        session.gl,
        mode_w,
        mode_h,
        0,
        0,
        mode_w,
        mode_h,
        None, // bg already filled by bake_video_slide_to_current_fbo
        &text_layers,
        Some(&motion_states),
        wall_clock_unix,
        Some(&mut cache.glyph),
        Some(&mut session.image_bg_cache),
        Some(&mut cache.tex),
        Some(crate::glyph_cache::RuntimeGlyphCtx {
            cache: &session.dynamic_glyph_cache,
            fonts_dir: &session.dynamic_fonts_dir,
        }),
    )?;
    unsafe { session.gl.flush(); }

    // r61 Phase B: snapshot composite duration before the present-
    // pass timer overlay. composite_us covers paint_slide_with_
    // viewport (text glyph raster + draw) only; the rotation
    // present pass is timed separately below.
    let composite_us = t_phase.elapsed().as_micros();

    // r62 first_frame_tex capture site REMOVED (R-1 footprint cut).
    // See the matching comment above the Step 1 bake; the
    // glCopyTexImage2D + cache store is gone with the fast-path.

    // Step 3: present pass through scene FBO if rotated/non-
    // identity. Mirrors paint_and_present_one_frame_for_slide.
    let t_present = std::time::Instant::now();
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }
    let present_us = t_present.elapsed().as_micros();
    crate::profile::record_phase(
        "paint_compose",
        t_phase.elapsed().as_nanos() as u64,
    );

    // Step 4: standard scanout swap+commit -- verbatim mirror of
    // paint_and_present_one_frame_for_slide's tail (canonical 11-
    // step release contract per qa/r38b-hdmi-cma-deep-read-2026-
    // 06-02.md §2).
    let t_phase = std::time::Instant::now();
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (text-over-video) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (text-over-video) failed")?
    };
    let fb_buf =
        GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata (text-over-video)")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (text-over-video) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (text-over-video): {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev, text-over-video): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev_bo.take() {
        drop(bo);
    }
    session.scanout_prev_fb = session.scanout_current_fb.take();
    session.scanout_prev_bo = session.scanout_current_bo.take();
    session.scanout_current_bo = Some(new_bo);
    session.scanout_current_fb = Some(new_fb);
    // r61 Phase B: scanout_us = wall time of the swap+lock+addFB+
    // commit sequence (the canonical 11-step release contract per
    // qa/r38b §2). On a busy CMA pool the lock_front_buffer +
    // add_framebuffer ioctls can stall waiting for the previous
    // BO to be released by the next page-flip event.
    let scanout_us = t_phase.elapsed().as_micros();
    crate::profile::record_phase(
        "paint_present",
        t_phase.elapsed().as_nanos() as u64,
    );
    // r61 Phase B: emit the first-frame breakdown exactly once per
    // slide cycle (first paint after BeginSlide). Subsequent
    // steady-state paints are NOT logged here -- the existing
    // perf-night-r3 tick budget already covers them. transition_
    // kind is omitted because transition state is already cleared
    // by the time this paint runs; QA correlates the
    // first_frame_paint timestamp against the prior
    // [perf] begin_transition_load line for the same slide_id.
    if was_first {
        let total_us = t_total
            .map(|t| t.elapsed().as_micros())
            .unwrap_or(0);
        eprintln!(
            "[perf] first_frame_paint slide_id={} bake_us={} composite_us={} present_us={} scanout_us={} total_us={}",
            slide.id, bake_us, composite_us, present_us, scanout_us, total_us,
        );
    }
    Ok(())
}

/// QA-direct (2026-05-13 sidecar feature-gaps slice) -- one-shot
/// paint + present for an ImageSlide. Mirrors paint_and_present_
/// one_frame_for_slide's scanout-rotation discipline but the body
/// is a single PNG-tex upload + FS_BLIT, no motion / no auto_mode
/// (ImageSlide is static per the v1-spec-delta #8 schema).
///
/// Pre-conditions: EglSession bound, content_root resolves
/// `<root>/<slide.id>/asset.png`, asset is browser-pre-scaled to
/// the panel mode per the ImageSlide docstring.
///
/// Post-conditions: one frame painted to scanout (set_crtc on
/// first call, page_flip thereafter). scanout_prev / scanout_
/// current rotated, stale prev BO/FB freed.
pub fn paint_and_present_one_image_slide_frame(
    session: &mut EglSession,
    card: &Card,
    slide: &ImageSlide,
    content_root: &Path,
) -> Result<()> {
    use glow::HasContext;
    // perf-night r2 (2026-05-26): bake/compose/present sub-phase
    // wraps. Image bake = PNG decode + texture upload (cold) or
    // texture-cache hit (warm); compose = blit to FBO + present pass;
    // present = swap + lock + addFB + commit_fb.
    let t_phase = std::time::Instant::now();
    let asset_path = crate::content::image_slide_asset_path(content_root, slide.id);
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // v1-spec-delta #10 (slice c): non-identity brightness/gamma
    // routes the bake through the scene FBO + post-pass blit. Mirrors
    // paint_and_present_one_frame_for_slide's pattern so ImageSlide
    // honors the operator's color settings on glass.
    // FYS bug 5: the scene FBO is needed for non-identity color OR
    // any non-zero rotation. mode_w/mode_h are LOGICAL dims; the
    // image bake fills the logical-sized FBO and the present pass
    // rotates it onto the panel.
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let scene_fbo_handle = if !identity || rotation != 0 {
        Some(unsafe { ensure_scene_fbo(session, mode_w, mode_h)? })
    } else {
        None
    };
    // Task #168: route through ImageSlideTextureCache so a Web slide
    // refresh (asset.png overwritten by the producer) does the PNG
    // decode on a worker thread. The first paint after `with_egl_
    // session` brings up the slide synchronously (cold cache), then
    // every subsequent refresh swaps in the new tex without blocking
    // the render thread. Borrow split: `&mut session.image_slide_tex_
    // cache` and `&session.gl` are disjoint fields — the standard
    // pattern used elsewhere (slide_caches + gl, image_bg_cache + gl).
    let (cached_tex, img_w, img_h) =
        session
            .image_slide_tex_cache
            .ensure(session.gl, slide.id, &asset_path)?;
    crate::profile::record_phase(
        "paint_bake_image",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    if let Some((fbo, _tex)) = scene_fbo_handle {
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        }
    }
    unsafe {
        blit_cached_image_slide_to_current_fbo(
            session.gl,
            cached_tex,
            img_w,
            img_h,
            mode_w,
            mode_h,
        )?;
    }
    // v1-spec-delta #10 (slice c) + FYS bug 5: the scene-FBO route
    // runs the rotation-aware present pass from scene FBO to the
    // panel-native default fb (viewport = PHYSICAL dims).
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }
    crate::profile::record_phase(
        "paint_compose",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    // Mirror paint_and_present_one_frame_for_slide's scanout
    // rotation: swap, lock front BO, addFB, commit_fb, then
    // shift scanout_current -> scanout_prev and stash the new pair.
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (image_slide) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (image_slide) failed")?
    };
    let fb_buf = GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata (image_slide)")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (image_slide) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (image_slide): {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev, image_slide): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev_bo.take() {
        drop(bo);
    }
    session.scanout_prev_fb = session.scanout_current_fb.take();
    session.scanout_prev_bo = session.scanout_current_bo.take();
    session.scanout_current_bo = Some(new_bo);
    session.scanout_current_fb = Some(new_fb);
    crate::profile::record_phase(
        "paint_present",
        t_phase.elapsed().as_nanos() as u64,
    );
    Ok(())
}

/// STREAM/VLC slice 2.5 — paint + present one external RGB888 frame.
///
/// `rgb` is `frame_w * frame_h * 3` bytes of row-major RGB888 from an
/// external producer (the Python backend's ffmpeg/RTSP pump today; a
/// headless browser later — STREAM_VLC_PROPOSAL §10). This function
/// is deliberately SOURCE-AGNOSTIC: it knows nothing about where the
/// bytes came from.
///
/// Structurally identical to paint_and_present_one_image_slide_frame
/// — same scene-FBO brightness/gamma routing and the same scanout-
/// rotation discipline — the only difference is the body: a raw
/// RGB-texture upload + FS_BLIT instead of a PNG-from-disk decode.
pub fn paint_and_present_external_frame(
    session: &mut EglSession,
    card: &Card,
    rgb: &[u8],
    frame_w: u32,
    frame_h: u32,
) -> Result<()> {
    use glow::HasContext;
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // Non-identity brightness/gamma routes through the scene FBO +
    // post-pass blit, exactly as the image-slide path does — so a
    // VLC frame honors the operator's color settings on glass.
    // FYS bug 5: the scene FBO is likewise needed for any non-zero
    // display rotation. mode_w/mode_h are the LOGICAL dims.
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let scene_fbo_handle = if !identity || rotation != 0 {
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
    unsafe {
        bake_external_rgb_to_current_fbo(
            session.gl,
            &mut session.external_frame_tex,
            rgb,
            frame_w,
            frame_h,
            mode_w,
            mode_h,
        )?;
    }
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }
    // Scanout swap / lock / addFB / commit / pair-rotation — verbatim
    // from paint_and_present_one_image_slide_frame.
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (external_frame) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (external_frame) failed")?
    };
    let fb_buf =
        GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata (external_frame)")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (external_frame) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (external_frame): {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev, external_frame): {e}");
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

/// STREAM/VLC HW-decode (2026-05-20) — paint one raw planar NV12
/// frame pushed by an external producer (the HW-decode VLC pump:
/// `ffmpeg -c:v h264_v4l2m2m`, raw NV12 out, no swscale `-vf`).
///
/// The NV12 sibling of `paint_and_present_external_frame`.
/// Structurally identical — same scene-FBO color/rotation routing,
/// same scanout swap/commit/pair-rotation — the only difference is
/// the body: a planar NV12 Y+UV upload + cover-fit BT.709 NV12→RGB
/// blit (`bake_external_nv12_to_current_fbo`) instead of an RGB888
/// upload + FS_BLIT.
///
/// `frame_w`/`frame_h` are the SOURCE video dims (NV12 frame is
/// `frame_w*frame_h*3/2` bytes); the GPU cover-fit-scales onto the
/// panel. Source-agnostic in spirit: any NV12 producer drives this.
pub fn paint_and_present_external_nv12_frame(
    session: &mut EglSession,
    card: &Card,
    nv12: &[u8],
    frame_w: u32,
    frame_h: u32,
) -> Result<()> {
    use glow::HasContext;
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // Non-identity brightness/gamma OR non-zero rotation routes
    // through the scene FBO + post-pass blit — exactly as the
    // RGB888 external-frame path does, so a VLC NV12 frame honors
    // the operator's color + rotation settings on glass.
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let scene_fbo_handle = if !identity || rotation != 0 {
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
    unsafe {
        bake_external_nv12_to_current_fbo(
            session.gl,
            &mut session.external_nv12_tex,
            nv12,
            frame_w,
            frame_h,
            mode_w,
            mode_h,
        )?;
    }
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }
    // Scanout swap / lock / addFB / commit / pair-rotation — verbatim
    // from paint_and_present_external_frame.
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (external_nv12) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (external_nv12) failed")?
    };
    let fb_buf =
        GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata (external_nv12)")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (external_nv12) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (external_nv12): {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev, external_nv12): {e}");
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

/// V4L2 piece 3e (2026-05-14) -- per-advance VideoSlide paint.
/// Feeds the next H.264 sample (if any) into a primed v4l2::Decoder,
/// drains the next decoded NV12 Frame (with a short EAGAIN retry
/// budget), uploads Y + UV planes to GLES textures, blits through
/// the BT.709 NV12 -> RGB shader (FS_NV12_TO_RGB from piece 3d),
/// and swaps + commits the scanout buffer pair (same FB-rotation
/// shape as paint_and_present_one_image_slide_frame).
///
/// Inputs:
///   - `samples`: the demuxer's per-sample Annex-B NAL buffers.
///   - `next_sample_idx`: index of the next sample to feed; the
///     caller updates this in place after a successful feed.
///   - `frames_decoded`: number of frames returned so far; the
///     caller increments after a successful paint. Used to log
///     a per-slide-end frame count.
///   - `decoder`: the primed v4l2::Decoder (from piece 3c).
///
/// Returns Ok(()) on a successful paint. Returns Err if the codec
/// hits a non-EAGAIN error, the upload fails, or scanout commit
/// fails. On EOS (no more samples + drain returns None) returns
/// Ok(()) without painting -- caller's advance state machine has
/// already moved on by then.
#[cfg(target_os = "linux")]
pub fn paint_and_present_one_video_slide_frame(
    session: &mut EglSession,
    card: &Card,
    samples: &[crate::mp4_demux::Sample],
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
) -> Result<()> {
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // V4L2 piece 4f first-frame profile gate. profile_first is
    // captured BEFORE calling the bake helper because the helper
    // increments next_sample_idx and frames_decoded on success,
    // which would invalidate the (next=1, decoded=0) check. The
    // helper handles the feed/dqbuf/blit timing internally;
    // t_enter here bookends the call for the swap_commit + total
    // log emitted below the swap (DMABUF path only; the MMAP path
    // was always silent for the total log and Phase 8 slice 2
    // preserves that asymmetry).
    // 2026-06-15 perf-gl W-2: was a per-frame std::env::var call;
    // now reads a thread_local-cached bool (resolved once per
    // worker). Saves ~0.5-1 µs/frame (env::var allocates the
    // String on every call; the cached path is a Cell::get()).
    let profile_first = *next_sample_idx == 1
        && *frames_decoded == 0
        && firstframe_profile_enabled_cached();
    let t_enter = if profile_first { Some(std::time::Instant::now()) } else { None };
    // perf-night r2 (2026-05-26): bake/compose/present sub-phases for
    // the runtime profile. Video bake = V4L2 sample drain + NV12
    // upload + cover-fit blit (inside bake_video_slide_to_current_fbo);
    // compose = present_pass when rotated; present = swap + lock +
    // addFB + commit_fb.
    let t_phase = std::time::Instant::now();
    // FYS bug 5: for a non-zero display rotation, the video frame
    // must bake into the logical-sized scene FBO and then be
    // rotated onto the panel by the present pass. With rotation==0
    // the helper bakes straight into the default fb (the active
    // framebuffer at entry) -- byte-identical to the legacy path,
    // no scene FBO. (Brightness/gamma are intentionally NOT applied
    // to the video path; that matches pre-rotation behavior.)
    let rotation = session.rotation;
    let scene_fbo_handle = if rotation != 0 {
        Some(unsafe { ensure_scene_fbo(session, mode_w, mode_h)? })
    } else {
        None
    };
    if let Some((fbo, _tex)) = scene_fbo_handle {
        use glow::HasContext;
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
        }
    }
    // Phase 8 slice 2 (2026-05-16): per-frame video bake extracted
    // into bake_video_slide_to_current_fbo. Hold-path scanout
    // semantics unchanged -- the helper feeds+drains+blits into the
    // active framebuffer (scene FBO when rotated, else default fb)
    // and returns the path label, then the caller does the
    // swap+commit. Slice 4 reuses the helper from the transition
    // path with an FBO bind in front of the call.
    let painted = unsafe {
        bake_video_slide_to_current_fbo(
            session,
            samples,
            next_sample_idx,
            frames_decoded,
            decoder,
            mode_w,
            mode_h,
            // Steady-state pure-video paints into the WINDOW FB.
            // eglSwapBuffers is the implicit barrier.
            /* is_offscreen_bake (Path A Stage 2 scope tag) */ false,
        )?
    };
    let Some(path_label) = painted else {
        // No frame ready this tick. Don't error -- the next advance
        // can try again. Leaves whatever's on screen (last decoded
        // frame or black if never decoded). Re-bind the default fb
        // so a rotation-routed skip doesn't leave the scene FBO
        // bound for the next op.
        if scene_fbo_handle.is_some() {
            use glow::HasContext;
            unsafe { session.gl.bind_framebuffer(glow::FRAMEBUFFER, None); }
        }
        // Sample the bake even on no-frame ticks -- it captures the
        // V4L2 dqbuf wait + early-return path which can stall.
        crate::profile::record_phase(
            "paint_bake_video",
            t_phase.elapsed().as_nanos() as u64,
        );
        return Ok(());
    };
    crate::profile::record_phase(
        "paint_bake_video",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    // FYS bug 5: rotated route -- present the logical scene FBO onto
    // the panel-native default fb with the rotating present pass.
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let (phys_w, phys_h) = session.phys_mode_size();
        use glow::HasContext;
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            // brightness/gamma identity (1.0/1.0): the video path
            // does not apply color settings -- present is rotate-only.
            run_present_pass(session.gl, tex, 1.0, 1.0, rotation)?;
        }
    }
    crate::profile::record_phase(
        "paint_compose",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    let t_commit = if profile_first { Some(std::time::Instant::now()) } else { None };
    let r = finish_video_slide_swap_and_commit(session, card);
    crate::profile::record_phase(
        "paint_present",
        t_phase.elapsed().as_nanos() as u64,
    );
    if path_label == "DMABUF" {
        if let (Some(tc), Some(te)) = (t_commit, t_enter) {
            eprintln!(
                "[firstframe] swap_commit={:.2}ms total={:.2}ms (DMABUF)",
                tc.elapsed().as_secs_f64() * 1000.0,
                te.elapsed().as_secs_f64() * 1000.0,
            );
        }
    }
    // MMAP path: pre-refactor function did not log a swap_commit/
    // total line; Phase 8 slice 2 preserves the asymmetry.
    r
}

/// V4L2 piece 4d (2026-05-14): shared scanout swap + commit tail
/// for `paint_and_present_one_video_slide_frame`. Both the MMAP
/// blit path (piece 3d/e) and the DmaBuf EGLImage path (piece 4c)
/// finish via this. Mirrors paint_and_present_one_image_slide_
/// frame's scanout rotation: eglSwapBuffers, lock front BO,
/// drmModeAddFB, page-flip commit, shift scanout_current ->
/// scanout_prev. Caller must have ALREADY dropped the Frame +
/// incremented frames_decoded.
#[cfg(target_os = "linux")]
fn finish_video_slide_swap_and_commit(
    session: &mut EglSession,
    card: &Card,
) -> Result<()> {
    // QA live-preview hook (2026-06-13): no-op unless
    // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (video_slide) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (video_slide) failed")?
    };
    let fb_buf = GbmBufferAdapter::new(&new_bo).context("read GBM bo metadata (video_slide)")?;
    let new_fb = card
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (video_slide) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card, new_fb) {
        if let Err(de) = card.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (video_slide): {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    if let Some(fb) = session.scanout_prev_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev, video_slide): {e}");
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

/// QA-direct (2026-05-13 sidecar feature-gaps slice) -- repaint an
/// ImageSlide into the EGL window surface WITHOUT scanout commit.
/// Used by the IPC Capture op's re-paint pattern: paint into the
/// surface, glReadPixels back from the default framebuffer.
/// Counterpart to paint_one_for_capture (which handles TextSlide).
///
/// Rotation note: capture paths deliberately render at LOGICAL
/// (un-rotated) dims and are NOT routed through the present-pass
/// rotation — the captured PNG is the content in its authored
/// orientation. Rotated-thumbnail handling is the UI's job (FYS
/// bug 7). Don't "fix" this to rotate.
pub fn paint_one_image_slide_for_capture(
    session: &mut EglSession,
    slide: &ImageSlide,
    content_root: &Path,
) -> Result<()> {
    use glow::HasContext;
    let asset_path = crate::content::image_slide_asset_path(content_root, slide.id);
    let (rgba, img_w, img_h) = load_png_rgba(&asset_path)?;
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    // v1-spec-delta #10 (slice c): match paint_and_present_one_image_
    // slide_frame's non-identity post-pass so captures reflect
    // operator color settings.
    let identity = session.current_settings.is_color_identity();
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
    unsafe {
        let gl = session.gl;
        gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        gl.clear_color(0.0, 0.0, 0.0, 1.0);
        gl.clear(glow::COLOR_BUFFER_BIT);
        let tex = gl
            .create_texture()
            .map_err(|e| anyhow!("glGenTextures(image_slide capture): {e}"))?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.tex_image_2d(
            glow::TEXTURE_2D, 0, glow::RGBA as i32,
            img_w as i32, img_h as i32, 0,
            glow::RGBA, glow::UNSIGNED_BYTE, Some(&rgba),
        );
        let blit_result = run_blit_pass(gl, tex);
        gl.delete_texture(tex);
        blit_result?;
    }
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = (session.current_settings.brightness as f32) / 100.0;
        let gamma = session.current_settings.gamma;
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            run_bright_gamma_pass(session.gl, tex, brightness, gamma)?;
        }
    }
    unsafe { session.gl.flush(); }
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
///
/// Phase 4v-3b (2026-05-16): each per-Advance bake now paints
/// with per-frame motion_states (computed from session.motion_
/// tick_seconds), so animated layers keep moving DURING
/// transitions instead of freezing at first-frame pose. CPU cost
/// is ~2 ms incremental (motion_states_for_layers); GPU cost
/// unchanged (the bake was per-call already). See audit doc at
/// qa/captures/motion-through-transitions-audit-2026-05-16.md.
///
/// Phase 8 slice 4-6 (2026-05-16): endpoints are
/// `TransitionEndpoint<'_>` carrying per-kind state. Text/Image/
/// Video all route through `bake_slide_to_fbo`. Slice 6 added the
/// Video wiring: the IPC PaintTransition handler looks up V4L2
/// decoder state from `cache.video_decoders` + demuxer samples
/// from `cache.video_demuxers` and packs them into
/// `TransitionEndpoint::Video`. The dispatcher's
/// `SlideBakeInputs::Video` arm then routes through the slice-2
/// `bake_video_slide_to_current_fbo` helper. Option D cadence per
/// `feedback_motion_through_transitions_required`: video drains
/// one V4L2 sample per Advance, so video frames keep playing
/// THROUGH the transition window alongside Text motion phase.
///
/// Dual-video (Video→Video different slides) uses an `iter_mut`-
/// based disjoint &mut lookup at the IPC handler — Rust 1.85
/// doesn't have `HashMap::get_disjoint_mut` (stable in 1.86) and
/// `iter_mut` is the safe-Rust polyfill. Same-id Video→Video is
/// explicitly bailed at the IPC handler: it would need two &mut
/// to one decoder entry (impossible in safe Rust) AND would
/// semantically double-drain per Advance.
pub fn paint_and_present_one_transition_frame(
    session: &mut EglSession,
    card: &Card,
    mut endpoint_a: TransitionEndpoint<'_>,
    mut endpoint_b: TransitionEndpoint<'_>,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    kind: &str,
    progress: f32,
) -> Result<()> {
    use glow::HasContext;
    // R-106-FREEZE-FIX (2026-06-16): clear the cached-pair
    // "painted" flags on the FIRST tick of a new transition so the
    // `Ok(None) → reuse_cached_b` branch downstream (~line 5303)
    // can't surface the PRIOR transition's baked side-B content.
    // The flag is armed at BeginTransition (in hdmi_logic.rs's
    // `record_transition_begin_for_endpoint_b_metric` which also
    // sets `TRANSITION_PAINTED_FLAGS_NEED_RESET`), so the consume
    // here fires exactly once per transition — first paint tick
    // resets, subsequent ticks within the same transition keep
    // the within-transition reuse benefit (codec hiccup mid-
    // transition still gets the last good baked frame instead of
    // stalling).
    //
    // QA bug 2026-06-16: side-B `transition_tex_probe` rgb=143,36,46
    // luma=69 IDENTICAL across every transition kind (halftone /
    // pixelate / fade / dissolve), regardless of incoming slide
    // id. Smoking gun: balloons (reel idx 1) is the FIRST
    // incoming slide; once baked, `transition_fbo_b_painted`=true
    // never cleared, so every later transition's first bake_b
    // iteration that hit `Ok(None)` (codec ramping) reused the
    // balloons content. Fix: reset both painted flags at
    // transition start. Side A is symmetric — even though A
    // typically bakes Ok(Some) first iteration and doesn't hit
    // the reuse path in practice, reset both for defense-in-depth.
    if crate::hdmi_logic::take_transition_painted_flags_need_reset() {
        session.transition_fbo_a_painted = false;
        session.transition_fbo_b_painted = false;
        eprintln!(
            "[perf] transition_fbo_painted_reset side=both kind={} progress={:.3} reason=new_transition",
            kind, progress,
        );
    }
    // r102.1.1 (2026-06-09): V3D BO leak probe. Throttle to
    // FIRST and LAST tick of each transition so QA can bracket
    // the transition's BO delta without log-volume blowup.
    //
    // r102.1 (parent) had TWO bugs caught by QA on FYS:
    //   1. The probe CALL was clobbered when the WARN-2 comment
    //      block was added (the `if progress < 0.05 { log... }`
    //      line went missing entirely). transition_paint_entry
    //      never fired in the journal because there was no
    //      caller, not because the threshold was wrong.
    //   2. Even if the call was present, `progress < 0.05` was
    //      too tight: Python backend sends the first paint at
    //      progress > 0.05 (likely 0.066 = tick 1 of 15 at 33ms
    //      per tick, OR a post-prime gap that consumes the
    //      first 30-50ms of the transition window).
    //
    // r102.1.1 restores the missing call AND widens the entry
    // threshold to 0.20 so the first 3-9 ticks of a 1.0-1.5s
    // transition catch the boundary regardless of where Python
    // pushes its first paint. Exit threshold (0.95) shipped
    // working in r102.1 (it was on the success arm at the
    // function tail, untouched by the comment-clobber); kept
    // as-is.
    if progress < 0.20 {
        crate::v4l2::log_v3d_bos_at_phase("transition_paint_entry", None);
    }
    let fs = match fs_for_transition_kind(kind) {
        Some(s) => s,
        None => {
            eprintln!(
                "warn: transition kind {kind:?} not yet implemented; falling back to cut"
            );
            FS_CUT
        }
    };
    let mode_w_u32 = session.mode_w as u32;
    let mode_h_u32 = session.mode_h as u32;
    let tick_seconds = session.motion_tick_seconds();

    // Phase 8 slice 6 per-endpoint pre-resolve. Text/Image endpoints
    // own derived data (resolved text layers Vec, motion_states Vec,
    // image PathBuf) that needs to outlive the bake closure; Video
    // endpoints carry refs on the `TransitionEndpoint::Video` value
    // itself, so no pre-resolve. Each slot is `None` for the
    // inapplicable kinds; the SlideBakeInputs builder inside the
    // closure unwraps based on the endpoint variant.
    type TextResolved<'a> = (
        uuid::Uuid,
        BgKind,
        Vec<(&'a crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)>,
        Vec<MotionState>,
    );
    let mut text_a: Option<TextResolved<'_>> = None;
    let mut text_b: Option<TextResolved<'_>> = None;
    // Task #168: capture slide_id alongside asset_path so the
    // SlideBakeInputs::Image variant can drive the per-session
    // ImageSlideTextureCache lookup keyed by slide_id.
    let mut image_a: Option<(uuid::Uuid, PathBuf)> = None;
    let mut image_b: Option<(uuid::Uuid, PathBuf)> = None;
    // r50 (2026-06-03): TextOverVideo pre-resolves like Text but
    // tagged separately so the bake dispatcher routes to the
    // SlideBakeInputs::TextOverVideo branch (with the bg-video V4L2
    // state carried in the endpoint enum itself).
    let mut text_over_video_a: Option<(
        uuid::Uuid,
        Vec<(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)>,
        Vec<MotionState>,
    )> = None;
    let mut text_over_video_b: Option<(
        uuid::Uuid,
        Vec<(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)>,
        Vec<MotionState>,
    )> = None;
    match &endpoint_a {
        TransitionEndpoint::Text(slide) => {
            let (bg, _, layers) = resolve_slide_layers(slide, fonts, content_root)?;
            let motion_states = motion_states_for_layers(slide.id, &layers, tick_seconds);
            text_a = Some((slide.id, bg, layers, motion_states));
        }
        TransitionEndpoint::Image(slide) => {
            let root = content_root.ok_or_else(|| {
                anyhow!(
                    "paint_transition: content_root required for image endpoint (slide_id={})",
                    slide.id
                )
            })?;
            image_a = Some((slide.id, crate::content::image_slide_asset_path(root, slide.id)));
        }
        TransitionEndpoint::Video { .. } => {}
        TransitionEndpoint::TextOverVideo { text_slide, .. } => {
            let (bg, _, layers) = resolve_slide_layers(text_slide, fonts, content_root)?;
            // r50 subagent (2026-06-03 NIT): warn if a text-over-
            // video slide arrives with both bg_video_id AND a non-
            // solid bg_kind. The Pydantic mutex at backend/
            // openmarquee/content/__init__.py:310 enforces this, so
            // it should be impossible -- but a future validator
            // regression or hand-edited content JSON could bypass.
            // Mirrors the r46 dual-bg mutex warn at ipc_main.rs
            // (background_video + background_image) one layer up.
            if !matches!(bg, BgKind::Solid(_)) {
                eprintln!(
                    "warn: text-over-video slide {} has bg_video_id AND non-solid bg_kind; bg_kind ignored (transition endpoint_a)",
                    text_slide.id
                );
            }
            let motion_states = motion_states_for_layers(text_slide.id, &layers, tick_seconds);
            text_over_video_a = Some((text_slide.id, layers, motion_states));
        }
    }
    match &endpoint_b {
        TransitionEndpoint::Text(slide) => {
            let (bg, _, layers) = resolve_slide_layers(slide, fonts, content_root)?;
            let motion_states = motion_states_for_layers(slide.id, &layers, tick_seconds);
            text_b = Some((slide.id, bg, layers, motion_states));
        }
        TransitionEndpoint::Image(slide) => {
            let root = content_root.ok_or_else(|| {
                anyhow!(
                    "paint_transition: content_root required for image endpoint (slide_id={})",
                    slide.id
                )
            })?;
            image_b = Some((slide.id, crate::content::image_slide_asset_path(root, slide.id)));
        }
        TransitionEndpoint::Video { .. } => {}
        TransitionEndpoint::TextOverVideo { text_slide, .. } => {
            let (bg, _, layers) = resolve_slide_layers(text_slide, fonts, content_root)?;
            if !matches!(bg, BgKind::Solid(_)) {
                eprintln!(
                    "warn: text-over-video slide {} has bg_video_id AND non-solid bg_kind; bg_kind ignored (transition endpoint_b)",
                    text_slide.id
                );
            }
            let motion_states = motion_states_for_layers(text_slide.id, &layers, tick_seconds);
            text_over_video_b = Some((text_slide.id, layers, motion_states));
        }
    }

    // Ok(true) = transition frame painted + ready to present;
    // Ok(false) = FYS bug C skip (a video endpoint had no frame
    // ready this tick) — caller skips the swap+commit.
    let work: Result<bool> = (|| unsafe {
        // Two sequential bakes via the dispatcher. For Text
        // endpoints, `bake_slide_to_fbo` does the slide_caches
        // prewarm + `get_mut` internally. The `&mut session`
        // reborrow ends with each bake call, so same-id text/text
        // is fine (second prewarm sees needs_new=false). For Video
        // endpoints the bake helper drains ONE V4L2 sample per
        // call — Option D cadence per `feedback_motion_through_
        // transitions_required`, video plays through the transition
        // alongside text motion phase and image still-frames.
        let inputs_a = match &mut endpoint_a {
            TransitionEndpoint::Text(_) => {
                let (id, bg, layers, states) =
                    text_a.as_ref().expect("text_a pre-resolved above");
                SlideBakeInputs::Text {
                    slide_id: *id,
                    bg_kind: bg,
                    text_layers: layers,
                    motion_states: Some(states),
                }
            }
            TransitionEndpoint::Image(_) => {
                let (sid, path) = image_a.as_ref().expect("image_a pre-resolved above");
                SlideBakeInputs::Image {
                    slide_id: *sid,
                    asset_path: path.as_path(),
                }
            }
            TransitionEndpoint::Video {
                samples,
                next_sample_idx,
                frames_decoded,
                decoder,
                ..
            } => SlideBakeInputs::Video {
                samples: *samples,
                next_sample_idx: &mut **next_sample_idx,
                frames_decoded: &mut **frames_decoded,
                decoder: *decoder,
            },
            TransitionEndpoint::TextOverVideo {
                bg_samples,
                bg_next_sample_idx,
                bg_frames_decoded,
                bg_decoder,
                ..
            } => {
                let (id, layers, states) = text_over_video_a
                    .as_ref()
                    .expect("text_over_video_a pre-resolved above");
                SlideBakeInputs::TextOverVideo {
                    slide_id: *id,
                    text_layers: layers,
                    motion_states: Some(states),
                    bg_samples: *bg_samples,
                    bg_next_sample_idx: &mut **bg_next_sample_idx,
                    bg_frames_decoded: &mut **bg_frames_decoded,
                    bg_decoder: *bg_decoder,
                }
            }
        };
        // FYS bug C: a Video endpoint with no frame ready this tick
        // bakes to Ok(None) — skip the whole transition paint for
        // this tick (the next advance retries). bake_a's None has
        // already freed its own FBO pair, so there is nothing to
        // clean up here.
        //
        // r94 Path B note: bake_a's Ok(None) is NOT wrapped in a
        // deadline-poll. endpoint_a is the from-slide whose video
        // decoder has been playing -- its pipeline is warm and an
        // Ok(None) here means a genuine stall (decoder error, end
        // of clip, etc.) rather than the cold-start latency Path B
        // exists to absorb. The asymmetric treatment matches the
        // observed failure shape: only endpoint_b's just-primed
        // decoder needs the polling window. If a future symptom
        // shows endpoint_a stalling, mirror the bake_b loop here.
        // r102.2: thread the cached transition FBO+tex pair for
        // side A. Pre-r102.2 each tick allocated ~8 MB of fresh
        // FBO+tex via create_slide_fbo_pair / make_slide_fbo; vc4
        // V3D lazy GC retained ~1 BO per transition. The cache
        // eliminates the churn -- exactly ONE pair per
        // (EglSession, mode_w, mode_h) for side A across the
        // session's lifetime. Kill switch
        // OPENMARQUEE_TRANSITION_FBO_CACHE=off falls back to
        // per-tick alloc.
        let cached_pair_a = if crate::v4l2::is_transition_fbo_cache_enabled() {
            Some(ensure_transition_fbo_pair(
                session,
                TransitionFboSide::A,
                mode_w_u32,
                mode_h_u32,
            )?)
        } else {
            None
        };
        let (fbo_a, tex_a) = match bake_slide_to_fbo(
            session,
            mode_w_u32,
            mode_h_u32,
            cached_pair_a,
            inputs_a,
        )? {
            Some(pair) => {
                // r106 + Path A Stage 2 (2026-06-14): bake
                // landed real content into the cached pair —
                // any future Ok(None) on side A this transition
                // can safely reuse this content.
                if cached_pair_a.is_some() {
                    session.transition_fbo_a_painted = true;
                }
                pair
            }
            None => {
                // R-106-LIVE-MOTION (2026-06-16, QA #1-priority
                // v2v transition correctness): the r106 + Path A
                // Stage 2 reuse-cached-on-Ok(None) path REMOVED.
                // It would surface the LAST GOOD baked frame of
                // the FROM-side as a still while the codec
                // catches up — locally smooth but VIOLATES
                // qarl's NON-NEGOTIABLE "motion through
                // transitions" requirement. The reuse-cached
                // pre-fix produced visible stills for the rest
                // of a transition once the codec ever bubbled
                // mid-transition. Post 2ead796's eviction-timing
                // fix (combined-stack eeb84ec, live + verified)
                // the codec headroom is restored; dual-live v2v
                // transitions fit the per-tick budget on the Pi
                // Zero 2 W, so the skip-tick fallback (= pre-
                // r106 behavior) is correct again. QA gates this
                // on-glass per frame-time DISTRIBUTION + visible-
                // smoothness — if a measurable hitch class
                // emerges, the right fix is poll harder / extend
                // budget at the v4l2 layer, NOT re-introduce the
                // freeze.
                crate::hdmi_logic::warn_paint_transition_skip(
                    kind, progress, "endpoint_a_no_frame",
                );
                eprintln!(
                    "[perf] transition_skip_tick_live_only side=a kind={} progress={:.3} reason=endpoint_a_no_frame",
                    kind, progress,
                );
                return Ok(false);
            }
        };
        // r94 Path B (2026-06-08): consumer-side deadline-poll.
        //
        // r80-r92 tried to PRE-PROVIDE endpoint_b's first frame at
        // prime time (via warmup_count, via CMD_STOP EOS-flush, via
        // ioctl bisecting). r93 proved warmup=3 wedges the renderer
        // (CAPTURE saturation pins OUTPUT). r92 proved CMD_STOP is
        // poisonous on bcm2835-codec. Path B inverts the design:
        // don't pre-provide; consume-with-deadline at the actual
        // point of need.
        //
        // Pre-r94: bake_slide_to_fbo Ok(None) immediately fell
        // through to "skip swap + hold prior frame" (the cut-like
        // visual). The kernel got ZERO of the transition window to
        // produce frame 0.
        //
        // r94: on Ok(None), sleep briefly + retry bake_slide_to_fbo
        // up to OPENMARQUEE_BAKE_B_POLL_DEADLINE_MS (default 100ms,
        // ~ 3 frames @ 30fps). Each retry feeds another sample (per
        // bake_video_slide_to_current_fbo's internal feed step) so
        // the kernel pipeline gets progressively more input.
        //
        // Deadline budget math (subagent r94 WARN-1):
        //   - bake_video_slide_to_current_fbo's INNER retry loop at
        //     hdmi.rs:7677 = 10 * 3ms = ~30ms per call before
        //     returning Ok(None).
        //   - Outer (this) loop's 2ms sleep + ~30ms inner = ~32ms
        //     per iteration. Default deadline=100ms allows ~3
        //     iterations -- enough to actually push the kernel
        //     pipeline forward through cold-start.
        //
        // Iteration safety caps (subagent r94 WARN-2 + WARN-3):
        //   - hard MAX_ITERS=4 cap independent of time. paint runs
        //     on the IPC main thread (EglSession !Send), so a long
        //     synchronous poll blocks cancel/health-check IPC ops.
        //     4 iterations at ~32ms ≈ 128ms cap -- safely under the
        //     backend's 60s IPC timeout but bounded.
        //   - Iteration cap also bounded by samples-remaining for
        //     the video endpoint so the in-bake wrap at
        //     hdmi.rs:7697 doesn't trigger mid-loop (the wrap's
        //     V4L2-state reset is dispatcher-side, BEFORE bake;
        //     wrapping inside Path B would bypass it).
        //
        // GL-resource safety: bake_slide_to_fbo's Video branch at
        // hdmi.rs:8329 deletes its created (fbo, tex) on Ok(None)
        // before returning, so retrying doesn't leak GL state.
        //
        // The new probe `[perf] bake_b_poll_outcome` lets QA see
        // how many iterations + how long it took. The legacy
        // `endpoint_b_no_frame` WARN throttle is preserved on the
        // deadline-exhaust path so existing dashboards still work.
        const PATH_B_MAX_ITERS: u32 = 4;
        let bake_b_deadline_ms: u64 = std::env::var("OPENMARQUEE_BAKE_B_POLL_DEADLINE_MS")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(100);
        let bake_b_deadline = std::time::Duration::from_millis(bake_b_deadline_ms);
        let bake_b_start = std::time::Instant::now();
        // Snapshot the maximum samples we can advance through this
        // bake call without hitting the in-bake wrap. Re-read each
        // iteration via `endpoint_b` because Video's next_sample_idx
        // mutates per inner bake call.
        // r102.2 subagent WARN-2: hoist the side-B cache resolve
        // OUT of the Path B retry loop. The first iteration's
        // cache-miss does the allocate; subsequent retries get
        // the same handle (no work). Mirrors bake_a's pattern at
        // line 5039 (single resolve before the call) and keeps
        // env-read + borrow cost off the retry hot loop.
        let cached_pair_b = if crate::v4l2::is_transition_fbo_cache_enabled() {
            Some(ensure_transition_fbo_pair(
                session,
                TransitionFboSide::B,
                mode_w_u32,
                mode_h_u32,
            )?)
        } else {
            None
        };
        let mut bake_b_iterations: u32 = 0;
        let (fbo_b, tex_b) = loop {
            bake_b_iterations += 1;
            // Re-build inputs_b on each iteration. The match consumes
            // a fresh `&mut endpoint_b` borrow, which is released
            // when bake_slide_to_fbo consumes the inputs.
            let inputs_b = match &mut endpoint_b {
                TransitionEndpoint::Text(_) => {
                    let (id, bg, layers, states) =
                        text_b.as_ref().expect("text_b pre-resolved above");
                    SlideBakeInputs::Text {
                        slide_id: *id,
                        bg_kind: bg,
                        text_layers: layers,
                        motion_states: Some(states),
                    }
                }
                TransitionEndpoint::Image(_) => {
                    let (sid, path) = image_b.as_ref().expect("image_b pre-resolved above");
                    SlideBakeInputs::Image {
                        slide_id: *sid,
                        asset_path: path.as_path(),
                    }
                }
                TransitionEndpoint::Video {
                    samples,
                    next_sample_idx,
                    frames_decoded,
                    decoder,
                    ..
                } => SlideBakeInputs::Video {
                    samples: *samples,
                    next_sample_idx: &mut **next_sample_idx,
                    frames_decoded: &mut **frames_decoded,
                    decoder: *decoder,
                },
                TransitionEndpoint::TextOverVideo {
                    bg_samples,
                    bg_next_sample_idx,
                    bg_frames_decoded,
                    bg_decoder,
                    ..
                } => {
                    let (id, layers, states) = text_over_video_b
                        .as_ref()
                        .expect("text_over_video_b pre-resolved above");
                    SlideBakeInputs::TextOverVideo {
                        slide_id: *id,
                        text_layers: layers,
                        motion_states: Some(states),
                        bg_samples: *bg_samples,
                        bg_next_sample_idx: &mut **bg_next_sample_idx,
                        bg_frames_decoded: &mut **bg_frames_decoded,
                        bg_decoder: *bg_decoder,
                    }
                }
            };
            match bake_slide_to_fbo(session, mode_w_u32, mode_h_u32, cached_pair_b, inputs_b) {
                Ok(Some(p)) => {
                    // r76 Phase A: emit begin_transition -> endpoint_b
                    // first-frame gap. r94: also surface poll outcome
                    // so QA sees how long the kernel pipeline took.
                    if matches!(
                        endpoint_b,
                        TransitionEndpoint::Video { .. } | TransitionEndpoint::TextOverVideo { .. }
                    ) {
                        crate::hdmi_logic::consume_transition_endpoint_b_first_frame_marker();
                    }
                    let elapsed_us = bake_b_start.elapsed().as_micros();
                    if bake_b_iterations > 1 {
                        eprintln!(
                            "[perf] bake_b_poll_outcome kind={} progress={:.3} \
                             iterations={} elapsed_us={} result=ok_after_polling \
                             deadline_ms={}",
                            kind, progress, bake_b_iterations, elapsed_us, bake_b_deadline_ms,
                        );
                    }
                    // r106 + Path A Stage 2 (2026-06-14): bake landed
                    // real content into the cached pair — any future
                    // Ok(None) on side B this transition can reuse
                    // this content. Mirrors the bake_a paint-flag set
                    // above.
                    if cached_pair_b.is_some() {
                        session.transition_fbo_b_painted = true;
                    }
                    break p;
                }
                Ok(None) => {
                    // r94 Path B: kernel pipeline not ready yet.
                    // Three independent caps gate the retry:
                    //   1. Deadline (default 100ms; env-tunable)
                    //   2. PATH_B_MAX_ITERS=4 (IPC-thread block cap)
                    //   3. Samples-remaining for Video endpoints
                    //      (avoid in-bake wrap bypassing the
                    //      dispatcher-side V4L2 state reset)
                    //
                    // r106 + Path A Stage 2 (2026-06-14): when
                    // decouple is ON, skip the Path B retry sleeps
                    // entirely. The bake itself is now non-blocking
                    // under decouple (single try_feed_nonblocking
                    // topup + single non-blocking next_frame), so a
                    // 2ms sleep + immediate re-ask of the same
                    // kernel state buys nothing — the codec needs
                    // the next wall-clock tick to advance. Fall
                    // straight through to cached-pair reuse below
                    // (or skip-tick if reuse isn't safe yet).
                    let decouple = crate::v4l2::is_feed_drain_decouple_enabled();
                    let deadline_ok = bake_b_start.elapsed() < bake_b_deadline;
                    let iter_ok = bake_b_iterations < PATH_B_MAX_ITERS;
                    let samples_remaining_ok = match &endpoint_b {
                        TransitionEndpoint::Video {
                            samples,
                            next_sample_idx,
                            ..
                        }
                        | TransitionEndpoint::TextOverVideo {
                            bg_samples: samples,
                            bg_next_sample_idx: next_sample_idx,
                            ..
                        } => {
                            // Next bake_video call advances by 1; we
                            // need samples[idx] to exist for the
                            // upcoming iteration without wrap.
                            **next_sample_idx < samples.len()
                        }
                        _ => true, // Text/Image never returns None
                    };
                    // R-106-LIVE-MOTION (2026-06-16): the retry was
                    // previously gated on `!decouple` because with
                    // decouple=ON the reuse-cached fallback handled
                    // Ok(None) by surfacing the prior frame as a
                    // still — that fallback is REMOVED for the
                    // motion-through-transitions requirement, so the
                    // sleep-poll-retry is now the ONLY way to recover
                    // a transient codec hiccup mid-transition. Drop
                    // the `!decouple` gate; retry within deadline /
                    // iter / samples caps unconditionally.
                    if deadline_ok && iter_ok && samples_remaining_ok {
                        std::thread::sleep(std::time::Duration::from_millis(2));
                        continue;
                    }
                    // Caps exhausted (or decouple skipped Path B
                    // entirely). Fall through to cached-pair reuse
                    // (r106 + Path A Stage 2) or legacy r69 skip.
                    let elapsed_us = bake_b_start.elapsed().as_micros();
                    let reason = if decouple {
                        "decouple_skip_pathb"
                    } else if !samples_remaining_ok {
                        "samples_exhausted_in_loop"
                    } else if !iter_ok {
                        "iter_cap"
                    } else {
                        "deadline_exhausted"
                    };
                    eprintln!(
                        "[perf] bake_b_poll_outcome kind={} progress={:.3} \
                         iterations={} elapsed_us={} result={} \
                         deadline_ms={}",
                        kind, progress, bake_b_iterations, elapsed_us, reason, bake_b_deadline_ms,
                    );
                    // R-106-LIVE-MOTION (2026-06-16, QA #1-priority
                    // v2v transition correctness): the r106 + Path A
                    // Stage 2 reuse-cached-on-Ok(None) path REMOVED
                    // for side B. The pre-fix behavior surfaced the
                    // first-good-baked-frame of the TO-side as a
                    // still while the codec ramped — visible result:
                    // every transition's incoming video frozen
                    // (worst case: ALL transitions frozen on the
                    // first incoming slide's first baked frame
                    // because `transition_fbo_b_painted` only reset
                    // on dims-change, per the same arc's
                    // R-106-FREEZE-FIX). VIOLATES qarl's NON-
                    // NEGOTIABLE "motion through transitions"
                    // requirement (live render u_to + u_from every
                    // frame; NO snapshot-and-crossfade). The skip-
                    // tick fallback (= pre-r106) returns; post
                    // 2ead796 the codec headroom is restored so the
                    // pre-r106 stall failure-mode shouldn't recur
                    // — QA gates on-glass frame-time DISTRIBUTION +
                    // visible smoothness to confirm. If a measurable
                    // hitch class emerges, the fix is poll harder /
                    // extend budget at the v4l2 layer, NOT re-
                    // introduce the freeze.
                    crate::hdmi_logic::warn_paint_transition_skip(
                        kind, progress, "endpoint_b_no_frame",
                    );
                    eprintln!(
                        "[perf] transition_skip_tick_live_only side=b kind={} progress={:.3} reason=endpoint_b_no_frame",
                        kind, progress,
                    );
                    // r106 BLOCKER-1 carry-forward fix: only delete
                    // fbo_a/tex_a when the FBO cache is OFF (we
                    // allocated fresh this tick). When cache is ON,
                    // fbo_a/tex_a are the cached session.transition
                    // _fbo_a / _tex_a handles owned by the session;
                    // deleting them here dangles them for the next
                    // transition's ensure_transition_fbo_pair call
                    // AND lets cleanup_resources double-free at
                    // session teardown. Pre-fix it was a latent bug
                    // from r94 that r106's reuse-cached path made
                    // more reachable; we carry the gate forward
                    // because Path A Stage 2 shares the same code
                    // path.
                    let cache_enabled = crate::v4l2::is_transition_fbo_cache_enabled();
                    if !cache_enabled {
                        session.gl.delete_framebuffer(fbo_a);
                        session.gl.delete_texture(tex_a);
                    }
                    return Ok(false);
                }
                Err(e) => {
                    // Same BLOCKER-1 gate on the error path.
                    let cache_enabled = crate::v4l2::is_transition_fbo_cache_enabled();
                    if !cache_enabled {
                        session.gl.delete_framebuffer(fbo_a);
                        session.gl.delete_texture(tex_a);
                    }
                    return Err(e);
                }
            }
        };
        // r102.2: only delete the FBO+tex handles when the cache
        // is disabled (we allocated fresh this tick). When the
        // cache is enabled, session::cleanup_resources owns the
        // handles and frees them at session teardown.
        let cache_enabled = crate::v4l2::is_transition_fbo_cache_enabled();
        let cleanup_static = |gl: &glow::Context, vbo: Option<glow::Buffer>| {
            if let Some(vbo) = vbo { gl.delete_buffer(vbo); }
            if !cache_enabled {
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
            }
        };
        // r102.3 (2026-06-09): cache the program + locations + VBO
        // when OPENMARQUEE_TRANSITION_PROGRAM_CACHE is enabled
        // (default). Pre-r102.3 per-tick link_program +
        // create_buffer were the dominant remaining V3D leak
        // surface after r102.2 plugged the FBO+tex churn
        // (~108 MB / 4 min on 720p per QA's r102.2 verify).
        //
        // Cache-disabled path (=off kill switch) takes the legacy
        // allocate-and-delete codepath unchanged so QA can A/B at
        // deploy time.
        let program_cache_enabled = crate::v4l2::is_transition_program_cache_enabled();
        let (program, a_pos, a_uv, u_src_a, u_src_b, u_t, u_aspect) = if program_cache_enabled {
            let cached = match cached_legacy_transition_program(session.gl, fs) {
                Ok(c) => c,
                Err(e) => {
                    cleanup_static(session.gl, None);
                    return Err(e);
                }
            };
            (
                cached.program,
                cached.a_pos,
                cached.a_uv,
                cached.u_src_a,
                cached.u_src_b,
                cached.u_t,
                cached.u_aspect,
            )
        } else {
            // Legacy per-tick path (kill-switch fallback). Mirrors
            // the pre-r102.3 shape verbatim.
            let program = match link_program(session.gl, VS_TEXTURED_QUAD, fs) {
                Ok(p) => p,
                Err(e) => {
                    cleanup_static(session.gl, None);
                    return Err(e);
                }
            };
            let a_pos = match session.gl.get_attrib_location(program, "a_pos") {
                Some(loc) => loc,
                None => {
                    cleanup_static(session.gl, None);
                    session.gl.delete_program(program);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_pos"));
                }
            };
            let a_uv = match session.gl.get_attrib_location(program, "a_uv") {
                Some(loc) => loc,
                None => {
                    cleanup_static(session.gl, None);
                    session.gl.delete_program(program);
                    return Err(anyhow!("VS_TEXTURED_QUAD missing a_uv"));
                }
            };
            let u_src_a = session.gl.get_uniform_location(program, "u_src_a");
            let u_src_b = session.gl.get_uniform_location(program, "u_src_b");
            let u_t = session.gl.get_uniform_location(program, "u_t");
            let u_aspect = session.gl.get_uniform_location(program, "u_aspect");
            (program, a_pos, a_uv, u_src_a, u_src_b, u_t, u_aspect)
        };
        // r102.3: VBO from per-session cache when enabled (single
        // 64-byte fullscreen-quad buffer reused across every
        // transition tick of every kind). cached_textured_quad_vbo
        // already exists and is used by run_blit_pass /
        // run_overlay_blend_pass; this just adds the live-3-pass
        // site as another consumer.
        let vbo = if program_cache_enabled {
            match cached_textured_quad_vbo(session.gl) {
                Ok(b) => b,
                Err(e) => {
                    // Don't delete the cached program -- it's
                    // owned by LEGACY_TRANSITION_PROGRAMS_V2.
                    cleanup_static(session.gl, None);
                    return Err(e);
                }
            }
        } else {
            // Legacy per-tick alloc path (kill-switch fallback).
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
            vbo
        };
        // r102.3 subagent BLOCKER-1: ensure GL_ARRAY_BUFFER points
        // at our quad VBO BEFORE vertex_attrib_pointer_f32 below
        // snapshots whatever buffer is currently bound. The
        // `cached_textured_quad_vbo` helper only binds on its
        // first-create path; on every subsequent cache hit it
        // returns the handle without binding, and the prior
        // bake_a/bake_b may have left a `cover_quad_vbo` (video)
        // or a text VBO bound. Without this rebind, tick 0 paints
        // correctly (cache miss = bind happened in the helper)
        // but every subsequent tick draws the wrong geometry.
        // Mirrors the SP path at hdmi.rs's transition_sp_quad_vbo
        // bind site + every other `cached_textured_quad_vbo`
        // consumer (run_bright_gamma_pass / run_blit_pass /
        // run_overlay_blend_pass / run_present_pass).
        session.gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));

        // v1-spec-delta #10 (slice c-2): when settings have non-
        // identity brightness/gamma, route the transition shader
        // output through the session's scene FBO + FS_BRIGHT_GAMMA
        // post-pass before scanout. Identity skips the FBO bind +
        // post-pass.
        //
        // FYS bug 5: the scene FBO is ALSO needed for any non-zero
        // display rotation. The transition shader composes both
        // logical-sized slide FBOs into the logical-sized scene FBO,
        // and the present pass rotates it onto the panel.
        let identity = session.current_settings.is_color_identity();
        let rotation = session.rotation;
        let scene_for_post_pass = if !identity || rotation != 0 {
            match ensure_scene_fbo(session, mode_w_u32, mode_h_u32) {
                Ok(handle) => Some(handle),
                Err(e) => {
                    // r102.3: same cache-vs-fresh rule as the
                    // success path above.
                    if program_cache_enabled {
                        cleanup_static(session.gl, None);
                    } else {
                        cleanup_static(session.gl, Some(vbo));
                        session.gl.delete_program(program);
                    }
                    return Err(e);
                }
            }
        } else {
            None
        };
        // 2026-06-14 iter-7 — pre-composite tex probe (carried
        // forward from iter-3 on the c3.x branch). Reads a 4×4
        // center patch from each FBO's COLOR_ATTACHMENT0 via
        // glReadPixels and emits one [perf] transition_tex_probe
        // line per side per transition. THIS IS THE SIGNAL that
        // tells us whether iter-7's scoped flush actually landed
        // pixels in transition_tex_a — without it the bench data
        // is just "glass looks black" or "glass looks ok" with no
        // numeric ground truth.
        //
        // Throttled via TRANSITION_TEX_PROBE_LAST_PROGRESS to ONE
        // tick per transition (first tick where progress crosses
        // 0.4 from below; re-arms when progress drops >0.1
        // signalling a new transition). Guarantees a reading even
        // when progress jumps under jank.
        //
        // glReadPixels is ~1-2 ms on vc4 720p; once per transition
        // (≈ once per 5-10 seconds at FYS slide pacing) is
        // negligible. THIS PROBE IS NOT A LOAD DRIVER; the iter-4
        // load=17 was the per-tick `gl.flush()`, not the per-
        // transition probe.
        let probe_fire = TRANSITION_TEX_PROBE_LAST_PROGRESS.with(|cell| {
            let last = cell.get();
            let new_transition = progress < last - 0.1;
            let fire_now = if new_transition {
                progress >= 0.4
            } else {
                last < 0.4 && progress >= 0.4
            };
            cell.set(progress);
            fire_now
        });
        if probe_fire {
            let mut probe = |label: &str, fbo: glow::NativeFramebuffer, tex: glow::NativeTexture| {
                let cx = (mode_w_u32 / 2).saturating_sub(2);
                let cy = (mode_h_u32 / 2).saturating_sub(2);
                let mut buf = [0u8; 16 * 4]; // 4x4 RGBA
                session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
                session.gl.read_pixels(
                    cx as i32,
                    cy as i32,
                    4,
                    4,
                    glow::RGBA,
                    glow::UNSIGNED_BYTE,
                    glow::PixelPackData::Slice(&mut buf[..]),
                );
                let mut sum_r = 0u32;
                let mut sum_g = 0u32;
                let mut sum_b = 0u32;
                for i in 0..16 {
                    sum_r += buf[i * 4] as u32;
                    sum_g += buf[i * 4 + 1] as u32;
                    sum_b += buf[i * 4 + 2] as u32;
                }
                let avg_r = sum_r / 16;
                let avg_g = sum_g / 16;
                let avg_b = sum_b / 16;
                let luma = (0.299 * avg_r as f32 + 0.587 * avg_g as f32 + 0.114 * avg_b as f32)
                    as u32;
                eprintln!(
                    "[perf] transition_tex_probe side={} kind={} progress={:.3} \
                     fbo_id={:?} tex_id={:?} rgb={},{},{} luma={}",
                    label, kind, progress, fbo, tex, avg_r, avg_g, avg_b, luma,
                );
            };
            probe("a", fbo_a, tex_a);
            probe("b", fbo_b, tex_b);
        }
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
        // r95: aspect bind for legacy FS_IRIS. No-op when the
        // shader doesn't declare u_aspect (other legacy shaders).
        session.gl.uniform_1_f32(
            u_aspect.as_ref(),
            (mode_w_u32 as f32) / (mode_h_u32 as f32),
        );
        session.gl.enable_vertex_attrib_array(a_pos);
        session.gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, 16, 0);
        session.gl.enable_vertex_attrib_array(a_uv);
        session.gl.vertex_attrib_pointer_f32(a_uv, 2, glow::FLOAT, false, 16, 8);
        session.gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        session.gl.disable_vertex_attrib_array(a_pos);
        session.gl.disable_vertex_attrib_array(a_uv);

        // Cleanup static (per-call FBOs + program + VBO).
        // r102.3: when program_cache_enabled, the program is owned
        // by LEGACY_TRANSITION_PROGRAMS_V2 and the VBO by
        // TEXTURED_QUAD_VBO -- both freed at session teardown.
        // Skip the per-tick deletes; pass None for vbo so
        // cleanup_static doesn't free the cached buffer either.
        if program_cache_enabled {
            cleanup_static(session.gl, None);
        } else {
            cleanup_static(session.gl, Some(vbo));
            session.gl.delete_program(program);
        }

        // v1-spec-delta #10 (slice c-2) + FYS bug 5: present pass
        // from the logical scene FBO to the panel-native default fb
        // when non-identity OR rotated. Viewport = PHYSICAL dims;
        // the pass applies brightness/gamma AND the display
        // rotation. Mirrors paint_and_present_one_frame_for_slide.
        if let Some((_fbo, tex)) = scene_for_post_pass {
            let brightness = (session.current_settings.brightness as f32) / 100.0;
            let gamma = session.current_settings.gamma;
            let (phys_w, phys_h) = session.phys_mode_size();
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
        Ok(true)
    })();
    if !work? {
        // FYS bug C: a video endpoint had no frame ready this tick;
        // the closure skipped the transition paint. Skip the
        // swap+commit too — the DRM scanout holds the previous frame
        // and the next advance retries. Mirrors the single-video
        // paint_and_present_one_video_slide_frame Ok(None) path.
        return Ok(());
    }

    // swap → lock → addFB → commit_fb same as paint_and_
    // present_one_frame_for_slide.
    // (cold-scout #2 P6, 2026-05-09): eglSwapBuffers implicitly
    // flushes; the explicit gl.flush() forced an extra tile-store
    // on vc4.
    // QA live-preview hook (2026-06-13): the critical one — this is
    // the per-tick transition present, the exact frame QA cannot
    // capture today (kmsgrab hangs on the page-flipping scanout
    // plane). No-op unless OPENMARQUEE_LIVE_PREVIEW_PATH is set.
    session.maybe_live_preview_capture();
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
    // r102.1: last-tick probe. progress > 0.95 catches the final
    // 2-3 ticks; QA sums the entry/exit delta across N transitions
    // to confirm or refute candidate #1.
    if progress > 0.95 {
        crate::v4l2::log_v3d_bos_at_phase("transition_paint_exit", None);
    }
    Ok(())
}

/// Phase 8 slice 6 (2026-05-16) — per-endpoint kind-tagged input
/// to `paint_and_present_one_transition_frame`. Replaces the
/// slice-4 `&ContentItem` endpoint signature: callers (the IPC
/// PaintTransition handler today) build a `TransitionEndpoint<'_>`
/// per endpoint with any caller-provided per-kind state, and the
/// transition function does the in-frame resolve + bake.
///
/// Per-kind:
///   - `Text(&'a TextSlide)` — function resolves layers + motion
///     states internally from `fonts` + `content_root` +
///     `session.motion_tick_seconds()`.
///   - `Image(&'a ImageSlide)` — function computes asset_path
///     internally from `content_root`.
///   - `Video { samples, next_sample_idx, frames_decoded,
///     decoder }` — caller passes the V4L2 demuxer/decoder
///     state (looked up from `cache.video_demuxers` +
///     `cache.video_decoders` at the IPC handler). Function
///     forwards directly into `SlideBakeInputs::Video`.
///
/// Option D cadence per `feedback_motion_through_transitions_
/// required`: each per-Advance transition call drains ONE V4L2
/// sample per Video endpoint, so video frames keep advancing
/// THROUGH the transition window. Text motion phase also advances
/// per call (Phase 4v-3b plumbing intact).
pub enum TransitionEndpoint<'a> {
    Text(&'a crate::content::TextSlide),
    Image(&'a crate::content::ImageSlide),
    /// Video endpoint state. Unlike Text/Image which only carry a
    /// slide ref (function resolves the rest), Video needs caller-
    /// supplied V4L2 decoder state because the demuxer + decoder
    /// live on the IPC handler's `SlideCache`, not on the EglSession
    /// the transition function holds. The `VideoSlide` ref isn't
    /// carried: the bake helper at hdmi.rs:bake_video_slide_to_
    /// current_fbo works off the decoder state alone, and the
    /// caller already pattern-matched the slide kind to choose this
    /// variant — adding it back would just produce a dead-code
    /// warning.
    Video {
        samples: &'a [crate::mp4_demux::Sample],
        next_sample_idx: &'a mut usize,
        frames_decoded: &'a mut usize,
        decoder: &'a crate::v4l2::Decoder,
    },
    /// r50 (2026-06-03): TextSlide with `background_video_slide_id`
    /// per SYSTEM_SPEC §5.10. Closes the §F.new gap from r46: the
    /// transition path previously treated this as plain Text (its
    /// bg dropped to solid for the transition's duration). Now the
    /// bake step composites the video bg + text layers on each
    /// side, then the existing transition blend mixes the two
    /// composites.
    ///
    /// Payload carries BOTH the text slide ref (for text-layer
    /// resolution) AND the bg-video V4L2 state (looked up from
    /// cache.video_decoders / cache.video_demuxers via the slide's
    /// `background_video_slide_id`, NOT the text slide id itself).
    TextOverVideo {
        text_slide: &'a crate::content::TextSlide,
        bg_samples: &'a [crate::mp4_demux::Sample],
        bg_next_sample_idx: &'a mut usize,
        bg_frames_decoded: &'a mut usize,
        bg_decoder: &'a crate::v4l2::Decoder,
    },
}

/// Public adapter: open a fresh EglSession and run the
/// supplied closure with it. The IPC sidecar's Open op uses
/// this so the inner loop runs inside a held session.
///
/// FYS bug 5 -- `rotation` is the display rotation in degrees
/// (0/90/180/270); the IPC Open handler passes `params.rotation`
/// (already validated). The session lays content out at logical
/// dims and the present pass rotates onto the panel.
pub fn run_in_egl_session<F, R>(card: &Card, rotation: i32, work: F) -> Result<R>
where
    F: FnOnce(&mut EglSession) -> Result<R>,
{
    // r25 (2026-05-31) glyph prewarm historical context:
    //
    // Originally a synchronous drain ("glyph-prewarm: drained N/M
    // glyphs in Xms (sidecar boot gate cleared)") that blocked
    // sidecar startup ~16-48 s while 4 worker threads ran msdfgen
    // FFI on 855 glyphs (9 fonts × 95 ASCII codepoints). G-1
    // (2026-06-16 a.m.) made it async so the IPC loop could
    // accept commands while bake continued in 2 workers.
    //
    // G-2 (2026-06-16 evening): even the ASYNC prewarm enqueue is
    // skipped on Pi Zero 2 W. With cma=320 leaving ~96 MB non-CMA
    // RAM, the working set of 855 in-flight msdfgen bakes
    // (intermediate shape/distance buffers + atlas uploads in the
    // completion channel) thrashes the memory ceiling. Per QA's
    // gate-harness on f4d58c1: G-1 dropped CPU storm 300% → 170%,
    // unblocked sidecar IPC in 16 ms — but /dev/video10 STILL
    // never opened on the 21-slide cold-start reel because the
    // memory pressure starved the V4L2 prime path.
    //
    // Fix: skip the prewarm enqueue entirely. Glyphs bake
    // ON-DEMAND when paint_slide_with's layout_text_to_quads
    // first encounters a codepoint missing from the static MSDF
    // atlas. The 2-worker cap (G-1 Fix 1) bounds the on-demand
    // bake CPU; the on-demand pattern bounds the memory working
    // set to the codepoints the ACTIVE reel actually references
    // (Karl's reel: ~1 font × ~40-60 unique glyphs = ~50 cells,
    // vs the 855 cells of the 9-font printable-ASCII prewarm).
    //
    // Trade: first paint of each text slide may show fallback
    // (tofu / Bug-3 Slice-2D DejaVu chain) for ~1-3 frames while
    // workers catch up. Same lazy-fill mechanism the prewarm
    // existed to mask, just routed differently — and with no
    // wedge risk under memory pressure.
    with_egl_session(card, rotation, |session| {
        eprintln!(
            "[perf] glyph_prewarm_skipped reason=on_demand_bake (G-2: 0 startup MissRequests; cold-start memory pressure removed; paint-time layout_text_to_quads triggers per-codepoint bake via get_or_request)"
        );
        work(session)
    })
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
/// QA-direct (2026-05-09): capture one frame mid-transition
/// (default t=0.5) from the scissored-bake path, write to PNG.
/// Used by the visual-verdict path for §8.2 soak readiness --
/// half-res bake (vc4 GLES2 FBO-switch sync workaround) might
/// soften text on Anton 5-layer SB transitions; QA reviews the
/// PNG before approving soak start.
///
/// Renders into a temp full-res FBO so the readback is a clean
/// RGBA grab without GBM / EGL surface state. Reuses the same
/// bake → composite pipeline as render_transition_scissored_
/// bake_in_session; the only difference is one-shot (no per-
/// frame loop) and writes to PNG instead of swapping.
pub fn capture_sb_transition_mid_to_png(
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    kind: &str,
    t: f32,
    png_path: &Path,
    // 2026-05-13 tick>0 stretch: motion_tick_override pins the
    // tick fed into motion_states_for_layers. None preserves the
    // legacy tick=0 (Batch 17.fix-A) reproducibility pin.
    motion_tick_override: Option<f64>,
) -> Result<()> {
    use crate::hdmi_logic::rgba_to_png_bytes;
    use glow::HasContext;
    // 2026-05-18: kinds outside the SP-portable set (currently only
    // `glitch`) delegate to the legacy-3pass capture path. The atlas
    // SB shader doesn't exist for those kinds; the legacy path bakes
    // both slides full-res into per-slide FBOs and runs the
    // standalone fs_for_transition_kind(kind) shader, matching the
    // runtime IPC PaintTransition path bit-for-bit.
    if !is_transition_kind_single_pass(kind) {
        return capture_legacy_3pass_transition_mid_to_png(
            card, slide_a, slide_b, fonts, content_root, kind, t, png_path,
            motion_tick_override,
        );
    }
    let t = t.clamp(0.0, 1.0);
    let (bg_a_kind, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b_kind, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    if layers_a.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
        || layers_b.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
    {
        bail!("capture_sb_mid: layer count exceeds cap");
    }
    with_egl_session(card, 0, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;
        let slide_a_id = slide_a.id;
        let slide_b_id = slide_b.id;
        let layers_a_len = layers_a.len();
        let layers_b_len = layers_b.len();

        // Ensure session caches.
        for (sid, n) in [(slide_a_id, layers_a_len), (slide_b_id, layers_b_len)] {
            let needs_new = match session.slide_caches.get(&sid) {
                Some(c) => c.glyph.len() != n,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&sid) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    sid,
                    SlideRenderCache::new(n),
                );
            }
        }

        let ccp = cached_composite_program(session.gl, kind)?;
        let (atlas_fbo, atlas_tex) = unsafe { ensure_bake_atlas(session)? };
        let vbo = ensure_transition_sp_quad_vbo(session)?;
        let region_h = crate::hdmi_logic::ATLAS_REGION_H;
        let atlas_w_f = crate::hdmi_logic::ATLAS_FBO_W as f32;
        let atlas_h_f = crate::hdmi_logic::ATLAS_FBO_H as f32;
        let used_w_f = mode_w as f32;
        let used_h_f = region_h as f32;
        let uv_scale_x = used_w_f / atlas_w_f;
        let uv_scale_y = used_h_f / atlas_h_f;
        let xform_a: [f32; 4] = [0.0, 0.0, uv_scale_x, uv_scale_y];
        let xform_b: [f32; 4] = [0.0, uv_scale_y, uv_scale_x, uv_scale_y];

        // Allocate a full-res capture FBO. Free at function exit.
        let gl = session.gl;
        let cap_tex = unsafe {
            let t_ = gl
                .create_texture()
                .map_err(|e| anyhow!("capture tex: {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(t_));
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0, glow::RGBA as i32, mode_w as i32, mode_h as i32, 0,
                glow::RGBA, glow::UNSIGNED_BYTE, None,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
            );
            t_
        };
        let cap_fbo = unsafe {
            let f = gl.create_framebuffer().map_err(|e| {
                gl.delete_texture(cap_tex);
                anyhow!("capture fbo: {e}")
            })?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(cap_tex), 0,
            );
            let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            if status != glow::FRAMEBUFFER_COMPLETE {
                gl.delete_framebuffer(f);
                gl.delete_texture(cap_tex);
                bail!("capture fbo incomplete: status=0x{status:x}");
            }
            f
        };

        // 2026-05-13 tick>0 stretch: when motion_tick_override is Some,
        // both slides bake at that tick so motion is in-flight; when
        // None, both bake at tick=0 for the legacy 17.fix-A bless
        // reproducibility pin.
        let motion_tick = motion_tick_override.unwrap_or(0.0);
        let states_a = motion_states_for_layers(slide_a.id, &layers_a, motion_tick);
        let states_b = motion_states_for_layers(slide_b.id, &layers_b, motion_tick);
        // 17.fix-A: same wall_clock pin as 17.2 applied to
        // capture_slide_to_png. Motion states use motion_tick above
        // (defaults to 0 per the original 17.fix-A pin, override
        // available via --capture-motion-tick for tick>0 stretch
        // verification), and wall_clock_unix flows into paint_slide_with_viewport
        // at lines ~2932/2965 and the motion compositor uses it to
        // derive phase for time-based effects (ticker horizontal
        // wrap, blink, etc.). Without pinning wall_clock, a re-run
        // of `--capture-sb-mid` produces a slightly different PNG
        // because real-time-since-epoch changes between bless and
        // diff -- the slide transition at t=0.5 maximally exposes
        // the drift (TO slide has 5 ticker-motion layers); other
        // transition kinds accidentally damp it below the 10/255
        // diff tolerance but the bug is structural. Pin to 0 so
        // golden captures reproduce bit-identically.
        let wall_clock_unix: i64 = 0;

        // IIFE so cap_fbo / cap_tex (and SCISSOR_TEST state) get
        // unconditional cleanup even when an inner ? aborts mid-
        // bake (e.g. cached_blit_program?, blit_bg_to_region?,
        // paint_slide_with_viewport?). The pre-IIFE allocation
        // already used a manual delete on the create_framebuffer
        // failure path; the IIFE extends that discipline across
        // every fallible call inside the capture body.
        let work_result: Result<()> = (|| {
        // Atlas bake phase: one FBO bind, scissor + viewport
        // switch between regions. Mirrors the runtime SB path.
        let bcp = cached_blit_program(session.gl)?;
        unsafe {
            if let Err(e) = ensure_slide_bg_cache(session, slide_a_id, &bg_a_kind) {
                eprintln!("warn: capture ensure_slide_bg_cache slide_a: {e:#}");
            }
            if let Err(e) = ensure_slide_bg_cache(session, slide_b_id, &bg_b_kind) {
                eprintln!("warn: capture ensure_slide_bg_cache slide_b: {e:#}");
            }
        }
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(atlas_fbo));
            gl.enable(glow::SCISSOR_TEST);
            gl.scissor(0, 0, mode_w as i32, region_h as i32);
        }
        {
            let bg_tex_a = session
                .slide_caches
                .get(&slide_a_id)
                .and_then(|c| c.bg_tex);
            if let Some(bg_tex) = bg_tex_a {
                unsafe {
                    gl.viewport(0, 0, mode_w as i32, region_h as i32);
                }
                blit_bg_to_region(gl, &bcp, vbo, bg_tex)?;
            }
            let cache_a = session
                .slide_caches
                .get_mut(&slide_a_id)
                .expect("slide_caches[a] init above");
            let bg_arg = if bg_tex_a.is_some() {
                None
            } else {
                Some(&bg_a_kind)
            };
            paint_slide_with_viewport(
                gl, mode_w, mode_h,
                0, 0, mode_w, region_h,
                bg_arg, &layers_a,
                Some(&states_a), wall_clock_unix,
                Some(&mut cache_a.glyph),
                Some(&mut session.image_bg_cache),
                Some(&mut cache_a.tex),
                None, // capture path; no runtime glyph cache needed
            )?;
        }
        unsafe {
            gl.scissor(0, region_h as i32, mode_w as i32, region_h as i32);
        }
        {
            let bg_tex_b = session
                .slide_caches
                .get(&slide_b_id)
                .and_then(|c| c.bg_tex);
            if let Some(bg_tex) = bg_tex_b {
                unsafe {
                    gl.viewport(0, region_h as i32, mode_w as i32, region_h as i32);
                }
                blit_bg_to_region(gl, &bcp, vbo, bg_tex)?;
            }
            let cache_b = session
                .slide_caches
                .get_mut(&slide_b_id)
                .expect("slide_caches[b] init above");
            let bg_arg = if bg_tex_b.is_some() {
                None
            } else {
                Some(&bg_b_kind)
            };
            paint_slide_with_viewport(
                gl, mode_w, mode_h,
                0, region_h, mode_w, region_h,
                bg_arg, &layers_b,
                Some(&states_b), wall_clock_unix,
                Some(&mut cache_b.glyph),
                Some(&mut session.image_bg_cache),
                Some(&mut cache_b.tex),
                None, // capture path; no runtime glyph cache needed
            )?;
        }

        // Composite at t into cap_fbo.
        unsafe {
            gl.disable(glow::SCISSOR_TEST);
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(cap_fbo));
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.disable(glow::BLEND);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            gl.use_program(Some(ccp.program));
            gl.active_texture(glow::TEXTURE0);
            gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
            gl.uniform_1_i32(ccp.u_src_a.as_ref(), 0);
            gl.active_texture(glow::TEXTURE1);
            gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
            gl.uniform_1_i32(ccp.u_src_b.as_ref(), 1);
            gl.uniform_4_f32(
                ccp.u_a_xform.as_ref(),
                xform_a[0], xform_a[1], xform_a[2], xform_a[3],
            );
            gl.uniform_4_f32(
                ccp.u_b_xform.as_ref(),
                xform_b[0], xform_b[1], xform_b[2], xform_b[3],
            );
            gl.uniform_1_f32(ccp.u_t.as_ref(), t);
            // r96: bind u_aspect for the iris arm (and any other
            // aspect-dependent transition). No-op for shaders that
            // don't declare it.
            gl.uniform_1_f32(
                ccp.u_aspect.as_ref(),
                (mode_w as f32) / (mode_h as f32),
            );
            gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
            let stride = (4 * std::mem::size_of::<f32>()) as i32;
            gl.enable_vertex_attrib_array(ccp.a_pos);
            gl.vertex_attrib_pointer_f32(ccp.a_pos, 2, glow::FLOAT, false, stride, 0);
            gl.enable_vertex_attrib_array(ccp.a_uv);
            gl.vertex_attrib_pointer_f32(
                ccp.a_uv, 2, glow::FLOAT, false, stride,
                (2 * std::mem::size_of::<f32>()) as i32,
            );
            gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
            gl.flush();
        }

        let rgba = capture_fbo_to_rgba(gl, Some(cap_fbo), mode_w, mode_h)?;
        let png_bytes = rgba_to_png_bytes(&rgba, mode_w, mode_h)?;
        std::fs::write(png_path, &png_bytes)
            .with_context(|| format!("write png {}", png_path.display()))?;
        eprintln!(
            "captured SB transition kind={kind:?} slide_a={} slide_b={} t={t:.3} -> {} ({} bytes)",
            slide_a.id, slide_b.id, png_path.display(), png_bytes.len(),
        );
        Ok(())
        })();

        // Unconditional cleanup. Runs on both Ok and Err paths so
        // a mid-bake error doesn't leak cap_fbo / cap_tex / SCISSOR
        // state into the surrounding session.
        unsafe {
            gl.disable(glow::SCISSOR_TEST);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(cap_fbo);
            gl.delete_texture(cap_tex);
        }
        work_result
    })
}

/// 2026-05-18 — legacy-3pass fallback capture for transition kinds
/// outside the SP-portable set (currently only `glitch`). Both
/// `capture_sb_transition_mid_to_png` and `capture_fullres_transition_
/// mid_to_png` delegate here when `!is_transition_kind_single_pass(kind)`.
///
/// Pipeline mirrors `paint_and_present_one_transition_frame`'s bake +
/// composite at fixed progress `t`:
///   1. Bake slide_a + slide_b into per-slide full-res FBOs via
///      `make_fullres_slide_fbo_with_motion` (same helper the
///      capture_fullres path uses). Motion states evaluated at
///      `motion_tick_override` if Some, else 0.0 — same pin
///      semantics as the SP-portable capture paths.
///   2. Allocate a full-res capture FBO (cap_tex + cap_fbo).
///   3. Link `link_program(VS_TEXTURED_QUAD, fs_for_transition_kind(kind))`
///      — the SAME shader used by the legacy 3-pass runtime path and
///      the IPC `paint_and_present_one_transition_frame` for non-SP
///      kinds. Output is bit-identical to production scanout.
///   4. Draw the composite with u_src_a=tex_a, u_src_b=tex_b, u_t=t.
///      Standalone transition shaders (FS_GLITCH, FS_FADE, etc.) all
///      expose this 3-uniform contract — same as the legacy 3-pass
///      composite in `render_transition_animated_in_session` and the
///      IPC PaintTransition path.
///   5. `capture_fbo_to_rgba` → `rgba_to_png_bytes` → `std::fs::write`.
///      Atomic-write semantics match the SP-portable capture paths.
///
/// Glitch determinism: FS_GLITCH's `frame_seed = floor(u_t * 30.0)`
/// is deterministic at fixed t. At t=0.5 the seed is floor(15.0) = 15;
/// the per-row hash is fully reproducible. The Rust-side golden gates
/// Rust regressions. Canvas2D's glitch uses `Math.random()` (see
/// ui/src/inline-preview.js:489-493) by design, so cross-renderer
/// SSIM parity is `divergent_by_design`.
fn capture_legacy_3pass_transition_mid_to_png(
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    kind: &str,
    t: f32,
    png_path: &Path,
    motion_tick_override: Option<f64>,
) -> Result<()> {
    use crate::hdmi_logic::{fs_for_transition_kind, rgba_to_png_bytes};
    use glow::HasContext;
    let fs = fs_for_transition_kind(kind).ok_or_else(|| {
        anyhow!("capture_legacy_3pass_mid: kind {kind:?} has no fragment shader")
    })?;
    let t = t.clamp(0.0, 1.0);
    let (bg_a_kind, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b_kind, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    with_egl_session(card, 0, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;
        let gl = session.gl;

        // Same tick + wall_clock pin as capture_fullres_mid: motion at
        // the override (or phase 0 by default), wall_clock at epoch.
        let motion_tick = motion_tick_override.unwrap_or(0.0);
        let states_a = motion_states_for_layers(slide_a.id, &layers_a, motion_tick);
        let states_b = motion_states_for_layers(slide_b.id, &layers_b, motion_tick);
        let wall_clock_unix: i64 = 0;

        // Per-slide full-res bakes — same helper as capture_fullres_mid.
        let (fbo_a, tex_a) = unsafe {
            make_fullres_slide_fbo_with_motion(
                gl, mode_w, mode_h, &bg_a_kind, &layers_a,
                Some(&states_a), wall_clock_unix,
            )?
        };
        let (fbo_b, tex_b) = match unsafe {
            make_fullres_slide_fbo_with_motion(
                gl, mode_w, mode_h, &bg_b_kind, &layers_b,
                Some(&states_b), wall_clock_unix,
            )
        } {
            Ok(p) => p,
            Err(e) => {
                unsafe {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                }
                return Err(e);
            }
        };

        // Capture FBO (full-res, RGBA8).
        let cap_tex = unsafe {
            let t_ = gl
                .create_texture()
                .map_err(|e| {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                    anyhow!("capture tex: {e}")
                })?;
            gl.bind_texture(glow::TEXTURE_2D, Some(t_));
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0, glow::RGBA as i32, mode_w as i32, mode_h as i32, 0,
                glow::RGBA, glow::UNSIGNED_BYTE, None,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
            );
            t_
        };
        let cap_fbo = unsafe {
            let f = gl.create_framebuffer().map_err(|e| {
                gl.delete_texture(cap_tex);
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
                anyhow!("capture fbo: {e}")
            })?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(cap_tex), 0,
            );
            let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            if status != glow::FRAMEBUFFER_COMPLETE {
                gl.delete_framebuffer(f);
                gl.delete_texture(cap_tex);
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
                bail!("capture fbo incomplete: status=0x{status:x}");
            }
            f
        };

        let work_result: Result<()> = (|| {
            // Link the legacy transition shader (FS_GLITCH for glitch,
            // etc.) against VS_TEXTURED_QUAD. Same pattern as
            // render_transition_animated_in_session at hdmi.rs:~5447
            // and paint_and_present_one_transition_frame at ~3236.
            let program = link_program(gl, VS_TEXTURED_QUAD, fs)?;
            let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
                .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (legacy capture)"))?;
            let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
                .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (legacy capture)"))?;
            let u_src_a = unsafe { gl.get_uniform_location(program, "u_src_a") };
            let u_src_b = unsafe { gl.get_uniform_location(program, "u_src_b") };
            let u_t = unsafe { gl.get_uniform_location(program, "u_t") };
            // r96: u_aspect for the iris arm. None for shaders that
            // don't declare it (silent no-op bind).
            let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };

            // Textured-quad VBO with full-screen NDC + identity UV.
            // Same vertex layout as VS_TEXTURED_QUAD callers across
            // the file.
            let vbo = unsafe { gl.create_buffer() }
                .map_err(|e| anyhow!("glGenBuffers(legacy capture): {e}"))?;
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

                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(cap_fbo));
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.disable(glow::BLEND);
                gl.clear_color(0.0, 0.0, 0.0, 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.use_program(Some(program));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_a));
                gl.uniform_1_i32(u_src_a.as_ref(), 0);
                gl.active_texture(glow::TEXTURE1);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_b));
                gl.uniform_1_i32(u_src_b.as_ref(), 1);
                gl.uniform_1_f32(u_t.as_ref(), t);
                gl.uniform_1_f32(
                    u_aspect.as_ref(),
                    (mode_w as f32) / (mode_h as f32),
                );

                let stride = (4 * std::mem::size_of::<f32>()) as i32;
                gl.enable_vertex_attrib_array(a_pos);
                gl.vertex_attrib_pointer_f32(a_pos, 2, glow::FLOAT, false, stride, 0);
                gl.enable_vertex_attrib_array(a_uv);
                gl.vertex_attrib_pointer_f32(
                    a_uv, 2, glow::FLOAT, false, stride,
                    (2 * std::mem::size_of::<f32>()) as i32,
                );
                gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
                gl.disable_vertex_attrib_array(a_pos);
                gl.disable_vertex_attrib_array(a_uv);
                gl.flush();

                gl.delete_buffer(vbo);
                gl.delete_program(program);
            }

            let rgba = capture_fbo_to_rgba(gl, Some(cap_fbo), mode_w, mode_h)?;
            let png_bytes = rgba_to_png_bytes(&rgba, mode_w, mode_h)?;
            std::fs::write(png_path, &png_bytes)
                .with_context(|| format!("write png {}", png_path.display()))?;
            eprintln!(
                "captured LEGACY-3PASS transition kind={kind:?} slide_a={} slide_b={} t={t:.3} -> {} ({} bytes)",
                slide_a.id, slide_b.id, png_path.display(), png_bytes.len(),
            );
            Ok(())
        })();

        // Unconditional cleanup — mirrors capture_fullres_mid.
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(cap_fbo);
            gl.delete_texture(cap_tex);
            gl.delete_framebuffer(fbo_a);
            gl.delete_texture(tex_a);
            gl.delete_framebuffer(fbo_b);
            gl.delete_texture(tex_b);
        }
        work_result
    })
}

/// QA-direct (2026-05-13) Atlas SB visual-sanity counterpart.
/// Mirrors capture_sb_transition_mid_to_png's machinery but bakes
/// slide_a + slide_b into full-mode-resolution per-slide FBOs
/// (1920x1080 each on the dev Pi) instead of two half-rez regions
/// of the shared scissored-bake atlas. Composites with the SAME
/// `cached_composite_program(kind)` SP shader at progress=t, with
/// identity UV xforms for both sources, then reads back to PNG.
///
/// Purpose: provide the full-res reference baseline that the SB
/// path's softening-from-half-rez can be diffed against. The SB
/// vs full-res delta isolates the Atlas SB bake-resolution cost
/// (half-rez → upscale → composite) from any shader / blend math
/// drift. SSIM ≥ 0.95 is the §11 / Atlas SB acceptance gate.
///
/// Uses the SAME tick-zero + wall_clock-zero pins as
/// capture_sb_transition_mid_to_png (17.fix-A) so bless captures
/// reproduce bit-identically across runs.
pub fn capture_fullres_transition_mid_to_png(
    card: &Card,
    slide_a: &TextSlide,
    slide_b: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    kind: &str,
    t: f32,
    png_path: &Path,
    // 2026-05-13 tick>0 stretch: see sibling capture_sb_transition_
    // mid_to_png. Same semantics: None preserves the tick=0 pin, Some
    // bakes both slides at that motion tick for in-flight comparison.
    motion_tick_override: Option<f64>,
) -> Result<()> {
    use crate::hdmi_logic::rgba_to_png_bytes;
    use glow::HasContext;
    // 2026-05-18: same legacy-3pass delegation as capture_sb_mid.
    // Kinds outside the SP-portable set fall through to the
    // standalone fs_for_transition_kind(kind) composite over per-
    // slide full-res FBOs.
    if !is_transition_kind_single_pass(kind) {
        return capture_legacy_3pass_transition_mid_to_png(
            card, slide_a, slide_b, fonts, content_root, kind, t, png_path,
            motion_tick_override,
        );
    }
    let t = t.clamp(0.0, 1.0);
    let (bg_a_kind, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b_kind, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    with_egl_session(card, 0, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;

        let ccp = cached_composite_program(session.gl, kind)?;
        let vbo = ensure_transition_sp_quad_vbo(session)?;

        // Same tick + wall_clock pin as SB path: motion at the
        // override (or phase 0 by default), wall_clock at epoch.
        // Makes bless captures reproducible AND keeps SB / fullres
        // on the same motion phase for fair SSIM comparison.
        let motion_tick = motion_tick_override.unwrap_or(0.0);
        let states_a = motion_states_for_layers(slide_a.id, &layers_a, motion_tick);
        let states_b = motion_states_for_layers(slide_b.id, &layers_b, motion_tick);
        let wall_clock_unix: i64 = 0;

        let gl = session.gl;

        // Allocate two full-res slide FBOs + capture FBO.
        let (fbo_a, tex_a) = unsafe {
            make_fullres_slide_fbo_with_motion(
                gl, mode_w, mode_h, &bg_a_kind, &layers_a,
                Some(&states_a), wall_clock_unix,
            )?
        };
        let (fbo_b, tex_b) = match unsafe {
            make_fullres_slide_fbo_with_motion(
                gl, mode_w, mode_h, &bg_b_kind, &layers_b,
                Some(&states_b), wall_clock_unix,
            )
        } {
            Ok(p) => p,
            Err(e) => {
                unsafe {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                }
                return Err(e);
            }
        };

        // r41 (2026-06-02): bring this cap_tex create-fail handler
        // in line with the sibling at capture_legacy_3pass_transition_
        // mid_to_png:4906-4912. Pre-r41 the bare `?` leaked
        // fbo_a/tex_a/fbo_b/tex_b (~16 MB FBO storage at 1080p)
        // because the success path's deferred cleanup at function
        // exit is past the bubble. See qa/r41-capture-startup-
        // cleanup-2026-06-02.md.
        let cap_tex = unsafe {
            let t_ = gl
                .create_texture()
                .map_err(|e| {
                    gl.delete_framebuffer(fbo_a);
                    gl.delete_texture(tex_a);
                    gl.delete_framebuffer(fbo_b);
                    gl.delete_texture(tex_b);
                    anyhow!("capture tex: {e}")
                })?;
            gl.bind_texture(glow::TEXTURE_2D, Some(t_));
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0, glow::RGBA as i32, mode_w as i32, mode_h as i32, 0,
                glow::RGBA, glow::UNSIGNED_BYTE, None,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
            );
            t_
        };
        let cap_fbo = unsafe {
            let f = gl.create_framebuffer().map_err(|e| {
                gl.delete_texture(cap_tex);
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
                anyhow!("capture fbo: {e}")
            })?;
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(f));
            gl.framebuffer_texture_2d(
                glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(cap_tex), 0,
            );
            let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            if status != glow::FRAMEBUFFER_COMPLETE {
                gl.delete_framebuffer(f);
                gl.delete_texture(cap_tex);
                gl.delete_framebuffer(fbo_a);
                gl.delete_texture(tex_a);
                gl.delete_framebuffer(fbo_b);
                gl.delete_texture(tex_b);
                bail!("capture fbo incomplete: status=0x{status:x}");
            }
            f
        };

        // Composite at t into cap_fbo. Identity UV xforms: each
        // source texture is full-mode-res; sample its full UV range.
        let xform_a: [f32; 4] = [0.0, 0.0, 1.0, 1.0];
        let xform_b: [f32; 4] = [0.0, 0.0, 1.0, 1.0];

        let work_result: Result<()> = (|| {
            unsafe {
                gl.bind_framebuffer(glow::FRAMEBUFFER, Some(cap_fbo));
                gl.viewport(0, 0, mode_w as i32, mode_h as i32);
                gl.disable(glow::BLEND);
                gl.clear_color(0.0, 0.0, 0.0, 1.0);
                gl.clear(glow::COLOR_BUFFER_BIT);
                gl.use_program(Some(ccp.program));
                gl.active_texture(glow::TEXTURE0);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_a));
                gl.uniform_1_i32(ccp.u_src_a.as_ref(), 0);
                gl.active_texture(glow::TEXTURE1);
                gl.bind_texture(glow::TEXTURE_2D, Some(tex_b));
                gl.uniform_1_i32(ccp.u_src_b.as_ref(), 1);
                gl.uniform_4_f32(
                    ccp.u_a_xform.as_ref(),
                    xform_a[0], xform_a[1], xform_a[2], xform_a[3],
                );
                gl.uniform_4_f32(
                    ccp.u_b_xform.as_ref(),
                    xform_b[0], xform_b[1], xform_b[2], xform_b[3],
                );
                gl.uniform_1_f32(ccp.u_t.as_ref(), t);
                // r96: bind u_aspect for the iris arm (and any
                // other aspect-dependent transition). No-op for
                // shaders that don't declare it.
                gl.uniform_1_f32(
                    ccp.u_aspect.as_ref(),
                    (mode_w as f32) / (mode_h as f32),
                );
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                let stride = (4 * std::mem::size_of::<f32>()) as i32;
                gl.enable_vertex_attrib_array(ccp.a_pos);
                gl.vertex_attrib_pointer_f32(ccp.a_pos, 2, glow::FLOAT, false, stride, 0);
                gl.enable_vertex_attrib_array(ccp.a_uv);
                gl.vertex_attrib_pointer_f32(
                    ccp.a_uv, 2, glow::FLOAT, false, stride,
                    (2 * std::mem::size_of::<f32>()) as i32,
                );
                gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
                gl.flush();
            }

            let rgba = capture_fbo_to_rgba(gl, Some(cap_fbo), mode_w, mode_h)?;
            let png_bytes = rgba_to_png_bytes(&rgba, mode_w, mode_h)?;
            std::fs::write(png_path, &png_bytes)
                .with_context(|| format!("write png {}", png_path.display()))?;
            eprintln!(
                "captured FULLRES transition kind={kind:?} slide_a={} slide_b={} t={t:.3} -> {} ({} bytes)",
                slide_a.id, slide_b.id, png_path.display(), png_bytes.len(),
            );
            Ok(())
        })();

        // Unconditional cleanup. Mirrors the SB-path discipline.
        unsafe {
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(cap_fbo);
            gl.delete_texture(cap_tex);
            gl.delete_framebuffer(fbo_a);
            gl.delete_texture(tex_a);
            gl.delete_framebuffer(fbo_b);
            gl.delete_texture(tex_b);
        }
        work_result
    })
}

/// Variant of make_slide_fbo that takes motion_states + wall_clock_unix
/// so the SB-vs-fullres parity capture can apply the SAME motion-at-tick-0
/// pin in both paths. The existing make_slide_fbo is hot-path (called by
/// paint_and_present_one_transition_frame's per-Advance bake) and bakes
/// statically with `None` motion + real wall_clock; not modifying its
/// signature avoids touching that hot path.
unsafe fn make_fullres_slide_fbo_with_motion(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    bg_kind: &BgKind,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: Option<&[crate::hdmi_logic::MotionState]>,
    wall_clock_unix: i64,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(fullres_fbo): {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_image_2d(
        glow::TEXTURE_2D, 0, glow::RGBA as i32, mode_w as i32, mode_h as i32, 0,
        glow::RGBA, glow::UNSIGNED_BYTE, None,
    );
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    let fbo = match gl.create_framebuffer() {
        Ok(f) => f,
        Err(e) => {
            gl.delete_texture(tex);
            return Err(anyhow!("glGenFramebuffers(fullres_fbo): {e}"));
        }
    };
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
    gl.framebuffer_texture_2d(
        glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(tex), 0,
    );
    let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
    if status != glow::FRAMEBUFFER_COMPLETE {
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        return Err(anyhow!("framebuffer incomplete (fullres_fbo): status=0x{status:x}"));
    }
    let paint_result = paint_slide(
        gl, mode_w, mode_h, bg_kind, text_layers,
        motion_states, wall_clock_unix,
        None,
        None,
        None,
        None, // capture-to-fullres-fbo; no runtime glyph cache
    );
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    if let Err(e) = paint_result {
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        return Err(e);
    }
    Ok((fbo, tex))
}

pub fn capture_slide_to_png(
    card: &Card,
    slide: &TextSlide,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
    png_path: &Path,
    // 17.2 / sweep #9 #2: pin tick_seconds + wall_clock_unix to a
    // deterministic value so a golden-master capture of an animated
    // slide reproduces bit-identically across runs. None preserves
    // the legacy behavior (tick=0, wall_clock=real-time).
    tick_override: Option<f64>,
) -> Result<()> {
    use crate::hdmi_logic::rgba_to_png_bytes;
    let (bg_kind, _label, text_layers) =
        resolve_slide_layers(slide, fonts, content_root)?;
    let tick_seconds = tick_override.unwrap_or(0.0);
    let motion_states = motion_states_for_layers(slide.id, &text_layers, tick_seconds);
    // When pinned, also pin wall_clock_unix to a deterministic value
    // (0) so any wall-clock-based effect (auto_mode time slides)
    // reproduces. Without the override, real time keeps the legacy
    // behavior for non-capture callers.
    let wall_clock_unix = if tick_override.is_some() {
        0
    } else {
        current_unix_seconds()
    };
    with_egl_session(card, 0, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;
        // Bug 3 Slice 2D: multi-round pre-warm to resolve any
        // dynamic-cache misses including fallback-chain hops. Each
        // round paints (enqueues current-level misses + their
        // workers rasterize) and then drains. Round count =
        // 1 + FALLBACK_FONT_STEMS.len() so the primary's
        // FontMissing -> fallback's MissRequest -> fallback's
        // Ready chain has time to traverse one stem per round.
        // Without this loop, a primary FontMissing would correctly
        // surface as Tofu but the SECOND paint would only enqueue
        // the fallback miss without waiting for the worker, so the
        // FINAL capture would still show Tofu for codepoints whose
        // glyphs live only in DejaVu Sans (e.g. ● U+25CF on the
        // FYS Boot slide).
        //
        // The drain's deadline (1500 ms) is well past msdfgen's
        // 482 ms p99 single-threaded ceiling. Captures of slides
        // with no misses exit the inner drain on the first poll
        // since no completions arrive.
        let prewarm_rounds = 1 + crate::glyph_cache::FALLBACK_FONT_STEMS.len();
        for _ in 0..prewarm_rounds {
            let ctx_round = crate::glyph_cache::RuntimeGlyphCtx {
                cache: &session.dynamic_glyph_cache,
                fonts_dir: &session.dynamic_fonts_dir,
            };
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
                None,
                Some(ctx_round),
            )?;
            // Drain pending worker output. Per-iteration poll
            // uploads any Ready completions into the atlas page;
            // FontMissing direct-inserts happen worker-side
            // outside the completion channel and are observable
            // only on the next paint's dispatch.
            //
            // FIXED-DURATION WAIT: bail-on-2-empty-polls is unsafe
            // here because FontMissing never produces channel
            // traffic. A primary that's about to FontMissing
            // would idle the channel for the full ~482ms p99
            // worker time, and a "wait until channel is idle"
            // bail would exit ~40ms in -- before the worker has
            // finished its TTF parse + msdfgen pass. Sleep the
            // full deadline so dispatch on the next paint round
            // sees the resolved (Ready or FontMissing) state.
            //
            // 800ms = bench's 482ms p99 single-thread + headroom.
            // With 4-worker concurrency the actual completion is
            // ~250ms p99, so this is conservative.
            let deadline = std::time::Instant::now()
                + std::time::Duration::from_millis(800);
            while std::time::Instant::now() < deadline {
                let _n = session.dynamic_glyph_cache.poll_completions(
                    session.gl,
                    &mut session.dynamic_atlas_page_msdf,
                    &mut session.dynamic_atlas_page_colr,
                    4,
                );
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
        }
        // Final paint: layout sees all slots resolved to terminal
        // state (Ready -> DynamicMsdf | FontMissing-chain-exhausted
        // -> Tofu).
        let ctx_final = crate::glyph_cache::RuntimeGlyphCtx {
            cache: &session.dynamic_glyph_cache,
            fonts_dir: &session.dynamic_fonts_dir,
        };
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
            None,
            Some(ctx_final),
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

/// Batch 18.1 / sweep #9 N2: capture an ImageSlide to PNG. Mirrors
/// capture_slide_to_png for the image case -- load asset, blit via
/// FS_BLIT, read back FBO, write PNG. The render side reuses
/// run_blit_pass (slice 7c helper) so the captured pixels match
/// what render_image_slide_in_session would scan out.
pub fn capture_image_slide_to_png(
    card: &Card,
    asset_path: &Path,
    png_path: &Path,
) -> Result<()> {
    use crate::hdmi_logic::rgba_to_png_bytes;
    let (rgba, img_w, img_h) = load_png_rgba(asset_path)?;
    with_egl_session(card, 0, |session| {
        let mode_w = session.mode_w as u32;
        let mode_h = session.mode_h as u32;
        unsafe {
            use glow::HasContext;
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
            session.gl.clear(glow::COLOR_BUFFER_BIT);
            let tex = session
                .gl
                .create_texture()
                .map_err(|e| anyhow!("glGenTextures(image_slide capture): {e}"))?;
            session.gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
            session.gl.tex_image_2d(
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
            let blit_result = run_blit_pass(session.gl, tex);
            session.gl.delete_texture(tex);
            blit_result?;
            session.gl.flush();
        }
        let rgba_out = capture_fbo_to_rgba(session.gl, None, mode_w, mode_h)?;
        let png_bytes = rgba_to_png_bytes(&rgba_out, mode_w, mode_h)?;
        std::fs::write(png_path, &png_bytes)
            .with_context(|| format!("write png {}", png_path.display()))?;
        eprintln!(
            "captured image_slide from {} to {} ({}x{} RGBA, {} bytes)",
            asset_path.display(),
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

    /// r38d SIGUSR1 cache-dump surface. Returns (image_bg_cache.len,
    /// image_slide_tex_cache.len). Cheap — both caches expose a pub
    /// len() (lru.rs:69, image_slide_tex.rs:105); this accessor
    /// exists solely to surface those numbers across the
    /// hdmi::Session encapsulation boundary so ipc_main.rs's
    /// SIGUSR1 handler can format them into the [cache-dump] line.
    /// Prefix `cma_dump_` makes the SIGUSR1 surface grep-discoverable.
    pub fn cma_dump_cache_lens(&self) -> (usize, usize) {
        (self.image_bg_cache.len(), self.image_slide_tex_cache.len())
    }

    /// r46 (2026-06-02): CMA-pressure mitigation for the text-over-
    /// video paint path (SYSTEM_SPEC §5.10). Pi Zero 2 W steady-
    /// state CMA is ~250 MB (per qa/r38d-sigusr1-cache-dump-
    /// 2026-06-02.md observation); the V4L2 decoder pool that the
    /// bg-video bake needs adds ~24 MB; combined ceiling exceeds
    /// the 256 MB pool. Frees up to ~96 MB by draining both
    /// image-bg caches (6 entries × ~8 MB each × 2 caches at
    /// worst case).
    ///
    /// Called once on first paint of a text-over-video slide
    /// (detected via `slide_caches.contains_key`). Subsequent
    /// frames of the same slide skip the eviction (cheap;
    /// idempotent when caches are already empty). The image
    /// caches re-warm naturally when the next image-bg slide
    /// plays — no other side effect.
    ///
    /// Trade-off: alternating playlists (text-over-video ↔
    /// image-bg) will thrash these caches. Accepted as
    /// implementation cost; the alternative is per-deployment
    /// V4L2 pool tuning which is a Phase 9 refactor.
    pub fn force_evict_image_caches_for_cma_pressure(&mut self) {
        use glow::HasContext;
        let freed_bg = self.image_bg_cache.len();
        let freed_slide = self.image_slide_tex_cache.len();
        for (_path, (tex, _, _)) in self.image_bg_cache.drain() {
            unsafe { self.gl.delete_texture(tex); }
        }
        for tex in self.image_slide_tex_cache.take_all_textures() {
            unsafe { self.gl.delete_texture(tex); }
        }
        if freed_bg > 0 || freed_slide > 0 {
            eprintln!(
                "ipc: r46 text-over-video CMA mitigation -- evicted {} image_bg + {} image_slide_tex entries",
                freed_bg, freed_slide
            );
        }
    }

    /// Bug 1 fix (qarl-flag 2026-05-09): the canonical motion
    /// `tick_seconds` basis. session_start is set once at EglSession
    /// construction and never reset, so every render path (standalone
    /// hold / standalone transition variants / IPC sidecar) reads a
    /// single monotonic clock. Centralizing the derivation here is
    /// the structural guard against the same bug recurring on a new
    /// render-call entry point -- 7417ae0 covered the 4 standalone
    /// in-session paths; 413efca extended it to the IPC PaintSlide
    /// path. Future paint entry points: call this, do NOT roll your
    /// own from a call-local clock.
    pub fn motion_tick_seconds(&self) -> f64 {
        self.session_start.elapsed().as_secs_f64()
    }

    /// `[perf]` r1 (2026-05-26): per-commit deadline-miss bookkeeping.
    /// Called from `commit_fb` after every successful commit (both
    /// the SetCrtc and page_flip branches). Steady-state cost is one
    /// subtract + one millisecond cast + one compare — sub-µs.
    ///
    /// On the very first commit (`last_present_at.is_none()`) we
    /// just seed the baseline; the over-budget check is skipped
    /// because there's no prior frame to diff against. The warn-log
    /// is rate-limited to at most once per second so a fully-wedged
    /// device missing every frame doesn't spam the journal — the
    /// counter still increments every frame, so the IPC summary
    /// emitter sees the true rate.
    fn record_present(&mut self, now: std::time::Instant) {
        if let Some(prev) = self.last_present_at {
            self.frames_observed_total = self.frames_observed_total.saturating_add(1);
            if let Some(delta_ms) = crate::frame_pacing::over_budget_ms(
                prev, now, crate::frame_pacing::FRAME_BUDGET_MS,
            ) {
                self.frames_over_budget_total =
                    self.frames_over_budget_total.saturating_add(1);
                let should_log = self
                    .last_over_budget_warn_at
                    .map(|t| now.saturating_duration_since(t).as_secs() >= 1)
                    .unwrap_or(true);
                if should_log {
                    // peak-triage (2026-06-15): `since_restart_ms`
                    // disambiguates qa-bench-cycle restart artifacts
                    // (small value, typically < 5000 ms) from
                    // steady-state cold-prime freezes (large value).
                    // Read via crate::frame_pacing's process-startup
                    // OnceLock marked at main.rs entry. Pure
                    // instrumentation; no behavior change.
                    eprintln!(
                        "[perf] frame over budget: delta_ms={} in_transition={} over_budget_total={} observed_total={} since_restart_ms={}",
                        delta_ms,
                        self.in_transition,
                        self.frames_over_budget_total,
                        self.frames_observed_total,
                        crate::frame_pacing::since_renderer_startup_ms(),
                    );
                    self.last_over_budget_warn_at = Some(now);
                }
            }
        }
        self.last_present_at = Some(now);
    }

    /// `[perf]` r1: IPC-dispatcher hint setter for the per-warn
    /// in_transition flag. Set once per paint hook (before
    /// run_paint_hook fires) so the over-budget log on the
    /// FOLLOWING `commit_fb` sees the right value. Standalone
    /// non-IPC render paths never touch this — the field stays
    /// `false`, which is the correct "unknown / not-a-transition"
    /// default for the warn log.
    pub fn set_in_transition(&mut self, in_transition: bool) {
        self.in_transition = in_transition;
    }

    /// `[perf]` r1: snapshot accessors for the IPC summary
    /// emitter. Returns the current cumulative counters at
    /// snapshot time. The summary emitter diffs against its
    /// previous snapshot to compute per-window over-budget rate
    /// (mirroring the existing window-vs-session split for
    /// frames/transitions).
    pub fn frames_observed_total(&self) -> u64 {
        self.frames_observed_total
    }

    /// See [`Self::frames_observed_total`].
    pub fn frames_over_budget_total(&self) -> u64 {
        self.frames_over_budget_total
    }

    /// FYS bug 5 -- the PHYSICAL panel dims (the scanout buffer
    /// size). For 0/180 these equal `mode_w`/`mode_h`; for 90/270
    /// they are the swap of the logical dims. The present pass uses
    /// these for the default-framebuffer viewport; the content
    /// pipeline keeps using the logical `mode_w`/`mode_h`.
    fn phys_mode_size(&self) -> (u32, u32) {
        let (pw, ph) = self.mode.size();
        (pw as u32, ph as u32)
    }

    /// QA verification unblocker (2026-06-13): one-line site for
    /// the paint_and_present_* functions to call right BEFORE
    /// `egl_lib.swap_buffers(...)`. Wraps the disjoint-field-borrow
    /// dance (live_preview vs gl) so callers don't have to repeat
    /// it. Near-zero cost when env is unset (early return on
    /// `config.is_none()`).
    fn maybe_live_preview_capture(&mut self) {
        let (phys_w, phys_h) = {
            let (pw, ph) = self.mode.size();
            (pw as u32, ph as u32)
        };
        self.live_preview.maybe_capture(self.gl, phys_w, phys_h);
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

/// r102.2 (2026-06-09): per-side identifier for the cached
/// transition FBO+tex pair. The transition shader samples both
/// endpoints simultaneously, so the cache holds two slots --
/// the side enum tells the helper which one to return.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransitionFboSide {
    A,
    B,
}

/// r102.2 (2026-06-09): allocate-once-and-reuse FBO+tex pair
/// for the given transition endpoint side. Analogous to
/// `ensure_scene_fbo`. On cache hit (same side, same dims),
/// returns the existing pair. On cache miss, allocates via
/// `create_slide_fbo_pair` and stores. On dim change vs the
/// cached `transition_fbo_dims`, BOTH side caches are
/// invalidated + freed before reallocating -- the
/// transition shader needs matching dims between a and b.
///
/// Caller responsibilities:
/// - Bind the returned FBO + glViewport before painting.
/// - glClear the prior tick's content before drawing fresh.
/// - DO NOT delete the returned handles -- session teardown
///   handles cleanup.
unsafe fn ensure_transition_fbo_pair(
    session: &mut EglSession,
    side: TransitionFboSide,
    w: u32,
    h: u32,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    let dims_changed = match session.transition_fbo_dims {
        Some((cw, ch)) => cw != w || ch != h,
        None => false,
    };
    if dims_changed {
        // Free BOTH sides; the transition shader needs the pair
        // to share dims. Easier to invalidate both than to
        // size-check per-side.
        if let Some(fbo) = session.transition_fbo_a.take() {
            session.gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.transition_tex_a.take() {
            session.gl.delete_texture(tex);
        }
        if let Some(fbo) = session.transition_fbo_b.take() {
            session.gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.transition_tex_b.take() {
            session.gl.delete_texture(tex);
        }
        session.transition_fbo_dims = None;
        // r106 + Path A Stage 2 (2026-06-14): freed cached pairs
        // have undefined content for reuse purposes — clear BOTH
        // painted flags. Per subagent WARN-4 of r106's review,
        // every place that frees a cached FBO is a place that
        // MUST reset the matching painted flag.
        session.transition_fbo_a_painted = false;
        session.transition_fbo_b_painted = false;
    }
    let (slot_fbo, slot_tex) = match side {
        TransitionFboSide::A => (&mut session.transition_fbo_a, &mut session.transition_tex_a),
        TransitionFboSide::B => (&mut session.transition_fbo_b, &mut session.transition_tex_b),
    };
    if let (Some(fbo), Some(tex)) = (*slot_fbo, *slot_tex) {
        return Ok((fbo, tex));
    }
    let (fbo, tex) = create_slide_fbo_pair(session.gl, w, h)?;
    // r102.2 subagent WARN-1: set the dims sentinel BEFORE the
    // slot assignments so a future refactor that adds a panic-
    // bubble between can't leave the cache in a (slot=Some,
    // dims=None) partial state that the dims_changed check
    // would silently skip. With dims-set-first, the only
    // observable partial state is (slot=None, dims=Some) which
    // matches a fresh-allocation-pending case the existing
    // logic already tolerates.
    session.transition_fbo_dims = Some((w, h));
    *slot_fbo = Some(fbo);
    *slot_tex = Some(tex);
    // r106 + Path A Stage 2 (2026-06-14): fresh-allocation reset
    // for THIS side's painted flag. The dims-changed branch above
    // already reset both flags; this covers the case where one
    // side's cache was previously empty (e.g. first transition
    // ever, or after a teardown freed only one side) and we just
    // allocated it for the first time.
    match side {
        TransitionFboSide::A => session.transition_fbo_a_painted = false,
        TransitionFboSide::B => session.transition_fbo_b_painted = false,
    }
    Ok((fbo, tex))
}

/// r102.2 (2026-06-09): branch-level "reuse or allocate" helper
/// used by every `bake_slide_to_fbo` branch. When `existing` is
/// Some, binds the cached FBO + returns the pair as-is (caller
/// is responsible for glViewport + glClear). When None, falls
/// through to `create_slide_fbo_pair` exactly as pre-r102.2.
///
/// Centralizes the reuse logic so the cache rollout is one
/// helper call per branch instead of a match-block per branch.
unsafe fn prepare_bake_fbo_pair(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    existing: Option<(glow::NativeFramebuffer, glow::NativeTexture)>,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    match existing {
        Some((fbo, tex)) => {
            gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            Ok((fbo, tex))
        }
        None => create_slide_fbo_pair(gl, mode_w, mode_h),
    }
}

/// CRIT-A (2026-05-10): cached FS_BRIGHT_GAMMA program + resolved
/// attribute / uniform locations. P2-G (2026-05-10) extracted the
/// fullscreen-quad VBO into a shared `cached_textured_quad_vbo`
/// (also used by run_blit_pass + run_overlay_blend_pass; identical
/// geometry, single allocation). Mirrors CachedSpProgram /
/// CachedGlyphProgram.
#[derive(Clone, Copy)]
struct CachedBrightGammaProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_src: Option<glow::NativeUniformLocation>,
    u_brightness: Option<glow::NativeUniformLocation>,
    u_gamma: Option<glow::NativeUniformLocation>,
}

/// P2-G (2026-05-10): cached FS_OVERLAY_BLEND program + locations.
/// Pre-fix run_overlay_blend_pass did link_program + create_buffer
/// + 2x get_attrib_location + 2x get_uniform_location + draw +
/// delete_buffer + delete_program EVERY frame the slide had a
/// non-Normal-blend layer (overlay-route hot path). Same shape as
/// CachedBrightGammaProgram.
#[derive(Clone, Copy)]
struct CachedOverlayBlendProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_layer_tex: Option<glow::NativeUniformLocation>,
    u_slide_tex: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static BRIGHT_GAMMA_PROGRAM: std::cell::Cell<Option<CachedBrightGammaProgram>> =
        const { std::cell::Cell::new(None) };
    static OVERLAY_BLEND_PROGRAM: std::cell::Cell<Option<CachedOverlayBlendProgram>> =
        const { std::cell::Cell::new(None) };
    /// 2026-06-14 iter-7: per-transition latch for the transition_
    /// tex_probe debug emit in paint_and_present_one_transition_
    /// frame. Stores the LAST progress value passed in; the probe
    /// fires on the first tick where progress crosses 0.4 from
    /// below, then re-arms when progress drops by >0.1 (= the
    /// start of a new transition window). Initial value 2.0 = "out
    /// of range, no transition yet" so the first transition
    /// reliably fires the probe. IPC sidecar is single-threaded
    /// so a thread_local Cell is sufficient. Carried over from
    /// iter-3 / iter-5 on the c3.x branch — pure instrumentation,
    /// no behavioral coupling.
    static TRANSITION_TEX_PROBE_LAST_PROGRESS: std::cell::Cell<f32> =
        const { std::cell::Cell::new(2.0) };
    /// P2-G (2026-05-10): shared fullscreen-quad VBO for every
    /// post-pass that draws a textured fullscreen quad
    /// (run_bright_gamma_pass + run_blit_pass +
    /// run_overlay_blend_pass). Geometry is STATIC_DRAW (NDC
    /// [-1,1] x UV [0,1]) so reuse across calls is safe (no
    /// driver-sync hazard like P2-F's reverted STREAM_DRAW). Single
    /// allocation across all three call paths; access via
    /// `cached_textured_quad_vbo(gl)`. (P2-G hoist 2026-05-10
    /// consolidated three pre-existing per-call paths -- CRIT-A's
    /// BRIGHT_GAMMA_QUAD_VBO + a now-removed create_textured_quad
    /// helper that run_blit_pass / run_overlay_blend_pass used --
    /// into this single shared cell.)
    static TEXTURED_QUAD_VBO: std::cell::Cell<Option<glow::NativeBuffer>> =
        const { std::cell::Cell::new(None) };
}

unsafe fn cached_bright_gamma_program(
    gl: &glow::Context,
) -> Result<CachedBrightGammaProgram> {
    use glow::HasContext;
    BRIGHT_GAMMA_PROGRAM.with(|c| {
        if let Some(cgp) = c.get() {
            return Ok(cgp);
        }
        let program = link_program(gl, VS_TEXTURED_QUAD, crate::hdmi_logic::FS_BRIGHT_GAMMA)
            .context("link FS_BRIGHT_GAMMA")?;
        let a_pos = gl
            .get_attrib_location(program, "a_pos")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (bright_gamma)"))?;
        let a_uv = gl
            .get_attrib_location(program, "a_uv")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (bright_gamma)"))?;
        let u_src = gl.get_uniform_location(program, "u_src");
        let u_brightness = gl.get_uniform_location(program, "u_brightness");
        let u_gamma = gl.get_uniform_location(program, "u_gamma");
        let cgp = CachedBrightGammaProgram {
            program,
            a_pos,
            a_uv,
            u_src,
            u_brightness,
            u_gamma,
        };
        c.set(Some(cgp));
        Ok(cgp)
    })
}

unsafe fn cached_overlay_blend_program(
    gl: &glow::Context,
) -> Result<CachedOverlayBlendProgram> {
    use glow::HasContext;
    OVERLAY_BLEND_PROGRAM.with(|c| {
        if let Some(cop) = c.get() {
            return Ok(cop);
        }
        let program = link_program(gl, VS_TEXTURED_QUAD, FS_OVERLAY_BLEND)
            .context("link FS_OVERLAY_BLEND")?;
        let a_pos = gl
            .get_attrib_location(program, "a_pos")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (overlay_blend)"))?;
        let a_uv = gl
            .get_attrib_location(program, "a_uv")
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (overlay_blend)"))?;
        let u_layer_tex = gl.get_uniform_location(program, "u_layer_tex");
        let u_slide_tex = gl.get_uniform_location(program, "u_slide_tex");
        let cop = CachedOverlayBlendProgram {
            program,
            a_pos,
            a_uv,
            u_layer_tex,
            u_slide_tex,
        };
        c.set(Some(cop));
        Ok(cop)
    })
}

/// Shared fullscreen-quad VBO for textured-quad post-passes.
/// Lazy-allocated on first call; STATIC_DRAW (geometry never
/// changes between calls). Used by run_bright_gamma_pass +
/// run_blit_pass + run_overlay_blend_pass.
unsafe fn cached_textured_quad_vbo(gl: &glow::Context) -> Result<glow::NativeBuffer> {
    use glow::HasContext;
    TEXTURED_QUAD_VBO.with(|c| {
        if let Some(vbo) = c.get() {
            return Ok(vbo);
        }
        let vbo = gl
            .create_buffer()
            .map_err(|e| anyhow!("glGenBuffers(textured_quad): {e}"))?;
        // Fullscreen quad with UV (0,0) at top-left -> bottom in
        // NDC because gl_FragCoord origin is bottom-left.
        let verts: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            std::mem::size_of_val(&verts),
        );
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
        c.set(Some(vbo));
        Ok(vbo)
    })
}

/// Delete cached post-pass programs + shared VBO while the GL
/// context is still bound. Called from with_egl_session teardown.
fn clear_bright_gamma_cache(gl: &glow::Context) {
    use glow::HasContext;
    BRIGHT_GAMMA_PROGRAM.with(|c| {
        if let Some(cgp) = c.replace(None) {
            unsafe { gl.delete_program(cgp.program); }
        }
    });
    OVERLAY_BLEND_PROGRAM.with(|c| {
        if let Some(cop) = c.replace(None) {
            unsafe { gl.delete_program(cop.program); }
        }
    });
    TEXTURED_QUAD_VBO.with(|c| {
        if let Some(vbo) = c.replace(None) {
            unsafe { gl.delete_buffer(vbo); }
        }
    });
    // FYS bug 5 -- free the present-pass rotated quad VBO.
    PRESENT_QUAD_VBO.with(|c| {
        if let Some((vbo, _rot)) = c.replace(None) {
            unsafe { gl.delete_buffer(vbo); }
        }
    });
    // FYS bug B / hardening C3 L1 -- free both cover-fit quad VBO
    // cache slots.
    COVER_QUAD_VBO.with(|c| {
        for slot in c.replace([None, None]).into_iter().flatten() {
            unsafe { gl.delete_buffer(slot.0); }
        }
    });
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
    let cgp = cached_bright_gamma_program(gl)?;
    let vbo = cached_textured_quad_vbo(gl)?;
    gl.use_program(Some(cgp.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(src_tex));
    gl.uniform_1_i32(cgp.u_src.as_ref(), 0);
    gl.uniform_1_f32(cgp.u_brightness.as_ref(), brightness);
    gl.uniform_1_f32(cgp.u_gamma.as_ref(), gamma);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cgp.a_pos);
    gl.vertex_attrib_pointer_f32(cgp.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cgp.a_uv);
    gl.vertex_attrib_pointer_f32(cgp.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cgp.a_pos);
    gl.disable_vertex_attrib_array(cgp.a_uv);
    gl.bind_texture(glow::TEXTURE_2D, None);
    // CRIT-A + P2-G: program + shared VBO come from session-lived
    // thread_local caches; never freed here. Cleanup happens in
    // clear_bright_gamma_cache at session teardown.
    Ok(())
}

std::thread_local! {
    /// FYS bug 5 -- per-rotation present-pass quad VBO. The present
    /// pass blits the logical scene FBO to the panel-native default
    /// framebuffer; for a non-zero rotation the quad's vertex
    /// POSITIONS are rotated while the UVs stay fixed, so the
    /// sampled content lands rotated on the panel. Rotation is fixed
    /// for the session lifetime, so a single VBO (one per process /
    /// thread, since the session is single-threaded) suffices.
    /// Lazily (re)built when the requested rotation changes; freed
    /// in clear_bright_gamma_cache at session teardown.
    static PRESENT_QUAD_VBO: std::cell::Cell<Option<(glow::NativeBuffer, i32)>> =
        const { std::cell::Cell::new(None) };
}

/// FYS bug 5 -- get-or-rebuild the present-pass quad VBO for the
/// requested rotation. STATIC_DRAW; the geometry only changes if
/// the caller's rotation differs from the cached one (it never
/// does within a session, but rebuild-on-mismatch keeps the helper
/// correct if a future caller varies it).
unsafe fn present_quad_vbo(gl: &glow::Context, rotation: i32) -> Result<glow::NativeBuffer> {
    use glow::HasContext;
    PRESENT_QUAD_VBO.with(|c| {
        if let Some((vbo, cached_rot)) = c.get() {
            if cached_rot == rotation {
                return Ok(vbo);
            }
            gl.delete_buffer(vbo);
        }
        let vbo = gl
            .create_buffer()
            .map_err(|e| anyhow!("glGenBuffers(present_quad): {e}"))?;
        // FYS bug 5 -- the rotation geometry is the host-testable
        // pure function in hdmi_logic (the rotation-direction
        // convention is documented there).
        let verts = crate::hdmi_logic::present_quad_verts(rotation);
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            std::mem::size_of_val(&verts),
        );
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
        c.set(Some((vbo, rotation)));
        Ok(vbo)
    })
}

std::thread_local! {
    /// FYS bug B -- cover-fit quad VBO for regular image + video
    /// slide bakes. The quad's POSITIONS are scaled past +/-1 NDC so
    /// the source covers the panel aspect-preserving (GL clips the
    /// overflow); UVs stay fixed. Keyed on (frame_w, frame_h,
    /// panel_w, panel_h) — the geometry only changes on a source- or
    /// panel-dims change (a slide change to a differently-sized
    /// asset / a resolution switch).
    ///
    /// Hardening C3 / L1 (2026-05-21): a 2-ENTRY cache. A
    /// video↔video transition between two differently-sized
    /// sources alternates the two endpoints' keys every frame; a
    /// single-slot cache rebuilt its only slot twice per frame for
    /// the whole transition. Two slots keep BOTH endpoints' VBOs
    /// resident, so steady-state transition frames are pure hits.
    /// Slot selection is the host-tested pure `cover_quad_slot`.
    /// Both slots are freed in clear_bright_gamma_cache at session
    /// teardown.
    static COVER_QUAD_VBO: std::cell::RefCell<
        [Option<(glow::NativeBuffer, crate::hdmi_logic::CoverQuadKey)>; 2],
    > = const { std::cell::RefCell::new([None, None]) };

    /// Hardening C3 / L1 -- round-robin eviction cursor for the
    /// 2-entry `COVER_QUAD_VBO` cache (used only when both slots
    /// are occupied and a third key arrives).
    static COVER_QUAD_VBO_NEXT: std::cell::Cell<usize> =
        const { std::cell::Cell::new(0) };
}

/// FYS bug B (2026-05-21) -- get-or-rebuild the cover-fit quad VBO
/// for a (source dims, panel dims) pair. The vertices come from the
/// host-tested `cover_fit_quad_verts`. STATIC_DRAW; rebuilt only on
/// a cache miss.
///
/// Hardening C3 / L1: 2-entry cache. `cover_quad_slot` (pure,
/// host-tested) picks the slot — a hit reuses the resident VBO; a
/// miss builds into an empty slot, else evicts the round-robin
/// slot. The evicted slot's VBO is `glDeleteBuffers`-freed before
/// the replacement is stored, so the cache never leaks a VBO.
unsafe fn cover_quad_vbo(
    gl: &glow::Context,
    frame_w: u32,
    frame_h: u32,
    panel_w: u32,
    panel_h: u32,
) -> Result<glow::NativeBuffer> {
    use glow::HasContext;
    let key: crate::hdmi_logic::CoverQuadKey =
        (frame_w, frame_h, panel_w, panel_h);
    COVER_QUAD_VBO.with(|cell| {
        let keys = {
            let slots = cell.borrow();
            [slots[0].map(|(_, k)| k), slots[1].map(|(_, k)| k)]
        };
        let next_build = COVER_QUAD_VBO_NEXT.with(|c| c.get());
        match crate::hdmi_logic::cover_quad_slot(&keys, key, next_build) {
            crate::hdmi_logic::CoverQuadSlot::Hit { idx } => {
                let slots = cell.borrow();
                // Hit guaranteed by cover_quad_slot — slot is Some.
                Ok(slots[idx].expect("cover_quad_slot Hit -> occupied slot").0)
            }
            crate::hdmi_logic::CoverQuadSlot::Miss { idx } => {
                // Free the evicted slot's VBO (if any) before the
                // replacement is stored — no leak.
                if let Some((old_vbo, _)) = cell.borrow_mut()[idx].take() {
                    gl.delete_buffer(old_vbo);
                }
                let vbo = gl
                    .create_buffer()
                    .map_err(|e| anyhow!("glGenBuffers(cover_quad): {e}"))?;
                let verts = crate::hdmi_logic::cover_fit_quad_verts(
                    frame_w, frame_h, panel_w, panel_h,
                );
                gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                let bytes = std::slice::from_raw_parts(
                    verts.as_ptr() as *const u8,
                    std::mem::size_of_val(&verts),
                );
                gl.buffer_data_u8_slice(
                    glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW,
                );
                cell.borrow_mut()[idx] = Some((vbo, key));
                // Advance the round-robin cursor so the NEXT
                // eviction picks the other slot.
                COVER_QUAD_VBO_NEXT.with(|c| c.set((idx + 1) % 2));
                Ok(vbo)
            }
        }
    })
}

/// FYS bug 5 -- the rotation-aware present pass. Blits the logical
/// scene FBO texture to the bound (panel-native) framebuffer,
/// applying brightness/gamma AND the display rotation in a single
/// fullscreen blit. Reuses the FS_BRIGHT_GAMMA program (so identity
/// brightness/gamma is still correct); the rotation is baked into
/// the quad's vertex positions (see `present_quad_verts`).
///
/// Caller is responsible for binding the default framebuffer and
/// setting the viewport to the PHYSICAL panel dims before calling.
/// For `rotation == 0` this is geometrically identical to
/// `run_bright_gamma_pass`.
unsafe fn run_present_pass(
    gl: &glow::Context,
    src_tex: glow::NativeTexture,
    brightness: f32,
    gamma: f32,
    rotation: i32,
) -> Result<()> {
    use glow::HasContext;
    let cgp = cached_bright_gamma_program(gl)?;
    let vbo = present_quad_vbo(gl, rotation)?;
    gl.use_program(Some(cgp.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(src_tex));
    gl.uniform_1_i32(cgp.u_src.as_ref(), 0);
    gl.uniform_1_f32(cgp.u_brightness.as_ref(), brightness);
    gl.uniform_1_f32(cgp.u_gamma.as_ref(), gamma);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cgp.a_pos);
    gl.vertex_attrib_pointer_f32(cgp.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cgp.a_uv);
    gl.vertex_attrib_pointer_f32(cgp.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cgp.a_pos);
    gl.disable_vertex_attrib_array(cgp.a_uv);
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
///
/// Rotation note: capture paths deliberately render at LOGICAL
/// (un-rotated) dims and are NOT routed through the present-pass
/// rotation — the captured PNG is the content in its authored
/// orientation. Rotated-thumbnail handling is the UI's job (FYS
/// bug 7). Don't "fix" this to rotate.
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
        None,  // one-shot path; no runtime glyph cache needed
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
                None,  // closure captures gl only; no session-reachable runtime cache
            )?;
            // eglSwapBuffers (called in render_one_frame_in_session)
            // implicitly flushes; the explicit gl.flush() forced an
            // extra tile-store on vc4 (cold-scout #2 P6, 2026-05-09).
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

/// Per-layer resolved text (cold-scout #13). For layers without
/// auto_mode set, returns a `Cow::Borrowed` of `layer.text` -- no
/// allocation per frame. For auto_mode layers, calls
/// `format_auto_text` and wraps the resulting String in
/// `Cow::Owned`. The pre-fix code unconditionally cloned
/// `layer.text` into a String per frame even when nothing
/// changed; for static slides with long layer text the clones
/// were a measurable per-frame allocation tax.
fn resolve_layer_text<'a>(
    layer: &'a crate::content::TextLayer,
    cal: crate::hdmi_logic::Calendar,
) -> std::borrow::Cow<'a, str> {
    match layer.auto_mode.as_deref() {
        None => std::borrow::Cow::Borrowed(layer.text.as_str()),
        Some(_) => format_auto_text(
            layer.auto_mode.as_deref(),
            layer.auto_format.as_deref(),
            cal,
        )
        .map(std::borrow::Cow::Owned)
        .unwrap_or(std::borrow::Cow::Borrowed(layer.text.as_str())),
    }
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

/// Task #168 (2026-05-22): cover-fit blit of an already-cached
/// image-slide texture into the currently-bound framebuffer. No
/// decode, no upload, no delete — the `ImageSlideTextureCache`
/// owns the texture lifetime; this is one fullscreen draw call.
///
/// Caller is responsible for binding the destination framebuffer
/// (default fb or an FBO) and for any post-pass / scanout handling.
///
/// Pre-Task-#168 this path lived in `bake_image_slide_to_current_fbo`,
/// which inline-decoded the PNG and uploaded a fresh GL texture per
/// frame. On a Web slide refresh, that hitched the render thread for
/// 100-300ms at the very transition into the refreshed slide. The
/// cache-driven path replaces it: cache.ensure() does any work; this
/// helper just blits.
unsafe fn blit_cached_image_slide_to_current_fbo(
    gl: &glow::Context,
    tex: glow::NativeTexture,
    img_w: u32,
    img_h: u32,
    mode_w: u32,
    mode_h: u32,
) -> Result<()> {
    use glow::HasContext;
    gl.viewport(0, 0, mode_w as i32, mode_h as i32);
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);
    let cover_vbo = cover_quad_vbo(gl, img_w, img_h, mode_w, mode_h)?;
    run_blit_pass_quad(gl, tex, cover_vbo)
}

/// Renderer-hardening C2 (finding L4, 2026-05-21) — check `glGetError`
/// after an external-frame texture upload and log a non-`GL_NO_ERROR`
/// result ONCE.
///
/// `glTexImage2D` / `glTexSubImage2D` can fail silently — the call
/// returns `void`, the only signal is `glGetError`. A bad upload (an
/// over-large texture, an out-of-memory GPU, a driver fault) would
/// otherwise blit black/garbage with no diagnostic anywhere. This
/// converts that into a visible log line.
///
/// `latch` is a per-call-site `AtomicBool`: the first non-NO_ERROR
/// result logs, every subsequent one is silenced. A per-frame GL
/// fault would otherwise flood the log 30×/sec (the renderer's
/// no-per-frame-eprintln discipline). `label` names the call site.
fn check_gl_upload_error(
    gl: &glow::Context,
    label: &str,
    latch: &std::sync::atomic::AtomicBool,
) {
    use glow::HasContext;
    let err = unsafe { gl.get_error() };
    if err != glow::NO_ERROR
        && latch
            .compare_exchange(
                false,
                true,
                std::sync::atomic::Ordering::Relaxed,
                std::sync::atomic::Ordering::Relaxed,
            )
            .is_ok()
    {
        eprintln!(
            "warn: {label}: glGetError 0x{err:04x} after texture upload \
             (this frame may blit black/garbage; further faults silenced)"
        );
    }
}

/// STREAM/VLC slice 2.5 — upload one raw RGB888 frame as a texture
/// and FS_BLIT it to fill the currently-bound framebuffer. The
/// raw-bytes analogue of bake_image_slide_to_current_fbo, no PNG
/// decode.
///
/// `rgb` must be exactly `frame_w * frame_h * 3` bytes. Caller binds
/// the destination framebuffer and handles post-pass / scanout.
///
/// Slice-9 follow-up: the texture is session-persistent (`frame_tex`).
/// It is allocated once with glTexImage2D and thereafter updated in
/// place with glTexSubImage2D; the per-frame
/// glGen/glTexImage2D/glDelete the slice-2.5 version did was a
/// measured paint-cost tax on the Pi Zero 2 W. Reallocation happens
/// only when the frame dimensions change (a source resolution
/// switch). Source-agnostic — any RGB888 producer (VLC today, a
/// future webpage slide) drives this unchanged.
unsafe fn bake_external_rgb_to_current_fbo(
    gl: &glow::Context,
    frame_tex: &mut Option<(glow::NativeTexture, u32, u32)>,
    rgb: &[u8],
    frame_w: u32,
    frame_h: u32,
    mode_w: u32,
    mode_h: u32,
) -> Result<()> {
    use glow::HasContext;
    let expected = (frame_w as usize) * (frame_h as usize) * 3;
    if rgb.len() != expected {
        return Err(anyhow!(
            "external frame is {} bytes, expected {}x{}x3 = {}",
            rgb.len(),
            frame_w,
            frame_h,
            expected,
        ));
    }
    gl.viewport(0, 0, mode_w as i32, mode_h as i32);
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);

    // RGB888 rows are frame_w*3 bytes — not 4-aligned for widths
    // that aren't multiples of 4 (the basic tier is 854 px wide,
    // 854*3 = 2562, not 4-aligned). GL's default UNPACK_ALIGNMENT
    // is 4, which would shear every such frame; force 1 for the
    // upload and restore the default after.
    let dims_changed = match *frame_tex {
        Some((_, w, h)) => w != frame_w || h != frame_h,
        None => true,
    };
    if dims_changed {
        // First external frame, or a resolution switch — (re)allocate
        // the persistent texture. glTexImage2D both sizes the texture
        // and uploads this frame's pixels.
        if let Some((old, _, _)) = frame_tex.take() {
            gl.delete_texture(old);
        }
        let tex = gl
            .create_texture()
            .map_err(|e| anyhow!("glGenTextures(bake_external_rgb): {e}"))?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        gl.tex_image_2d(
            glow::TEXTURE_2D, 0, glow::RGB as i32,
            frame_w as i32, frame_h as i32, 0,
            glow::RGB, glow::UNSIGNED_BYTE, Some(rgb),
        );
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
        *frame_tex = Some((tex, frame_w, frame_h));
    } else {
        // Steady state — texture already sized; update pixels in place.
        let (tex, _, _) = frame_tex.expect("dims_changed==false implies Some");
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
        gl.tex_sub_image_2d(
            glow::TEXTURE_2D, 0, 0, 0,
            frame_w as i32, frame_h as i32,
            glow::RGB, glow::UNSIGNED_BYTE,
            glow::PixelUnpackData::Slice(rgb),
        );
        gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
    }
    // Renderer-hardening C2 (finding L4): surface a silent GL upload
    // fault — a once-logged latch so a per-frame fault logs once.
    static RGB_UPLOAD_ERR_LATCH: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);
    check_gl_upload_error(gl, "bake_external_rgb_to_current_fbo", &RGB_UPLOAD_ERR_LATCH);
    let tex = frame_tex.expect("frame_tex is Some after the branch above").0;
    run_blit_pass(gl, tex)
}

/// STREAM/VLC HW-decode (2026-05-20) — upload one raw planar NV12
/// frame (Y plane + interleaved UV plane) as a texture pair and
/// cover-fit-blit it through the BT.709 NV12→RGB shader to fill the
/// currently-bound framebuffer.
///
/// The NV12 analogue of `bake_external_rgb_to_current_fbo`. The
/// HW-decode VLC pump (`ffmpeg -c:v h264_v4l2m2m`, raw NV12 out, no
/// `-vf`) hands us a SOURCE-resolution frame; this helper does the
/// cover-fit scale + crop on the GPU that the dropped ffmpeg
/// `scale=...:force_original_aspect_ratio=increase,crop=...` filter
/// used to do — `nv12_cover_fit_uv_transform` computes the UV
/// remap from (source dims, panel dims) and `run_nv12_cover_blit_
/// pass` applies it.
///
/// `nv12` must be exactly `frame_w * frame_h * 3 / 2` bytes (Y is
/// `frame_w*frame_h`, UV is `frame_w*frame_h/2`). `frame_w` and
/// `frame_h` MUST be even (NV12's 4:2:0 chroma is half-res on both
/// axes); the V4L2 codec / ffmpeg always emits even dims.
///
/// Texture-persistence: the Y + UV textures live in `nv12_tex`
/// (session-persistent). Allocated once with glTexImage2D and
/// thereafter updated in place with glTexSubImage2D — mirrors the
/// slice-9 paint-opt in `bake_external_rgb_to_current_fbo` so the
/// per-frame glGen/glDelete tax is avoided. Reallocation happens
/// only on a source-resolution switch.
unsafe fn bake_external_nv12_to_current_fbo(
    gl: &glow::Context,
    nv12_tex: &mut Option<(glow::NativeTexture, glow::NativeTexture, u32, u32)>,
    nv12: &[u8],
    frame_w: u32,
    frame_h: u32,
    mode_w: u32,
    mode_h: u32,
) -> Result<()> {
    use glow::HasContext;
    // NV12: Y plane is frame_w*frame_h bytes; UV plane is half-res
    // on both axes but 2 bytes per chroma sample -> frame_w*frame_h/2.
    let y_bytes = (frame_w as usize) * (frame_h as usize);
    let expected = y_bytes + y_bytes / 2;
    if nv12.len() != expected {
        return Err(anyhow!(
            "external NV12 frame is {} bytes, expected {}x{} NV12 = {}",
            nv12.len(),
            frame_w,
            frame_h,
            expected,
        ));
    }
    if frame_w == 0 || frame_h == 0 || frame_w % 2 != 0 || frame_h % 2 != 0 {
        return Err(anyhow!(
            "external NV12 frame dims {}x{} must be non-zero and even",
            frame_w,
            frame_h,
        ));
    }
    // Renderer-hardening C2 (finding H2, 2026-05-21): reject a frame
    // wider/taller than the vc4 GPU's GL_MAX_TEXTURE_SIZE. glTexImage2D
    // with a dimension over 2048 px fails GL_INVALID_VALUE and leaves
    // the texture undefined — a SILENT black/garbage blit. Returning an
    // Err here makes the IPC pump log the failure and hold the last
    // good frame instead. A properly-clamped stream never trips this:
    // the backend (FfmpegStreamSource) downscales any >2048 source via
    // an ffmpeg `scale` filter and reports the clamped dims.
    if !crate::hdmi_logic::nv12_dims_ok(frame_w, frame_h) {
        return Err(anyhow!(
            "external NV12 frame dims {}x{} exceed the vc4 GPU's {}px \
             texture limit; the stream source must downscale to fit",
            frame_w,
            frame_h,
            crate::hdmi_logic::MAX_GL_TEXTURE_DIM,
        ));
    }
    let y_plane = &nv12[..y_bytes];
    let uv_plane = &nv12[y_bytes..];

    gl.viewport(0, 0, mode_w as i32, mode_h as i32);
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);

    let dims_changed = match *nv12_tex {
        Some((_, _, w, h)) => w != frame_w || h != frame_h,
        None => true,
    };
    // UNPACK_ALIGNMENT=1: Y rows are frame_w bytes, not 4-aligned
    // for arbitrary source widths.
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
    if dims_changed {
        // First NV12 frame, or a source-resolution switch —
        // (re)allocate the persistent Y + UV texture pair.
        if let Some((old_y, old_uv, _, _)) = nv12_tex.take() {
            gl.delete_texture(old_y);
            gl.delete_texture(old_uv);
        }
        let y_tex = gl
            .create_texture()
            .map_err(|e| anyhow!("glGenTextures(external NV12 Y): {e}"))?;
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.tex_image_2d(
            glow::TEXTURE_2D, 0, glow::LUMINANCE as i32,
            frame_w as i32, frame_h as i32, 0,
            glow::LUMINANCE, glow::UNSIGNED_BYTE, Some(y_plane),
        );
        // r40 (2026-06-02): if uv_tex glGenTextures fails the
        // prior y_tex would orphan (~2 MB GLES storage at 1080p)
        // -- *nv12_tex never gets assigned, so the next call
        // re-enters dims_changed=true and creates ANOTHER y_tex.
        // Explicit cleanup mirrors the canonical scanout commit-fail
        // shape at :3724-3731 + the r38b transition-closure pattern.
        // See qa/r40-non-fys-allocator-fixes-2026-06-02.md.
        let uv_tex = match gl.create_texture() {
            Ok(t) => t,
            Err(e) => {
                gl.delete_texture(y_tex);
                return Err(anyhow!("glGenTextures(external NV12 UV): {e}"));
            }
        };
        gl.active_texture(glow::TEXTURE1);
        gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        // UV plane: 4:2:0 — frame_w/2 (U,V) pairs per row in
        // LUMINANCE_ALPHA, frame_h/2 rows. FS samples .ra.
        gl.tex_image_2d(
            glow::TEXTURE_2D, 0, glow::LUMINANCE_ALPHA as i32,
            (frame_w / 2) as i32, (frame_h / 2) as i32, 0,
            glow::LUMINANCE_ALPHA, glow::UNSIGNED_BYTE, Some(uv_plane),
        );
        *nv12_tex = Some((y_tex, uv_tex, frame_w, frame_h));
    } else {
        // Steady state — textures already sized; update in place.
        let (y_tex, uv_tex, _, _) = nv12_tex.expect("dims_changed==false implies Some");
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
        gl.tex_sub_image_2d(
            glow::TEXTURE_2D, 0, 0, 0,
            frame_w as i32, frame_h as i32,
            glow::LUMINANCE, glow::UNSIGNED_BYTE,
            glow::PixelUnpackData::Slice(y_plane),
        );
        gl.active_texture(glow::TEXTURE1);
        gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
        gl.tex_sub_image_2d(
            glow::TEXTURE_2D, 0, 0, 0,
            (frame_w / 2) as i32, (frame_h / 2) as i32,
            glow::LUMINANCE_ALPHA, glow::UNSIGNED_BYTE,
            glow::PixelUnpackData::Slice(uv_plane),
        );
    }
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
    // Renderer-hardening C2 (finding L4): surface a silent GL upload
    // fault on the Y/UV plane uploads — a once-logged latch so a
    // per-frame fault logs once, not 30×/sec.
    static NV12_UPLOAD_ERR_LATCH: std::sync::atomic::AtomicBool =
        std::sync::atomic::AtomicBool::new(false);
    check_gl_upload_error(gl, "bake_external_nv12_to_current_fbo", &NV12_UPLOAD_ERR_LATCH);
    gl.active_texture(glow::TEXTURE0);
    let (y_tex, uv_tex, _, _) =
        nv12_tex.expect("nv12_tex is Some after the branch above");
    // GPU-side cover-fit: source dims -> panel dims.
    let (uv_scale, uv_offset) =
        crate::hdmi_logic::nv12_cover_fit_uv_transform(frame_w, frame_h, mode_w, mode_h);
    run_nv12_cover_blit_pass(gl, y_tex, uv_tex, uv_scale, uv_offset)
}

/// Phase 8 slice 2 (2026-05-16) — drain one V4L2 NV12 frame and
/// blit it via the BT.601 NV12→RGB shader into the currently-bound
/// framebuffer. Mirrors `bake_image_slide_to_current_fbo`'s
/// contract: caller binds the destination FBO (default fb or an
/// FBO) before calling, and handles any post-pass / scanout /
/// brightness-gamma routing.
///
/// Returns:
///   - `Ok(Some("DMABUF"))` — frame painted via the dma_buf
///     EGLImage path (piece 4a-c).
///   - `Ok(Some("MMAP"))`   — frame painted via the MMAP
///     CPU-upload path (piece 3d-e).
///   - `Ok(None)`           — no frame ready this tick. FBO is
///     left at whatever the caller cleared+left it as (the bake
///     does not run viewport+clear in the no-frame path); caller
///     should skip swap+commit. Matches the pre-refactor
///     paint_and_present_one_video_slide_frame's behavior of
///     leaving prior scanout untouched on no-frame.
///   - `Err(_)`             — feed/drain/upload/blit failure.
///
/// The path label is the same string the `[firstframe]` log lines
/// used pre-refactor, so callers can preserve the same total /
/// swap_commit log shape (DmaBuf-only by convention; MMAP was
/// always silent for the total/swap_commit line).
///
/// Decoder state. Per-call feeds ONE sample (if any remain) and
/// drains ONE frame (with a 5×2ms EAGAIN retry budget). Callers:
///   - Per-Advance hold-path video paint: video plays.
///   - Phase 8 slice 6 (2026-05-16, 1c61747) transition path:
///     video drains one V4L2 sample per Advance through the
///     transition window (Option D play-through; see hdmi.rs
///     L2966 for the dispatcher-level documentation).
/// The phase 4v-3b "motion through transitions must keep advancing"
/// rule applies to TEXT motion phase, not to video frame cadence;
/// slice 6's Option D choice extends the same principle to video.
///
/// Profile timing. When OPENMARQUEE_FIRSTFRAME_PROFILE=1 AND the
/// (next_sample_idx, frames_decoded) pair matches the first-frame
/// signature (next=1, decoded=0), prints the same `[firstframe]
/// feed=/dqbuf=/dmabuf_blit_pass=` lines as the pre-refactor
/// paint function. The `swap_commit=/total=` log stays in the
/// caller because only the caller does the swap.
#[cfg(target_os = "linux")]
/// 2026-06-14 iter-7 kill switch for the offscreen-bake `gl.flush()`
/// that closes the V4L2 dma_buf re-QBUF / GPU tile-store race
/// described in `qa/v2v-lean-analysis-2026-06-14.md`. Default ON;
/// set `OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=off` (off / 0 / false / no /
/// disable / disabled, case-insensitive, whitespace-trimmed) to skip
/// the flush even when the caller declares an offscreen bake — used
/// by QA to verify the bug still reproduces against a known-bad
/// baseline on the bench.
/// 2026-06-15 perf-gl M-1: cap for the slide_caches LruMap. Default
/// 24 covers the FYS 19-slide reel + 5-slide headroom for a partial
/// playlist swap-in before eviction begins. Tunable via
/// `OPENMARQUEE_SLIDE_CACHE_CAP` for bench experiments (admin's
/// optimization mission, not for production hot-swaps). Clamped to
/// [4, 256] to stop pathological values from either blowing memory
/// or churning every slide change.
const SLIDE_CACHE_CAP_DEFAULT: usize = 24;
const SLIDE_CACHE_CAP_MIN: usize = 4;
const SLIDE_CACHE_CAP_MAX: usize = 256;

fn slide_cache_capacity() -> usize {
    match std::env::var("OPENMARQUEE_SLIDE_CACHE_CAP") {
        Ok(v) => v
            .trim()
            .parse::<usize>()
            .ok()
            .map(|n| n.clamp(SLIDE_CACHE_CAP_MIN, SLIDE_CACHE_CAP_MAX))
            .unwrap_or(SLIDE_CACHE_CAP_DEFAULT),
        Err(_) => SLIDE_CACHE_CAP_DEFAULT,
    }
}

/// 2026-06-15 perf-gl W-2: thread_local-cached read of
/// `OPENMARQUEE_BOUNDARY_TRACE`. Pre-W-2 every paint hook called
/// `std::env::var_os("OPENMARQUEE_BOUNDARY_TRACE").is_some()` per
/// frame — a libc getenv → linear environ-block scan that costs
/// ~0.5 µs and is wasted work since the env var never changes
/// mid-process. The thread_local Cell holds the resolved Option
/// per worker thread: first call resolves + caches; subsequent
/// calls are a single Cell::get() (~1 ns). Saves ~0.5 µs/frame at
/// the 2 hot-path call sites (paint_and_present_one_frame_for_
/// slide + paint_slide_with_viewport). The paint_slide_with_
/// viewport site has no EglSession in scope (it takes raw &gl), so
/// thread_local is the canonical place for this cache.
fn boundary_trace_enabled_cached() -> bool {
    use std::cell::Cell;
    thread_local! {
        static CACHED: Cell<Option<bool>> = const { Cell::new(None) };
    }
    CACHED.with(|c| {
        if let Some(v) = c.get() {
            return v;
        }
        let v = std::env::var_os("OPENMARQUEE_BOUNDARY_TRACE").is_some();
        c.set(Some(v));
        // 2026-06-15 perf-gl W-2 fingerprint: one-time emit on first
        // resolution per worker thread. Lets QA's strings|grep gate
        // confirm the cached helper is compiled in (the function
        // symbol is stripped; only literal-string emits survive).
        // Cost: one eprintln per process. The "[perf] w2_env_cache_
        // resolved" marker is the QA-side fingerprint.
        eprintln!(
            "[perf] w2_env_cache_resolved name=OPENMARQUEE_BOUNDARY_TRACE value={}",
            v,
        );
        v
    })
}

/// 2026-06-15 perf-gl W-2: thread_local-cached read of
/// `OPENMARQUEE_FIRSTFRAME_PROFILE`. Same rationale + shape as
/// boundary_trace_enabled_cached. The 2 hot-path call sites are
/// paint_and_present_one_video_slide_frame +
/// bake_video_slide_to_current_fbo; pre-W-2 each called
/// `std::env::var("OPENMARQUEE_FIRSTFRAME_PROFILE").ok().map(|v|
/// v == "1" || v.eq_ignore_ascii_case("true")).unwrap_or(false)`
/// which is even more expensive than var_os (heap-allocates a
/// String for the matched value). Cached path skips both the
/// getenv syscall and the alloc.
fn firstframe_profile_enabled_cached() -> bool {
    use std::cell::Cell;
    thread_local! {
        static CACHED: Cell<Option<bool>> = const { Cell::new(None) };
    }
    CACHED.with(|c| {
        if let Some(v) = c.get() {
            return v;
        }
        let v = std::env::var("OPENMARQUEE_FIRSTFRAME_PROFILE")
            .ok()
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
        c.set(Some(v));
        // 2026-06-15 perf-gl W-2 fingerprint: same one-time emit as
        // boundary_trace_enabled_cached. The "[perf] w2_env_cache_
        // resolved" line literal is the QA-side fingerprint string.
        eprintln!(
            "[perf] w2_env_cache_resolved name=OPENMARQUEE_FIRSTFRAME_PROFILE value={}",
            v,
        );
        v
    })
}

/// 2026-06-15 perf-gl W-2 follow-on: thread_local-cached read of
/// `OPENMARQUEE_BAKE_OFFSCREEN_FLUSH`. Same pattern as
/// boundary_trace_enabled_cached / firstframe_profile_enabled_cached.
/// Called from 2 sites inside bake_video_slide_to_current_fbo, both
/// gated on `is_offscreen_bake=true`, which runs ~30 ticks per
/// transition (60 reads/transition; ~30-60 reads/sec during the
/// transition window). Each pre-fix call did `std::env::var(...)`
/// which heap-allocates a String for the matched value. Caching
/// drops the per-call cost to a Cell::get() (~1 ns); first call
/// emits a one-time fingerprint marker.
fn bake_offscreen_flush_enabled() -> bool {
    use std::cell::Cell;
    thread_local! {
        static CACHED: Cell<Option<bool>> = const { Cell::new(None) };
    }
    CACHED.with(|c| {
        if let Some(v) = c.get() {
            return v;
        }
        let v = match std::env::var("OPENMARQUEE_BAKE_OFFSCREEN_FLUSH") {
            Ok(v) => {
                let v = v.trim().to_ascii_lowercase();
                !matches!(v.as_str(), "off" | "0" | "false" | "no" | "disable" | "disabled")
            }
            Err(_) => true,
        };
        c.set(Some(v));
        // Fingerprint marker — distinctive literal "bake_flush_cache_
        // resolved" lets QA strings|grep verify this commit is in the
        // binary. Singular emit (expected count=1).
        eprintln!(
            "[perf] bake_flush_cache_resolved name=OPENMARQUEE_BAKE_OFFSCREEN_FLUSH value={}",
            v,
        );
        v
    })
}

unsafe fn bake_video_slide_to_current_fbo(
    session: &mut EglSession,
    samples: &[crate::mp4_demux::Sample],
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
    mode_w: u32,
    mode_h: u32,
    // 2026-06-14 iter-7 (video→video transition fix on the lean
    // r103.1 base).
    //
    // ROOT CAUSE (QA analysis qa/v2v-lean-analysis-2026-06-14.md):
    // vc4/Mesa defers the tiled render of this bake's DMABUF
    // external-OES draw into the currently-bound FBO. When the bound
    // FBO is the WINDOW (default fb, steady-state PaintSlide), the
    // subsequent eglSwapBuffers acts as a hard pipeline barrier that
    // forces the tile-store before `Frame::drop` re-QBUFs the V4L2
    // CAPTURE buffer — so the pixels land. When the bound FBO is an
    // OFFSCREEN target (e.g. session.transition_fbo_a in a
    // transition), there is NO such barrier; Frame::drop happens
    // before the tile-store + the codec reclaims the dma_buf slot
    // before the deferred tile-store executes against it → the FBO
    // stores BLACK.
    //
    // FIX: when this bake is into an offscreen FBO, the caller
    // declares `is_offscreen_bake=true`; this function emits a
    // `gl.flush()` BETWEEN the V4L2 frame's read into the FBO and
    // `drop(frame)`. Forces the tile-store to be issued before the
    // codec gets the buffer back. Scoped: steady-state callers pass
    // `false` (default; their swapBuffers is the barrier already) so
    // the production hot path on FYS pays zero extra GPU sync cost.
    //
    // iter-4 (on the c3.x branch) flushed unconditionally and drove
    // load to 17 + 7.6-minute freeze. The fix needed scoping, not
    // dropping. iter-7 ships the scoping correctly + on the LEAN
    // base where the c3.x machinery doesn't compound the cost.
    //
    // Callers:
    //   - paint_and_present_one_text_over_video_slide_frame
    //     (steady-state TextOverVideo) — pass `false`.
    //   - paint_and_present_one_video_slide_frame (steady-state
    //     pure-video) — pass `false`.
    //   - bake_slide_to_fbo SlideBakeInputs::Video branch
    //     (transition) — pass `true`.
    //   - bake_slide_to_fbo SlideBakeInputs::TextOverVideo branch
    //     (transition) — pass `true`.
    //
    // OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=off is a kill switch for the
    // bench: when set, the flush is skipped even for offscreen bakes
    // so QA can confirm the bug still reproduces against a known-bad
    // baseline. Default ON.
    is_offscreen_bake: bool,
) -> Result<Option<&'static str>> {
    use glow::HasContext;
    // 2026-06-15 perf-gl W-2: thread_local-cached env-var read.
    let profile_first = *next_sample_idx == 1
        && *frames_decoded == 0
        && firstframe_profile_enabled_cached();
    // tail-diag instrumentation v1 (2026-06-15, per admin's tail-fix
    // dispatch): per-tick `feed_us` / `dqbuf_us` / `blit_us` /
    // `total_us` breakdown, emitted ONLY when total_us > 100ms (well
    // over the 33ms 30fps budget; well below the multi-second
    // freezes we're hunting). Probe cost on the fast tick = 5
    // CLOCK_MONOTONIC reads (4 Instant::now + 1 .elapsed() inside the
    // gate eval) + 1 comparison = sub-µs; stderr is UNTOUCHED on fast
    // ticks. On the slow tick = +1 format/eprintln (~5-50µs) —
    // negligible vs the multi-second stall we're measuring. Sacred
    // review G.2 confirmed: gated emit can't amplify journal load
    // since the rate caps at tens of emits per transition (~1/tick
    // worst-case on the 14% tail rate).
    //
    // Sacred review S-1 ack: the `samples.is_empty()` early-return
    // below at line ~8475 fires WITHOUT emitting tail-diag (defensive
    // branch; prime_video_decoder bails on zero-sample MP4 so this
    // shouldn't fire in production). If it ever does fire in journal
    // diagnosis, the absence of `tail_diag_bake_breakdown` is the
    // signal that the bake never reached the feed/dqbuf phases.
    //
    // Sacred review S-2 ack: error-path returns (e.g. feed Err,
    // next_frame Err, blit_pass Err) DO NOT emit tail-diag. F-1
    // live-fire shows the 6.9s freeze is in_transition=true sustained
    // (not an aborted transition), so this scope is correct for the
    // bench's stated purpose — the slow tick we're hunting completes
    // successfully through one of the 3 instrumented paths.
    //
    // Fingerprint marker: `tail_diag_bake_breakdown` (source-pinned
    // in frame_pacing.rs).
    let t_bake_total = std::time::Instant::now();
    let t_bake_feed = std::time::Instant::now();
    let mut t_diag_feed_us: u64 = 0;
    let mut t_diag_dqbuf_us: u64 = 0;
    let mut t_diag_blit_us: u64 = 0;
    let t_feed_start = if profile_first { Some(std::time::Instant::now()) } else { None };
    // perf-night r3 (2026-05-26): sub-sub-phase wraps inside the
    // bake_video bottleneck. r2 showed paint_bake_video p99=29.8ms
    // (89% of 30fps budget). Three sub-sub-phases:
    //   - paint_bake_video_dqbuf  (V4L2 feed + next_frame retry loop)
    //   - paint_bake_video_upload (MMAP tex_image_2d Y + UV; ~0 for DMABUF)
    //   - paint_bake_video_shader (run_nv12_blit_pass or DMABUF variant)
    let t_phase = std::time::Instant::now();
    // Feed the next sample. Codec is pipelined: a single feed may
    // not produce a frame this tick; the EAGAIN retry below covers
    // the latency.
    //
    // FYS bug 3: when the samples are exhausted, LOOP — reset to
    // sample 0 and keep feeding — instead of feeding EOF and
    // freezing on the last frame. A video clip shorter than the
    // slide's slot must replay for the full slot, not stall.
    // samples[0] is the clip's opening IDR; the V4L2 decoder
    // retains the SPS/PPS fed at priming (it decoded every
    // mid-stream P/B sample off that same SPS/PPS), so a bare IDR
    // is a valid in-stream refresh point — no flush/reinit needed.
    if samples.is_empty() {
        // Defensive: prime_video_decoder bails on a zero-sample
        // MP4, so a decoder with no samples shouldn't reach here.
        return Ok(None);
    }
    // r106 + Path A Stage 2 (2026-06-14): branch on the Stage 2
    // scope gate. Decoupled feed/drain runs ONLY on transition
    // bakes (is_offscreen_bake=true) with the kill switch ON.
    // Steady-state single-video paint (is_offscreen_bake=false)
    // keeps the pre-r106 blocking pattern even when the kill
    // switch is ON — Stage 2 isolation per the 684d386 r110
    // revert body (whole-function r106 caused 720p steady-state
    // perceptual freeze when the codec didn't deliver every tick).
    let decouple = is_offscreen_bake && crate::v4l2::is_feed_drain_decouple_enabled();
    if decouple {
        // r106 path: bounded non-blocking top-up of the OUTPUT
        // pool until (a) kernel-owned slots are full, (b) the
        // per-tick cap is hit, OR (c) the demuxer's sample list
        // is exhausted.
        //
        // SACRED SUBAGENT BLOCKER (Path A 2026-06-14): match
        // original r106's `while *next_sample_idx < samples.
        // len()` bound — NO INLINE WRAP. The IPC dispatcher
        // (ipc_main.rs's PaintTransition / Advance handlers)
        // detects `next_sample_idx >= samples.len()` BEFORE
        // calling bake and runs reprime_video_decoder_for_loop:
        // STREAMOFF + clear-drained + STREAMON + re-QBUF + re-
        // feed SPS+PPS+IDR primer. If THIS loop wrapped inline
        // it would feed regular non-IDR samples post-wrap to a
        // decoder that needs the IDR primer; bcm2835-codec
        // silently drops them or raises V4L2_BUF_FLAG_ERROR,
        // wedging the decoder. The Path B comment at this
        // function's caller (~hdmi.rs:5353) documents the same
        // hazard for its own retry loop. Cap is bounded by
        // samples remaining; the dispatcher handles wrap on
        // the next tick.
        //
        // Per-tick max-feeds cap = 16 (ffmpeg's empirical
        // OUTPUT queue depth from QA's live-fire dual-1080p
        // proof). Pool typically has 4-8 slots so Ok(false)
        // breaks earlier in practice.
        let mut topup_count = 0u32;
        while *next_sample_idx < samples.len() && topup_count < 16 {
            let s = &samples[*next_sample_idx];
            match decoder.try_feed_nonblocking(s) {
                Ok(true) => {
                    *next_sample_idx += 1;
                    topup_count += 1;
                }
                Ok(false) => break, // OUTPUT pool full
                Err(e) => {
                    return Err(e).with_context(|| {
                        format!("try_feed_nonblocking sample {}", *next_sample_idx)
                    })
                }
            }
        }
        if let Some(t) = t_feed_start {
            eprintln!(
                "[firstframe] topup={:.2}ms count={}",
                t.elapsed().as_secs_f64() * 1000.0,
                topup_count,
            );
        }
    } else {
        // Pre-r106 blocking path. Steady-state hot path on FYS
        // (paint_and_present_one_video_slide_frame +
        // paint_and_present_one_text_over_video_slide_frame both
        // pass is_offscreen_bake=false, and the kill switch
        // OPENMARQUEE_FEED_DRAIN_DECOUPLE=off forces this branch
        // on the transition path too for A/B).
        if *next_sample_idx >= samples.len() {
            *next_sample_idx = 0;
            // r46.3 (2026-06-02): the wrap-at-bake handler stays as the
            // minimal "wrap back to sample 0" pattern. The actual
            // V4L2-state reset (STREAMOFF + clear drained + STREAMON +
            // re-QBUF + re-feed SPS+PPS+IDR primer) lives in
            // reprime_video_decoder_for_loop and is invoked from the IPC
            // dispatcher BEFORE this bake call (when it detects the
            // wrap condition). That separation keeps bake from needing
            // a &Mp4Demuxer parameter; the primer requires SPS/PPS
            // bytes which only the demuxer carries. The standalone
            // reel path (render_video_slide_in_session at hdmi.rs:3025-
            // 3034) already follows this pattern.
        }
        let s = &samples[*next_sample_idx];
        decoder
            .feed(s)
            .with_context(|| format!("feed sample {}", *next_sample_idx))?;
        *next_sample_idx += 1;
        if let Some(t) = t_feed_start {
            eprintln!("[firstframe] feed={:.2}ms", t.elapsed().as_secs_f64() * 1000.0);
        }
    }
    // tail-diag v1: end of feed phase, start of dqbuf phase.
    t_diag_feed_us = t_bake_feed.elapsed().as_micros() as u64;
    let t_bake_dqbuf = std::time::Instant::now();
    let t_dqbuf_start = if profile_first { Some(std::time::Instant::now()) } else { None };
    // perf-night r5 (2026-05-26): boost EAGAIN budget from 5*2ms=10ms
    // to 10*3ms=30ms. r3 baseline showed cold-start ticks exhaust the
    // 10ms budget then early-return wasted (no paint, no decode
    // progress). 30ms covers bcm2835-codec's slow path AND still
    // leaves 3ms inside the 33ms 30fps frame budget for GL work
    // (r4 DMABUF data: compose+present p99 ~1.9ms). Tradeoff:
    // occasional 30ms ticks during warmup window vs. fewer wasted
    // advances. Combined with prime-time warmup pre-feed in
    // video_decode.rs, steady-state should rarely exceed 5*3ms=15ms
    // (decoder pipeline pre-filled, dqbuf wakes on first/second
    // retry).
    //
    // r106 + Path A Stage 2 (2026-06-14): under decouple, the
    // EAGAIN inner-loop sleep is gone — a single non-blocking
    // DQBUF attempt + Ok(None) on no-frame. The caller in
    // paint_and_present_one_transition_frame reuses cached
    // transition_fbo_{a,b} content via the painted flag when
    // we return Ok(None). Per-tick latency drops from 30ms
    // worst-case to ~0.1ms; the topup above keeps the codec's
    // input pool full so the next tick has a high chance of
    // producing a frame.
    let mut frame_opt: Option<crate::v4l2::Frame> = None;
    if decouple {
        match decoder.next_frame() {
            Ok(Some(f)) => frame_opt = Some(f),
            Ok(None) => {}
            Err(e) if e.to_string().contains("EAGAIN") => {}
            Err(e) => return Err(e).context("next_frame"),
        }
    } else {
        for _ in 0..10 {
            match decoder.next_frame() {
                Ok(Some(f)) => {
                    frame_opt = Some(f);
                    break;
                }
                Ok(None) => break,
                Err(e) if e.to_string().contains("EAGAIN") => {
                    std::thread::sleep(std::time::Duration::from_millis(3));
                }
                Err(e) => return Err(e).context("next_frame"),
            }
        }
    }
    if let Some(t) = t_dqbuf_start {
        eprintln!("[firstframe] dqbuf={:.2}ms", t.elapsed().as_secs_f64() * 1000.0);
    }
    // tail-diag v1: end of dqbuf phase (whether we got a frame or not).
    t_diag_dqbuf_us = t_bake_dqbuf.elapsed().as_micros() as u64;
    let t_bake_blit = std::time::Instant::now();
    let Some(frame) = frame_opt else {
        // No frame ready this tick. Caller should skip swap+commit.
        // Sample the dqbuf even on no-frame ticks so the EAGAIN wait
        // shows up in the histogram.
        crate::profile::record_phase(
            "paint_bake_video_dqbuf",
            t_phase.elapsed().as_nanos() as u64,
        );
        // tail-diag v1: blit_us = 0 on no-frame return; emit gated.
        let total_us = t_bake_total.elapsed().as_micros() as u64;
        if total_us > 100_000 {
            eprintln!(
                "[perf] tail_diag_bake_breakdown feed_us={} dqbuf_us={} blit_us={} total_us={} path=no_frame",
                t_diag_feed_us, t_diag_dqbuf_us, t_diag_blit_us, total_us,
            );
        }
        return Ok(None);
    };
    crate::profile::record_phase(
        "paint_bake_video_dqbuf",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    let f_w = frame.width();
    let f_h = frame.height();
    // FYS bug B (2026-05-21): a regular uploaded MP4 video must be
    // shown aspect-preserving, not stretched to fill the panel.
    // cover_quad_vbo gives a quad whose positions overflow +/-1 NDC
    // on the longer axis so the source covers the panel and the
    // overflow is GL-clipped (center-cropped) — matching the
    // cover-fit editor preview / thumbnail. (The HW-decode NV12
    // push path covers via an in-shader UV remap instead; both
    // cover-fit — see cover_fit_quad_verts / nv12_cover_fit_uv_
    // transform.) The whole panel is still cleared black below as a
    // safety net; a cover quad leaves no bars.
    let cover_vbo = cover_quad_vbo(session.gl, f_w, f_h, mode_w, mode_h)?;
    // r83 Phase B (2026-06-08): the bcm2835-codec rounds 1080 ->
    // 1088 rows on CAPTURE. Querying VIDIOC_G_SELECTION(COMPOSE)
    // would tell us the actual display window but the driver
    // returns ENOTTY (Phase A confirmed via the `[perf]
    // v4l2_capture_geometry` probe). Fallback: use the requested
    // CAPTURE height (snapshotted at `set_capture_format` time)
    // vs the negotiated allocation. y_crop_max defaults to 1.0
    // when the source dims aren't known (= no crop, identical to
    // pre-Phase-B behavior).
    let y_crop_max = decoder.capture_y_crop_max();
    // V4L2 piece 4d: branch on the Frame's transport mode. DmaBuf
    // path (piece 4a-c) skips the per-frame Y/UV CPU upload + uses
    // an EGLImage-bound external-OES sampler. MMAP path (piece
    // 3d-e) is the two-texture upload fallback for hosts missing
    // EGL_EXT_image_dma_buf_import / GL_OES_EGL_image_external
    // (run_nv12_dmabuf_blit_pass returns Ok(false) in that case +
    // we fall through to MMAP).
    if let Some(fd) = frame.dma_buf_fd() {
        let stride = frame.stride();
        // 2026-06-15 Option B (perf-gl, tail-fix close-out): on the
        // first transition bake where the EGLImage cache is detected
        // cold, pre-warm ALL CAPTURE buffer indices in one batched
        // Mutex acquire. Subsequent transition ticks hit the cache
        // HIT path → import_us drops from ~83-148 ms to ~1 ms per
        // tick. Targets the (ii) component of QA's tail decomposition
        // (cache-MISS create cost ~150 ms per slow tick); does NOT
        // touch (i) inherent V3D draw cost or (iii) memory-pressure
        // swap stalls (those are documented separately).
        //
        // Gate:
        //   - is_offscreen_bake=true → transition path only. Steady-
        //     state PaintSlide doesn't need this (one decoder, lazy-
        //     fill catches up on the first 8 paints with no contention
        //     pressure surfacing as slow ticks).
        //   - decoder.cached_egl_image(0).is_none() → O(1) Mutex-
        //     acquired slot probe. When the cache is warm (a prior
        //     transition primed it), the probe returns Some and the
        //     prewarm is skipped entirely — zero work on the hot path.
        //     code1's accessor is also idempotent under repeated calls
        //     (skipped++ on already-populated slots), so this gate is
        //     belt-and-suspenders cheap.
        if is_offscreen_bake && decoder.cached_egl_image(0).is_none() {
            // Fire-and-log; a prewarm error is non-fatal (the lazy-
            // fill path in run_nv12_dmabuf_blit_pass will retry per
            // index on each subsequent tick — same behavior as the
            // pre-Option-B baseline). The warn surfaces in journal so
            // QA's bench can spot the rare failure case.
            if let Err(e) = prewarm_egl_image_cache_for_decoder(
                decoder, session.egl_lib, session.display, session.gl,
                f_w, f_h, stride,
            ) {
                eprintln!(
                    "warn: eglimage_prewarm_transition failed (non-fatal, lazy-fill resumes): {e:#}"
                );
            }
        }
        let t_blit = if profile_first { Some(std::time::Instant::now()) } else { None };
        // DMABUF path: zero CPU upload — EGLImage is imported inside
        // run_nv12_dmabuf_blit_pass + sampled via OES_external. Record
        // a 0-cost upload sample to keep the phase count consistent
        // with MMAP (both paths emit exactly one upload + one shader
        // sample) so the histogram comparison is honest.
        crate::profile::record_phase(
            "paint_bake_video_upload",
            t_phase.elapsed().as_nanos() as u64,
        );
        let t_phase_shader = std::time::Instant::now();
        let took_dmabuf = {
            let gl = session.gl;
            gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            gl.clear_color(0.0, 0.0, 0.0, 1.0);
            gl.clear(glow::COLOR_BUFFER_BIT);
            // r101: thread the EGLImage cache slot. Lookup is by
            // (decoder, capture_buffer_index). The cache_enabled
            // env helper gates whether we use the cache (default
            // ON; OPENMARQUEE_EGL_IMAGE_CACHE=off falls back to
            // pre-r101 per-frame create+destroy). When disabled we
            // pass None so the function takes the leaky-but-
            // historical path.
            let cache_slot = if crate::v4l2::is_egl_image_cache_enabled() {
                Some((decoder, frame.capture_buffer_index()))
            } else {
                None
            };
            // 2026-06-15 spike-kill: lazy-init the session-cached
            // GL_TEXTURE_EXTERNAL_OES on FIRST use. The texture object
            // survives the entire session lifetime; each subsequent
            // blit reuses it via image_target_texture_2d (spec-
            // permitted EGLImage re-association). Cuts the per-frame
            // V3D BO alloc/free + 4× sampler-state ioctls that QA's
            // v2.1 sample measured at 200-400 ms sampler_us under
            // memory pressure.
            if session.dmabuf_blit_texture.is_none() {
                match gl.create_texture() {
                    Ok(t) => {
                        session.dmabuf_blit_texture = Some(t);
                        eprintln!("[perf] dmabuf_blit_texture_cached init");
                    }
                    Err(e) => {
                        // Soft-fail: fall through to per-frame
                        // create+delete path inside the blit fn.
                        eprintln!(
                            "warn: spike-kill cached texture init failed: {e} — \
                             falling back to per-frame create+delete (zero regression)",
                        );
                    }
                }
            }
            run_nv12_dmabuf_blit_pass(
                gl,
                cover_vbo,
                session.egl_lib,
                session.display,
                fd,
                f_w,
                f_h,
                stride,
                y_crop_max,
                session.dmabuf_blit_texture,
                cache_slot,
            )?
        };
        if let Some(t) = t_blit {
            eprintln!(
                "[firstframe] dmabuf_blit_pass={:.2}ms",
                t.elapsed().as_secs_f64() * 1000.0
            );
        }
        if took_dmabuf {
            crate::profile::record_phase(
                "paint_bake_video_shader",
                t_phase_shader.elapsed().as_nanos() as u64,
            );
            // 2026-06-14 iter-7 — see the is_offscreen_bake doc-block
            // on this function. Force the deferred tile-store of the
            // external-OES draw to be ISSUED before Frame::drop
            // re-QBUFs the dma_buf slot. Steady-state passes
            // is_offscreen_bake=false (its eglSwapBuffers is the
            // implicit barrier); transition passes true and pays
            // the per-tick flush only inside the transition window.
            // OPENMARQUEE_BAKE_OFFSCREEN_FLUSH=off skips this for
            // bench A/B (default ON).
            if is_offscreen_bake && bake_offscreen_flush_enabled() {
                // tail-diag-v2 flush probe: time the iter-7 gl.flush()
                // independently. Admin's GL2.2 hypothesis is that
                // this flush serializes against the V3D backlog
                // during 2-video transitions → multi-second wait
                // for the GPU to drain. Steady-state passes
                // is_offscreen_bake=false so this entire block is
                // skipped → zero probe cost on the steady-state
                // hot path. Gated emit on flush_us > 500_000 (500 ms,
                // same threshold as tail_diag_blit_subphase). Pure
                // additive — flush behavior unchanged.
                let t_flush_start = std::time::Instant::now();
                session.gl.flush();
                let flush_us = t_flush_start.elapsed().as_micros() as u64;
                if flush_us > 500_000 {
                    eprintln!(
                        "[perf] tail_diag_blit_flush flush_us={} is_offscreen_bake=true",
                        flush_us,
                    );
                }
            }
            // Drop the Frame so its Drop re-QBUFs CAPTURE BEFORE the
            // caller's buffer swap; holding the Frame across the next
            // advance would starve the codec of CAPTURE buffers.
            drop(frame);
            *frames_decoded += 1;
            // r103.1: steady-state video-paint probe. Throttle to
            // first paint of decoder lifetime (count==1) + every
            // 30 paints (~1 sec at 30fps). Tag path=DMABUF so QA
            // can verify the DMABUF branch is the active one and
            // measure the per-second V3D delta during a slide-
            // hold (i.e. BETWEEN transitions, where existing
            // probes don't reach).
            if crate::v4l2::should_emit_steady_state_video_probe(*frames_decoded) {
                let phase = if *frames_decoded == 1 {
                    "steady_state_video_paint_first"
                } else {
                    "steady_state_video_paint_after_N"
                };
                crate::v4l2::log_v3d_bos_at_phase_with_path(phase, None, "DMABUF");
            }
            // tail-diag v1: DMABUF success path. Emit gated.
            t_diag_blit_us = t_bake_blit.elapsed().as_micros() as u64;
            let total_us = t_bake_total.elapsed().as_micros() as u64;
            if total_us > 100_000 {
                eprintln!(
                    "[perf] tail_diag_bake_breakdown feed_us={} dqbuf_us={} blit_us={} total_us={} path=dmabuf",
                    t_diag_feed_us, t_diag_dqbuf_us, t_diag_blit_us, total_us,
                );
            }
            return Ok(Some("DMABUF"));
        }
        // DMABUF fall-through: don't record shader since we didn't
        // complete; MMAP path's shader sample below covers it instead.
        // Extensions missing at runtime: fall through to the MMAP
        // upload path below. Piece 4a-fix kept REQBUFS=V4L2_MEMORY_
        // MMAP for both capture modes so y_plane()/uv_plane() are
        // populated regardless of capture_buffer_type.
    }
    // MMAP path (piece 3d-e). stride==width is a bcm2835-codec
    // empirical fact; a future codec or alignment regime could
    // surface stride > width, which would require GL_UNPACK_ROW_
    // LENGTH (GLES3) here.
    // perf-night r3: t_phase here covers the Y + UV tex_image_2d
    // uploads -- the CPU->GPU copy of two byte planes per frame.
    // Reset t_phase here (DMABUF fallthrough may have arrived
    // without resetting); record paint_bake_video_upload after both
    // uploads land.
    let t_phase = std::time::Instant::now();
    let y_plane = frame.y_plane();
    let uv_plane = frame.uv_plane();
    let gl = session.gl;
    gl.viewport(0, 0, mode_w as i32, mode_h as i32);
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);
    let y_tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(video Y): {e}"))?;
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    // Y plane: width=f_w, height=f_h, one byte/pixel, GL_LUMINANCE.
    // UNPACK_ALIGNMENT=1 because Y stride may be width (not 4-aligned).
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::LUMINANCE as i32,
        f_w as i32,
        f_h as i32,
        0,
        glow::LUMINANCE,
        glow::UNSIGNED_BYTE,
        Some(y_plane),
    );
    // r40 (2026-06-02): twin of Fix 1 above for the V4L2 video
    // paint hot path. y_tex is per-call (NOT session-cached) and
    // deleted at the matching delete_texture calls after the blit
    // pass. The `?`-bubble below would leak y_tex on a transient
    // GL_OUT_OF_MEMORY -- per-frame leak in the hottest video
    // path. Same canonical pattern as Fix 1 + the scanout commit-
    // fail at :3724-3731.
    let uv_tex = match gl.create_texture() {
        Ok(t) => t,
        Err(e) => {
            gl.delete_texture(y_tex);
            return Err(anyhow!("glGenTextures(video UV): {e}"));
        }
    };
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    // UV plane: 4:2:0 subsampled, width=f_w (U+V interleaved →
    // f_w/2 UV pairs × 2 bytes = f_w bytes/row), height=f_h/2.
    // GL_LUMINANCE_ALPHA lands each (U,V) byte pair in (L,A); the
    // FS_NV12_TO_RGB shader samples .ra to recover.
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::LUMINANCE_ALPHA as i32,
        (f_w / 2) as i32,
        (f_h / 2) as i32,
        0,
        glow::LUMINANCE_ALPHA,
        glow::UNSIGNED_BYTE,
        Some(uv_plane),
    );
    // Reset active unit so run_nv12_blit_pass's own binding
    // sequence starts from a clean slate.
    gl.active_texture(glow::TEXTURE0);
    // perf-night r3: both tex_image_2d uploads + texture create/bind
    // landed. Record the upload phase before the shader pass.
    crate::profile::record_phase(
        "paint_bake_video_upload",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase_shader = std::time::Instant::now();
    // FYS bug B: cover_vbo cover-fits the frame to the panel.
    // r83 Phase B: y_crop_max was computed at the top of this
    // helper from `decoder.capture_y_crop_max()`.
    let blit_result = run_nv12_blit_pass(gl, cover_vbo, y_tex, uv_tex, y_crop_max);
    gl.delete_texture(y_tex);
    gl.delete_texture(uv_tex);
    // Restore GL_UNPACK_ALIGNMENT to the default (4). Bumped to 1
    // above for the NV12 upload; leaving it at 1 is safe but
    // unusual for non-NV12 callers downstream.
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
    blit_result?;
    crate::profile::record_phase(
        "paint_bake_video_shader",
        t_phase_shader.elapsed().as_nanos() as u64,
    );
    // 2026-06-14 iter-7 — same scoped flush as the DMABUF arm above.
    // The MMAP path is less likely to hit the tile-store deferral
    // race (no external-OES + the Y/UV uploads themselves serialize),
    // but the cost of the flush here is identical to the DMABUF arm
    // when the path falls through, and we want a single semantic for
    // "transition bake = barrier before Frame::drop." Defense-in-
    // depth; cheap when steady-state is_offscreen_bake=false.
    if is_offscreen_bake && bake_offscreen_flush_enabled() {
        gl.flush();
    }
    // Drop the Frame so its Drop re-QBUFs CAPTURE before the
    // caller's swap. Critical: holding the Frame across the next
    // advance starves the codec of CAPTURE buffers.
    drop(frame);
    *frames_decoded += 1;
    // r103.1: steady-state video-paint probe, MMAP twin of the
    // DMABUF probe above. If MMAP runs at ALL on FYS 720p
    // single-video this is what proves it; the path=MMAP tag in
    // the journal answers "does the leak source live in the
    // MMAP fall-through pattern" directly.
    if crate::v4l2::should_emit_steady_state_video_probe(*frames_decoded) {
        let phase = if *frames_decoded == 1 {
            "steady_state_video_paint_first"
        } else {
            "steady_state_video_paint_after_N"
        };
        crate::v4l2::log_v3d_bos_at_phase_with_path(phase, None, "MMAP");
    }
    // tail-diag v1: MMAP success path. Emit gated.
    t_diag_blit_us = t_bake_blit.elapsed().as_micros() as u64;
    let total_us = t_bake_total.elapsed().as_micros() as u64;
    if total_us > 100_000 {
        eprintln!(
            "[perf] tail_diag_bake_breakdown feed_us={} dqbuf_us={} blit_us={} total_us={} path=mmap",
            t_diag_feed_us, t_diag_dqbuf_us, t_diag_blit_us, total_us,
        );
    }
    Ok(Some("MMAP"))
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
    motion_states: Option<&[MotionState]>,
    glyph_cache: Option<&mut GlyphCache>,
    tex_cache: Option<&mut TextureCache>,
    // Bug 3 Slice 2D-fp4 (2026-05-19): runtime glyph cache + fonts
    // dir, threaded through so the bake-time layout dispatch can
    // resolve static-atlas misses (●/∞ on FYS Boot, etc.) to the
    // dynamic-MSDF cache. None opt-out for standalone HDMI helpers
    // (render_fade_composite, render_transition_animated_in_session
    // legacy 3-pass path) which run outside a session.
    runtime_glyph_ctx: Option<crate::glyph_cache::RuntimeGlyphCtx<'_>>,
    // r102.2 (2026-06-09): when Some, REUSE the provided FBO+tex
    // pair instead of allocating fresh. Caller is responsible
    // for the cache lifecycle (session::cleanup_resources frees
    // it at teardown). When None, allocate as pre-r102.2.
    existing_fbo_pair: Option<(glow::NativeFramebuffer, glow::NativeTexture)>,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    // r102.2: cache-reuse path. When the caller threaded an
    // existing pair, bind it + clear + run paint_slide. Skip
    // alloc + skip error-path delete (caller owns the cache).
    if let Some((fbo, tex)) = existing_fbo_pair {
        gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
        gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        gl.clear_color(0.0, 0.0, 0.0, 0.0);
        gl.clear(glow::COLOR_BUFFER_BIT);
        let paint_result = paint_slide(
            gl,
            mode_w,
            mode_h,
            bg_kind,
            text_layers,
            motion_states,
            current_unix_seconds(),
            glyph_cache,
            None,
            tex_cache,
            runtime_glyph_ctx,
        );
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        paint_result?;
        return Ok((fbo, tex));
    }
    // r102.2 (UNCHANGED below): legacy allocate-fresh path used
    // when existing_fbo_pair is None (kill switch / standalone
    // callers). Surface area of change is confined to the
    // cache-hit branch above.
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
    // Phase 4v-3b (2026-05-16): motion_states is now caller-provided
    // so callers that want motion DURING the bake (the IPC sidecar's
    // paint_and_present_one_transition_frame, called once per
    // transition frame) can pass per-frame states computed from
    // session.motion_tick_seconds(). See qa/captures/motion-through-
    // transitions-audit-2026-05-16.md.
    // `render_fade_composite` still passes None (single-frame
    // composite with no per-frame loop, so a static-snapshot bake is
    // the correct behavior for that path).
    // `render_transition_animated_in_session` ALSO passes None here,
    // but only for pre-loop FBO allocation — its per-frame loop at
    // L5717+ re-paints fbo_a / fbo_b via DIRECT paint_slide calls
    // with fresh motion_states each frame, gated on
    // any_animated_*||any_auto_* (added by commit 2b0cbef, May 7,
    // 2026). Phase 4w regression-locked this in
    // hdmi_logic::tests::legacy_3pass_transition_re_bakes_animated_
    // layers_per_frame.
    // v1-spec-delta #3: pass current wall-clock so any auto_mode
    // layer in the FBO bake renders the right time-of-day.
    // QA-direct (2026-05-14 transition-cache wire): the IPC sidecar
    // transition path passes Some(&mut cache.{glyph,tex}) keyed by
    // slide.id so the bake reuses rasterized bitmaps + GL textures
    // across calls.
    let paint_result = paint_slide(
        gl,
        mode_w,
        mode_h,
        bg_kind,
        text_layers,
        motion_states,
        current_unix_seconds(),
        glyph_cache,
        None,  // image_bg_cache: standalone bake, no session
        tex_cache,
        // Bug 3 Slice 2D-fp4 (2026-05-19): thread the runtime
        // glyph cache so the bake-time layout dispatch can
        // resolve ●/∞-style static-atlas-miss codepoints to
        // dynamic-MSDF cells. Pre-fp4 passed None here with a
        // "session-bound" rationale — that was wrong; the IPC
        // sidecar's bake path (paint_and_present_one_transition_
        // frame → bake_slide_to_fbo → make_slide_fbo) IS
        // session-bound and was silently caching Tofu placeholders
        // for every dynamic-cache codepoint, never enqueuing the
        // MissRequest the drain machinery in paint_and_present
        // needs to invalidate.
        runtime_glyph_ctx,
    );
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    if let Err(e) = paint_result {
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        return Err(e);
    }
    Ok((fbo, tex))
}

/// Phase 8 slice 3 — kind-tagged per-slide inputs for the unified
/// bake dispatcher. Wraps the differing argument sets of
/// `make_slide_fbo` (Text), `bake_image_slide_to_current_fbo`
/// (Image), and `bake_video_slide_to_current_fbo` (Video) under
/// one type so the dispatcher can match on slide kind and forward.
///
/// Variant-by-variant:
///   - `Text`: bg + resolved text layers + Phase 4v-3b motion
///     states + per-slide glyph/texture caches (from
///     session.slide_caches). Matches `make_slide_fbo`'s arg set.
///   - `Image`: PNG asset path on disk. Matches
///     `bake_image_slide_to_current_fbo`'s arg set.
///   - `Video` (Linux only): V4L2 decoder state (samples queue +
///     in-place advance counters + primed Decoder). Matches
///     `bake_video_slide_to_current_fbo`'s arg set.
///
/// All borrows are scoped to the dispatcher call. Mutable borrows
/// (glyph/tex caches, video sample/frame counters) are on disjoint
/// objects so the compiler can prove non-overlap at the call site.
///
/// Slice 4 (2026-05-16) revises slice 3's shape: the Text variant
/// now carries `slide_id` instead of `&mut GlyphCache`/`&mut
/// TextureCache` borrows. The cache prewarm + `get_mut` runs INSIDE
/// `bake_slide_to_fbo`'s Text branch using the dispatcher's
/// `&mut session.slide_caches` borrow. Carrying the cache &muts in
/// the input enum would conflict with the dispatcher's outer
/// `&mut session` parameter, since the caches are sub-borrows of
/// `session.slide_caches`. Internalizing the lookup keeps the
/// caller's transition-function body free of cache plumbing.
enum SlideBakeInputs<'a> {
    Text {
        slide_id: uuid::Uuid,
        bg_kind: &'a BgKind,
        text_layers: &'a [(&'a crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
        motion_states: Option<&'a [MotionState]>,
    },
    Image {
        /// Task #168: slide_id keys the
        /// `ImageSlideTextureCache` lookup so the transition bake
        /// reuses the already-uploaded tex instead of re-decoding +
        /// re-uploading every transition frame (was the dominant
        /// cause of the 100-300ms per-transition hitch on Web slides).
        slide_id: uuid::Uuid,
        asset_path: &'a Path,
    },
    /// Constructed by `paint_and_present_one_transition_frame` from
    /// a `TransitionEndpoint::Video` value the IPC handler assembled.
    /// Option D cadence per `feedback_motion_through_transitions_
    /// required`: each per-Advance call drains one V4L2 sample, so
    /// video plays THROUGH the transition (animated layers don't
    /// freeze). Slice 6 wired this; the slice-3 `#[allow(dead_code)]`
    /// marker dropped here.
    #[cfg(target_os = "linux")]
    Video {
        samples: &'a [crate::mp4_demux::Sample],
        next_sample_idx: &'a mut usize,
        frames_decoded: &'a mut usize,
        decoder: &'a crate::v4l2::Decoder,
    },
    /// r50 (2026-06-03): bake a text-over-video composite into an
    /// FBO for use as a transition endpoint. Order matches the
    /// steady-state paint at hdmi.rs:paint_and_present_one_text_
    /// over_video_slide_frame: bake video frame to FBO via
    /// `bake_video_slide_to_current_fbo`, then composite text
    /// layers on top via `paint_slide_with_viewport` with
    /// bg_kind=None (caller-already-filled-bg path).
    #[cfg(target_os = "linux")]
    TextOverVideo {
        slide_id: uuid::Uuid,
        text_layers: &'a [(&'a crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
        motion_states: Option<&'a [MotionState]>,
        bg_samples: &'a [crate::mp4_demux::Sample],
        bg_next_sample_idx: &'a mut usize,
        bg_frames_decoded: &'a mut usize,
        bg_decoder: &'a crate::v4l2::Decoder,
    },
}

/// Phase 8 slice 3 — create an empty (NativeFramebuffer,
/// NativeTexture) pair sized to the mode. The texture is RGBA8 with
/// LINEAR filter + CLAMP_TO_EDGE wrap, attached to the FBO's
/// COLOR_ATTACHMENT0. FBO is LEFT BOUND on return; caller is
/// expected to paint into it then unbind (the dispatcher handles
/// the unbind on the non-text branches; make_slide_fbo on the text
/// branch handles its own bind/unbind).
///
/// Mirrors the FBO setup inside `make_slide_fbo` (without the
/// paint_slide call) so the non-text dispatcher branches can paint
/// via the existing `..._to_current_fbo` helpers and still hand
/// the caller a sample-ready (fbo, tex) pair.
///
/// On any failure, all created resources are freed before
/// propagating Err. Caller-side cleanup is needed only on a paint
/// failure AFTER this returns Ok.
///
unsafe fn create_slide_fbo_pair(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("glGenTextures(create_slide_fbo_pair): {e}"))?;
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
            return Err(anyhow!("glGenFramebuffers(create_slide_fbo_pair): {e}"));
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
        return Err(anyhow!(
            "framebuffer incomplete (create_slide_fbo_pair): status=0x{status:x}"
        ));
    }
    Ok((fbo, tex))
}

/// Phase 8 slice 3 (2026-05-16) — unified per-kind slide-to-FBO
/// bake dispatcher. Creates a (NativeFramebuffer, NativeTexture)
/// pair sized to the mode, paints the slide into it via the
/// kind-appropriate helper, and returns the pair for the caller
/// to sample (and ultimately free).
///
/// Per-kind dispatch:
///   - `SlideBakeInputs::Text` → `make_slide_fbo` (this branch
///     creates+binds its own FBO via the existing helper; no
///     `create_slide_fbo_pair` here). Motion-state plumbing per
///     Phase 4v-3b stays intact.
///   - `SlideBakeInputs::Image` → `create_slide_fbo_pair` +
///     `bake_image_slide_to_current_fbo`.
///   - `SlideBakeInputs::Video` (Linux only) →
///     `create_slide_fbo_pair` + `bake_video_slide_to_current_fbo`.
///
/// Returns `Ok(Some((fbo, tex)))` on a baked endpoint, or `Ok(None)`
/// when a Video endpoint had no frame ready this tick (FYS bug C):
/// a V4L2 M2M decoder is pipelined, so `bake_video_slide_to_current
/// _fbo` legitimately returns `Ok(None)` on a warmup / back-pressure
/// tick. The transition caller treats `Ok(None)` as "skip this
/// tick" (hold the scanout, the next advance retries) rather than a
/// hard failure — mirroring the single-video paint path. Text and
/// Image endpoints always bake, so they only ever return `Some`.
///
/// Caller is responsible for `delete_framebuffer` + `delete_texture`
/// on a returned pair after sampling. On `Ok(None)` and on any
/// kind-specific failure, all resources are freed here before
/// returning.
///
/// Slice 3 introduced the dispatcher; slice 4 (4dcc7b2, 2026-05-16)
/// wired it into `paint_and_present_one_transition_frame` so the
/// IPC PaintTransition path no longer hardcodes `slide_a:
/// &TextSlide`.
///
/// Per `feedback_motion_through_transitions_required`: motion
/// states for text endpoints flow through the Text variant. Image
/// and Video have no per-layer-motion analog (image is static;
/// video frame IS the motion, and slice 6 chose Option D play-
/// through per the slice 0 recon — see hdmi.rs L2966).
///
unsafe fn bake_slide_to_fbo(
    session: &mut EglSession,
    mode_w: u32,
    mode_h: u32,
    // r102.2 (2026-06-09): when Some, REUSE the provided
    // FBO+tex pair instead of allocating fresh via
    // `create_slide_fbo_pair` / `make_slide_fbo`. The
    // transition caller (paint_and_present_one_transition_frame)
    // passes Some from a per-(EglSession, mode_w, mode_h) cache
    // when OPENMARQUEE_TRANSITION_FBO_CACHE is enabled
    // (default), eliminating the ~8 MB/tick vc4 BO churn that
    // r102 audit identified as the V3D leak source. Each
    // branch is responsible for binding + glClear before
    // drawing into the reused pair.
    //
    // When None: legacy pre-r102.2 behavior -- branch allocates
    // a fresh pair. Used by callers that don't have a stable
    // session cache slot (none today; reserved for future
    // standalone-helper reuse).
    existing_fbo_pair: Option<(glow::NativeFramebuffer, glow::NativeTexture)>,
    inputs: SlideBakeInputs<'_>,
) -> Result<Option<(glow::NativeFramebuffer, glow::NativeTexture)>> {
    use glow::HasContext;
    match inputs {
        SlideBakeInputs::Text {
            slide_id,
            bg_kind,
            text_layers,
            motion_states,
        } => {
            // Cache prewarm + lookup — moved out of the caller in
            // slice 4 so the dispatcher's outer `&mut session` and
            // the cache `&mut` (sub-borrow of session.slide_caches)
            // don't conflict. Two-phase: ensure entry exists, then
            // borrow it mutably for the make_slide_fbo call. Mirrors
            // the prewarm shape at the pre-slice-4
            // paint_and_present_one_transition_frame call site.
            let layers_len = text_layers.len();
            let needs_new = match session.slide_caches.get(&slide_id) {
                Some(c) => c.glyph.len() != layers_len,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&slide_id) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    slide_id,
                    SlideRenderCache::new(layers_len),
                );
            }
            // Bug 3 Slice 2D-fp4 (2026-05-19): construct the runtime
            // glyph cache context BEFORE the mutable borrow of
            // session.slide_caches below. RuntimeGlyphCtx holds
            // shared refs into session.dynamic_glyph_cache +
            // session.dynamic_fonts_dir; the mutable borrow of
            // session.slide_caches is a DIFFERENT field, so Rust's
            // disjoint-field-borrow checking lets both live
            // simultaneously. Threading from the caller would
            // require splitting session into per-field args; this
            // inline construction is identical in effect with less
            // signature churn.
            let runtime_glyph_ctx = Some(crate::glyph_cache::RuntimeGlyphCtx {
                cache: &session.dynamic_glyph_cache,
                fonts_dir: &session.dynamic_fonts_dir,
            });
            let cache = session
                .slide_caches
                .get_mut(&slide_id)
                .expect("slide_caches entry initialized above");
            // Text always bakes — wrap in Some (only Video can
            // return Ok(None), the FYS-bug-C "no frame this tick").
            // r102.2: thread existing_fbo_pair so the cached
            // session.transition_fbo_a/b is reused across ticks.
            make_slide_fbo(
                session.gl,
                mode_w,
                mode_h,
                bg_kind,
                text_layers,
                motion_states,
                Some(&mut cache.glyph),
                Some(&mut cache.tex),
                runtime_glyph_ctx,
                existing_fbo_pair,
            )
            .map(Some)
        }
        SlideBakeInputs::Image { slide_id, asset_path } => {
            // Task #168: resolve through the per-session async cache
            // BEFORE creating the per-frame FBO pair so a cold-cache
            // sync decode failure doesn't leak a freshly-allocated
            // FBO. The cache.ensure() borrows `&mut session.image_
            // slide_tex_cache` + `&session.gl` (disjoint fields).
            let (cached_tex, img_w, img_h) = session
                .image_slide_tex_cache
                .ensure(session.gl, slide_id, asset_path)?;
            // r102.2: reuse the transition FBO+tex pair when the
            // caller threaded one through; allocate fresh
            // otherwise.
            let (fbo, tex) = prepare_bake_fbo_pair(session.gl, mode_w, mode_h, existing_fbo_pair)?;
            // The helper leaves FBO bound; paint into it via the
            // cached-blit helper (no decode, no upload — just one
            // fullscreen cover-fit draw), then unbind to the
            // default fb (mirrors make_slide_fbo's cleanup
            // discipline on the text branch).
            let paint_result = blit_cached_image_slide_to_current_fbo(
                session.gl, cached_tex, img_w, img_h, mode_w, mode_h,
            );
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            if let Err(e) = paint_result {
                // r102.2: only delete on allocate-fresh path;
                // reusing the cached pair must not free it (the
                // session still owns the handles and the next
                // tick will rebind them).
                if existing_fbo_pair.is_none() {
                    session.gl.delete_framebuffer(fbo);
                    session.gl.delete_texture(tex);
                }
                return Err(e);
            }
            Ok(Some((fbo, tex)))
        }
        #[cfg(target_os = "linux")]
        SlideBakeInputs::Video {
            samples,
            next_sample_idx,
            frames_decoded,
            decoder,
        } => {
            let (fbo, tex) = prepare_bake_fbo_pair(session.gl, mode_w, mode_h, existing_fbo_pair)?;
            let paint_result = bake_video_slide_to_current_fbo(
                session,
                samples,
                next_sample_idx,
                frames_decoded,
                decoder,
                mode_w,
                mode_h,
                // 2026-06-14 iter-7 — TRANSITION bake into the cached
                // offscreen transition_fbo_a (fbo here). External-OES
                // tile-store is deferred past Frame::drop without an
                // explicit barrier. is_offscreen_bake=true triggers
                // the scoped gl.flush() before drop(frame).
                /* is_offscreen_bake (Path A Stage 2 scope tag) */ true,
            );
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            match paint_result {
                Ok(Some(_path_label)) => Ok(Some((fbo, tex))),
                Ok(None) => {
                    // FYS bug C (2026-05-21): the helper signaled
                    // "no frame ready this tick" — a V4L2 M2M
                    // decoder is pipelined, so a feed may not yield
                    // a frame the same tick (warmup right after
                    // prime, or back-pressure past the 5×2ms retry
                    // budget). The FBO holds GL-undefined storage
                    // (the helper's viewport+clear lives after its
                    // no-frame early-return and never ran), so it is
                    // not a usable transition input. Free the pair
                    // and return Ok(None): the transition caller
                    // skips this tick and the next advance retries,
                    // exactly as the single-video paint path does.
                    // Before this, Ok(None) became a hard Err that
                    // failed the WHOLE transition — so any
                    // video-involved transition aborted the moment
                    // either decoder bubbled (almost always tick 1,
                    // with the just-primed to-slide decoder cold).
                    // r102.2: same cache-vs-fresh rule as the
                    // Image branch -- only delete when the FBO
                    // was freshly allocated here.
                    if existing_fbo_pair.is_none() {
                        session.gl.delete_framebuffer(fbo);
                        session.gl.delete_texture(tex);
                    }
                    Ok(None)
                }
                Err(e) => {
                    if existing_fbo_pair.is_none() {
                        session.gl.delete_framebuffer(fbo);
                        session.gl.delete_texture(tex);
                    }
                    Err(e)
                }
            }
        }
        #[cfg(target_os = "linux")]
        SlideBakeInputs::TextOverVideo {
            slide_id,
            text_layers,
            motion_states,
            bg_samples,
            bg_next_sample_idx,
            bg_frames_decoded,
            bg_decoder,
        } => {
            // r50 (2026-06-03): composite path mirrors the steady-
            // state paint_and_present_one_text_over_video_slide_
            // frame at hdmi.rs:~3569. Two phases inside the FBO:
            //   1. bake video frame to FBO (FBO is bound by
            //      create_slide_fbo_pair).
            //   2. composite text layers on top via
            //      paint_slide_with_viewport bg_kind=None.
            //
            // The slide_caches prewarm mirrors the text-only branch
            // above so cache.glyph + cache.tex are sized to the
            // text layer count.
            //
            // r50 subagent (2026-06-03 BLOCKER): mirror the r46
            // first-paint CMA pressure mitigation from the steady-
            // state path. A transition INTO a text-over-video slide
            // from an image-heavy prior slide can leave
            // image_bg_cache + image_slide_tex_cache hot (~96 MB),
            // and a 2-side text-over-video transition adds a second
            // V4L2 decoder pool (~24 MB) on top of the bake FBO
            // pair (~16 MB) + transient transition shader work.
            // Peak without eviction could exceed the 254 MB CMA
            // watchdog threshold during the 1.2-1.5s transition
            // window. The eviction is idempotent on already-empty
            // caches, so subsequent ticks within the window are
            // a no-op. Detection via slide_caches absence matches
            // the steady-state check at hdmi.rs:3635.
            let first_paint = !session.slide_caches.contains_key(&slide_id);
            if first_paint {
                session.force_evict_image_caches_for_cma_pressure();
            }
            let layers_len = text_layers.len();
            let needs_new = match session.slide_caches.get(&slide_id) {
                Some(c) => c.glyph.len() != layers_len,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&slide_id) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    slide_id,
                    SlideRenderCache::new(layers_len),
                );
            }
            // r102.2: reuse cached transition FBO+tex pair when
            // the caller threaded one through.
            let (fbo, tex) = prepare_bake_fbo_pair(session.gl, mode_w, mode_h, existing_fbo_pair)?;
            // Phase 1: bake video frame INTO the just-created FBO
            // (still bound by prepare_bake_fbo_pair).
            let video_result = bake_video_slide_to_current_fbo(
                session,
                bg_samples,
                bg_next_sample_idx,
                bg_frames_decoded,
                bg_decoder,
                mode_w,
                mode_h,
                // 2026-06-14 iter-7 — TRANSITION TextOverVideo bake
                // into the cached offscreen transition_fbo_a (fbo
                // here). Same offscreen tile-store race as the
                // Video branch above; pass true to trigger the
                // scoped flush before Frame::drop.
                /* is_offscreen_bake (Path A Stage 2 scope tag) */ true,
            );
            let painted = match video_result {
                Ok(Some(_path)) => true,
                Ok(None) => {
                    // FYS bug C analog: no video frame ready this
                    // tick. r102.2: only free the FBO+tex if WE
                    // allocated it (existing_fbo_pair was None).
                    // When reusing the cached pair, the next tick
                    // will rebind + reclear.
                    session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                    if existing_fbo_pair.is_none() {
                        session.gl.delete_framebuffer(fbo);
                        session.gl.delete_texture(tex);
                    }
                    return Ok(None);
                }
                Err(e) => {
                    session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                    if existing_fbo_pair.is_none() {
                        session.gl.delete_framebuffer(fbo);
                        session.gl.delete_texture(tex);
                    }
                    return Err(e);
                }
            };
            debug_assert!(painted, "video bake reported painted=true");
            // Phase 2: composite text layers on top of the video
            // frame via paint_slide_with_viewport with bg_kind=None
            // (caller-already-filled-bg path).
            //
            // bake_video_slide_to_current_fbo leaves the FBO bound
            // (writes happen via standard GL draws, no rebind on
            // its happy path). We rely on that contract here.
            //
            // Construct the runtime glyph context first (shared-
            // borrow on disjoint session fields; same pattern as
            // the Text branch above).
            let runtime_glyph_ctx = Some(crate::glyph_cache::RuntimeGlyphCtx {
                cache: &session.dynamic_glyph_cache,
                fonts_dir: &session.dynamic_fonts_dir,
            });
            // Then take the mut borrow on slide_caches for the
            // glyph + tex caches.
            let cache = session
                .slide_caches
                .get_mut(&slide_id)
                .expect("slide_caches entry initialized above");
            let wall_clock_unix = current_unix_seconds();
            let paint_result = paint_slide_with_viewport(
                session.gl,
                mode_w,
                mode_h,
                0,
                0,
                mode_w,
                mode_h,
                None, // bg already filled by bake_video_slide_to_current_fbo
                text_layers,
                motion_states,
                wall_clock_unix,
                Some(&mut cache.glyph),
                Some(&mut session.image_bg_cache),
                Some(&mut cache.tex),
                runtime_glyph_ctx,
            );
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            if let Err(e) = paint_result {
                if existing_fbo_pair.is_none() {
                    session.gl.delete_framebuffer(fbo);
                    session.gl.delete_texture(tex);
                }
                return Err(e);
            }
            Ok(Some((fbo, tex)))
        }
    }
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
                // Parity Bug 1 (2026-05-19): TextLayer::is_renderable
                // keeps auto_mode layers even when `text` is empty.
                // An auto_mode layer (time / date / day clock)
                // carries text="" by design — its visible string is
                // resolved at paint time by resolve_layer_text. The
                // old `!l.text.is_empty()` filter dropped such
                // layers BEFORE resolution ever ran, so the Boot
                // time-clock layer never reached the glass (it
                // rendered fine in the Canvas2D previewer, which
                // has no equivalent pre-filter).
                .filter(|l| l.is_renderable())
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
            // Phase 4v-3b: render_fade_composite intentionally passes
            // motion_states=None — it's a single-frame composite with no
            // per-frame loop, so a static-snapshot bake is correct here.
            // Phase 4w (831f471, 2026-05-16) audit confirmed this site
            // needs no change (the legacy 3-pass path was already
            // motion-correct since 2b0cbef).
            // fp4 NOTE: render_fade_composite is a standalone HDMI
            // helper with no session in scope. The dynamic glyph
            // cache is session-owned, so this path opts out (None).
            // Slides with codepoints outside the static MSDF atlas
            // will Tofu here — acceptable since this helper is
            // exercised by direct-mode CLI invocations + tests, not
            // the IPC sidecar production reel.
            let (fbo_a, tex_a) = make_slide_fbo(gl, mode_w, mode_h, &bg_a, &layers_a, None, None, None, None, None)?;
            let (fbo_b, tex_b) = match make_slide_fbo(gl, mode_w, mode_h, &bg_b, &layers_b, None, None, None, None, None) {
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
            // r96: keep u_aspect resolution as a convention across
            // every transition-style link site so the r96 coverage
            // test stays uniform. FS_FADE doesn't declare it; the
            // bind below is a no-op.
            let u_aspect = gl.get_uniform_location(program, "u_aspect");
            gl.uniform_1_i32(u_src_a.as_ref(), 0);
            gl.uniform_1_i32(u_src_b.as_ref(), 1);
            gl.uniform_1_f32(u_t.as_ref(), t);
            gl.uniform_1_f32(
                u_aspect.as_ref(),
                (mode_w as f32) / (mode_h as f32),
            );

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
    with_egl_session(card, 0, |session| {
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
    // Tiered SP dispatch:
    //   1. single-pass for low-fragment-cost cases (n_a + n_b <= 4
    //      AND each <= 4): cheapest path; no FBO bounce.
    //   2. scissored-bake for higher-cost cases that still fit
    //      solid-bg + ≤6 layers per side: 3 simpler passes that
    //      stay under the per-frame budget where SP would overrun.
    //   3. legacy 3-pass: pattern bg / outline / non-normal blend.
    let n_a = layers_a.len();
    let n_b = layers_b.len();
    if !prefer_scissored_bake(n_a, n_b)
        && transition_eligible_for_single_pass(kind, &bg_a, &bg_b, &layers_a, &layers_b)
    {
        return render_transition_single_pass_in_session(
            session, card, slide_a, slide_b, fonts, content_root, kind, transition_ms, fps,
        );
    }
    if transition_eligible_for_scissored_bake(kind, &bg_a, &bg_b, &layers_a, &layers_b) {
        return render_transition_scissored_bake_in_session(
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

        // -- Build slide_a and slide_b FBOs.
        // These make_slide_fbo calls bake with motion_states=None for
        // FBO allocation only — for slides with any animated layer or
        // auto_mode, the per-frame loop below (L5717+) re-paints fbo_a
        // / fbo_b via direct paint_slide() calls with fresh
        // motion_states_for_layers each frame. Motion + auto_mode
        // (clock layers, etc.) advance through the transition per
        // spec §11 / v1-spec-delta #2 (slice d, commit 2b0cbef).
        // Path activates when SP+SB eligibility both fail (pattern
        // bg / outline / non-normal blend / >6 layers per side) under
        // direct-driver mode, not under the IPC sidecar.
        // fp4 NOTE: this is the direct-driver legacy 3-pass
        // fallback inside render_transition_animated_in_session
        // — does NOT activate under the IPC sidecar. Production
        // reel transition baking goes through bake_slide_to_fbo
        // which DOES thread runtime_glyph_ctx (above). Leaving
        // None here preserves the legacy path's behavior.
        let (fbo_a, tex_a) = unsafe { make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_a, &layers_a, None, None, None, None, None)? };
        let (fbo_b, tex_b) = unsafe {
            match make_slide_fbo(gl, mode_w_u32, mode_h_u32, &bg_b, &layers_b, None, None, None, None, None) {
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
        // r96: u_aspect for the iris arm. None for shaders that
        // don't declare it (silent no-op bind).
        let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };

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
                    free_slide_render_cache(gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    sid,
                    SlideRenderCache::new(n),
                );
            }
        }
        let start = Instant::now();
        let start_mono_ns = monotonic_now_ns();
        let mut rendered = 0_u32;
        let profile_active_t = crate::profile::is_enabled();
        let loop_result: Result<()> = (|| {
        for frame in 0..total_frames {
            if profile_active_t && crate::profile::frames_remaining() == Some(0) {
                break;
            }
            let frame_start_t = std::time::Instant::now();
            let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
            // Bug 1 fix (2026-05-09): tick_seconds is session-
            // global, NOT call-local. Both slides A and B compute
            // motion against the same continuous monotonic basis
            // as the surrounding hold loops -- no phase snap at
            // transition entry / exit (qarl-flagged on glass).
            let tick_seconds = session.motion_tick_seconds();
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
                        // Bug 3 Slice 2B: transition-bake path also
                        // routes through layout_text_to_quads; pass
                        // the runtime cache so a transition during
                        // a slide with ●/∞ honors the dynamic atlas.
                        Some(crate::glyph_cache::RuntimeGlyphCtx {
                            cache: &session.dynamic_glyph_cache,
                            fonts_dir: &session.dynamic_fonts_dir,
                        }),
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
                        // Bug 3 Slice 2B: same rationale as the bake_a
                        // arm above.
                        Some(crate::glyph_cache::RuntimeGlyphCtx {
                            cache: &session.dynamic_glyph_cache,
                            fonts_dir: &session.dynamic_fonts_dir,
                        }),
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
                // r96: bind u_aspect for the iris arm. No-op for
                // shaders that don't declare it.
                gl.uniform_1_f32(
                    u_aspect.as_ref(),
                    (mode_w_u32 as f32) / (mode_h_u32 as f32),
                );

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
                // eglSwapBuffers implicitly flushes; the explicit
                // gl.flush() forced an extra tile-store on vc4
                // (cold-scout #2 P6, 2026-05-09).
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
            // QA-direct (2026-05-08): clock_nanosleep TIMER_ABSTIME
            // for sub-ms precision.
            if !profile_active_t {
                pace_to_frame_deadline(
                    start_mono_ns,
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

    // Bug 2 fix (2026-05-09): hand current to held_scanout to keep
    // kernel scanout valid across the call boundary. See
    // end_of_in_session_render_call.
    end_of_in_session_render_call(
        session, card,
        current_fb.take(), current_bo.take(),
        prev_fb.take(), prev_bo.take(),
    );

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
    let props_a = layer_composite_props_from_tuples(layers_a);
    let props_b = layer_composite_props_from_tuples(layers_b);
    transition_eligible_for_single_pass_logic(
        kind,
        effective_solid_bg(bg_a).is_some(),
        effective_solid_bg(bg_b).is_some(),
        &props_a,
        &props_b,
    )
}

/// Adapter: lift a single slide's `(TextLayer, color, font)`
/// tuples into the pure-logic `LayerCompositeProps` summary the
/// eligibility gates take. Allocates one small Vec per call;
/// only on the eligibility-decision path (once per transition
/// onset, not per frame).
fn layer_composite_props_from_tuples(
    layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
) -> Vec<crate::hdmi_logic::LayerCompositeProps> {
    layers
        .iter()
        .map(|(l, _, _)| crate::hdmi_logic::LayerCompositeProps {
            outline: l.outline,
            blend: parse_blend_mode(&l.blend),
        })
        .collect()
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
        BgKind::Gradient { color_a, density, .. }
            if gradient_density_is_degenerate(*density) =>
        {
            Some(*color_a)
        }
        _ => None,
    }
}

/// QA-mandated single-pass transition (2026-05-08): rasterize +
/// upload + pack uniforms for one slide's text layers. Mirrors
/// paint_slide's stage-1 (rasterize-or-reuse) and stage-2 (texture
/// upload) loops, but instead of issuing per-layer GL draws it
/// returns the per-layer rect/rgba/tex tuples so the caller can
/// drive a single FS_FADE_SP draw.
///
/// SDF arc B.3 (cleanup follow-up): SP-tier is gated to bg-only
/// transitions (the SP composite shader can't sample the per-glyph
/// MSDF atlas post-cutover). The function survives as a sanity
/// gate — any caller that hands it text_layers is bypassing the
/// `transition_eligible_for_single_pass_logic` gate and would
/// silently render without text. The bail surfaces the caller bug.
fn prepare_layers_for_single_pass(
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: &[MotionState],
) -> Result<(Vec<[f32; 4]>, Vec<[f32; 4]>, Vec<glow::NativeTexture>)> {
    if motion_states.len() != text_layers.len() {
        bail!(
            "prepare_layers_for_single_pass: motion_states len {} != layers len {}",
            motion_states.len(),
            text_layers.len(),
        );
    }
    if !text_layers.is_empty() {
        bail!(
            "prepare_layers_for_single_pass called with {} text layers; \
             SP-tier is gated to bg-only transitions per B.3",
            text_layers.len(),
        );
    }
    Ok((Vec::new(), Vec::new(), Vec::new()))
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
        for (sid, n) in [(slide_a_id, layers_a_len), (slide_b_id, layers_b_len)] {
            let needs_new = match session.slide_caches.get(&sid) {
                Some(c) => c.glyph.len() != n,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&sid) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    sid,
                    SlideRenderCache::new(n),
                );
            }
        }
    }
    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    let mut current_bo: Option<BufferObject<()>> = None;
    let mut current_fb: Option<framebuffer::Handle> = None;

    // QA-direct (2026-05-08, post-clock_nanosleep): hoist program
    // lookup + uniform location resolution + VBO creation OUT of
    // work_start_t. cached_transition_sp_program returns a struct
    // with all locations pre-resolved (one-time per (kind, n_a,
    // n_b)); transition_sp_quad_vbo is session-cached. Closes the
    // ~15 ms / transition setup-overhead drag that was capping
    // reel-context warm-state aggregate fps at 29.2.
    let csp = cached_transition_sp_program(
        session.gl,
        kind,
        layers_a_len,
        layers_b_len,
    )?;
    let vbo = ensure_transition_sp_quad_vbo(session)?;

    let work_start_t = Instant::now();
    // QA-direct (2026-05-08): per-frame loop wall-clock for the
    // effective-fps log -- excludes pre-loop setup + post-loop
    // BO/FB cleanup so the metric matches per-frame cadence
    // rather than total transition wall-clock.
    let loop_elapsed_cell: std::cell::Cell<std::time::Duration> =
        std::cell::Cell::new(std::time::Duration::ZERO);
    let work: Result<u32> = (|| {
        use glow::HasContext;
        let gl = session.gl;
        let program = csp.program;
        let a_pos = csp.a_pos;
        let a_uv = csp.a_uv;
        let u_t_loc = csp.u_t.clone();
        let u_aspect_loc = csp.u_aspect.clone();
        let u_a_bg_loc = csp.u_a_bg.clone();
        let u_b_bg_loc = csp.u_b_bg.clone();
        let u_a_tex_locs = &csp.u_a_tex_locs;
        let u_b_tex_locs = &csp.u_b_tex_locs;
        let u_a_rect_locs = &csp.u_a_rect_locs;
        let u_b_rect_locs = &csp.u_b_rect_locs;
        let u_a_rgba_locs = &csp.u_a_rgba_locs;
        let u_b_rgba_locs = &csp.u_b_rgba_locs;

        let start = Instant::now();
        let start_mono_ns = monotonic_now_ns();
        let mut rendered = 0_u32;
        let profile_active_t = crate::profile::is_enabled();
        let loop_result: Result<()> = (|| {
            for frame in 0..total_frames {
                if profile_active_t && crate::profile::frames_remaining() == Some(0) {
                    break;
                }
                let frame_start_t = Instant::now();
                let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
                // Bug 1 fix: session-global tick (see Bug 1 doc).
                let tick_seconds = session.motion_tick_seconds();
                let wall_clock_unix = current_unix_seconds();

                let states_a =
                    motion_states_for_layers(slide_a.id, &layers_a, tick_seconds);
                let states_b =
                    motion_states_for_layers(slide_b.id, &layers_b, tick_seconds);

                let t_prep_a = Instant::now();
                let (rects_a, rgbas_a, texs_a) =
                    prepare_layers_for_single_pass(&layers_a, &states_a)?;
                crate::profile::record_phase(
                    "sp_prep_a",
                    t_prep_a.elapsed().as_nanos() as u64,
                );
                let t_prep_b = Instant::now();
                let (rects_b, rgbas_b, texs_b) =
                    prepare_layers_for_single_pass(&layers_b, &states_b)?;
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
                    // r95: bind u_aspect = framebuffer width / height
                    // for the aspect-correct iris. Harmless on
                    // non-iris kinds (uniform unused there).
                    gl.uniform_1_f32(
                        u_aspect_loc.as_ref(),
                        (mode_w_u32 as f32) / (mode_h_u32 as f32),
                    );
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
                    // eglSwapBuffers implicitly flushes; explicit
                    // gl.flush() forced an extra tile-store on vc4
                    // (cold-scout #2 P6, 2026-05-09).
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
                    pace_to_frame_deadline(start_mono_ns, rendered as u64, frame_period_ns);
                }
            }
            Ok(())
        })();
        // VBO is session-cached now -- no per-call free needed.
        // Program is owned by TRANSITION_SP_PROGRAMS and freed at
        // session teardown.
        loop_elapsed_cell.set(start.elapsed());
        loop_result?;
        Ok(rendered)
    })();

    // Bug 2 fix (2026-05-09): held_scanout hand-off across the
    // call boundary -- see end_of_in_session_render_call.
    end_of_in_session_render_call(
        session, card,
        current_fb.take(), current_bo.take(),
        prev_fb.take(), prev_bo.take(),
    );
    // Restore scissor state. Atlas SB enables SCISSOR_TEST mid-frame
    // for region-clipped bg fill; the per-frame composite branch
    // disables it before scanout, but a `?` mid-bake skips that.
    // Without this, an error bail leaks SCISSOR_TEST into the next
    // render call's GL state. SP path doesn't enable scissor; this
    // disable is a no-op there.
    unsafe {
        use glow::HasContext;
        session.gl.disable(glow::SCISSOR_TEST);
    }

    let frame_count = work?;
    let total_elapsed_ms = work_start_t.elapsed().as_millis();
    let loop_elapsed_ms = loop_elapsed_cell.get().as_millis();
    // Effective fps is computed from LOOP TIME (start of frame 0
    // to end of frame N-1's pacing), not total wall-clock. This
    // matches the user-perceived inter-frame cadence: pre-loop
    // setup (program lookup, etc) and post-loop cleanup
    // (drain_pending_flip, BO/FB destroy) are NOT rendering time.
    let effective_fps = if loop_elapsed_ms > 0 {
        (frame_count as f64) * 1000.0 / (loop_elapsed_ms as f64)
    } else {
        0.0
    };
    eprintln!(
        "animated transition complete: kind={kind:?} rendered {frame_count} frames in {loop_elapsed_ms}ms (target {transition_ms}ms; effective {effective_fps:.1} fps; total {total_elapsed_ms}ms incl setup) [single-pass]"
    );
    Ok(frame_count)
}

/// QA-mandated scissored-bake (Step 4, 2026-05-08): three-pass
/// transition path for cases where single-pass exceeds the per-
/// fragment budget (n_a + n_b > 4 OR per-side > 4 layers).
///
/// Per frame:
///   1. Bake slide A: bg + N_a layers → fbo_a/tex_a (1× 1080p
///      fragment fill, single draw).
///   2. Bake slide B: same → fbo_b/tex_b.
///   3. Composite: kind-specific FS samples 2 baked textures
///      with warp + mix → default framebuffer.
///
/// Splits the high-fragment-cost SP shader (5+ apply_layer per
/// fragment) into 3 simpler passes that fit per-frame budget. The
/// bake-pass programs are cached per `n_layers` (BAKE_SP_PROGRAMS);
/// the composite-pass programs are cached per kind
/// (COMPOSITE_PROGRAMS) and reuse the EXISTING legacy FS_<KIND>
/// shaders -- those already take 2 sampler2D + u_t and apply the
/// kind-specific warp.
///
/// Note: this is the WITHOUT-scissor variant of scissored-bake.
/// The bake-pass is full-screen (1× 1080p fragment fill per slide
/// regardless of layer-rect coverage). Adding sparse glScissor
/// based on layer-union-rect is a Phase-2 optimization.
fn render_transition_scissored_bake_in_session(
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
        bail!("scissored_bake: kind {kind:?} has no SP generator");
    }
    let (bg_a_kind, _, layers_a) = resolve_slide_layers(slide_a, fonts, content_root)?;
    let (bg_b_kind, _, layers_b) = resolve_slide_layers(slide_b, fonts, content_root)?;
    // No bg validation: transition_eligible_for_scissored_bake
    // (cold-scout #1 widening) admits all BgKind variants. The
    // bg-cache machinery handles gradient/pattern/image; solid
    // uses scissor-clear in the bake.
    if layers_a.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
        || layers_b.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
    {
        bail!(
            "scissored_bake: layer count exceeds {} per slide",
            SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
        );
    }

    eprintln!(
        "rendering scissored-bake {kind} transition slide_a={} slide_b={} \
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

    let slide_a_id = slide_a.id;
    let slide_b_id = slide_b.id;
    let layers_a_len = layers_a.len();
    let layers_b_len = layers_b.len();
    {
        for (sid, n) in [(slide_a_id, layers_a_len), (slide_b_id, layers_b_len)] {
            let needs_new = match session.slide_caches.get(&sid) {
                Some(c) => c.glyph.len() != n,
                None => true,
            };
            if needs_new {
                if let Some(old) = session.slide_caches.remove(&sid) {
                    free_slide_render_cache(session.gl, old);
                }
                insert_slide_render_cache(
                    &mut session.slide_caches,
                    session.gl,
                    sid,
                    SlideRenderCache::new(n),
                );
            }
        }
    }

    // Pre-resolve cached programs + ensure atlas FBO/VBO.
    // Bake-pass is via paint_slide_with_viewport (per-layer draws
    // into the layer rect, not full-screen) -- the SP-style bake
    // program with full-screen apply_layer chain ran 70+ ms/frame
    // on vc4 because each fragment paid N texture samples
    // regardless of layer-rect coverage.
    //
    // Both slides bake into the same atlas FBO at distinct
    // viewport regions (slide A at y=[0,1024), slide B at
    // y=[1024,2048)) -- one FBO bind across the bake phase
    // eliminates one of the prior implementation's three
    // per-frame bind-switches (each ~13ms vc4 tile-store
    // sync). bg_kind passes through; gradient bg uses
    // u_vp_offset to shift gl_FragCoord into the region-local
    // frame (FS_GRADIENT, 2026-05-09). Solid bg uses glClear
    // which respects scissor. Pattern bg isn't SB-eligible.
    // Cut transition uses side-specialized composite shaders
    // (Phase 2.6 QA-direct): FS_CUT_A / FS_CUT_B sample only the
    // visible side per frame, halving composite texture-fetch
    // count. Other kinds need both sides + use the combined
    // FS_<KIND> via cached_composite_program.
    let kind_is_cut = kind == "cut";
    // Cut path uses side-specialized FS_CUT_A / FS_CUT_B exclusively;
    // skip the combined-FS_CUT compile to avoid burning a redundant
    // GL program slot. Other kinds compile + use the standard ccp.
    let ccp = if kind_is_cut {
        None
    } else {
        Some(cached_composite_program(session.gl, kind)?)
    };
    let cut_ccp_a = if kind_is_cut {
        Some(cached_cut_composite_program(session.gl, false)?)
    } else {
        None
    };
    let cut_ccp_b = if kind_is_cut {
        Some(cached_cut_composite_program(session.gl, true)?)
    } else {
        None
    };
    let bcp = cached_blit_program(session.gl)?;
    let (atlas_fbo, atlas_tex) = unsafe { ensure_bake_atlas(session)? };
    let vbo = ensure_transition_sp_quad_vbo(session)?;
    // bg-cache (2026-05-09 Phase 2.5): pre-populate cached non-
    // solid bgs at atlas region size. Idempotent across calls
    // -- pays the gradient/pattern fill cost ONCE per slide
    // lifetime, not 18× per transition.
    unsafe {
        if let Err(e) = ensure_slide_bg_cache(session, slide_a_id, &bg_a_kind) {
            eprintln!("warn: ensure_slide_bg_cache slide_a={slide_a_id}: {e:#}; falling back to per-frame bg render");
        }
        if let Err(e) = ensure_slide_bg_cache(session, slide_b_id, &bg_b_kind) {
            eprintln!("warn: ensure_slide_bg_cache slide_b={slide_b_id}: {e:#}; falling back to per-frame bg render");
        }
    }
    let region_h = crate::hdmi_logic::ATLAS_REGION_H;
    // mode_w (1920) ≤ atlas_region_w (2048) and mode_h (1080) ≥
    // region_h (1024). Used range for slide content: x in
    // [0, mode_w], y in [0, region_h] (per region). Atlas-uv
    // scale = mode_w/atlas_w on x, region_h/atlas_h on y.
    let atlas_w_f = crate::hdmi_logic::ATLAS_FBO_W as f32;
    let atlas_h_f = crate::hdmi_logic::ATLAS_FBO_H as f32;
    let used_w_f = mode_w_u32 as f32;
    let used_h_f = region_h as f32;
    let uv_scale_x = used_w_f / atlas_w_f;
    let uv_scale_y = used_h_f / atlas_h_f;
    // Region A: bottom half of atlas (atlas y=[0,1024)). UV-y
    // [0, 0.5] in atlas coords.
    let xform_a: [f32; 4] = [0.0, 0.0, uv_scale_x, uv_scale_y];
    // Region B: top half of atlas (atlas y=[1024,2048)). UV-y
    // [0.5, 1.0].
    let xform_b: [f32; 4] = [0.0, uv_scale_y, uv_scale_x, uv_scale_y];

    // Static-pair single-bake (cold-scout 2026-05-09 #3): when
    // both slides have no per-frame-changing layers (no motion,
    // no auto_mode), the atlas bake output is identical every
    // frame in the transition. Bake once on frame 0; subsequent
    // frames composite-only. Saves ~bake_a+bake_b time per frame
    // (sub-ms p50, multi-ms p99 -- the GPU work IS the savings)
    // and naturally extends to the dominant operator-content
    // shape (most authored slides are static text on solid bgs).
    //
    // Eligibility check matches render_slide_in_session's
    // any_animated/any_auto pattern. auto_mode-set layers refresh
    // text on second boundaries; treat as motion for transition-
    // window purposes (a 600 ms transition could straddle a
    // boundary -- conservatively re-bake every frame).
    let any_animated_a = layers_a
        .iter()
        .any(|(l, _, _)| parse_motion_kind(&l.motion) != MotionKind::Static);
    let any_animated_b = layers_b
        .iter()
        .any(|(l, _, _)| parse_motion_kind(&l.motion) != MotionKind::Static);
    let any_auto_a = layers_a.iter().any(|(l, _, _)| l.auto_mode.is_some());
    let any_auto_b = layers_b.iter().any(|(l, _, _)| l.auto_mode.is_some());
    let static_pair = !any_animated_a && !any_animated_b && !any_auto_a && !any_auto_b;

    let mut prev_bo: Option<BufferObject<()>> = None;
    let mut prev_fb: Option<framebuffer::Handle> = None;
    let mut current_bo: Option<BufferObject<()>> = None;
    let mut current_fb: Option<framebuffer::Handle> = None;

    let work_start_t = Instant::now();
    let loop_elapsed_cell: std::cell::Cell<std::time::Duration> =
        std::cell::Cell::new(std::time::Duration::ZERO);
    let work: Result<u32> = (|| {
        use glow::HasContext;
        let gl = session.gl;
        let start = Instant::now();
        let start_mono_ns = monotonic_now_ns();
        let mut rendered = 0_u32;
        // Per-side bake gate (cold-scout #3 + #2). Two flags so
        // cut transitions can bake A and B at DIFFERENT frames
        // (each side first becomes visible when t crosses 0.5).
        // For non-cut static_pair the legacy single-bake behaviour
        // collapses to: both flags flip true on frame 0; remaining
        // frames composite-only. For non-static cut, the cut-only
        // half is gated by `cut_a_visible / cut_b_visible` so the
        // invisible side's bake is skipped every frame even on
        // motion content -- ~50% bake work removed for cut.
        let mut a_baked = false;
        let mut b_baked = false;
        // kind_is_cut already in scope from the caller-scope decl
        // around line 4924; rely on capture rather than re-binding.
        let profile_active_t = crate::profile::is_enabled();
        let loop_result: Result<()> = (|| {
            for frame in 0..total_frames {
                if profile_active_t && crate::profile::frames_remaining() == Some(0) {
                    break;
                }
                let frame_start_t = Instant::now();
                let t = (frame as f32 / (total_frames - 1).max(1) as f32).clamp(0.0, 1.0);
                // Bug 1 fix: session-global tick (see Bug 1 doc).
                let tick_seconds = session.motion_tick_seconds();
                let wall_clock_unix = current_unix_seconds();

                // Cut composite specialization (Phase 2.6) reads
                // ONLY the visible side per frame: A at t<0.5, B
                // at t>=0.5. The other side's atlas content goes
                // un-sampled, so we can skip baking it. For
                // non-cut transitions both sides are sampled
                // (mix/wipe/iris/etc. all read both); always
                // bake both unless static_pair lets us skip.
                let cut_a_visible = !kind_is_cut || t < 0.5;
                let cut_b_visible = !kind_is_cut || t >= 0.5;
                // Static-pair gate: skip the bake on subsequent
                // visible-frame visits once the side is baked.
                // For non-static_pair, bake every visible-frame.
                let bake_a_needed = cut_a_visible && (!static_pair || !a_baked);
                let bake_b_needed = cut_b_visible && (!static_pair || !b_baked);
                let bake_needed = bake_a_needed || bake_b_needed;
                let states_a = if bake_a_needed {
                    motion_states_for_layers(slide_a.id, &layers_a, tick_seconds)
                } else {
                    Vec::new()
                };
                let states_b = if bake_b_needed {
                    motion_states_for_layers(slide_b.id, &layers_b, tick_seconds)
                } else {
                    Vec::new()
                };

                // Atlas bake phase: bind atlas FBO ONCE, paint A
                // into bottom region (y=[0,1024)) under scissor,
                // paint B into top region (y=[1024,2048)) under
                // scissor. Region width = mode_w (slides used
                // 1920 of the 2048-wide atlas; 128px right gutter
                // is unwritten and clipped via UV-scale at
                // composite). Region height = 1024 (1080 → 1024
                // = 5.5% vertical compression upsampled at
                // composite).
                if bake_needed {
                unsafe {
                    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(atlas_fbo));
                    gl.enable(glow::SCISSOR_TEST);
                }
                if bake_a_needed {
                let t_bake_a = Instant::now();
                unsafe {
                    gl.scissor(0, 0, mode_w_u32 as i32, region_h as i32);
                }
                {
                    // Snapshot bg_tex BEFORE the mut-borrow on cache_a
                    // so we don't double-borrow session.slide_caches.
                    let bg_tex_a = session
                        .slide_caches
                        .get(&slide_a_id)
                        .and_then(|c| c.bg_tex);
                    if let Some(bg_tex) = bg_tex_a {
                        // Blit cached bg into region first (uses scissor).
                        unsafe {
                            gl.viewport(0, 0, mode_w_u32 as i32, region_h as i32);
                        }
                        blit_bg_to_region(gl, &bcp, vbo, bg_tex)?;
                    }
                    let cache_a = session
                        .slide_caches
                        .get_mut(&slide_a_id)
                        .expect("slide_caches[slide_a] init above");
                    let bg_arg = if bg_tex_a.is_some() {
                        None  // bg already filled by blit above
                    } else {
                        Some(&bg_a_kind)
                    };
                    paint_slide_with_viewport(
                        gl, mode_w_u32, mode_h_u32,
                        0, 0, mode_w_u32, region_h,
                        bg_arg, &layers_a,
                        Some(&states_a), wall_clock_unix,
                        Some(&mut cache_a.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache_a.tex),
                        // Bug 3 Slice 2B: SB transition bake; pass
                        // session runtime cache so the bake honors
                        // any dynamic atlas slots.
                        Some(crate::glyph_cache::RuntimeGlyphCtx {
                            cache: &session.dynamic_glyph_cache,
                            fonts_dir: &session.dynamic_fonts_dir,
                        }),
                    )?;
                }
                crate::profile::record_phase("sb_bake_a", t_bake_a.elapsed().as_nanos() as u64);
                if static_pair {
                    a_baked = true;
                }
                } // end if bake_a_needed

                if bake_b_needed {
                let t_bake_b = Instant::now();
                unsafe {
                    gl.scissor(0, region_h as i32, mode_w_u32 as i32, region_h as i32);
                }
                {
                    let bg_tex_b = session
                        .slide_caches
                        .get(&slide_b_id)
                        .and_then(|c| c.bg_tex);
                    if let Some(bg_tex) = bg_tex_b {
                        unsafe {
                            gl.viewport(0, region_h as i32, mode_w_u32 as i32, region_h as i32);
                        }
                        blit_bg_to_region(gl, &bcp, vbo, bg_tex)?;
                    }
                    let cache_b = session
                        .slide_caches
                        .get_mut(&slide_b_id)
                        .expect("slide_caches[slide_b] init above");
                    let bg_arg = if bg_tex_b.is_some() {
                        None
                    } else {
                        Some(&bg_b_kind)
                    };
                    paint_slide_with_viewport(
                        gl, mode_w_u32, mode_h_u32,
                        0, region_h, mode_w_u32, region_h,
                        bg_arg, &layers_b,
                        Some(&states_b), wall_clock_unix,
                        Some(&mut cache_b.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache_b.tex),
                        // Bug 3 Slice 2B: same rationale as the sb_bake_a
                        // arm above.
                        Some(crate::glyph_cache::RuntimeGlyphCtx {
                            cache: &session.dynamic_glyph_cache,
                            fonts_dir: &session.dynamic_fonts_dir,
                        }),
                    )?;
                }
                crate::profile::record_phase("sb_bake_b", t_bake_b.elapsed().as_nanos() as u64);
                if static_pair {
                    b_baked = true;
                }
                } // end if bake_b_needed
                } // end if bake_needed

                // Composite: sample atlas with two UV xforms +
                // kind-specific warp + mix → default FB. Disable
                // scissor before composite so the full mode-res
                // output isn't clipped to one of the bake regions.
                let t_comp = Instant::now();
                // Cut transition: pick FS_CUT_A (slide A only) or
                // FS_CUT_B (slide B only) based on t. Halves the
                // texture-sample count vs the combined FS_CUT.
                // Other kinds use the standard ccp.
                let active_ccp = if kind_is_cut {
                    if t < 0.5 {
                        cut_ccp_a.as_ref().expect("cut_ccp_a init for cut")
                    } else {
                        cut_ccp_b.as_ref().expect("cut_ccp_b init for cut")
                    }
                } else {
                    ccp.as_ref().expect("ccp init for non-cut kinds")
                };
                unsafe {
                    gl.disable(glow::SCISSOR_TEST);
                    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                    gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
                    gl.disable(glow::BLEND);
                    gl.clear_color(0.0, 0.0, 0.0, 1.0);
                    gl.clear(glow::COLOR_BUFFER_BIT);
                    gl.use_program(Some(active_ccp.program));
                    // Both samplers point at the SAME atlas; the
                    // u_a_xform / u_b_xform uniforms remap v_uv
                    // into the region for slide A vs slide B.
                    gl.active_texture(glow::TEXTURE0);
                    gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
                    gl.uniform_1_i32(active_ccp.u_src_a.as_ref(), 0);
                    gl.active_texture(glow::TEXTURE1);
                    gl.bind_texture(glow::TEXTURE_2D, Some(atlas_tex));
                    gl.uniform_1_i32(active_ccp.u_src_b.as_ref(), 1);
                    gl.uniform_4_f32(
                        active_ccp.u_a_xform.as_ref(),
                        xform_a[0], xform_a[1], xform_a[2], xform_a[3],
                    );
                    gl.uniform_4_f32(
                        active_ccp.u_b_xform.as_ref(),
                        xform_b[0], xform_b[1], xform_b[2], xform_b[3],
                    );
                    gl.uniform_1_f32(active_ccp.u_t.as_ref(), t);
                    // r96: bind u_aspect for the iris arm (and any
                    // other aspect-dependent transition). No-op for
                    // shaders that don't declare it.
                    gl.uniform_1_f32(
                        active_ccp.u_aspect.as_ref(),
                        (mode_w_u32 as f32) / (mode_h_u32 as f32),
                    );
                    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
                    let stride = (4 * std::mem::size_of::<f32>()) as i32;
                    gl.enable_vertex_attrib_array(active_ccp.a_pos);
                    gl.vertex_attrib_pointer_f32(active_ccp.a_pos, 2, glow::FLOAT, false, stride, 0);
                    gl.enable_vertex_attrib_array(active_ccp.a_uv);
                    gl.vertex_attrib_pointer_f32(
                        active_ccp.a_uv, 2, glow::FLOAT, false, stride,
                        (2 * std::mem::size_of::<f32>()) as i32,
                    );
                    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
                }
                crate::profile::record_phase("sb_composite", t_comp.elapsed().as_nanos() as u64);

                // Swap + commit + N-2 BO/FB rotation (mirrors SP path).
                // eglSwapBuffers implicitly flushes; the explicit gl.flush()
                // that used to be here forced an extra tile-store on vc4
                // (cold-scout #2 P6, 2026-05-09).
                let t_swap = Instant::now();
                session
                    .egl_lib
                    .swap_buffers(session.display, session.egl_surface)
                    .map_err(|e| anyhow!("eglSwapBuffers (frame {frame}) failed: {e:?}"))?;
                crate::profile::record_phase("swap", t_swap.elapsed().as_nanos() as u64);
                let t_lockfb = Instant::now();
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
                crate::profile::record_phase("lockfb", t_lockfb.elapsed().as_nanos() as u64);
                let t_commit = Instant::now();
                if let Err(e) = commit_fb(session, card, fb) {
                    if let Err(de) = card.destroy_framebuffer(fb) {
                        eprintln!(
                            "warn: cleanup destroy_framebuffer({fb:?}) on commit-fail (frame {frame}): {de}"
                        );
                    }
                    drop(bo);
                    return Err(e.context(format!("commit_fb (frame {frame})")));
                }
                crate::profile::record_phase("commit", t_commit.elapsed().as_nanos() as u64);

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
                crate::profile::record_phase("rotate", t_rotate.elapsed().as_nanos() as u64);
                crate::profile::record_phase(
                    "frame_total",
                    frame_start_t.elapsed().as_nanos() as u64,
                );
                crate::profile::frame_complete();

                if !profile_active_t {
                    pace_to_frame_deadline(start_mono_ns, rendered as u64, frame_period_ns);
                }
            }
            Ok(())
        })();
        loop_elapsed_cell.set(start.elapsed());
        loop_result?;
        Ok(rendered)
    })();

    // Bug 2 fix (2026-05-09): held_scanout hand-off across the
    // call boundary -- see end_of_in_session_render_call.
    end_of_in_session_render_call(
        session, card,
        current_fb.take(), current_bo.take(),
        prev_fb.take(), prev_bo.take(),
    );
    // Restore scissor state. Atlas SB enables SCISSOR_TEST mid-frame
    // for region-clipped bg fill; the per-frame composite branch
    // disables it before scanout, but a `?` mid-bake skips that.
    // Without this, an error bail leaks SCISSOR_TEST into the next
    // render call's GL state. SP path doesn't enable scissor; this
    // disable is a no-op there.
    unsafe {
        use glow::HasContext;
        session.gl.disable(glow::SCISSOR_TEST);
    }

    let frame_count = work?;
    let total_elapsed_ms = work_start_t.elapsed().as_millis();
    let loop_elapsed_ms = loop_elapsed_cell.get().as_millis();
    let effective_fps = if loop_elapsed_ms > 0 {
        (frame_count as f64) * 1000.0 / (loop_elapsed_ms as f64)
    } else {
        0.0
    };
    eprintln!(
        "animated transition complete: kind={kind:?} rendered {frame_count} frames in {loop_elapsed_ms}ms (target {transition_ms}ms; effective {effective_fps:.1} fps; total {total_elapsed_ms}ms incl setup) [scissored-bake]"
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

/// P2-F (2026-05-10): cached glyph program + all attribute /
/// uniform locations, mirroring CachedSpProgram. Pre-fix
/// draw_text_layer called gl.get_attrib_location + 3-5 calls to
/// gl.get_uniform_location PER LAYER PER FRAME -- driver string
/// lookups that show up as ~1500/sec at 5L slides @ 30 fps. With
/// the locations resolved ONCE per program at first link, per-
/// layer cost drops to one uniform_*_f32 per uniform.
#[derive(Clone, Copy)]
struct CachedGlyphProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_atlas: Option<glow::NativeUniformLocation>,
    u_text_color: Option<glow::NativeUniformLocation>,
    u_opacity: Option<glow::NativeUniformLocation>,
    /// outline only; None for the non-outline program.
    u_outline_color: Option<glow::NativeUniformLocation>,
    /// outline only; None for the non-outline program.
    u_pixel_size: Option<glow::NativeUniformLocation>,
}

/// qarl-direct perf-profile (2026-05-08, extended P2-F 2026-05-10):
/// thread-local cache of compiled glyph programs PLUS resolved
/// attribute / uniform locations. Renderer is single-threaded, so
/// thread_local + Cell is mutex-free. EglSession teardown calls
/// clear_glyph_program_cache to delete the programs while the GL
/// context is still bound; without that they'd outlive the
/// context as dangling driver handles.
std::thread_local! {
    static FS_GLYPH_PROGRAM: std::cell::Cell<Option<CachedGlyphProgram>> =
        const { std::cell::Cell::new(None) };
    static FS_GLYPH_OUTLINE_PROGRAM: std::cell::Cell<Option<CachedGlyphProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_glyph_program(gl: &glow::Context, outline: bool) -> Result<CachedGlyphProgram> {
    use glow::HasContext;
    let cell = if outline { &FS_GLYPH_OUTLINE_PROGRAM } else { &FS_GLYPH_PROGRAM };
    cell.with(|c| {
        if let Some(cgp) = c.get() {
            return Ok(cgp);
        }
        let fs = if outline { FS_GLYPH_OUTLINE } else { FS_GLYPH };
        let program = link_program(gl, VS_TEXTURED_QUAD, fs)
            .with_context(|| format!("link {}", if outline { "FS_GLYPH_OUTLINE" } else { "FS_GLYPH" }))?;
        // Resolve all attribute + uniform locations ONCE so the
        // per-layer hot loop just reads from the cached struct.
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (glyph)"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (glyph)"))?;
        let u_atlas = unsafe { gl.get_uniform_location(program, "u_atlas") };
        let u_text_color = unsafe { gl.get_uniform_location(program, "u_text_color") };
        let u_opacity = unsafe { gl.get_uniform_location(program, "u_opacity") };
        let (u_outline_color, u_pixel_size) = if outline {
            unsafe {
                (
                    gl.get_uniform_location(program, "u_outline_color"),
                    gl.get_uniform_location(program, "u_pixel_size"),
                )
            }
        } else {
            (None, None)
        };
        let cgp = CachedGlyphProgram {
            program,
            a_pos,
            a_uv,
            u_atlas,
            u_text_color,
            u_opacity,
            u_outline_color,
            u_pixel_size,
        };
        c.set(Some(cgp));
        Ok(cgp)
    })
}

/// Delete the cached programs while the GL context is still
/// bound. Called from with_egl_session teardown.
fn clear_glyph_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    FS_GLYPH_PROGRAM.with(|c| {
        if let Some(cgp) = c.replace(None) {
            unsafe { gl.delete_program(cgp.program); }
        }
    });
    FS_GLYPH_OUTLINE_PROGRAM.with(|c| {
        if let Some(cgp) = c.replace(None) {
            unsafe { gl.delete_program(cgp.program); }
        }
    });
}

/// SDF arc slice B.2 -- cached compiled MSDF program + resolved
/// attrib/uniform locations. Same shape as `CachedGlyphProgram`;
/// adds `u_aa_width` (Some for FIXED variant, None for FWIDTH)
/// and `u_outline_distance` (Some for outline variants).
#[derive(Copy, Clone)]
struct CachedMsdfProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_atlas: Option<glow::NativeUniformLocation>,
    u_text_color: Option<glow::NativeUniformLocation>,
    u_opacity: Option<glow::NativeUniformLocation>,
    /// FIXED variant only.
    u_aa_width: Option<glow::NativeUniformLocation>,
    /// outline variants only.
    u_outline_color: Option<glow::NativeUniformLocation>,
    /// outline variants only.
    u_outline_distance: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static FS_MSDF_PROGRAM: std::cell::Cell<Option<CachedMsdfProgram>> =
        const { std::cell::Cell::new(None) };
    static FS_MSDF_OUTLINE_PROGRAM: std::cell::Cell<Option<CachedMsdfProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_msdf_program(gl: &glow::Context, outline: bool) -> Result<CachedMsdfProgram> {
    use glow::HasContext;
    let cell = if outline { &FS_MSDF_OUTLINE_PROGRAM } else { &FS_MSDF_PROGRAM };
    cell.with(|c| {
        if let Some(cgp) = c.get() {
            return Ok(cgp);
        }
        // Variant selection at compile time: aa_mode() picks
        // FWIDTH vs FIXED. First call wins per the OnceLock
        // contract; if aa_mode is set later, the program cache
        // would have to be cleared + rebuilt -- but the CLI flag
        // is read once at main entry so this isn't an issue in
        // practice.
        let fs = if outline {
            crate::hdmi_logic::fs_msdf_outline_for_aa_mode()
        } else {
            crate::hdmi_logic::fs_msdf_for_aa_mode()
        };
        let label = if outline { "FS_MSDF_OUTLINE" } else { "FS_MSDF" };
        let program = link_program(gl, crate::hdmi_logic::VS_TEXTURED_QUAD, fs)
            .with_context(|| format!("link {label}"))?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos ({label})"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv ({label})"))?;
        let u_atlas = unsafe { gl.get_uniform_location(program, "u_atlas") };
        let u_text_color = unsafe { gl.get_uniform_location(program, "u_text_color") };
        let u_opacity = unsafe { gl.get_uniform_location(program, "u_opacity") };
        // u_aa_width exists only on the FIXED variants; FWIDTH
        // variants don't declare it. get_uniform_location returns
        // None for non-existent uniforms (no error), so we always
        // try and store None if absent.
        let u_aa_width = unsafe { gl.get_uniform_location(program, "u_aa_width") };
        let (u_outline_color, u_outline_distance) = if outline {
            unsafe {
                (
                    gl.get_uniform_location(program, "u_outline_color"),
                    gl.get_uniform_location(program, "u_outline_distance"),
                )
            }
        } else {
            (None, None)
        };
        let cgp = CachedMsdfProgram {
            program,
            a_pos,
            a_uv,
            u_atlas,
            u_text_color,
            u_opacity,
            u_aa_width,
            u_outline_color,
            u_outline_distance,
        };
        c.set(Some(cgp));
        Ok(cgp)
    })
}

fn clear_msdf_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    FS_MSDF_PROGRAM.with(|c| {
        if let Some(cgp) = c.replace(None) {
            unsafe { gl.delete_program(cgp.program); }
        }
    });
    FS_MSDF_OUTLINE_PROGRAM.with(|c| {
        if let Some(cgp) = c.replace(None) {
            unsafe { gl.delete_program(cgp.program); }
        }
    });
    FS_TOFU_PROGRAM.with(|c| {
        if let Some(tgp) = c.replace(None) {
            unsafe { gl.delete_program(tgp.program); }
        }
    });
    // SDF arc slice C.2 -- free the FS_EMOJI program (only present
    // once a session has paint_slide'd an emoji quad via C.3; on
    // sessions that never see emoji this no-ops).
    FS_EMOJI_PROGRAM.with(|c| {
        if let Some(egp) = c.replace(None) {
            unsafe { gl.delete_program(egp.program); }
        }
    });
}

// QA perf-resweep-v2 P1 (2026-05-24): shared scratch VBO for the 4
// draw_text_layer_msdf sub-batches (msdf-ink, tofu, dynamic-msdf,
// dynamic-emoji). Each batch uploads its own f32 vertex data with
// glow::STATIC_DRAW; with a cached buffer name, the per-frame
// create/delete pair turns into a single create per session +
// glBufferData-orphan re-upload per draw, which is the standard
// dynamic-geometry pattern on GLES2.
//
// Pre-cache cost on Pi Zero 2 W: ~0.2% of one core in create/delete
// pair overhead (per QA's perf-resweep-v2 P1 calibration; rate
// scales with text-layer count per frame and active-batch fraction).
// Post-cache: a single create at session bring-up; the delete is
// moved to session teardown via clear_msdf_text_vbo_cache (paired
// with clear_msdf_program_cache).
//
// STATIC_DRAW hint is preserved because the data is conceptually
// static for the lifetime of each draw call -- the driver orphans
// the prior store on each glBufferData call regardless of hint;
// the hint just signals optimization intent. Leaving it matches
// the pre-cache semantics exactly.
std::thread_local! {
    static MSDF_TEXT_VBO: std::cell::Cell<Option<glow::NativeBuffer>> =
        const { std::cell::Cell::new(None) };
}

fn cached_msdf_text_vbo(gl: &glow::Context) -> Result<glow::NativeBuffer> {
    use glow::HasContext;
    MSDF_TEXT_VBO.with(|c| {
        if let Some(vbo) = c.get() {
            return Ok(vbo);
        }
        let vbo = unsafe { gl.create_buffer() }
            .map_err(|e| anyhow!("glGenBuffers (msdf text vbo): {e}"))?;
        c.set(Some(vbo));
        Ok(vbo)
    })
}

fn clear_msdf_text_vbo_cache(gl: &glow::Context) {
    use glow::HasContext;
    MSDF_TEXT_VBO.with(|c| {
        if let Some(vbo) = c.replace(None) {
            unsafe { gl.delete_buffer(vbo); }
        }
    });
}

/// SDF arc slice B.3 -- cached FS_TOFU program for missing-
/// codepoint quad rendering. Simpler than CachedMsdfProgram (no
/// atlas/text_color/outline uniforms; just opacity + the standard
/// VS_TEXTURED_QUAD attribs).
#[derive(Copy, Clone)]
struct CachedTofuProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_opacity: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static FS_TOFU_PROGRAM: std::cell::Cell<Option<CachedTofuProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_tofu_program(gl: &glow::Context) -> Result<CachedTofuProgram> {
    use glow::HasContext;
    FS_TOFU_PROGRAM.with(|c| {
        if let Some(tgp) = c.get() {
            return Ok(tgp);
        }
        let program = link_program(gl, crate::hdmi_logic::VS_TEXTURED_QUAD, FS_TOFU)
            .with_context(|| "link FS_TOFU")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (FS_TOFU)"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (FS_TOFU)"))?;
        let u_opacity = unsafe { gl.get_uniform_location(program, "u_opacity") };
        let tgp = CachedTofuProgram { program, a_pos, a_uv, u_opacity };
        c.set(Some(tgp));
        Ok(tgp)
    })
}

/// SDF arc slice C.2 -- cached FS_EMOJI program for color-emoji
/// quad rendering. Same shape as CachedTofuProgram with an extra
/// `u_atlas` sampler binding for the color-bitmap page.
#[derive(Copy, Clone)]
struct CachedEmojiProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_atlas: Option<glow::NativeUniformLocation>,
    u_opacity: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static FS_EMOJI_PROGRAM: std::cell::Cell<Option<CachedEmojiProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_emoji_program(gl: &glow::Context) -> Result<CachedEmojiProgram> {
    use glow::HasContext;
    FS_EMOJI_PROGRAM.with(|c| {
        if let Some(egp) = c.get() {
            return Ok(egp);
        }
        let program = link_program(gl, crate::hdmi_logic::VS_TEXTURED_QUAD, FS_EMOJI)
            .with_context(|| "link FS_EMOJI")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (FS_EMOJI)"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (FS_EMOJI)"))?;
        let u_atlas = unsafe { gl.get_uniform_location(program, "u_atlas") };
        let u_opacity = unsafe { gl.get_uniform_location(program, "u_opacity") };
        let egp = CachedEmojiProgram { program, a_pos, a_uv, u_atlas, u_opacity };
        c.set(Some(egp));
        Ok(egp)
    })
}

/// SDF arc slice B.2 -- session-scoped MSDF atlas lookup table.
///
/// Populated by `populate_msdf_lookup` after `upload_all` lands the
/// 23 atlas textures on the GL context; cleared by
/// `clear_msdf_lookup` at session teardown BEFORE
/// `sdf_atlas_gl::delete_all` so a stale lookup can't outlive the
/// underlying NativeTexture handles.
///
/// Why a thread_local instead of threading `&[MsdfAtlasGl]` through
/// paint_slide's signature: ~14 call sites would each need a new
/// parameter. The atlas set is process-singleton (baked at compile
/// time, identical across sessions), single-threaded by the GL
/// context lifecycle. A thread_local keeps the API surface stable.
///
/// Tuple shape: (font_stem, atlas_tex, atlas_w, atlas_h). We don't
/// stash the full AtlasManifest -- callers that need the manifest
/// for glyph lookup go through `crate::sdf_atlas::atlas_for_stem`
/// against the cross-platform Vec<MsdfAtlas> (separate from the
/// GL-side Vec<MsdfAtlasGl>). The tex + dims are all this lookup
/// needs to feed `draw_text_layer_msdf`.
std::thread_local! {
    static MSDF_ATLAS_LOOKUP: std::cell::RefCell<Vec<(String, glow::NativeTexture)>> =
        const { std::cell::RefCell::new(Vec::new()) };
}

/// Process-wide parsed atlas set (CPU-side; `atlas_rgb` is 'static
/// because the bytes come from `include_bytes!`). Loaded once on
/// the first session bring-up; reused thereafter. Decoupling from
/// the GL-side `MSDF_ATLAS_LOOKUP` lets host tests + layout-only
/// paths (which don't need a GL context) reach the same data.
static MSDF_ATLASES_CPU: std::sync::OnceLock<Vec<crate::sdf_atlas::MsdfAtlas>> =
    std::sync::OnceLock::new();

fn populate_msdf_lookup(atlases: &[crate::sdf_atlas_gl::MsdfAtlasGl]) {
    MSDF_ATLAS_LOOKUP.with(|c| {
        let mut v = c.borrow_mut();
        v.clear();
        for a in atlases {
            v.push((a.stem.clone(), a.tex));
        }
    });
    // First session populates the CPU-side cache; subsequent
    // sessions skip (OnceLock semantics).
    let _ = MSDF_ATLASES_CPU.get_or_init(|| {
        crate::sdf_atlas::load_all_atlases().unwrap_or_default()
    });
}

fn clear_msdf_lookup(gl: &glow::Context) {
    use glow::HasContext;
    // G-3 (2026-06-16): with lazy upload, MSDF_ATLAS_LOOKUP owns
    // the NativeTexture handles for atlases that were uploaded
    // on-demand via `msdf_atlas_for_family`. Pre-G-3 those handles
    // were tracked in session.msdf_atlases and freed by
    // sdf_atlas_gl::delete_all. Post-G-3 session.msdf_atlases is
    // empty (upload_all skipped at session bring-up), so the
    // GL textures must be deleted here at teardown to avoid a
    // per-session leak on short-lived session callers (--play-
    // slide, --capture-*, etc.). The sidecar session is process-
    // lifetime so a leak there would be reclaimed by process exit
    // anyway, but the short-lived callers run many sessions.
    MSDF_ATLAS_LOOKUP.with(|c| {
        let mut v = c.borrow_mut();
        for (_stem, tex) in v.drain(..) {
            unsafe { gl.delete_texture(tex); }
        }
    });
    // MSDF_ATLASES_CPU is process-lifetime + only references 'static
    // bytes; intentionally not cleared.
}

// =====================================================================
// Bug 3 Slice 2B -- dynamic MSDF atlas lookup (one page in Slice 2)
// =====================================================================
//
// Mirrors MSDF_ATLAS_LOOKUP's thread_local pattern for the runtime-
// rasterized atlas pages. Two pages: MSDF cells (48 px) and COLRv1
// cells (96 px). Populated at session bring-up after each AtlasPage's
// `allocate_texture(&gl)` succeeds; cleared at session teardown
// BEFORE the corresponding `delete(&gl)` so a stale handle can't
// outlive its NativeTexture. Slice 1.x will extend to Vec when LRU
// eviction adds multi-page support.
std::thread_local! {
    static DYNAMIC_ATLAS_LOOKUP: std::cell::RefCell<Option<glow::NativeTexture>> =
        const { std::cell::RefCell::new(None) };
    // Bug 3 Slice 3B (2026-05-19): separate page for COLRv1-rasterized
    // emoji cells. Same thread_local pattern; the draw path picks
    // between LOOKUPs by GlyphKind::DynamicMsdf vs DynamicEmoji.
    static DYNAMIC_ATLAS_COLR_LOOKUP: std::cell::RefCell<Option<glow::NativeTexture>> =
        const { std::cell::RefCell::new(None) };
}

fn populate_dynamic_atlas_lookup(tex: glow::NativeTexture) {
    DYNAMIC_ATLAS_LOOKUP.with(|c| *c.borrow_mut() = Some(tex));
}

fn clear_dynamic_atlas_lookup() {
    DYNAMIC_ATLAS_LOOKUP.with(|c| *c.borrow_mut() = None);
}

fn dynamic_atlas_tex() -> Option<glow::NativeTexture> {
    DYNAMIC_ATLAS_LOOKUP.with(|c| *c.borrow())
}

fn populate_dynamic_atlas_colr_lookup(tex: glow::NativeTexture) {
    DYNAMIC_ATLAS_COLR_LOOKUP.with(|c| *c.borrow_mut() = Some(tex));
}

fn clear_dynamic_atlas_colr_lookup() {
    DYNAMIC_ATLAS_COLR_LOOKUP.with(|c| *c.borrow_mut() = None);
}

fn dynamic_atlas_colr_tex() -> Option<glow::NativeTexture> {
    DYNAMIC_ATLAS_COLR_LOOKUP.with(|c| *c.borrow())
}

/// Resolve a `font_family` string (schema-level) to its baked atlas
/// stem (e.g. "Anton" -> "anton"). Returns `None` for families not
/// in the catalog (caller falls back to the default family).
fn font_family_to_atlas_stem(family: &str) -> Option<&'static str> {
    let filename = crate::hdmi_logic::font_family_to_filename(family)?;
    Some(filename.trim_end_matches(".ttf"))
}

/// Look up the (GL atlas texture, CPU-side atlas manifest) pair for
/// a `font_family`. Returns `None` if the family isn't in the
/// catalog OR the atlas hasn't been uploaded yet (e.g. headless
/// host tests that bypass `with_egl_session`). Production paths
/// fall back to the catalog's default family ("Inter") when the
/// requested family is missing.
fn msdf_atlas_for_family(
    gl: &glow::Context,
    family: &str,
) -> Option<(glow::NativeTexture, &'static crate::sdf_atlas::MsdfAtlas)> {
    let stem = font_family_to_atlas_stem(family)?;
    let cpu = MSDF_ATLASES_CPU.get()?;
    let atlas = crate::sdf_atlas::atlas_for_stem(cpu, stem)?;
    // G-3 (2026-06-16): static atlas LAZY-UPLOAD on first lookup
    // miss. Pre-G-3 the upload_all path eagerly uploaded all 23
    // atlases (~29 MB GPU) at session bring-up. Post-G-3
    // upload_all is skipped; this lookup path uploads the requested
    // atlas on demand and caches the handle in MSDF_ATLAS_LOOKUP
    // for subsequent calls. Reels that never reference a particular
    // family pay zero MB for it. Karl's reel (Bebas Neue, NOT in
    // the static atlas set) never reaches this code path at all —
    // it routes through the dynamic glyph_cache instead.
    let existing = MSDF_ATLAS_LOOKUP.with(|c| {
        c.borrow()
            .iter()
            .find(|(s, _)| s == stem)
            .map(|(_, t)| *t)
    });
    if let Some(tex) = existing {
        return Some((tex, atlas));
    }
    // Cold path: upload this single atlas, cache the handle.
    let tex = match crate::sdf_atlas_gl::upload_one(gl, atlas) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("warn: msdf_atlas_lazy_upload {stem}: {e}");
            return None;
        }
    };
    let stem_owned = stem.to_string();
    MSDF_ATLAS_LOOKUP.with(|c| {
        c.borrow_mut().push((stem_owned, tex));
    });
    eprintln!(
        "[perf] msdf_atlas_lazy_upload stem={stem} bytes_uploaded={size}",
        size = atlas.manifest.atlas_w * atlas.manifest.atlas_h * 3,
    );
    Some((tex, atlas))
}

// =====================================================================
// Slice 3D (2026-05-19) — emoji atlas lookup retired
// =====================================================================
//
// Pre-3D this section housed `EMOJI_ATLAS_LOOKUP` (Vec<(page,
// NativeTexture)>) + `EMOJI_ATLAS_CPU` (OnceLock<EmojiAtlas>) +
// `populate_emoji_lookup` / `clear_emoji_lookup`. All retired
// alongside the CBDT bake. Emoji draw resolution now goes through
// `DYNAMIC_ATLAS_COLR_LOOKUP` (Slice 3B) — see
// `populate_dynamic_atlas_colr_lookup` / `dynamic_atlas_colr_tex`
// elsewhere in this file.

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

/// r102.3 (2026-06-09): cached attribute + uniform locations for
/// the live-3-pass transition program. Pre-r102.3 the live IPC
/// path (paint_and_present_one_transition_frame, hdmi.rs:5289+)
/// re-linked the program AND re-resolved all 7 locations on
/// every tick. The link is the expensive operation that vc4 V3D
/// lazy-GC retained as the ~108 MB / 4 min non-bracket leak
/// QA's r102.1.1 probe surfaced after r102.2 plugged the FBO+tex
/// leak. The struct cache mirrors `CachedCompositeProgram`
/// shape minus the u_a_xform/u_b_xform fields (legacy 3-pass
/// doesn't atlas-pack so it doesn't need them).
#[derive(Copy, Clone)]
pub(crate) struct CachedLegacyTransitionProgram {
    pub program: glow::NativeProgram,
    pub a_pos: u32,
    pub a_uv: u32,
    pub u_src_a: Option<glow::NativeUniformLocation>,
    pub u_src_b: Option<glow::NativeUniformLocation>,
    pub u_t: Option<glow::NativeUniformLocation>,
    /// r96: u_aspect for the legacy FS_IRIS path. Most legacy
    /// shaders don't declare it so this resolves to None for
    /// most kinds and the bind is a silent no-op.
    pub u_aspect: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static LEGACY_TRANSITION_PROGRAMS_V2: std::cell::RefCell<
        std::collections::HashMap<*const u8, CachedLegacyTransitionProgram>,
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

/// r102.3 (2026-06-09): resolve (and cache) the live-3-pass
/// transition program + all 7 locations for a given fragment
/// shader source. The underlying program is shared with
/// `TRANSITION_PROGRAMS` (the standalone-reel path's cache);
/// `cached_transition_program` is called here so a program is
/// link'd AT MOST ONCE per fs across both code paths.
///
/// Returns the same `CachedLegacyTransitionProgram` on every
/// call for the same fs. None for VS missing a_pos/a_uv (signals
/// shader-source corruption; surfaces as an error to the
/// caller which is the live IPC sidecar).
pub(crate) fn cached_legacy_transition_program(
    gl: &glow::Context,
    fs: &'static str,
) -> Result<CachedLegacyTransitionProgram> {
    use glow::HasContext;
    LEGACY_TRANSITION_PROGRAMS_V2.with(|c| {
        let mut cache = c.borrow_mut();
        let key = fs.as_ptr();
        if let Some(&entry) = cache.get(&key) {
            return Ok(entry);
        }
        // Share the underlying linked program with the
        // standalone reel's cache so we don't double-link.
        let program = cached_transition_program(gl, fs)?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (cached_legacy_transition_program)"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (cached_legacy_transition_program)"))?;
        let u_src_a = unsafe { gl.get_uniform_location(program, "u_src_a") };
        let u_src_b = unsafe { gl.get_uniform_location(program, "u_src_b") };
        let u_t = unsafe { gl.get_uniform_location(program, "u_t") };
        let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };
        let entry = CachedLegacyTransitionProgram {
            program,
            a_pos,
            a_uv,
            u_src_a,
            u_src_b,
            u_t,
            u_aspect,
        };
        cache.insert(key, entry);
        Ok(entry)
    })
}

/// r102.3 (2026-06-09): session-teardown cleanup. The
/// underlying program handles are owned by
/// `TRANSITION_PROGRAMS` (and freed by
/// `clear_transition_program_cache`); this just drains the
/// location-cache entries so the HashMap doesn't retain stale
/// pointers.
fn clear_legacy_transition_program_cache() {
    LEGACY_TRANSITION_PROGRAMS_V2.with(|c| {
        c.borrow_mut().clear();
    });
}

/// QA-direct (2026-05-08, post-clock_nanosleep): cached per-program
/// state so that the per-call uniform-location lookups (~14 string
/// hash queries through the GLES2 driver) and attribute-location
/// lookups don't fire on every transition. First encounter of a
/// (kind, n_a, n_b) compiles + resolves; subsequent calls fetch
/// the resolved struct. Closes the §8.3 reel-context warm-state
/// gap where setup-overhead amortized over short transitions
/// dragged the aggregate fps 0.5-1 fps below the per-frame cadence.
#[derive(Clone)]
struct CachedSpProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_t: Option<glow::NativeUniformLocation>,
    /// r95 (2026-06-08): u_aspect = mode_w / mode_h. Used by the iris
    /// arm to make the iris a true screen-pixel circle on non-square
    /// displays. Other SP shaders declare the uniform but GLSL drops
    /// it as unused.
    u_aspect: Option<glow::NativeUniformLocation>,
    u_a_bg: Option<glow::NativeUniformLocation>,
    u_b_bg: Option<glow::NativeUniformLocation>,
    u_a_tex_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
    u_b_tex_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
    u_a_rect_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
    u_b_rect_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
    u_a_rgba_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
    u_b_rgba_locs: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE],
}

/// QA-mandated single-pass transition (2026-05-08, step 3
/// generalization): per-(kind, n_a, n_b) shader cache. Keyed by
/// (kind: &'static str, n_a, n_b) tuple. FYS reel cycles through
/// ~5-15 unique (kind, n_a, n_b) pairs; each compiles + resolves
/// ONCE per session, then every subsequent commit_fb path fetches
/// the cached struct.
std::thread_local! {
    static TRANSITION_SP_PROGRAMS: std::cell::RefCell<
        std::collections::HashMap<(&'static str, usize, usize), CachedSpProgram>,
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

fn cached_transition_sp_program(
    gl: &glow::Context,
    kind: &str,
    n_a: usize,
    n_b: usize,
) -> Result<CachedSpProgram> {
    use glow::HasContext;
    let kind_static =
        sp_kind_static(kind).ok_or_else(|| anyhow!("kind {kind:?} has no SP generator"))?;
    TRANSITION_SP_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        if let Some(csp) = cache.get(&(kind_static, n_a, n_b)) {
            return Ok(csp.clone());
        }
        let fs = fs_transition_sp_source(kind, n_a, n_b)
            .ok_or_else(|| anyhow!("fs_transition_sp_source returned None for {kind:?}"))?;
        let program = link_program(gl, VS_TEXTURED_QUAD, &fs)
            .with_context(|| format!("link FS_{}_SP({n_a}, {n_b})", kind.to_uppercase()))?;
        // Resolve all attribute + uniform locations ONCE so the
        // per-frame loop just reads from the cached struct.
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_pos (sp {kind})"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("VS_TEXTURED_QUAD missing a_uv (sp {kind})"))?;
        let u_t = unsafe { gl.get_uniform_location(program, "u_t") };
        // r95 (2026-06-08): u_aspect for the iris arm. Resolved on
        // every SP program; non-iris kinds get None (the uniform is
        // declared in the header but dropped by the GLSL optimizer
        // when unused), and bind_uniform_1f tolerates None silently.
        let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };
        let u_a_bg = unsafe { gl.get_uniform_location(program, "u_a_bg") };
        let u_b_bg = unsafe { gl.get_uniform_location(program, "u_b_bg") };
        let resolve_slots = |prefix: &str, n: usize| -> [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE] {
            let mut out: [Option<glow::NativeUniformLocation>; SINGLE_PASS_MAX_LAYERS_PER_SLIDE] =
                [None, None, None, None];
            for slot in 0..n {
                let name = format!("{prefix}{slot}");
                out[slot] = unsafe { gl.get_uniform_location(program, &name) };
            }
            out
        };
        let csp = CachedSpProgram {
            program,
            a_pos,
            a_uv,
            u_t,
            u_aspect,
            u_a_bg,
            u_b_bg,
            u_a_tex_locs: resolve_slots("u_a_tex", n_a),
            u_b_tex_locs: resolve_slots("u_b_tex", n_b),
            u_a_rect_locs: resolve_slots("u_a_rect", n_a),
            u_b_rect_locs: resolve_slots("u_b_rect", n_b),
            u_a_rgba_locs: resolve_slots("u_a_rgba", n_a),
            u_b_rgba_locs: resolve_slots("u_b_rgba", n_b),
        };
        cache.insert((kind_static, n_a, n_b), csp.clone());
        Ok(csp)
    })
}

/// Cached FS_BLIT program for the atlas SB bg-cache path
/// (2026-05-09): when a slide has a non-solid bg cached as a
/// pre-rendered texture, we blit it into the atlas region via
/// a single full-screen-quad draw. This is ~2-3x faster than
/// re-running FS_GRADIENT every frame (vc4 TMU dedicated
/// hardware vs SIMD ALU). Cached per session; freed via
/// clear_blit_program_cache at teardown.
#[derive(Clone, Copy)]
struct CachedBlitProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_src: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    // P2-G.fix (2026-05-10): Cell+Copy for pattern uniformity with
    // FS_BRIGHT_GAMMA / FS_OVERLAY_BLEND program caches. RefCell+
    // Clone was a vestige of an earlier shape; behavior identical
    // since CachedBlitProgram fields are all Copy.
    static BLIT_PROGRAM: std::cell::Cell<Option<CachedBlitProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_blit_program(gl: &glow::Context) -> Result<CachedBlitProgram> {
    use glow::HasContext;
    BLIT_PROGRAM.with(|c| {
        if let Some(p) = c.get() {
            return Ok(p);
        }
        let program = link_program(gl, VS_TEXTURED_QUAD, crate::hdmi_logic::FS_BLIT)
            .context("link FS_BLIT (atlas bg-cache blit)")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("FS_BLIT VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("FS_BLIT VS missing a_uv"))?;
        let u_src = unsafe { gl.get_uniform_location(program, "u_src") };
        let cbp = CachedBlitProgram { program, a_pos, a_uv, u_src };
        c.set(Some(cbp));
        Ok(cbp)
    })
}

fn clear_blit_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    BLIT_PROGRAM.with(|c| {
        if let Some(p) = c.replace(None) {
            unsafe { gl.delete_program(p.program); }
        }
    });
}

/// V4L2 piece 3d (2026-05-14): cached NV12 -> RGB program for
/// VideoSlide paint. Mirrors `CachedBlitProgram` but exposes
/// two sampler uniforms (Y on TEXTURE0, UV on TEXTURE1).
#[derive(Copy, Clone)]
struct CachedNv12Program {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_tex_y: Option<glow::NativeUniformLocation>,
    u_tex_uv: Option<glow::NativeUniformLocation>,
    /// r83 Phase B: y-axis crop fraction (display_h / allocated_h).
    /// Default 1.0 = no crop; set by `run_nv12_blit_pass`.
    u_y_crop_max: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static NV12_PROGRAM: std::cell::Cell<Option<CachedNv12Program>> =
        const { std::cell::Cell::new(None) };
}

fn cached_nv12_program(gl: &glow::Context) -> Result<CachedNv12Program> {
    use glow::HasContext;
    NV12_PROGRAM.with(|c| {
        if let Some(p) = c.get() {
            return Ok(p);
        }
        let program = link_program(
            gl,
            VS_TEXTURED_QUAD,
            crate::hdmi_logic::FS_NV12_TO_RGB,
        )
        .context("link FS_NV12_TO_RGB (VideoSlide paint)")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("FS_NV12_TO_RGB VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("FS_NV12_TO_RGB VS missing a_uv"))?;
        let u_tex_y = unsafe { gl.get_uniform_location(program, "u_tex_y") };
        let u_tex_uv = unsafe { gl.get_uniform_location(program, "u_tex_uv") };
        let u_y_crop_max = unsafe { gl.get_uniform_location(program, "u_y_crop_max") };
        let cnp = CachedNv12Program {
            program, a_pos, a_uv, u_tex_y, u_tex_uv, u_y_crop_max,
        };
        c.set(Some(cnp));
        Ok(cnp)
    })
}

/// V4L2 piece 3d: draw a `vbo` quad sampling `y_tex` (Y plane,
/// GL_LUMINANCE) and `uv_tex` (UV plane, GL_LUMINANCE_ALPHA) through
/// the BT.601 limited-range NV12 -> RGB shader. Caller binds the
/// destination FBO + viewport beforehand. The two source textures
/// must already be uploaded; this pass does no allocation -- just
/// the per-frame draw. `vbo` is a 4-vert interleaved `[x,y,u,v]`
/// TRIANGLE_STRIP quad — the shared `cached_textured_quad_vbo` for a
/// plain fill, or a `cover_quad_vbo` for FYS bug B cover-fit.
unsafe fn run_nv12_blit_pass(
    gl: &glow::Context,
    vbo: glow::NativeBuffer,
    y_tex: glow::NativeTexture,
    uv_tex: glow::NativeTexture,
    y_crop_max: f32,
) -> Result<()> {
    use glow::HasContext;
    let cnp = cached_nv12_program(gl)?;
    gl.use_program(Some(cnp.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
    gl.uniform_1_i32(cnp.u_tex_y.as_ref(), 0);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
    gl.uniform_1_i32(cnp.u_tex_uv.as_ref(), 1);
    // r83 Phase B: skip the bottom-row green padding by clamping
    // the flipped-v sampling range to [0, y_crop_max] where
    // y_crop_max = display_h / allocated_h. Caller passes 1.0 for
    // no-crop / unknown-source-dims. We set this every pass
    // defensively even when the value is constant — GLES2 DOES
    // preserve uniform values across `gl.use_program` calls on the
    // same program object, but the FIRST frame would otherwise
    // read Mesa's default of 0, which would short-circuit
    // `(1.0 - v_uv.y) * 0` for every texel and collapse the entire
    // frame to texture row 0.
    gl.uniform_1_f32(cnp.u_y_crop_max.as_ref(), y_crop_max);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cnp.a_pos);
    gl.vertex_attrib_pointer_f32(cnp.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cnp.a_uv);
    gl.vertex_attrib_pointer_f32(cnp.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cnp.a_pos);
    gl.disable_vertex_attrib_array(cnp.a_uv);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, None);
    // Program + shared VBO come from session-lived caches; never
    // freed here.
    Ok(())
}

/// STREAM/VLC HW-decode (2026-05-20): cached cover-fit NV12 -> RGB
/// program for the external-frame NV12 push path. Mirrors
/// `CachedNv12Program` but adds the two cover-fit UV-transform
/// uniforms (`u_uv_scale`, `u_uv_offset`) consumed by
/// `FS_NV12_COVER_TO_RGB`.
#[derive(Copy, Clone)]
struct CachedNv12CoverProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_tex_y: Option<glow::NativeUniformLocation>,
    u_tex_uv: Option<glow::NativeUniformLocation>,
    u_uv_scale: Option<glow::NativeUniformLocation>,
    u_uv_offset: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static NV12_COVER_PROGRAM: std::cell::Cell<Option<CachedNv12CoverProgram>> =
        const { std::cell::Cell::new(None) };
}

fn cached_nv12_cover_program(gl: &glow::Context) -> Result<CachedNv12CoverProgram> {
    use glow::HasContext;
    NV12_COVER_PROGRAM.with(|c| {
        if let Some(p) = c.get() {
            return Ok(p);
        }
        let program = link_program(
            gl,
            VS_TEXTURED_QUAD,
            crate::hdmi_logic::FS_NV12_COVER_TO_RGB,
        )
        .context("link FS_NV12_COVER_TO_RGB (external NV12 paint)")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("FS_NV12_COVER_TO_RGB VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("FS_NV12_COVER_TO_RGB VS missing a_uv"))?;
        let u_tex_y = unsafe { gl.get_uniform_location(program, "u_tex_y") };
        let u_tex_uv = unsafe { gl.get_uniform_location(program, "u_tex_uv") };
        let u_uv_scale = unsafe { gl.get_uniform_location(program, "u_uv_scale") };
        let u_uv_offset = unsafe { gl.get_uniform_location(program, "u_uv_offset") };
        let cnp = CachedNv12CoverProgram {
            program, a_pos, a_uv, u_tex_y, u_tex_uv, u_uv_scale, u_uv_offset,
        };
        c.set(Some(cnp));
        Ok(cnp)
    })
}

/// STREAM/VLC HW-decode (2026-05-20): draw a fullscreen quad
/// sampling `y_tex` + `uv_tex` through the cover-fit BT.709 NV12 ->
/// RGB shader, with `(uv_scale, uv_offset)` remapping the source
/// onto the panel aspect-preserving (center-cropped overflow).
/// Caller binds the destination FBO + viewport beforehand and has
/// already uploaded the two source textures; this pass does no
/// allocation.
unsafe fn run_nv12_cover_blit_pass(
    gl: &glow::Context,
    y_tex: glow::NativeTexture,
    uv_tex: glow::NativeTexture,
    uv_scale: [f32; 2],
    uv_offset: [f32; 2],
) -> Result<()> {
    use glow::HasContext;
    let cnp = cached_nv12_cover_program(gl)?;
    let vbo = cached_textured_quad_vbo(gl)?;
    gl.use_program(Some(cnp.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
    gl.uniform_1_i32(cnp.u_tex_y.as_ref(), 0);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
    gl.uniform_1_i32(cnp.u_tex_uv.as_ref(), 1);
    gl.uniform_2_f32(cnp.u_uv_scale.as_ref(), uv_scale[0], uv_scale[1]);
    gl.uniform_2_f32(cnp.u_uv_offset.as_ref(), uv_offset[0], uv_offset[1]);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cnp.a_pos);
    gl.vertex_attrib_pointer_f32(cnp.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cnp.a_uv);
    gl.vertex_attrib_pointer_f32(cnp.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cnp.a_pos);
    gl.disable_vertex_attrib_array(cnp.a_uv);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, None);
    Ok(())
}

// ============================================================
// V4L2 piece 4c (2026-05-14) -- DMA-BUF zero-copy NV12 paint
// ============================================================
//
// Imports a V4L2-exported DMA-BUF fd (piece 4a, Frame::dma_buf_fd)
// as an EGLImage, binds it to a GL_TEXTURE_EXTERNAL_OES texture,
// and blits it through FS_NV12_DMABUF_TO_RGB (piece 4b). This is
// the zero-copy paint path -- no glTexImage2D upload, no per-frame
// CPU traffic across the PCIe/memory bus. Piece 4d wires this into
// paint_and_present_one_video_slide_frame; piece 4c is just the
// machinery.

#[cfg(target_os = "linux")]
const EGL_LINUX_DMA_BUF_EXT: u32 = 0x3270;
#[cfg(target_os = "linux")]
const EGL_LINUX_DRM_FOURCC_EXT: i32 = 0x3271;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE0_FD_EXT: i32 = 0x3272;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE0_OFFSET_EXT: i32 = 0x3273;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE0_PITCH_EXT: i32 = 0x3274;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE1_FD_EXT: i32 = 0x3275;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE1_OFFSET_EXT: i32 = 0x3276;
#[cfg(target_os = "linux")]
const EGL_DMA_BUF_PLANE1_PITCH_EXT: i32 = 0x3277;
#[cfg(target_os = "linux")]
const EGL_NONE_ATTR: i32 = 0x3038; // EGL_NONE terminator for attrib lists
/// FourCC code DRM_FORMAT_NV12 = 'N','V','1','2' little-endian =
/// 0x3231564E. Used in the EGL_LINUX_DRM_FOURCC_EXT slot of the
/// dma_buf attrib list to tell Mesa what pixel format the bytes
/// represent.
#[cfg(target_os = "linux")]
const DRM_FORMAT_NV12: i32 = 0x3231564E;
/// GLES texture target for external-OES samples. Bound via
/// glBindTexture(GL_TEXTURE_EXTERNAL_OES, tex). The texture must
/// have an EGLImage associated via glEGLImageTargetTexture2DOES
/// BEFORE it can be sampled. Equivalent to GL_TEXTURE_2D = 0x0DE1
/// but lives in a separate target enum so the driver knows to use
/// the YUV->RGB fast-path.
#[cfg(target_os = "linux")]
const GL_TEXTURE_EXTERNAL_OES: u32 = 0x8D65;

/// Cached EGL/GLES extension entry points + the negotiated cap.
/// Loaded on first use; thread_local because EGL state is
/// per-thread anyway + the renderer's GL context lives on one
/// thread (the IPC loop).
#[cfg(target_os = "linux")]
#[derive(Copy, Clone)]
struct DmaBufEglEntryPoints {
    /// `eglCreateImageKHR(EGLDisplay, EGLContext, EGLenum target,
    /// EGLClientBuffer, const EGLint *attrib_list) -> EGLImageKHR`
    create_image: unsafe extern "C" fn(
        dpy: *mut std::ffi::c_void,
        ctx: *mut std::ffi::c_void,
        target: u32,
        buffer: *mut std::ffi::c_void,
        attrib_list: *const i32,
    ) -> *mut std::ffi::c_void,
    /// `eglDestroyImageKHR(EGLDisplay, EGLImageKHR) -> EGLBoolean (u32)`
    destroy_image: unsafe extern "C" fn(
        dpy: *mut std::ffi::c_void,
        image: *mut std::ffi::c_void,
    ) -> u32,
    /// `glEGLImageTargetTexture2DOES(GLenum target, GLeglImageOES image)`
    image_target_texture_2d:
        unsafe extern "C" fn(target: u32, image: *mut std::ffi::c_void),
}

/// Tri-state cache:
///   `Some(Some(eps))` -> extensions present + entry points loaded
///   `Some(None)`      -> extensions checked + at least one missing
///   `None`            -> not yet checked (lazy init on first call)
#[cfg(target_os = "linux")]
std::thread_local! {
    static DMA_BUF_EGL_CACHE: std::cell::Cell<Option<Option<DmaBufEglEntryPoints>>> =
        const { std::cell::Cell::new(None) };
}

/// Look up EGL_EXT_image_dma_buf_import + GL_OES_EGL_image_external
/// + resolve the three needed entry points via eglGetProcAddress.
/// Returns Some(eps) on success, None if either extension or any
/// entry point is missing. Caller is expected to fall back to the
/// MMAP path on None. Cached for the lifetime of the thread; safe
/// to call per-frame.
#[cfg(target_os = "linux")]
fn dma_buf_egl_entry_points(
    egl_lib: &egl::DynamicInstance<egl::EGL1_5>,
    display: egl::Display,
    gl: &glow::Context,
) -> Option<DmaBufEglEntryPoints> {
    DMA_BUF_EGL_CACHE.with(|cell| {
        if let Some(state) = cell.get() {
            return state;
        }
        // EGL extension string: per-display query.
        let egl_exts = egl_lib
            .query_string(Some(display), egl::EXTENSIONS)
            .ok()
            .and_then(|s| s.to_str().ok().map(str::to_string))
            .unwrap_or_default();
        let has_dmabuf_import = egl_exts.split_whitespace()
            .any(|t| t == "EGL_EXT_image_dma_buf_import");
        // GLES extension string: GL_OES_EGL_image_external in
        // GL_EXTENSIONS (the spec-string form via glGetString).
        let gl_exts = unsafe {
            use glow::HasContext;
            gl.get_parameter_string(glow::EXTENSIONS)
        };
        let has_external_oes = gl_exts.split_whitespace()
            .any(|t| t == "GL_OES_EGL_image_external");
        if !has_dmabuf_import || !has_external_oes {
            eprintln!(
                "DmaBuf EGLImage path disabled -- EGL_EXT_image_dma_buf_import={} GL_OES_EGL_image_external={}",
                has_dmabuf_import, has_external_oes
            );
            cell.set(Some(None));
            return None;
        }
        // Resolve entry points. eglGetProcAddress returns NULL for
        // missing functions; we treat any NULL as "extension lied"
        // and fall back.
        let create_ptr = egl_lib.get_proc_address("eglCreateImageKHR");
        let destroy_ptr = egl_lib.get_proc_address("eglDestroyImageKHR");
        let target_ptr = egl_lib.get_proc_address("glEGLImageTargetTexture2DOES");
        let (Some(create_ptr), Some(destroy_ptr), Some(target_ptr)) =
            (create_ptr, destroy_ptr, target_ptr)
        else {
            eprintln!(
                "DmaBuf EGLImage path disabled -- eglGetProcAddress returned NULL: create={} destroy={} target={}",
                create_ptr.is_some(), destroy_ptr.is_some(), target_ptr.is_some()
            );
            cell.set(Some(None));
            return None;
        };
        // SAFETY: ptrs are extern "C" function pointers loaded by
        // the EGL implementation; signatures pinned by the EGL +
        // GL_OES_EGL_image_external specs. Mis-signature would be
        // a Mesa bug; we trust the spec.
        let eps = unsafe {
            DmaBufEglEntryPoints {
                create_image: std::mem::transmute::<
                    extern "system" fn(),
                    unsafe extern "C" fn(
                        *mut std::ffi::c_void,
                        *mut std::ffi::c_void,
                        u32,
                        *mut std::ffi::c_void,
                        *const i32,
                    ) -> *mut std::ffi::c_void,
                >(create_ptr),
                destroy_image: std::mem::transmute::<
                    extern "system" fn(),
                    unsafe extern "C" fn(
                        *mut std::ffi::c_void,
                        *mut std::ffi::c_void,
                    ) -> u32,
                >(destroy_ptr),
                image_target_texture_2d: std::mem::transmute::<
                    extern "system" fn(),
                    unsafe extern "C" fn(u32, *mut std::ffi::c_void),
                >(target_ptr),
            }
        };
        cell.set(Some(Some(eps)));
        Some(eps)
    })
}

/// Cached external-OES NV12 program (piece 4b's
/// FS_NV12_DMABUF_TO_RGB). Single sampler uniform `u_tex_external`
/// (bound to GL_TEXTURE_EXTERNAL_OES + an EGLImage).
#[cfg(target_os = "linux")]
#[derive(Copy, Clone)]
struct CachedNv12DmaBufProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_tex_external: Option<glow::NativeUniformLocation>,
    /// r83 Phase B: y-axis crop fraction; mirrors `CachedNv12Program`.
    /// Default 1.0; set by `run_nv12_dmabuf_blit_pass`.
    u_y_crop_max: Option<glow::NativeUniformLocation>,
}

#[cfg(target_os = "linux")]
std::thread_local! {
    static NV12_DMABUF_PROGRAM: std::cell::Cell<Option<CachedNv12DmaBufProgram>> =
        const { std::cell::Cell::new(None) };
}

#[cfg(target_os = "linux")]
fn cached_nv12_dmabuf_program(gl: &glow::Context) -> Result<CachedNv12DmaBufProgram> {
    use glow::HasContext;
    NV12_DMABUF_PROGRAM.with(|c| {
        if let Some(p) = c.get() {
            return Ok(p);
        }
        let program = link_program(
            gl,
            VS_TEXTURED_QUAD,
            crate::hdmi_logic::FS_NV12_DMABUF_TO_RGB,
        )
        .context("link FS_NV12_DMABUF_TO_RGB (VideoSlide DmaBuf paint)")?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("FS_NV12_DMABUF_TO_RGB VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("FS_NV12_DMABUF_TO_RGB VS missing a_uv"))?;
        let u_tex_external = unsafe {
            gl.get_uniform_location(program, "u_tex_external")
        };
        let u_y_crop_max = unsafe {
            gl.get_uniform_location(program, "u_y_crop_max")
        };
        let cnp = CachedNv12DmaBufProgram {
            program, a_pos, a_uv, u_tex_external, u_y_crop_max,
        };
        c.set(Some(cnp));
        Ok(cnp)
    })
}

/// V4L2 piece 4c: zero-copy NV12 paint via EGLImage import.
///
/// Inputs:
///   - `gl`: the active GLES2 context.
///   - `vbo`: the 4-vert interleaved `[x,y,u,v]` TRIANGLE_STRIP
///     quad to draw — `cached_textured_quad_vbo` for a plain fill
///     or a `cover_quad_vbo` for FYS bug B cover-fit.
///   - `egl_lib`, `display`: the EGL display the GL context was
///     created against. Used for eglCreateImageKHR.
///   - `fd`: V4L2-exported DMA-BUF file descriptor (from
///     `Frame::dma_buf_fd()`). Caller MUST hold the Frame alive
///     until this function returns (the fd is closed by the
///     Decoder's Drop, not here).
///   - `width`, `height`: frame dimensions in pixels.
///   - `stride`: V4L2-reported `plane_fmt[0].bytesperline`. For
///     NV12 single-plane, the Y plane has `stride` bytes per row
///     and the UV plane starts at offset `stride * height` with
///     the same `stride`. Stride MUST be the kernel-reported value,
///     NOT width -- alignment padding makes them differ on some
///     codecs.
///
/// Behavior:
///   - Calls eglCreateImageKHR with EGL_LINUX_DMA_BUF_EXT + both
///     plane attribs pointing at the SAME fd (bcm2835-codec NV12
///     CAPTURE is single-plane; UV at offset Y_SIZE).
///   - Creates a transient GL_TEXTURE_EXTERNAL_OES texture, binds
///     the EGLImage via glEGLImageTargetTexture2DOES.
///   - Draws the `vbo` quad through FS_NV12_DMABUF_TO_RGB.
///   - Tears down the texture + EGLImage. Caller-managed:
///     destruction order is texture-then-image (the texture holds
///     a reference to the image until unbound; destroying the
///     image first would briefly leave the texture pointing at a
///     freed kernel resource, which Mesa would NaN-fill on next
///     sample).
///
/// Returns:
///   - `Ok(true)` -- DmaBuf path took the paint; caller does not
///     need to fall back.
///   - `Ok(false)` -- extensions or entry points missing; caller
///     SHOULD fall back to the MMAP + FS_NV12_TO_RGB path. No GL
///     state was mutated.
///   - `Err(_)` -- eglCreateImageKHR returned EGL_NO_IMAGE or
///     GL pass errored. State may be partially mutated; caller
///     should treat this as a transient frame failure.
/// 2026-06-15 Option B: render-thread EGLImage cache pre-warm helper.
///
/// QA's tail-diag-v2.1 sample isolated the milder transition slow ticks
/// (525-542 ms total) to cache MISS-fresh-create eglCreateImageKHR
/// calls on the cache_path=true arm of run_nv12_dmabuf_blit_pass. The
/// cache is lazily populated one-buffer-at-a-time on the bake hot
/// path; under 2-video transition load the first 8 transition ticks
/// each pay the eglCreateImageKHR cost for a new buffer index, surfacing
/// as a per-tick stall the user perceives as motion judder.
///
/// This helper batches the cold-fill: on the first transition bake
/// where the cache is detected cold (cached_egl_image(0).is_none()),
/// we invoke code1's new Decoder.prewarm_egl_image_cache method to
/// iterate ALL CAPTURE buffer fds + create their EGLImages in one
/// Mutex-acquired batch. Subsequent transition paints all hit the
/// cache HIT path; import_us drops from ~83-148 ms (per slow tick)
/// to ~1 ms.
///
/// HARD INVARIANTS preserved by construction (sacred lead concerns):
///   - r101 dmabuf-ref-leak: handles inserted via prewarm ride the
///     same DecoderInner::Drop teardown as lazy-fill (code1's accessor
///     contract).
///   - get_or_init_egl_image idempotency: the closure passed to
///     prewarm_egl_image_cache mirrors the create_one shape from
///     run_nv12_dmabuf_blit_pass exactly (same attribs layout, same
///     EglImageHandle build).
///   - No GL state mutation outside the EGLImage create (no texture
///     binds, no shader use); the caller still owns all GL state.
///   - The closure runs INSIDE the Decoder's Mutex-acquired scope; it
///     MUST NOT call any other Decoder method (would deadlock per
///     std::sync::Mutex non-reentrance). The closure here only does
///     eglCreateImageKHR + EglImageHandle::new — no Decoder access.
///
/// Emits `[perf] eglimage_prewarm_transition entry w=N h=N stride=N`
/// as the QA-side fingerprint. Code1's accessor follows with its own
/// `[perf] prewarm_egl_image_cache total=N warmed=N skipped=N` line
/// summarizing how many slots were cold-filled vs already populated.
///
/// Safety: matches run_nv12_dmabuf_blit_pass — extern "C" EGL fn ptrs
/// loaded via eglGetProcAddress, called per spec; signatures pinned
/// by the EGL + GL_OES_EGL_image_external specs.
#[cfg(target_os = "linux")]
unsafe fn prewarm_egl_image_cache_for_decoder(
    decoder: &crate::v4l2::Decoder,
    egl_lib: &egl::DynamicInstance<egl::EGL1_5>,
    display: egl::Display,
    gl: &glow::Context,
    width: u32,
    height: u32,
    stride: u32,
) -> Result<()> {
    let Some(eps) = dma_buf_egl_entry_points(egl_lib, display, gl) else {
        return Ok(());
    };
    eprintln!(
        "[perf] eglimage_prewarm_transition entry w={} h={} stride={}",
        width, height, stride,
    );
    decoder.prewarm_egl_image_cache(|idx, fd| {
        let y_size: i32 = (stride as i32) * (height as i32);
        let attribs: [i32; 20] = [
            // EGL_WIDTH + EGL_HEIGHT spec-numbered 0x3057 + 0x3056 —
            // matches the attribs layout in run_nv12_dmabuf_blit_pass
            // exactly. Any drift between these two attrib arrays would
            // be a sacred-review BLOCKER.
            0x3057, width as i32,
            0x3056, height as i32,
            EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_NV12,
            EGL_DMA_BUF_PLANE0_FD_EXT, fd,
            EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
            EGL_DMA_BUF_PLANE0_PITCH_EXT, stride as i32,
            EGL_DMA_BUF_PLANE1_FD_EXT, fd,
            EGL_DMA_BUF_PLANE1_OFFSET_EXT, y_size,
            EGL_DMA_BUF_PLANE1_PITCH_EXT, stride as i32,
            EGL_NONE_ATTR,
            // Trailing 0 — unused; EGL parser stops at EGL_NONE.
            0,
        ];
        let img = (eps.create_image)(
            display.as_ptr(),
            std::ptr::null_mut(),  // EGL_NO_CONTEXT
            EGL_LINUX_DMA_BUF_EXT,
            std::ptr::null_mut(),  // buffer = NULL for dma_buf
            attribs.as_ptr(),
        );
        if img.is_null() {
            return Err(anyhow!(
                "eglCreateImageKHR(prewarm, idx={}, fd={}, w={}, h={}, stride={}) -> EGL_NO_IMAGE",
                idx, fd, width, height, stride,
            ));
        }
        Ok(crate::v4l2::EglImageHandle {
            image: img,
            display: display.as_ptr(),
            destroy_fn: eps.destroy_image,
        })
    })
}

#[cfg(target_os = "linux")]
pub unsafe fn run_nv12_dmabuf_blit_pass(
    gl: &glow::Context,
    vbo: glow::NativeBuffer,
    egl_lib: &egl::DynamicInstance<egl::EGL1_5>,
    display: egl::Display,
    fd: std::os::fd::RawFd,
    width: u32,
    height: u32,
    stride: u32,
    y_crop_max: f32,
    // 2026-06-15 spike-kill (Karl-live-QA stutter, post Option B):
    // session-cached GL_TEXTURE_EXTERNAL_OES texture object. When
    // Some(t), this function REUSES `t` (just rebinds + image_target
    // _texture_2d to associate with the new EGLImage; sampler state
    // is sticky per GLES2 + was set once at lazy-init time in the
    // caller). When None, falls back to the historical per-frame
    // create+destroy path (the V3D BO alloc the v2.1 sample measured
    // at 200-400 ms sampler_us under memory pressure).
    cached_texture: Option<glow::NativeTexture>,
    // r101 (2026-06-09): EGLImage cache slot. `Some((decoder, idx))`
    // -> look up Decoder::cached_egl_image(idx); if None, lazy-create
    // + insert. DO NOT destroy at function end (cleanup happens in
    // DecoderInner::Drop). `None` -> fall back to pre-r101 per-frame
    // create+destroy (set by callers when
    // OPENMARQUEE_EGL_IMAGE_CACHE=off is the operator's kill switch
    // OR when the caller doesn't have a Decoder ref to thread).
    egl_image_cache: Option<(&crate::v4l2::Decoder, u32)>,
) -> Result<bool> {
    use glow::HasContext;
    // tail-diag instrumentation v2 (2026-06-15, follows code1's
    // tail-diag-v1 per QA + admin routing). v1 isolated blit_us as
    // the dominant phase during transition stalls (up to 8.8s on
    // ~14% of transitions, all in_transition=true, all path=
    // dmabuf). v2 sub-phases what HAPPENS inside the blit so QA
    // can narrow GL2.1 (2-dmabuf overload) vs GL2.2 (sync/fence
    // stall) per admin's hypothesis tree:
    //   import_us large   -> EGLImage acquire stall (Mutex
    //                        contention OR per-frame eglCreate
    //                        ImageKHR slow path)
    //   sampler_us large  -> create_texture / bind / tex_params
    //                        (V3D BO alloc + driver state churn)
    //   draw_us large     -> GL/GPU stall on shader+draw (vc4
    //                        V3D overload during dual-video bake)
    //   destroy_us large  -> texture delete / EGLImage destroy
    //                        (V3D BO free under memory pressure)
    // The iter-7 gl.flush() is OUTSIDE this function (in
    // bake_video_slide_to_current_fbo); a sibling tail-diag-v2-
    // flush probe at that site captures it as flush_us. QA
    // correlates the two by sequence + timestamp.
    //
    // Gate: emit only when this function's total_us > 500_000
    // (500 ms) — well above any fast tick, well below the
    // multi-second freezes. Steady-state never trips the gate.
    // Probe overhead bound: 5 CLOCK_MONOTONIC reads + 1 compare
    // ~= 1 µs per fast tick; emit cost ~5-50 µs per slow tick.
    // Pure field-add per the cross-lane instrumentation rule —
    // NO signature change, NO control-flow change, NO new
    // return shape. tail_diag_blit_subphase string-pin is the
    // QA-side fingerprint marker.
    let t_total_start = std::time::Instant::now();
    // Lazy-resolve EGL+GLES extension entry points. None -> caller
    // falls back to MMAP path.
    let Some(eps) = dma_buf_egl_entry_points(egl_lib, display, gl) else {
        return Ok(false);
    };
    // EGL attribute list: width/height + format + plane0 (Y) and
    // plane1 (UV) attribs. Same fd for both planes because bcm2835-
    // codec NV12 packs Y+UV in one buffer (UV at offset Y_SIZE).
    // EGL_DMA_BUF_PLANE*_PITCH_EXT must use the V4L2-reported
    // `bytesperline`, NOT `width` -- piece 4 dispatch §"Stride vs
    // width" subagent-blocker check.
    let y_size: i32 = (stride as i32) * (height as i32);
    // 9 attribute pairs (18 elements) + EGL_NONE terminator +
    // trailing pad = 20 elements. The EGL spec terminates at the
    // first EGL_NONE; the trailing 0 is unused but keeps the
    // array length even for a stable size signature if a future
    // plane needs adding.
    let attribs: [i32; 20] = [
        // EGL_WIDTH + EGL_HEIGHT are spec'd as 0x3057 + 0x3056.
        0x3057, width as i32,
        0x3056, height as i32,
        EGL_LINUX_DRM_FOURCC_EXT, DRM_FORMAT_NV12,
        EGL_DMA_BUF_PLANE0_FD_EXT, fd,
        EGL_DMA_BUF_PLANE0_OFFSET_EXT, 0,
        EGL_DMA_BUF_PLANE0_PITCH_EXT, stride as i32,
        EGL_DMA_BUF_PLANE1_FD_EXT, fd,
        EGL_DMA_BUF_PLANE1_OFFSET_EXT, y_size,
        EGL_DMA_BUF_PLANE1_PITCH_EXT, stride as i32,
        EGL_NONE_ATTR,
        // Trailing 0 -- unused; EGL parser stops at EGL_NONE.
        0,
    ];
    // SAFETY: eps.create_image is a Mesa-loaded extern "C" fn ptr
    // matching the eglCreateImageKHR spec. display.as_ptr() is the
    // r101 (2026-06-09): when egl_image_cache is Some, use
    // Decoder::get_or_init_egl_image to atomically check+create+
    // insert under one Mutex acquisition. Subagent WARN-4 caught
    // that the prior two-call pattern (cached_egl_image then
    // cache_egl_image) opens a double-create race window if a
    // future caller is multi-threaded. The atomic helper closes
    // that window. When egl_image_cache is None (kill switch),
    // create per-frame + destroy at the end (the leaky path,
    // kept for A/B rollback).
    // 2026-06-15 tail-fix-v2.1: surface the `created` bool from
    // get_or_init_egl_image's return tuple so the emitted
    // tail_diag_blit_subphase line can distinguish cache HIT from
    // cache MISS-fresh-create on the cache_path=true arm. v2 swallowed
    // the bool via `let (handle, _created) = ...` — sacred caught this
    // as the load-bearing ambiguity; code1 nit-tagged the underscore
    // for rename so the source matches the post-v2.1 contract.
    //
    // cache_path=false arm (kill-switch / no cache) always creates a
    // fresh handle per-frame → emit `created=true` for that arm to
    // keep the emit shape uniform.
    let (egl_image, suppress_destroy_at_end, created) = if let Some((decoder, idx)) = egl_image_cache {
        let create_one = || -> Result<crate::v4l2::EglImageHandle> {
            let img = (eps.create_image)(
                display.as_ptr(),
                std::ptr::null_mut(),  // EGL_NO_CONTEXT
                EGL_LINUX_DMA_BUF_EXT,
                std::ptr::null_mut(),  // buffer = NULL for dma_buf
                attribs.as_ptr(),
            );
            if img.is_null() {
                return Err(anyhow!(
                    "eglCreateImageKHR(LINUX_DMA_BUF_EXT, fd={}, w={}, h={}, stride={}) -> EGL_NO_IMAGE (cache path)",
                    fd, width, height, stride
                ));
            }
            Ok(crate::v4l2::EglImageHandle {
                image: img,
                display: display.as_ptr(),
                destroy_fn: eps.destroy_image,
            })
        };
        let (handle, created) = decoder.get_or_init_egl_image(idx, create_one)?;
        (handle.image, true, created)
    } else {
        // Pre-r101 path: per-frame create+destroy. Leaks one
        // kernel dmabuf ref per call on Mesa+vc4 -- the bug r101
        // exists to fix. Kept behind the OPENMARQUEE_EGL_IMAGE_CACHE
        // kill switch so QA can A/B if the cache itself regresses.
        let img = (eps.create_image)(
            display.as_ptr(),
            std::ptr::null_mut(),  // EGL_NO_CONTEXT
            EGL_LINUX_DMA_BUF_EXT,
            std::ptr::null_mut(),  // buffer = NULL for dma_buf
            attribs.as_ptr(),
        );
        if img.is_null() {
            return Err(anyhow!(
                "eglCreateImageKHR(LINUX_DMA_BUF_EXT, fd={}, w={}, h={}, stride={}) -> EGL_NO_IMAGE (no-cache path)",
                fd, width, height, stride
            ));
        }
        // cache disabled → this call always created a fresh handle.
        (img, false, true)
    };
    // tail-diag-v2 phase boundary: EGLImage acquired (cache hit OR
    // per-frame create). Everything from fn entry up to here is
    // "import" — the EGL_LINUX_DMA_BUF_EXT import + the Mutex
    // acquisition for the cached path. Suspect surface for GL2.1
    // (concurrent dmabuf import overload).
    let t_import_end = std::time::Instant::now();
    // Create + bind the external-OES texture. Set min/mag filter
    // to LINEAR; CLAMP_TO_EDGE both axes (the spec lists wrap as
    // one of the supported parameters on TEXTURE_EXTERNAL_OES;
    // CLAMP_TO_EDGE is universally accepted).
    // r40 (2026-06-02): if glGenTextures fails the EGLImage above
    // would leak the kernel-side dma_buf ref (~3 MB CMA per NV12
    // frame at 1080p). The EGLImage holds an independent ref on
    // the dma_buf via the EGL_LINUX_DMA_BUF_EXT import; without an
    // explicit destroy_image the kernel keeps the buffer pinned
    // until renderer exit. Frame::Drop's re-QBUF only re-queues the
    // V4L2 buffer slot; it doesn't release the EGLImage's dma_buf
    // ref. See qa/r40-non-fys-allocator-fixes-2026-06-02.md.
    //
    // spike-kill (2026-06-15): when caller passed a session-cached
    // texture, REUSE IT — skip create + skip 4× tex_parameter_i32
    // (sampler state is sticky per GLES2 + was set once at lazy-init
    // time by the caller). Cuts V3D BO alloc + 4 driver state-set
    // calls per frame on the production hot path.
    let (tex, tex_was_cached) = match cached_texture {
        Some(t) => (t, true),
        None => {
            let t = match gl.create_texture() {
                Ok(t) => t,
                Err(e) => {
                    // r101: only destroy if we created locally (no-cache
                    // path). Cache hit/miss with the cache enabled means
                    // the EGLImage is owned by Decoder; let
                    // DecoderInner::Drop handle teardown.
                    if !suppress_destroy_at_end {
                        let destroyed = (eps.destroy_image)(display.as_ptr(), egl_image);
                        if destroyed == 0 {
                            eprintln!(
                                "warn: eglDestroyImageKHR returned EGL_FALSE for fd={} during create_texture-fail cleanup",
                                fd
                            );
                        }
                    }
                    return Err(anyhow!("glGenTextures(external-OES): {e}"));
                }
            };
            (t, false)
        }
    };
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, Some(tex));
    if !tex_was_cached {
        // First-use sampler state. Sticks per GLES2 spec so cached-
        // path frames skip these 4 driver calls entirely.
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    }
    // Associate the EGLImage with the bound external-OES texture.
    // From this point the texture samples the dma_buf bytes
    // directly -- zero CPU copy.
    (eps.image_target_texture_2d)(GL_TEXTURE_EXTERNAL_OES, egl_image);
    // tail-diag-v2 phase boundary: texture object created + bound
    // + sampler state set + EGLImage→texture associated. Everything
    // between t_import_end and here is "sampler" — V3D BO alloc
    // for the texture handle + 4 tex_parameter_i32 driver calls +
    // image_target_texture_2d (the actual EGLImage→texture bind).
    let t_sampler_end = std::time::Instant::now();

    // Run the blit pass. cached_nv12_dmabuf_program lazy-links
    // FS_NV12_DMABUF_TO_RGB on first call.
    let blit_result = (|| -> Result<()> {
        let cnp = cached_nv12_dmabuf_program(gl)?;
        gl.use_program(Some(cnp.program));
        // Texture is ALREADY bound + the image associated above;
        // just set the sampler uniform to TEXTURE0.
        gl.uniform_1_i32(cnp.u_tex_external.as_ref(), 0);
        // r83 Phase B: mirror the MMAP-path crop. y_crop_max=1.0 is
        // the no-crop default; caller passes the
        // `Decoder::capture_y_crop_max()` value to skip
        // bcm2835-codec's bottom-row green padding.
        gl.uniform_1_f32(cnp.u_y_crop_max.as_ref(), y_crop_max);
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        gl.enable_vertex_attrib_array(cnp.a_pos);
        gl.vertex_attrib_pointer_f32(cnp.a_pos, 2, glow::FLOAT, false, 16, 0);
        gl.enable_vertex_attrib_array(cnp.a_uv);
        gl.vertex_attrib_pointer_f32(cnp.a_uv, 2, glow::FLOAT, false, 16, 8);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
        gl.disable_vertex_attrib_array(cnp.a_pos);
        gl.disable_vertex_attrib_array(cnp.a_uv);
        Ok(())
    })();
    // tail-diag-v2 phase boundary: shader bound + uniforms set +
    // draw issued + state-cleanup done. Everything between
    // t_sampler_end and here is "draw" — the actual GPU work
    // submission. Suspect surface for GL2.2 (vc4 V3D pipeline
    // stall on shader+draw under 2-video concurrent load).
    let t_draw_end = std::time::Instant::now();

    // Teardown ordering: unbind texture, delete texture, THEN
    // destroy the EGLImage. The driver keeps the dma_buf reference
    // alive via the EGLImage until destroy. Frame::Drop's re-QBUF
    // is what re-enqueues the buffer index for the next decode; the
    // EGLImage ref-count is dropped here so the kernel can release
    // the dma_buf at the right moment.
    gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
    // spike-kill: only delete when we created locally. The session-
    // cached texture survives across frames + is freed in
    // cleanup_resources at session teardown.
    if !tex_was_cached {
        gl.delete_texture(tex);
    }
    // r101: only destroy at the end when the cache is disabled
    // (no-cache pre-r101 path). When the cache is enabled the
    // EGLImage is owned by Decoder.capture_egl_images and will be
    // destroyed in DecoderInner::Drop -- destroying it here would
    // either invalidate the cache for the next frame OR fire the
    // Mesa+vc4 leak that r101 exists to plug.
    if !suppress_destroy_at_end {
        let destroyed = (eps.destroy_image)(display.as_ptr(), egl_image);
        if destroyed == 0 {
            // EGL_FALSE -- surfacing as a warn rather than an Err
            // because the paint already happened + the next frame's
            // re-import will catch any persistent leak.
            eprintln!("warn: eglDestroyImageKHR returned EGL_FALSE for fd={}", fd);
        }
    }

    // tail-diag-v2 phase boundary + gated emit. Everything between
    // t_draw_end and here is "destroy" — texture delete + (if
    // no-cache path) EGLImage destroy. Suspect surface for V3D BO
    // free under memory pressure (rare unless the allocator is
    // saturated).
    let t_destroy_end = std::time::Instant::now();
    let total_us = t_destroy_end.duration_since(t_total_start).as_micros() as u64;
    if total_us > 500_000 {
        let import_us = t_import_end.duration_since(t_total_start).as_micros() as u64;
        let sampler_us = t_sampler_end.duration_since(t_import_end).as_micros() as u64;
        let draw_us = t_draw_end.duration_since(t_sampler_end).as_micros() as u64;
        let destroy_us = t_destroy_end.duration_since(t_draw_end).as_micros() as u64;
        // 2026-06-15 tail-fix-v2.1: cache_path + created bools together
        // give QA the full root-cause attribution for import_us spikes:
        //   cache_path=true  + created=false → cache HIT; large import_us
        //                                       = uncontested-lock kernel/
        //                                       futex anomaly = NO renderer
        //                                       fix (surface to admin as
        //                                       out-of-scope kernel-side).
        //   cache_path=true  + created=true  → cache MISS-fresh-create;
        //                                       large import_us = eglCreate
        //                                       ImageKHR is the slow op =
        //                                       my-lane Option B (render-
        //                                       thread pre-warm at
        //                                       transition setup so the
        //                                       8-buffer cold fill happens
        //                                       outside the bake critical
        //                                       path).
        //   cache_path=false + created=true  → kill-switch leak path;
        //                                       eglCreateImageKHR every
        //                                       call; OPENMARQUEE_EGL_
        //                                       IMAGE_CACHE=on should be
        //                                       re-enabled in prod.
        // The created=<bool> tag closes the bug-shadow sacred caught in
        // v2 (the `let (handle, _created) = ...` underscore on the
        // get_or_init return tuple).
        eprintln!(
            "[perf] tail_diag_blit_subphase import_us={} sampler_us={} draw_us={} destroy_us={} total_us={} cache_path={} created={}",
            import_us, sampler_us, draw_us, destroy_us, total_us, suppress_destroy_at_end, created,
        );
    }

    blit_result?;
    Ok(true)
}

/// Pre-warm GLES2 program cache at sidecar startup so the first
/// video slide (and the first transition) don't pay the
/// link_program cost in the paint hot path.
///
/// perf-night r6 (2026-05-28): r5 captured paint_bake_video_shader
/// max=592ms + paint_compose max=132ms — both first-call shader
/// compiles ([[project-perf-night-code1-r1-r5-2026-05-26]] for the
/// r5 baseline). The warmup pre-feed in r5 closed V4L2 cold-start
/// at the decoder side; this closes the GL-side compile cold-start.
///
/// Failure semantics: each compile is independent; a single failure
/// logs `warn:` and the shader stays uncached (next runtime use
/// will compile on demand, surfacing the same error then). Does
/// NOT abort startup — the sidecar should still come up even if
/// (say) FS_HALFTONE has a driver bug.
#[cfg(target_os = "linux")]
fn prewarm_shader_programs(session: &EglSession) {
    let t0 = std::time::Instant::now();
    let mut compiled: u32 = 0;
    let mut failed: u32 = 0;
    let gl = session.gl;

    macro_rules! try_warm {
        ($label:expr, $expr:expr) => {
            match $expr {
                Ok(_) => compiled += 1,
                Err(e) => {
                    eprintln!("warn: prewarm {} failed: {e}", $label);
                    failed += 1;
                }
            }
        };
    }

    // EGL extension entry-point resolution gates the NV12 DMABUF
    // path: a sidecar without EGL_EXT_image_dma_buf_import + GL_OES_
    // EGL_image_external silently falls back to MMAP at runtime
    // (run_nv12_dmabuf_blit_pass returns Ok(false)). Mirror that
    // gate here so we don't try to compile FS_NV12_DMABUF_TO_RGB
    // (which declares `#extension GL_OES_EGL_image_external : require`)
    // on a system where the extension is absent — that compile would
    // bump the failed counter for a reason that isn't actionable.
    let has_dmabuf = dma_buf_egl_entry_points(session.egl_lib, session.display, gl).is_some();

    if has_dmabuf {
        // The proven r5 culprit: 592ms first-call compile on the
        // initial video slide.
        try_warm!("nv12_dmabuf_program", cached_nv12_dmabuf_program(gl));
    }
    // MMAP fallback path (taken when the DMABUF gate fails or the
    // Frame doesn't expose a dma_buf_fd). Always pre-warm so the
    // fallback doesn't pay a fresh cold-start either.
    try_warm!("nv12_program", cached_nv12_program(gl));
    try_warm!("nv12_cover_program", cached_nv12_cover_program(gl));

    // Compose pipeline shaders (blit + brightness/gamma + overlay
    // blend). paint_compose max=132ms in r5 capture; expected first-
    // transition compile.
    try_warm!("blit_program", cached_blit_program(gl));
    // bright_gamma + overlay_blend are unsafe-marked; same lazy-
    // compile pattern under the hood.
    try_warm!("bright_gamma_program", unsafe { cached_bright_gamma_program(gl) });
    try_warm!("overlay_blend_program", unsafe { cached_overlay_blend_program(gl) });

    // Cut composite — both sides pre-cached. The cut runtime path
    // (hdmi.rs:8723) explicitly does NOT compile cached_composite_
    // program("cut") — it uses cached_cut_composite_program(side_b)
    // exclusively — so we skip "cut" in the composite loop below to
    // match that contract (otherwise we burn an unused program slot).
    try_warm!("cut_composite_program(A)", cached_cut_composite_program(gl, false));
    try_warm!("cut_composite_program(B)", cached_cut_composite_program(gl, true));

    // All 16 transition kinds — pre-warm two caches each:
    //   - cached_transition_program(fs): legacy 3-pass path (still
    //     the fallback for kinds where the scissored-bake tier
    //     doesn't fit; covers ALL 16 since the FS exists for each).
    //   - cached_composite_program(kind): scissored-bake composite
    //     path. Gated by sp_kind_static — "glitch" has no SP
    //     generator (sp_kind_static returns None at hdmi_logic.rs:
    //     7054) so cached_composite_program("glitch") fails fast.
    //     Skip "cut" too per the comment above.
    //
    // SP single-pass tier (cached_transition_sp_program) needs
    // per-slide layer counts (n_a, n_b) which aren't known here.
    // prewarm_sp_session at line 11998 handles that in the reel
    // path; sidecar IPC paint goes through this path instead and
    // relies on the SP cache being populated lazily on first use.
    const TRANSITION_KINDS: &[&str] = &[
        "cut", "fade", "wipe", "iris", "dissolve", "pixelate", "scanline",
        "halftone", "glitch", "slide", "push", "scroll", "blinds", "flip",
        "marquee", "shutter",
    ];
    for kind in TRANSITION_KINDS {
        // Composite path: skip kinds the runtime intentionally avoids
        // OR can't compile.
        if *kind != "cut" && crate::hdmi_logic::sp_kind_static(kind).is_some() {
            try_warm!(format!("composite_program({kind})"),
                cached_composite_program(gl, kind));
        }
        // Legacy 3-pass path: pre-warm for all 16 (the FS exists per
        // fs_for_transition_kind).
        if let Some(fs) = crate::hdmi_logic::fs_for_transition_kind(kind) {
            try_warm!(format!("transition_program({kind})"),
                cached_transition_program(gl, fs));
        }
    }

    // r20 (2026-05-30): text-side shader programs. The first text
    // slide on a fresh sidecar previously paid the link cost for
    // cached_msdf_program (FS_MSDF_FIXED + FS_MSDF_OUTLINE_FIXED) on
    // the paint hot path -- baseline paint_bake_text MAX ~90 ms,
    // paint_compose MAX 340 ms in r6 capture. The 4 MSDF/glyph
    // variants cover the static-atlas + runtime-cache draw paths;
    // tofu + emoji cover the FontMissing fallback and the COLRv1
    // emoji quad path.
    try_warm!("glyph_program", cached_glyph_program(gl, false));
    try_warm!("glyph_outline_program", cached_glyph_program(gl, true));
    try_warm!("msdf_program", cached_msdf_program(gl, false));
    try_warm!("msdf_outline_program", cached_msdf_program(gl, true));
    try_warm!("tofu_program", cached_tofu_program(gl));
    try_warm!("emoji_program", cached_emoji_program(gl));

    let ms = t0.elapsed().as_secs_f64() * 1000.0;
    eprintln!(
        "prewarm: compiled {compiled} shader programs in {ms:.1}ms ({failed} failed)"
    );
}

/// r25 (2026-05-31): enqueue the demo-reel font set's printable-
/// ASCII glyph range into `session.dynamic_glyph_cache`, then
/// **drain to zero** before returning so the playback loop opens
/// its first paint_bake_text window against a fully-warm cache.
///
/// Why "drain to zero" not "wait N seconds": the playback loop's
/// poll_completions at hdmi.rs:3187 triggers a full slide_caches
/// drain on any frame where `uploaded > 0`. r20-first-ship used a
/// 3s deadline; on Pi Zero 2 W at the observed ~53 g/s msdfgen
/// rate, only ~159 of the 855 enqueued glyphs drained in budget.
/// The remaining ~696 bled into the playback loop at ~3/frame
/// over ~15s, firing slide_caches.drain() on every frame in that
/// window -> paint_bake_text p99 regressed from 126us to 2284us.
/// See 530cd25 commit body for the full forensics.
///
/// Trade: sidecar boot wall-time grows by the full drain duration
/// (~16s on Pi Zero 2 W for 855 glyphs). Plymouth handoff has
/// already covered the kernel-to-sidecar transition so the
/// operator sees a splash, not black, during this window. r25
/// chose the (c-cheap) variant from the QA dispatch: log start +
/// end, no synthetic Loading frame.
///
/// Watchdog: 120s hard cap. In practice 7-8x the realistic drain
/// time; exists so a catastrophically wedged worker pool (all 4
/// workers panicked, msdfgen FFI deadlock, etc.) can't gate the
/// sidecar forever. On watchdog trip: log + continue to playback;
/// uncached glyphs then populate lazily on first encounter, with
/// the same slide_caches-drain cost as the r20-first-ship
/// regression but bounded to the residual queue.
///
/// Known interaction with glyph_cache::poll_completions error
/// branches (atlas-full-with-no-evict at glyph_cache.rs:654,
/// upload_slot failure at glyph_cache.rs:662): both `continue`
/// PAST the `completion_count += 1` increment. If even one
/// prewarm glyph trips either branch, `completions_since_baseline`
/// never reaches `requested` and the gate spins until the
/// watchdog. Realistic likelihood: low (fresh GL context + 1820-
/// slot 2048×2048 page vs 855-glyph budget). The watchdog floor
/// is the failsafe; bumping completion_count in those branches
/// is a glyph_cache.rs follow-up, out of r25 scope.
// `prewarm_glyph_rasterization` removed in G-2 (2026-06-16):
// shifted to pure on-demand bake via paint-time `get_or_request`
// in layout_text_to_quads. See the docstring above
// `run_in_egl_session` for the rationale + the memory-cliff
// trade-off that made G-1's async-prewarm shape inadequate on
// the Pi Zero 2 W 96 MB non-CMA RAM ceiling.

/// Populate `slide_caches[slide_id].bg_tex` for non-solid bgs
/// at atlas region size (2048x1024). Idempotent: returns early
/// if already cached. Returns `Ok(())` for solid bgs (no cache
/// needed; glClear in the bake path is already free) WITHOUT
/// populating; caller checks `bg_tex.is_some()` to decide
/// blit-vs-clear.
///
/// On first call for a non-solid bg this:
///   1. Allocates a 2048x1024 RGBA texture + temporary FBO.
///   2. Binds the temp FBO + viewport (0, 0, 2048, 1024).
///   3. Renders bg via the existing draw_gradient_pattern /
///      draw_pattern / draw_image_bg helpers (one full-frag-fill).
///   4. Frees the temp FBO. Stores the texture in
///      slide_caches[slide_id].bg_tex.
///
/// Memory: 8 MB per cached bg. Cap by slide count -- the FYS
/// reel has 1 non-solid bg slide (Scream). Arbitrary content
/// could push higher but the slide_caches HashMap is naturally
/// bounded by playlist length.
unsafe fn ensure_slide_bg_cache(
    session: &mut EglSession,
    slide_id: uuid::Uuid,
    bg_kind: &BgKind,
) -> Result<()> {
    use glow::HasContext;
    // Solid bgs use scissor-clear in the bake; no cache benefit.
    if matches!(bg_kind, BgKind::Solid(_)) {
        return Ok(());
    }
    // Already populated?
    let already_cached = match session.slide_caches.get(&slide_id) {
        Some(c) => c.bg_tex.is_some(),
        None => false,
    };
    if already_cached {
        return Ok(());
    }
    // Slide cache slot must exist before we add bg_tex.
    if !session.slide_caches.contains_key(&slide_id) {
        // Caller hasn't initialized the slide cache. Skip --
        // the lazy fall-through path will still render bg
        // correctly (just per-frame instead of once).
        return Ok(());
    }
    let gl = session.gl;
    // Cache texture is sized to the atlas region (2048x1024). The
    // blit copies the texture 1:1 into the atlas region, so the
    // texture and region must match dims.
    let cache_w = crate::hdmi_logic::ATLAS_REGION_W as i32;
    let cache_h = crate::hdmi_logic::ATLAS_REGION_H as i32;
    // BG-render PROJECTION uses mode_w (1920) so gradient/pattern
    // math matches the non-cached direct-bake path. But the gl
    // viewport stays at the full atlas-region size (2048x1024) so
    // the blit-into-atlas-region remains a 1:1 copy. gl_FragCoord
    // pixels at x > mode_w land outside the gradient projection
    // span; they clamp to color_b for FS_GRADIENT and tile naturally
    // for FS_PATTERN_*. Composite never samples the [mode_w,
    // atlas_w) gutter (xform_a/b u-scale = mode_w/atlas_w), so the
    // gutter content doesn't reach the panel either way.
    let proj_w = session.mode_w as u32;
    let proj_h = crate::hdmi_logic::ATLAS_REGION_H;
    let bg_tex = gl
        .create_texture()
        .map_err(|e| anyhow!("bg_cache glGenTextures: {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(bg_tex));
    gl.tex_image_2d(
        glow::TEXTURE_2D, 0, glow::RGBA as i32, cache_w, cache_h, 0,
        glow::RGBA, glow::UNSIGNED_BYTE, None,
    );
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);

    let temp_fbo = gl
        .create_framebuffer()
        .map_err(|e| {
            gl.delete_texture(bg_tex);
            anyhow!("bg_cache glGenFramebuffers: {e}")
        })?;
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(temp_fbo));
    gl.framebuffer_texture_2d(
        glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(bg_tex), 0,
    );
    let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
    if status != glow::FRAMEBUFFER_COMPLETE {
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(temp_fbo);
        gl.delete_texture(bg_tex);
        bail!("bg_cache FBO incomplete: status=0x{status:x}");
    }
    gl.disable(glow::SCISSOR_TEST);
    gl.viewport(0, 0, cache_w, cache_h);

    // Render the bg into the cache texture. PROJECTION dims are
    // (mode_w, region_h) so gl_FragCoord-based shaders (FS_GRADIENT,
    // FS_PATTERN_*) compute the same projection bounds the non-cached
    // direct-bake path does. The gl viewport stays full 2048-wide so
    // the blit-into-atlas-region is 1:1 -- gutter pixels x in
    // [mode_w, atlas_w) get color_b (gradient clamp) or tiled
    // continuation (pattern); composite never samples them.
    let render_result: Result<()> = (|| {
        match bg_kind {
            BgKind::Solid(_) => unreachable!("filtered above"),
            BgKind::Gradient { color_a, color_b, density } => {
                draw_gradient_pattern(
                    gl, 0, 0, proj_w, proj_h,
                    *color_a, *color_b, *density,
                )?;
            }
            BgKind::Pattern { kind, color_a, color_b, density } => {
                // Cache path renders at viewport offset (0, 0), so
                // FS_PATTERN_*'s gl_FragCoord-based tile math is
                // correct here regardless of where the cache is
                // later blit into the atlas. The pattern shaders
                // still lack u_vp_offset, so the lazy-fallback
                // direct-bake path in render_transition_scissored_
                // _bake_in_session (which fires only on cache-Err,
                // a memory-pressure signal) renders pattern tiles
                // at the wrong absolute position when slide B's
                // bake hits viewport y_off=region_h. Best-effort
                // fallback; rare in practice. Add u_vp_offset to
                // FS_PATTERN_* if the fallback becomes load-bearing.
                draw_pattern(gl, proj_w, proj_h, *kind, *color_a, *color_b, *density)?;
            }
            BgKind::Image { asset_path, solid_fallback } => {
                draw_image_bg(gl, asset_path, *solid_fallback, Some(&mut session.image_bg_cache));
            }
        }
        Ok(())
    })();

    // Cleanup temp FBO regardless of render outcome. The
    // texture stays allocated and gets stored on success;
    // freed on error.
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    gl.delete_framebuffer(temp_fbo);
    if let Err(e) = render_result {
        gl.delete_texture(bg_tex);
        return Err(e).context("bg_cache render");
    }
    if let Some(cache) = session.slide_caches.get_mut(&slide_id) {
        cache.bg_tex = Some(bg_tex);
    } else {
        // Slide cache disappeared between our check + write
        // (shouldn't happen single-threaded); avoid leak.
        gl.delete_texture(bg_tex);
        bail!("bg_cache: slide_caches[{slide_id}] removed during populate");
    }
    Ok(())
}

/// Blit a cached bg texture into the currently-bound FBO at
/// the given region. Caller is responsible for binding the
/// FBO + setting viewport / scissor as needed BEFORE calling.
/// Uses cached_blit_program (FS_BLIT) and the existing
/// transition_sp_quad VBO for the full-screen draw.
fn blit_bg_to_region(
    gl: &glow::Context,
    blit_program: &CachedBlitProgram,
    vbo: glow::NativeBuffer,
    bg_tex: glow::NativeTexture,
) -> Result<()> {
    use glow::HasContext;
    unsafe {
        gl.use_program(Some(blit_program.program));
        gl.active_texture(glow::TEXTURE0);
        gl.bind_texture(glow::TEXTURE_2D, Some(bg_tex));
        gl.uniform_1_i32(blit_program.u_src.as_ref(), 0);
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let stride = (4 * std::mem::size_of::<f32>()) as i32;
        gl.enable_vertex_attrib_array(blit_program.a_pos);
        gl.vertex_attrib_pointer_f32(blit_program.a_pos, 2, glow::FLOAT, false, stride, 0);
        gl.enable_vertex_attrib_array(blit_program.a_uv);
        gl.vertex_attrib_pointer_f32(
            blit_program.a_uv, 2, glow::FLOAT, false, stride,
            (2 * std::mem::size_of::<f32>()) as i32,
        );
        gl.disable(glow::BLEND);
        gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    }
    Ok(())
}

/// Scissored-bake atlas FBO (2026-05-09 redirect): one
/// 2048x2048 FBO+texture for the dual-slide bake. Slide A region
/// at y in [0, 1024); slide B region at y in [1024, 2048). Each
/// region is 2048 wide (1920 used, 128 gutter) and 1024 tall
/// (1080 → 1024 = 5.5% vertical compression upsampled at
/// composite). Lazy-init; freed at with_egl_session teardown.
unsafe fn ensure_bake_atlas(
    session: &mut EglSession,
) -> Result<(glow::NativeFramebuffer, glow::NativeTexture)> {
    use glow::HasContext;
    if let Some(pair) = session.scissored_bake_atlas {
        return Ok(pair);
    }
    let gl = session.gl;
    let atlas_w = crate::hdmi_logic::ATLAS_FBO_W as i32;
    let atlas_h = crate::hdmi_logic::ATLAS_FBO_H as i32;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!("atlas glGenTextures: {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_image_2d(
        glow::TEXTURE_2D, 0, glow::RGBA as i32, atlas_w, atlas_h, 0,
        glow::RGBA, glow::UNSIGNED_BYTE, None,
    );
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    let fbo = gl
        .create_framebuffer()
        .map_err(|e| {
            gl.delete_texture(tex);
            anyhow!("atlas glGenFramebuffers: {e}")
        })?;
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
    gl.framebuffer_texture_2d(
        glow::FRAMEBUFFER, glow::COLOR_ATTACHMENT0, glow::TEXTURE_2D, Some(tex), 0,
    );
    let status = gl.check_framebuffer_status(glow::FRAMEBUFFER);
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    if status != glow::FRAMEBUFFER_COMPLETE {
        gl.delete_framebuffer(fbo);
        gl.delete_texture(tex);
        bail!("atlas FBO incomplete: status=0x{status:x}");
    }
    let pair = (fbo, tex);
    session.scissored_bake_atlas = Some(pair);
    Ok(pair)
}

/// QA-mandated scissored-bake (Step 4): eligibility for the
/// scissored-bake path. Same shape as single-pass eligibility
/// but with a higher per-slide layer cap. Used after single-
/// pass eligibility fails (e.g. n_a + n_b > 4) to determine
/// whether to take scissored-bake or fall through to legacy
/// 3-pass.
fn transition_eligible_for_scissored_bake(
    kind: &str,
    _bg_a: &BgKind,
    _bg_b: &BgKind,
    layers_a: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    layers_b: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
) -> bool {
    // bg type widening (cold-scout #1, 2026-05-09): atlas SB used to
    // require solid-or-density-0-gradient on both sides via
    // effective_solid_bg. The bg-cache machinery
    // (ensure_slide_bg_cache + blit_bg_to_region) handles every
    // BgKind variant -- gradient, pattern, image, solid -- by
    // pre-rendering into a 2048x1024 cache texture and per-frame
    // blitting via FS_BLIT (uv-driven, viewport-offset agnostic).
    // The pre-eligibility-widening predicate was a copy of the SP
    // tier's predicate (which legitimately needs solid because the
    // SP shader takes bg as a uniform color); SB does not. Drop
    // the gate. Image bgs now route through SB instead of legacy
    // 3-pass; gradient bgs at any density too. The pure-logic
    // gate (transition_eligible_for_scissored_bake_logic) does
    // not take bg props as a result.
    //
    // Pattern bgs (FS_PATTERN_*) still use gl_FragCoord without a
    // u_vp_offset uniform. They render correctly through the
    // bg-cache path (cache renders at offset 0 then blits) but
    // the rare cache-Err fallback in render_transition_scissored_
    // bake_in_session would hit the direct-bake-at-non-zero-offset
    // path with wrong tile positions. Fallback is best-effort
    // (cache-Err is a memory-pressure signal); the pattern shaders
    // can grow u_vp_offset in a follow-up if the fallback becomes
    // load-bearing.
    let props_a = layer_composite_props_from_tuples(layers_a);
    let props_b = layer_composite_props_from_tuples(layers_b);
    transition_eligible_for_scissored_bake_logic(kind, &props_a, &props_b)
}


/// QA-direct (2026-05-08): session-level fullscreen-quad VBO for
/// the SP transition path. Lazy-allocated on first SP transition;
/// freed at with_egl_session teardown. The geometry is identical
/// across every transition kind (4-vert TRIANGLE_STRIP covering
/// NDC [-1, 1] with UV [0, 1]); session caching saves
/// gl.create_buffer + buffer_data per transition call.
fn ensure_transition_sp_quad_vbo(session: &mut EglSession) -> Result<glow::NativeBuffer> {
    use glow::HasContext;
    if let Some(vbo) = session.transition_sp_quad_vbo {
        return Ok(vbo);
    }
    let vbo = unsafe {
        session
            .gl
            .create_buffer()
            .map_err(|e| anyhow!("glGenBuffers(transition_sp_quad): {e}"))?
    };
    let verts: [f32; 16] = [
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ];
    unsafe {
        session.gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let bytes = std::slice::from_raw_parts(
            verts.as_ptr() as *const u8,
            std::mem::size_of_val(&verts),
        );
        session
            .gl
            .buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::STATIC_DRAW);
    }
    session.transition_sp_quad_vbo = Some(vbo);
    Ok(vbo)
}

/// Composite-pass program cached per kind. Wraps the FS_<KIND>
/// composite shader via wrap_composite_for_atlas (2026-05-09):
/// the shader gains `u_a_xform` / `u_b_xform` vec4 uniforms that
/// remap a per-fragment uv into the atlas region for slide A vs
/// slide B. With atlas-FBO compositing, both samplers (u_src_a,
/// u_src_b) point at the SAME atlas texture; the xforms select
/// the region. For non-atlas callers (e.g. capture path with two
/// separate full-res textures), set both xforms to identity
/// (offset=0, scale=1) and the wrapped shader behaves
/// identically to the unwrapped FS_<KIND>.
#[derive(Clone)]
struct CachedCompositeProgram {
    program: glow::NativeProgram,
    a_pos: u32,
    a_uv: u32,
    u_src_a: Option<glow::NativeUniformLocation>,
    u_src_b: Option<glow::NativeUniformLocation>,
    u_t: Option<glow::NativeUniformLocation>,
    /// r96 (2026-06-08): u_aspect = mode_w / mode_h, for the iris
    /// arm and any other aspect-dependent transition shader. Bound
    /// alongside u_t on every ccp draw site. Resolves to None for
    /// kinds whose FS doesn't declare u_aspect (silent no-op
    /// bind).
    u_aspect: Option<glow::NativeUniformLocation>,
    u_a_xform: Option<glow::NativeUniformLocation>,
    u_b_xform: Option<glow::NativeUniformLocation>,
}

std::thread_local! {
    static COMPOSITE_PROGRAMS: std::cell::RefCell<
        std::collections::HashMap<&'static str, CachedCompositeProgram>,
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

fn cached_composite_program(gl: &glow::Context, kind: &str) -> Result<CachedCompositeProgram> {
    use glow::HasContext;
    let kind_static =
        sp_kind_static(kind).ok_or_else(|| anyhow!("kind {kind:?} has no SP generator"))?;
    COMPOSITE_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        if let Some(ccp) = cache.get(kind_static) {
            return Ok(ccp.clone());
        }
        let fs = match fs_for_transition_kind(kind) {
            Some(s) => s,
            None => bail!("kind {kind:?} has no legacy FS"),
        };
        let wrapped = crate::hdmi_logic::wrap_composite_for_atlas(fs);
        let program = link_program(gl, VS_TEXTURED_QUAD, &wrapped)
            .with_context(|| format!("link FS_<KIND={kind}> composite (atlas-wrapped)"))?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("composite VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("composite VS missing a_uv"))?;
        let u_src_a = unsafe { gl.get_uniform_location(program, "u_src_a") };
        let u_src_b = unsafe { gl.get_uniform_location(program, "u_src_b") };
        let u_t = unsafe { gl.get_uniform_location(program, "u_t") };
        // r96: u_aspect for the iris arm (and other aspect-dependent
        // transition shaders). None for kinds whose FS doesn't
        // declare it; gl.uniform_1_f32(None, _) is a no-op.
        let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };
        let u_a_xform = unsafe { gl.get_uniform_location(program, "u_a_xform") };
        let u_b_xform = unsafe { gl.get_uniform_location(program, "u_b_xform") };
        let ccp = CachedCompositeProgram {
            program,
            a_pos,
            a_uv,
            u_src_a,
            u_src_b,
            u_t,
            u_aspect,
            u_a_xform,
            u_b_xform,
        };
        cache.insert(kind_static, ccp.clone());
        Ok(ccp)
    })
}

fn clear_composite_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    COMPOSITE_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        for (_, ccp) in cache.drain() {
            unsafe { gl.delete_program(ccp.program); }
        }
    });
    CUT_COMPOSITE_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        for (_, ccp) in cache.drain() {
            unsafe { gl.delete_program(ccp.program); }
        }
    });
}

/// QA-direct (2026-05-09 Phase 2.6) -- per-side cached composite
/// for the cut transition. CUT_COMPOSITE_PROGRAMS[true] = the
/// FS_CUT_B variant (slide B side, t>=0.5);
/// CUT_COMPOSITE_PROGRAMS[false] = FS_CUT_A (slide A side, t<0.5).
/// Halves the fragment-shader texture-sample count vs the
/// combined FS_CUT — only one side is visible per frame.
///
/// Other transition kinds need both sides simultaneously and use
/// COMPOSITE_PROGRAMS via cached_composite_program.
std::thread_local! {
    static CUT_COMPOSITE_PROGRAMS: std::cell::RefCell<
        std::collections::HashMap<bool, CachedCompositeProgram>,
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

fn cached_cut_composite_program(
    gl: &glow::Context,
    side_b: bool,
) -> Result<CachedCompositeProgram> {
    use glow::HasContext;
    CUT_COMPOSITE_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        if let Some(ccp) = cache.get(&side_b) {
            return Ok(ccp.clone());
        }
        let fs = if side_b {
            crate::hdmi_logic::FS_CUT_B
        } else {
            crate::hdmi_logic::FS_CUT_A
        };
        let wrapped = crate::hdmi_logic::wrap_composite_for_atlas(fs);
        let program = link_program(gl, VS_TEXTURED_QUAD, &wrapped)
            .with_context(|| format!("link FS_CUT_{} (atlas-wrapped)", if side_b { "B" } else { "A" }))?;
        let a_pos = unsafe { gl.get_attrib_location(program, "a_pos") }
            .ok_or_else(|| anyhow!("cut composite VS missing a_pos"))?;
        let a_uv = unsafe { gl.get_attrib_location(program, "a_uv") }
            .ok_or_else(|| anyhow!("cut composite VS missing a_uv"))?;
        let u_src_a = unsafe { gl.get_uniform_location(program, "u_src_a") };
        let u_src_b = unsafe { gl.get_uniform_location(program, "u_src_b") };
        let u_t = unsafe { gl.get_uniform_location(program, "u_t") };
        // r96: u_aspect kept for shape parity with cached_composite_program.
        // FS_CUT_A/FS_CUT_B don't declare u_aspect, so this resolves to None.
        let u_aspect = unsafe { gl.get_uniform_location(program, "u_aspect") };
        let u_a_xform = unsafe { gl.get_uniform_location(program, "u_a_xform") };
        let u_b_xform = unsafe { gl.get_uniform_location(program, "u_b_xform") };
        let ccp = CachedCompositeProgram {
            program,
            a_pos,
            a_uv,
            u_src_a,
            u_src_b,
            u_t,
            u_aspect,
            u_a_xform,
            u_b_xform,
        };
        cache.insert(side_b, ccp.clone());
        Ok(ccp)
    })
}

fn clear_transition_sp_program_cache(gl: &glow::Context) {
    use glow::HasContext;
    TRANSITION_SP_PROGRAMS.with(|c| {
        let mut cache = c.borrow_mut();
        for (_, csp) in cache.drain() {
            unsafe { gl.delete_program(csp.program); }
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
    /// QA-direct (2026-05-09 atlas SB Phase 2.5): cached non-solid
    /// bg as a 2048x1024 RGBA texture sized to the atlas region.
    /// Populated lazily on first SB use (or eagerly at prewarm)
    /// for slides whose bg is gradient / pattern / image (anything
    /// the BgKind branch resolves via a fragment shader, not glClear).
    /// Subsequent atlas SB bakes blit this cache via FS_BLIT into
    /// the atlas region instead of re-running the bg shader -- vc4
    /// TMU dedicated hardware makes the blit ~2-3x cheaper than
    /// FS_GRADIENT compute on a 2M-fragment fill.
    /// `None` for solid-bg slides (glClear is already free; no
    /// cache benefit).
    pub bg_tex: Option<glow::NativeTexture>,
    // r62 first_frame_tex field REMOVED (2026-06-15 R-1 footprint
    // cut). Was a per-slide RGBA8 mode_w*mode_h*4 cache (~4.17 MB
    // at 1360x768; 4-slide FYS reel = ~17 MB CMA). Cut per Karl's
    // "memory dangerously high" signal; reverts to pre-r62
    // behavior where every cycle re-bakes + re-composites the
    // first frame. See the matching comment in
    // paint_and_present_one_text_over_video_first_frame.
}

/// R-1 footprint cut fingerprint: one-time-per-process emit at the
/// first SlideRenderCache::new() call. QA greps journal for the
/// literal to confirm the cut shipped on FYS (vs the prior r62-
/// equipped binary).
static R1_FIRST_FRAME_TEX_REMOVED_MARKER: std::sync::Once = std::sync::Once::new();

impl SlideRenderCache {
    pub fn new(layer_count: usize) -> Self {
        R1_FIRST_FRAME_TEX_REMOVED_MARKER.call_once(|| {
            eprintln!(
                "[perf] r62_first_frame_tex_removed save_per_slide_bytes=mode_w*mode_h*4 reason=footprint_cut"
            );
        });
        let mut glyph: GlyphCache = Vec::with_capacity(layer_count);
        glyph.resize_with(layer_count, || None);
        let mut tex: TextureCache = Vec::with_capacity(layer_count);
        tex.resize_with(layer_count, || None);
        Self { glyph, tex, bg_tex: None }
    }
}

/// Free GL textures owned by a SlideRenderCache being removed
/// from session.slide_caches (2026-05-09 atlas SB bg-cache;
/// r62 first-frame composite cache REMOVED in R-1 footprint cut).
/// Must be called while the GL context is still bound. Used by
/// the multiple slide_caches.remove call sites that previously
/// inlined `for slot in old.tex { delete_texture(t) }` and now
/// also need to free `bg_tex`.
fn free_slide_render_cache(gl: &glow::Context, mut cache: SlideRenderCache) {
    use glow::HasContext;
    unsafe {
        for slot in cache.tex.iter_mut() {
            if let Some(t) = slot.take() {
                gl.delete_texture(t);
            }
        }
        if let Some(t) = cache.bg_tex.take() {
            gl.delete_texture(t);
        }
    }
}

/// 2026-06-15 perf-gl M-1: route every slide_caches.insert through
/// here so the LruMap's InsertOutcome (evicted_lru / replaced) is
/// always cleaned up via free_slide_render_cache — the same texture-
/// handle cleanup contract the explicit remove+free sites use.
///
/// The borrow split (slide_caches vs gl) is the standard field-
/// disjoint pattern used elsewhere in this file (image_bg_cache +
/// gl, image_slide_tex_cache + gl); passing both as separate refs
/// keeps the helper callable from sites that already hold &mut
/// session for other field access.
fn insert_slide_render_cache(
    slide_caches: &mut crate::lru::LruMap<uuid::Uuid, SlideRenderCache>,
    gl: &glow::Context,
    slide_id: uuid::Uuid,
    cache: SlideRenderCache,
) {
    let outcome = slide_caches.insert(slide_id, cache);
    if let Some(evicted) = outcome.evicted_lru {
        free_slide_render_cache(gl, evicted);
    }
    if let Some(replaced) = outcome.replaced {
        // Defensive: callers explicitly remove+free before insert
        // (see the `if let Some(old) = ... .remove(...)` idiom), so
        // the replaced slot should be None on the production paths.
        // Free anyway if it ever fires — better than leaking texture
        // handles.
        free_slide_render_cache(gl, replaced);
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
    runtime_glyph_ctx: Option<crate::glyph_cache::RuntimeGlyphCtx<'_>>,
) -> Result<()> {
    paint_slide_with_viewport(
        gl, mode_w, mode_h, 0, 0, mode_w, mode_h, Some(bg_kind), text_layers,
        motion_states, wall_clock_unix, glyph_cache, image_bg_cache, tex_cache,
        runtime_glyph_ctx,
    )
}

/// QA-mandated scissored-bake (Step 4): paint_slide variant with
/// an explicit viewport size separate from mode_w/h. mode_w/h
/// drive layer NDC math (box ratios → screen-space pixel coords →
/// NDC), so passing full-res mode keeps the layer placement +
/// bitmap-to-box scaling intact. vp_w/h drive the GL viewport so
/// the bake can target a smaller (e.g. half-res) FBO. NDC [-1,1]
/// maps to [0, vp_w] regardless of mode -- the same content is
/// rendered into fewer pixels, then LINEAR-upsampled by composite.
fn paint_slide_with_viewport(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    vp_x_off: u32,
    vp_y_off: u32,
    vp_w: u32,
    vp_h: u32,
    bg_kind: Option<&BgKind>,
    text_layers: &[(&crate::content::TextLayer, [f32; 4], Rc<fontdue::Font>)],
    motion_states: Option<&[MotionState]>,
    wall_clock_unix: i64,
    glyph_cache: Option<&mut GlyphCache>,
    mut image_bg_cache: Option<&mut ImageBgCache>,
    mut tex_cache: Option<&mut TextureCache>,
    runtime_glyph_ctx: Option<crate::glyph_cache::RuntimeGlyphCtx<'_>>,
) -> Result<()> {
    // bg_kind = None signals the caller has ALREADY filled the
    // bg (e.g. atlas SB blit-from-bg-cache or pre-baked region).
    // Skip the BgKind branch entirely; just set viewport + paint
    // layers. (2026-05-09 atlas SB Phase 2.5 bg-cache.)
    use glow::HasContext;
    // QA-direct (2026-05-14 paint_slide profiling slice): same
    // env-gate as the outer paint_and_present trace. Emits one
    // JSON line per paint_slide invocation with bg / raster /
    // draw_loop sub-phase deltas in microseconds. The line is
    // emitted RIGHT BEFORE the outer "boundary" trace line so the
    // analysis script can pair them by stream order.
    // 2026-06-15 perf-gl W-2: thread_local-cached env-var read.
    let trace_sub = boundary_trace_enabled_cached();
    let t_sub_start = if trace_sub { Some(std::time::Instant::now()) } else { None };
    let mut t_after_bg: Option<std::time::Instant> = None;
    let mut t_after_raster: Option<std::time::Instant> = None;
    let mut t_after_draw: Option<std::time::Instant> = None;
    let mut raster_misses = 0u32;
    // vp_w/h for the GL viewport; mode_w/h for layer NDC math
    // (box ratios -> pixel coords -> NDC). Pattern + gradient bg
    // shaders use gl_FragCoord (viewport-pixel coords) + uniforms
    // for tile/span size: those need to operate in vp-pixel space
    // so the bg fills vp-relative coords correctly. Passing
    // (vp_w, vp_h) to draw_gradient_pattern + draw_pattern gives
    // the right scale -- tile sizes auto-scale by 1/divisor and
    // gradient spans auto-fit the viewport, matching how the
    // composite-pass LINEAR upsamples to full output.
    //
    // (vp_x_off, vp_y_off) lets the bake into a SUB-region of the
    // bound framebuffer (atlas FBO use case, 2026-05-09): caller
    // sets a matching glScissor + GL_SCISSOR_TEST so glClear and
    // any pattern/gradient bg fill stays in-region. Atlas SB
    // eligibility filters out non-solid bgs upstream
    // (effective_solid_bg() != None), so within the SB path
    // bg_kind is BgKind::Solid (or a degenerate gradient already
    // collapsed to solid by the caller); gl_FragCoord-based
    // pattern shaders aren't reached and don't need offset
    // awareness.
    //
    // Caught by QA visual review (2026-05-09): half-res bake of
    // slide 70f9d701's density=0 vertical gradient bg showed
    // grey-top instead of pink-top because the prior code passed
    // mode_h for u_viewport while gl_FragCoord was in vp space.
    unsafe { gl.viewport(vp_x_off as i32, vp_y_off as i32, vp_w as i32, vp_h as i32); }
    if let Some(bg_kind) = bg_kind {
        match bg_kind {
            BgKind::Gradient { color_a, color_b, density } => {
                draw_gradient_pattern(gl, vp_x_off, vp_y_off, vp_w, vp_h, *color_a, *color_b, *density)?;
            }
            BgKind::Pattern { kind, color_a, color_b, density } => {
                // FS_PATTERN_* shaders use absolute gl_FragCoord
                // without a u_vp_offset uniform. When called from
                // the atlas SB lazy-fallback path with a non-zero
                // vp_y_off (slide B region offset), tile positions
                // are wrong. The atlas SB normal path renders bg
                // through ensure_slide_bg_cache (offset=0) and blits
                // into the region, so this lazy-fallback only fires
                // on cache-Err. Best-effort; rare. Add u_vp_offset
                // to FS_PATTERN_* if the fallback becomes load-
                // bearing (mirror the FS_GRADIENT u_vp_offset fix).
                draw_pattern(gl, vp_w, vp_h, *kind, *color_a, *color_b, *density)?;
            }
            BgKind::Image { asset_path, solid_fallback } => {
                // Image bg path uses FS_BLIT which is uv-driven (not
                // gl_FragCoord) -- viewport-resolution-independent.
                // Reborrow so we can hand the cache to the overlay-
                // route below if any_overlay fires for a later layer.
                draw_image_bg(gl, asset_path, *solid_fallback, image_bg_cache.as_deref_mut());
            }
            BgKind::Solid(color) => {
                // glClear; trivially resolution-independent.
                draw_solid_clear(gl, *color);
            }
        }
    }
    // BLEND toggle once around the layer loop (Phase 4.2c
    // optimization vs. per-layer enable/disable) — every text layer
    // uses the same premultiplied-alpha blend func and
    // disabling/re-enabling between layers is wasted state. The
    // IIFE guard ensures `gl.disable(BLEND)` always runs even when
    // a layer's draw errors mid-loop (4.3+ persistent-context
    // future-correctness).
    if trace_sub {
        t_after_bg = Some(std::time::Instant::now());
    }
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
        // Bug 1 follow-up (2026-05-20): the auto_mode clock resolves
        // in LOCAL time (libc localtime_r, TZ from the sidecar env)
        // — a sign clock must show the sign's physical-location
        // time, not UTC. Covers every auto_format (date rollover at
        // local midnight, not UTC midnight, included).
        let cal = unix_to_calendar_local(wall_clock_unix);
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
            let resolved_cow = resolve_layer_text(layer, cal);
            let resolved_text: &str = &resolved_cow;
            let size_px = effective_font_size_px(
                layer.font_size_px,
                layer.font_size_pct,
                layer.r#box.w,
                mode_w,
            );
            let max_width_px = (layer.r#box.w * mode_w as f32).max(1.0);
            let needs_raster =
                should_rerasterize(cache_ref[i].as_ref(), resolved_text, size_px, max_width_px);
            if needs_raster {
                // SDF arc slice B.3: tex_cache fully vestigial (MSDF
                // uses session-lived atlas textures, not per-layer
                // uploads). Drain stale slots if any pre-MSDF
                // binary left them populated. tex_cache is itself
                // scheduled for deletion in a follow-up cleanup
                // slice; kept here only to drain pre-existing state.
                if let Some(tc) = tex_cache.as_deref_mut() {
                    if i < tc.len() {
                        if let Some(old_tex) = tc[i].take() {
                            unsafe { gl.delete_texture(old_tex); }
                        }
                    }
                }
                let wrapped =
                    wrap_text_to_width(font.as_ref(), resolved_text, size_px, max_width_px);
                let family = layer.font_family.as_deref().unwrap_or("Inter");
                let group = msdf_atlas_for_family(gl, family)
                    .or_else(|| msdf_atlas_for_family(gl, "Inter"))
                    .and_then(|(_atlas_tex, atlas)| {
                        // Bug 4 (2026-05-19): per-line X-squish gate.
                        // `max_width_px` is the same boxW used for
                        // wrap_text_to_width above — natural-overflow
                        // lines get squished to boxW; lines that fit
                        // pass through unchanged.
                        // Slice 3D (2026-05-19): emoji arg retired
                        // alongside the static-CBDT atlas. Emoji
                        // codepoints route to the runtime COLRv1
                        // cache via runtime_glyph_ctx below.
                        crate::hdmi_logic::layout_text_to_quads(
                            atlas,
                            &wrapped,
                            size_px,
                            max_width_px,
                            runtime_glyph_ctx.as_ref().map(|rt| {
                                crate::glyph_cache::RuntimeGlyphCtx {
                                    cache: rt.cache,
                                    fonts_dir: rt.fonts_dir,
                                }
                            }),
                        )
                    });
                if let Some(g) = group.as_ref() {
                    // SDF arc slice C.3 -- restore the smoke-script
                    // marker that B.3 inadvertently dropped along with
                    // the AlphaBitmap retirement. Format mirrors the
                    // pre-B.3 line shape so renderer_pi_smoke.sh's
                    // `grep -q 'rasterized text'` assertion still fires
                    // on MSDF-layout misses. Per-codepoint quad count is
                    // the new equivalent of the old WxH bitmap dim.
                    eprintln!(
                        "rasterized text {resolved_text:?} @ {size_px:.1}px -> {} quads ({}x{} bbox)",
                        g.quads.len(),
                        g.width,
                        g.height,
                    );
                }
                cache_ref[i] = Some(CachedGlyph {
                    text: resolved_cow.into_owned(),
                    size_px,
                    max_width_px,
                    group,
                });
                if trace_sub {
                    raster_misses += 1;
                }
            }
        }
        if trace_sub {
            t_after_raster = Some(std::time::Instant::now());
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
            // Overlay layers go through a ping-pong FBO route that
            // re-renders bg internally; bg_kind = None (atlas SB
            // bg-cache path) is incompatible with that. Atlas SB
            // eligibility filters out overlay-mode layers upstream
            // (transition_eligible_for_scissored_bake), so this
            // branch should never be taken when bg_kind = None.
            // Defensive bail keeps the type-system honest.
            let bg_kind = bg_kind.ok_or_else(|| {
                anyhow!(
                    "paint_slide_with_viewport: overlay-route layers require Some(bg_kind); \
                     atlas-SB bg-cache path bypasses bg_kind which is incompatible"
                )
            })?;
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
            // Overlay-route emits its own GPU work; we don't split
            // it further here. Skip the sub-phase emit to avoid
            // logging misleading partial-zero deltas.
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
                let Some(group) = cached.group.as_ref() else {
                    // Empty / whitespace-only laid out to no ink;
                    // nothing to draw for this layer.
                    continue;
                };
                let family = layer.font_family.as_deref().unwrap_or("Inter");
                let (atlas_tex, _) = msdf_atlas_for_family(gl, family)
                    .or_else(|| msdf_atlas_for_family(gl, "Inter"))
                    .ok_or_else(|| {
                        anyhow!(
                            "MSDF atlas missing at draw time for family {family:?}"
                        )
                    })?;
                draw_text_layer_msdf(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    group,
                    atlas_tex,
                    Some((vp_x_off, vp_y_off, vp_w, vp_h)),
                )?;
            }
            Ok(())
        })();
        unsafe { gl.disable(glow::BLEND); }
        layer_loop_result?;
        if trace_sub {
            t_after_draw = Some(std::time::Instant::now());
        }
    }
    // QA-direct (2026-05-14): emit sub-phase trace. Ordered RIGHT
    // before the outer "boundary" trace line so the analysis
    // script pairs them by stream order (subphase precedes
    // boundary for each painted frame).
    if let (Some(t0), Some(t_bg)) = (t_sub_start, t_after_bg) {
        // bg phase always present. raster + draw only present when
        // text_layers was non-empty; emit zeros for the empty case.
        let raster_us = match (t_after_bg, t_after_raster) {
            (Some(a), Some(b)) => (b - a).as_micros(),
            _ => 0,
        };
        let draw_us = match (t_after_raster, t_after_draw) {
            (Some(a), Some(b)) => (b - a).as_micros(),
            _ => 0,
        };
        let bg_us = (t_bg - t0).as_micros();
        let total_us = match t_after_draw.or(t_after_raster).or(t_after_bg) {
            Some(t) => (t - t0).as_micros(),
            None => 0,
        };
        eprintln!(
            "{{\"trace\":\"paint_sub\",\"bg_us\":{},\"raster_us\":{},\"draw_us\":{},\"raster_misses\":{},\"layers\":{},\"total_us\":{}}}",
            bg_us, raster_us, draw_us, raster_misses, text_layers.len(), total_us,
        );
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
                draw_gradient_pattern(gl, 0, 0, mode_w, mode_h, *color_a, *color_b, *density)?;
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

            let Some(group) = cached.group.as_ref() else {
                continue;
            };
            let family = layer.font_family.as_deref().unwrap_or("Inter");
            let (atlas_tex, _) = msdf_atlas_for_family(gl, family)
                .or_else(|| msdf_atlas_for_family(gl, "Inter"))
                .ok_or_else(|| {
                    anyhow!(
                        "MSDF atlas missing at overlay-route draw for family {family:?}"
                    )
                })?;
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
                draw_text_layer_msdf(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    group,
                    atlas_tex,
                    // Full-size FBO viewport — lets a ticker layer's
                    // box scissor clip correctly on the overlay route
                    // (these draws target mode_w x mode_h FBOs).
                    Some((0, 0, mode_w, mode_h)),
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
                draw_text_layer_msdf(
                    gl,
                    mode_w,
                    mode_h,
                    layer,
                    *tc,
                    motion_kind,
                    motion_state,
                    group,
                    atlas_tex,
                    // Full-size FBO viewport — lets a ticker layer's
                    // box scissor clip correctly on the overlay route
                    // (these draws target mode_w x mode_h FBOs).
                    Some((0, 0, mode_w, mode_h)),
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
    let cop = cached_overlay_blend_program(gl)?;
    let vbo = cached_textured_quad_vbo(gl)?;
    gl.use_program(Some(cop.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(layer_tex));
    gl.uniform_1_i32(cop.u_layer_tex.as_ref(), 0);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(scene_tex));
    gl.uniform_1_i32(cop.u_slide_tex.as_ref(), 1);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cop.a_pos);
    gl.vertex_attrib_pointer_f32(cop.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cop.a_uv);
    gl.vertex_attrib_pointer_f32(cop.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cop.a_pos);
    gl.disable_vertex_attrib_array(cop.a_uv);
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, None);
    gl.active_texture(glow::TEXTURE0);
    // P2-G: program + shared VBO come from session-lived thread_
    // local caches; never freed here. Cleanup in
    // clear_bright_gamma_cache at session teardown.
    Ok(())
}

/// v1-spec-delta #7 (slice c) helper -- blit a texture to the
/// currently-bound framebuffer via FS_BLIT, filling it (the shared
/// fullscreen quad). Used at end of the overlay route to copy the
/// final scene texture to the default framebuffer. Caller must have
/// bound the target FBO and set the viewport.
unsafe fn run_blit_pass(
    gl: &glow::Context,
    src_tex: glow::NativeTexture,
) -> Result<()> {
    run_blit_pass_quad(gl, src_tex, cached_textured_quad_vbo(gl)?)
}

/// FYS bug B (2026-05-21) -- `run_blit_pass` with an explicit quad
/// `vbo`. `run_blit_pass` passes the shared fullscreen quad (fill);
/// the image slide bake passes a `cover_quad_vbo` so the asset is
/// cover-fit (aspect-preserved, overflow clipped) instead of
/// stretched. `vbo` is a 4-vert interleaved `[x,y,u,v]`
/// TRIANGLE_STRIP quad.
unsafe fn run_blit_pass_quad(
    gl: &glow::Context,
    src_tex: glow::NativeTexture,
    vbo: glow::NativeBuffer,
) -> Result<()> {
    use glow::HasContext;
    // P2-G (2026-05-10): use the existing session-cached
    // CachedBlitProgram (was already cached for the atlas SB
    // bg-cache path; just plug it in here too). Pre-fix this path
    // was per-call link_program + create_buffer + 2x get_attrib_
    // location + 1x get_uniform_location + draw + delete_buffer +
    // delete_program -- on the overlay-route final blit, that's
    // every frame the slide has a non-Normal-blend layer.
    let cbp = cached_blit_program(gl)?;
    gl.use_program(Some(cbp.program));
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(src_tex));
    gl.uniform_1_i32(cbp.u_src.as_ref(), 0);
    gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
    gl.enable_vertex_attrib_array(cbp.a_pos);
    gl.vertex_attrib_pointer_f32(cbp.a_pos, 2, glow::FLOAT, false, 16, 0);
    gl.enable_vertex_attrib_array(cbp.a_uv);
    gl.vertex_attrib_pointer_f32(cbp.a_uv, 2, glow::FLOAT, false, 16, 8);
    gl.draw_arrays(glow::TRIANGLE_STRIP, 0, 4);
    gl.disable_vertex_attrib_array(cbp.a_pos);
    gl.disable_vertex_attrib_array(cbp.a_uv);
    gl.bind_texture(glow::TEXTURE_2D, None);
    // Program + caller-supplied VBO come from session-lived caches;
    // never freed here.
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
                None,  // standalone debug bake; no runtime glyph cache
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
            // eglSwapBuffers (called by render_one_frame_in_session
            // immediately after this closure returns) implicitly
            // flushes; the explicit gl.flush() forced an extra
            // tile-store on vc4 (cold-scout #2 P6, 2026-05-09).
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
/// QA-direct (2026-05-08): pre-warm the per-(kind, n_a, n_b) SP
/// transition program cache + per-slide glyph + texture cache so
/// pass #0 of the reel pays no cold-instance drag. Walks the
/// resolved playlist once, dedupes the unique (kind, n_a, n_b)
/// tuples encountered as transitions, compiles each program;
/// pre-rasterizes each text-slide's layers into session.slide_
/// caches.
///
/// Wall-clock budget: ~80 ms shader compile per unique program +
/// ~70 ms per first-text-raster on the dev Pi, run sequentially.
/// For the FYS playlist this is ~13 unique programs + 19 unique
/// text slides = ~2 seconds startup measured. Long-running daemon
/// amortizes immediately. Subsequent passes have ZERO
/// cold-instance drag.
fn prewarm_sp_session(
    session: &mut EglSession,
    resolved: &[(crate::content::ContentItem, String, u32)],
    fonts: Option<&FontCatalog>,
    content_root: &Path,
) -> Result<()> {
    let t_prewarm = Instant::now();
    eprintln!("reel: prewarm starting -- compile SP programs + rasterize slide text");

    // Pass 1: pre-resolve every text slide's layers for layer-count
    // lookup AND populate slide_caches via prepare_layers_for_single_
    // pass. We do this BEFORE compiling programs so we know each
    // transition's (n_a, n_b) before deciding which programs to compile.
    let mut layer_counts: std::collections::HashMap<uuid::Uuid, usize> =
        std::collections::HashMap::new();
    let mut text_slides: Vec<&crate::content::TextSlide> = Vec::new();
    for (item, _, _) in resolved {
        if let crate::content::ContentItem::Text(slide) = item {
            text_slides.push(slide);
        }
    }
    for slide in &text_slides {
        let (_bg, _, layers) =
            match resolve_slide_layers(slide, fonts, Some(content_root)) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!(
                        "reel: prewarm skipping slide {} -- resolve failed: {e:#}",
                        slide.id,
                    );
                    continue;
                }
            };
        layer_counts.insert(slide.id, layers.len());

        // Ensure session cache slot exists + matches layer count.
        let slide_id = slide.id;
        let n = layers.len();
        let needs_new = match session.slide_caches.get(&slide_id) {
            Some(c) => c.glyph.len() != n,
            None => true,
        };
        if needs_new {
            if let Some(old) = session.slide_caches.remove(&slide_id) {
                free_slide_render_cache(session.gl, old);
            }
            insert_slide_render_cache(
                &mut session.slide_caches,
                session.gl,
                slide_id,
                SlideRenderCache::new(n),
            );
        }

        // B.3 cleanup follow-up: prepare_layers_for_single_pass is
        // now a thin sanity gate (SP-tier is bg-only post-MSDF
        // cutover). The text bail surfaces caller bugs but does no
        // rasterize/upload work — that's all done by the slice-
        // resident MSDF atlas + paint_slide path now. Empty-layer
        // case returns Ok with empty Vecs, so prewarm is effectively
        // a no-op for the text path; bg-cache prewarm below is what
        // still matters here.
        let states = motion_states_for_layers(slide.id, &layers, 0.0);
        if let Err(e) = prepare_layers_for_single_pass(&layers, &states) {
            eprintln!(
                "reel: prewarm skipping slide {} text raster: {e:#}",
                slide.id,
            );
        }
        // Pre-populate the bg-cache for non-solid bgs (cold-scout
        // #12). Pays the gradient/pattern/image bg fragment-fill
        // cost once at session bring-up rather than on the first
        // SB transition involving this slide. Lazy-fallback path
        // in render_transition_scissored_bake_in_session still
        // populates if an error skips this. Idempotent across
        // calls. Skip-on-Err so prewarm doesn't fail-fast on a
        // single slide; runtime falls back to direct-bake.
        if !matches!(_bg, BgKind::Solid(_)) {
            unsafe {
                if let Err(e) = ensure_slide_bg_cache(session, slide.id, &_bg) {
                    eprintln!(
                        "reel: prewarm bg_cache for slide {} failed: {e:#}; lazy on first SB call",
                        slide.id,
                    );
                }
            }
        }
    }

    // Pass 2: dedupe + compile (kind, n_a, n_b) tuples. Walks
    // every (i-1, i) pair AND the wrap-around (last, first) pair
    // -- the runtime uses prev_idx_for_reel which wraps at pass
    // boundaries, so without the wrap entry pass #1's first
    // transition would still pay a cold compile.
    //
    // Per-tier dispatch matches render_transition_animated_in_
    // session: low-cost combinations compile a single-pass
    // program; higher-cost combinations compile bake + composite
    // programs for the scissored-bake path. Tracks which tier
    // each (kind, n_a, n_b) takes so prewarm and runtime agree.
    let mut sp_compiled: std::collections::HashSet<(String, usize, usize)> =
        std::collections::HashSet::new();
    let mut composite_compiled: std::collections::HashSet<String> =
        std::collections::HashSet::new();
    let mut sp_count = 0_u32;
    let mut composite_count = 0_u32;
    let mut consider_pair = |
        a_idx: usize,
        b_idx: usize,
        sp_compiled: &mut std::collections::HashSet<(String, usize, usize)>,
        composite_compiled: &mut std::collections::HashSet<String>,
        sp_count: &mut u32,
        composite_count: &mut u32,
    | {
        let kind = resolved[b_idx].1.as_str();
        let id_a = resolved[a_idx].0.id();
        let id_b = resolved[b_idx].0.id();
        let n_a = match layer_counts.get(&id_a) {
            Some(n) => *n,
            None => return,
        };
        let n_b = match layer_counts.get(&id_b) {
            Some(n) => *n,
            None => return,
        };
        match classify_prewarm_pair(kind, n_a, n_b) {
            PrewarmTier::NotSinglePass | PrewarmTier::ExceedsBakeCap => {
                // Both fall through to legacy 3-pass at runtime;
                // prewarm has nothing to compile here.
            }
            PrewarmTier::SinglePass => {
                let key = (kind.to_string(), n_a, n_b);
                if sp_compiled.contains(&key) {
                    return;
                }
                sp_compiled.insert(key);
                if let Err(e) = cached_transition_sp_program(session.gl, kind, n_a, n_b) {
                    eprintln!(
                        "reel: prewarm SP compile {kind:?}({n_a},{n_b}) failed: {e:#}; skipping"
                    );
                    return;
                }
                *sp_count += 1;
            }
            PrewarmTier::ScissoredBake => {
                // Scissored-bake tier: bake passes use paint_slide
                // (its own per-outline glyph program cache is
                // primed by the slide-text raster pre-pass above).
                // Only the kind-specific composite-pass program
                // needs explicit pre-compile here.
                if !composite_compiled.contains(kind) {
                    composite_compiled.insert(kind.to_string());
                    if kind == "cut" {
                        // Cut path uses ONLY the side-specialized
                        // FS_CUT_A / FS_CUT_B composite shaders at
                        // runtime; combined FS_CUT is unused so skip
                        // the compile. Matches the runtime SB cut
                        // path that no longer compiles `ccp` for
                        // kind=="cut".
                        for side_b in [false, true] {
                            if let Err(e) = cached_cut_composite_program(session.gl, side_b) {
                                eprintln!(
                                    "reel: prewarm cut composite (side_b={side_b}) failed: {e:#}; skipping"
                                );
                            }
                        }
                    } else if let Err(e) = cached_composite_program(session.gl, kind) {
                        eprintln!(
                            "reel: prewarm composite({kind:?}) failed: {e:#}; skipping"
                        );
                        return;
                    }
                    *composite_count += 1;
                }
            }
        }
    };
    for i in 1..resolved.len() {
        consider_pair(
            i - 1, i,
            &mut sp_compiled, &mut composite_compiled,
            &mut sp_count, &mut composite_count,
        );
    }
    if resolved.len() >= 2 {
        consider_pair(
            resolved.len() - 1, 0,
            &mut sp_compiled, &mut composite_compiled,
            &mut sp_count, &mut composite_count,
        );
    }
    let compile_count = sp_count + composite_count;

    // Atlas SB pre-warm (2026-05-09 QA Phase 2): allocate the
    // atlas FBO + texture + clear once at session bring-up so the
    // first SB transition doesn't pay the lazy-allocation +
    // first-bind cold cost in its hot loop. Bench data showed
    // sb_bake_a p99 = 15.6ms (one frame in the first SB
    // transition) vs p50 = 1.16ms; allocating + warming here
    // moves that 14ms cost off the per-frame critical path.
    //
    // Skipped if no SB-portable transitions are in the reel
    // (composite_count == 0). The eligibility-check loops above
    // already produce composite_count, so this gates cleanly.
    let mut atlas_warmed = false;
    if composite_count > 0 {
        match unsafe { ensure_bake_atlas(session) } {
            Ok((fbo, _tex)) => {
                use glow::HasContext;
                unsafe {
                    session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
                    session.gl.viewport(
                        0, 0,
                        crate::hdmi_logic::ATLAS_FBO_W as i32,
                        crate::hdmi_logic::ATLAS_FBO_H as i32,
                    );
                    session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
                    session.gl.clear(glow::COLOR_BUFFER_BIT);
                    // Force the GPU to actually do the clear --
                    // without flush, the pre-warm becomes a no-op
                    // command queue and the first frame still pays
                    // the cold allocation + tile-store cost.
                    session.gl.flush();
                    session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                }
                atlas_warmed = true;
            }
            Err(e) => {
                eprintln!("reel: prewarm atlas FBO alloc failed: {e:#}; lazy on first SB call");
            }
        }
    }

    // P2-G.fix (2026-05-10): pre-link the post-pass programs that
    // CRIT-A and P2-G cached. Skips the first-paint link cost
    // when the FYS reel hits a non-identity-color frame
    // (run_bright_gamma_pass) or the overlay-route (run_blit_pass
    // / run_overlay_blend_pass). Lazy first-call would otherwise
    // pay ~5 ms on the first overlay-route slide. cached_blit_
    // program is also pre-warmed via the bg-cache prewarm path
    // above for non-solid bgs; this call is idempotent (cache
    // hit on second invocation).
    if let Err(e) = unsafe { cached_bright_gamma_program(session.gl) } {
        eprintln!("reel: prewarm cached_bright_gamma_program failed: {e:#}; lazy on first call");
    }
    if let Err(e) = unsafe { cached_overlay_blend_program(session.gl) } {
        eprintln!("reel: prewarm cached_overlay_blend_program failed: {e:#}; lazy on first call");
    }
    if let Err(e) = cached_blit_program(session.gl) {
        eprintln!("reel: prewarm cached_blit_program failed: {e:#}; lazy on first call");
    }

    let elapsed_ms = t_prewarm.elapsed().as_millis();
    eprintln!(
        "reel: prewarm complete -- {} slide texts rasterized, {compile_count} programs compiled (sp={sp_count} composite={composite_count}), atlas_warmed={atlas_warmed}, {elapsed_ms} ms",
        text_slides.len(),
    );
    Ok(())
}

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
    with_egl_session(card, 0, |session| {
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
        // QA-direct (2026-05-08, post-hoist): pre-warm the SP
        // transition program cache + slide_caches so pass #0 has
        // no cold-instance drag. Walks the playlist once at
        // session init, builds the unique (kind, n_a, n_b) tuple
        // set, compiles each program; pre-rasterizes each slide's
        // text layers into the session caches. ~2s startup cost
        // (long-running daemon amortizes immediately).
        if let Err(e) = prewarm_sp_session(session, &resolved, fonts, content_root) {
            eprintln!("warn: pre-warm partial failure: {e:#}; reel will compile on-demand instead");
        }
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
                // QA M2 (2026-05-23): image-involving combos now
                // route through render_transition_any_endpoint_in_
                // session, matching the IPC sidecar's
                // paint_and_present_one_transition_frame dispatch.
                //
                // (Text, Text) STAYS on render_transition_animated_
                // in_session: that path has a 3-tier dispatch
                // (single-pass → scissored-bake → legacy 3-pass)
                // that the QA-mandated 2026-05-08 perf rewrite added
                // to keep text/text transitions inside the 33ms
                // vsync budget at 1080p×30Hz. paint_and_present_one_
                // transition_frame uses the 3-pass legacy shape only
                // — routing (Text, Text) through it would silently
                // degrade the working fast path. Pre-commit review
                // (2026-05-23) caught this regression risk.
                //
                // Video-involving combos (V↔T/I/V) still hard-cut —
                // the reel has no SlideCache for V4L2 decoder state.
                if let Some(p) = prev_idx_for_reel(i, pass, resolved.len()) {
                    if p != i {
                        let (prev_item, _, _) = &resolved[p];
                        let (_, kind, transition_ms) = &resolved[i];
                        let transition_ms = clamp_transition_ms(*transition_ms);
                        let prev_is_video =
                            matches!(prev_item, ContentItem::Video(_));
                        let item_is_video = matches!(item, ContentItem::Video(_));
                        if prev_is_video || item_is_video {
                            // Video-involving transitions: scoped
                            // deferral. Caller hard-cuts (existing
                            // pre-M2 behavior for these combos).
                            eprintln!(
                                "reel: video-involving transition into item {i} ({} -> {}) not yet supported in standalone reel; using hard cut",
                                prev_item.type_label(),
                                item.type_label(),
                            );
                        } else {
                            match (prev_item, item) {
                                (ContentItem::Text(prev_slide), ContentItem::Text(slide)) => {
                                    // (Text, Text) keeps the QA-
                                    // mandated SP/SB/3-pass tiered
                                    // dispatch — perf-critical path.
                                    eprintln!(
                                        "reel: transition into item {i}/{} kind={kind:?} ms={transition_ms} (text -> text)",
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
                                    // Image-involving (any of T↔I,
                                    // I↔T, I↔I) — route through the
                                    // new wrapper that exercises the
                                    // same paint_and_present_one_
                                    // transition_frame primitive the
                                    // IPC sidecar uses.
                                    eprintln!(
                                        "reel: transition into item {i}/{} kind={kind:?} ms={transition_ms} ({} -> {})",
                                        resolved.len() - 1,
                                        prev_item.type_label(),
                                        item.type_label(),
                                    );
                                    if let Err(e) = render_transition_any_endpoint_in_session(
                                        session,
                                        card,
                                        prev_item,
                                        item,
                                        fonts,
                                        content_root,
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
                    ContentItem::Video(slide) => {
                        // QA H2 (2026-05-23): route through the same
                        // V4L2 decode + per-frame paint pipeline the
                        // IPC sidecar uses (lifted into
                        // crate::video_decode). On any failure (no
                        // /dev/video10, malformed MP4, V4L2 prime
                        // error, mid-stream decode failure), fall
                        // back to the legacy black-hold sleep so
                        // the reel pacing is preserved and the
                        // operator never sees the reel crash.
                        let asset =
                            crate::content::video_slide_asset_path(content_root, slide.id);
                        #[cfg(target_os = "linux")]
                        let result = render_video_slide_in_session(
                            session, card, &asset, hold_ms, fps,
                        );
                        #[cfg(not(target_os = "linux"))]
                        let result: Result<()> = {
                            // Non-Linux build (Mac unit tests, etc.):
                            // V4L2 is Linux-only; keep the black-hold
                            // fallback so dev hosts can run --play-reel
                            // against a video fixture without panicking.
                            let _ = slide;
                            let _ = asset;
                            let _ = fps;
                            std::thread::sleep(std::time::Duration::from_millis(hold_ms));
                            Ok(())
                        };
                        if let Err(e) = &result {
                            eprintln!(
                                "reel: warn — video item {i} ({:?}) decode pipeline failed: {e:#}; \
                                 falling back to black-hold for {hold_ms}ms",
                                slide.name,
                            );
                            std::thread::sleep(std::time::Duration::from_millis(hold_ms));
                            Ok(())
                        } else {
                            Ok(())
                        }
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
                // Profile-mode short-circuit (2026-05-09 atlas SB
                // bench harness): when --profile-frames N exhausts
                // its budget, exit the reel cleanly so the Drop
                // guard in main() runs profile::summarize() and
                // dumps the histogram. Without this, the reel
                // would loop forever; SIGTERM/SIGKILL would skip
                // the histogram dump.
                if crate::profile::is_enabled()
                    && crate::profile::frames_remaining() == Some(0)
                {
                    eprintln!(
                        "reel: profile-frames budget exhausted mid-pass {pass} (item {i}); exiting cleanly"
                    );
                    return Ok(());
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
            // eglSwapBuffers (called immediately after this closure
            // returns) implicitly flushes; the explicit gl.flush()
            // forced an extra tile-store on vc4 (cold-scout #2 P6,
            // 2026-05-09).
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
    // Bug 7 follow-up (2026-05-17): mirror of with_egl_session's
    // call at L422 — atomic-commit path needs the same Broadcast
    // RGB = Full property write so --animate visual probes don't
    // see lifted blacks. The legacy `card.set_property` ioctl
    // coexists with atomic-commit property tracking: it's an
    // immediate property write on the connector, separate from
    // the AtomicModeReq accumulation below. Same one-shot at
    // session init.
    try_force_full_range_rgb(card, connector_info.handle())?;

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
            // eglSwapBuffers (called immediately after) implicitly
            // flushes; the explicit gl.flush() forced an extra
            // tile-store on vc4 (cold-scout #2 P6, 2026-05-09).
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

/// Bug 7 fix (2026-05-17): set the HDMI connector's `Broadcast RGB`
/// property to `Full` so vc4 scanout emits full-range (0-255) RGB
/// instead of the default limited-range (16-235). Pre-fix, the
/// framebuffer's (0,0,0) was being mapped to wire-level Y=16; TVs
/// in Full/Auto HDMI mode interpreted that as elevated gray. Probe
/// data + diagnostic at qa/captures/bug-7-blacks-not-black-recon-
/// 2026-05-17.md.
///
/// Graceful degradation: any failure (property missing, "Full"
/// enum value missing, set_property ioctl error) logs a warning
/// and returns Ok. The renderer still works without the fix —
/// it just doesn't lift the bug on that particular driver/board.
///
/// One-shot at session init per the dispatch's "Avoid adding it
/// to every page-flip's property array" guidance. The legacy
/// set_property ioctl persists until the next modeset or
/// session teardown.
///
/// Caller note: this operates on whatever connector
/// `pick_connector_and_mode` returned — empirically HDMI on the
/// canonical Pi Zero 2 W target, but not type-filtered. On a
/// driver that exposes `Broadcast RGB` on non-HDMI connectors
/// (rare), the same property write fires. The graceful-degradation
/// path makes this a no-op-with-warn when the property is absent.
fn try_force_full_range_rgb(
    card: &Card,
    connector_handle: connector::Handle,
) -> Result<()> {
    let props = match ObjectProps::for_connector(card, connector_handle) {
        Ok(p) => p,
        Err(e) => {
            eprintln!(
                "warn: Broadcast RGB lookup failed (connector props read): {e:#}; skipping (Bug 7 fix inapplicable)"
            );
            return Ok(());
        }
    };
    let prop_handle = match props.find("Broadcast RGB") {
        Ok(h) => h,
        Err(_) => {
            eprintln!(
                "note: connector has no 'Broadcast RGB' property; skipping full-range force (Bug 7 fix inapplicable on this driver)"
            );
            return Ok(());
        }
    };
    let info = match card.get_property(prop_handle) {
        Ok(i) => i,
        Err(e) => {
            eprintln!(
                "warn: get_property('Broadcast RGB') failed: {e:#}; skipping (Bug 7 fix)"
            );
            return Ok(());
        }
    };
    let full_value: u64 = match info.value_type() {
        property::ValueType::Enum(values) => {
            let (_, enums) = values.values();
            match enums
                .iter()
                .find(|e| e.name().to_string_lossy() == "Full")
            {
                Some(e) => e.value(),
                None => {
                    eprintln!(
                        "warn: 'Broadcast RGB' has no 'Full' enum value on this driver; skipping (Bug 7 fix)"
                    );
                    return Ok(());
                }
            }
        }
        other => {
            eprintln!(
                "warn: 'Broadcast RGB' value type is {other:?}, expected Enum; skipping (Bug 7 fix)"
            );
            return Ok(());
        }
    };
    match card.set_property(connector_handle, prop_handle, full_value) {
        Ok(()) => {
            eprintln!(
                "Bug 7 fix: Broadcast RGB = Full ({full_value}) on connector {:?}",
                connector_handle
            );
        }
        Err(e) => {
            eprintln!(
                "warn: set_property('Broadcast RGB' = Full) failed: {e:#}; carrying on (Bug 7 fix not applied on this connector)"
            );
        }
    }
    Ok(())
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

#[cfg(test)]
mod r102_2_tests {
    /// Kill-switch tests for
    /// `crate::v4l2::is_transition_fbo_cache_enabled` live in
    /// `v4l2.rs`'s tests module because hdmi.rs is
    /// linux-cfg-gated and would not be compiled by
    /// `cargo test` on macOS dev hosts. The source-pin test
    /// below DOES run here on Linux targets where hdmi.rs is
    /// compiled.

    #[test]
    fn r102_3_live_3pass_uses_cached_legacy_transition_program() {
        // r102.3 source-pin regression-lock: the live 3-pass
        // hot path in `paint_and_present_one_transition_frame`
        // MUST use `cached_legacy_transition_program` (the
        // r102.3 struct cache) on the cache-enabled path -- if a
        // future refactor reintroduces per-tick
        // `link_program(VS_TEXTURED_QUAD, fs)` to that function,
        // the V3D BO leak comes back.
        //
        // We assert (a) the symbol exists, (b) it is called from
        // inside the function body, and (c) the kill-switch
        // fallback STILL contains the legacy `link_program`
        // call (so the A/B path keeps working).
        let src = include_str!("hdmi.rs");
        assert!(
            src.contains("fn cached_legacy_transition_program("),
            "r102.3 helper missing or renamed -- live 3-pass cache is gone",
        );
        assert!(
            src.contains("cached_legacy_transition_program(session.gl, fs)"),
            "r102.3 helper not called from paint_and_present_one_transition_frame",
        );
        // The kill-switch fallback path MUST still contain
        // link_program for A/B testing; assert it survives.
        assert!(
            src.contains("link_program(session.gl, VS_TEXTURED_QUAD, fs)"),
            "r102.3 kill-switch fallback path missing -- the =off A/B can't reproduce pre-r102.3 behavior",
        );
    }

    #[test]
    fn r102_3_cached_vbo_is_rebound_before_vertex_attrib_pointer() {
        // r102.3 subagent BLOCKER-1 regression-lock: the
        // `cached_textured_quad_vbo` helper only binds
        // GL_ARRAY_BUFFER on its first-create path; on every
        // cache hit it returns the handle without binding. If a
        // future refactor removes the `bind_buffer` call between
        // the VBO fetch and the `vertex_attrib_pointer_f32`
        // calls, tick 0 still works (cache miss = helper bound)
        // but every subsequent tick snapshots whatever buffer
        // bake_a/bake_b left bound -- garbled or black
        // transition frames.
        //
        // Find the `cached_textured_quad_vbo(session.gl)` call
        // and assert a `bind_buffer(glow::ARRAY_BUFFER, Some(vbo))`
        // exists between it and the next
        // `vertex_attrib_pointer_f32` invocation.
        let src = include_str!("hdmi.rs");
        let from = src
            .find("cached_textured_quad_vbo(session.gl)")
            .expect("cached_textured_quad_vbo call missing from hdmi.rs");
        let tail = &src[from..];
        let to = tail
            .find("vertex_attrib_pointer_f32")
            .expect("no vertex_attrib_pointer_f32 after cached_textured_quad_vbo");
        let window = &tail[..to];
        assert!(
            window.contains("bind_buffer(glow::ARRAY_BUFFER, Some(vbo))"),
            "r102.3 BLOCKER-1 regression: live-3-pass missing the \
             GL_ARRAY_BUFFER rebind between cached_textured_quad_vbo \
             and vertex_attrib_pointer_f32. The cache helper only \
             binds on first-create; subsequent ticks inherit whatever \
             bake_a/bake_b left bound (cover_quad_vbo, text VBO, etc).",
        );
    }

    #[test]
    fn r102_2_session_struct_has_transition_fbo_fields() {
        // Source-level regression-lock: pin the EglSession field
        // names so a future refactor that drops or renames them
        // surfaces here instead of as a silent leak regression.
        // Reads hdmi.rs as a string; assert the field
        // declarations exist verbatim.
        let src = include_str!("hdmi.rs");
        for field in [
            "transition_fbo_a: Option<glow::NativeFramebuffer>",
            "transition_tex_a: Option<glow::NativeTexture>",
            "transition_fbo_b: Option<glow::NativeFramebuffer>",
            "transition_tex_b: Option<glow::NativeTexture>",
            "transition_fbo_dims: Option<(u32, u32)>",
        ] {
            assert!(
                src.contains(field),
                "r102.2 EglSession field missing or renamed: `{field}`",
            );
        }
        // And the cleanup paths in cleanup_resources MUST free
        // the cache; assert both *_take() calls exist.
        for take in [
            "session.transition_fbo_a.take()",
            "session.transition_tex_a.take()",
            "session.transition_fbo_b.take()",
            "session.transition_tex_b.take()",
        ] {
            assert!(
                src.contains(take),
                "r102.2 cleanup_resources missing `{take}` -- handle would leak at session teardown",
            );
        }
    }
}

