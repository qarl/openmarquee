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
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// r110 stage 2 (2026-06-11): instrumentation counters for
/// `bake_video_slide_to_current_fbo` Ok(None) returns.
///
/// Distinguishes two cases:
/// - `BAKE_VIDEO_OK_NONE_EAGAIN`: the 10x3ms inner DQBUF loop
///   exhausted without a frame. Steady-state lag indicator —
///   the codec couldn't deliver within ~30ms, the caller skips
///   swap+commit, the wall holds the prior frame.
/// - `BAKE_VIDEO_OK_NONE_EOS`: the inner loop got Ok(None) from
///   next_frame, signalling end-of-stream. Normal end-of-clip
///   cycle event; not a lag indicator. Live-path Ok(None) is
///   EPIPE-only post-c3.0 (2026-06-11) — the FLAG_LAST-mid-
///   stream → Ok(None) emission was defanged because bcm2835-
///   codec emits FLAG_LAST spuriously on short clips. See
///   v4l2.rs:3216-3237 for the contract block.
///
/// Emitted as delta on each `ipc.soak` 30s summary line so QA
/// can verify the r106 Ok(None)-steady-state-lag hypothesis
/// (H4) empirically on FYS. r103.1 baseline should show
/// `bake_ok_none_eagain` ≈ 0 in steady-state.
pub static BAKE_VIDEO_OK_NONE_EAGAIN: AtomicU64 = AtomicU64::new(0);
pub static BAKE_VIDEO_OK_NONE_EOS: AtomicU64 = AtomicU64::new(0);

/// r110 stage 3 commit 3.3.1 (2026-06-11) — explicit poster-
/// sourced signal for the c3.3 recreate predicate.
///
/// Replaces the inferred `frames_decoded == 0` heuristic
/// shipped in c3.3 (commit f681f7e) which fired on EVERY
/// preloaded video slide because video_decode.rs:825 + :1058
/// explicitly reset the counter to 0 on healthy preload
/// handoff. On FYS the resulting decoder_drop/cache_load/
/// REQBUFS storm crashed the renderer + wedged bcm2835-codec
/// kernel-side (rebooted box to recover). See feedback memory
/// "subagent-reset-then-check-inverted-logic" for the pattern.
///
/// The explicit signal: c3.2.2's bake_a/bake_b poster fast-
/// paths CALL `poster_source_event(video_id)` whenever a
/// poster was sourced. c3.3.1's predicate at BeginSlide reads
/// the set; `clear_poster_source_record()` runs at
/// BeginTransition entry so the set scopes exactly to "the
/// most recent transition window."
// Vec (not HashSet) because HashSet::new() isn't const. The
// per-transition set is at most 2 entries (poster_a_video_id +
// poster_b_video_id), so linear scan is fine.
pub static LAST_TRANSITION_POSTER_SOURCED_VIDEO_IDS:
    std::sync::Mutex<Vec<uuid::Uuid>> =
    std::sync::Mutex::new(Vec::new());

pub fn poster_source_event(video_id: uuid::Uuid) {
    if let Ok(mut set) = LAST_TRANSITION_POSTER_SOURCED_VIDEO_IDS.lock() {
        if !set.contains(&video_id) {
            set.push(video_id);
        }
    }
}

pub fn poster_was_sourced_for(video_id: uuid::Uuid) -> bool {
    LAST_TRANSITION_POSTER_SOURCED_VIDEO_IDS
        .lock()
        .map(|set| set.contains(&video_id))
        .unwrap_or(false)
}

pub fn clear_poster_source_record() {
    if let Ok(mut set) = LAST_TRANSITION_POSTER_SOURCED_VIDEO_IDS.lock() {
        set.clear();
    }
}

/// r110 c3.3.1: pure-function recreate predicate, factored for
/// host-portable unit tests (closes c3.3 subagent WARN-2).
/// `is_video` = slide is a video endpoint (Pure Video or
/// Text-with-bg-video). `poster_was_sourced` = the explicit
/// signal: c3.2.2's poster fast-path fired for this video_id
/// during the just-completed transition.
pub fn should_recreate_decoder(is_video: bool, poster_was_sourced: bool) -> bool {
    is_video && poster_was_sourced
}

#[cfg(test)]
mod c331_should_recreate_decoder_tests {
    use super::should_recreate_decoder;

    #[test]
    fn non_video_never_recreates() {
        assert!(!should_recreate_decoder(false, true));
        assert!(!should_recreate_decoder(false, false));
    }

    #[test]
    fn video_without_poster_source_does_not_recreate() {
        // 720p success path: live decoder produced; poster
        // never sourced; decoder is healthy.
        assert!(!should_recreate_decoder(true, false));
    }

    #[test]
    fn video_with_poster_source_recreates() {
        // 1080p wedge path: poster sourced during transition;
        // decoder is presumed wedged; tear down + re-prime.
        assert!(should_recreate_decoder(true, true));
    }

    #[test]
    fn poster_source_event_dedups_and_clears() {
        // subagent NIT-5 from c3.3.1: pin the
        // poster_source_event dedup behavior and the
        // clear_poster_source_record idempotence.
        use super::{clear_poster_source_record, poster_source_event,
                    poster_was_sourced_for};
        let v = uuid::Uuid::new_v4();
        clear_poster_source_record();
        assert!(!poster_was_sourced_for(v));
        poster_source_event(v);
        poster_source_event(v); // dedup: no panic, no duplicate
        assert!(poster_was_sourced_for(v));
        clear_poster_source_record();
        assert!(!poster_was_sourced_for(v));
    }
}

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
/// per memory budget §4. Without eviction, a long-running renderer
/// with many distinct images grows CMA without bound until OOM.
/// Implementation lives in crate::lru as a generic LruMap so the
/// eviction policy is host-testable on Mac (hdmi.rs is Linux-only).
///
/// CMA-arc 2026-06-21 C2: cap cut 6 → 3 to claw back ~25 MB
/// of worst-case headroom (3 × ~8.3 MB at 1080p RGBA). The
/// working set is current + next image-bg slide; 3 keeps one
/// slot of slack for preload-mode=max scenarios. A reel that
/// cycles >3 distinct image-bg backgrounds in flight will
/// trip eviction churn on transition; per QA the seed image
/// assets are currently invalid PNGs so the path is rare. If
/// production reels grow image-bg breadth, revisit (or add
/// time-expiry to the LRU).
pub const IMAGE_BG_CACHE_CAPACITY: usize = 3;

pub type ImageBgCache = crate::lru::LruMap<PathBuf, (glow::NativeTexture, u32, u32)>;

/// r110 stage 3 commit 3.1 (2026-06-11): per-session cache for
/// VideoSlide poster textures (poster frozen-entry strategy).
///
/// Same shape as `ImageBgCache` — PNG-on-disk → GL texture, keyed
/// by full filesystem path so the LRU eviction observes natural
/// load patterns. Separate cache so poster textures don't compete
/// for slots with image-slide backgrounds.
///
/// Capacity is intentionally modest: at the poster strategy's
/// steady state only the CURRENT slide and (during transition)
/// the NEXT slide's posters need to be hot. A small cache that
/// evicts to disk between transitions is correct.
pub type PosterCache = crate::lru::LruMap<PathBuf, (glow::NativeTexture, u32, u32)>;

/// r110 stage 3 commit 3.1: capacity for `PosterCache`.
///
/// CMA-arc 2026-06-21 C2: cap cut 4 → 2. The docstring's prior
/// "~3 MB at 1080p RGBA" math was off by ~3× (1920 × 1080 × 4 =
/// ~8.3 MB, not 3 MB); the actual 4-entry worst case was ~33 MB,
/// not ~12 MB. New sizing: 2 entries = "current + next slide"
/// (the active fade's A + B endpoints). The prior "next 2 slides
/// hot + preload-mode=max slack" justifies 3-4 cap but each entry
/// is a real ~8.3 MB, and the 320 MB Pi Zero 2 W CMA budget can't
/// afford the slack. Reclaim: ~17 MB worst-case. If a future
/// reel exercises preload-mode=max with video slides and the
/// 2-slot cap causes poster thrash at fade boundaries, revisit
/// (raise to 3, or add a time-expiry layer to defer eviction
/// during active transitions).
pub const POSTER_CACHE_CAPACITY: usize = 2;

/// CMA-arc 2026-06-21 C3: bounded LRU on `slide_caches` (was an
/// unbounded HashMap). Each entry caches per-layer text bitmaps +
/// optionally bg_tex + first_frame_tex (potentially ~8.3 MB at
/// 1080p RGBA each). Pre-arc the cache grew to the playlist's
/// distinct slide count with no eviction; on long-running reels
/// with many distinct slides the working set ballooned.
///
/// Cap = 6. The hot working set is current + next (during a
/// transition) + 1 slack; 6 keeps an additional ~4 slots for
/// recently-played slides that the reel may revisit in the next
/// cycle (FYS reel cycles every 19 slides, so the second-pass
/// hit-rate matters). On a reel with >6 distinct slides per
/// active cycle the LRU evicts cold-end entries; eviction is
/// leak-safe via `free_slide_render_cache` in the insert
/// wrapper.
///
/// If production reels grow beyond 6 hot slides at once, revisit
/// (raise the cap, or add a time-expiry layer).
pub const SLIDE_CACHES_CAPACITY: usize = 6;

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
    /// r110 stage 3 commit 3.1 (2026-06-11): per-session cache
    /// of decoded + uploaded VideoSlide poster textures (poster
    /// frozen-entry strategy). See PosterCache docs.
    poster_cache: PosterCache,
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
    /// Flip-race fix D (2026-06-22): 3rd scanout slot for triple-
    /// buffering. Fix A (gl.finish) + C (EGL fence) both closed the
    /// race correctness-wise but cost ~28ms per present stall =
    /// halved FPS (single-frame vc4 1080p render time, not
    /// pipeline-backlog). Pivot per QA: HOLD an extra scanout slot
    /// so GBM's pool grows to 3 BOs. When GBM recycles a BO into
    /// the next swap, that BO was last drawn ~2 frames ago = GPU
    /// long done = no stale-tile race + no current-frame stall.
    ///
    /// Rotation depth = 3:
    ///   prev2 (oldest, just released by recent page_flip) → recycled
    ///   prev  (penultimate)
    ///   current (just page-flipped, kernel scanning)
    ///
    /// Per-tick: destroy prev2 → shift prev→prev2 → shift current→
    /// prev → set new→current. Destroy of prev2 is preceded by a
    /// fence wait on prev2's sync (created when this slot WAS
    /// current; by the time it reaches prev2 ~2 frames later, the
    /// fence is signaled → wait ≈0us). Cheap completion barrier on
    /// a known-done buffer, vs fix A/C's full-pipeline stall on the
    /// current.
    ///
    /// Resource cost: +1 1080p ARGB8888 BO ≈ 8 MB CMA. Headroom
    /// verified on Pi Zero 2 W.
    scanout_prev2_bo: Option<BufferObject<()>>,
    scanout_prev2_fb: Option<framebuffer::Handle>,
    /// Flip-race fix D (2026-06-22): per-slot EGL fence sync for the
    /// 3-deep scanout rotation. Created when a BO enters `current`
    /// (= just rendered + page-flipped). Shifts through `prev` →
    /// `prev2`. When the slot reaches `prev2` and is about to be
    /// recycled, the rotation helper waits the fence (~0us for a
    /// 2-frames-old fence) + destroys it before dropping the BO.
    /// The wait guarantees the GPU is done with this BO's PRIOR
    /// draw — necessary because the BO returns to GBM's pool on
    /// drop, and a subsequent swap may pick it as the backbuffer.
    ///
    /// `None` when sync creation failed (driver lacks EGL_KHR_
    /// fence_sync or attrib check failed — both should be impossible
    /// on Mesa+vc4 with the ATTRIB_NONE-terminated list, but
    /// defensive None handling preserves correctness via the GBM
    /// implicit-sync fallback path).
    scanout_current_sync: Option<egl::Sync>,
    scanout_prev_sync: Option<egl::Sync>,
    scanout_prev2_sync: Option<egl::Sync>,
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
    /// Snapshot-side-A (2026-06-21): captured outgoing video
    /// frame for an in-flight video→video transition. Populated
    /// on the FIRST tick of a v2v fade (after bake_a succeeds);
    /// freed at progress>=0.99, on entry to a non-v2v transition
    /// (defensive), and at session teardown. Commit 1 captures
    /// but does not consume — verifies the GLES2 copy path
    /// doesn't regress baseline. Commit 2 wires the side-A
    /// bypass that reads this texture instead of re-feeding the
    /// outgoing decoder.
    transition_still_a_tex: Option<glow::NativeTexture>,
    /// 2026-07-04 (Jason device): session-cached RGBA texture
    /// containing the freshly-decoded first frame of the incoming
    /// (B) side's video, uploaded from the `CapturedNv12Frame` the
    /// preload worker captured during its handoff drain. Replaces
    /// the (potentially days-stale) poster.png fallback as the
    /// frozen-entry visual during video→video transitions.
    ///
    /// Shape: `(source_video_id, tex, w, h)`. The `source_video_id`
    /// invalidates the cache when a new transition targets a
    /// different B-side video (free tex + re-upload). Slot is
    /// populated at the FIRST paint tick of a transition where the
    /// caller passed a `preloaded_first_frame_b` reference AND
    /// the slot was either empty or held a different video_id.
    /// Freed at session teardown + on invalidation.
    transition_preloaded_first_frame_b_tex:
        Option<(uuid::Uuid, glow::NativeTexture, u32, u32)>,
    /// 2026-07-04 (Jason device H2 arc): "last already-displayed
    /// frame" for the outgoing side of the next transition. Captured
    /// by `paint_and_present_one_frame_for_slide` at end of the
    /// slide-hold paint, gated by `PlaybackState.capture_composite_
    /// video_id` (set by the PreloadSlide IPC arm during the ~1s
    /// preload-lead window before a transition; NOT full-time — a
    /// 6-12% GPU overhead on a 24fps at-budget sign is not
    /// acceptable). Consumed by `paint_and_present_one_transition_
    /// frame` as the H2-safe `SideAPlan::UseCachedComposite`
    /// source: zero V4L2 activity on side A at transition time
    /// → no 2nd outgoing decoder open → no r97 codec-contention
    /// deferral on the incoming preload → pr38's preloaded-first-
    /// frame path continues to work.
    ///
    /// Shape: `(source_video_id, tex, mode_w, mode_h)`. Freed on
    /// session teardown.
    last_video_paint_composite_tex:
        Option<(uuid::Uuid, glow::NativeTexture, u32, u32)>,
    /// r102.2: dims the cached transition_fbo_a/b were
    /// allocated against. Invalidates the cache on mode change
    /// (HDMI hot-plug, rotation flip). `None` while the cache
    /// is empty.
    transition_fbo_dims: Option<(u32, u32)>,
    /// CMA-arc 2026-06-22 RANK 3: timestamp of the most recent
    /// `ensure_transition_fbo_pair` call. The transition FBO
    /// pair (~16.6 MB CMA at 1080p ARGB8888) is alloc-once-and-
    /// reuse — pre-RANK-3 it was held through every static-slide
    /// hold + only freed at session teardown. `free_idle_session_fbos`
    /// (called at the top of paint_and_present_one_frame_for_slide)
    /// frees the pair when this stamp is older than
    /// `IDLE_FBO_THRESHOLD` (so consecutive transition ticks
    /// never trigger a free, but a long-running hold after a
    /// transition reclaims the CMA). `None` while the pair has
    /// never been allocated this session.
    last_transition_fbo_use: Option<std::time::Instant>,
    /// CMA-arc 2026-06-22 RANK 3: timestamp of the most recent
    /// `ensure_bake_atlas` call. The scissored-bake atlas
    /// (2048×2048 RGBA = ~16 MB CMA) is alloc-once-and-reuse —
    /// pre-RANK-3 held through every hold + only freed at
    /// teardown. Same idle-free pattern as
    /// `last_transition_fbo_use`.
    last_scissored_bake_use: Option<std::time::Instant>,
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
    /// Keyed by slide_id (Uuid).
    ///
    /// CMA-arc 2026-06-21 C3: swapped from HashMap to LruMap with
    /// `SLIDE_CACHES_CAPACITY` (= 6) entries. The original
    /// HashMap's design assumption ("19 slides × ~1 MB =
    /// 19 MB fits trivially") was based on small text bitmaps —
    /// but the SlideRenderCache also holds bg_tex + first_frame_tex
    /// + per-layer textures (each potentially ~8.3 MB at 1080p).
    /// On a reel that warms many slides without revisits, the
    /// HashMap grew CMA without bound until session teardown.
    /// The LruMap caps the working set; evicted entries are
    /// fed to `free_slide_render_cache` (which deletes the
    /// cached GL textures) so the eviction is leak-safe. See
    /// the `slide_caches_insert` helper in this file.
    ///
    /// Cleanup at with_egl_session teardown drains all entries
    /// + delete_textures while gl context is still bound.
    slide_caches: crate::lru::LruMap<uuid::Uuid, SlideRenderCache>,
    /// QA-direct (2026-05-08, post-clock_nanosleep): session-cached
    /// fullscreen-quad VBO for the SP transition path. The same
    /// 4-vert TRIANGLE_STRIP geometry is used by every transition
    /// kind; lifting it out of the per-call setup saves the
    /// gl.create_buffer + buffer_data ioctl pair on every call
    /// (~1 ms per transition * 18 reel transitions). Lazy-init on
    /// first SP transition; freed at with_egl_session teardown.
    transition_sp_quad_vbo: Option<glow::NativeBuffer>,
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
    // CMA-arc 2026-06-21 C4: the prior `msdf_atlases: Vec<MsdfAtlasGl>`
    // session field is retired. Owned MsdfAtlasGl entries now live
    // in the `MSDF_ATLAS_OWNED` thread_local Vec (see further down
    // in this file). The migration was necessary because lazy
    // upload now happens from `ensure_msdf_atlas_uploaded` called
    // from paint_slide_with_viewport, which receives `&gl` (not
    // `&mut session`); plumbing &mut session through the paint
    // stack would have been a far larger refactor. The
    // thread_local approach matches the sibling LOOKUP pattern
    // already in use for MSDF_ATLAS_LOOKUP /
    // DYNAMIC_ATLAS_LOOKUP. Teardown drains via
    // `delete_owned_msdf_atlases` in `cleanup_resources`.
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

    // r117 (2026-07-15) — the GBM+EGL bring-up dance moved to the
    // shared `crate::egl_bringup` primitive (PR #B0.5 refactor)
    // so `colorlight_gpu_compositor::HeadlessGpuCompositor` can call
    // the same code path without duplication.  `for_drm_scanout`
    // preset is byte-identical to what this function used inline
    // pre-refactor (Argb8888 + SCANOUT|RENDERING + swap_interval(0));
    // any observable behavior change here would be a regression.
    //
    // Destructured into individual locals so downstream code
    // (~500 lines below using `&gl`, `&egl_lib`, `context`,
    // `display`, etc.) keeps its original shape — the refactor
    // is a lift of the bring-up + teardown ONLY, not a rewrite
    // of `with_egl_session`.  Teardown stays inline below (see
    // `warn:` clauses matching `crate::egl_bringup::tear_down_egl`)
    // so hdmi.rs can continue to interleave EGL cleanup with its
    // content-cache teardown as needed.
    let handles = crate::egl_bringup::bring_up_egl(
        &crate::egl_bringup::EglBringUpSpec::for_drm_scanout(phys_w as u32, phys_h as u32),
        card,
    )?;
    // Local declaration order matters: Rust drops locals in
    // REVERSE-declaration order at scope exit.  `_gbm_dev` MUST
    // outlive `gbm_surface` (surface holds a WeakPtr into the
    // device; destroying the device before the surface leaves the
    // surface's Drop calling `gbm_surface_destroy` against a dead
    // device — driver-defined behavior).  Pre-refactor hdmi.rs
    // declared gbm_dev first + gbm_surface second (pre-refactor
    // lines 926 + 935), which drops surface → device.  We preserve
    // that order here — declare `_gbm_dev` FIRST so it drops LAST.
    let _gbm_dev = handles._gbm_dev;
    let egl_lib = handles.egl_lib;
    let display = handles.display;
    let context = handles.context;
    let egl_surface = handles.egl_surface;
    let mut gbm_surface = handles.gbm_surface;
    let gl = handles.gl;

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
        poster_cache: PosterCache::with_capacity(POSTER_CACHE_CAPACITY),
        image_slide_tex_cache: crate::image_slide_tex::ImageSlideTextureCache::with_capacity(
            crate::image_slide_tex::IMAGE_SLIDE_TEX_CACHE_CAPACITY,
        ),
        scanout_prev_bo: None,
        scanout_prev_fb: None,
        scanout_current_bo: None,
        scanout_current_fb: None,
        // Flip-race fix D (2026-06-22).
        scanout_prev2_bo: None,
        scanout_prev2_fb: None,
        scanout_current_sync: None,
        scanout_prev_sync: None,
        scanout_prev2_sync: None,
        held_scanout_fb: None,
        held_scanout_bo: None,
        session_start: std::time::Instant::now(),
        scene_fbo: None,
        scene_tex: None,
        transition_fbo_a: None,
        transition_tex_a: None,
        transition_fbo_b: None,
        transition_tex_b: None,
        transition_still_a_tex: None,
        transition_preloaded_first_frame_b_tex: None,
        last_video_paint_composite_tex: None,
        transition_fbo_dims: None,
        // CMA-arc 2026-06-22 RANK 3: idle-free timestamps. Stamped
        // by ensure_transition_fbo_pair + ensure_bake_atlas; read
        // by free_idle_session_fbos.
        last_transition_fbo_use: None,
        last_scissored_bake_use: None,
        external_frame_tex: None,
        external_nv12_tex: None,
        current_settings: crate::content::Settings::default(),
        // CMA-arc 2026-06-21 C3: bounded LRU. See
        // SLIDE_CACHES_CAPACITY doc + `slide_caches_insert` helper
        // for the eviction-cleanup pattern.
        slide_caches: crate::lru::LruMap::with_capacity(SLIDE_CACHES_CAPACITY),
        transition_sp_quad_vbo: None,
        scissored_bake_atlas: None,
        // CMA-arc 2026-06-21 C4: `msdf_atlases` field retired;
        // owned MsdfAtlasGl entries now in the MSDF_ATLAS_OWNED
        // thread_local. No init needed (RefCell::new(Vec::new())
        // at thread_local decl).
        // Bug 3 Slice 1 part B (2026-05-19): construct the dynamic
        // glyph cache + its backing atlas page upfront. GlyphCache
        // spawns 4 std::thread workers via crossbeam-channel mpsc;
        // for Slice 1 those workers are stubs that drain + discard
        // MissRequest. AtlasPage::allocate_texture is called below
        // (after GL context is current) to set up the GPU-resident
        // Backing texture format: MSDF page = RGB8 (12 MB; alpha
        // unused — all four MSDF shaders sample .rgb only), COLR
        // page = RGBA8 (16 MB; premultiplied alpha load-bearing
        // for color glyphs). 4 MB CMA win on the MSDF side.
        dynamic_glyph_cache: crate::glyph_cache::GlyphCache::new(4),
        dynamic_atlas_page_msdf: crate::atlas_page::AtlasPage::new_rgb8(
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

    // CMA-arc 2026-06-21 C4: pre-arc this block UNCONDITIONALLY
    // uploaded ALL 23 SDF atlases at session bring-up (~30 MB
    // CMA, RGB8 textures). Even a reel that only uses one or two
    // font families paid the full 30 MB at session start —
    // load-bearing on the 320 MB Pi Zero 2 W budget. The 3-video
    // wedge reel (post-C1-C3 + code2's #1-#2 combined) saw the
    // crossfade peak drop CmaFree to ~6.6 MB — razor-thin. Lazy-
    // loading frees ~30 MB upfront and pays ~1.3 MB per family
    // ONLY on first text draw of that family. Typical reels use
    // 1-3 distinct families → ~26-28 MB stays reclaimed at
    // steady state.
    //
    // The lazy path lives in `ensure_msdf_atlas_uploaded` (this
    // file) and is invoked at each `msdf_atlas_for_family` call
    // site before the (read-only) lookup runs. MSDF_ATLASES_CPU
    // (the CPU-side parsed atlas Vec) is still process-singleton
    // OnceLock — `load_all_atlases` is parse-only over
    // `include_bytes!`-backed slices, so the CPU work is cheap +
    // happens once on first lazy upload. The GPU side
    // (session.msdf_atlases + MSDF_ATLAS_LOOKUP thread_local)
    // grows as families are touched.
    //
    // Failure semantics preserved: per-family upload failure is
    // logged + the family falls back to Inter (via the existing
    // `or_else(|| msdf_atlas_for_family("Inter"))` chains at the
    // call sites). If Inter ALSO fails, the same anyhow! error
    // surfaces as before — the operator sees the failure
    // immediately rather than getting silently-broken text. The
    // pre-arc bring-up path was fatal-on-fail for ANY atlas;
    // post-arc only Inter is load-bearing (the others can fail
    // safely with a visual fallback to Inter glyph metrics).

    // CMA-arc 2026-06-21 (was Bug 3 Slice 1/3B 2026-05-19): the
    // dynamic atlas pages' GPU textures (2048×2048 RGBA8 = 16 MB
    // each = 32 MB CMA total) were UNCONDITIONALLY allocated here
    // at session bring-up. Even a text-only reel with no dynamic
    // glyph misses paid the 32 MB cost, leaving little headroom
    // for a video decoder + crossfade on the Pi Zero 2 W's 320 MB
    // CMA budget. Now lazy: AtlasPage::allocate_texture is invoked
    // by glyph_cache::poll_completions on first Ready completion
    // for that render_mode; the DYNAMIC_ATLAS_LOOKUP thread_local
    // is published from poll_dynamic_glyph_completions (this file)
    // each call. allocate_texture is idempotent (atlas_page.rs:
    // 91-93 early-return on Some) so the per-call cost after the
    // first allocation is one borrow_mut on a thread_local.
    // Sample-site safety: all draw sites already gate on
    // `if let Some(dyn_tex) = dynamic_atlas_tex()` (e.g. L2825)
    // and skip the batch when None — the prior "alloc failed at
    // bring-up" branch and the new "alloc not yet fired" branch
    // both surface as the same None, both result in the same
    // skip. delete (in cleanup_resources below) is also a no-op
    // on a never-allocated page (atlas_page.rs:142 `take` no-ops
    // on None).

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
        // r110 stage 3 commit 3.1: drain the poster cache before
        // the GL context dies. Same shape as image_bg_cache.
        for (path, (tex, _, _)) in session.poster_cache.drain() {
            unsafe { gl.delete_texture(tex); }
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
        // added to SlideRenderCache (like r62's first_frame_tex)
        // are freed by the canonical single-source-of-truth helper
        // and not the inline tex+bg_tex deletion that diverged
        // from it. The 9+ slide_caches.remove call sites already
        // route through free_slide_render_cache; matching here
        // closes the last divergent path.
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
    // teardown can't dereference dead texture handles.
    clear_msdf_lookup();
    // CMA-arc 2026-06-21 C4: ownership of uploaded MsdfAtlasGl
    // moved from session.msdf_atlases (pre-arc field, now removed)
    // to the MSDF_ATLAS_OWNED thread_local Vec. Drain + delete
    // here so the GL handles release while the context is bound.
    delete_owned_msdf_atlases(&gl);
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
    // Flip-race fix D (2026-06-22): free the 3rd scanout slot
    // (prev2) + the per-slot EGL fence syncs. The kernel switched
    // away from prev2 multiple frames ago — safe to destroy.
    if let Some(fb) = session.scanout_prev2_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!("warn: destroy_framebuffer(scanout_prev2): {e}");
        }
    }
    if let Some(bo) = session.scanout_prev2_bo.take() {
        drop(bo);
    }
    for sync_slot in [
        session.scanout_current_sync.take(),
        session.scanout_prev_sync.take(),
        session.scanout_prev2_sync.take(),
    ] {
        if let Some(sync) = sync_slot {
            let _ = unsafe {
                session.egl_lib.destroy_sync(session.display, sync)
            };
        }
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
        if let Some(tex) = session.transition_still_a_tex.take() {
            gl.delete_texture(tex);
        }
        // 2026-07-04: preloaded first-frame RGBA texture. Freed on
        // session teardown so slot's video_id / dims don't survive
        // into a next session.
        if let Some((_vid, tex, _, _)) = session.transition_preloaded_first_frame_b_tex.take() {
            gl.delete_texture(tex);
        }
        // 2026-07-04 H2 arc: last-video-paint composite (side-A
        // frozen-entry source). Same teardown shape as the pr38
        // preloaded_first_frame slot above.
        if let Some((_vid, tex, _, _)) = session.last_video_paint_composite_tex.take() {
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
        if let Some((fbo, tex)) = session.scissored_bake_atlas.take() {
            gl.delete_framebuffer(fbo);
            gl.delete_texture(tex);
        }
    }
    drop(session);

    // Cleanup — unconditional, warn-on-Err so the original cause
    // propagates via `work_result?`.  Inline (not calling
    // `crate::egl_bringup::tear_down_egl`) so future work that
    // interleaves EGL cleanup with content-cache teardown keeps
    // this shape available.  The bring-up moved to the shared
    // primitive; teardown stayed here for that reason.  Behavior
    // MUST match `crate::egl_bringup::tear_down_egl` line-for-line
    // — any divergence is a bug.
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

    // Flip-race fix D2 (2026-06-22): DROP DRM_MODE_PAGE_FLIP_ASYNC.
    //
    // Why: fix D (triple-buffer + per-slot fence) reduced the snap-
    // back visibility but qarl still sees it (QA glass: "much less
    // visible than before"). Triple-buffer protects against reusing
    // a buffer too early, but ASYNC bypasses dma-buf implicit fence
    // → kernel can scan the just-flipped BO BEFORE the GPU finishes
    // writing it (vc4 tile-store ~28ms; vblank ~16ms; ASYNC kicks
    // in instantly = scan starts before GPU done).
    //
    // Dropping ASYNC restores vblank-synchronized flip. The kernel
    // waits for vblank AND honors the BO's implicit dma-buf fence
    // before scanning. Per the Mesa-vc4 implicit-sync contract,
    // GL writes attach a fence on the BO; vsync page_flip respects
    // it. Net: kernel never scans a still-being-written BO.
    //
    // Cost: vsync caps frame rate at vblank (60Hz). With
    // triple-buffer + pipelined render (~28ms), effective rate
    // settles ~30fps (every-other-vblank). Per QA prediction this
    // "likely only reduces further, not fully closes" — if so we
    // escalate to the atomic-in-fence fix (the kernel-side wait
    // that doesn't sacrifice vblank but does the same job
    // explicitly via plane IN_FENCE_FD prop).
    //
    // PRIOR ASYNC RATIONALE (QA-direct 2026-05-08): "use ASYNC so
    // the kernel performs the flip immediately rather than waiting
    // for vblank. Drops the per-frame commit_drain_poll wait
    // (~8ms p50 at 60Hz) to ~0 ms. Tradeoff: tearing during the
    // half-vblank window."
    // → That rationale predates the snap-back arc. The "tearing"
    //   tradeoff turned out to be a full-frame stale scanout under
    //   GBM pool cycling (qarl's snap-back). Fix D2 reverses this
    //   decision; the original 8ms vblank wait is the lesser evil
    //   vs the stale-frame race.
    let t_pageflip = std::time::Instant::now();
    card.page_flip(
        session.crtc_handle,
        fb,
        PageFlipFlags::EVENT,
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

/// Flip-race fix D (2026-06-22): 3-deep scanout buffer rotation.
///
/// Replaces the 9-line inlined rotation pattern previously at every
/// paint_and_present_*_frame tail (destroy scanout_prev + shift
/// current → prev + set new → current). Adds a third generation
/// (prev2) so the GBM surface pool grows to 3 BOs: when GBM
/// recycles a BO for the next backbuffer, that BO was last drawn
/// ~2 frames ago, giving the GPU enough lead time that its prior
/// content is COMPLETE before the next render starts — closing the
/// snap-back race (kernel scanning a still-being-drawn BO) without
/// the ~28ms current-frame stall fix A (gl.finish) and fix C (EGL
/// fence wait) both incurred.
///
/// Per-tick:
///   1. Wait on prev2's fence (created when this BO was current ~2
///      frames ago; by now signaled — wait ≈0us). Confirms the
///      GPU is done with the recycled BO before we hand it back to
///      GBM. Destroys the fence after.
///   2. Destroy prev2's DRM FB + drop prev2's BO (drop returns BO
///      to GBM pool for re-use as a future backbuffer).
///   3. Shift prev → prev2 (incl. fence). Shift current → prev.
///   4. New BO/FB becomes current. Create a fresh fence at this
///      point — it will be waited on ~2 frames hence when this
///      slot reaches prev2.
///
/// Telemetry: `[perf] scanout_rotate site=X prev2_wait_us=N
/// new_sync_create_us=M elapsed_us=T`. prev2_wait_us is the key
/// number — should be ~0us once the rotation is steady-state
/// (after the first 2 frames). If it climbs to ~28ms, the fence
/// strategy degenerated to fix A/C's cost — debug.
///
/// Held-scanout interaction: `end_of_in_session_render_call`
/// continues to operate on `scanout_current` for cross-call
/// preservation. prev2 + prev are released at end-of-call along
/// with current's rotation into held_scanout. See that function
/// for details.
#[allow(clippy::too_many_arguments)]
fn rotate_scanout_3_deep(
    session: &mut EglSession,
    card: &Card,
    new_bo: BufferObject<()>,
    new_fb: framebuffer::Handle,
    site: &'static str,
) {
    let t_recycle = std::time::Instant::now();

    // Step 1: wait + destroy prev2's fence (it's been signaled for ~2
    // frames, so the wait is essentially a no-op). If fence creation
    // failed back when this slot was current, sync is None — skip.
    let mut prev2_wait_us: u128 = 0;
    if let Some(sync) = session.scanout_prev2_sync.take() {
        let t_wait = std::time::Instant::now();
        let _ = unsafe {
            session.egl_lib.client_wait_sync(
                session.display,
                sync,
                egl::SYNC_FLUSH_COMMANDS_BIT,
                500_000_000,
            )
        };
        prev2_wait_us = t_wait.elapsed().as_micros();
        let _ = unsafe { session.egl_lib.destroy_sync(session.display, sync) };
    }

    // Step 2: destroy prev2's FB; drop prev2's BO (releases to GBM
    // pool — GBM may hand it back as the next backbuffer).
    if let Some(fb) = session.scanout_prev2_fb.take() {
        if let Err(e) = card.destroy_framebuffer(fb) {
            eprintln!(
                "warn: destroy_framebuffer(scanout_prev2, {site}): {e}",
            );
        }
    }
    if let Some(bo) = session.scanout_prev2_bo.take() {
        drop(bo);
    }

    // Step 3: shift prev → prev2; current → prev. Sync travels with
    // its BO so the fence is always tied to the correct frame's
    // GPU completion.
    session.scanout_prev2_sync = session.scanout_prev_sync.take();
    session.scanout_prev2_fb = session.scanout_prev_fb.take();
    session.scanout_prev2_bo = session.scanout_prev_bo.take();
    session.scanout_prev_sync = session.scanout_current_sync.take();
    session.scanout_prev_fb = session.scanout_current_fb.take();
    session.scanout_prev_bo = session.scanout_current_bo.take();

    // Step 4: new becomes current. Create a fresh fence to track
    // this draw's GPU completion (consumed when this slot reaches
    // prev2 ~2 ticks hence). Defensive: if create_sync fails, the
    // slot's sync stays None; future recycle skips the wait + relies
    // on natural GBM/Mesa implicit sync. Snap-back may reappear in
    // that path — log loudly.
    let t_create = std::time::Instant::now();
    let new_sync = match unsafe {
        session.egl_lib.create_sync(
            session.display,
            egl::SYNC_FENCE as egl::Enum,
            &[egl::ATTRIB_NONE],
        )
    } {
        Ok(s) => Some(s),
        Err(e) => {
            eprintln!(
                "warn: scanout_rotate create_sync failed site={}: {:?} \
                 (next recycle's fence wait will be skipped)",
                site, e,
            );
            None
        }
    };
    let new_sync_create_us = t_create.elapsed().as_micros();
    session.scanout_current_sync = new_sync;
    session.scanout_current_bo = Some(new_bo);
    session.scanout_current_fb = Some(new_fb);

    // Flip-race fix D2 finalize (2026-06-22): gate the per-rotation
    // perf line behind OPENMARQUEE_SCANOUT_ROTATE_LOG=1 so production
    // doesn't emit ~22 lines/sec to stderr. Default OFF. QA enables
    // env on instrumented builds when measuring; prod stays silent.
    // The gate is cached once via OnceLock (env-var read is the
    // first call's overhead; every subsequent call is an atomic
    // load).
    if scanout_rotate_log_enabled() {
        eprintln!(
            "[perf] scanout_rotate site={} prev2_wait_us={} \
             new_sync_create_us={} elapsed_us={}",
            site, prev2_wait_us, new_sync_create_us,
            t_recycle.elapsed().as_micros(),
        );
    }
}

/// Flip-race fix D2 finalize (2026-06-22): env gate for the
/// per-rotation scanout_rotate perf line. OnceLock-cached so the
/// env var is read exactly once per process. Default OFF: prod
/// stays silent (the line fires ~22 times/sec at full reel rate;
/// cumulative log volume across days of uptime is non-trivial).
/// QA enables via `OPENMARQUEE_SCANOUT_ROTATE_LOG=1` (or "true")
/// when measuring on glass.
fn scanout_rotate_log_enabled() -> bool {
    use std::sync::OnceLock;
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        std::env::var("OPENMARQUEE_SCANOUT_ROTATE_LOG")
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false)
    })
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
            slide_caches_insert(session, slide_id, SlideRenderCache::new(text_layers.len()));
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
            // CMA-arc 2026-06-21: wrap via
            // poll_dynamic_glyph_completions so the lazy-allocated
            // atlas textures are published to DYNAMIC_ATLAS_LOOKUP /
            // DYNAMIC_ATLAS_COLR_LOOKUP after this poll.
            let uploaded = poll_dynamic_glyph_completions(session, 4);
            if uploaded > 0 {
                if let Some(old) = session.slide_caches.remove(&slide_id) {
                    free_slide_render_cache(session.gl, old);
                }
                slide_caches_insert(
                    session,
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
    //
    // qarl 2026-07-16 — PER-LETTER jitter: Shake now offsets each
    // GLYPH independently (in the per-glyph loop below) instead of
    // translating the whole laid-out line as a rigid unit. So Shake
    // deliberately SKIPS this layer-level translate — applying both
    // would double-move the letters (line drifts + letters jitter).
    // Every other motion (ticker / bounce / …) is unchanged.
    let box_w_px = (layer.r#box.w * mode_w as f32).max(1.0);
    let box_h_px = (layer.r#box.h * mode_h as f32).max(1.0);
    let per_glyph_shake = if motion_kind == MotionKind::Shake {
        motion_state.shake
    } else {
        None
    };
    let (dx_px, dy_px) = if per_glyph_shake.is_some() {
        (0.0, 0.0)
    } else {
        motion_offset_to_px(motion_kind, motion_state, box_w_px, box_h_px, size_px)
    };
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
    for (glyph_index, q) in group.quads.iter().enumerate() {
        // qarl 2026-07-16 — per-LETTER jitter. For Shake, each glyph
        // gets its own offset derived from (layer basis, glyph_index)
        // instead of the line translating as a unit (Stage 3 above is
        // skipped for Shake). Amplitude is unchanged (±0.5–4 % of
        // glyph height), so letters jitter next to their neighbours
        // and the word stays readable. px→NDC conversion mirrors
        // Stage 3's exactly. Zero for every other motion.
        let (gdx_ndc, gdy_ndc) = match per_glyph_shake {
            Some(basis) => {
                let (ox_norm, oy_norm) =
                    crate::hdmi_logic::shake_glyph_offset_norm(basis, glyph_index);
                (
                    ((ox_norm * size_px) / mode_w as f32) * 2.0,
                    -(((oy_norm * size_px) / mode_h as f32) * 2.0),
                )
            }
            None => (0.0, 0.0),
        };
        let xl = to_ndc_x(q.px_left) + copy_dx + gdx_ndc;
        let xr = to_ndc_x(q.px_right) + copy_dx + gdx_ndc;
        let yt = to_ndc_y(q.px_top) + gdy_ndc;
        let yb = to_ndc_y(q.px_bottom) + gdy_ndc;
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

/// 2026-06-13 OFFSCREEN-CAPTURE-ONLY bg substitution for text-over-video.
///
/// Background: `resolve_slide_bg` (and therefore `resolve_slide_layers`)
/// does NOT handle `background_video_slide_id`. For a TextSlide carrying
/// that field, it falls through to `BgKind::Solid(background_color)` —
/// which the live IPC paint path bypasses entirely (it routes via
/// `TransitionEndpoint::TextOverVideo` and V4L2-decodes one bg frame per
/// tick). The offscreen capture functions (`capture_sb_transition_mid_to
/// _png`, `capture_legacy_3pass_transition_mid_to_png`, `capture_fullres
/// _transition_mid_to_png`) have no V4L2 plumbing, so they were painting
/// solid `background_color` (`#000000` default) behind the text composite.
/// That is the QA-reported "video background renders solid BLACK on both
/// endpoints" bug, shipped to fireplacesign on 2026-06-13.
///
/// This helper substitutes the bg with the referenced video's `poster.png`
/// when one exists on disk. It returns `BgKind::Image{asset_path=poster,
/// solid_fallback=<previous solid color>}` so that:
///   * paint_slide's existing Image arm loads + blits the PNG as the bg;
///   * if PNG decode fails at draw time, paint_slide's existing fallback
///     to solid_fallback keeps the offscreen output deterministic.
///
/// When the helper returns the bg unchanged (no bg_video_id, no content
/// root, or no poster on disk), the capture path keeps its pre-fix
/// solid-bg behavior — same PNG bytes as before for any slide that
/// wouldn't have hit the bug anyway. This is critical for not flooding
/// the existing golden-bless lineage with re-bless requests.
///
/// NEVER call this from the live IPC paint path. The live path uses
/// `TransitionEndpoint::TextOverVideo` (V4L2-decoded LIVE frame per tick);
/// substituting a frozen poster there would replace motion with a still
/// frame on FYS.
// `mod hdmi` is itself Linux-only (main.rs:19 `#[cfg(target_os = "linux")]`),
// so this fn never compiles on macOS — the cfg_attr below is belt-and-
// braces against any future build matrix where hdmi.rs is reachable on
// other targets.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
fn substitute_video_poster_bg(
    slide: &TextSlide,
    bg_kind: BgKind,
    content_root: Option<&Path>,
) -> BgKind {
    let Some(poster_path) =
        crate::content::capture_video_bg_poster_path(slide, content_root)
    else {
        return bg_kind;
    };
    // Preserve any pre-existing solid color as the fallback. resolve_
    // slide_bg never returns a non-solid for a text-over-video slide
    // today (the bg_video_id falls through pattern/image checks), but
    // future schema additions could; default to black for safety.
    let solid_fallback = match &bg_kind {
        BgKind::Solid(c) => *c,
        _ => [0.0, 0.0, 0.0, 1.0],
    };
    BgKind::Image {
        asset_path: poster_path,
        solid_fallback,
    }
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
    let mut rgba: Vec<u8> = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => {
            // CMA-arc 2026-06-22 C5: expand RGB -> RGBA into a
            // fresh buffer (can't avoid: 3 bytes/px -> 4
            // bytes/px), then explicit drop(buf) so the RGB
            // intermediate frees BEFORE the flip allocates its
            // scratch. Prior code held buf in scope to fn end —
            // ~6.2 MB at 1080p hot for no benefit after the
            // expand loop.
            let mut out = vec![0u8; (w * h) as usize * 4];
            for (src, dst) in
                buf.chunks_exact(3).zip(out.chunks_exact_mut(4))
            {
                dst[0] = src[0];
                dst[1] = src[1];
                dst[2] = src[2];
                dst[3] = 0xFF;
            }
            drop(buf);
            out
        }
        other => bail!(
            "png {}: color type {other:?} not supported (need RGB or RGBA)",
            path.display(),
        ),
    };
    // Bug W2: flip to bottom-up row order so the GL `v` convention
    // (see the doc comment above) renders the image right-side up.
    // CMA-arc 2026-06-22 C5: in-place flip — saves the second
    // ~8.3 MB allocation the prior consuming version paid (3-buffer
    // dance: decode buf + RGBA out + flipped). Per-image peak heap
    // drops from ~16.6 MB (RGBA path) or ~22.7 MB (RGB path) to
    // ~8.3 MB. Uses a single stride-sized scratch row (~7.7 KB at
    // 1080p) for the swap.
    crate::hdmi_logic::flip_rgba_rows_in_place(&mut rgba, w, h);
    Ok((rgba, w, h))
}

/// r110 stage 3 commit 3.1 (2026-06-11): cache-or-load lookup for
/// a VideoSlide poster texture (poster frozen-entry strategy).
///
/// On cache hit: returns the cached `(NativeTexture, width,
/// height)` tuple. The cache is touched via `get(&mut)` for LRU
/// recency ordering.
///
/// On cache miss: decodes `<content_root>/<slide_id>/poster.png`
/// via `load_png_rgba`, uploads it as a GL texture (mirroring
/// `image_bg` upload conventions: RGBA8, LINEAR filter,
/// CLAMP_TO_EDGE), inserts into the cache, and returns the new
/// tuple. LRU-evicted textures are freed; on a key replacement
/// (rare; only on retry-after-failure), the replaced texture is
/// also freed.
///
/// Returns `Ok(None)` when the poster file doesn't exist on disk
/// (slide has no poster yet — backend hasn't run the import
/// recipe, or this is a brand-new slide). Caller should fall
/// back to live-decode-only path. ENOMEM-fallback semantics
/// follow naturally: with no poster on disk, the poster frozen-
/// entry path falls back to whatever B's decoder is doing today.
///
/// Returns `Err` on disk-present-but-malformed PNG (wrong bit
/// depth, decode failure, GL upload failure). Caller should log
/// + fall back same as Ok(None).
///
/// `#[allow(dead_code)]` for stage 3 commit 3.1 — wired into
/// the transition composite path in commit 3.2.
#[allow(dead_code)]
pub unsafe fn ensure_poster_cached(
    session: &mut EglSession,
    content_root: &Path,
    slide_id: uuid::Uuid,
) -> Result<Option<(glow::NativeTexture, u32, u32)>> {
    use glow::HasContext;
    let path = crate::content::video_slide_poster_path(content_root, slide_id);
    // Cache hit: touch LRU ordering + return.
    if let Some((tex, w, h)) = session.poster_cache.get(&path) {
        return Ok(Some((*tex, *w, *h)));
    }
    // Backend hasn't run the import recipe yet for this slide
    // (or it's a brand-new slide). Caller falls back to live-
    // decode-only path.
    if !path.exists() {
        return Ok(None);
    }
    // Decode the PNG.
    let (rgba, w, h) = match load_png_rgba(&path) {
        Ok(t) => t,
        Err(e) => {
            return Err(e).with_context(|| format!(
                "ensure_poster_cached: load_png_rgba for slide {} at {}",
                slide_id, path.display(),
            ));
        }
    };
    // Upload to a fresh GL texture.
    let gl = session.gl;
    let tex = gl
        .create_texture()
        .map_err(|e| anyhow!(
            "ensure_poster_cached: glGenTextures for slide {}: {}",
            slide_id, e,
        ))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
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
    // Insert into cache; free evicted/replaced LRU entries.
    let outcome = session.poster_cache.insert(path.clone(), (tex, w, h));
    if let Some((evicted, _, _)) = outcome.evicted_lru {
        gl.delete_texture(evicted);
    }
    if let Some((replaced, _, _)) = outcome.replaced {
        gl.delete_texture(replaced);
    }
    // judder-instrument (2026-06-22): visual fingerprint to
    // correlate the poster against the first-displayed live
    // incoming frame. qarl spotted a START-OF-INCOMING judder
    // where the live video appears to start BEFORE the poster
    // ("like it goes back in time"). His hypothesis is that
    // the poster is NOT actually frame 0 (the safest backend
    // assumption was ffmpeg -ss 0 = IDR), but rather a later
    // or LAST frame -> backward jump. The fingerprint is 9
    // pixel R-channel values sampled in a 3x3 grid. Cheap
    // (~9 sample loads + log); compared against the Y-plane
    // fingerprint logged from drain_one_capture_for_preload
    // (frame 0, drained during preload) and from the first
    // 2 live frames in bake_video_slide_to_current_fbo.
    let fp = fingerprint_9_points(&rgba, w as usize * 4, w as usize, h as usize, 4);
    eprintln!(
        "[perf] poster_cache_loaded slide_id={} dims={}x{} cache_len={} fp_r={:?}",
        slide_id, w, h, session.poster_cache.len(), fp,
    );
    Ok(Some((tex, w, h)))
}

/// judder-instrument (2026-06-22): 9-sample pixel fingerprint for
/// cross-frame visual identity. Samples 9 points in a 3x3 grid
/// inset 16 px from each edge; returns the first byte of each
/// sampled pixel (R channel for RGBA buffers, Y for NV12 Y planes).
///
/// `byte_stride` = bytes per row (e.g. w*4 for tightly-packed RGBA,
/// kernel-reported stride for V4L2 Y planes).
/// `bytes_per_pixel` = 4 for RGBA, 1 for Y plane.
///
/// Returns all-zeros if dims are too small or the buffer is too
/// short. Intentionally cheap (~9 byte loads).
pub fn fingerprint_9_points(
    buf: &[u8],
    byte_stride: usize,
    w: usize,
    h: usize,
    bytes_per_pixel: usize,
) -> [u8; 9] {
    let mut out = [0u8; 9];
    if w < 48 || h < 48 || byte_stride < w * bytes_per_pixel {
        return out;
    }
    let inset = 16usize;
    let cx = w / 2;
    let cy = h / 2;
    let xs = [inset, cx, w - inset - 1];
    let ys = [inset, cy, h - inset - 1];
    let mut idx = 0;
    for &y in &ys {
        for &x in &xs {
            let byte_off = y * byte_stride + x * bytes_per_pixel;
            if byte_off < buf.len() {
                out[idx] = buf[byte_off];
            }
            idx += 1;
        }
    }
    out
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
        dem.sample_count(),
    );
    for _frame in 0..total_frames {
        let frame_start = std::time::Instant::now();
        if state.next_sample_idx >= dem.sample_count() {
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
            &dem,
            &mut state.next_sample_idx,
            &mut state.frames_decoded,
            &state.decoder,
            // 2026-07-04 (Jason device H2 arc): standalone reel
            // preview path — no PreloadSlide signal machinery,
            // never captures. Pass None; paint fn skips the
            // glCopyTexImage2D cost.
            None,
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
            // r110 stage 3 commit 3.2.0: standalone reel preview
            // path never uses poster frozen-entry — always live-
            // decode. Pass None for both poster ids.
            None,
            None,
            // 2026-07-04: standalone reel doesn't participate in
            // the preload-first-frame path (no IPC-side preload
            // worker, no `cache.preloaded_first_frames` map to
            // read from). Pass None; the paint helper falls
            // through to its existing live-decode branch.
            None,
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
    // CMA-arc 2026-06-22 RANK 3: free session-lifetime FBOs that
    // aren't currently in use (scene_fbo when settings are
    // identity, transition pair + scissored-bake atlas when idle
    // > 5s). Reclaims ~24-40 MB CMA on long static-hold periods
    // that previously held the FBOs through session teardown.
    // Each FBO group is lazy-ensure (re-allocates on next use),
    // so freeing is safe; the idle thresholds prevent churn.
    // NOT called from paint_and_present_one_transition_frame —
    // transition ticks stamp last_transition_fbo_use themselves.
    unsafe { free_idle_session_fbos(session); }
    // QA-direct (2026-05-14 slide-boundary characterization slice):
    // OPENMARQUEE_BOUNDARY_TRACE=1 emits one JSON line per painted
    // frame to stderr with per-phase Instant deltas in microseconds.
    // Zero overhead when off (one env::var lookup per frame; the
    // Instant captures are skipped entirely). Drained by the
    // sidecar smoke driver's stderr thread for offline analysis.
    let trace = std::env::var_os("OPENMARQUEE_BOUNDARY_TRACE").is_some();
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
    // CMA-arc 2026-06-21: wrap via poll_dynamic_glyph_completions.
    let uploaded = poll_dynamic_glyph_completions(session, 4);
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
            slide_caches_insert(
                session,
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

    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "text");
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
    demuxer: &crate::mp4_demux::Mp4Demuxer,
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
    // 2026-07-04 (Jason device H2 arc): when `Some(vid)`, capture
    // the currently-bound framebuffer's composited color attachment
    // into `session.last_video_paint_composite_tex` at end of paint
    // (before eglSwapBuffers). Gated by the PreloadSlide-set
    // `PlaybackState.capture_composite_video_id` — caller passes
    // Some only when the signal is active AND matches this slide's
    // outgoing video_id. See `SideAPlan::UseCachedComposite`
    // consumer in `paint_and_present_one_transition_frame`.
    capture_composite_for: Option<uuid::Uuid>,
) -> Result<()> {
    use glow::HasContext;
    // CMA-arc 2026-06-22 RANK 3: same idle-FBO free as the
    // text-only path. Wedge + video-only test reels never exercise
    // the text-slide hold path, so this call site is load-bearing
    // for the wedge-reel A/B.
    unsafe { free_idle_session_fbos(session); }
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
    // CMA-arc 2026-06-21: wrap via poll_dynamic_glyph_completions.
    let uploaded = poll_dynamic_glyph_completions(session, 4);
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
            slide_caches_insert(
                session,
                slide.id,
                SlideRenderCache::new(text_layers.len()),
            );
        }
    }
    crate::profile::record_phase(
        "paint_bake_text",
        t_phase.elapsed().as_nanos() as u64,
    );

    // r62 Phase B (2026-06-05): fast-path cache lookup. If we
    // captured this slide's composited first-frame on a prior
    // BeginSlide (in a cycling playlist this means "after the
    // first cycle"), short-circuit the V4L2 bake + text composite
    // + present + scanout sequence by drawing the cached texture
    // fullscreen, then doing the same swap+commit the normal path
    // does. ZERO V4L2 dependency on this paint -- first-frame
    // visible-on-glass latency drops from "wait for cold decoder
    // primer + first frame decode + composite + scanout" to
    // "blit a ~3.5MB texture + scanout" -- the goal qarl set.
    //
    // The live V4L2 decoder continues cold-priming in parallel
    // (preload IPC sent ~2s before this BeginSlide); on
    // subsequent ticks, *frames_decoded > 0 skips this fast
    // path and the live decoder takes over.
    //
    // Motion-text caveat: cached frame captures motion at the
    // phase active when it was first captured. On re-entry the
    // cached blit shows that snapshot, then live frames resume
    // at the CURRENT phase -- a small "jump" possible. Trade-off
    // is accepted: jump is far less visible than the stall it
    // replaces.
    //
    // r62 subagent (BLOCKER fix): the fast-path + capture are
    // gated on `rotation == 0`. On a rotated display the fast-
    // path would need to call run_present_pass TWICE per cached
    // paint (once with rotation=0 to blit cached into scene FBO,
    // once with session_rotation to present scene FBO into
    // default fb). PRESENT_QUAD_VBO is a 1-slot Cell explicitly
    // documented "rotation is fixed for the session lifetime, so
    // a single VBO suffices"; calling it with rotation=0 then
    // session_rotation back-to-back rebuilds the VBO TWICE per
    // fast-path paint and again at the next live paint -- 3x
    // VBO rebuilds vs zero, defeating the very latency win.
    // Widening PRESENT_QUAD_VBO to 2-slot (a la COVER_QUAD_VBO)
    // would be the proper fix but is larger scope; r62 ships the
    // simpler gate. FYS panel is rotation=0 (operator config) so
    // qarl gets the full r62 win; operators on rotated panels
    // fall through to the slow path -- same behavior as r61, no
    // regression.
    let cache_eligible = was_first && rotation == 0;
    let cache_hit_first_frame_tex = if cache_eligible {
        session
            .slide_caches
            .get(&slide.id)
            .and_then(|c| c.first_frame_tex)
    } else {
        None
    };
    if let Some(cached_tex) = cache_hit_first_frame_tex {
        // Fast path: cached blit + swap + commit + early return.
        // Self-contained so the cache-miss code below stays
        // untouched (cleaner diff + lower regression surface).
        let t_bake = std::time::Instant::now();
        unsafe {
            // Bound FBO is scene FBO (rotation) or default fb
            // (identity) -- set up by scene_fbo_handle binding
            // above. Since cache_eligible gates on rotation == 0,
            // scene_fbo_handle is None here when identity (the
            // common case); the bound FBO is default fb. Draw
            // cached texture verbatim into it.
            //
            // r62 subagent (WARN fix): no gl.flush() here. The
            // canonical implicit-flush boundary is eglSwapBuffers
            // below at the scanout step (the non-fast-path tail
            // relies on this too); an explicit flush would induce
            // a CPU-side roundtrip that defeats the pipelining
            // around the single-blit fast path.
            session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
            run_present_pass(session.gl, cached_tex, 1.0, 1.0, 0)?;
        }
        let bake_us = t_bake.elapsed().as_micros();

        // 2026-07-04 (Jason device H2 arc — geometry fix): capture
        // the LOGICAL composite BEFORE the rotation present pass.
        // Post-fix qarl reported top-half-black frames; QA
        // diagnosed the source: `copy_tex_image_2d(mode_w, mode_h)`
        // read from the POST-rotation default fb (physical dims
        // = swapped mode dims on rotated panels), so on a portrait
        // (rotation=90/270) sign the read exceeded the fb's height
        // and returned undefined bytes for the excess rows. Fix:
        // source from the LOGICAL composite here — either
        // `scene_fbo` (rotated / non-identity) or default fb
        // (identity + rotation=0). At this point in the fn (before
        // the rotation present pass at Step 3 below), the LOGICAL
        // composite is what's bound. Source dims here equal
        // (mode_w, mode_h) — the size the transition side's
        // UseCachedComposite blit expects.
        unsafe {
            capture_last_video_paint_composite(
                session,
                capture_composite_for,
                H2CaptureSite::TextOverVideoCached,
                mode_w,
                mode_h,
            )?
        };
        // Step 3 (rotation case): present scene FBO -> default fb
        // with CURRENT brightness/gamma/rotation. Honors settings
        // changes even on cached paint.
        let t_present = std::time::Instant::now();
        if let Some((_fbo, scene_tex)) = scene_fbo_handle {
            let brightness = (session.current_settings.brightness as f32) / 100.0;
            let gamma = session.current_settings.gamma;
            let (phys_w, phys_h) = session.phys_mode_size();
            unsafe {
                session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
                session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
                run_present_pass(session.gl, scene_tex, brightness, gamma, rotation)?;
            }
        }
        let present_us = t_present.elapsed().as_micros();

        // Step 4: standard scanout swap+commit -- verbatim mirror
        // of the non-cached path's tail.
        let t_scanout = std::time::Instant::now();
        // QA live-preview hook (2026-06-13): no-op unless
        // OPENMARQUEE_LIVE_PREVIEW_PATH is set in the env.
        session.maybe_live_preview_capture();
        session
            .egl_lib
            .swap_buffers(session.display, session.egl_surface)
            .map_err(|e| anyhow!("eglSwapBuffers (text-over-video cached) failed: {e:?}"))?;
        let new_bo = unsafe {
            session
                .gbm_surface
                .lock_front_buffer()
                .context("gbm_surface_lock_front_buffer (text-over-video cached) failed")?
        };
        let fb_buf = GbmBufferAdapter::new(&new_bo)
            .context("read GBM bo metadata (text-over-video cached)")?;
        let new_fb = card
            .add_framebuffer(&fb_buf, 32, 32)
            .map_err(|e| anyhow!("drmModeAddFB (text-over-video cached) failed: {e}"))?;
        if let Err(e) = commit_fb(session, card, new_fb) {
            if let Err(de) = card.destroy_framebuffer(new_fb) {
                eprintln!(
                    "warn: cleanup destroy_framebuffer({new_fb:?}) on commit-fail (text-over-video cached): {de}"
                );
            }
            drop(new_bo);
            return Err(e);
        }
        // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
        rotate_scanout_3_deep(session, card, new_bo, new_fb, "text_over_video_cached");
        let scanout_us = t_scanout.elapsed().as_micros();

        // Mark frame so subsequent ticks use live path.
        *frames_decoded = 1;

        // [perf] line with cache_hit=true so QA can see the wins
        // in journal. composite_us is 0 (skipped: text is already
        // in cached frame).
        let total_us = t_total
            .map(|t| t.elapsed().as_micros())
            .unwrap_or(0);
        eprintln!(
            "[perf] first_frame_paint slide_id={} cache_hit=true bake_us={} composite_us=0 present_us={} scanout_us={} total_us={}",
            slide.id, bake_us, present_us, scanout_us, total_us,
        );
        return Ok(());
    }

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
            demuxer,
            next_sample_idx,
            frames_decoded,
            decoder,
            mode_w,
            mode_h,
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

    // r62 Phase B (2026-06-05): capture the composited frame for
    // the cache. After the bake + composite steps, the currently
    // bound FBO (scene FBO when rotated/non-identity, default fb
    // otherwise) contains the video + text composite. Copy it
    // into a new RGBA8 texture via glCopyTexImage2D and store in
    // SlideRenderCache. On a subsequent BeginSlide for this
    // slide_id (cycling playlist re-entry), the fast-path
    // lookup at the top of this function will hit and produce
    // an instant first frame.
    //
    // Capture runs ONLY on `was_first` (i.e. this is the live
    // first paint after BeginSlide) AND when the cache entry is
    // currently None AND rotation == 0 (see the fast-path
    // BLOCKER-fix gate above). glCopyTexImage2D is GPU-to-GPU
    // (no CPU readback) -- cost ~few ms on bcm2835. One-time per
    // slide.
    if cache_eligible {
        let needs_capture = session
            .slide_caches
            .get(&slide.id)
            .map(|c| c.first_frame_tex.is_none())
            .unwrap_or(false);
        if needs_capture {
            let capture_result: Result<glow::NativeTexture> = unsafe {
                // Create destination texture sized to mode dims.
                // RGBA8 is implicit in glCopyTexImage2D's GL_RGBA
                // internalformat argument.
                let dest_tex = session.gl.create_texture()
                    .map_err(|e| anyhow!("r62 first_frame_tex create_texture: {e}"))?;
                session.gl.bind_texture(glow::TEXTURE_2D, Some(dest_tex));
                // Filter to LINEAR + CLAMP_TO_EDGE so the fast-path
                // present pass sampling produces clean pixels with
                // no border bleed (matches create_slide_fbo_pair's
                // texture setup).
                session.gl.tex_parameter_i32(
                    glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
                );
                session.gl.tex_parameter_i32(
                    glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
                );
                session.gl.tex_parameter_i32(
                    glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32,
                );
                session.gl.tex_parameter_i32(
                    glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32,
                );
                // glCopyTexImage2D reads from the currently bound
                // GL_FRAMEBUFFER. That's the scene FBO (rotation)
                // or default fb (identity) -- exactly what we want
                // to cache.
                session.gl.copy_tex_image_2d(
                    glow::TEXTURE_2D,
                    0, // mip level
                    glow::RGBA,
                    0, // x
                    0, // y
                    mode_w as i32,
                    mode_h as i32,
                    0, // border (must be 0 in GLES)
                );
                session.gl.bind_texture(glow::TEXTURE_2D, None);
                Ok(dest_tex)
            };
            match capture_result {
                Ok(dest_tex) => {
                    // Store in cache. If the cache entry isn't
                    // there (race with slide_caches.drain on
                    // glyph atlas upload), drop the texture
                    // cleanly.
                    if let Some(cache) = session.slide_caches.get_mut(&slide.id) {
                        cache.first_frame_tex = Some(dest_tex);
                    } else {
                        unsafe { session.gl.delete_texture(dest_tex); }
                    }
                }
                Err(e) => {
                    // Non-fatal: log + skip capture. Subsequent
                    // first-paint of this slide will retry.
                    eprintln!(
                        "warn: r62 first_frame_tex capture failed for slide {}: {e}",
                        slide.id
                    );
                }
            }
        }
    }

    // 2026-07-04 (Jason device H2 arc — geometry fix): capture the
    // LOGICAL composite BEFORE the rotation present pass. See the
    // capture-site comment in the cached-blit path above for the
    // full rationale (post-rotation default fb is physical dims;
    // sourcing mode dims from it exceeds the fb bounds on rotated
    // targets and produces the "top half black" symptom).
    unsafe {
        capture_last_video_paint_composite(
            session,
            capture_composite_for,
            H2CaptureSite::TextOverVideoLive,
            mode_w,
            mode_h,
        )?
    };
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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "text_over_video");
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
    // CMA-arc 2026-06-22 RANK 3: idle-free FBOs at image-slide
    // hold entry. Image-only reels never touch the text-slide
    // path so this is needed to actually trigger the ~40 MB
    // reclaim on image-heavy reels.
    unsafe { free_idle_session_fbos(session); }
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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "image");
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
    // CMA-arc 2026-06-22 RANK 3: idle-free at external-frame hold
    // entry (STREAM/VLC RGB push path).
    unsafe { free_idle_session_fbos(session); }
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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "external");
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
    // CMA-arc 2026-06-22 RANK 3: idle-free at external-NV12 hold
    // entry (STREAM/VLC HW-decode NV12 push path).
    unsafe { free_idle_session_fbos(session); }
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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "external_nv12");
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
    demuxer: &crate::mp4_demux::Mp4Demuxer,
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
    // 2026-07-04 (Jason device H2 arc): capture-composite arg.
    // Same shape + semantics as
    // paint_and_present_one_text_over_video_slide_frame's arg;
    // see that fn's docs. For pure-Video slides, the video_id
    // matches the slide's own id.
    capture_composite_for: Option<uuid::Uuid>,
) -> Result<()> {
    // CMA-arc 2026-06-22 RANK 3: idle-free at video-slide hold
    // entry. THIS IS THE WEDGE-REEL PATH (3-video crossfade
    // reel) — per QA the wedge reel never invokes the text-slide
    // path, so the original RANK 3 commit's free helper never
    // fired on the wedge-reel A/B. Adding here lights the
    // reclaim where the test reel exercises it.
    unsafe { free_idle_session_fbos(session); }
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
    let profile_first = *next_sample_idx == 1
        && *frames_decoded == 0
        && std::env::var("OPENMARQUEE_FIRSTFRAME_PROFILE")
            .ok()
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
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
            demuxer,
            next_sample_idx,
            frames_decoded,
            decoder,
            mode_w,
            mode_h,
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
    // 2026-07-04 (Jason device H2 arc — geometry fix): capture the
    // LOGICAL composite BEFORE the rotation present pass on the
    // pure-video path too. Same rotation-vs-mode dim mismatch
    // rationale as the text-over-video paths above.
    let (mode_w_cap, mode_h_cap) = (session.mode_w as u32, session.mode_h as u32);
    unsafe {
        capture_last_video_paint_composite(
            session,
            capture_composite_for,
            H2CaptureSite::VideoSlide,
            mode_w_cap,
            mode_h_cap,
        )?
    };
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
/// 2026-07-04 (Jason device H2 arc): capture the currently-bound
/// framebuffer's composited color attachment into
/// `session.last_video_paint_composite_tex`. Called at end of a
/// video-bearing slide-hold paint, RIGHT BEFORE eglSwapBuffers,
/// gated by the PreloadSlide signal.
///
/// Cost: one `glCopyTexImage2D` at mode dims (~2-4ms at 720p on
/// bcm2835). Only fires when `capture_composite_for` is `Some(vid)`
/// (i.e. the PreloadSlide-set `PlaybackState.capture_composite_
/// video_id` matched THIS slide's video id at ipc dispatch time).
///
/// Reuses an existing slot texture if it matches the current
/// `vid` AND is sized to the current mode; otherwise frees the
/// stale entry and creates a fresh one. The texture is a plain
/// RGBA8 sized to `(mode_w, mode_h)` — the same shape the
/// transition side's `run_blit_pass` expects for
/// `SideAPlan::UseCachedComposite`.
///
/// GLES2-safe (no READ_FRAMEBUFFER dance; `copy_tex_image_2d`
/// sources from whatever is bound to `GL_FRAMEBUFFER`).
/// 2026-07-04 (Jason device H2 arc — instrumented pass): seq
/// counter for the env-gated PNG dump path. Wraps at
/// `H2_DUMP_RING_SIZE` so `/var/tmp` doesn't fill on a long soak.
#[cfg(target_os = "linux")]
static H2_DUMP_SEQ: std::sync::atomic::AtomicU32 =
    std::sync::atomic::AtomicU32::new(0);

#[cfg(target_os = "linux")]
const H2_DUMP_RING_SIZE: u32 = 20;

/// 2026-07-04 (Jason device H2 arc — instrumented): capture-site
/// tag for the log marker. Runtime callers pass the paint-fn name
/// so QA can grep which of the three call sites fired.
#[cfg(target_os = "linux")]
#[derive(Copy, Clone, Debug)]
enum H2CaptureSite {
    VideoSlide,
    TextOverVideoCached,
    TextOverVideoLive,
    TransitionBlit,
}

#[cfg(target_os = "linux")]
unsafe fn capture_last_video_paint_composite(
    session: &mut EglSession,
    capture_composite_for: Option<uuid::Uuid>,
    site: H2CaptureSite,
    source_fb_w: u32,
    source_fb_h: u32,
) -> Result<()> {
    use glow::HasContext;
    let Some(vid) = capture_composite_for else {
        return Ok(());
    };
    let mode_w = session.mode_w as u32;
    let mode_h = session.mode_h as u32;
    let rotation = session.rotation;
    let gl = session.gl;
    // Query the currently-bound GL_FRAMEBUFFER so the log line names
    // the actual source of the copy_tex_image_2d read. `0` means the
    // default framebuffer (window-system surface). Nonzero = a user-
    // created FBO (scene_fbo, cached_pair_a/b, bake atlas, etc).
    let bound_fbo = gl.get_parameter_i32(glow::FRAMEBUFFER_BINDING);
    // QA-mandated (2026-07-04) diagnostic line: bound_fbo + its dims
    // + mode dims + rotation + site tag. Lets QA grep for
    // "h2_composite_capture ... rotation=90 fb_dims=1280x720 mode=720x1280"
    // and confirm the geometry mismatch class without needing a
    // PNG. Emitted on every capture attempt (light — one per
    // slide-hold paint during the ~1s PreloadSlide-armed window).
    eprintln!(
        "[perf] h2_composite_capture site={:?} vid={} bound_fbo={} fb_dims={}x{} \
         mode={}x{} rotation={} slot_dims_match={}",
        site,
        &vid.to_string()[..8],
        bound_fbo,
        source_fb_w,
        source_fb_h,
        mode_w,
        mode_h,
        rotation,
        matches!(
            session.last_video_paint_composite_tex,
            Some((slot_vid, _, w, h)) if slot_vid == vid && w == mode_w && h == mode_h,
        ),
    );
    // 2026-07-04 (Jason device H2 arc — geometry fix): copy dims
    // must be the MIN of the source framebuffer's dims and the
    // mode dims — reading (mode_w × mode_h) from a physically-
    // smaller default fb (post-rotation present pass) reads
    // BEYOND the fb bounds → the "top half of the frame is
    // black" symptom qarl reported. Callers are responsible for
    // routing the capture through the LOGICAL composite FBO
    // (scene_fbo when rotated / non-identity; default fb when
    // identity + rotation=0) BEFORE the rotation present pass,
    // so `source_fb_w/h` should always equal or exceed
    // `mode_w/h`. Belt-and-suspenders clamp here so a future
    // caller mis-wire won't produce black output — it'll produce
    // a smaller-region capture that will at least be legible.
    let copy_w = std::cmp::min(source_fb_w, mode_w);
    let copy_h = std::cmp::min(source_fb_h, mode_h);
    // Reuse existing slot if it matches the vid AND is sized to
    // the current mode. Otherwise free + create fresh.
    let reuse = matches!(
        session.last_video_paint_composite_tex,
        Some((slot_vid, _, w, h)) if slot_vid == vid && w == mode_w && h == mode_h,
    );
    let dest_tex = if reuse {
        session
            .last_video_paint_composite_tex
            .expect("guarded above")
            .1
    } else {
        if let Some((_, stale_tex, _, _)) = session.last_video_paint_composite_tex.take() {
            gl.delete_texture(stale_tex);
        }
        let tex = gl
            .create_texture()
            .map_err(|e| anyhow!("H2 last-composite create_texture: {e}"))?;
        gl.bind_texture(glow::TEXTURE_2D, Some(tex));
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
        gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
        gl.bind_texture(glow::TEXTURE_2D, None);
        session.last_video_paint_composite_tex = Some((vid, tex, mode_w, mode_h));
        tex
    };
    // copy_tex_image_2d sources from currently bound
    // GL_FRAMEBUFFER's color attachment (the composited frame).
    gl.bind_texture(glow::TEXTURE_2D, Some(dest_tex));
    gl.copy_tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::RGBA,
        0,
        0,
        copy_w as i32,
        copy_h as i32,
        0,
    );
    gl.bind_texture(glow::TEXTURE_2D, None);
    // Env-gated PNG dump (2026-07-04 QA-mandated). When
    // OPENMARQUEE_H2_DUMP_COMPOSITE=1, glReadPixels the currently-
    // bound framebuffer into an RGBA byte buffer + encode PNG at
    // /var/tmp/h2-composite-<site>-<vid8>-<seq>.png. Wraps at
    // H2_DUMP_RING_SIZE (20) so a long soak doesn't fill disk.
    // OFF by default (env unset → zero cost beyond the getenv
    // check).
    if std::env::var_os("OPENMARQUEE_H2_DUMP_COMPOSITE").is_some() {
        let seq = H2_DUMP_SEQ
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            % H2_DUMP_RING_SIZE;
        let mut pixels: Vec<u8> = vec![0u8; (copy_w * copy_h * 4) as usize];
        gl.read_pixels(
            0,
            0,
            copy_w as i32,
            copy_h as i32,
            glow::RGBA,
            glow::UNSIGNED_BYTE,
            glow::PixelPackData::Slice(pixels.as_mut_slice()),
        );
        // GL's origin is bottom-left; PNG wants top-down. Flip.
        let stride = (copy_w * 4) as usize;
        let mut flipped: Vec<u8> = vec![0u8; pixels.len()];
        for row in 0..(copy_h as usize) {
            let src_off = row * stride;
            let dst_off = (copy_h as usize - 1 - row) * stride;
            flipped[dst_off..dst_off + stride]
                .copy_from_slice(&pixels[src_off..src_off + stride]);
        }
        let path = format!(
            "/var/tmp/h2-composite-{:?}-{}-{:02}.png",
            site,
            &vid.to_string()[..8],
            seq,
        );
        match std::fs::File::create(&path) {
            Ok(file) => {
                let mut enc = png::Encoder::new(std::io::BufWriter::new(file), copy_w, copy_h);
                enc.set_color(png::ColorType::Rgba);
                enc.set_depth(png::BitDepth::Eight);
                match enc.write_header() {
                    Ok(mut writer) => {
                        if let Err(e) = writer.write_image_data(&flipped) {
                            eprintln!(
                                "[perf] h2_dump_write_err path={} err={:#}",
                                path, e,
                            );
                        } else {
                            eprintln!(
                                "[perf] h2_dump_wrote path={} dims={}x{} bytes={}",
                                path,
                                copy_w,
                                copy_h,
                                flipped.len(),
                            );
                        }
                    }
                    Err(e) => eprintln!(
                        "[perf] h2_dump_header_err path={} err={:#}",
                        path, e,
                    ),
                }
            }
            Err(e) => eprintln!(
                "[perf] h2_dump_create_err path={} err={:#}",
                path, e,
            ),
        }
    }
    Ok(())
}

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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "video");
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

/// 2026-07-04 (Jason device): upload a `CapturedNv12Frame` from the
/// preload worker's handoff drain into an RGBA texture on the GPU
/// and return it. Same NV12 → RGBA path as
/// `bake_video_slide_to_current_fbo`'s MMAP branch: Y goes into a
/// LUMINANCE tex, UV goes into a LUMINANCE_ALPHA tex, then
/// `run_nv12_blit_pass` cover-fits the frame into a scratch FBO
/// bound to a fresh RGBA destination texture sized to the panel's
/// mode. Callers stash the returned RGBA tex in
/// `session.transition_preloaded_first_frame_b_tex` and re-blit it
/// as the frozen-entry visual on every transition tick — replacing
/// the (potentially stale) disk poster.png fallback that the
/// c3.2.2 poster fast-path was previously sourcing 100% of the
/// time on the Jason device.
///
/// The Y and UV source textures + scratch FBO are one-shot: they
/// exist only for this single upload call and are deleted before
/// return. The RGBA destination texture is the caller's to manage.
unsafe fn upload_preloaded_first_frame_b(
    session: &mut EglSession,
    mode_w: u32,
    mode_h: u32,
    frame: &crate::video_decode::CapturedNv12Frame,
) -> Result<glow::NativeTexture> {
    use glow::HasContext;
    let gl = session.gl;
    let f_w = frame.width;
    let f_h = frame.height;
    // Destination RGBA texture: sized to the panel mode so the
    // subsequent frozen-entry blit_pass_quad is a cheap texture-
    // to-texture copy.
    let dest_tex = gl
        .create_texture()
        .map_err(|e| anyhow!("preloaded_first_frame_b create dest_tex: {e}"))?;
    gl.bind_texture(glow::TEXTURE_2D, Some(dest_tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
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
    gl.bind_texture(glow::TEXTURE_2D, None);
    let dest_fbo = gl
        .create_framebuffer()
        .map_err(|e| { gl.delete_texture(dest_tex); anyhow!("preloaded_first_frame_b create dest_fbo: {e}") })?;
    gl.bind_framebuffer(glow::FRAMEBUFFER, Some(dest_fbo));
    gl.framebuffer_texture_2d(
        glow::FRAMEBUFFER,
        glow::COLOR_ATTACHMENT0,
        glow::TEXTURE_2D,
        Some(dest_tex),
        0,
    );
    if gl.check_framebuffer_status(glow::FRAMEBUFFER) != glow::FRAMEBUFFER_COMPLETE {
        gl.bind_framebuffer(glow::FRAMEBUFFER, None);
        gl.delete_framebuffer(dest_fbo);
        gl.delete_texture(dest_tex);
        return Err(anyhow!("preloaded_first_frame_b dest_fbo not complete"));
    }
    // Y plane → LUMINANCE tex + UV plane → LUMINANCE_ALPHA tex;
    // mirrors the MMAP branch in `bake_video_slide_to_current_fbo`.
    let y_tex = gl
        .create_texture()
        .map_err(|e| { gl.bind_framebuffer(glow::FRAMEBUFFER, None); gl.delete_framebuffer(dest_fbo); gl.delete_texture(dest_tex); anyhow!("preloaded_first_frame_b Y tex: {e}") })?;
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(glow::TEXTURE_2D, Some(y_tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
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
        Some(&frame.y),
    );
    let uv_tex = match gl.create_texture() {
        Ok(t) => t,
        Err(e) => {
            gl.delete_texture(y_tex);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(dest_fbo);
            gl.delete_texture(dest_tex);
            return Err(anyhow!("preloaded_first_frame_b UV tex: {e}"));
        }
    };
    gl.active_texture(glow::TEXTURE1);
    gl.bind_texture(glow::TEXTURE_2D, Some(uv_tex));
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    gl.tex_image_2d(
        glow::TEXTURE_2D,
        0,
        glow::LUMINANCE_ALPHA as i32,
        (f_w / 2) as i32,
        (f_h / 2) as i32,
        0,
        glow::LUMINANCE_ALPHA,
        glow::UNSIGNED_BYTE,
        Some(&frame.uv),
    );
    gl.active_texture(glow::TEXTURE0);
    // Cover-fit into the panel-sized dest FBO.
    gl.viewport(0, 0, mode_w as i32, mode_h as i32);
    gl.clear_color(0.0, 0.0, 0.0, 1.0);
    gl.clear(glow::COLOR_BUFFER_BIT);
    // Reviewer fix (2026-07-04 concern 1a): `?` on cover_quad_vbo
    // would bubble past the cleanup below and leak all four GL
    // objects (y_tex, uv_tex, dest_fbo, dest_tex). Match instead
    // and free everything before returning Err.
    let cover_vbo = match cover_quad_vbo(gl, f_w, f_h, mode_w, mode_h) {
        Ok(v) => v,
        Err(e) => {
            gl.delete_texture(y_tex);
            gl.delete_texture(uv_tex);
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            gl.delete_framebuffer(dest_fbo);
            gl.delete_texture(dest_tex);
            return Err(e).context("preloaded_first_frame_b cover_quad_vbo");
        }
    };
    let blit_result = run_nv12_blit_pass(gl, cover_vbo, y_tex, uv_tex, frame.y_crop_max);
    gl.delete_texture(y_tex);
    gl.delete_texture(uv_tex);
    gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
    gl.bind_framebuffer(glow::FRAMEBUFFER, None);
    gl.delete_framebuffer(dest_fbo);
    if let Err(e) = blit_result {
        gl.delete_texture(dest_tex);
        return Err(e).context("preloaded_first_frame_b run_nv12_blit_pass");
    }
    Ok(dest_tex)
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
    // r110 stage 3 commit 3.2.0 (2026-06-11): poster-resolution
    // video ids for the poster frozen-entry strategy. Caller
    // (ipc_main.rs:run_paint_hook PaintTransition arm) resolves
    // these from the cache.items lookup:
    //   - Text with background_video_slide_id: that bg id (the
    //     MP4 + poster.png live under the bg video's content id,
    //     NOT the text slide id)
    //   - Video: the slide's own id
    //   - Text without bg, Image: None
    //
    // c3.2.0 is plumbing only; the values are accepted but not
    // read here. c3.2.1 calls `ensure_poster_cached` at function
    // entry; c3.2.2 wires the sourcing into bake_b.
    //
    // The standalone-reel caller at hdmi.rs:3456 passes (None,
    // None) — the reel preview path doesn't have a poster
    // strategy at all; it always live-decodes.
    poster_a_video_id: Option<uuid::Uuid>,
    poster_b_video_id: Option<uuid::Uuid>,
    // 2026-07-04 (Jason device): freshly-decoded NV12 first frame
    // for the incoming (B) side, captured by the preload worker's
    // handoff drain. When Some, uploads to a session-cached RGBA
    // texture on tick 1 and takes precedence over the disk poster
    // as the frozen-entry visual. See
    // `transition_preloaded_first_frame_b_tex` docs on `EglSession`
    // for the cache-slot lifecycle. Caller (ipc_main.rs
    // PaintTransition arm) removes from `cache.preloaded_first_
    // frames` on lookup so a single transition consumes the
    // capture exactly once; subsequent ticks receive `None` here
    // and paint from the cached RGBA texture in the session slot.
    preloaded_first_frame_b: Option<&crate::video_decode::CapturedNv12Frame>,
) -> Result<()> {
    // r110 stage 3 commit 3.2.1 (2026-06-11): cache-or-load
    // posters for both endpoints at function entry. The
    // posters drive bake_a/bake_b sourcing in c3.2.2 — if a
    // poster texture exists for an endpoint AND the endpoint
    // is video-bearing, the poster is the FROZEN-ENTRY visual
    // for the transition window (sources unconditionally on
    // 1080p per QA c3.2 correctness note: tick-1 preference
    // is poster-if-exists BEFORE the first bake_video attempt,
    // else 1080p tick 1 may present garbage/black before the
    // first Ok(None) is observed). For 720p where the live
    // decoder reliably produces frames, c3.2.2 still tries
    // bake_video first and only falls back to poster on
    // Ok(None) — the threshold logic is in c3.2.2.
    //
    // Both A and B posters loaded: dual-1080p contention can
    // cause A to wedge mid-transition (reloc=19M can't fit
    // dual 1080p DPB per QA plan review), so A needs the
    // same poster-fallback option as B.
    //
    // Loaded into `Option<(NativeTexture, u32, u32)>` locals
    // that c3.2.2 reads. None = no poster on disk → caller
    // falls back to live-decode-only. content_root=None
    // (defensive) → both posters None (the standalone-reel
    // path passes Some(content_root) so this is realistically
    // unreachable from the IPC path).
    let poster_a_texture: Option<(glow::NativeTexture, u32, u32)> =
        match (content_root, poster_a_video_id) {
            (Some(root), Some(vid)) => {
                match unsafe { ensure_poster_cached(session, root, vid) } {
                    Ok(opt) => opt,
                    Err(e) => {
                        eprintln!(
                            "[perf] poster_load_err side=a video_id={} err={:#}",
                            vid, e,
                        );
                        None
                    }
                }
            }
            _ => None,
        };
    let poster_b_texture_disk: Option<(glow::NativeTexture, u32, u32)> =
        match (content_root, poster_b_video_id) {
            (Some(root), Some(vid)) => {
                match unsafe { ensure_poster_cached(session, root, vid) } {
                    Ok(opt) => opt,
                    Err(e) => {
                        eprintln!(
                            "[perf] poster_load_err side=b video_id={} err={:#}",
                            vid, e,
                        );
                        None
                    }
                }
            }
            _ => None,
        };
    // 2026-07-04 (Jason device): if the preload worker captured a
    // fresh NV12 first frame for the incoming side, upload it to a
    // session-cached RGBA texture and use that as `poster_b_texture`
    // in preference to the (potentially stale) disk poster.png.
    // Cache-slot lifecycle:
    //   - slot Some, video_id matches → reuse (subsequent ticks)
    //   - slot Some, different video_id → free + upload fresh
    //   - slot None, bytes Some → upload (tick 1 of new transition)
    //   - slot None, bytes None → nothing to do (fall through to disk poster)
    // The uploaded texture stays in the slot for the whole transition
    // window so ticks 2..N don't re-upload — poster fast-path uses
    // it via a cheap run_blit_pass_quad each tick, identical to how
    // disk-poster texture is consumed today.
    if let Some((slot_vid, tex, _, _)) = session.transition_preloaded_first_frame_b_tex {
        if Some(slot_vid) != poster_b_video_id {
            // Different transition target: free stale slot.
            unsafe { session.gl.delete_texture(tex); }
            session.transition_preloaded_first_frame_b_tex = None;
        }
    }
    if session.transition_preloaded_first_frame_b_tex.is_none() {
        if let (Some(frame), Some(vid)) = (preloaded_first_frame_b, poster_b_video_id) {
            let m_w = session.mode_w as u32;
            let m_h = session.mode_h as u32;
            match unsafe { upload_preloaded_first_frame_b(session, m_w, m_h, frame) } {
                Ok(tex) => {
                    // Reviewer fix (2026-07-04 concern 1b): `dest_tex`
                    // inside `upload_preloaded_first_frame_b` was
                    // created at mode_w x mode_h with the NV12
                    // cover-fit already baked in — it is a mode-
                    // sized RGBA texture, not a native-frame-sized
                    // one. Store the MODE dims so the downstream
                    // poster-fast-path's `cover_quad_vbo(poster_w,
                    // poster_h, mode_w, mode_h)` degenerates to an
                    // identity fit instead of a second cover-fit
                    // (which on non-square-aspect displays would
                    // squash the already-fit image to the center
                    // strip). Only visible when frame dims differ
                    // from mode dims (e.g. rotated portrait target).
                    session.transition_preloaded_first_frame_b_tex = Some((
                        vid, tex, m_w, m_h,
                    ));
                    eprintln!(
                        "[perf] preloaded_first_frame_b_uploaded video_id={} native_dims={}x{} slot_dims={}x{}",
                        vid, frame.width, frame.height, m_w, m_h,
                    );
                }
                Err(e) => {
                    eprintln!(
                        "[perf] preloaded_first_frame_b_upload_err video_id={} err={:#}",
                        vid, e,
                    );
                }
            }
        }
    }
    // Precedence: preloaded fresh first-frame > disk poster.
    let poster_b_texture: Option<(glow::NativeTexture, u32, u32)> = session
        .transition_preloaded_first_frame_b_tex
        .map(|(_, tex, w, h)| (tex, w, h))
        .or(poster_b_texture_disk);
    // Whether the current poster_b_texture came from the preloaded
    // fresh-decode path (true) or the on-disk poster.png cache
    // (false). The c3.3.1 poster_source_event signal (which triggers
    // a 1080p decoder recreate on the next BeginSlide) fires ONLY
    // when the on-disk poster was actually sourced — a preloaded
    // fresh first frame means the decoder just delivered a real
    // frame, so no recreate is warranted.
    let poster_b_is_preloaded_first_frame =
        session.transition_preloaded_first_frame_b_tex.is_some();
    // c3.2.1: locals defined but not yet read by bake_a/bake_b.
    // c3.2.2 wires the sourcing logic.
    let _ = (poster_a_texture, poster_b_texture, poster_b_is_preloaded_first_frame);
    use glow::HasContext;
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

    // Snapshot-side-A Commit 2 (2026-06-21): tighter eligibility
    // than C1's is_dual_video. Endpoint A must be PLAIN Video --
    // TextOverVideo on side A would freeze text into the still
    // (the existing poster fast-path's "text is GL-cheap, MUST
    // NOT be frozen into the poster" contract; we honor the same
    // rule for the runtime snapshot). Endpoint B can be any
    // video-bearing kind (the bypass only frees side A's HW
    // decoder; side B is unaffected). Single-decoder transitions
    // (text<->video, image<->video) take other arms entirely.
    //
    // Defensive entry free belt-and-suspenders: ipc_main hooks
    // (BeginSlide / BeginTransition / Advance-after-Slide-paint)
    // are the authoritative free sites. This catches the rare
    // case where a non-snapshot-eligible transition fires
    // without an intervening Advance-Slide-paint to clear a
    // stale still (e.g. back-to-back BeginTransition without an
    // intervening Slide hold -- BeginTransition handler frees
    // anyway, so this is theoretically dead, but cheap).
    // 2026-07-04 (Jason device H2 arc): project the runtime
    // `TransitionEndpoint` variants to `EndpointKind` so the pure
    // decision layer in hdmi_logic.rs can key on kind without
    // touching any &mut V4L2 state. `is_snapshot_eligible` here
    // is called with the widened endpoint_a set (Video |
    // TextOverVideo) matching the pure-fn definition in
    // hdmi_logic.rs.
    use crate::hdmi_logic::{
        decide_side_a_plan, decide_side_b_plan, is_snapshot_eligible,
        unpin_target_for_endpoint_a, EndpointKind, SideAInputs, SideAPlan,
        SideBInputs, SideBPlan, UnpinTarget,
    };
    let endpoint_a_kind = match &endpoint_a {
        TransitionEndpoint::Text(_) => EndpointKind::Text,
        TransitionEndpoint::Image(_) => EndpointKind::Image,
        TransitionEndpoint::Video { .. } => EndpointKind::Video,
        TransitionEndpoint::TextOverVideo { .. } => EndpointKind::TextOverVideo,
    };
    let endpoint_b_kind = match &endpoint_b {
        TransitionEndpoint::Text(_) => EndpointKind::Text,
        TransitionEndpoint::Image(_) => EndpointKind::Image,
        TransitionEndpoint::Video { .. } => EndpointKind::Video,
        TransitionEndpoint::TextOverVideo { .. } => EndpointKind::TextOverVideo,
    };
    let snapshot_eligible = is_snapshot_eligible(endpoint_a_kind, endpoint_b_kind);
    if !snapshot_eligible {
        free_transition_still_a_tex(session);
    }
    // 2026-07-04 (Jason device H2 arc): does the last-video-paint
    // composite slot hold a matching frame for THIS transition's
    // outgoing? Matches by Uuid — slot's `source_video_id` must
    // equal the current `poster_a_video_id` (Video's own id OR
    // TextOverVideo's bg-video slide id). Populated by the
    // slide-hold paint during the PreloadSlide-armed window
    // preceding this BeginTransition.
    let cached_composite_present_for_this_endpoint = matches!(
        (session.last_video_paint_composite_tex, poster_a_video_id),
        (Some((slot_vid, _, _, _)), Some(cur_vid)) if slot_vid == cur_vid,
    );

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
        // r110 c3.2.2: pre-compute use_poster_a HERE (shared
        // borrow on endpoint_a) BEFORE inputs_a takes &mut.
        // The matches! on a shared borrow doesn't hold across
        // statements; we then read the bool downstream.
        let use_poster_a = matches!(
            &endpoint_a,
            TransitionEndpoint::Video { .. } | TransitionEndpoint::TextOverVideo { .. }
        ) && poster_a_texture.is_some();
        let endpoint_a_is_text_over_video =
            matches!(&endpoint_a, TransitionEndpoint::TextOverVideo { .. });
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
                demuxer,
                next_sample_idx,
                frames_decoded,
                decoder,
                ..
            } => SlideBakeInputs::Video {
                demuxer: *demuxer,
                next_sample_idx: &mut **next_sample_idx,
                frames_decoded: &mut **frames_decoded,
                decoder: *decoder,
            },
            TransitionEndpoint::TextOverVideo {
                bg_demuxer,
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
                    bg_demuxer: *bg_demuxer,
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
        // r110 stage 3 commit 3.2.2 (2026-06-11): poster fast-path.
        // When endpoint_a is video-bearing AND we have a poster on
        // disk AND a cached FBO pair, source from the poster
        // unconditionally for the entire transition window (this is
        // the FROZEN ENTRY contract — c3.1.1 BT.709 limited recipe
        // makes the poster pixel-identical to the live first frame,
        // so the handoff at c3.3 spin-up is invisible).
        //
        // Per QA c3.2 correctness note: tick-1 preference is
        // poster-if-exists BEFORE the first bake_video attempt at
        // 1080p, else tick 1 may present garbage/black. The
        // "unconditional source" pattern here satisfies that — we
        // never try bake_video when poster is available.
        //
        // TextOverVideo: composite text layers live on top of the
        // poster (text is GL-cheap, MUST NOT be frozen into the
        // poster — poster represents only what the V4L2 decoder
        // would have produced).
        // Snapshot-side-A Commit 2.2 (2026-06-21): snapshot
        // OVERRIDES poster for the OUTGOING side. QA glass
        // attempt #1 confirmed `[perf] snapshot_side_a_
        // captured` never fired on the all-stock reel because
        // r110 c3.2.2's poster fast-path preempted snapshot
        // for every postered video.
        //
        // Per QA's design question (c) -- the correct
        // resolution: r110's "poster is pixel-identical to
        // first frame" contract applies to INCOMING (side B
        // hasn't seen any frame yet; poster ≈ first live
        // frame ≈ what the user is about to see). For
        // OUTGOING (side A has been playing for the entire
        // hold; user just saw frame N of the clip), sourcing
        // the poster JUMPS visibly to frame 0 at fade start.
        // Snapshot captures the actual current frame, freezing
        // at the user-visible moment -- strictly better UX.
        //
        // Precedence is therefore A-side-asymmetric:
        //   - OUTGOING (A): snapshot > live-decode. Poster is
        //     IGNORED when snapshot_eligible.
        //   - INCOMING (B): poster > live-decode (UNCHANGED;
        //     side-B preserves r110 c3.2.2's frozen-entry
        //     contract verbatim).
        //
        // Resource cost: +1 V4L2 feed on tick 1 vs the all-
        // poster path (live-decode of A once to produce the
        // capture source). Ticks 2..N have zero A-side
        // V4L2 feeds (snapshot blit). r97 deferred-preload +
        // side-B Path B retry still active as the ceiling
        // guard + cold-start fallback.
        // 2026-07-04 (Jason device H2 arc): all side-A source-
        // selection is now driven by the pure `decide_side_a_plan`
        // from hdmi_logic.rs. Runtime bools below are 1:1 with
        // SideAPlan variants; the plan_tests mod exhaustively
        // pins the truth table (see the h2_* tests) so
        // TextOverVideo endpoint_a can NEVER land on a Bake
        // variant — compile-checked by the enum + covered by
        // the exhaustive-loop regression test.
        let side_a_plan = decide_side_a_plan(SideAInputs {
            snapshot_eligible,
            endpoint_a_kind,
            cached_composite_present_for_this_endpoint,
            still_a_present: session.transition_still_a_tex.is_some(),
            cached_pair_a_present: cached_pair_a.is_some(),
            poster_a_present: poster_a_texture.is_some(),
        });
        let use_cached_composite_a_now = side_a_plan == SideAPlan::UseCachedComposite;
        let use_still_a_now = side_a_plan == SideAPlan::UseStill;
        let use_poster_a_now = side_a_plan == SideAPlan::UsePoster;
        let should_bake_a_and_capture = side_a_plan == SideAPlan::BakeAndCapture;
        let should_skip_a = side_a_plan == SideAPlan::Skip;
        // Emit the tick-1 journal marker QA asked for. Plan is
        // constant across ticks 2..N so once is sufficient; the
        // marker lets QA correlate side_a_plan=UseCachedComposite
        // firing WITH side_b_plan=UsePreloadedSlot/UploadPreloaded
        // Input on the sign log — both signals green together
        // guards against the pr37/pr38/f4ec9501 whack-a-mole.
        if progress < 0.05 {
            eprintln!(
                "[perf] side_a_plan={:?} side_b_plan_hint=pending endpoint_a_kind={:?} \
                 endpoint_b_kind={:?} snapshot_eligible={} cached_composite_hit={} \
                 progress={:.3}",
                side_a_plan, endpoint_a_kind, endpoint_b_kind,
                snapshot_eligible, cached_composite_present_for_this_endpoint,
                progress,
            );
        }
        // If the plan is Skip, bail early: no side-A source is
        // available (rare — TextOverVideo with no cached composite,
        // no still, no poster). Caller returns Ok(false) so the
        // tick doesn't paint.
        if should_skip_a {
            crate::hdmi_logic::warn_paint_transition_skip(
                kind, progress, "side_a_plan_skip",
            );
            return Ok(false);
        }
        // Silence unused-variable warnings on the plan bits we
        // haven't wired into their own branch below yet. Both
        // BakeAndCapture / BakeOnly land in the shared `else` bake
        // path since the pure-fn already gated their eligibility;
        // the `should_bake_a_and_capture` flag guides the
        // subsequent snapshot-capture block.
        let _ = should_bake_a_and_capture;
        let (fbo_a, tex_a) = if use_cached_composite_a_now {
            // H2-safe side-A source: blit the previously-captured
            // slide-hold composite into the cached transition
            // FBO_A. Zero V4L2 activity on side A this tick.
            let (_vid, composite_tex, _tex_w, _tex_h) = session
                .last_video_paint_composite_tex
                .expect("guarded by cached_composite_present_for_this_endpoint");
            let (fbo, tex) = cached_pair_a.expect("guarded by cached_pair_a_present");
            use glow::HasContext;
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
            session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
            session.gl.clear(glow::COLOR_BUFFER_BIT);
            run_blit_pass(session.gl, composite_tex)?;
            (fbo, tex)
        } else if use_poster_a_now {
            let (poster_tex, poster_w, poster_h) = poster_a_texture.expect("guarded above");
            let (fbo, tex) = cached_pair_a.expect("guarded above");
            use glow::HasContext;
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
            session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
            session.gl.clear(glow::COLOR_BUFFER_BIT);
            // poster-fit (2026-06-22): cover-fit the poster's
            // native dims onto the panel, mirroring how
            // bake_video_slide_to_current_fbo aspect-preserves
            // live video frames. Pre-fix the poster used the
            // shared fullscreen quad (STRETCH), which on glass
            // showed a visibly-mis-sized frozen entry vs how
            // the live video plays. Now identical fit.
            let cover_vbo = cover_quad_vbo(
                session.gl, poster_w, poster_h, mode_w_u32, mode_h_u32,
            )?;
            run_blit_pass_quad(session.gl, poster_tex, cover_vbo)?;
            // TextOverVideo: composite text live on top.
            if endpoint_a_is_text_over_video {
                if let Some((slide_id, layers, motion_states)) = text_over_video_a.as_ref() {
                    let slide_id = *slide_id;
                    let layers_len = layers.len();
                    let needs_new = match session.slide_caches.get(&slide_id) {
                        Some(c) => c.glyph.len() != layers_len,
                        None => true,
                    };
                    if needs_new {
                        if let Some(old) = session.slide_caches.remove(&slide_id) {
                            free_slide_render_cache(session.gl, old);
                        }
                        slide_caches_insert(session, slide_id, SlideRenderCache::new(layers_len));
                    }
                    let runtime_glyph_ctx = Some(crate::glyph_cache::RuntimeGlyphCtx {
                        cache: &session.dynamic_glyph_cache,
                        fonts_dir: &session.dynamic_fonts_dir,
                    });
                    let cache = session.slide_caches.get_mut(&slide_id)
                        .expect("inserted above");
                    let wall_clock_unix = current_unix_seconds();
                    paint_slide_with_viewport(
                        session.gl,
                        mode_w_u32, mode_h_u32, 0, 0, mode_w_u32, mode_h_u32,
                        None, // bg already filled by poster blit
                        layers,
                        Some(motion_states),
                        wall_clock_unix,
                        Some(&mut cache.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache.tex),
                        runtime_glyph_ctx,
                    )?;
                }
            }
            // r110 c3.3.1 (subagent BLOCKER-1 fix): only signal
            // recreate for 1080p posters. 720p video slides ALSO
            // have posters on FYS (QA generated for all 21 video
            // assets across all resolutions), and their live
            // decoders work fine; recreating them would
            // reproduce c3.3's storm. The wedge condition is
            // bcm2835-codec's 1080p30-TOTAL spec — only 1080p
            // material triggers it.
            //
            // Threshold: >= 1080 height OR >= 1920 width.
            // Catches 1920x1080 + portrait 1080x1920 + any
            // larger material.
            if let Some(vid) = poster_a_video_id {
                if poster_w >= 1920 || poster_h >= 1080 {
                    poster_source_event(vid);
                }
            }
            eprintln!(
                "[perf] poster_a_sourced progress={:.3} dims={}x{} signal_set={}",
                progress, poster_w, poster_h,
                poster_w >= 1920 || poster_h >= 1080,
            );
            (fbo, tex)
        } else if use_still_a_now {
            // Snapshot-side-A Commit 2 (2026-06-21): runtime-
            // captured still bypass. fbo_a is the cached side-A
            // pair filled by blitting the still tex. Outgoing
            // decoder is NOT fed this tick — that's the fix.
            let still_tex = session.transition_still_a_tex.expect("guarded above");
            let (fbo, tex) = cached_pair_a.expect("guarded above");
            use glow::HasContext;
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
            session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
            session.gl.clear(glow::COLOR_BUFFER_BIT);
            run_blit_pass(session.gl, still_tex)?;
            (fbo, tex)
        } else {
            let Some((fa, ta)) = bake_slide_to_fbo(session, mode_w_u32, mode_h_u32, cached_pair_a, inputs_a)?
            else {
                crate::hdmi_logic::warn_paint_transition_skip(
                    kind, progress, "endpoint_a_no_frame",
                );
                return Ok(false);
            };
            (fa, ta)
        };
        // Snapshot-side-A Commit 2.2 (2026-06-21): capture the
        // freshly-baked side-A frame into transition_still_a_tex
        // on the FIRST tick of a snapshot-eligible transition.
        // We don't need the !use_poster_a_now gate anymore --
        // snapshot_eligible already suppresses use_poster_a_now
        // via the precedence flip above, so when this site
        // runs with snapshot_eligible=true, fbo_a came from
        // live-decode (not poster). Capture is one-shot per
        // transition; ticks 2..N find still.is_some() and
        // consume via use_still_a_now (no more bake_video on
        // side A).
        //
        // GLES2-safe FRAMEBUFFER bind (READ_FRAMEBUFFER is
        // GLES3-only and would silently error here).
        //
        // Frees: BeginSlide / BeginTransition / Advance-after-
        // Slide-paint hooks in ipc_main.rs handle lifecycle.
        // 2026-07-04 (Jason device H2 arc): snapshot-capture site
        // gated on `side_a_plan == BakeAndCapture` (the ONLY plan
        // that leaves a freshly-baked live-decode frame in fbo_a).
        // Pre-H2-fix the gate was `snapshot_eligible &&
        // !still_a_present`, which fired whenever the plan was
        // BakeAndCapture BUT ALSO whenever poster/still had just
        // been consumed (spurious captures of stale/poster
        // content). The pure-fn gate is tighter + kind-safe.
        if should_bake_a_and_capture
            && cached_pair_a.is_some()
            && session.transition_still_a_tex.is_none()
        {
            let dest_tex = session.gl.create_texture()
                .map_err(|e| anyhow!("snapshot-side-A create_texture: {e}"))?;
            session.gl.bind_texture(glow::TEXTURE_2D, Some(dest_tex));
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
            );
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
            );
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32,
            );
            session.gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32,
            );
            // Explicit bind: bake_slide_to_fbo's inner helpers
            // may leave the binding at default. GLES2 uses
            // FRAMEBUFFER as the single bind point (READ_/DRAW_
            // are GLES3-only).
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo_a));
            session.gl.copy_tex_image_2d(
                glow::TEXTURE_2D, 0, glow::RGBA,
                0, 0, mode_w_u32 as i32, mode_h_u32 as i32, 0,
            );
            session.gl.bind_texture(glow::TEXTURE_2D, None);
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.transition_still_a_tex = Some(dest_tex);
            eprintln!(
                "[perf] snapshot_side_a_captured progress={:.3} dims={}x{}",
                progress, mode_w_u32, mode_h_u32,
            );
            // CMA R2-RANK5 (2026-06-22): with the snapshot
            // committed, endpoint_a's decoder will sit allocated-
            // but-parked for the rest of the fade (snapshot-side-A
            // contract: no more bake_video on side A). Its 1-4
            // cached DMABUF EGLImages (~3MB each via Mesa+vc4)
            // are now dead weight pinning kernel dmabuf refs.
            // Proactively destroy them; the decoder stays alive
            // (LRU may keep it primed for a same-clip wrap
            // around, or a future transition-cancel could
            // resume feeding). If we ever DO need EGLImages
            // again, get_or_init_egl_image lazy-recreates on
            // demand. Per QA RANK 5 framing: "free before bake_b
            // instead of waiting on Arc refcount" -- timing
            // arrives ~bake_b boundary, exactly the CMA-peak
            // moment.
            //
            // snapshot_eligible guarantees endpoint_a is plain
            // Video, so a Video destructure is total. Subagent
            // BLOCKER avoidance: borrow endpoint_a IMMUTABLY
            // (`&endpoint_a`) — the prior `&mut endpoint_a`
            // borrow ended at the inputs_a match (the bake call
            // released the inner reborrows), so a fresh `&`
            // borrow is fine here.
            // 2026-07-04 (Jason device H2 arc): unpin dispatch
            // driven by the pure `unpin_target_for_endpoint_a`.
            // Returns `Decoder` for Video, `BgDecoder` for
            // TextOverVideo (though TextOverVideo can never reach
            // BakeAndCapture per the H2 invariant), and `None`
            // otherwise. Compile-checked by the exhaustive
            // `unpin_target_matches_bake_endpoint_kind` test.
            match unpin_target_for_endpoint_a(endpoint_a_kind, side_a_plan) {
                UnpinTarget::Decoder => {
                    if let TransitionEndpoint::Video { decoder, .. } = &endpoint_a {
                        decoder.unpin_egl_refs();
                    }
                }
                UnpinTarget::BgDecoder => {
                    if let TransitionEndpoint::TextOverVideo { bg_decoder, .. } = &endpoint_a {
                        bg_decoder.unpin_egl_refs();
                    }
                }
                UnpinTarget::None => {}
            }
        }
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
        // 2026-07-04 (Jason device H2 arc): compute the side-B
        // plan via the pure `decide_side_b_plan` for the tick-1
        // journal marker. The runtime dispatch below still uses
        // the existing `use_poster_b` boolean (behavior unchanged
        // from pr38) — the plan is derived from the SAME inputs so
        // the two agree by construction. Emitting the marker at
        // tick 1 lets QA correlate side_a_plan=UseCachedComposite
        // firing WITH side_b_plan=UsePreloadedSlot/UploadPreloaded
        // Input in the sign log (both signals green together).
        let side_b_plan = decide_side_b_plan(SideBInputs {
            endpoint_b_kind,
            preloaded_slot_present_for_this_endpoint: matches!(
                (session.transition_preloaded_first_frame_b_tex, poster_b_video_id),
                (Some((slot_vid, _, _, _)), Some(cur_vid)) if slot_vid == cur_vid,
            ),
            preloaded_input_present: preloaded_first_frame_b.is_some(),
            poster_b_disk_present: poster_b_texture_disk.is_some(),
            cached_pair_b_present: cached_pair_b.is_some(),
        });
        if progress < 0.05 {
            eprintln!(
                "[perf] side_b_plan={:?} endpoint_b_kind={:?} progress={:.3}",
                side_b_plan, endpoint_b_kind, progress,
            );
        }
        let _ = side_b_plan;
        let _: SideBPlan;  // keep the type reference so unused-import doesn't trip
        // r110 stage 3 commit 3.2.2: poster fast-path for bake_b
        // (mirrors bake_a's poster fast-path above; same FROZEN
        // ENTRY contract). When endpoint_b is video-bearing AND
        // we have a poster on disk AND a cached FBO pair, source
        // from the poster unconditionally for the entire transition
        // window. TextOverVideo composes text live on top.
        let use_poster_b = matches!(
            &endpoint_b,
            TransitionEndpoint::Video { .. } | TransitionEndpoint::TextOverVideo { .. }
        ) && poster_b_texture.is_some()
          && cached_pair_b.is_some();
        let (fbo_b, tex_b) = if use_poster_b {
            let (poster_tex, poster_w, poster_h) = poster_b_texture.expect("guarded above");
            let (fbo, tex) = cached_pair_b.expect("guarded above");
            use glow::HasContext;
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
            session.gl.viewport(0, 0, mode_w_u32 as i32, mode_h_u32 as i32);
            session.gl.clear_color(0.0, 0.0, 0.0, 1.0);
            session.gl.clear(glow::COLOR_BUFFER_BIT);
            // poster-fit (2026-06-22): see use_poster_a_now
            // branch for rationale. Same cover-fit fix on the
            // INCOMING side -- this is the path qarl directly
            // observed mis-sized (frozen-entry placeholder
            // before B's live decoder produces frame 0).
            let cover_vbo = cover_quad_vbo(
                session.gl, poster_w, poster_h, mode_w_u32, mode_h_u32,
            )?;
            run_blit_pass_quad(session.gl, poster_tex, cover_vbo)?;
            if matches!(&endpoint_b, TransitionEndpoint::TextOverVideo { .. }) {
                if let Some((slide_id, layers, motion_states)) = text_over_video_b.as_ref() {
                    let slide_id = *slide_id;
                    let layers_len = layers.len();
                    let needs_new = match session.slide_caches.get(&slide_id) {
                        Some(c) => c.glyph.len() != layers_len,
                        None => true,
                    };
                    if needs_new {
                        if let Some(old) = session.slide_caches.remove(&slide_id) {
                            free_slide_render_cache(session.gl, old);
                        }
                        slide_caches_insert(session, slide_id, SlideRenderCache::new(layers_len));
                    }
                    let runtime_glyph_ctx = Some(crate::glyph_cache::RuntimeGlyphCtx {
                        cache: &session.dynamic_glyph_cache,
                        fonts_dir: &session.dynamic_fonts_dir,
                    });
                    let cache = session.slide_caches.get_mut(&slide_id)
                        .expect("inserted above");
                    let wall_clock_unix = current_unix_seconds();
                    paint_slide_with_viewport(
                        session.gl,
                        mode_w_u32, mode_h_u32, 0, 0, mode_w_u32, mode_h_u32,
                        None,
                        layers,
                        Some(motion_states),
                        wall_clock_unix,
                        Some(&mut cache.glyph),
                        Some(&mut session.image_bg_cache),
                        Some(&mut cache.tex),
                        runtime_glyph_ctx,
                    )?;
                }
            }
            // r110 c3.3.1 (subagent BLOCKER-1 fix): only signal
            // recreate for 1080p posters. See bake_a comment.
            // 2026-07-04 (Jason device): additional gate — do NOT
            // signal recreate when `poster_b_texture` came from a
            // freshly-decoded preloaded first frame. The c3.3.1
            // recreate is a workaround for stale posters causing
            // a wedged live decoder on 1080p reels; when we're
            // sourcing a live first frame, the decoder just
            // delivered a real frame at drain time so there's no
            // stale-poster wedge to work around.
            if let Some(vid) = poster_b_video_id {
                if (poster_w >= 1920 || poster_h >= 1080)
                    && !poster_b_is_preloaded_first_frame
                {
                    poster_source_event(vid);
                }
            }
            eprintln!(
                "[perf] poster_b_sourced progress={:.3} dims={}x{} signal_set={} preloaded_first_frame={}",
                progress, poster_w, poster_h,
                (poster_w >= 1920 || poster_h >= 1080) && !poster_b_is_preloaded_first_frame,
                poster_b_is_preloaded_first_frame,
            );
            (fbo, tex)
        } else { let mut bake_b_iterations: u32 = 0;
        loop {
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
                    demuxer,
                    next_sample_idx,
                    frames_decoded,
                    decoder,
                    ..
                } => SlideBakeInputs::Video {
                    demuxer: *demuxer,
                    next_sample_idx: &mut **next_sample_idx,
                    frames_decoded: &mut **frames_decoded,
                    decoder: *decoder,
                },
                TransitionEndpoint::TextOverVideo {
                    bg_demuxer,
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
                        bg_demuxer: *bg_demuxer,
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
                    let deadline_ok = bake_b_start.elapsed() < bake_b_deadline;
                    let iter_ok = bake_b_iterations < PATH_B_MAX_ITERS;
                    let samples_remaining_ok = match &endpoint_b {
                        TransitionEndpoint::Video {
                            demuxer,
                            next_sample_idx,
                            ..
                        }
                        | TransitionEndpoint::TextOverVideo {
                            bg_demuxer: demuxer,
                            bg_next_sample_idx: next_sample_idx,
                            ..
                        } => {
                            // Next bake_video call advances by 1; we
                            // need sample(idx) to be in range for the
                            // upcoming iteration without wrap.
                            **next_sample_idx < demuxer.sample_count()
                        }
                        _ => true, // Text/Image never returns None
                    };
                    if deadline_ok && iter_ok && samples_remaining_ok {
                        std::thread::sleep(std::time::Duration::from_millis(2));
                        continue;
                    }
                    // Caps exhausted. Fall through to the legacy r69
                    // skip + WARN behavior.
                    let elapsed_us = bake_b_start.elapsed().as_micros();
                    let reason = if !samples_remaining_ok {
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
                    crate::hdmi_logic::warn_paint_transition_skip(
                        kind, progress, "endpoint_b_no_frame",
                    );
                    session.gl.delete_framebuffer(fbo_a);
                    session.gl.delete_texture(tex_a);
                    return Ok(false);
                }
                Err(e) => {
                    session.gl.delete_framebuffer(fbo_a);
                    session.gl.delete_texture(tex_a);
                    return Err(e);
                }
            }
        }
        };  // r110 c3.2.2: closes else-block of `if use_poster_b`
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
        let (program, a_pos, a_uv, u_src_a, u_src_b, u_t, u_aspect, u_resolution) = if program_cache_enabled {
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
                cached.u_resolution,
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
            let u_resolution = session.gl.get_uniform_location(program, "u_resolution");
            (program, a_pos, a_uv, u_src_a, u_src_b, u_t, u_aspect, u_resolution)
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
        // 2026-07-03 (Jason device): u_resolution for FS_PIXELATE's
        // fixed-size mosaic block. No-op on shaders that don't
        // declare it. Same LOGICAL dims as u_aspect (session.mode_w
        // / session.mode_h are already swapped for 90/270 rotation).
        session.gl.uniform_2_f32(
            u_resolution.as_ref(),
            mode_w_u32 as f32,
            mode_h_u32 as f32,
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
    // Flip-race fix D (2026-06-22): 3-deep scanout rotation.
    rotate_scanout_3_deep(session, card, new_bo, new_fb, "transition");
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
        // CMA #1 (2026-06-21): demuxer ref replaces the pre-loaded
        // samples slice. bake helpers call demuxer.sample(i) on
        // demand (pread + owned Vec, dropped per-tick).
        demuxer: &'a crate::mp4_demux::Mp4Demuxer,
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
        // CMA #1 (2026-06-21): bg_demuxer ref replaces bg_samples
        // slice. Same on-demand pread pattern as Video variant.
        bg_demuxer: &'a crate::mp4_demux::Mp4Demuxer,
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
    // LEVER 1 (2026-06-24): glyph prewarm moved to the IPC sidecar
    // caller (ipc_main.rs's run_open_and_inner_loop_linux) where the
    // content_root + playlist_path are in scope and the prewarm can
    // be scoped to actually-used codepoints. Pre-LEVER-1 the prewarm
    // ran here unconditionally over a hardcoded DEMO_REEL × ASCII set
    // (~855 glyphs) regardless of what the device actually played.
    //
    // Non-IPC `with_egl_session` callers (the standalone --play-slide,
    // --capture-*, --solid-color, --fade-*, QA visual-verdict snapshots,
    // host-side smoke tests) keep the same zero-prewarm-tax behavior
    // they had before because they never went through this path.
    with_egl_session(card, rotation, work)
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
    // 2026-06-13 offscreen-capture bg-video fix — see
    // `crate::content::capture_video_bg_poster_path` for the why.
    // resolve_slide_bg returns BgKind::Solid for a TextSlide carrying
    // background_video_slide_id (it has no V4L2 plumbing); the offscreen
    // capture path inherits that solid bg and renders BLACK behind the
    // text composite. Substitute the bg with the referenced video's
    // poster.png when one exists; otherwise keep the solid fallback.
    let bg_a_kind = substitute_video_poster_bg(slide_a, bg_a_kind, content_root);
    let bg_b_kind = substitute_video_poster_bg(slide_b, bg_b_kind, content_root);
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
                slide_caches_insert(session, sid, SlideRenderCache::new(n));
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
            // 2026-07-03: u_resolution for FS_PIXELATE's fixed-size
            // mosaic. Same LOGICAL dims as u_aspect.
            gl.uniform_2_f32(
                ccp.u_resolution.as_ref(),
                mode_w as f32,
                mode_h as f32,
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
    // 2026-06-13 offscreen-capture bg-video fix — see the matching site
    // in `capture_sb_transition_mid_to_png` above.
    let bg_a_kind = substitute_video_poster_bg(slide_a, bg_a_kind, content_root);
    let bg_b_kind = substitute_video_poster_bg(slide_b, bg_b_kind, content_root);
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
            // 2026-07-03: u_resolution for pixelate arm.
            let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };

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
                gl.uniform_2_f32(
                    u_resolution.as_ref(),
                    mode_w as f32,
                    mode_h as f32,
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
    // 2026-06-13 offscreen-capture bg-video fix — same substitution as
    // the SB-portable + legacy-3pass paths.
    let bg_a_kind = substitute_video_poster_bg(slide_a, bg_a_kind, content_root);
    let bg_b_kind = substitute_video_poster_bg(slide_b, bg_b_kind, content_root);
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
                // 2026-07-03: u_resolution for pixelate arm.
                gl.uniform_2_f32(
                    ccp.u_resolution.as_ref(),
                    mode_w as f32,
                    mode_h as f32,
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
                // CMA-arc 2026-06-21: wrap via
                // poll_dynamic_glyph_completions.
                let _n = poll_dynamic_glyph_completions(session, 4);
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

    /// PR3 (2026-06-27): logical render-target width in pixels.
    /// Used by `maybe_paint_system_card_overlay` to scale the
    /// normalized card shapes (0..1) to physical pixels.
    pub fn mode_w(&self) -> u16 {
        self.mode_w
    }

    /// PR3 (2026-06-27): logical render-target height in pixels.
    pub fn mode_h(&self) -> u16 {
        self.mode_h
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
        // r110 stage 3 commit 3.1: also evict the poster cache
        // on CMA pressure events. CMA-arc 2026-06-21 C2: cap is
        // 2 = ~17 MB worst-case at 1080p RGBA (8.3 MB/entry).
        // Worth reclaiming alongside the image caches when the
        // r46 mitigation fires.
        let freed_poster = self.poster_cache.len();
        for (_path, (tex, _, _)) in self.image_bg_cache.drain() {
            unsafe { self.gl.delete_texture(tex); }
        }
        for tex in self.image_slide_tex_cache.take_all_textures() {
            unsafe { self.gl.delete_texture(tex); }
        }
        for (_path, (tex, _, _)) in self.poster_cache.drain() {
            unsafe { self.gl.delete_texture(tex); }
        }
        if freed_bg > 0 || freed_slide > 0 || freed_poster > 0 {
            eprintln!(
                "ipc: r46 text-over-video CMA mitigation -- evicted {} image_bg + {} image_slide_tex + {} poster entries",
                freed_bg, freed_slide, freed_poster
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
                    eprintln!(
                        "[perf] frame over budget: delta_ms={} in_transition={} over_budget_total={} observed_total={}",
                        delta_ms,
                        self.in_transition,
                        self.frames_over_budget_total,
                        self.frames_observed_total,
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

/// CMA-arc 2026-06-22 RANK 3: free session-lifetime FBO state
/// that's not currently in use. Pre-RANK-3 these were allocated
/// on first need + held through every static-slide hold + only
/// freed at session teardown — so a reel that briefly hit a
/// non-identity setting OR did one scissored-bake transition
/// would pin ~24-40 MB CMA for the rest of the session.
///
/// All three FBO groups are lazy-ensure (re-allocate automatically
/// on next use), so freeing them is safe. The transition pair +
/// scissored-bake atlas are gated on an idle threshold to avoid
/// freeing-and-realloc churn during active transitions; the
/// scene_fbo is freed immediately when settings return to identity
/// + rotation==0 (the precondition that made scene_fbo unnecessary
/// in the first place per its docstring).
///
/// Called at the top of `paint_and_present_one_frame_for_slide`
/// (static-hold path). NOT called from
/// `paint_and_present_one_transition_frame` — transition ticks
/// stamp `last_transition_fbo_use` themselves; freeing at the top
/// of a transition tick would just immediately re-allocate.
#[cfg(target_os = "linux")]
unsafe fn free_idle_session_fbos(session: &mut EglSession<'_>) {
    use glow::HasContext;
    // Settings returned to identity + rotation==0 → scene_fbo is
    // no longer wired into the render path. Free immediately.
    // The next non-identity frame re-allocates lazily via
    // ensure_scene_fbo. Settings + rotation churn between identity
    // and non-identity per frame would cause alloc/free churn —
    // unrealistic for operator-driven settings; documented assumption.
    if session.current_settings.is_color_identity() && session.rotation == 0 {
        if let Some(fbo) = session.scene_fbo.take() {
            session.gl.delete_framebuffer(fbo);
        }
        if let Some(tex) = session.scene_tex.take() {
            session.gl.delete_texture(tex);
        }
    }

    // 5 seconds gives reels with back-to-back transitions a wide
    // margin against churn; a reel that's actively crossfading
    // re-stamps `last_transition_fbo_use` every tick well within
    // this window. A reel that goes back to a long static hold
    // reclaims after 5s.
    const IDLE_FBO_THRESHOLD: std::time::Duration =
        std::time::Duration::from_secs(5);
    let now = std::time::Instant::now();

    // Transition pair (a+b FBOs + textures) + the snapshot-side-A
    // captured still texture. The pair shares dims via
    // transition_fbo_dims; reset that sentinel alongside the
    // free so the next ensure_transition_fbo_pair sees a fresh
    // cache.
    let transition_idle = session
        .last_transition_fbo_use
        .map_or(true, |t| now.duration_since(t) > IDLE_FBO_THRESHOLD);
    let transition_holds_state =
        session.transition_fbo_a.is_some()
            || session.transition_fbo_b.is_some()
            || session.transition_still_a_tex.is_some();
    if transition_idle && transition_holds_state {
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
        if let Some(tex) = session.transition_still_a_tex.take() {
            session.gl.delete_texture(tex);
        }
        session.transition_fbo_dims = None;
        // Don't clear `last_transition_fbo_use` — the next ensure
        // call will re-stamp; leaving the stale stamp is fine
        // because the gate above is "older than threshold," not
        // an is_some / is_none check.
    }

    // Scissored-bake atlas (2048×2048 RGBA = ~16 MB CMA). Only
    // exercised by `transition_eligible_for_scissored_bake`
    // transitions (text-heavy paths). Hold-only or non-eligible-
    // transition reels reclaim after the idle threshold.
    let bake_idle = session
        .last_scissored_bake_use
        .map_or(true, |t| now.duration_since(t) > IDLE_FBO_THRESHOLD);
    if bake_idle {
        if let Some((fbo, tex)) = session.scissored_bake_atlas.take() {
            session.gl.delete_framebuffer(fbo);
            session.gl.delete_texture(tex);
        }
    }
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
    // CMA-arc 2026-06-22 RANK 3: stamp every transition-tick use so
    // free_idle_session_fbos knows the pair is currently active.
    // Stamping at the TOP (before the dims-change-rebuild branch
    // and the cache-hit return) covers both fresh allocs and warm
    // re-uses.
    session.last_transition_fbo_use = Some(std::time::Instant::now());
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
        // Snapshot-side-A Commit 2.1 (2026-06-21): invalidate
        // the captured still on mid-fade dims change too. A
        // stale-dims still would blit at old dims into the new-
        // dims fbo_a via run_blit_pass -> stretched frozen
        // frame + orphaned-tex leak until the next free hook.
        // Rare (mode change during ~1s fade -- HDMI hot-plug
        // or rotation flip) + cosmetic but a correctness gap
        // QA flagged on independent review. Inline take +
        // delete mirrors the fbo/tex pattern above; can't call
        // the free helper because we already hold &mut to
        // session.gl via the surrounding context.
        if let Some(tex) = session.transition_still_a_tex.take() {
            session.gl.delete_texture(tex);
        }
        session.transition_fbo_dims = None;
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
    Ok((fbo, tex))
}

/// Snapshot-side-A Commit 2 (2026-06-21): free the captured
/// outgoing-video still texture. Called from the ipc_main.rs
/// BeginSlide handler, BeginTransition handler, and the
/// Advance dispatcher when paint_kind != Transition (i.e. a
/// transition just ended or no transition is in flight).
///
/// Idempotent: a None.take() is a no-op, so the per-Advance
/// call costs ~1ns in steady state outside transitions.
///
/// Caller must hold the EGL context current (true for all 3
/// callsites — IPC main thread owns the context).
pub fn free_transition_still_a_tex(session: &mut EglSession) {
    if let Some(tex) = session.transition_still_a_tex.take() {
        unsafe {
            use glow::HasContext;
            session.gl.delete_texture(tex);
        }
    }
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
/// fullscreen blit. Rotation is baked into the quad's vertex
/// positions (see `present_quad_verts`).
///
/// FPS arc cheap win #1 (2026-06-22): dispatches to FS_BLIT (pure
/// passthrough) when brightness == 1.0 AND gamma == 1.0 — the
/// common production case (default settings + video path's
/// hardcoded 1.0,1.0). Avoids the per-pixel clamp+pow in
/// FS_BRIGHT_GAMMA which produces identical pixels for identity
/// transforms. Non-identity callers (operator-applied brightness/
/// gamma) still go through FS_BRIGHT_GAMMA with the real
/// tonemapping. Pixel-bit-identical for the fast path.
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
    // FPS arc cheap win #1 (2026-06-22): bypass FS_BRIGHT_GAMMA's
    // per-pixel clamp+pow when brightness=gamma=1.0 (identity color
    // settings AND identity gamma). The video present path passes
    // hardcoded (1.0, 1.0) — every pixel every frame ran the pow
    // for zero visual difference. Prod gamma is also 1.0 by
    // default. Switching to FS_BLIT (cached_blit_program) removes
    // the pow + clamp from the per-pixel inner loop.
    //
    // Rotation is encoded in present_quad_vbo's vertex positions,
    // not the fragment shader, so the blit shader preserves it
    // verbatim. Zero visual change for the identity case; biggest
    // FPS win per QA's profiling read (Pass2 of every video frame).
    //
    // Non-identity (settings panel applied a tint or gamma): fall
    // through to FS_BRIGHT_GAMMA which has the actual tonemapping.
    // `is_color_identity()` lives on the settings type; here we
    // approximate with float-equality on the canonical "no
    // transform" values (1.0, 1.0). Tolerance is intentional: the
    // settings API stores brightness as u8 percentage, gamma as
    // float; both are explicit values, not computed, so float-eq
    // is correct here. A future regression where someone passes
    // 1.000001 would fall through to FS_BRIGHT_GAMMA (= the
    // pre-fix path, no functional regression).
    let identity = brightness == 1.0 && gamma == 1.0;
    let vbo = present_quad_vbo(gl, rotation)?;
    if identity {
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
        return Ok(());
    }
    let cgp = cached_bright_gamma_program(gl)?;
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
/// blit it via the BT.709 NV12→RGB shader into the currently-bound
// r110 c3.1.1 (2026-06-11): stale "BT.601" corrected to "BT.709";
// the shader at hdmi_logic.rs:3133-3137 uses ITU-R BT.709 Annex B
// limited-range coefficients (1.5748 / 0.1873 / 0.4681 / 1.8556).
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
unsafe fn bake_video_slide_to_current_fbo(
    session: &mut EglSession,
    demuxer: &crate::mp4_demux::Mp4Demuxer,
    next_sample_idx: &mut usize,
    frames_decoded: &mut usize,
    decoder: &crate::v4l2::Decoder,
    mode_w: u32,
    mode_h: u32,
) -> Result<Option<&'static str>> {
    use glow::HasContext;
    let profile_first = *next_sample_idx == 1
        && *frames_decoded == 0
        && std::env::var("OPENMARQUEE_FIRSTFRAME_PROFILE")
            .ok()
            .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
            .unwrap_or(false);
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
    let sample_count = demuxer.sample_count();
    if sample_count == 0 {
        // Defensive: prime_video_decoder bails on a zero-sample
        // MP4, so a decoder with no samples shouldn't reach here.
        return Ok(None);
    }
    if *next_sample_idx >= sample_count {
        *next_sample_idx = 0;
        // r46.3 (2026-06-02): the wrap-at-bake handler stays as the
        // minimal "wrap back to sample 0" pattern. The actual
        // V4L2-state reset (STREAMOFF + clear drained + STREAMON +
        // re-QBUF + re-feed SPS+PPS+IDR primer) lives in
        // reprime_video_decoder_for_loop and is invoked from the IPC
        // dispatcher BEFORE this bake call (when it detects the
        // wrap condition). Post-CMA-#1 bake gets the &Mp4Demuxer
        // directly (was &[Sample]) but still defers re-prime to the
        // dispatcher — primer call requires SPS/PPS + V4L2-state
        // reset that crosses the bake boundary.
    }
    // CMA #1 (2026-06-21): pread the current sample from the
    // streaming demuxer instead of indexing a pre-loaded Vec.
    // `owned` lives until end of function (~33ms tick budget);
    // dropped immediately after the V4L2 feed.
    let owned = demuxer.sample(*next_sample_idx)
        .with_context(|| format!("read sample {}", *next_sample_idx))?;
    decoder
        .feed(&owned)
        .with_context(|| format!("feed sample {}", *next_sample_idx))?;
    *next_sample_idx += 1;
    drop(owned);
    if let Some(t) = t_feed_start {
        eprintln!("[firstframe] feed={:.2}ms", t.elapsed().as_secs_f64() * 1000.0);
    }
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
    let mut frame_opt: Option<crate::v4l2::Frame> = None;
    // r110 stage 2 (2026-06-11): distinguish EOS (next_frame
    // Ok(None)) from EAGAIN-exhausted (10x3ms loop ran out) so
    // the Ok(None) counters below can attribute correctly.
    let mut eos_seen = false;
    for _ in 0..10 {
        match decoder.next_frame() {
            Ok(Some(f)) => {
                frame_opt = Some(f);
                break;
            }
            Ok(None) => {
                eos_seen = true;
                break;
            }
            Err(e) if e.to_string().contains("EAGAIN") => {
                std::thread::sleep(std::time::Duration::from_millis(3));
            }
            Err(e) => return Err(e).context("next_frame"),
        }
    }
    if let Some(t) = t_dqbuf_start {
        eprintln!("[firstframe] dqbuf={:.2}ms", t.elapsed().as_secs_f64() * 1000.0);
    }
    let Some(frame) = frame_opt else {
        // r110 stage 2: increment the appropriate Ok(None)
        // counter. `eos_seen` distinguishes "normal end-of-clip"
        // from "EAGAIN-exhausted, codec didn't deliver in time"
        // — the latter is QA's H4 steady-state lag indicator.
        if eos_seen {
            BAKE_VIDEO_OK_NONE_EOS.fetch_add(1, Ordering::Relaxed);
        } else {
            BAKE_VIDEO_OK_NONE_EAGAIN.fetch_add(1, Ordering::Relaxed);
        }
        // No frame ready this tick. Caller should skip swap+commit.
        // Sample the dqbuf even on no-frame ticks so the EAGAIN wait
        // shows up in the histogram.
        crate::profile::record_phase(
            "paint_bake_video_dqbuf",
            t_phase.elapsed().as_nanos() as u64,
        );
        return Ok(None);
    };
    crate::profile::record_phase(
        "paint_bake_video_dqbuf",
        t_phase.elapsed().as_nanos() as u64,
    );
    let t_phase = std::time::Instant::now();
    let f_w = frame.width();
    let f_h = frame.height();
    // judder-instrument (2026-06-22): fingerprint the FIRST 2 live
    // frames so QA can correlate against poster_cache_loaded fp_r
    // + drain_one_capture_for_preload fp_y. qarl observed a
    // BACKWARD jump at the poster->live handoff (live appears
    // earlier than poster). This probe answers:
    //   (i) is poster_b actually frame 0 (matches drained
    //       fingerprint)?
    //   (ii) what frame does the first-displayed live show
    //        (matches drained+1's fingerprint? matches poster's
    //        fingerprint? something else)?
    // *frames_decoded is the pre-increment count (still pre-bump
    // here). Logs gated at < 2 so we get cold-start first +
    // preload first AND first-after-cold-start = 2 lines max
    // per slide play instance.
    if *frames_decoded < 2 {
        let y_plane = frame.y_plane();
        let stride = frame.stride() as usize;
        let fp = fingerprint_9_points(
            y_plane, stride, f_w as usize, f_h as usize, 1,
        );
        eprintln!(
            "[perf] live_frame_fp frames_decoded_pre={} next_sample_idx_post={} \
             frame_dims={}x{} stride={} fp_y={:?}",
            *frames_decoded, *next_sample_idx, f_w, f_h, stride, fp,
        );
    }
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
        // CMA #1 (2026-06-21): demuxer ref replaces samples slice.
        demuxer: &'a crate::mp4_demux::Mp4Demuxer,
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
        // CMA #1 (2026-06-21): bg_demuxer ref replaces bg_samples
        // slice.
        bg_demuxer: &'a crate::mp4_demux::Mp4Demuxer,
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
                slide_caches_insert(
                    session,
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
            demuxer,
            next_sample_idx,
            frames_decoded,
            decoder,
        } => {
            let (fbo, tex) = prepare_bake_fbo_pair(session.gl, mode_w, mode_h, existing_fbo_pair)?;
            let paint_result = bake_video_slide_to_current_fbo(
                session,
                demuxer,
                next_sample_idx,
                frames_decoded,
                decoder,
                mode_w,
                mode_h,
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
            bg_demuxer,
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
                slide_caches_insert(
                    session,
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
                bg_demuxer,
                bg_next_sample_idx,
                bg_frames_decoded,
                bg_decoder,
                mode_w,
                mode_h,
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
            let u_resolution = gl.get_uniform_location(program, "u_resolution");
            gl.uniform_1_i32(u_src_a.as_ref(), 0);
            gl.uniform_1_i32(u_src_b.as_ref(), 1);
            gl.uniform_1_f32(u_t.as_ref(), t);
            gl.uniform_1_f32(
                u_aspect.as_ref(),
                (mode_w as f32) / (mode_h as f32),
            );
            gl.uniform_2_f32(
                u_resolution.as_ref(),
                mode_w as f32,
                mode_h as f32,
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
        // 2026-07-03: u_resolution for pixelate arm.
        let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };

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
                slide_caches_insert(session, sid, SlideRenderCache::new(n));
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
                // 2026-07-03: u_resolution for pixelate arm.
                gl.uniform_2_f32(
                    u_resolution.as_ref(),
                    mode_w_u32 as f32,
                    mode_h_u32 as f32,
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
                slide_caches_insert(session, sid, SlideRenderCache::new(n));
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
        let u_resolution_loc = csp.u_resolution.clone();
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
                    // 2026-07-03: u_resolution for pixelate arm.
                    // Same LOGICAL dims as u_aspect.
                    gl.uniform_2_f32(
                        u_resolution_loc.as_ref(),
                        mode_w_u32 as f32,
                        mode_h_u32 as f32,
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
                slide_caches_insert(session, sid, SlideRenderCache::new(n));
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
                    // 2026-07-03: u_resolution for pixelate arm.
                    gl.uniform_2_f32(
                        active_ccp.u_resolution.as_ref(),
                        mode_w_u32 as f32,
                        mode_h_u32 as f32,
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
/// CMA-arc 2026-06-21 C4: populated incrementally by
/// `ensure_msdf_atlas_uploaded` (lazy-per-family upload). Cleared
/// by `clear_msdf_lookup` at session teardown BEFORE
/// `delete_owned_msdf_atlases` so a stale lookup can't outlive
/// the underlying NativeTexture handles. The pre-arc bring-up
/// upload (`upload_all` + `populate_msdf_lookup`) was retired
/// to free ~30 MB CMA upfront.
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
    /// CMA-arc 2026-06-21 C4: per-thread OWNED MsdfAtlasGl store.
    /// Pre-arc the owned MsdfAtlasGl entries lived on
    /// `session.msdf_atlases` (a Vec<MsdfAtlasGl> field). Lazy-
    /// upload via `ensure_msdf_atlas_uploaded` happens from
    /// paint_slide_with_viewport which receives only `&gl` (not
    /// `&mut session`), so the owned Vec moved to a thread_local
    /// to match. Teardown helper `delete_owned_msdf_atlases` drains
    /// + calls `sdf_atlas_gl::delete_all` on the contents while
    /// the GL context is still bound (`with_egl_session` cleanup
    /// runs both this AND clear_msdf_lookup before the GL
    /// context tears down).
    static MSDF_ATLAS_OWNED: std::cell::RefCell<Vec<crate::sdf_atlas_gl::MsdfAtlasGl>> =
        const { std::cell::RefCell::new(Vec::new()) };
}

/// Process-wide parsed atlas set (CPU-side; `atlas_rgb` is 'static
/// because the bytes come from `include_bytes!`). Loaded once on
/// the first session bring-up; reused thereafter. Decoupling from
/// the GL-side `MSDF_ATLAS_LOOKUP` lets host tests + layout-only
/// paths (which don't need a GL context) reach the same data.
static MSDF_ATLASES_CPU: std::sync::OnceLock<Vec<crate::sdf_atlas::MsdfAtlas>> =
    std::sync::OnceLock::new();

// CMA-arc 2026-06-21 C4: `populate_msdf_lookup` (eager bring-up
// publish of all 23 atlas textures) retired. The thread_local is
// now populated incrementally by `ensure_msdf_atlas_uploaded` on
// first text draw of each font family. MSDF_ATLASES_CPU's
// OnceLock get_or_init is mirrored inside `ensure_msdf_atlas_uploaded`
// so the CPU-side parse fires on first lazy upload.

fn clear_msdf_lookup() {
    MSDF_ATLAS_LOOKUP.with(|c| c.borrow_mut().clear());
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

/// CMA-arc 2026-06-21 C3: leak-safe wrapper around the bounded
/// `slide_caches` LruMap insert. Pre-arc `slide_caches` was an
/// unbounded HashMap so insert never evicted; post-arc the LruMap
/// returns the LRU entry via `InsertOutcome::evicted_lru` when at
/// capacity and the key is new. The evicted SlideRenderCache holds
/// GL texture handles (per-layer tex Vec + bg_tex + first_frame_tex)
/// that MUST be released via `free_slide_render_cache` while the
/// GL context is bound; this helper does that atomically with the
/// insert.
///
/// Behavior:
///   * If the key already existed (replace), `InsertOutcome::replaced`
///     is Some — but every call site explicitly removes the prior
///     entry via `slide_caches.remove(&id)` + free_slide_render_cache
///     BEFORE inserting, so the post-remove insert sees an empty
///     slot and `replaced` is None. (Documented + asserted in the
///     debug build below.)
///   * If at capacity with a new key, `evicted_lru` is Some — pass
///     it to free_slide_render_cache.
///   * Else no cleanup needed.
#[cfg(target_os = "linux")]
fn slide_caches_insert(
    session: &mut EglSession<'_>,
    slide_id: uuid::Uuid,
    cache: SlideRenderCache,
) {
    let outcome = session.slide_caches.insert(slide_id, cache);
    // Call sites remove-then-insert when the key existed — so on
    // this path `replaced` must be None. If it isn't, a caller
    // missed the remove and the GL texture in the replaced value
    // would leak. Debug-assert; in release we still drop the
    // texture via the LruMap to avoid the leak.
    debug_assert!(
        outcome.replaced.is_none(),
        "slide_caches_insert: caller skipped the remove-before-insert pattern; \
         slide_id={slide_id}"
    );
    if let Some(evicted) = outcome.evicted_lru {
        free_slide_render_cache(session.gl, evicted);
    }
    // Release-mode fallback in case the debug_assert above is
    // disabled and a future caller skips the remove: free the
    // replaced cache to prevent the leak. Costs one extra
    // function call on the no-replaced fast path.
    if let Some(replaced) = outcome.replaced {
        free_slide_render_cache(session.gl, replaced);
    }
}

/// CMA-arc 2026-06-21: wrapper around `GlyphCache::poll_completions`
/// that publishes the (possibly lazy-allocated) dynamic atlas
/// textures to `DYNAMIC_ATLAS_LOOKUP` / `DYNAMIC_ATLAS_COLR_LOOKUP`
/// after the call. Pre-arc the unconditional bring-up alloc
/// published the textures once at session start; now the pages are
/// lazy-allocated inside `poll_completions` (glyph_cache.rs) on
/// first Ready completion of that mode, so the lookup must be
/// re-checked after each poll. The publish is idempotent (a
/// thread_local set with the same handle), so the cost after the
/// first allocation is a single borrow_mut.
#[cfg(target_os = "linux")]
fn poll_dynamic_glyph_completions(
    session: &mut EglSession<'_>,
    max_uploads_per_call: usize,
) -> usize {
    let uploaded = session.dynamic_glyph_cache.poll_completions(
        session.gl,
        &mut session.dynamic_atlas_page_msdf,
        &mut session.dynamic_atlas_page_colr,
        max_uploads_per_call,
    );
    if let Some(tex) = session.dynamic_atlas_page_msdf.texture() {
        populate_dynamic_atlas_lookup(tex);
    }
    if let Some(tex) = session.dynamic_atlas_page_colr.texture() {
        populate_dynamic_atlas_colr_lookup(tex);
    }
    uploaded
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
///
/// CMA-arc 2026-06-21 C4: pure lookup (no `&mut` / no GL work).
/// The caller MUST have invoked `ensure_msdf_atlas_uploaded` (or
/// `ensure_msdf_atlas_for_family_or_default`) earlier in the same
/// frame for any family it queries — otherwise this returns
/// `None` for never-touched families and the call site's
/// `or_else(|| msdf_atlas_for_family("Inter"))` fallback fires.
fn msdf_atlas_for_family(
    family: &str,
) -> Option<(glow::NativeTexture, &'static crate::sdf_atlas::MsdfAtlas)> {
    let stem = font_family_to_atlas_stem(family)?;
    let cpu = MSDF_ATLASES_CPU.get()?;
    let atlas = crate::sdf_atlas::atlas_for_stem(cpu, stem)?;
    let tex = MSDF_ATLAS_LOOKUP.with(|c| {
        c.borrow()
            .iter()
            .find(|(s, _)| s == stem)
            .map(|(_, t)| *t)
    })?;
    Some((tex, atlas))
}

/// CMA-arc 2026-06-21 C4: lazy-upload the static MSDF atlas for
/// `family` if it isn't already uploaded. Returns `Some(tex)` on
/// success (already-uploaded OR newly-uploaded), `None` if the
/// family isn't in the catalog OR the on-the-fly upload failed.
/// Idempotent.
///
/// MSDF_ATLASES_CPU (the CPU-side parsed atlas Vec backed by
/// `include_bytes!` slices) is initialized on first call via the
/// OnceLock's get_or_init — cheap, parse-only, no I/O.
///
/// Per-family GPU upload pays the ~1.3 MB CMA cost only on first
/// touch + transfers ownership of the new MsdfAtlasGl to the
/// `MSDF_ATLAS_OWNED` thread_local Vec so the existing teardown
/// path (`delete_owned_msdf_atlases` in cleanup_resources) cleans
/// up the texture handles symmetrically. The MSDF_ATLAS_LOOKUP
/// thread_local is appended in lock-step so `msdf_atlas_for_family`
/// finds the new entry.
///
/// Takes `&glow::Context` (not `&mut session`) so all 3 paint-
/// site callers — which receive `gl` as a parameter but not
/// `session` — can invoke without a refactor.
#[cfg(target_os = "linux")]
fn ensure_msdf_atlas_uploaded(
    gl: &glow::Context,
    family: &str,
) -> Option<glow::NativeTexture> {
    let stem = font_family_to_atlas_stem(family)?;
    let cpu = MSDF_ATLASES_CPU.get_or_init(|| {
        crate::sdf_atlas::load_all_atlases().unwrap_or_default()
    });
    let atlas = crate::sdf_atlas::atlas_for_stem(cpu, stem)?;
    // Fast path: already uploaded.
    if let Some(tex) = MSDF_ATLAS_LOOKUP.with(|c| {
        c.borrow()
            .iter()
            .find(|(s, _)| s == stem)
            .map(|(_, t)| *t)
    }) {
        return Some(tex);
    }
    // Slow path: lazy upload. Per-family ~1.3 MB CMA.
    match crate::sdf_atlas_gl::upload_one(gl, atlas) {
        Ok(gl_atlas) => {
            let tex = gl_atlas.tex;
            let stem_str = gl_atlas.stem.clone();
            MSDF_ATLAS_LOOKUP.with(|c| {
                c.borrow_mut().push((stem_str, tex))
            });
            MSDF_ATLAS_OWNED.with(|c| {
                c.borrow_mut().push(gl_atlas)
            });
            eprintln!("msdf: lazy-uploaded atlas {stem} (family={family:?})");
            Some(tex)
        }
        Err(e) => {
            eprintln!("msdf: lazy upload {stem} failed: {e}");
            None
        }
    }
}

/// CMA-arc 2026-06-21 C4: ensure the per-family atlas is uploaded;
/// if the requested family isn't in the catalog OR upload failed,
/// ensure the "Inter" fallback is uploaded so the call site's
/// `msdf_atlas_for_family("Inter")` or_else chain has a target.
#[cfg(target_os = "linux")]
fn ensure_msdf_atlas_for_family_or_default(
    gl: &glow::Context,
    family: &str,
) {
    if ensure_msdf_atlas_uploaded(gl, family).is_some() {
        return;
    }
    // Family-specific upload didn't succeed — pre-warm Inter for
    // the fallback. If family already IS "Inter", the inner
    // is_some() check above short-circuits + we don't reach this.
    let _ = ensure_msdf_atlas_uploaded(gl, "Inter");
}

/// CMA-arc 2026-06-21 C4: drain the per-thread MSDF_ATLAS_OWNED
/// Vec and delete every uploaded texture via
/// `sdf_atlas_gl::delete_all`. Called from `cleanup_resources` at
/// session teardown while the GL context is still bound, after
/// `clear_msdf_lookup` has cleared the thread_local lookup.
#[cfg(target_os = "linux")]
fn delete_owned_msdf_atlases(gl: &glow::Context) {
    MSDF_ATLAS_OWNED.with(|c| {
        let mut owned = c.borrow_mut();
        crate::sdf_atlas_gl::delete_all(gl, &mut owned);
    });
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
    /// 2026-07-03 (Jason device): u_resolution = (width, height) in
    /// device pixels. Bound alongside u_aspect on every draw site;
    /// FS_PIXELATE uses it to convert its fixed 10 px block into
    /// UV space. Resolves to None for shaders that don't declare
    /// it (silent no-op bind).
    pub u_resolution: Option<glow::NativeUniformLocation>,
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
        let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };
        let entry = CachedLegacyTransitionProgram {
            program,
            a_pos,
            a_uv,
            u_src_a,
            u_src_b,
            u_t,
            u_aspect,
            u_resolution,
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
    /// 2026-07-03 (Jason device): u_resolution = (mode_w, mode_h).
    /// FS_PIXELATE uses it to keep its mosaic at a fixed
    /// device-pixel size across resolutions. Silent no-op on
    /// arms that don't declare it.
    u_resolution: Option<glow::NativeUniformLocation>,
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
        let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };
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
            u_resolution,
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
/// the BT.709 limited-range NV12 -> RGB shader. Caller binds the
// r110 c3.1.1 (2026-06-11): stale "BT.601" corrected to "BT.709";
// the shader at hdmi_logic.rs:3133-3137 is ITU-R BT.709 Annex B.
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
    let (egl_image, suppress_destroy_at_end) = if let Some((decoder, idx)) = egl_image_cache {
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
        let (handle, _created) = decoder.get_or_init_egl_image(idx, create_one)?;
        (handle.image, true)
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
        (img, false)
    };
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
    let tex = match gl.create_texture() {
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
    gl.active_texture(glow::TEXTURE0);
    gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, Some(tex));
    gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
    gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
    gl.tex_parameter_i32(GL_TEXTURE_EXTERNAL_OES, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
    // Associate the EGLImage with the bound external-OES texture.
    // From this point the texture samples the dma_buf bytes
    // directly -- zero CPU copy.
    (eps.image_target_texture_2d)(GL_TEXTURE_EXTERNAL_OES, egl_image);

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

    // Teardown ordering: unbind texture, delete texture, THEN
    // destroy the EGLImage. The driver keeps the dma_buf reference
    // alive via the EGLImage until destroy. Frame::Drop's re-QBUF
    // is what re-enqueues the buffer index for the next decode; the
    // EGLImage ref-count is dropped here so the kernel can release
    // the dma_buf at the right moment.
    gl.bind_texture(GL_TEXTURE_EXTERNAL_OES, None);
    gl.delete_texture(tex);
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
    // 2026-07-03 (Jason device): `marquee` removed. The pre-warm
    // list is iterated linearly; no downstream code indexes into it
    // by ordinal, so the N → N-1 shrink is safe.
    const TRANSITION_KINDS: &[&str] = &[
        "cut", "fade", "wipe", "iris", "dissolve", "pixelate", "scanline",
        "halftone", "glitch", "slide", "push", "scroll", "blinds", "flip",
        "shutter",
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
/// Hardcoded fallback prewarm set. Used when the active-playlist
/// scoping pass fails (missing playlist.json, parse error, etc.) so
/// the prewarm still primes a reasonable subset rather than running
/// nothing. Mirrors the previous (pre-LEVER-1) hardcoded behavior:
/// all printable ASCII × the seed demo reel's 9 fonts.
///
/// Stems match hdmi_logic::font_family_to_filename's basename-
/// minus-extension. Keep in sync with seed.py _DEMO_REEL's
/// font_family set + FALLBACK_FONT_STEMS if either changes.
#[cfg(target_os = "linux")]
const DEMO_REEL_FALLBACK_STEMS: &[&str] = &[
    "anton",
    "alfa-slab-one",
    "bowlby-one-sc",
    "playfair-display",
    "vt323",
    "permanent-marker",
    "caveat-brush",
    "jetbrains-mono",
    "dejavu-sans",
];

#[cfg(target_os = "linux")]
const FALLBACK_PRINTABLE_ASCII_START: u32 = 0x20;
#[cfg(target_os = "linux")]
const FALLBACK_PRINTABLE_ASCII_END: u32 = 0x7E;

/// LEVER 1 (2026-06-24): build the (stem, codepoint) prewarm set
/// from the live playlist + content dir. On any fs / parse failure,
/// returns None so the caller can fall back to the hardcoded demo-
/// reel scope. The scoping pass is best-effort — if any part fails,
/// we'd rather over-prewarm (fallback) than under-prewarm (empty).
///
/// Iterates EVERY playlist in playlist.json (not just the actively-
/// scheduled one). Reason: the active playlist switches on
/// schedules without renderer restart, and we don't want a schedule
/// switch to find the next playlist's codepoints un-prewarmed and
/// trip the per-slide SD-thrash QA's localizing. Iterating all
/// playlists bounds the scope to "what the operator built" instead
/// of "every printable ASCII × every installed font" — still a big
/// reduction on most real reels (e.g. the FYS 19-slide reel uses
/// ~50 distinct codepoints across 3-4 fonts vs the hardcoded 855).
#[cfg(target_os = "linux")]
fn build_prewarm_scope_from_playlists(
    content_root: &std::path::Path,
    playlist_path: &std::path::Path,
) -> Option<Vec<(String, u32)>> {
    let env = match crate::content::load_playlist(playlist_path) {
        Ok(e) => e,
        Err(e) => {
            eprintln!(
                "glyph-prewarm: scope-from-playlists FAILED to load {} ({e:#}); falling back to demo-reel ASCII",
                playlist_path.display(),
            );
            return None;
        }
    };

    // Collect every (Option<font_family>, text) pair across all
    // text layers across all referenced TextSlides. Non-text items
    // are skipped silently (find_text_slide returns Ok(None) for
    // image/video/etc.). Items that fail to load (rare: race with
    // operator-delete) are skipped — first such per call logs
    // once for the journal breadcrumb so a silent
    // under-prewarm doesn't go un-explained.
    let mut pairs: Vec<(Option<String>, String)> = Vec::new();
    let mut item_err_logged = false;
    let mut item_err_count: u32 = 0;
    for playlist in &env.playlists {
        for item_ref in &playlist.items {
            match crate::content::find_text_slide(content_root, item_ref.item_id) {
                Ok(Some(slide)) => {
                    for layer in &slide.text_layers {
                        pairs.push((layer.font_family.clone(), layer.text.clone()));
                    }
                }
                Ok(None) => {} // not a TextSlide (image/video/etc.)
                Err(e) => {
                    item_err_count += 1;
                    if !item_err_logged {
                        item_err_logged = true;
                        eprintln!(
                            "glyph-prewarm: scope-from-playlists skipped item {} (first item err; subsequent errs counted): {e:#}",
                            item_ref.item_id,
                        );
                    }
                }
            }
        }
    }
    if item_err_count > 0 {
        eprintln!(
            "glyph-prewarm: scope-from-playlists total skipped_items_with_err={item_err_count} (under-prewarm bounded; runtime tofus those codepoints)",
        );
    }

    if pairs.is_empty() {
        eprintln!(
            "glyph-prewarm: scope-from-playlists found ZERO TextSlide layers across {} playlists; falling back to demo-reel ASCII",
            env.playlists.len(),
        );
        return None;
    }

    // Borrow the (Option<&str>, &str) view the pure scope-builder
    // wants. Owned strings stay alive in `pairs` until the function
    // returns.
    let layer_views = pairs
        .iter()
        .map(|(family, text)| (family.as_deref(), text.as_str()));
    Some(crate::hdmi_logic::build_prewarm_scope_from_text_layers(
        layer_views,
        "anton", // catalog default (mirrors FontCatalog::new in ipc_main.rs)
    ))
}

/// LEVER 1 (2026-06-24): enqueue a prewarm set into the dynamic
/// glyph cache. Pure enqueue — does NOT drain. The IPC inner loop's
/// per-Advance `poll_dynamic_glyph_completions(session, 4)` drains
/// the queue concurrently with playback (preserves the Issue-1
/// enqueue-only model from the c9f391b → 63d9bef stability arc).
///
/// `scope_label` is a short, log-friendly tag identifying the
/// source ("playlist-scoped" / "demo-reel-fallback") so QA can
/// grep journals to confirm which path fired.
#[cfg(target_os = "linux")]
fn enqueue_prewarm_scope(
    session: &mut EglSession,
    scope: &[(String, u32)],
    scope_label: &str,
) {
    use crate::glyph_cache::{font_family_id_from_stem, GlyphKey, RenderMode};

    let t0 = std::time::Instant::now();
    let mut requested: u64 = 0;
    let mut skipped_missing_font: u32 = 0;
    let mut unique_fonts: std::collections::HashSet<&str> = std::collections::HashSet::new();

    for (stem, codepoint) in scope {
        let font_path = session.dynamic_fonts_dir.join(format!("{stem}.ttf"));
        if !font_path.exists() {
            // First miss per stem logs; subsequent codepoints of
            // the same missing stem stay quiet (avoid 95-line log
            // spam for a single missing font).
            if unique_fonts.insert(stem.as_str()) {
                eprintln!(
                    "glyph-prewarm: skip {stem} -- font file not found at {font_path:?}"
                );
            }
            skipped_missing_font += 1;
            continue;
        }
        unique_fonts.insert(stem.as_str());
        let fid = font_family_id_from_stem(stem);
        let key = GlyphKey {
            font_family_id: fid,
            codepoint: *codepoint,
            render_mode: RenderMode::Msdf,
        };
        let _ = session
            .dynamic_glyph_cache
            .get_or_request(key, || font_path.clone());
        requested += 1;
    }

    let enqueue_us = t0.elapsed().as_micros();
    eprintln!(
        "glyph-prewarm: enqueued {requested} glyphs across {} fonts (scope={scope_label}, skipped_missing_codepoints={skipped_missing_font}) in {enqueue_us}us; \
         draining IN BACKGROUND via IPC poll_dynamic_glyph_completions (sidecar boot gate clearing now)",
        unique_fonts.len(),
    );
}

/// Build the hardcoded DEMO_REEL × printable-ASCII fallback scope.
/// Used when the playlist-derived scoping pass fails. Same total
/// size as the pre-LEVER-1 prewarm (~855 = 9 fonts × 95 ASCII).
#[cfg(target_os = "linux")]
fn build_demo_reel_fallback_scope() -> Vec<(String, u32)> {
    let mut scope = Vec::with_capacity(
        DEMO_REEL_FALLBACK_STEMS.len()
            * ((FALLBACK_PRINTABLE_ASCII_END - FALLBACK_PRINTABLE_ASCII_START + 1) as usize),
    );
    for stem in DEMO_REEL_FALLBACK_STEMS {
        for cp in FALLBACK_PRINTABLE_ASCII_START..=FALLBACK_PRINTABLE_ASCII_END {
            scope.push((stem.to_string(), cp));
        }
    }
    scope
}

/// LEVER 1 (2026-06-24): active-reel-scoped glyph rasterization
/// prewarm. Replaces the pre-LEVER-1 unconditional 855-glyph
/// (9 fonts × 95 ASCII) enqueue with a scope derived from the
/// playlist's actual TextSlide content.
///
/// Two-stage prewarm pipeline:
///   1. Try `build_prewarm_scope_from_playlists` (reads
///      playlist.json + per-item content). On the FYS 19-slide
///      reel the scope is ~50 codepoints across 3-4 fonts =
///      ~150-200 enqueues. On a small text reel it can drop
///      below 100.
///   2. On any failure (no playlist.json, all items non-text,
///      parse error), fall back to the hardcoded demo-reel
///      scope so the prewarm never silently does nothing.
///
/// Pre-LEVER-1 boot-burst pressure was ~5.9 MB of in-flight
/// rgba_bytes Completion::Ready messages on the worker channel
/// (855 × 6912 B per RGB8 48² MSDF cell). Scoped to ~150
/// enqueues that's ~1 MB peak — about an 80% reduction on the
/// boot working-set spike that QA's text-reel SD-thrash
/// localization pointed at. LEVER 2 (Python lazy-imports) has
/// already removed the steady-state thrash; LEVER 1 closes
/// the boot-burst delta + buys headroom for the full 19-slide
/// FYS reel to fit without re-thrashing.
///
/// Preserves the Issue-1 enqueue-only behavior: the IPC inner
/// loop's existing per-Advance `poll_dynamic_glyph_completions
/// (session, 4)` drains the queue concurrently with playback.
/// Open-response latency is unaffected.
#[cfg(target_os = "linux")]
pub fn prewarm_glyph_rasterization_scoped(
    session: &mut EglSession,
    content_root: &std::path::Path,
    playlist_path: &std::path::Path,
) {
    match build_prewarm_scope_from_playlists(content_root, playlist_path) {
        Some(scope) => enqueue_prewarm_scope(session, &scope, "playlist-scoped"),
        None => {
            let scope = build_demo_reel_fallback_scope();
            enqueue_prewarm_scope(session, &scope, "demo-reel-fallback");
        }
    }
}

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
    // CMA-arc 2026-06-22 RANK 3: stamp every scissored-bake use so
    // free_idle_session_fbos knows the atlas is currently active.
    session.last_scissored_bake_use = Some(std::time::Instant::now());
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
    /// 2026-07-03 (Jason device): u_resolution = (mode_w, mode_h).
    /// Used by the pixelate arm to convert its fixed 10 px block
    /// into UV. Silent no-op on other kinds.
    u_resolution: Option<glow::NativeUniformLocation>,
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
        let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };
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
            u_resolution,
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
        let u_resolution = unsafe { gl.get_uniform_location(program, "u_resolution") };
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
            u_resolution,
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
    /// r62 (2026-06-05): cached COMPOSITED first-frame texture for
    /// text-over-video slides. Captured at the end of the FIRST
    /// successful live paint of a slide via glCopyTexImage2D from
    /// the composite framebuffer (scene FBO when rotated/non-
    /// identity, default fb otherwise). On a subsequent
    /// BeginSlide for the same id (e.g. cycling playlist), the
    /// next first-frame paint short-circuits to a fast blit of
    /// this texture, then the live decoder hands off via the
    /// existing frames_decoded > 0 path.
    ///
    /// Dimensions: mode_w * mode_h * 4 bytes (RGBA8). FYS panel
    /// (1360x768) = ~4.17 MB. The 19-slide FYS reel with each
    /// slide cached worst-case = ~79 MB.
    ///
    /// IMPORTANT subagent caveat (r62 review): Pi Zero 2 W is a
    /// UMA SoC — there is no separate "GPU VRAM." The cma=256M
    /// cmdline carves CMA out of the 512 MB system RAM for V4L2 /
    /// DRM scanout / GBM; GLES texture allocations come from the
    /// REMAINING ~256 MB shared with kernel + userspace + Python
    /// backend + renderer. The cached textures DO NOT compete
    /// with CMA (they're regular kernel-managed buffers), but
    /// they DO compete with non-CMA system memory.
    ///
    /// session.slide_caches is currently an unbounded HashMap
    /// with no LRU. If empirical measurement on real hardware
    /// shows total first_frame_tex VRAM crossing ~50 MB, an N-
    /// most-recent-slides ring buffer becomes the right follow-
    /// up. For r62 + the 4-video qarl playlist (~17 MB), no
    /// bound is needed.
    ///
    /// Cache lifetime is tied to SlideRenderCache itself:
    /// invalidation paths that drop the SlideRenderCache (mtime
    /// change, glyph atlas reload, evict_other_video_state-driven
    /// BeginSlide for a different slide) also drop this texture
    /// via free_slide_render_cache below.
    pub first_frame_tex: Option<glow::NativeTexture>,
}

impl SlideRenderCache {
    pub fn new(layer_count: usize) -> Self {
        let mut glyph: GlyphCache = Vec::with_capacity(layer_count);
        glyph.resize_with(layer_count, || None);
        let mut tex: TextureCache = Vec::with_capacity(layer_count);
        tex.resize_with(layer_count, || None);
        Self { glyph, tex, bg_tex: None, first_frame_tex: None }
    }
}

/// Free GL textures owned by a SlideRenderCache being removed
/// from session.slide_caches (2026-05-09 atlas SB bg-cache;
/// r62 first-frame composite cache).
/// Must be called while the GL context is still bound. Used by
/// the multiple slide_caches.remove call sites that previously
/// inlined `for slot in old.tex { delete_texture(t) }` and now
/// also need to free `bg_tex` and the r62 `first_frame_tex`.
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
        if let Some(t) = cache.first_frame_tex.take() {
            gl.delete_texture(t);
        }
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
    // analysis script can pair them by stream order. Zero overhead
    // when off (single env::var_os check at entry).
    let trace_sub = std::env::var_os("OPENMARQUEE_BOUNDARY_TRACE").is_some();
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
                // CMA-arc 2026-06-21 C4: lazy-upload this family
                // (and the Inter fallback if family fails) before
                // the read-only msdf_atlas_for_family lookup.
                ensure_msdf_atlas_for_family_or_default(gl, family);
                let group = msdf_atlas_for_family(family)
                    .or_else(|| msdf_atlas_for_family("Inter"))
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
                // CMA-arc 2026-06-21 C4: lazy-upload before the
                // read-only lookup.
                ensure_msdf_atlas_for_family_or_default(gl, family);
                let (atlas_tex, _) = msdf_atlas_for_family(family)
                    .or_else(|| msdf_atlas_for_family("Inter"))
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
            // CMA-arc 2026-06-21 C4: lazy-upload before the
            // read-only lookup (overlay route).
            ensure_msdf_atlas_for_family_or_default(gl, family);
            let (atlas_tex, _) = msdf_atlas_for_family(family)
                .or_else(|| msdf_atlas_for_family("Inter"))
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
            slide_caches_insert(
                session,
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

// ============================================================
// PR3 (2026-06-27) onboarding system-card MVP paint.
//
// Per QA call (b): BG color-fill + text rendering via the existing
// MSDF stack. Mark-image/chip/QR/spinner primitives ship in PR3.1.
//
// The supervisor sends RenderSystemCard which sets
// state.active_system_card. On each frame, paint_and_present_*
// callers check for an active card and, if present, paint it
// instead of the playlist content. The card auto-clears when its
// optional ttl deadline elapses, OR on explicit ClearSystemCard
// (the supervisor sends this on ONLINE).
// ============================================================

/// PR3 finish-pass safety cap for `ttl_ms=None` cards: even the
/// "until-state-change" cards (SETUP/CONNECTING/DEGRADED) get
/// force-cleared after this window so a supervisor crash / missed-
/// ClearSystemCard can never wedge the sign forever. 60 minutes is
/// long enough that a legitimate slow onboarding never trips it
/// but short enough that a wedged sign self-heals within an hour.
const SYSTEM_CARD_MAX_LIFETIME_S: u64 = 60 * 60;

/// MVP system-card overlay paint + present.
///
/// Returns Ok(true) when a card was painted AND presented to the
/// panel (the per-frame caller should skip the normal playlist
/// paint for this frame — the frame is already on scanout). Returns
/// Ok(false) when no card is active OR the ttl deadline has just
/// elapsed (the caller falls through to normal playlist paint).
///
/// Present sequence mirrors `paint_and_present_one_frame_for_slide`
/// verbatim: `eglSwapBuffers` → `gbm_surface.lock_front_buffer` →
/// `add_framebuffer` → `commit_fb` → `rotate_scanout_3_deep`. The
/// PR3-original hook was inert (drew to the FBO but never swapped)
/// which froze the sign on the last playlist frame for the whole
/// onboarding — QA review 2026-07-01 called this out as the sign-
/// freezing bug. The full present-sequence here is what makes the
/// card actually reach the panel.
///
/// PR3 MVP scope: paints Background (glClear) + Text (synthetic
/// TextLayer through the existing MSDF stack) + QR panel
/// (scissored glClear-per-module). Image / Chip / Spinner /
/// Footer / BootHint remain on the skip arm — PR3.1 fidelity.
pub fn maybe_paint_system_card_overlay(
    state: &mut crate::playback::PlaybackState,
    session: &mut EglSession,
    card_drm: &Card,
) -> Result<bool> {
    use crate::system_card::CardShape;

    // 1. Snapshot + ttl deadline check + max-lifetime safety cap.
    //    An expired card clears the slot + returns "no card active"
    //    so the playlist paint resumes on the next frame.
    {
        let Some(card) = state.active_system_card.as_ref() else {
            return Ok(false);
        };
        let now = std::time::Instant::now();
        // Deadline (from RenderSystemCard.ttl_ms > 0) OR the
        // absolute max-lifetime cap (ttl_ms=None safety net).
        let ttl_expired = card.deadline.map(|d| now >= d).unwrap_or(false);
        let max_life_expired =
            card.activated_at.elapsed().as_secs() >= SYSTEM_CARD_MAX_LIFETIME_S;
        if ttl_expired || max_life_expired {
            state.active_system_card = None;
            return Ok(false);
        }
    }

    // 2. Borrow shapes + a spinner-phase seed from the active card
    //    WITHOUT cloning the shape Vec (perf, and the borrow lifetime
    //    stays under our control by shadowing `session`/`state`
    //    borrows below). We DO copy the small cache (QR modules) via
    //    Arc so per-frame QR encoding is skipped once the card is
    //    active (PR3.1 perf item #7).
    //
    //    activated_at gives the spinner its animation phase without
    //    needing a separate frame counter.
    let (shapes, qr_cache, activated_at): (
        Vec<CardShape>,
        Option<std::sync::Arc<crate::qr::QrBitmap>>,
        std::time::Instant,
    ) = {
        let card = state
            .active_system_card
            .as_ref()
            .expect("active card checked above");
        (
            card.shapes.clone(),
            card.qr_cache.clone(),
            card.activated_at,
        )
    };
    let spinner_phase = activated_at.elapsed().as_secs_f32();

    // 3. PR3.1 rotation/brightness fix (item #1): route the card paint
    //    through the SAME scene_fbo + run_present_pass pipeline as
    //    text/image/video arms. Direct-to-scanout paint (PR3 MVP)
    //    rendered wrong-oriented on rotated panels + un-dimmed under
    //    non-identity brightness/gamma. Now: paint shapes into
    //    scene_fbo when settings are non-identity OR rotated; the
    //    present pass then blits scene_tex → default fb with the same
    //    brightness / gamma / rotation shader every other arm uses.
    let identity = session.current_settings.is_color_identity();
    let rotation = session.rotation;
    let mode_w = u32::from(session.mode_w);
    let mode_h = u32::from(session.mode_h);
    let scene_fbo_handle = if !identity || rotation != 0 {
        Some(unsafe { ensure_scene_fbo(session, mode_w, mode_h)? })
    } else {
        None
    };

    use glow::HasContext;
    unsafe {
        if let Some((fbo, _)) = scene_fbo_handle {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, Some(fbo));
        }
        session.gl.viewport(0, 0, mode_w as i32, mode_h as i32);
        session.gl.disable(glow::SCISSOR_TEST);
    }
    // Immutable gl handle for the per-shape paint helpers. The
    // helpers only read the GL context; the scene_fbo bind + present
    // pass mutate via session.gl directly.
    let gl: &glow::Context = session.gl;

    for shape in &shapes {
        match shape {
            CardShape::Background { color } => unsafe {
                gl.disable(glow::SCISSOR_TEST);
                gl.clear_color(
                    f32::from(color.0) / 255.0,
                    f32::from(color.1) / 255.0,
                    f32::from(color.2) / 255.0,
                    1.0,
                );
                gl.clear(glow::COLOR_BUFFER_BIT);
            },
            CardShape::Text {
                anchor,
                max_height,
                color,
                font,
                text,
                align,
            } => {
                paint_system_card_text(
                    gl, mode_w, mode_h, *anchor, *max_height, *color, *font, *align, text,
                )?;
            }
            CardShape::Image { top_left, height } => {
                paint_system_card_image(gl, mode_w, mode_h, *top_left, *height)?;
            }
            CardShape::QrPanel {
                top_left,
                size,
                payload,
                caption,
            } => {
                paint_system_card_qr_panel(
                    gl,
                    mode_w,
                    mode_h,
                    *top_left,
                    *size,
                    payload,
                    caption,
                    qr_cache.as_deref(),
                )?;
            }
            // PR3.1 fidelity primitives.
            CardShape::Chip {
                top_right,
                label,
                bg,
                ink,
                text_height,
            } => {
                paint_system_card_chip(
                    gl,
                    mode_w,
                    mode_h,
                    *top_right,
                    label,
                    *bg,
                    *ink,
                    *text_height,
                )?;
            }
            CardShape::Spinner {
                center,
                radius,
                color,
            } => {
                paint_system_card_spinner(
                    gl,
                    mode_w,
                    mode_h,
                    *center,
                    *radius,
                    *color,
                    spinner_phase,
                );
            }
            CardShape::Footer {
                text,
                color,
                max_height,
            } => {
                paint_system_card_footer(gl, mode_w, mode_h, text, *color, *max_height)?;
            }
            CardShape::BootHint {
                center_bottom,
                text,
                color,
            } => {
                paint_system_card_boot_hint(
                    gl,
                    mode_w,
                    mode_h,
                    *center_bottom,
                    text,
                    *color,
                )?;
            }
        }
    }
    unsafe {
        session.gl.disable(glow::SCISSOR_TEST);
    }

    // 3a. Present pass: bind default fb + blit scene_tex through the
    //     rotation/brightness/gamma shader when we painted into
    //     scene_fbo.
    if let Some((_fbo, tex)) = scene_fbo_handle {
        let brightness = session.current_settings.brightness as f32 / 100.0;
        let gamma = session.current_settings.gamma;
        let (phys_w, phys_h) = session.phys_mode_size();
        unsafe {
            session.gl.bind_framebuffer(glow::FRAMEBUFFER, None);
            session.gl.viewport(0, 0, phys_w as i32, phys_h as i32);
            run_present_pass(session.gl, tex, brightness, gamma, rotation)?;
        }
    }

    // 4. PRESENT — the critical piece the original PR3 was missing.
    //    Same sequence as paint_and_present_one_frame_for_slide.
    session.maybe_live_preview_capture();
    session
        .egl_lib
        .swap_buffers(session.display, session.egl_surface)
        .map_err(|e| anyhow!("eglSwapBuffers (system-card) failed: {e:?}"))?;
    let new_bo = unsafe {
        session
            .gbm_surface
            .lock_front_buffer()
            .context("gbm_surface_lock_front_buffer (system-card) failed")?
    };
    let fb_buf = GbmBufferAdapter::new(&new_bo)
        .context("read GBM bo metadata (system-card)")?;
    let new_fb = card_drm
        .add_framebuffer(&fb_buf, 32, 32)
        .map_err(|e| anyhow!("drmModeAddFB (system-card) failed: {e}"))?;
    if let Err(e) = commit_fb(session, card_drm, new_fb) {
        // Roll back: free the FB + drop the BO before propagating.
        if let Err(de) = card_drm.destroy_framebuffer(new_fb) {
            eprintln!(
                "warn: system-card cleanup destroy_framebuffer({new_fb:?}) on commit-fail: {de}"
            );
        }
        drop(new_bo);
        return Err(e);
    }
    // 3-deep scanout rotation matches the flip-race-fix-D pattern
    // used by every other paint arm.
    rotate_scanout_3_deep(session, card_drm, new_bo, new_fb, "system_card");

    Ok(true)
}

/// The baked boot-card brand mark (the real dot-matrix wordmark artwork,
/// mark.png) — build.rs copies it into OUT_DIR, we bake it into the binary
/// (no runtime file dependency, same discipline as the SDF atlases).
static MARK_PNG: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/mark.png"));

thread_local! {
    /// The uploaded mark GL texture + its dims, decoded + uploaded once on
    /// first paint. A context reopen (rare — rotation only, which rebuilds
    /// everything) would leave this stale; the short-lived boot card never
    /// spans a reopen, so a one-shot upload is sufficient. Copy (glow
    /// texture handles are Copy) so `*borrow()` is cheap.
    static MARK_TEX: std::cell::RefCell<Option<(glow::NativeTexture, u32, u32)>> =
        const { std::cell::RefCell::new(None) };
    /// The mark's quad VBO, created once + reused (re-uploaded each frame
    /// since the rect can differ per card). Same context-lifetime caveat +
    /// cache pattern as `TEXTURED_QUAD_VBO`.
    static MARK_VBO: std::cell::Cell<Option<glow::NativeBuffer>> =
        const { std::cell::Cell::new(None) };
}

/// Decode the baked mark.png to row-flipped RGBA8 (bottom-up, to match the
/// `VS_TEXTURED_QUAD` `v=0`-at-bottom convention). Returns None on any
/// decode failure so the card renders without the mark rather than crashing.
fn decode_mark_rgba() -> Option<(Vec<u8>, u32, u32)> {
    let decoder = png::Decoder::new(std::io::Cursor::new(MARK_PNG));
    let mut reader = decoder.read_info().ok()?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).ok()?;
    let (w, h) = (info.width, info.height);
    let mut rgba = match info.color_type {
        png::ColorType::Rgba => {
            buf.truncate(info.buffer_size());
            buf
        }
        png::ColorType::Rgb => {
            let mut out = Vec::with_capacity((w * h * 4) as usize);
            for px in buf[..info.buffer_size()].chunks_exact(3) {
                out.extend_from_slice(px);
                out.push(0xff);
            }
            out
        }
        _ => return None,
    };
    crate::hdmi_logic::flip_rgba_rows_in_place(&mut rgba, w, h);
    Some((rgba, w, h))
}

/// Blit the baked brand mark (mark.png) as an alpha-blended textured quad.
/// `top_left` is normalized card space (y-down); `height` is a fraction of
/// the card HEIGHT — the width is derived from the texture's own aspect so
/// the artwork is never distorted (matches `system_card::MARK_ASPECT`).
/// Fail-soft: a decode/upload/draw failure just omits the mark.
fn paint_system_card_image(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    top_left: (f32, f32),
    height: f32,
) -> Result<()> {
    use glow::HasContext;

    // Decode + upload once, cache the texture.
    let tex_dims = MARK_TEX.with(|cell| -> Option<(glow::NativeTexture, u32, u32)> {
        if let Some(t) = *cell.borrow() {
            return Some(t);
        }
        let (rgba, w, h) = decode_mark_rgba()?;
        let tex = unsafe {
            let t = gl.create_texture().ok()?;
            gl.bind_texture(glow::TEXTURE_2D, Some(t));
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32);
            gl.tex_parameter_i32(glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32);
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0, glow::RGBA as i32,
                w as i32, h as i32, 0,
                glow::RGBA, glow::UNSIGNED_BYTE, Some(&rgba),
            );
            gl.bind_texture(glow::TEXTURE_2D, None);
            t
        };
        *cell.borrow_mut() = Some((tex, w, h));
        Some((tex, w, h))
    });
    let Some((tex, tw, th)) = tex_dims else {
        eprintln!("[system-card] mark.png decode/upload failed; omitting mark");
        return Ok(());
    };

    // Undistorted rect: height is a card-height fraction; width follows the
    // texture aspect, corrected for the card's own aspect.
    let h_frac = height;
    let w_frac = h_frac * (mode_h as f32 / mode_w as f32) * (tw as f32 / th as f32);
    // Normalized top-left (y-down) -> NDC (y-up). Bottom-left vertex carries
    // UV (0,0); rows were flipped at decode so the image is upright.
    let x0 = top_left.0 * 2.0 - 1.0;
    let x1 = (top_left.0 + w_frac) * 2.0 - 1.0;
    let y0 = 1.0 - (top_left.1 + h_frac) * 2.0;
    let y1 = 1.0 - top_left.1 * 2.0;
    let verts: [f32; 16] = [
        x0, y0, 0.0, 0.0,
        x1, y0, 1.0, 0.0,
        x0, y1, 0.0, 1.0,
        x1, y1, 1.0, 1.0,
    ];

    // Reuse a cached VBO handle (created once); re-upload the verts each
    // frame since the rect can differ per card. Avoids per-frame
    // create/delete churn on the weak Pi GPU.
    let vbo = MARK_VBO.with(|c| -> Result<glow::NativeBuffer> {
        if let Some(v) = c.get() {
            return Ok(v);
        }
        let v = unsafe { gl.create_buffer() }
            .map_err(|e| anyhow!("glGenBuffers(system-card mark): {e}"))?;
        c.set(Some(v));
        Ok(v)
    })?;
    unsafe {
        gl.bind_buffer(glow::ARRAY_BUFFER, Some(vbo));
        let bytes =
            std::slice::from_raw_parts(verts.as_ptr() as *const u8, std::mem::size_of_val(&verts));
        gl.buffer_data_u8_slice(glow::ARRAY_BUFFER, bytes, glow::DYNAMIC_DRAW);

        gl.enable(glow::BLEND);
        gl.blend_func(glow::SRC_ALPHA, glow::ONE_MINUS_SRC_ALPHA);
        let res = run_blit_pass_quad(gl, tex, vbo);
        gl.disable(glow::BLEND);
        if let Err(e) = res {
            eprintln!("[system-card] mark blit failed: {e}");
        }
    }
    Ok(())
}

/// PR3.1 (2026-07-01) — paint the QR panel using a CACHED QrBitmap
/// (built once at RenderSystemCard activation, see ipc_main.rs) so
/// the per-frame cost is O(dark_module_count) scissored glClears
/// instead of re-encoding the QR + rebuilding the bitmap every tick.
/// Falls back to on-demand encoding when the cache is None (defensive:
/// keeps the panel painting even on a codepath that skipped the
/// activation-time encode).
///
/// Below the QR grid we render a small caption line ("Scan to join" /
/// "Scan to fix") via the standard MSDF text pipeline so the panel
/// visually reads as one unit.
fn paint_system_card_qr_panel(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    top_left: (f32, f32),
    size: f32,
    payload: &str,
    caption: &str,
    cached: Option<&crate::qr::QrBitmap>,
) -> Result<()> {
    use glow::HasContext;

    let panel_x_px = (top_left.0 * mode_w as f32) as i32;
    let panel_y_px = (top_left.1 * mode_h as f32) as i32;
    let panel_w_px = (size * mode_w as f32) as i32;
    let panel_h_px = panel_w_px; // square panel

    // GL scissor Y is bottom-origin; convert card-space top-left
    // (top-origin) to bottom-origin.
    let panel_y_bl = mode_h as i32 - panel_y_px - panel_h_px;

    // 1. White panel background.
    unsafe {
        gl.enable(glow::SCISSOR_TEST);
        gl.scissor(panel_x_px, panel_y_bl, panel_w_px, panel_h_px);
        gl.clear_color(1.0, 1.0, 1.0, 1.0);
        gl.clear(glow::COLOR_BUFFER_BIT);
    }

    // 2. Prefer the cached bitmap. Fall back to a fresh encode only
    //    when the activation-time cache is unavailable.
    let owned_fallback;
    let qr: &crate::qr::QrBitmap = match cached {
        Some(c) => c,
        None => {
            let Ok(bmp) = crate::qr::encode_qr(payload) else {
                eprintln!(
                    "[system-card] qr encode failed for payload of length {}; drawing empty panel",
                    payload.len()
                );
                unsafe {
                    gl.disable(glow::SCISSOR_TEST);
                }
                return Ok(());
            };
            owned_fallback = bmp;
            &owned_fallback
        }
    };

    // Inset the QR modules by ~5% of the panel to give a quiet
    // zone (spec: 4-module quiet zone; 5% at typical QR sizes is
    // close enough for the MVP).
    let quiet_zone_frac = 0.05;
    let inner_offset = (panel_w_px as f32 * quiet_zone_frac) as i32;
    let inner_w = panel_w_px - 2 * inner_offset;
    let module_px = (inner_w as f32 / qr.size as f32).max(1.0);

    unsafe {
        gl.clear_color(0.0, 0.0, 0.0, 1.0);
    }
    for y in 0..qr.size {
        for x in 0..qr.size {
            if !qr.module(x, y) {
                continue;
            }
            let mx_px = panel_x_px + inner_offset + (x as f32 * module_px) as i32;
            let my_px = panel_y_px + inner_offset + (y as f32 * module_px) as i32;
            // Round up so adjacent modules touch (avoids single-
            // pixel white seams at fractional sizes).
            let mw_px = module_px.ceil() as i32;
            let mh_px = module_px.ceil() as i32;
            // Convert to bottom-origin for scissor.
            let my_bl = mode_h as i32 - my_px - mh_px;
            unsafe {
                gl.scissor(mx_px, my_bl, mw_px, mh_px);
                gl.clear(glow::COLOR_BUFFER_BIT);
            }
        }
    }
    unsafe {
        gl.disable(glow::SCISSOR_TEST);
    }

    // 3. Caption line below the panel. `size` is a WIDTH fraction and
    //    the panel is square in pixels, so its height as a fraction of
    //    the card is `size * (mode_w / mode_h)`. Using bare `size` here
    //    (pre-2026-07-06 bug) placed the caption INSIDE the panel on a
    //    16:9 card, painting over the lower QR modules and eating into
    //    the error-correction budget — surfaced by the boot identity
    //    card's standalone-centered QR, but SETUP/DEGRADED shared it.
    //    Aspect-correct so the caption sits just below the real panel
    //    bottom (~2% of card width of breathing room).
    if !caption.is_empty() {
        let panel_h_frac = size * (mode_w as f32 / mode_h as f32);
        let caption_y = top_left.1 + panel_h_frac + 0.012;
        paint_system_card_text(
            gl,
            mode_w,
            mode_h,
            (top_left.0 + size * 0.5, caption_y),
            0.019,
            crate::system_card::MUTED,
            crate::system_card::DisplayFont::Body,
            crate::system_card::Align::Center,
            caption,
        )?;
    }
    Ok(())
}

/// PR3.1 (2026-07-01) — utility: paint a solid-color axis-aligned
/// rectangle in normalized card-space (0..1). Wraps the scissored-
/// glClear idiom in one helper so every rect-drawing primitive
/// (chip pill, footer bar backing) shares the same
/// coordinate conversion.
fn paint_system_card_rect(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    color: crate::system_card::Rgb,
) {
    use glow::HasContext;
    let x_px = (x * mode_w as f32) as i32;
    let y_px = (y * mode_h as f32) as i32;
    let w_px = ((w * mode_w as f32) as i32).max(1);
    let h_px = ((h * mode_h as f32) as i32).max(1);
    let y_bl = mode_h as i32 - y_px - h_px;
    unsafe {
        gl.enable(glow::SCISSOR_TEST);
        gl.scissor(x_px, y_bl, w_px, h_px);
        gl.clear_color(
            f32::from(color.0) / 255.0,
            f32::from(color.1) / 255.0,
            f32::from(color.2) / 255.0,
            1.0,
        );
        gl.clear(glow::COLOR_BUFFER_BIT);
        gl.disable(glow::SCISSOR_TEST);
    }
}

/// PR3.1 (2026-07-01) — state chip: solid-fill amber/green/red
/// background with a label. Anchor is the chip's top-right corner
/// in normalized card-space (matches the mockup's `.chip`
/// placement).
fn paint_system_card_chip(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    top_right: (f32, f32),
    label: &str,
    bg: crate::system_card::Rgb,
    ink: crate::system_card::Rgb,
    text_height: f32,
) -> Result<()> {
    // Approximate chip width from the label length (mono-ish font;
    // 0.011 per char is a safe ratio for the 700-weight display
    // types). Padding is 0.017 of card width per side (mockup:
    // 1.7cqw). Height is the text cap-height + ~0.015 padding
    // above/below.
    let padding_x = 0.017_f32;
    let padding_y = 0.010_f32;
    let approx_char_w = text_height * 0.85;
    let chip_w = (label.chars().count() as f32) * approx_char_w + padding_x * 2.0;
    let chip_h = text_height + padding_y * 2.0;
    let chip_x = (1.0 - top_right.0 - chip_w).max(0.0);
    let chip_y = top_right.1;
    paint_system_card_rect(gl, mode_w, mode_h, chip_x, chip_y, chip_w, chip_h, bg);
    paint_system_card_text(
        gl,
        mode_w,
        mode_h,
        (chip_x + padding_x, chip_y + padding_y),
        text_height,
        ink,
        crate::system_card::DisplayFont::Headline,
        crate::system_card::Align::Left,
        label,
    )?;
    Ok(())
}

/// PR3.1 (2026-07-01) — CONNECTING-card spinner: 8 rotating dots
/// around a circle. Uses `phase_s` (seconds since card activation)
/// to compute the current angular offset — one rotation per 1.2s.
///
/// Dots are drawn as square scissored glClears (the visual size is
/// small enough that the difference from a proper circle is not
/// perceptible at panel resolution — same rationale as the sharp-
/// cornered chip pill).
fn paint_system_card_spinner(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    center: (f32, f32),
    radius: f32,
    color: crate::system_card::Rgb,
    phase_s: f32,
) {
    let dot_count = 8;
    let rotation_period_s = 1.2_f32;
    let base_angle = (phase_s / rotation_period_s) * std::f32::consts::TAU;
    let dot_size = radius * 0.28;
    for i in 0..dot_count {
        let angle = base_angle
            + (i as f32) * (std::f32::consts::TAU / dot_count as f32);
        // Rotation clockwise on screen; y axis inverted vs math.
        let dx = angle.cos() * radius;
        let dy = angle.sin() * radius;
        let x = center.0 + dx - dot_size * 0.5;
        let y = center.1 - dy - dot_size * 0.5;
        paint_system_card_rect(gl, mode_w, mode_h, x, y, dot_size, dot_size, color);
    }
}

/// PR3.1 (2026-07-01) — footer bar: single centered muted-color text
/// line at the bottom of the card. Used by CONNECTED to remind the
/// user where the sign lives.
fn paint_system_card_footer(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    text: &str,
    color: crate::system_card::Rgb,
    max_height: f32,
) -> Result<()> {
    if text.is_empty() {
        return Ok(());
    }
    // Centered horizontally, ~4.6% up from the bottom edge (mockup
    // `.foot.b`).
    paint_system_card_text(
        gl,
        mode_w,
        mode_h,
        (0.5, 1.0 - 0.046 - max_height),
        max_height,
        color,
        crate::system_card::DisplayFont::Body,
        crate::system_card::Align::Center,
        text,
    )
}

/// PR3.1 (2026-07-01) — BOOT-card hint line ("Restart 2× more for
/// Setup Mode"). PR4 turns this on when the rapid-boot counter is
/// armed; PR3.1 just paints the text when present.
fn paint_system_card_boot_hint(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    center_bottom: (f32, f32),
    text: &str,
    color: crate::system_card::Rgb,
) -> Result<()> {
    if text.is_empty() {
        return Ok(());
    }
    let max_height = 0.020_f32;
    paint_system_card_text(
        gl,
        mode_w,
        mode_h,
        (center_bottom.0, center_bottom.1 - max_height),
        max_height,
        color,
        crate::system_card::DisplayFont::Body,
        crate::system_card::Align::Center,
        text,
    )
}

/// PR3 MVP system-card text paint. Builds a synthetic TextLayer
/// (so the existing MSDF pipeline does the layout + GL draws),
/// computes the MsdfQuadGroup via `layout_text_to_quads`, and
/// dispatches to `draw_text_layer_msdf`.
fn paint_system_card_text(
    gl: &glow::Context,
    mode_w: u32,
    mode_h: u32,
    anchor: (f32, f32),
    max_height: f32,
    color: crate::system_card::Rgb,
    font: crate::system_card::DisplayFont,
    align: crate::system_card::Align,
    text: &str,
) -> Result<()> {
    use crate::hdmi_logic::{layout_text_to_quads, MotionState};

    let family = match font {
        crate::system_card::DisplayFont::Headline => "Oswald",
        crate::system_card::DisplayFont::Body => "Inter",
        crate::system_card::DisplayFont::Mono => "JetBrains Mono",
    };

    let align_str = match align {
        crate::system_card::Align::Left => "left",
        crate::system_card::Align::Center => "center",
        crate::system_card::Align::Right => "right",
    };

    let mut size_px = (max_height * mode_h as f32).max(8.0);

    // Box covers the card from the anchor x rightward to the right
    // edge (or symmetric for center-aligned). Vertical box is just
    // tall enough for ~6 lines so multi-line text fits.
    let box_x = anchor.0;
    let box_y = anchor.1;
    let box_w = match align {
        crate::system_card::Align::Center => {
            let half = box_x.min(1.0 - box_x);
            (half * 2.0).clamp(0.05, 1.0)
        }
        _ => (1.0 - box_x - 0.046).max(0.05),
    };
    let box_h = (max_height * 6.0).min(1.0 - box_y).max(max_height);
    // Shift box_x left by half its width for centered alignment so
    // the TextLayer's left-anchored box still centers visually.
    let box_x = match align {
        crate::system_card::Align::Center => (anchor.0 - box_w / 2.0).max(0.0),
        _ => box_x,
    };

    let color_hex = format!(
        "#{:02x}{:02x}{:02x}",
        color.0, color.1, color.2
    );

    let mut layer = crate::content::TextLayer {
        text: text.to_string(),
        name: String::from("system-card-text"),
        font_family: Some(family.to_string()),
        font_size_px: Some(size_px),
        font_size_pct: None,
        text_color: color_hex,
        text_align: align_str.to_string(),
        opacity: 1.0,
        visible: true,
        motion: String::from("static"),
        motion_intensity: 0,
        motion_phase: 0.0,
        motion_speed: 0.0,
        auto_mode: None,
        auto_format: None,
        outline: false,
        drop_shadow: false,
        blend: String::from("normal"),
        anchor: String::from("top"),
        weight: None,
        r#box: crate::content::TextBox {
            x: box_x,
            y: box_y,
            w: box_w,
            h: box_h,
        },
    };

    // Atlas lookup + lazy-upload.
    ensure_msdf_atlas_for_family_or_default(gl, family);
    let Some((atlas_tex, atlas)) = msdf_atlas_for_family(family)
        .or_else(|| msdf_atlas_for_family("Inter"))
    else {
        eprintln!(
            "[system-card] paint_text: no MSDF atlas for family={family} or Inter; skipping line"
        );
        return Ok(());
    };

    let box_w_px = layer.r#box.w * mode_w as f32;

    // #2 boot-card fit-to-width (2026-07-07): the Mono label lines
    // (mDNS URL / SSID / Wi-Fi / address / Password) are single tokens that
    // can't word-wrap, so an over-long one would get X-squished by
    // layout_text_to_quads into a distorted, hard-to-read line. Measure
    // the natural (un-squished, box=∞) width and shrink the FONT SIZE
    // uniformly so the widest line fits `box_w_px` undistorted. Prose
    // (Headline/Body) is left alone — it keeps the normal per-line
    // X-squish-at-box behavior (layout splits on '\n' only; it never
    // word-wraps).
    if matches!(font, crate::system_card::DisplayFont::Mono) {
        if let Some(natural) =
            layout_text_to_quads(atlas, &layer.text, size_px, f32::INFINITY, None)
        {
            let fitted =
                crate::hdmi_logic::shrink_font_to_fit_width(size_px, natural.width as f32, box_w_px);
            if fitted < size_px {
                size_px = fitted;
                layer.font_size_px = Some(size_px);
            }
        }
    }

    let Some(group) = layout_text_to_quads(atlas, &layer.text, size_px, box_w_px, None) else {
        // Empty / whitespace-only laid out to no ink.
        return Ok(());
    };

    // Parse the text_color hex into RGBA floats matching the
    // draw_text_layer_msdf convention.
    let text_color = [
        f32::from(color.0) / 255.0,
        f32::from(color.1) / 255.0,
        f32::from(color.2) / 255.0,
        1.0,
    ];

    use glow::HasContext;
    unsafe {
        gl.enable(glow::BLEND);
        gl.blend_func(glow::SRC_ALPHA, glow::ONE_MINUS_SRC_ALPHA);
    }
    let res = draw_text_layer_msdf(
        gl,
        mode_w,
        mode_h,
        &layer,
        text_color,
        MotionKind::Static,
        MotionState::IDENTITY,
        &group,
        atlas_tex,
        Some((0, 0, mode_w, mode_h)),
    );
    unsafe {
        gl.disable(glow::BLEND);
    }
    res
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

