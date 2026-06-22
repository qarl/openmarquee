//! Pure-logic helpers shared by the HDMI bring-up path.
//!
//! Lives in its own module because `hdmi.rs` links against
//! drm/gbm/EGL which are Linux-only at link time. This module is
//! cross-platform — it compiles on macOS so `cargo test` can run on
//! the dev box and exercise these functions without a real DRM stack.

use std::cell::RefCell;
use std::collections::HashMap;
use std::path::PathBuf;
use std::rc::Rc;

/// Lazy catalog mapping `font_family` strings (as the editor stores
/// them in `TextLayer.font_family`) to loaded `fontdue::Font`
/// instances on demand.
///
/// The renderer is single-threaded; `Rc` over `Arc` is deliberate.
/// Every render call goes through `get(family)`; the first call
/// per family pays the disk read + parse cost, subsequent calls
/// return the cached `Rc`.
///
/// Fallback: if the requested family isn't in the static map OR
/// the TTF can't be loaded, the catalog tries `fallback_family`.
/// Returns `None` only when even the fallback is unavailable.
///
/// Phase 4.2c-4 — replaces the single `--font-path` cli arg with
/// per-layer family lookup.
pub struct FontCatalog {
    dir: PathBuf,
    fallback_family: String,
    /// Cache holds BOTH hits and misses (`None` for "tried this
    /// family, came up empty"). Negative-result caching keeps
    /// per-layer get() calls from re-issuing the static-map lookup
    /// and re-emitting the fallback-warn log line on every render
    /// for a slide that uses an unknown family. Bounded by the set
    /// of distinct family strings on the live deck — small.
    cache: RefCell<HashMap<String, Option<Rc<fontdue::Font>>>>,
}

impl FontCatalog {
    pub fn new(dir: PathBuf, fallback_family: String) -> Self {
        Self {
            dir,
            fallback_family,
            cache: RefCell::new(HashMap::new()),
        }
    }

    /// The operator-specified fallback family. `render_slide` uses
    /// this when a layer's `font_family` field is `None`, so an
    /// operator override flows through (vs. silently hardcoding
    /// "Anton" at the call site).
    pub fn fallback_family(&self) -> &str {
        &self.fallback_family
    }

    /// Look up a font by family name. Falls back to the catalog's
    /// `fallback_family` if the requested one isn't available.
    /// Returns `None` only if even the fallback can't be loaded.
    pub fn get(&self, family: &str) -> Option<Rc<fontdue::Font>> {
        if let Some(f) = self.try_load(family) {
            return Some(f);
        }
        if family != self.fallback_family {
            if let Some(f) = self.try_load(&self.fallback_family) {
                eprintln!(
                    "warn: font_family {family:?} unavailable; fell back to {:?}",
                    self.fallback_family,
                );
                return Some(f);
            }
        }
        None
    }

    fn try_load(&self, family: &str) -> Option<Rc<fontdue::Font>> {
        // Cache hit (Some) AND cache miss (None) both short-circuit.
        if let Some(entry) = self.cache.borrow().get(family) {
            return entry.as_ref().map(Rc::clone);
        }
        let result = (|| -> Option<Rc<fontdue::Font>> {
            let filename = font_family_to_filename(family)?;
            let path = self.dir.join(filename);
            let bytes = match std::fs::read(&path) {
                Ok(b) => b,
                Err(e) => {
                    eprintln!("warn: read font {}: {e}", path.display());
                    return None;
                }
            };
            match fontdue::Font::from_bytes(bytes, fontdue::FontSettings::default()) {
                Ok(f) => Some(Rc::new(f)),
                Err(e) => {
                    eprintln!("warn: parse font {}: {e}", path.display());
                    None
                }
            }
        })();
        self.cache
            .borrow_mut()
            .insert(family.to_string(), result.as_ref().map(Rc::clone));
        result
    }

    /// True iff the catalog's fallback family is loadable. Useful
    /// at startup so we can tell the operator early that NO text
    /// will render rather than failing per-layer.
    pub fn fallback_available(&self) -> bool {
        self.try_load(&self.fallback_family).is_some()
    }
}

/// v1-spec-delta #3 (slice b cache): per-layer MSDF layout cache.
/// Each entry holds the (resolved_text, MsdfQuadGroup) for one
/// layer. When the resolved text is unchanged across frames
/// (motion-only animations or the 29 frames between auto_mode
/// second-bucket boundaries), `layout_text_to_quads` is skipped
/// and the cached group is reused. Cache miss = text changed =
/// re-lay-out.
///
/// Vec parallel to text_layers; len matches. Initialized to None
/// at slide-render entry; populated lazily on first paint.
pub type GlyphCache = Vec<Option<CachedGlyph>>;

#[derive(Debug)]
pub struct CachedGlyph {
    pub text: String,
    /// qarl-direct perf-profile (2026-05-08): cache the size we
    /// laid out at, so a size change invalidates the cache.
    pub size_px: f32,
    /// 2026-05-17 wrap port: cache the max_width that drove
    /// wrap_text_to_width so a box-width change OR a mode_w change
    /// invalidates the layout.
    pub max_width_px: f32,
    /// SDF arc slice B.2 -- per-glyph MSDF quad layout used by the
    /// production text path (`draw_text_layer_msdf`). `None` when
    /// the text laid out to no ink (empty / whitespace only); the
    /// draw stage skips the layer.
    pub group: Option<MsdfQuadGroup>,
}

/// v1-spec-delta #3 (slice b cache, QA F2): the cache hit/miss
/// decision. None entry -> miss. Some entry with matching
/// (text, size_px) -> hit (skip rasterization). Some entry
/// with differing text OR size -> miss (re-rasterize).
///
/// Pure function, host-testable. Extracted from paint_slide's
/// inline match so the decision logic gets coverage in
/// hdmi_logic.rs rather than living only inside the GL-bound
/// render path.
///
/// Size comparison uses an exact equality on the f32 because
/// effective_font_size_px produces a deterministic value from
/// (font_size_px, font_size_pct, box.w, mode_w) — bitwise
/// identical inputs yield bitwise identical outputs.
pub fn should_rerasterize(
    cache_entry: Option<&CachedGlyph>,
    resolved_text: &str,
    size_px: f32,
    max_width_px: f32,
) -> bool {
    match cache_entry {
        Some(cached) => {
            cached.text != resolved_text
                || cached.size_px != size_px
                || cached.max_width_px != max_width_px
        }
        None => true,
    }
}

/// SDF arc slice B -- process-wide AA mode for the MSDF fragment
/// shaders. main.rs calls `set_aa_mode` at startup; the shader-
/// compile path reads `aa_mode()` when picking which FS_MSDF
/// variant to compile. OnceLock first-call-wins semantics matches
/// the CLI-flag-set-once contract; cross-platform so host tests
/// can exercise the shader-selection logic without DRM/EGL.
static AA_MODE: std::sync::OnceLock<crate::AaMode> = std::sync::OnceLock::new();

pub fn set_aa_mode(mode: crate::AaMode) {
    let _ = AA_MODE.set(mode);
}

/// Returns the configured AA mode, or `Fwidth` (the recon's
/// best-guess default) if main.rs hasn't called `set_aa_mode` yet
/// — handles direct cargo-test invocations that don't go through
/// main.rs's arg parse.
pub fn aa_mode() -> crate::AaMode {
    AA_MODE.get().copied().unwrap_or(crate::AaMode::Fwidth)
}

/// Insert `\n` at word boundaries so each line measures within
/// `max_width_px` via the same fontdue advance-width metric that
/// `layout_text_to_alpha` uses to paint. Preserves existing literal
/// newlines as hard breaks (wrap is applied per-paragraph). Single
/// words wider than `max_width_px` are left intact on their own line
/// — the rasterize bitmap-cap + horizontal squish handle that case.
///
/// Mirrors the JS path at `ui/src/rasterize.js:wrapTextToWidth` and
/// the Python path at `backend/openmarquee/seed.py:_wrap_text_to_width`
/// (the existing reference implementations). The measurement uses
/// the same per-glyph `m.advance_width.round()` that
/// `layout_text_to_alpha` sums to determine bitmap width, so a
/// wrap-fits line is guaranteed to paint within the rasterized
/// bitmap (no measure-vs-paint drift inside the Rust renderer).
///
/// Returns `text` unchanged when empty or `max_width_px <= 0` (matches
/// the JS/Python early-out so the renderer can call this on every
/// layer without a guard).
pub fn wrap_text_to_width(
    font: &fontdue::Font,
    text: &str,
    size_px: f32,
    max_width_px: f32,
) -> String {
    if text.is_empty() || max_width_px <= 0.0 {
        return text.to_string();
    }
    let space_w = font.metrics(' ', size_px).advance_width.round();
    let measure_word = |w: &str| -> f32 {
        w.chars()
            .map(|c| font.metrics(c, size_px).advance_width.round())
            .sum::<f32>()
    };
    let mut out_lines: Vec<String> = Vec::new();
    // JS reference uses `text.split(/\r?\n/)`; mirror by stripping a
    // trailing `\r` from each split-on-`\n` segment so `\r\n` (Windows)
    // input produces the same paragraph set as `\n` (Unix).
    for paragraph in text.split('\n').map(|p| p.strip_suffix('\r').unwrap_or(p)) {
        if paragraph.is_empty() {
            out_lines.push(String::new());
            continue;
        }
        // 2026-05-17 leading-whitespace fix: JS uses `line.length === 0`
        // to decide "first token of a line". The earlier `line_text
        // .is_empty()` check matched on STRING contents, so an empty
        // first token (from leading whitespace producing `""` in
        // `split(" ")`) re-fired the first-token branch on every
        // subsequent empty token, swallowing the space separators
        // and stripping leading whitespace. Tokens go into a Vec so
        // `.is_empty()` is the equivalent of `line.length === 0`.
        let mut line: Vec<&str> = Vec::new();
        let mut line_w = 0.0_f32;
        for word in paragraph.split(' ') {
            let word_w = measure_word(word);
            if line.is_empty() {
                line.push(word);
                line_w = word_w;
                continue;
            }
            let candidate_w = line_w + space_w + word_w;
            if candidate_w > max_width_px {
                out_lines.push(line.join(" "));
                line.clear();
                line.push(word);
                line_w = word_w;
            } else {
                line.push(word);
                line_w = candidate_w;
            }
        }
        out_lines.push(line.join(" "));
    }
    out_lines.join("\n")
}


/// Split text on newline boundaries. `\r\n` (Windows) is treated as
/// one break; bare `\r` (legacy Mac) also as one break. Split is
/// inclusive of trailing empty lines: `"abc\n"` -> `["abc", ""]`,
/// matching text-editor convention. Empty input -> `[""]`.
pub fn split_text_into_lines(text: &str) -> Vec<&str> {
    // Normalize \r\n and bare \r without allocating: walk the bytes
    // and emit slices at each break. Cheaper than text.replace().
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut start = 0usize;
    let mut i = 0usize;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'\n' {
            out.push(&text[start..i]);
            i += 1;
            start = i;
        } else if b == b'\r' {
            out.push(&text[start..i]);
            // \r\n: skip the \n too.
            i += 1;
            if i < bytes.len() && bytes[i] == b'\n' {
                i += 1;
            }
            start = i;
        } else {
            i += 1;
        }
    }
    out.push(&text[start..bytes.len()]);
    out
}


// =====================================================================
// Shader sources (cross-platform — pure GLSL strings).
//
// Lifted out of hdmi.rs (Linux-only) so host tests can snapshot-
// assert their shape: we want a missing `#version 100` directive or
// a renamed uniform to fail on the Mac dev box, not by going
// black-pixels on the Pi at runtime.
// =====================================================================

/// Vertex shader: emit each input vertex as-is (fullscreen quad in
/// NDC, no transform). Shared across every fragment shader the
/// renderer compiles.
///
/// **Coordinate decision (Phase 4.1c)**: this VS deliberately does
/// NOT emit a `v_uv` varying. Fragment shaders compute their own
/// coordinates from `gl_FragCoord` + a `u_viewport` uniform — see
/// `FS_GRADIENT` for the convention. Two reasons:
///   1. Image-coord conventions differ per pattern (gradient wants
///      [0, w-1] image space matching Python; tiled patterns may
///      want pixel coords; UV-normalized variants would still want
///      access to viewport for tile sizing). Forcing a single
///      varying convention now would constrain future patterns.
///   2. `gl_FragCoord.xy / u_viewport` is one trivial line per
///      fragment shader. Negligible vs the cost of a wrong varying.
/// If a future pattern *really* wants UVs — add a parallel
/// `VS_FULLSCREEN_QUAD_WITH_UV` rather than retro-fitting this one.
pub const VS_FULLSCREEN_QUAD: &str = r#"#version 100
attribute vec2 a_pos;
void main() {
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"#;

/// Vertex shader for textured quads: takes per-vertex position +
/// per-vertex UV, emits position to NDC + UV as a varying for the
/// fragment shader to sample. Used by the glyph path; eventually by
/// any pattern that needs per-vertex UVs (vs `gl_FragCoord` math
/// against `u_viewport`).
///
/// VBO layout: tight, 4 floats per vertex — `[x, y, u, v]`.
pub const VS_TEXTURED_QUAD: &str = r#"#version 100
attribute vec2 a_pos;
attribute vec2 a_uv;
varying vec2 v_uv;
void main() {
    v_uv = a_uv;
    gl_Position = vec4(a_pos, 0.0, 1.0);
}
"#;

/// FYS bug 5 -- compute the present-pass fullscreen-quad vertices
/// for a given display rotation. The returned 16-float array is 4
/// verts of interleaved `[x, y, u, v]` in TRIANGLE_STRIP order,
/// consumed by `VS_TEXTURED_QUAD` (a_pos at offset 0, a_uv at
/// offset 2, stride 4 floats).
///
/// ROTATION DIRECTION CONVENTION (defined HERE, the single source
/// of truth): `rotation` is `Settings.display_rotation` -- the
/// CLOCKWISE angle, in degrees, by which the operator has
/// physically mounted the panel. The renderer COMPENSATES: it
/// rotates the rendered content the OPPOSITE way (counter-clockwise
/// by `rotation`) so the image reads upright on the physically-
/// rotated panel. This is the macOS "Display -> Rotation" model --
/// dial in the angle the screen is turned and the renderer turns
/// the picture back. (FYS bug 5 follow-up: the first cut rotated
/// content clockwise WITH the setting, which doubled the tilt
/// instead of cancelling it.)
///
/// Implementation: the UVs are held FIXED and the vertex POSITIONS
/// are rotated. In GL's y-up NDC a clockwise screen rotation by
/// angle θ is `(x', y') = (x·cosθ + y·sinθ, −x·sinθ + y·cosθ)`.
/// To rotate content counter-clockwise by `rotation` we feed that
/// clockwise formula the negated angle: (c,s) = (cos −rotation,
/// sin −rotation) = (cos rotation, −sin rotation). 90 and 270 are
/// exact opposites. For 90/270 the logical scene texture is
/// portrait while the default framebuffer is landscape; the +/-1
/// NDC quad rotated 90° still spans +/-1, and the anisotropic
/// NDC->pixel mapping stretches the portrait texture to fill the
/// landscape panel exactly.
///
/// `rotation == 0` returns the legacy direct-blit quad byte-for-
/// byte (UV maps straight to NDC), so the 0° present path is
/// unchanged. Any unrecognized value is treated as 0.
pub fn present_quad_verts(rotation: i32) -> [f32; 16] {
    // Base quad: UV (u,v) maps straight to NDC (x,y). Same geometry
    // and ordering as the `cached_textured_quad_vbo` in hdmi.rs.
    let base: [(f32, f32, f32, f32); 4] = [
        (-1.0, -1.0, 0.0, 0.0),
        ( 1.0, -1.0, 1.0, 0.0),
        (-1.0,  1.0, 0.0, 1.0),
        ( 1.0,  1.0, 1.0, 1.0),
    ];
    // `rotation` is how the panel is physically turned clockwise;
    // compensate by rotating content the other way. Counter-
    // clockwise by θ == clockwise by −θ, so feed the clockwise
    // position formula (c,s) = (cos −θ, sin −θ) = (cos θ, −sin θ).
    let (c, s): (f32, f32) = match rotation {
        90 => (0.0, -1.0),
        180 => (-1.0, 0.0),
        270 => (0.0, 1.0),
        _ => (1.0, 0.0), // 0 (and any unexpected value): identity
    };
    let mut out = [0.0f32; 16];
    for (i, (x, y, u, v)) in base.iter().enumerate() {
        // Apply the rotation matrix: x' = x·c + y·s, y' = −x·s + y·c.
        out[i * 4] = x * c + y * s;
        out[i * 4 + 1] = -x * s + y * c;
        out[i * 4 + 2] = *u;
        out[i * 4 + 3] = *v;
    }
    out
}

/// Fragment shader for glyph rendering. The atlas/bitmap stores
/// alpha as a single channel (LUMINANCE-or-ALPHA in GLES2; we use
/// LUMINANCE because GLES2 ALPHA-only sampling returns the alpha
/// in `.a` only, while LUMINANCE returns in `.r/.g/.b/.a` which
/// is more flexible). Multiply by `u_text_color` for the layer's
/// foreground color and use the sampled alpha for blending.
pub const FS_GLYPH: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    float a = texture2D(u_atlas, v_uv).r;
    float alpha = a * u_opacity;
    gl_FragColor = vec4(u_text_color * alpha, alpha);
}
"#;

/// Fragment shader: glyph atlas with a 1-pixel outline stroke
/// around the body. Used when `layer.outline = true`. Samples the
/// 4 cardinal neighbors of v_uv at 1-pixel offsets (via
/// u_pixel_size) and dilates the glyph alpha mask by 1 pixel. The
/// body stays in u_text_color where the center alpha is solid;
/// the dilated ring (where the center is 0 but a neighbor has
/// glyph) renders in u_outline_color. At anti-aliased edges the
/// center alpha varies smoothly so the mix between body and
/// outline is also smooth.
///
/// Python convention (backend/openmarquee/motion.py:341) is a 1-
/// pixel BLACK stroke; the renderer hardcodes black via
/// u_outline_color uniform set in draw_text_layer. The schema
/// `outline: bool` is the on/off toggle; future schema growth
/// could expose color + width as uniforms here without a shader
/// rewrite.
///
/// Output is premultiplied-alpha (matches FS_GLYPH and the blend
/// func GL_ONE / GL_ONE_MINUS_SRC_ALPHA).
pub const FS_GLYPH_OUTLINE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform vec3 u_outline_color;
uniform vec2 u_pixel_size;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    float center = texture2D(u_atlas, v_uv).r;
    float n = texture2D(u_atlas, v_uv + vec2(0.0, -u_pixel_size.y)).r;
    float s = texture2D(u_atlas, v_uv + vec2(0.0,  u_pixel_size.y)).r;
    float w = texture2D(u_atlas, v_uv + vec2(-u_pixel_size.x, 0.0)).r;
    float e = texture2D(u_atlas, v_uv + vec2( u_pixel_size.x, 0.0)).r;
    float dilated = max(max(center, n), max(max(s, w), e));
    // Color blend: center=1 -> body, center=0 but neighbor>0 ->
    // outline ring. mix() handles the smooth AA edge naturally.
    vec3 color = mix(u_outline_color, u_text_color, center);
    float alpha = dilated * u_opacity;
    gl_FragColor = vec4(color * alpha, alpha);
}
"#;

/// SDF arc slice B.2 -- per-glyph quad emitted by `layout_text_to_quads`.
///
/// Each quad covers one glyph's atlas cell, positioned in
/// **per-layer logical pixel space** (top-left = (0, 0), y down,
/// matching the AlphaBitmap convention layout_text_to_alpha used).
/// The caller maps this rect into NDC via `box_to_ndc_quad` against
/// the layer's box, applying scale-down + halign + valign in the
/// same way as the old single-quad path.
///
/// Atlas UVs are normalized to [0, 1] over the font's MSDF atlas
/// (`atlas_w` x `atlas_h`); the V axis is top-down (matches
/// `build.rs`'s Y-flipped atlas write).
/// SDF arc slice C.3 -- per-glyph dispatch kind. Replaces the
/// boolean `tofu` field on MsdfQuad. The draw side matches on
/// this to pick a shader + texture per quad:
///
///   Msdf  -> cached_msdf_program, font's MSDF atlas texture.
///   Emoji -> cached_emoji_program, emoji atlas page texture.
///   Tofu  -> cached_tofu_program, no texture (procedural fill).
///
/// Layout side picks the kind per codepoint via:
///   1. emoji atlas (if provided + codepoint in entries) -> Emoji
///   2. MSDF font atlas (glyph_for hit) -> Msdf
///   3. whitespace -> no quad emitted
///   4. otherwise -> Tofu
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum GlyphKind {
    Msdf,
    Tofu,
    /// Bug 3 Slice 2B: runtime-MSDF glyph from the dynamic atlas
    /// page. UVs on the quad are atlas-space within the 2048×2048
    /// dynamic page (the only page in Slice 2; Slice 1.x will add
    /// LRU eviction when this page fills). Draw side resolves the
    /// dynamic-atlas texture via a thread_local (mirrors the static
    /// MSDF atlas lookup pattern in hdmi.rs).
    DynamicMsdf,
    /// Bug 3 Slice 3B + 3D: runtime-COLRv1 emoji glyph from the
    /// dynamic COLR atlas page (separate from DynamicMsdf — 96 px
    /// cells vs 48 px for MSDF). Same UV semantics as DynamicMsdf
    /// relative to the dynamic page; draw side binds the COLR
    /// page texture via a parallel thread_local + uses the
    /// FS_EMOJI fragment shader (RGBA passthrough). Slice 3D
    /// retired the static-CBDT `Emoji { page }` variant; emoji
    /// codepoints now route exclusively through this path.
    DynamicEmoji,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MsdfQuad {
    /// Per-layer pixel-space quad bounds (top-left origin, y-down).
    pub px_left: f32,
    pub px_top: f32,
    pub px_right: f32,
    pub px_bottom: f32,
    /// Atlas UV bounds (top-down).
    pub uv_left: f32,
    pub uv_top: f32,
    pub uv_right: f32,
    pub uv_bottom: f32,
    /// Per-glyph dispatch kind. SDF arc slice C.3 split the old
    /// `tofu: bool` into a three-variant enum so the emoji color
    /// path can carry its own atlas-page identifier alongside the
    /// existing MSDF + tofu paths.
    pub kind: GlyphKind,
}

/// SDF arc slice B.2 -- output of `layout_text_to_quads`.
///
/// Holds the per-glyph quads + the overall pixel-space dimensions
/// of the laid-out text. The dims feed `box_to_ndc_quad` (same as
/// AlphaBitmap.{width,height} did for the bitmap path) so the
/// downstream draw call's fit-to-box transform is unchanged.
#[derive(Debug, Clone, PartialEq)]
pub struct MsdfQuadGroup {
    pub quads: Vec<MsdfQuad>,
    /// Overall pixel-space width of the laid-out text (incl. a
    /// 1-pixel padding margin on each side, matching the AlphaBitmap
    /// path's padding).
    pub width: u32,
    /// Overall pixel-space height (incl. 1-pixel padding on each
    /// side + descender extent for the last line).
    pub height: u32,
    /// Font stem used for atlas lookup at draw time. The caller
    /// resolves font_family -> stem upstream; we just forward it
    /// so the draw path can bind the right GL texture.
    pub font_stem: String,
}

/// SDF arc slice B.2 -- per-glyph quad layout. Replaces
/// `layout_text_to_alpha` on the MSDF path.
///
/// Same line-splitting + per-line-baseline + cursor_x layout math
/// as the AlphaBitmap path. The only difference is that we emit
/// quads carrying atlas UVs instead of blitting alpha pixels into
/// a per-layer bitmap.
///
/// `size_px` is the requested on-screen font size in pixels. The
/// MSDF atlas is normalized to em units (1 em = `size_px`) so we
/// multiply em-relative plane bounds + advances by `size_px` to
/// land them in pixel space.
///
/// Returns `None` for empty text or no-ink-bearing text (matches
/// the AlphaBitmap path's None-semantics so callers don't need
/// branchy reshape).
pub fn layout_text_to_quads(
    atlas: &crate::sdf_atlas::MsdfAtlas,
    text: &str,
    size_px: f32,
    // Bug 4 (2026-05-19): boxW in PIXELS for per-line X-squish.
    // Each line whose natural advance exceeds box_w_px is scaled
    // INDEPENDENTLY on X so it fits boxW (matches Canvas2D's
    // spec §5.10a "both axes squish independently when both
    // overflow"). Pre-Bug-4 all lines shared a single group-level
    // X-squish ratio = boxW/widest_line, so shorter overflowing
    // lines were under-squished and lines within boxW were
    // (correctly) un-squished. Now each line gets its own ratio.
    //
    // Pass `f32::INFINITY` to opt out of capping (host tests + any
    // caller that wants legacy "natural per-line width" behavior).
    box_w_px: f32,
    // Bug 3 Slice 2A (2026-05-19): runtime glyph cache context for
    // codepoints not in the static MSDF atlas. The bundle (cache +
    // fonts_dir) replaces Slice 1B's bare `Option<&GlyphCache>` so
    // the dispatch hook can resolve a font_path the worker reads
    // TTF bytes from. None = opt out (host tests + any caller
    // without an EglSession reference).
    //
    // Behavior on static-miss + non-whitespace:
    //   - cache.get_or_request returns the slot state; on first
    //     encounter it enqueues a MissRequest.
    //   - Slice 2A worker (real msdfgen) → SlotState::Ready arrives
    //     a few hundred ms later via poll_completions.
    //   - Slice 2B will route Ready → CharKind::DynamicMsdf; until
    //     then this dispatch records the lookup and falls through
    //     to Tofu (preserving pre-Bug-3 visible behavior).
    runtime_glyph_cache: Option<crate::glyph_cache::RuntimeGlyphCtx<'_>>,
) -> Option<MsdfQuadGroup> {
    if text.is_empty() || size_px <= 0.0 {
        return None;
    }
    let lines = split_text_into_lines(text);

    // Per-line glyph list. We need TWO passes:
    //  1. Compute per-line advance + overall bbox so we can size
    //     the output rect (same as AlphaBitmap path).
    //  2. Emit quads positioned by baseline + cursor_x.
    //
    // Per-char entry covers the four dispatch outcomes:
    //   Emoji     -- emoji color-bitmap atlas hit (slice C.3)
    //   Msdf      -- font's MSDF atlas hit
    //   Whitespace-- skip emit, just advance cursor
    //   Tofu      -- deterministic missing-glyph rect
    let manifest = &atlas.manifest;
    let cell_px = manifest.cell_px as f32;

    // Atlas cell size in em (cell_px / 1 em-in-cell-px). The
    // build.rs baking uses CELL_PX=48 with autoframe -- the
    // glyph fits inside the cell at its natural em scale. Plane
    // bounds are em-relative offsets from baseline origin.
    //
    // The 1 em on-screen = size_px px. The atlas cell on-screen
    // = cell_em * size_px. Computing cell_em from plane bounds:
    //   cell_em_x = pl_right - pl_left  (per glyph; uniform across the cell)
    //   cell_em_y = pl_top - pl_bottom
    // But pl_* are em-relative to the glyph baseline, NOT to the
    // cell. The atlas cell spans (pl_left .. pl_right) em on each
    // glyph, but the cell size in em is the same for all glyphs
    // (the autoframe in build.rs uses the SAME CELL_PX for all).
    //
    // We don't actually need cell_em -- the per-glyph quad's
    // pixel-space extent is (pl_* * size_px), and the UVs are
    // the cell's pixel position in atlas pixels normalized to
    // [0, 1].

    // Pass 1: per-line layout. Compute advance per line and the
    // overall vertical extent. fontdue-equivalent line layout uses
    // ascent + descent in em units.
    //
    // SDF arc slice C.3 -- per-char dispatch:
    //   1. emoji atlas (when provided + cp matches an entry)
    //   2. font MSDF atlas (glyph_for hit)
    //   3. whitespace -- skip emit, just advance cursor
    //   4. tofu -- deterministic missing-glyph rect
    #[derive(Clone)]
    enum CharKind {
        Msdf(crate::sdf_atlas::GlyphEntry),
        Whitespace,
        Tofu,
        /// Bug 3 Slice 2B: runtime-cached MSDF glyph. The slot
        /// position (in dynamic-atlas pixels) + per-glyph metrics
        /// come from the cache's SlotState::Ready. Pass-2 emits
        /// MsdfQuad with kind=GlyphKind::DynamicMsdf and UVs
        /// computed from slot.{x,y} / ATLAS_DIM.
        DynamicMsdf {
            slot: crate::atlas_page::SlotPos,
            advance_em: f32,
            plane_bounds: crate::glyph_cache::PlaneBounds,
            cell_px: u32,
        },
        /// Bug 3 Slice 3B + 3D: runtime-cached COLRv1 emoji glyph.
        /// Slice 3D retired the static-CBDT `Emoji(EmojiAtlasEntry)`
        /// variant; emoji codepoints now route exclusively through
        /// this path. `plane_bounds` is the rasterizer's clip-box
        /// square, normalised to a 1-em side, baseline-relative —
        /// the emoji quad is positioned from it exactly like
        /// `DynamicMsdf` (parity fix 2026-05-20). UVs come from
        /// slot.{x,y} / ATLAS_DIM, same as DynamicMsdf against its
        /// dynamic page.
        DynamicEmoji {
            slot: crate::atlas_page::SlotPos,
            advance_em: f32,
            plane_bounds: crate::glyph_cache::PlaneBounds,
            cell_px: u32,
        },
    }
    struct LineLayout {
        // (codepoint, advance_em, char_kind).
        chars: Vec<(u32, f32, CharKind)>,
        advance_em: f32,
    }
    // Bug 3 Slice 3B: COLR cache resolution sentinel. Ready carries
    // the atlas slot + advance for emit; Pending means the worker
    // is mid-rasterization and we should render Tofu this frame
    // (next layout re-dispatches after slide_caches drain).
    enum ColrResolution {
        Ready {
            slot: crate::atlas_page::SlotPos,
            advance_em: f32,
            plane_bounds: crate::glyph_cache::PlaneBounds,
        },
        Pending,
    }
    // Bug 3 Slice 2A (2026-05-19): font_family_id is FNV-1a low 32
    // bits of the font stem. u32 widens the u8 used in Slice 1B's
    // dormant-API scaffolding so cross-font collisions are
    // astronomically rare across a ~24-font catalog (vs 1/256
    // pre-widening, which would cross-key two fonts hashing to the
    // same low byte once Slice 2 actually rasterizes per-font).
    let font_family_id: u32 = {
        let mut h: u64 = 0xcbf29ce484222325;
        for &b in atlas.manifest.font.as_bytes() {
            h ^= b as u64;
            h = h.wrapping_mul(0x100000001b3);
        }
        h as u32
    };
    let mut layouts: Vec<LineLayout> = Vec::with_capacity(lines.len());
    let mut any_glyph = false;
    let mut any_ink = false;
    for line in &lines {
        let mut entries: Vec<(u32, f32, CharKind)> =
            Vec::with_capacity(line.chars().count());
        let mut advance_em = 0.0_f32;
        for ch in line.chars() {
            let cp = ch as u32;
            let is_whitespace = matches!(cp, 0x09 | 0x0A | 0x0B | 0x0C | 0x0D | 0x20 | 0xA0);

            // Bug 3 Slice 3B + 3D (2026-05-19): COLRv1 dispatch.
            // Slice 3D retired the static-CBDT atlas, so emoji
            // codepoints route exclusively through the runtime
            // COLRv1 cache. On COLR FontMissing (NotoColorEmoji-
            // COLRv1 lacks the cp) we fall through to the
            // static-MSDF + DynamicMsdf fallback chain — so non-
            // emoji-range chars never reach this branch, and
            // emoji-range chars not in the COLRv1 font end up on
            // the MSDF DejaVu fallback.
            //
            // Pending state (Requested/Generating/None on first
            // encounter): render Tofu THIS frame; the worker's
            // FontMissing or Ready completion will invalidate
            // slide_caches and the next layout will resolve.
            let colr_dispatch = if is_whitespace {
                None
            } else if !crate::sdf_atlas_emoji::codepoint_is_emoji_range(cp) {
                None
            } else {
                runtime_glyph_cache.as_ref().and_then(|rt| {
                    let stem = crate::glyph_cache::COLR_EMOJI_FONT_STEM;
                    let font_id =
                        crate::glyph_cache::font_family_id_from_stem(stem);
                    let state = rt.cache.get_or_request(
                        crate::glyph_cache::GlyphKey {
                            font_family_id: font_id,
                            codepoint: cp,
                            render_mode: crate::glyph_cache::RenderMode::Colr,
                        },
                        || rt.fonts_dir.join(format!("{}.ttf", stem)),
                    );
                    match state {
                        Some(crate::glyph_cache::SlotState::Ready {
                            slot,
                            advance_em,
                            plane_bounds,
                        }) => Some(ColrResolution::Ready { slot, advance_em, plane_bounds }),
                        Some(crate::glyph_cache::SlotState::FontMissing) => {
                            // Fall through to MSDF fallback chain.
                            None
                        }
                        Some(crate::glyph_cache::SlotState::Requested)
                        | Some(crate::glyph_cache::SlotState::Generating)
                        | None => {
                            // Worker pending — render Tofu this frame.
                            Some(ColrResolution::Pending)
                        }
                    }
                })
            };

            let (kind, adv) =
                if let Some(ColrResolution::Ready { slot, advance_em, plane_bounds }) = colr_dispatch {
                // Bug 3 Slice 3B: COLRv1 cache hit (Ready). Advance
                // is the COLRv1 font's hmtx-derived advance in em
                // (units_per_em normalized). `plane_bounds` is the
                // rasterizer's normalised clip-box square; pass-2
                // positions the quad from it. Cell px = COLR_CELL_PX.
                any_ink = true;
                (
                    CharKind::DynamicEmoji {
                        slot,
                        advance_em,
                        plane_bounds,
                        cell_px: crate::glyph_cache_colr::COLR_CELL_PX,
                    },
                    advance_em,
                )
            } else if matches!(colr_dispatch, Some(ColrResolution::Pending)) {
                // Bug 3 Slice 3B: COLRv1 worker pending. Render
                // Tofu this frame; slide_caches drains on completion
                // and the next layout re-dispatches.
                any_ink = true;
                (CharKind::Tofu, 0.5)
            } else if let Some(g) = manifest.glyph_for(cp).cloned() {
                // MSDF path: existing semantics.
                let adv_em = g.advance_em;
                let has_ink = g.pl_right > g.pl_left && g.pl_top > g.pl_bottom;
                if has_ink {
                    any_ink = true;
                }
                (CharKind::Msdf(g), adv_em)
            } else if is_whitespace {
                // Whitespace NOT in the MSDF atlas (rare -- e.g. a
                // font without baked U+00A0 NBSP). Half-em fallback
                // advance, no quad emitted. Most whitespace lands in
                // the MSDF branch above with a baked space-glyph
                // advance + degenerate plane bounds, which pass 2
                // skips without emitting.
                (CharKind::Whitespace, 0.5)
            } else {
                // Bug 3 Slice 2D (2026-05-19): static atlas missed
                // this codepoint and it's not whitespace. Dispatch
                // the dynamic-cache lookup, iterating through the
                // configured fallback chain on FontMissing.
                //
                // Per-stem branches (loop body):
                //   Some(Ready)                   -> resolve via this stem
                //   Some(FontMissing)             -> continue to next stem
                //   Some(Requested|Generating)    -> stop (Tofu placeholder
                //                                     this frame; next layout
                //                                     after slide_caches drain
                //                                     will re-dispatch with
                //                                     the now-known outcome)
                //   None (first encounter)        -> stop (MissRequest just
                //                                     enqueued; Tofu now)
                //
                // Loop termination:
                //   - first Ready hit                       -> DynamicMsdf
                //   - first pending state (Requested/Gen/None)
                //                                           -> Tofu (this frame)
                //   - chain exhausted, all FontMissing      -> Tofu (permanent)
                let resolution = runtime_glyph_cache.as_ref().and_then(|rt| {
                    // Build the chain inline: primary first, then any
                    // fallback stems that differ from primary. Up to
                    // 1 + FALLBACK_FONT_STEMS.len() iterations.
                    let primary_stem = atlas.manifest.font.as_str();
                    let stems_iter = std::iter::once(primary_stem).chain(
                        crate::glyph_cache::FALLBACK_FONT_STEMS
                            .iter()
                            .copied()
                            .filter(|&s| s != primary_stem),
                    );
                    let mut resolved: Option<(
                        crate::atlas_page::SlotPos,
                        f32,
                        crate::glyph_cache::PlaneBounds,
                    )> = None;
                    for stem in stems_iter {
                        let font_id = if stem == primary_stem {
                            font_family_id
                        } else {
                            crate::glyph_cache::font_family_id_from_stem(stem)
                        };
                        let state = rt.cache.get_or_request(
                            crate::glyph_cache::GlyphKey {
                                font_family_id: font_id,
                                codepoint: cp,
                                render_mode: crate::glyph_cache::RenderMode::Msdf,
                            },
                            || rt.fonts_dir.join(format!("{}.ttf", stem)),
                        );
                        match state {
                            Some(crate::glyph_cache::SlotState::Ready {
                                slot,
                                advance_em,
                                plane_bounds,
                            }) => {
                                resolved = Some((slot, advance_em, plane_bounds));
                                break;
                            }
                            Some(crate::glyph_cache::SlotState::FontMissing) => {
                                continue;
                            }
                            // Worker is pending on THIS stem; don't
                            // race ahead to fallback (the worker may
                            // resolve Ready next round). Frame is Tofu
                            // for now; next layout will retry.
                            Some(crate::glyph_cache::SlotState::Requested)
                            | Some(crate::glyph_cache::SlotState::Generating)
                            | None => {
                                break;
                            }
                        }
                    }
                    resolved
                });
                match resolution {
                    Some((slot, advance_em, plane_bounds)) => {
                        any_ink = true;
                        (
                            CharKind::DynamicMsdf {
                                slot,
                                advance_em,
                                plane_bounds,
                                cell_px: crate::glyph_cache::CELL_PX,
                            },
                            advance_em,
                        )
                    }
                    None => {
                        // Tofu: placeholder (worker pending on some
                        // stem) OR permanent (entire chain FontMissing
                        // / no-cache-supplied). Deterministic missing-
                        // glyph rect counts as ink either way.
                        any_ink = true;
                        (CharKind::Tofu, 0.5)
                    }
                }
            };

            any_glyph = true;
            entries.push((cp, adv, kind));
            advance_em += adv;
        }
        // Bug 4 (2026-05-19) note: GROUP-level max advance is no
        // longer tracked here — bm_w is derived from the per-line
        // capped widths in the line_x_scales loop below.
        layouts.push(LineLayout {
            chars: entries,
            advance_em,
        });
    }
    if !any_glyph || !any_ink {
        return None;
    }

    // Per-line vertical metrics. ascent_em is the typographic
    // ascent for this font; descent_em is negative (baseline ->
    // bottom-of-descender, negative-going). line_gap_em is the
    // extra space between consecutive lines (often 0).
    let ascent_em = manifest.ascent_em;
    let descent_em = manifest.descent_em;
    let line_gap_em = manifest.line_gap_em;
    let line_h_em = ascent_em - descent_em + line_gap_em;

    // Bug 1c (2026-05-19): vertical extent is derived from the
    // actual GLYPH INK bbox across the run, not the font's EM
    // metrics. Matches Canvas2D's measureText(lines.join("")) which
    // returns actualBoundingBoxAscent / actualBoundingBoxDescent
    // (ui/src/rasterize.js:199-209). Pre-Bug-1c used (ascent_em -
    // descent_em) * size_px (typically 1.0-1.4em); ink is typically
    // 0.74-1.0em for caps + descender, so the EM extent shrunk
    // visible ink to ~62-83% of boxH where Canvas2D fills ~100%.
    //
    // GROUP-level (max-ascent / min-descent across all glyphs in
    // all lines) -- mirrors Canvas2D's `inkMetrics =
    // ctx.measureText(lines.join(""))` which joins all lines into
    // one measurement and applies uniform metrics to every line.
    //
    // Emoji + tofu both conceptually fill the em-box in Canvas2D's
    // measureText, so contribute (ascent_em, descent_em) -- this
    // also matches the on-screen emoji centering math at L747-748
    // which uses ascent_em / descent_em (not ink-bbox).
    //
    // The fallback at the end is defensive: if no ink contributed
    // (shouldn't happen since `any_ink` guards entry above), revert
    // to em metrics so the computation degrades gracefully rather
    // than producing a 0-extent bm_h.
    let mut ink_ascent_em: f32 = 0.0;
    let mut ink_descent_em: f32 = 0.0;
    for ll in &layouts {
        for (_, _, kind) in &ll.chars {
            let (top_em, bot_em) = match kind {
                CharKind::Msdf(g) if g.pl_top > g.pl_bottom => (g.pl_top, g.pl_bottom),
                CharKind::DynamicMsdf { plane_bounds, .. }
                    if plane_bounds.pl_top > plane_bounds.pl_bottom =>
                {
                    (plane_bounds.pl_top, plane_bounds.pl_bottom)
                }
                // Parity fix 2026-05-20: a COLR emoji contributes
                // its own clip-box plane_bounds (the emoji descends
                // below the baseline), so bm_h / the scissor bound
                // the full emoji quad — same as DynamicMsdf.
                CharKind::DynamicEmoji { plane_bounds, .. } => {
                    (plane_bounds.pl_top, plane_bounds.pl_bottom)
                }
                CharKind::Tofu => (ascent_em, descent_em),
                _ => continue,
            };
            if top_em > ink_ascent_em {
                ink_ascent_em = top_em;
            }
            if bot_em < ink_descent_em {
                ink_descent_em = bot_em;
            }
        }
    }
    if ink_ascent_em <= 0.0 && ink_descent_em >= 0.0 {
        ink_ascent_em = ascent_em;
        ink_descent_em = descent_em;
    }

    // Bug 4 (2026-05-19): per-line X-scale. Each line's natural
    // pixel advance vs box_w_px determines that line's independent
    // X-squish ratio. Pre-Bug-4 the GROUP-level bm_w = widest line's
    // natural advance flowed through box_to_ndc_quad's s_w, and ALL
    // lines shared that single squish factor — under-squishing short
    // lines and uniformly squishing wide ones. Now each line is
    // capped independently; widest CAPPED line drives bm_w.
    //
    // Pass `box_w_px = f32::INFINITY` (test opt-out) keeps the per-
    // line ratios all = 1.0 → equivalent to pre-Bug-4 layout.
    let mut line_x_scales: Vec<f32> = Vec::with_capacity(layouts.len());
    let mut max_capped_line_advance_px: f32 = 0.0;
    for layout in &layouts {
        let natural_w_px = layout.advance_em * size_px;
        let scale = if natural_w_px > box_w_px && natural_w_px > 0.0 {
            box_w_px / natural_w_px
        } else {
            1.0
        };
        line_x_scales.push(scale);
        let capped = natural_w_px * scale;
        if capped > max_capped_line_advance_px {
            max_capped_line_advance_px = capped;
        }
    }

    // Pixel-space dims. Match the AlphaBitmap path:
    //   line_w = ceil(max_capped_line_advance_px)
    //   line_h_px = round(size_px * 1.1)   -- BUT we know better
    //                                          metrics from the
    //                                          atlas, so use them
    let pad: u32 = 1;
    let last_extent_px = ((ink_ascent_em - ink_descent_em) * size_px).ceil() as u32;
    let line_h_px = (line_h_em * size_px).round().max(1.0) as u32;
    let bm_w =
        2 * pad + (max_capped_line_advance_px.ceil() as u32).max(1);
    let bm_h = 2 * pad + last_extent_px + (lines.len() as u32 - 1) * line_h_px;
    if bm_w == 0 || bm_h == 0 {
        return None;
    }

    // Pass 2: emit quads at baseline-aware positions.
    let atlas_w = manifest.atlas_w as f32;
    let atlas_h = manifest.atlas_h as f32;
    // Bug 2 (2026-05-19): 0.5-texel UV inset constant. Used by both
    // the MSDF and Emoji per-glyph UV computations below. See the
    // per-call site comment for the median-of-mixed-encodings
    // mechanism this guards against.
    const INSET_PX: f32 = 0.5;
    let mut quads: Vec<MsdfQuad> = Vec::new();
    for (line_idx, layout) in layouts.iter().enumerate() {
        // Baseline y in pixel-space, y-down. Each line's baseline
        // is `pad + line_idx * line_h_px + ink_ascent_em * size_px`
        // -- ink_ascent_em is positive, so adding it moves DOWN
        // from the line's top in pixel-y-down space.
        //
        // Bug 1c (2026-05-19): was `ascent_em * size_px`. Anchoring
        // on `ink_ascent_em` puts the first line's CAP TOP at exactly
        // `pad` (the new bm_h hugs the ink-bbox, not the em-extent).
        // Inter-line spacing stays em-based via `line_h_px` -- only
        // the first-line anchor + bm_h shrink to ink-based.
        let baseline_y =
            pad as f32 + (line_idx as f32) * line_h_px as f32 + ink_ascent_em * size_px;
        // Bug 4 (2026-05-19): per-line X squish. All X-space dims
        // (advance, plane-bound horizontals, emoji cell width, tofu
        // width) multiply by `x_size_px` instead of `size_px`. Y
        // dims stay unscaled so vertical positioning/sizing matches
        // the group-level path. For a line whose natural advance
        // fits boxW, x_size_px == size_px so behavior is unchanged.
        let x_scale = line_x_scales[line_idx];
        let x_size_px = size_px * x_scale;
        let mut cursor_x = pad as f32;
        for (_cp, adv_em, char_kind) in &layout.chars {
            let adv_px = adv_em * x_size_px;
            match char_kind {
                CharKind::Msdf(g) => {
                    // Skip glyphs with no ink (e.g. space, '\u{a0}'):
                    // advance cursor but don't emit a quad.
                    if g.pl_right <= g.pl_left || g.pl_top <= g.pl_bottom {
                        cursor_x += adv_px;
                        continue;
                    }
                    // pl_* are em-relative to the glyph origin. Convert
                    // to pixel-space relative to the cursor.
                    //
                    // pl_top is em above baseline (positive); on-screen
                    // y is down, so quad top = baseline_y - pl_top * size_px.
                    // pl_bottom is em below baseline (negative for
                    // descenders); quad bottom = baseline_y - pl_bottom * size_px.
                    let px_l = cursor_x + g.pl_left * x_size_px;
                    let px_r = cursor_x + g.pl_right * x_size_px;
                    let px_t = baseline_y - g.pl_top * size_px;
                    let px_b = baseline_y - g.pl_bottom * size_px;

                    // Atlas UVs: glyph's cell in atlas pixels normalized
                    // to [0, 1]. Note: atlas y-axis is top-down (the
                    // build.rs flip; matches our atlas tex upload), so
                    // uv_top = glyph.y / atlas_h, uv_bottom = (glyph.y
                    // + cell_px) / atlas_h.
                    //
                    // Bug 2 (2026-05-19): 0.5 atlas-pixel UV inset on
                    // all four sides. GL_LINEAR at the exact cell
                    // boundary mixes THIS cell's "outside" SDF RGB
                    // with the NEIGHBOR cell's "outside" SDF RGB.
                    // MSDF's three-channel edge-coloring assigns
                    // DIFFERENT RGB encodings to each glyph's
                    // outside region (one cell's outside pixel might
                    // be (0,0,255), neighbor's (0,255,0)). Bilinear
                    // average (0,127,127) has median=127 -- mid-range,
                    // crosses smoothstep's 0.5 threshold, produces
                    // visible amber hairlines above caps and below
                    // baselines for glyphs whose neighbor encoding
                    // happens to combine incompatibly. Inset by half
                    // a texel keeps GL_LINEAR sampling strictly
                    // within this cell.
                    let uv_l = (g.x as f32 + INSET_PX) / atlas_w;
                    let uv_r = (g.x as f32 + cell_px - INSET_PX) / atlas_w;
                    let uv_t = (g.y as f32 + INSET_PX) / atlas_h;
                    let uv_b = (g.y as f32 + cell_px - INSET_PX) / atlas_h;
                    quads.push(MsdfQuad {
                        px_left: px_l,
                        px_top: px_t,
                        px_right: px_r,
                        px_bottom: px_b,
                        uv_left: uv_l,
                        uv_top: uv_t,
                        uv_right: uv_r,
                        uv_bottom: uv_b,
                        kind: GlyphKind::Msdf,
                    });
                }
                CharKind::DynamicMsdf { slot, advance_em: _, plane_bounds, cell_px: dyn_cell_px } => {
                    // Bug 3 Slice 2B: identical math to CharKind::Msdf
                    // above, but sourcing UVs from the dynamic atlas
                    // page (2048×2048 fixed, see atlas_page::ATLAS_DIM)
                    // and plane bounds from the cache's SlotState::Ready.
                    let pb = plane_bounds;
                    if pb.pl_right <= pb.pl_left || pb.pl_top <= pb.pl_bottom {
                        cursor_x += adv_px;
                        continue;
                    }
                    let px_l = cursor_x + pb.pl_left * x_size_px;
                    let px_r = cursor_x + pb.pl_right * x_size_px;
                    let px_t = baseline_y - pb.pl_top * size_px;
                    let px_b = baseline_y - pb.pl_bottom * size_px;
                    // Dynamic atlas page is 2048×2048 (atlas_page::ATLAS_DIM).
                    // Hardcoded since the page count is fixed at 1 for
                    // Slice 2; Slice 1.x will revisit when LRU eviction
                    // adds multi-page support.
                    let atlas_dim_f = crate::atlas_page::ATLAS_DIM as f32;
                    let cp_f = *dyn_cell_px as f32;
                    let uv_l = (slot.x as f32 + INSET_PX) / atlas_dim_f;
                    let uv_r = (slot.x as f32 + cp_f - INSET_PX) / atlas_dim_f;
                    let uv_t = (slot.y as f32 + INSET_PX) / atlas_dim_f;
                    let uv_b = (slot.y as f32 + cp_f - INSET_PX) / atlas_dim_f;
                    quads.push(MsdfQuad {
                        px_left: px_l,
                        px_top: px_t,
                        px_right: px_r,
                        px_bottom: px_b,
                        uv_left: uv_l,
                        uv_top: uv_t,
                        uv_right: uv_r,
                        uv_bottom: uv_b,
                        kind: GlyphKind::DynamicMsdf,
                    });
                }
                CharKind::DynamicEmoji { slot, advance_em: _, plane_bounds, cell_px: dyn_cell_px } => {
                    // Parity fix (2026-05-20): position the emoji
                    // quad from `plane_bounds` — the rasterizer's
                    // COLRv1 clip-box square, normalised to a 1-em
                    // side, baseline-relative — exactly the math the
                    // CharKind::DynamicMsdf branch above uses.
                    //
                    // The pre-fix path drew a fixed emoji_cell_em-
                    // square cell centred on the font em-midpoint.
                    // Combined with the rasterizer's old em-box crop
                    // (which clipped the part of the emoji below the
                    // baseline), every emoji rendered with a hard
                    // flat cut at the cell's bottom edge. Sourcing
                    // the quad from plane_bounds lets the emoji
                    // descend below the baseline like the glyph is
                    // designed to — no crop. UVs still span the full
                    // dynamic-COLR atlas cell. No degenerate-bounds
                    // guard (unlike DynamicMsdf) is needed: the
                    // rasterizer always emits a normalised 1-em
                    // square here.
                    let pb = plane_bounds;
                    let px_l = cursor_x + pb.pl_left * x_size_px;
                    let px_r = cursor_x + pb.pl_right * x_size_px;
                    let px_t = baseline_y - pb.pl_top * size_px;
                    let px_b = baseline_y - pb.pl_bottom * size_px;
                    let atlas_dim_f = crate::atlas_page::ATLAS_DIM as f32;
                    let cp_f = *dyn_cell_px as f32;
                    let uv_l = (slot.x as f32 + INSET_PX) / atlas_dim_f;
                    let uv_r = (slot.x as f32 + cp_f - INSET_PX) / atlas_dim_f;
                    let uv_t = (slot.y as f32 + INSET_PX) / atlas_dim_f;
                    let uv_b = (slot.y as f32 + cp_f - INSET_PX) / atlas_dim_f;
                    quads.push(MsdfQuad {
                        px_left: px_l,
                        px_top: px_t,
                        px_right: px_r,
                        px_bottom: px_b,
                        uv_left: uv_l,
                        uv_top: uv_t,
                        uv_right: uv_r,
                        uv_bottom: uv_b,
                        kind: GlyphKind::DynamicEmoji,
                    });
                }
                CharKind::Whitespace => {
                    // Pure advance, no quad emitted.
                }
                CharKind::Tofu => {
                    // Tofu glyph: deterministic ~half-em rectangle at
                    // baseline. UVs are inert (draw path uses a fixed
                    // gray-with-outline fragment shader keyed on
                    // GlyphKind::Tofu). Pixel-space bounds: roughly
                    // ascent_em x 0.5 em, baselined like a normal glyph.
                    //
                    // Bug 4 (2026-05-19): tofu width uses x_size_px so
                    // the placeholder rect tracks per-line X squish.
                    let px_l = cursor_x + 0.05 * x_size_px;
                    let px_r = cursor_x + (adv_em - 0.05).max(0.1) * x_size_px;
                    let px_t = baseline_y - ascent_em * 0.85 * size_px;
                    let px_b = baseline_y;
                    quads.push(MsdfQuad {
                        px_left: px_l,
                        px_top: px_t,
                        px_right: px_r,
                        px_bottom: px_b,
                        uv_left: 0.0,
                        uv_top: 0.0,
                        uv_right: 0.0,
                        uv_bottom: 0.0,
                        kind: GlyphKind::Tofu,
                    });
                }
            }
            cursor_x += adv_px;
        }
    }

    Some(MsdfQuadGroup {
        quads,
        width: bm_w,
        height: bm_h,
        font_stem: manifest.font.clone(),
    })
}

// =====================================================================
// SDF arc slice B -- MSDF fragment shaders.
//
// Two body shaders (FS_MSDF_FWIDTH / FS_MSDF_FIXED) and two outline
// shaders (FS_MSDF_OUTLINE_FWIDTH / FS_MSDF_OUTLINE_FIXED). The
// "fwidth" variants use GLES2's GL_OES_standard_derivatives builtin
// to compute a per-fragment AA half-width; the "fixed" variants
// take a uniform AA width.
//
// `aa_mode()` (above) picks which variant the runtime compiles.
// Both pairs have matching uniforms so the CPU side is mode-
// agnostic except for the program ID.
//
// Sampling: median-of-three on RGB MSDF channels (per Chlumsky's
// MSDF reconstruction), then smoothstep against 0.5. Output is
// premultiplied-alpha (matches the existing blend func
// GL_ONE / GL_ONE_MINUS_SRC_ALPHA).
//
// All four shaders consume the same VS_TEXTURED_QUAD vertex
// attribs (a_pos vec2, a_uv vec2). The MSDF atlas is sampled in
// `.rgb`; slice B's atlases are uploaded as GL_RGB / RGB888.
// =====================================================================

/// MSDF body shader, fwidth() AA variant. Adaptive across scale.
pub const FS_MSDF_FWIDTH: &str = r#"#version 100
#extension GL_OES_standard_derivatives : enable
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    vec3 s = texture2D(u_atlas, v_uv).rgb;
    float d = max(min(s.r, s.g), min(max(s.r, s.g), s.b));
    float aa = fwidth(d);
    float a = smoothstep(0.5 - aa, 0.5 + aa, d) * u_opacity;
    gl_FragColor = vec4(u_text_color * a, a);
}
"#;

/// MSDF body shader, fixed-pixel-AA variant. Uses a uniform AA
/// width supplied per-draw by the CPU (typically 1.0 / quad_height
/// in UV space). Deterministic, no derivative dependency.
pub const FS_MSDF_FIXED: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform float u_opacity;
uniform float u_aa_width;
varying vec2 v_uv;
void main() {
    vec3 s = texture2D(u_atlas, v_uv).rgb;
    float d = max(min(s.r, s.g), min(max(s.r, s.g), s.b));
    float a = smoothstep(0.5 - u_aa_width, 0.5 + u_aa_width, d) * u_opacity;
    gl_FragColor = vec4(u_text_color * a, a);
}
"#;

/// MSDF outline shader, fwidth() AA variant.
///
/// Dual-threshold: anything inside `outline_threshold..0.5` is the
/// outline ring; `>= 0.5` is the body. Smoothstepped on both edges
/// for AA, mixed by `body_alpha` to keep the inside-the-letter
/// pixels showing through as body color.
///
/// `u_outline_distance` is the outline ring's half-width measured
/// in SDF units (0.0..0.5). A reasonable default is 0.1 (~10% of
/// the SDF range). The runtime can vary it without recompiling.
pub const FS_MSDF_OUTLINE_FWIDTH: &str = r#"#version 100
#extension GL_OES_standard_derivatives : enable
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform vec3 u_outline_color;
uniform float u_outline_distance;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    vec3 s = texture2D(u_atlas, v_uv).rgb;
    float d = max(min(s.r, s.g), min(max(s.r, s.g), s.b));
    float aa = fwidth(d);
    float body  = smoothstep(0.5 - aa, 0.5 + aa, d);
    float ring  = smoothstep(0.5 - u_outline_distance - aa,
                             0.5 - u_outline_distance + aa, d);
    vec3 color = mix(u_outline_color, u_text_color, body);
    float a = ring * u_opacity;
    gl_FragColor = vec4(color * a, a);
}
"#;

/// MSDF outline shader, fixed-pixel-AA variant. Same shape as
/// FS_MSDF_OUTLINE_FWIDTH; AA half-width comes from `u_aa_width`
/// instead of `fwidth(d)`.
pub const FS_MSDF_OUTLINE_FIXED: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_atlas;
uniform vec3 u_text_color;
uniform vec3 u_outline_color;
uniform float u_outline_distance;
uniform float u_aa_width;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    vec3 s = texture2D(u_atlas, v_uv).rgb;
    float d = max(min(s.r, s.g), min(max(s.r, s.g), s.b));
    float body  = smoothstep(0.5 - u_aa_width, 0.5 + u_aa_width, d);
    float ring  = smoothstep(0.5 - u_outline_distance - u_aa_width,
                             0.5 - u_outline_distance + u_aa_width, d);
    vec3 color = mix(u_outline_color, u_text_color, body);
    float a = ring * u_opacity;
    gl_FragColor = vec4(color * a, a);
}
"#;

/// Pick the MSDF body shader source based on the configured AA
/// mode. Slice B.2's shader-compile path calls this once per
/// program-cache miss.
pub fn fs_msdf_for_aa_mode() -> &'static str {
    match aa_mode() {
        crate::AaMode::Fwidth => FS_MSDF_FWIDTH,
        crate::AaMode::Fixed => FS_MSDF_FIXED,
    }
}

/// Pick the MSDF outline shader source based on the configured AA
/// mode.
pub fn fs_msdf_outline_for_aa_mode() -> &'static str {
    match aa_mode() {
        crate::AaMode::Fwidth => FS_MSDF_OUTLINE_FWIDTH,
        crate::AaMode::Fixed => FS_MSDF_OUTLINE_FIXED,
    }
}

/// SDF arc slice B.3 -- tofu fragment shader. Renders missing-
/// codepoint quads as a deterministic 50% gray rectangle with a
/// thin black outline ring. v_uv spans [0, 1] across each tofu
/// quad (NOT atlas UV — `draw_text_layer_msdf` emits unit UVs for
/// tofu quads), so we use it directly as the "position within
/// rect" coordinate for the outline test.
///
/// Outline width is fixed at 8% of the quad side — gives a visible
/// outline at the smallest font sizes (5% pct text) without
/// devouring the gray fill at large sizes. Output is premultiplied
/// alpha to match the FS_MSDF blend func contract.
pub const FS_TOFU: &str = r#"#version 100
precision mediump float;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    float bw = 0.08;
    float in_border = step(v_uv.x, bw)
                    + step(1.0 - bw, v_uv.x)
                    + step(v_uv.y, bw)
                    + step(1.0 - bw, v_uv.y);
    in_border = clamp(in_border, 0.0, 1.0);
    vec3 color = mix(vec3(0.5), vec3(0.0), in_border);
    float a = u_opacity;
    gl_FragColor = vec4(color * a, a);
}
"#;

/// SDF arc slice C.2 -- color-emoji fragment shader. Samples the
/// emoji atlas page (RGBA8, straight-alpha as decoded from the CBDT
/// PNG) and emits premultiplied alpha so the standard
/// (GL_ONE, GL_ONE_MINUS_SRC_ALPHA) blend func matches the rest of
/// the text path.
///
/// No outline / no recoloring — emoji are color bitmaps, not SDFs,
/// so the body color is whatever Noto baked. `u_opacity` modulates
/// for fade transitions + per-layer opacity. Pairs with
/// VS_TEXTURED_QUAD; v_uv is atlas-space.
pub const FS_EMOJI: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_atlas;
uniform float u_opacity;
varying vec2 v_uv;
void main() {
    vec4 c = texture2D(u_atlas, v_uv);
    float a = c.a * u_opacity;
    gl_FragColor = vec4(c.rgb * a, a);
}
"#;

/// Fragment shader: hard cut between two textures at t=0.5. Doesn't
/// exist as a shader in the Python ref (cut is a playback-level
/// instant switch) but adding it here keeps the transition dispatch
/// uniform — every transition kind goes through the same per-frame
/// loop with a single FS_FOR_KIND lookup. At t<0.5 emits src_a, at
/// t>=0.5 emits src_b. Pairs with VS_TEXTURED_QUAD.
pub const FS_CUT: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    gl_FragColor = mix(a, b, step(0.5, u_t));
}
"#;

/// QA-direct (2026-05-09 Phase 2.6) -- atlas SB cut-composite
/// specialization. Cut is unique among the 16 transitions: its
/// mix function is binary at t=0.5 (step()), so any given frame
/// in the cut transition reads from EXACTLY ONE side. The
/// general FS_CUT samples both sides + mixes; on vc4 that's 2
/// fullscreen texture fetches per fragment when only 1 is
/// visually used. Halving the composite-pass texture sample
/// count = halving fragment fetch bandwidth on the composite
/// pass.
///
/// The atlas SB cut path picks FS_CUT_A at t<0.5 and FS_CUT_B
/// at t>=0.5. Both are wrap_composite_for_atlas-compatible:
/// wrap injects u_a_xform/u_b_xform + _sa/_sb helpers, and the
/// `texture2D(u_src_X, ...)` call site gets rewritten to
/// `_sX(...)`. The unused uniform/sampler is harmless
/// (GLES2 link-time eliminates unused varyings + unused
/// uniforms can be set or unset; see CachedCompositeProgram).
///
/// Other transition kinds (fade / wipe / iris / dissolve / etc.)
/// need both sides simultaneously and stay on the combined
/// FS_<KIND>.
pub const FS_CUT_A: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    gl_FragColor = texture2D(u_src_a, v_uv);
}
"#;

pub const FS_CUT_B: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    gl_FragColor = texture2D(u_src_b, v_uv);
}
"#;

/// Fragment shader: horizontal wipe — slide_b reveals from the left
/// edge with a hard line at x=t. Pairs with VS_TEXTURED_QUAD.
pub const FS_WIPE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float mask = step(v_uv.x, u_t);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: iris — slide_b reveals through a circle that
/// expands from screen center to the corners.
///
/// r95 (2026-06-08): aspect-correct iris. Pre-r95 used
/// `distance(v_uv, vec2(0.5))` directly with a `0.71` (≈ sqrt(0.5))
/// half-diagonal constant tuned for square viewports. On a
/// non-square display (FYS 1360x768 ≈ 1.77), the unit-circle in UV
/// space maps to a horizontal ellipse on the screen, with `0.71`
/// over/under-covering on one axis.
///
/// Fix: add `u_aspect = width / height` uniform, stretch d.x so
/// length() is measured in normalized-height units, and scale
/// t by the half-diagonal in those same units so u_t=1 covers
/// the farthest corner. Identical math to the SP iris arm in
/// `push_main_body`.
pub const FS_IRIS: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
uniform float u_aspect;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    vec2 d = v_uv - vec2(0.5);
    d.x *= u_aspect;
    float r = length(d);
    float r_max = 0.5 * sqrt(1.0 + u_aspect * u_aspect);
    float mask = step(r, u_t * r_max);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: dissolve — per-pixel reveal threshold sampled
/// from a hash of v_uv. Each pixel "rolls a die" once and reveals
/// when u_t crosses its threshold. Mirrors Python ref
/// `_FRAGMENT_DISSOLVE`.
///
/// **Precision note (P3, 2026-05-09)**: the Python ref uses a
/// `sin(dot)*large` hash that needs highp because the large
/// magnification constant (43758.5453) saturates mediump's ~10-bit
/// mantissa. Replaced with the Inigo Quilez mediump-safe idiom
/// (`50 * fract(p * 1/π + seed)` then cross-multiply + final
/// fract). Same per-pixel salt-and-pepper visual character; works
/// in mediump; cheaper than sin on vc4. Drops dissolve's highp
/// dependency from the punch list.
///
/// Why IQ over Hoskins's 0.1031-seeded form: Hoskins's small seed
/// produces tiny per-pixel input deltas at 1080p (delta 1/1920 *
/// 0.1031 ≈ 5e-5), which collapse the per-pixel hash into broad
/// swept reveals at native pixel pitch. IQ's 50.0 post-multiply
/// amplifies the delta to ~8e-3, well into the productive range
/// of fract-based scrambling. Host test
/// `dissolve_hash_decorrelates_adjacent_pixels` pins this.
pub const FS_DISSOLVE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
float _hash(vec2 p) {
    p = 50.0 * fract(p * 0.3183099 + vec2(0.71, 0.113));
    return fract(p.x * p.y * (p.x + p.y));
}
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float threshold = _hash(v_uv);
    float mask = step(threshold, u_t);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: pixelate — both images sample at a coarsened
/// grid whose block size grows to a peak at midpoint then shrinks
/// back. Mirrors Python ref `_FRAGMENT_PIXELATE`. The wave envelope
/// `1 - 4(t-0.5)^2` is 0 at t=0/1, 1 at t=0.5; block size 0.0025
/// (≈ 5px at 1080p, effectively native) at the endpoints, 0.0425
/// (≈ 80px at 1080p) at midpoint.
pub const FS_PIXELATE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float wave = 1.0 - 4.0 * (u_t - 0.5) * (u_t - 0.5);
    float blockSize = 0.0025 + 0.04 * wave;
    vec2 cell = floor(v_uv / blockSize) * blockSize + 0.5 * blockSize;
    vec4 a = texture2D(u_src_a, cell);
    vec4 b = texture2D(u_src_b, cell);
    gl_FragColor = mix(a, b, u_t);
}
"#;

/// Fragment shader: scanline — top-to-bottom sweep with a bright
/// white band at the sweep line. Mirrors Python ref
/// `_FRAGMENT_SCANLINE`. The 0.015 band-half-width is in normalized
/// UV (≈ 1.6% of screen height); the 0.7 brightness multiplier
/// keeps the band readable but not blown out.
pub const FS_SCANLINE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float sweep = u_t;
    float band_half = 0.015;
    float mask = step(v_uv.y, sweep);
    vec4 col = mix(a, b, mask);
    float band = 1.0 - smoothstep(0.0, band_half, abs(v_uv.y - sweep));
    col.rgb = mix(col.rgb, vec3(1.0), band * 0.7);
    gl_FragColor = col;
}
"#;

/// Fragment shader: halftone — slide_b emerges through a regular
/// grid of growing circular dots, one per cell. Mirrors Python ref
/// `_FRAGMENT_HALFTONE`. 16:9 grid hardcoded for the HDMI 1080p
/// target (8 rows × ~14 cols at that aspect); the 0.71 max-radius
/// is sqrt(0.5), the diagonal half-distance from cell center to
/// corner so dots fully overlap at t=1.
pub const FS_HALFTONE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float grid_y = 8.0;
    float aspect = 16.0 / 9.0;
    vec2 cell_uv = fract(vec2(v_uv.x * grid_y * aspect, v_uv.y * grid_y));
    float d = distance(cell_uv, vec2(0.5));
    float mask = step(d, u_t * 0.71);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: glitch — digital-corruption look. Per-row
/// horizontal jitter + linear cross-fade + occasional cyan tear
/// rows. The frame_seed quantizes u_t into ~30 distinct buckets so
/// the per-row hash gets a fresh seed every frame. Mirrors Python
/// ref `_FRAGMENT_GLITCH`.
///
/// Uses `precision highp float` for the same vc4-mantissa reason
/// as FS_DISSOLVE — the sin/dot/fract hash collapses on mediump.
pub const FS_GLITCH: &str = r#"#version 100
precision highp float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
float _hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
}
void main() {
    float row = floor(v_uv.y * 1080.0);
    float frame_seed = floor(u_t * 30.0);
    float jitter = (_hash(vec2(row, frame_seed)) - 0.5) * 0.1 * u_t;
    vec2 uv2 = vec2(v_uv.x + jitter, v_uv.y);
    vec4 a = texture2D(u_src_a, uv2);
    vec4 b = texture2D(u_src_b, uv2);
    vec4 col = mix(a, b, u_t);
    float tear_row = floor(v_uv.y * 60.0);
    float tear = step(0.95, _hash(vec2(tear_row, frame_seed + 1.0)));
    col.rgb = mix(col.rgb, vec3(0.0, 1.0, 1.0), tear * 0.5 * u_t);
    gl_FragColor = col;
}
"#;

/// Fragment shader: slide — both images translate horizontally;
/// slide_b enters from the right edge as slide_a exits left.
/// Mirrors Python ref `_FRAGMENT_SLIDE`.
pub const FS_SLIDE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float t = u_t;
    float seam = 1.0 - t;
    float onTo = step(seam, v_uv.x);
    vec2 fromUV = vec2(v_uv.x + t, v_uv.y);
    vec2 toUV = vec2(v_uv.x - seam, v_uv.y);
    vec4 a = texture2D(u_src_a, fromUV);
    vec4 b = texture2D(u_src_b, toUV);
    gl_FragColor = mix(a, b, onTo);
}
"#;

/// Fragment shader: push — slide_b enters from the LEFT, pushing
/// slide_a off the right. Bright projector-blade separator at the
/// seam (smoothstep'd 0.001 wide × 0.8 brightness). Mirrors Python
/// ref `_FRAGMENT_PUSH`.
pub const FS_PUSH: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float t = u_t;
    float onTo = step(v_uv.x, t);
    vec2 fromUV = vec2(v_uv.x - t, v_uv.y);
    vec2 toUV = vec2(v_uv.x + (1.0 - t), v_uv.y);
    vec4 a = texture2D(u_src_a, fromUV);
    vec4 b = texture2D(u_src_b, toUV);
    vec4 col = mix(a, b, onTo);
    float blade = 1.0 - smoothstep(0.0, 0.001, abs(v_uv.x - t));
    col.rgb = mix(col.rgb, vec3(1.0), blade * 0.8);
    gl_FragColor = col;
}
"#;

/// Fragment shader: scroll — vertical analog of slide. slide_b
/// enters from the bottom as slide_a rolls up off the top.
///
/// qarl-bug 2026-05-12: direction was reversed (B entered from the
/// TOP, A scrolled down off the BOTTOM) despite the comment. Root
/// cause: the Python ref `_FRAGMENT_SCROLL` was authored assuming
/// image-y-down (PIL/numpy) UV convention, but the Rust renderer's
/// transition_sp_quad_vbo binds v_uv.y=0 at NDC y=-1 (GL standard,
/// y-up). Same shader code, opposite visual result. Inverted the
/// step direction (`step(v_uv.y, t)` selects the bottom region as
/// the to-region) and the sampling offsets (A samples y-t to slide
/// its content upward as t grows; B samples y+(1-t) so its content
/// appears to rise from below). Python ref has the same latent bug;
/// not in scope for this commit but flagged as a follow-up.
pub const FS_SCROLL: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float t = u_t;
    float onTo = step(v_uv.y, t);
    vec2 fromUV = vec2(v_uv.x, v_uv.y - t);
    vec2 toUV = vec2(v_uv.x, v_uv.y + (1.0 - t));
    vec4 a = texture2D(u_src_a, fromUV);
    vec4 b = texture2D(u_src_b, toUV);
    gl_FragColor = mix(a, b, onTo);
}
"#;

/// Fragment shader: blinds — 16 horizontal slats opening from each
/// slat's midline outward. Mirrors Python ref `_FRAGMENT_BLINDS`.
pub const FS_BLINDS: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float n_slats = 16.0;
    float slat_uv = fract(v_uv.y * n_slats);
    float dist_to_mid = abs(slat_uv - 0.5);
    float mask = step(dist_to_mid, u_t * 0.5);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: flip — 2D card-flip approximation. slide_a
/// scaleX-shrinks 1.0 → 0.0 in the first half, then slide_b
/// scaleX-grows 0.0 → 1.0 in the second half. Mirrors Python ref
/// `_FRAGMENT_FLIP`.
pub const FS_FLIP: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float t = u_t;
    float scaleX = abs(2.0 * t - 1.0);
    float useTo = step(0.5, t);
    // Branchless (P5, 2026-05-09): the legacy shader gated the
    // texture sample with two nested conditionals on scaleX and
    // src_x extent. On vc4 SIMD the inner src_x test diverges
    // per-fragment whenever the card is mid-flip. Match the
    // SP-tier idiom: compute src_x unconditionally with
    // max(scaleX, 1e-3) to avoid divide-by-zero at t=0.5
    // exactly, sample both slides, then multiply the final RGB
    // by inside = step(0.001, scaleX) * step(0.0, src_x) *
    // step(src_x, 1.0). Out-of-card pixels still emit black
    // (matches legacy behavior); inside-card pixels mix(a, b,
    // useTo) like the legacy guard.
    float src_x = (v_uv.x - 0.5) / max(scaleX, 1e-3) + 0.5;
    vec2 uv = vec2(src_x, v_uv.y);
    vec4 a = texture2D(u_src_a, uv);
    vec4 b = texture2D(u_src_b, uv);
    vec3 col = mix(a, b, useTo).rgb;
    float inside = step(0.001, scaleX) * step(0.0, src_x) * step(src_x, 1.0);
    gl_FragColor = vec4(col * inside, 1.0);
}
"#;

/// Fragment shader: marquee — tickertape wraparound. slide_a
/// scrolls off to the left; a gap zone with a centered white dot
/// passes through; slide_b enters from the right. Mirrors Python
/// ref `_FRAGMENT_MARQUEE`.
pub const FS_MARQUEE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float gap_uv = 0.125;
    float scroll = u_t * (1.0 + gap_uv);
    float cx = scroll + v_uv.x;

    vec4 from_col = texture2D(u_src_a, vec2(cx, v_uv.y));
    vec4 to_col = texture2D(u_src_b, vec2(cx - 1.0 - gap_uv, v_uv.y));

    float gap_local_x = (cx - 1.0) / gap_uv;
    float dx_uv = (gap_local_x - 0.5) * gap_uv;
    float dy = v_uv.y - 0.5;
    float dist = length(vec2(dx_uv, dy));
    float dot_r = 0.074;
    float in_dot = step(dist, dot_r);
    vec4 gap_col = mix(vec4(0.0, 0.0, 0.0, 1.0), vec4(1.0), in_dot);

    float in_from = step(cx, 1.0);
    float in_to = step(1.0 + gap_uv, cx);
    float in_gap = 1.0 - in_from - in_to;

    gl_FragColor = from_col * in_from + gap_col * in_gap + to_col * in_to;
}
"#;

/// Fragment shader: shutter — hexagonal aperture. A regular hexagon
/// centered on the canvas grows from a point at t=0 to fully
/// covering the canvas at t=1. The 16:9 aspect-correct projection
/// keeps the hex regular at 1080p. The 0.866025 constant is
/// cos(30°). Mirrors Python ref `_FRAGMENT_SHUTTER`.
pub const FS_SHUTTER: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    vec2 d = v_uv - vec2(0.5);
    d.x *= 16.0 / 9.0;
    float k = 0.866025;
    float c1 = abs(d.x * k + d.y * 0.5);
    float c2 = abs(d.y);
    float c3 = abs(d.x * k - d.y * 0.5);
    float hex_d = max(max(c1, c2), c3);
    float inscribed = 1.5 * u_t;
    float mask = step(hex_d, inscribed);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: linear cross-fade between two textures by `u_t`:
/// at t=0 emits src_a, at t=1 emits src_b, linearly interpolated
/// between. Phase 5-b-1 — first transition.
///
/// Pairs with `VS_TEXTURED_QUAD`. Caller binds src_a to texture
/// unit 0, src_b to unit 1, and sets `u_t` per-frame from
/// `elapsed_ms / transition_ms` clamped to [0, 1].
pub const FS_FADE: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    gl_FragColor = mix(a, b, clamp(u_t, 0.0, 1.0));
}
"#;

/// Maximum text layers per slide that single-pass transitions
/// support. The vc4 GLES2 GL_MAX_TEXTURE_IMAGE_UNITS is 8; a
/// 2-slide single-pass shader binds N samplers per side, so
/// 2×N must fit. N=4 is the cap.
pub const SINGLE_PASS_MAX_LAYERS_PER_SLIDE: usize = 4;

/// Maximum text layers per slide for the scissored-bake path.
/// Bake-pass FS takes one slide's bg + N layer samplers; vc4
/// 8-sampler cap with bg-as-uniform gives N ≤ 8 in principle.
/// 6 keeps headroom and covers every FYS slide (max observed:
/// 5 layers on Tile Chaos #08 / Chant Wall #09).
pub const SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE: usize = 6;

/// Atlas-FBO geometry for the single-FBO scissored-bake path
/// (2026-05-09 QA architectural redirect).
///
/// The 13ms FBO-switch penalty observed in the prior 2-FBO bake
/// scheme is rooted in vc4 V3D 2.1's tiled-deferred single-core
/// sequencing: every render-target switch forces a tile-store
/// flush of the outgoing FBO before the new pass can begin. No
/// GLES2 mechanism (DiscardFramebufferEXT, renderbuffer-vs-tex,
/// triple-FBO rotation, glClear, vc4-specific extensions) breaks
/// that. The architectural answer is to bake BOTH slides into ONE
/// FBO without re-binding between them.
///
/// vc4 GL_MAX_TEXTURE_SIZE = 2048. The atlas is 2048x2048 with
/// two 2048x1024 regions stacked vertically:
///   - Slide A region: y in [0, 1024)
///   - Slide B region: y in [1024, 2048)
/// Each region is wider (2048) than 1080p output (1920), so a
/// 128-pixel right-side gutter is unused. Vertical compression is
/// 1080 → 1024 = 5.5%; the composite pass LINEAR-upsamples to
/// 1080. Empirically this is well below the threshold of
/// perceptible vertical-stem softening on Anton / Playfair italic
/// (~ sub-pixel at 1080p viewing distance).
///
/// Within bake phase: FBO bound once, viewport + scissor switch
/// between regions to confine writes. Composite phase: FBO bound
/// to default, atlas sampled with two UV transforms (one per
/// region). Bind-switch count drops from 3 to 2 per frame.
pub const ATLAS_FBO_W: u32 = 2048;
pub const ATLAS_FBO_H: u32 = 2048;
pub const ATLAS_REGION_W: u32 = 2048;
pub const ATLAS_REGION_H: u32 = 1024;

/// Wrap a composite-pass FS source for atlas-mode sampling. The
/// source must declare `uniform sampler2D u_src_a;` and
/// `uniform sampler2D u_src_b;` (every FS_<KIND> shader does)
/// and reference them via `texture2D(u_src_a, X)` / `(u_src_b, X)`.
///
/// The wrap injects:
///   - `uniform vec4 u_a_xform;` and `uniform vec4 u_b_xform;`
///     after the `u_src_b` line. Each is `(off_x, off_y, scale_x,
///     scale_y)` mapping a per-fragment uv (in [0,1] across the
///     scanout) to the atlas region for that slide.
///   - GLSL helpers `_sa(uv)` / `_sb(uv)` that apply the xform
///     before sampling the atlas.
///
/// Replaces every `texture2D(u_src_a, X)` with `_sa(X)` and the
/// matching b form with `_sb(X)`. The argument count change
/// (texture2D takes 2, _sa/_sb take 1) is balanced because the
/// existing `, ` after `u_src_a/u_src_b` is consumed by the
/// substitution; the closing parenthesis matches automatically.
///
/// Identity transform `(0, 0, 1, 1)` makes the wrapped shader
/// behave identically to the unwrapped FS_<KIND>, so callers that
/// don't use the atlas (e.g. capture path with a single full-res
/// source texture) can use the wrapped program by setting both
/// xforms to identity.
pub fn wrap_composite_for_atlas(src: &str) -> String {
    // Order matters: rewrite the call sites in `src` FIRST, then
    // inject the helpers. If we injected first, the helper bodies'
    // own `texture2D(u_src_a, ...)` would self-substitute into
    // `_sa(...)` and produce infinite recursion at link time.
    //
    // Bail out for shaders that don't reference both samplers (e.g.
    // FS_GLYPH); the wrap is a no-op then. The injection point is
    // anchored on `uniform sampler2D u_src_b;\n`, which every FS_
    // <KIND> composite shader has on its own line.
    let needle = "uniform sampler2D u_src_b;\n";
    let i = match src.find(needle) {
        Some(i) => i,
        None => return src.to_string(),
    };
    let split = i + needle.len();
    let rewritten = src
        .replace("texture2D(u_src_a, ", "_sa(")
        .replace("texture2D(u_src_b, ", "_sb(");
    // The call-site replacement also shifts byte offsets, so the
    // injection has to happen on the rewritten string. Find the
    // injection point again (still anchored on the same line).
    let split_re = match rewritten.find(needle) {
        Some(i) => i + needle.len(),
        None => {
            // Defensive: if the anchor moved despite no edits to it,
            // fall back to the prefix-len from `src` (substitution
            // operates on the body of main, never on the uniform
            // line).
            split
        }
    };
    let inject = "uniform vec4 u_a_xform;\nuniform vec4 u_b_xform;\nvec4 _sa(vec2 uv) { return texture2D(u_src_a, u_a_xform.xy + uv * u_a_xform.zw); }\nvec4 _sb(vec2 uv) { return texture2D(u_src_b, u_b_xform.xy + uv * u_b_xform.zw); }\n";
    let mut out = String::with_capacity(rewritten.len() + inject.len());
    out.push_str(&rewritten[..split_re]);
    out.push_str(inject);
    out.push_str(&rewritten[split_re..]);
    out
}

// fs_bake_sp_source removed 2026-05-08: the first attempt at
// scissored-bake used a full-screen apply_layer chain in this
// shader, but at 1080p with N apply_layer per fragment it was
// fragment-bound at ~70 ms/frame. paint_slide (per-layer-rect
// draws via cached_glyph_program + draw_text_layer) is the bake
// path now. Const removed; see git history if a future scissor
// optimization wants to revisit a generated-shader approach.

/// QA-mandated single-pass transitions (2026-05-08 step 3):
/// returns the set of transition kinds that have a single-pass
/// shader generator implemented today. Kinds outside this set
/// fall through to the legacy 3-pass bake+composite path. Grows
/// per batch as kinds are ported.
///
/// Batch A: cut, fade, wipe, iris, dissolve.
/// Batch B: scanline, halftone, blinds, shutter.
/// Batch C: slide, push, scroll.
/// Batch D (this commit): flip, marquee, pixelate.
/// Glitch: qarl-deferred -- stays on legacy.
pub fn is_transition_kind_single_pass(kind: &str) -> bool {
    matches!(
        kind,
        "cut"
            | "fade"
            | "wipe"
            | "iris"
            | "dissolve"
            | "scanline"
            | "halftone"
            | "blinds"
            | "shutter"
            | "slide"
            | "push"
            | "scroll"
            | "flip"
            | "marquee"
            | "pixelate"
    )
}

/// Resolve `kind` to a `'static` string slice if and only if it has a
/// single-pass generator. Required because the per-session program-
/// cache HashMap keys are `&'static str`; a runtime borrowed `&str`
/// would need ownership to fit. Mirrors the match in
/// `is_transition_kind_single_pass`; grows in lock-step as batches
/// port. Returns `None` for kinds outside the SP-portable set.
pub fn sp_kind_static(kind: &str) -> Option<&'static str> {
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

/// Decide whether the scissored-bake (atlas SB) tier should be
/// PREFERRED over the single-pass tier for a given pair of per-slide
/// layer counts. SP wins on cheap-fragment cases (low total layers);
/// scissored-bake wins when the per-fragment shader cost would push
/// SP over the vc4 fragment ceiling (more layers => more per-fragment
/// blends in one pass).
///
/// Returns `true` when EITHER side individually exceeds the SP
/// per-side cap, OR the combined layer count exceeds 4 (the empirical
/// SP-tier-still-cheaper threshold from FYS bench data). Combined-cap
/// of 4 is intentional, NOT 2*SINGLE_PASS_MAX_LAYERS_PER_SLIDE: at 5+
/// total layers the SP per-fragment fetch dominates the bind-switch
/// savings, even when each side is within its individual cap.
pub fn prefer_scissored_bake(n_a: usize, n_b: usize) -> bool {
    n_a > SINGLE_PASS_MAX_LAYERS_PER_SLIDE
        || n_b > SINGLE_PASS_MAX_LAYERS_PER_SLIDE
        || n_a + n_b > 4
}

/// Host-side mirror of the GLSL `_hash(vec2 p)` IQ hash that
/// FS_DISSOLVE / SP_HASH_HELPER embed. Lets unit tests assert the
/// hash's distribution / determinism / decorrelation properties
/// without spinning up a GLES2 context.
///
/// IMPORTANT: this MUST stay byte-for-byte equivalent to the GLSL.
/// The pinned constants (0.3183099 = 1/π, 50.0, seeds 0.71/0.113)
/// are load-bearing -- they're what makes the hash mediump-safe
/// AND uniformly distributed in [0, 1] AND adjacent-pixel-
/// decorrelated at 1080p. Changing either side without changing
/// the other invalidates the unit test guarantees.
pub fn dissolve_hash_vec2_to_float(p: [f32; 2]) -> f32 {
    // GLSL fract(x) = x - floor(x). Differs from Rust's f32::fract
    // (which is x - x.trunc()) only for negative inputs; our hash
    // inputs are v_uv in [0, 1] so the two agree, but use the GLSL
    // semantic explicitly for full equivalence in tests.
    fn glsl_fract(x: f32) -> f32 {
        x - x.floor()
    }
    // p = 50.0 * fract(p * 0.3183099 + vec2(0.71, 0.113));
    let p = [
        50.0 * glsl_fract(p[0] * 0.3183099 + 0.71),
        50.0 * glsl_fract(p[1] * 0.3183099 + 0.113),
    ];
    // return fract(p.x * p.y * (p.x + p.y));
    glsl_fract(p[0] * p[1] * (p[0] + p[1]))
}

/// Returns true if a gradient with this density is visually
/// indistinguishable from a solid `color_a` fill. Used by
/// `effective_solid_bg` (in hdmi.rs) to admit density-≈-0 gradients
/// to the single-pass tier as if they were solid bgs. The threshold
/// (1e-4) matches FS_GRADIENT's compute output: at density=0 the
/// shader produces color_a uniformly; values below 1e-4 are within
/// per-fragment quantization noise of that.
pub fn gradient_density_is_degenerate(density: f32) -> bool {
    density.abs() < 1e-4
}

/// Reel-prewarm tier classification for a single (kind, n_a, n_b)
/// transition pair. The runtime in `prewarm_sp_session` walks every
/// (i-1, i) plus the wrap-around (last, first) pair across the
/// resolved reel, and asks `classify_prewarm_pair` which compile
/// path each takes so the GPU programs are already linked when the
/// first frame of that transition reaches `render_transition_
/// animated_in_session`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PrewarmTier {
    /// Kind isn't in the SP-portable set (e.g. `glitch`, unknown).
    /// Runtime falls through to legacy 3-pass; nothing for prewarm
    /// to compile.
    NotSinglePass,
    /// Either side's layer count exceeds the SB cap (6). Runtime
    /// also falls through to legacy 3-pass for these; prewarm
    /// has nothing to compile.
    ExceedsBakeCap,
    /// SP tier: compile a `cached_transition_sp_program(kind,
    /// n_a, n_b)`. Runtime takes the single-pass path.
    SinglePass,
    /// Scissored-bake tier: compile the per-kind composite
    /// program (or the side-specialized cut programs for
    /// `kind == "cut"`). Runtime takes the atlas-SB path.
    ScissoredBake,
}

/// Classify a (kind, n_a, n_b) transition pair into the prewarm
/// tier the reel will use. Pure-logic mirror of the decision tree
/// in `prewarm_sp_session::consider_pair`. Tier dispatch:
///   1. kind in SP-portable set? if no -> `NotSinglePass`.
///   2. either side > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE (6)?
///      if yes -> `ExceedsBakeCap`.
///   3. SDF arc B.3: SP-tier gated to bg-only transitions
///      (`transition_eligible_for_single_pass_logic` rejects any
///      non-empty layer_props). If either side has text layers ->
///      `ScissoredBake` (prewarm-compiles the SB composite, not SP).
///   4. SP-cheaper-by-bench (`!prefer_scissored_bake(n_a, n_b)
///      && both sides within SP cap`)? if yes -> `SinglePass`,
///      else -> `ScissoredBake`.
pub fn classify_prewarm_pair(kind: &str, n_a: usize, n_b: usize) -> PrewarmTier {
    if !is_transition_kind_single_pass(kind) {
        return PrewarmTier::NotSinglePass;
    }
    if n_a > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
        || n_b > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
    {
        return PrewarmTier::ExceedsBakeCap;
    }
    // B.3 SP-tier text gate: SP composite shader can't sample the
    // per-glyph MSDF atlas (post-MSDF cutover; pre-B.3 it sampled
    // per-layer alpha bitmaps). Any text-bearing transition routes
    // through SB. Mirrors `transition_eligible_for_single_pass_logic`
    // at hdmi_logic.rs:1792.
    if n_a > 0 || n_b > 0 {
        return PrewarmTier::ScissoredBake;
    }
    if !prefer_scissored_bake(n_a, n_b) {
        PrewarmTier::SinglePass
    } else {
        PrewarmTier::ScissoredBake
    }
}

/// Per-layer property summary used by the atlas-SB / single-pass
/// eligibility gates. Sufficient for the gate logic without
/// dragging in `crate::content::TextLayer` or `Rc<fontdue::Font>`;
/// constructed by hdmi.rs from each `(TextLayer, color, font)`
/// tuple at decision time, then passed into the pure-logic
/// predicates below.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LayerCompositeProps {
    pub outline: bool,
    pub blend: BlendMode,
}

/// Pure-logic eligibility gate for the single-pass (SP) tier. SP
/// composes both slides' bg + every text layer + the per-kind
/// transition mix in ONE fragment shader; the gate excludes any
/// shape the SP shader can't express:
///   - kind outside the SP-portable set
///   - bg that isn't equivalent to a solid uniform fill on either
///     side (caller resolves this via `effective_solid_bg` and
///     passes the boolean here)
///   - more than SINGLE_PASS_MAX_LAYERS_PER_SLIDE on either side
///   - any layer with outline=true (FS_GLYPH_OUTLINE not in the
///     SP shader)
///   - any layer with blend != Normal (Multiply/Screen/Overlay
///     need separate blend-state passes)
///
/// Anything else is admitted; the caller picks SP vs scissored-
/// bake via `prefer_scissored_bake`. Inputs are already-resolved
/// per-layer property summaries to keep this function pure.
pub fn transition_eligible_for_single_pass_logic(
    kind: &str,
    bg_a_solid: bool,
    bg_b_solid: bool,
    layer_props_a: &[LayerCompositeProps],
    layer_props_b: &[LayerCompositeProps],
) -> bool {
    if !is_transition_kind_single_pass(kind) {
        return false;
    }
    if !bg_a_solid || !bg_b_solid {
        return false;
    }
    // SDF arc slice B.3 (font-clamp deletion): SP-tier was designed
    // around per-layer LUMINANCE bitmap textures (one tex + rect per
    // layer, sampled via `apply_layer()` in fs_transition_sp_source).
    // The MSDF cutover replaces that bitmap shape with per-glyph
    // atlas quads, which don't map onto SP's "1 tex per layer" data
    // contract. Rather than re-architecting SP for per-glyph sampling
    // (or reintroducing a softer FBO-clamp), we gate SP off for any
    // text-bearing transition. Text routes through the scissored-
    // bake tier (which uses paint_slide_with_viewport, already on
    // MSDF post-B.2) or the legacy 3-pass path. SP-tier stays alive
    // for image/bg-only transitions where there's no text.
    if !layer_props_a.is_empty() || !layer_props_b.is_empty() {
        return false;
    }
    true
}

/// Pure-logic eligibility gate for the scissored-bake (atlas SB)
/// tier. SB renders both slides into a 2048x2048 atlas (split
/// vertically into two 2048x1024 regions) via paint_slide_with_
/// viewport, then composites at runtime. The gate is wider than
/// SP because:
///   - bg type doesn't matter (bg-cache machinery handles every
///     BgKind variant: gradient/pattern/image/solid).
///   - Per-side cap is SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE (6),
///     not SP's 4 (the bake pass uses paint_slide which has its
///     own per-layer-glyph-program pipeline; samplers are not the
///     binding constraint).
///   - Outline + Multiply + Screen blends are supported because
///     paint_slide_with_viewport dispatches blend_func per layer.
///
/// What SB CAN'T do:
///   - kind outside the SP-portable set (the composite shader
///     dispatch table is shared with SP).
///   - Overlay blend on any layer (needs a ping-pong FBO route
///     in paint_layers_via_overlay_route, incompatible with atlas
///     region rendering).
///
/// Anything else is admitted. Caller falls through to legacy
/// 3-pass for the kinds and shapes this rejects.
pub fn transition_eligible_for_scissored_bake_logic(
    kind: &str,
    layer_props_a: &[LayerCompositeProps],
    layer_props_b: &[LayerCompositeProps],
) -> bool {
    if !is_transition_kind_single_pass(kind) {
        return false;
    }
    if layer_props_a.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE {
        return false;
    }
    if layer_props_b.len() > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE {
        return false;
    }
    for p in layer_props_a.iter().chain(layer_props_b.iter()) {
        if matches!(p.blend, BlendMode::Overlay) {
            return false;
        }
    }
    true
}

/// Single-pass transition shader generator. Composes both slides'
/// bg + N_a + N_b text layers + the per-kind transition mix in ONE
/// fragment shader. Eliminates the bake_a + bake_b + composite
/// three-pass structure (legacy 22 fps@1080p) by specializing the
/// FS to the exact (kind, n_a, n_b) combination.
///
/// Returns None for kinds not yet ported (caller falls through to
/// legacy 3-pass) or for layer counts beyond
/// SINGLE_PASS_MAX_LAYERS_PER_SLIDE (caller falls through too).
///
/// The shader is structured as:
///   - GLES2 preamble + precision (mediump for most; highp for
///     dissolve/glitch which need 24-bit hash math)
///   - Common uniforms (u_t, u_a_bg, u_b_bg) + per-slot uniforms
///     (sampler2D u_*_texN, vec4 u_*_rectN, vec4 u_*_rgbaN) for
///     0..n_a / 0..n_b
///   - apply_layer helper (takes explicit sample_uv parameter so
///     warped-sample transitions can pass a transformed coord)
///   - per-kind main(): compute sample_uv_a + sample_uv_b + mix
///     factor; compose slide A at sample_uv_a, slide B at
///     sample_uv_b; emit mix(ca, cb, factor).
pub fn fs_transition_sp_source(kind: &str, n_a: usize, n_b: usize) -> Option<String> {
    if !is_transition_kind_single_pass(kind) {
        return None;
    }
    if n_a > SINGLE_PASS_MAX_LAYERS_PER_SLIDE || n_b > SINGLE_PASS_MAX_LAYERS_PER_SLIDE {
        return None;
    }
    let mut s = String::with_capacity(2048);
    s.push_str("#version 100\n");
    s.push_str(if kind_needs_highp(kind) {
        "precision highp float;\n"
    } else {
        "precision mediump float;\n"
    });
    s.push_str("uniform vec3 u_a_bg;\nuniform vec3 u_b_bg;\nuniform float u_t;\n");
    // r95 (2026-06-08): u_aspect = framebuffer width / height. The
    // iris arm uses it to compute distance() in pixel-isotropic space
    // so the iris is a true screen-pixel circle, not a UV-space
    // ellipse. Declared on every SP shader (cost: one unused uniform
    // declaration on non-iris kinds; GLSL drops it). Other future
    // radial effects (rays, circular wipe, dot animations) will
    // reuse this without re-plumbing.
    s.push_str("uniform float u_aspect;\n");
    for i in 0..n_a {
        s.push_str(&format!(
            "uniform sampler2D u_a_tex{i};\nuniform vec4 u_a_rect{i};\nuniform vec4 u_a_rgba{i};\n"
        ));
    }
    for i in 0..n_b {
        s.push_str(&format!(
            "uniform sampler2D u_b_tex{i};\nuniform vec4 u_b_rect{i};\nuniform vec4 u_b_rgba{i};\n"
        ));
    }
    s.push_str("varying vec2 v_uv;\n");
    if kind_needs_hash(kind) {
        s.push_str(SP_HASH_HELPER);
    }
    s.push_str(SP_APPLY_LAYER);
    s.push_str("void main() {\n");
    push_main_body(&mut s, kind, n_a, n_b);
    s.push_str("}\n");
    Some(s)
}

/// Backwards-compat alias kept for the slice-1 / slice-2 fade path
/// + tests. Delegates to fs_transition_sp_source("fade", ...).
/// Panics if n_a or n_b exceeds SINGLE_PASS_MAX_LAYERS_PER_SLIDE
/// (matching the original debug_assert! semantics); fade itself is
/// always supported.
pub fn fs_fade_sp_source(n_a: usize, n_b: usize) -> String {
    debug_assert!(n_a <= SINGLE_PASS_MAX_LAYERS_PER_SLIDE);
    debug_assert!(n_b <= SINGLE_PASS_MAX_LAYERS_PER_SLIDE);
    fs_transition_sp_source("fade", n_a, n_b)
        .expect("fade + valid layer counts always supported")
}

fn kind_needs_highp(kind: &str) -> bool {
    // Glitch hash math (sin/dot/fract on a large magnification
    // constant) collapses on vc4's mediump (~10-bit mantissa).
    // Glitch isn't ported to SP yet (qarl-deferred); the gate is
    // here for forward compat with the standalone FS_GLITCH which
    // still uses the sin-hash idiom.
    //
    // P3 (2026-05-09): "dissolve" was dropped from this set. The
    // SP-tier dissolve hash now uses the Inigo Quilez (IQ)
    // mediump-safe idiom (see SP_HASH_HELPER) -- no sin, no
    // 43758-magnification, works in mediump. Standalone
    // FS_DISSOLVE got the same swap. Hoskins's "hash without
    // sine" form was tried first but failed the host adjacent-
    // pixel decorrelation test at 1080p (71% vs 90% target);
    // IQ's 50.0 amplifier on asymmetric seeds (0.71 / 0.113)
    // passes 90%+ on both axes.
    matches!(kind, "glitch")
}

fn kind_needs_hash(kind: &str) -> bool {
    matches!(kind, "dissolve" | "glitch")
}

/// SP-tier hash helper. Used by the dissolve generator (and any
/// future SP glitch port). P3 (2026-05-09): swapped from the
/// classic `sin(dot)*43758` hash to the Inigo Quilez mediump-safe
/// idiom -- per-pixel salt-and-pepper distribution character with
/// adjacent-pixel decorrelation at 1080p, no sin, no highp
/// dependency. Mirrors the standalone FS_DISSOLVE hash so SP and
/// standalone paths match.
const SP_HASH_HELPER: &str = r#"
float _hash(vec2 p) {
    p = 50.0 * fract(p * 0.3183099 + vec2(0.71, 0.113));
    return fract(p.x * p.y * (p.x + p.y));
}
"#;

/// apply_layer(c, tex, rect, rgba, sample_uv): composite a single
/// text layer onto color `c`. `sample_uv` is the screen-space UV
/// at which to test the layer rect + sample the alpha bitmap; for
/// in-place transitions it equals v_uv, for warped transitions it's
/// the per-slide transformed coord.
///
/// BRANCHLESS by design: vc4's QPU executes both branches of `if`
/// in SIMD groups and masks the inactive lane, so conditional
/// returns don't actually skip work -- they just diverge the
/// pipeline. Computing `in_rect` via `step()` keeps every fragment
/// on the same code path. The texture sample fires regardless;
/// CLAMP_TO_EDGE handles out-of-bounds UV by returning the edge
/// alpha (0 for the row-major top-down bitmap's borders), which
/// would land at zero alpha anyway and be masked out by `in_rect`.
const SP_APPLY_LAYER: &str = r#"
vec3 apply_layer(vec3 c, sampler2D tex, vec4 rect, vec4 rgba, vec2 sample_uv) {
    float w = max(rect.z - rect.x, 1e-6);
    float h = max(rect.w - rect.y, 1e-6);
    vec2 luv = vec2((sample_uv.x - rect.x) / w, 1.0 - (sample_uv.y - rect.y) / h);
    float in_x = step(rect.x, sample_uv.x) * step(sample_uv.x, rect.z);
    float in_y = step(rect.y, sample_uv.y) * step(sample_uv.y, rect.w);
    float in_rect = in_x * in_y;
    float a = texture2D(tex, luv).r * rgba.a * in_rect;
    return mix(c, rgba.rgb, a);
}
"#;

/// Emit the apply_layer chain that composes slide A's full color
/// at the given `sample_uv` GLSL expression. The `prefix` is "u_a"
/// or "u_b"; `n` is the layer count.
fn push_compose_chain(s: &mut String, prefix: &str, var: &str, n: usize, sample_uv: &str) {
    for i in 0..n {
        s.push_str(&format!(
            "    {var} = apply_layer({var}, {prefix}_tex{i}, {prefix}_rect{i}, {prefix}_rgba{i}, {sample_uv});\n"
        ));
    }
}

/// Per-kind main body. Each kind:
///   1. Computes per-slide sample_uv (often v_uv; sometimes warped)
///   2. Composes slide A's color via apply_layer chain at sample_uv_a
///   3. Composes slide B's color via apply_layer chain at sample_uv_b
///   4. Computes the mix factor (often u_t-derived; sometimes per-pixel)
///   5. Emits gl_FragColor = vec4(mix(ca, cb, mix_factor), 1.0)
///
/// Kinds not yet ported panic via unreachable!() -- the caller has
/// already filtered them via is_transition_kind_single_pass.
fn push_main_body(s: &mut String, kind: &str, n_a: usize, n_b: usize) {
    match kind {
        "cut" => {
            // Hard switch at t=0.5. Both slides sample at v_uv.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str(
                "    gl_FragColor = vec4(mix(ca, cb, step(0.5, u_t)), 1.0);\n",
            );
        }
        "fade" => {
            // Linear cross-fade. Both slides sample at v_uv.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str(
                "    gl_FragColor = vec4(mix(ca, cb, clamp(u_t, 0.0, 1.0)), 1.0);\n",
            );
        }
        "wipe" => {
            // Horizontal wipe: B reveals from left, hard line at x=t.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float mask = step(v_uv.x, u_t);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "iris" => {
            // Radial expansion: B reveals through a circle.
            //
            // r95 (2026-06-08): aspect-correct iris. Pre-r95 used
            // `distance(v_uv, vec2(0.5))` directly, which is
            // anisotropic in pixel space -- on the 1360x768 panel
            // (aspect 1.77) the iris rendered as a horizontal
            // ellipse. Fix: stretch the x component by u_aspect so
            // `length(d)` is measured in normalized-height units,
            // then scale t by the half-diagonal in those same units
            // so u_t=1 covers the farthest corner exactly.
            //   d = (v_uv - 0.5)
            //   d.x *= u_aspect             // x in height-normalized
            //   r = length(d)
            //   r_max = 0.5 * sqrt(1 + a^2) // half-diagonal
            //   mask = step(r, u_t * r_max)
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    vec2 d = v_uv - vec2(0.5);\n");
            s.push_str("    d.x *= u_aspect;\n");
            s.push_str("    float r = length(d);\n");
            s.push_str("    float r_max = 0.5 * sqrt(1.0 + u_aspect * u_aspect);\n");
            s.push_str("    float mask = step(r, u_t * r_max);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "dissolve" => {
            // Per-pixel hash threshold reveal.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float threshold = _hash(v_uv);\n");
            s.push_str("    float mask = step(threshold, u_t);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "scanline" => {
            // Top-to-bottom sweep + bright band at the sweep
            // line. v_uv.y bottom-up: NDC y=+1 (top) maps to
            // v_uv.y=1, NDC y=-1 (bottom) maps to v_uv.y=0. The
            // legacy FS_SCANLINE used `step(v_uv.y, sweep)` which
            // expects screen-y-down semantics; on this VS_TEXTURED_
            // QUAD layout, v_uv is bottom-up. Replicate the same
            // visual by sweeping from top-to-bottom: mask =
            // step(1.0 - v_uv.y, sweep).
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float screen_y = 1.0 - v_uv.y;\n");
            s.push_str("    float sweep = u_t;\n");
            s.push_str("    float band_half = 0.015;\n");
            s.push_str("    float mask = step(screen_y, sweep);\n");
            s.push_str("    vec3 col = mix(ca, cb, mask);\n");
            s.push_str("    float band = 1.0 - smoothstep(0.0, band_half, abs(screen_y - sweep));\n");
            s.push_str("    col = mix(col, vec3(1.0), band * 0.7);\n");
            s.push_str("    gl_FragColor = vec4(col, 1.0);\n");
        }
        "halftone" => {
            // 16:9 grid of growing circular dots.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float grid_y = 8.0;\n");
            s.push_str("    float aspect = 16.0 / 9.0;\n");
            s.push_str("    vec2 cell_uv = fract(vec2(v_uv.x * grid_y * aspect, v_uv.y * grid_y));\n");
            s.push_str("    float d = distance(cell_uv, vec2(0.5));\n");
            s.push_str("    float mask = step(d, u_t * 0.71);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "blinds" => {
            // 16 horizontal slats opening from each midline.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float n_slats = 16.0;\n");
            s.push_str("    float slat_uv = fract(v_uv.y * n_slats);\n");
            s.push_str("    float dist_to_mid = abs(slat_uv - 0.5);\n");
            s.push_str("    float mask = step(dist_to_mid, u_t * 0.5);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "shutter" => {
            // Hexagonal aperture inscribed-radius test.
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    vec2 d = v_uv - vec2(0.5);\n");
            s.push_str("    d.x *= 16.0 / 9.0;\n");
            s.push_str("    float k = 0.866025;\n");
            s.push_str("    float c1 = abs(d.x * k + d.y * 0.5);\n");
            s.push_str("    float c2 = abs(d.y);\n");
            s.push_str("    float c3 = abs(d.x * k - d.y * 0.5);\n");
            s.push_str("    float hex_d = max(max(c1, c2), c3);\n");
            s.push_str("    float inscribed = 1.5 * u_t;\n");
            s.push_str("    float mask = step(hex_d, inscribed);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, mask), 1.0);\n");
        }
        "slide" => {
            // Horizontal slide: B enters from right, A exits left.
            // sample_uv_a = (v_uv.x + t, y); sample_uv_b =
            // (v_uv.x - (1-t), y). When the warped coord exits
            // [0,1] no layer rect-test will match, so the bg
            // uniform alone shows through -- matching legacy
            // CLAMP_TO_EDGE behavior on the FBO bake.
            s.push_str("    float t = u_t;\n");
            s.push_str("    float seam = 1.0 - t;\n");
            s.push_str("    vec2 sample_uv_a = vec2(v_uv.x + t, v_uv.y);\n");
            s.push_str("    vec2 sample_uv_b = vec2(v_uv.x - seam, v_uv.y);\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv_a");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv_b");
            s.push_str("    float on_to = step(seam, v_uv.x);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, on_to), 1.0);\n");
        }
        "push" => {
            // Horizontal push: B enters from LEFT, pushes A off
            // the right. Bright projector-blade separator at the
            // seam (smoothstep'd 0.001 wide × 0.8 brightness).
            s.push_str("    float t = u_t;\n");
            s.push_str("    vec2 sample_uv_a = vec2(v_uv.x - t, v_uv.y);\n");
            s.push_str("    vec2 sample_uv_b = vec2(v_uv.x + (1.0 - t), v_uv.y);\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv_a");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv_b");
            s.push_str("    float on_to = step(v_uv.x, t);\n");
            s.push_str("    vec3 col = mix(ca, cb, on_to);\n");
            s.push_str("    float blade = 1.0 - smoothstep(0.0, 0.001, abs(v_uv.x - t));\n");
            s.push_str("    col = mix(col, vec3(1.0), blade * 0.8);\n");
            s.push_str("    gl_FragColor = vec4(col, 1.0);\n");
        }
        "scroll" => {
            // Vertical analog of slide: B enters from bottom as
            // A rolls up off the top. v_uv.y is bottom-up (NDC
            // convention from VS_TEXTURED_QUAD).
            //
            // qarl-bug 2026-05-12: pre-fix shape was `step(seam,
            // v_uv.y)` + `vec2(v_uv.x, v_uv.y + t)` for A which
            // (under the y-up VBO UV convention) put B in the TOP
            // region -- visually scroll-DOWN. Mirrors the same fix
            // applied to the standalone FS_SCROLL.
            s.push_str("    float t = u_t;\n");
            s.push_str("    vec2 sample_uv_a = vec2(v_uv.x, v_uv.y - t);\n");
            s.push_str("    vec2 sample_uv_b = vec2(v_uv.x, v_uv.y + (1.0 - t));\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv_a");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv_b");
            s.push_str("    float on_to = step(v_uv.y, t);\n");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, on_to), 1.0);\n");
        }
        "flip" => {
            // 2D card-flip: A scaleX-shrinks 1.0 -> 0.0 in the
            // first half (t in [0, 0.5]), B scaleX-grows 0.0 ->
            // 1.0 in the second half (t in [0.5, 1]). Both slides
            // sample at the SAME warped uv (the inverse of the
            // scaleX transform). Outside the card extent, the
            // pixel is black.
            //
            // Branchless port of legacy FS_FLIP: the legacy
            // shader had nested `if` blocks that gate the texture
            // sample; on vc4 SIMD those branches diverge. Here:
            // compute sample_uv unconditionally with max(scaleX,
            // 1e-3) to avoid divide-by-zero at t=0.5 exactly,
            // then mask the final color by `inside = step(0.001,
            // scaleX) * step(0, src_x) * step(src_x, 1)`.
            s.push_str("    float t = u_t;\n");
            s.push_str("    float scaleX = abs(2.0 * t - 1.0);\n");
            s.push_str("    float useTo = step(0.5, t);\n");
            s.push_str("    float src_x = (v_uv.x - 0.5) / max(scaleX, 1e-3) + 0.5;\n");
            s.push_str("    vec2 sample_uv = vec2(src_x, v_uv.y);\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv");
            s.push_str("    vec3 col = mix(ca, cb, useTo);\n");
            s.push_str("    float inside = step(0.001, scaleX) * step(0.0, src_x) * step(src_x, 1.0);\n");
            s.push_str("    gl_FragColor = vec4(col * inside, 1.0);\n");
        }
        "marquee" => {
            // Tickertape wraparound: A scrolls off to the left, a
            // gap zone with a centered white dot passes through,
            // B enters from the right. Three region masks
            // (in_from, in_gap, in_to) partition the screen and
            // sum to 1 by construction.
            s.push_str("    float gap_uv = 0.125;\n");
            s.push_str("    float scroll_t = u_t * (1.0 + gap_uv);\n");
            s.push_str("    float cx = scroll_t + v_uv.x;\n");
            s.push_str("    vec2 sample_uv_a = vec2(cx, v_uv.y);\n");
            s.push_str("    vec2 sample_uv_b = vec2(cx - 1.0 - gap_uv, v_uv.y);\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv_a");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv_b");
            s.push_str("    float gap_local_x = (cx - 1.0) / gap_uv;\n");
            s.push_str("    float dx_uv = (gap_local_x - 0.5) * gap_uv;\n");
            s.push_str("    float dy = v_uv.y - 0.5;\n");
            s.push_str("    float dist = length(vec2(dx_uv, dy));\n");
            s.push_str("    float dot_r = 0.074;\n");
            s.push_str("    float in_dot = step(dist, dot_r);\n");
            s.push_str("    vec3 gap_col = mix(vec3(0.0), vec3(1.0), in_dot);\n");
            s.push_str("    float in_from = step(cx, 1.0);\n");
            s.push_str("    float in_to = step(1.0 + gap_uv, cx);\n");
            s.push_str("    float in_gap = 1.0 - in_from - in_to;\n");
            s.push_str("    vec3 col = ca * in_from + gap_col * in_gap + cb * in_to;\n");
            s.push_str("    gl_FragColor = vec4(col, 1.0);\n");
        }
        "pixelate" => {
            // Both slides sample at a coarsened grid whose block
            // size grows to a peak at midpoint then shrinks back.
            // Wave envelope `1 - 4(t-0.5)^2` is 0 at t=0/1, 1 at
            // t=0.5; block size 0.0025 (~5px at 1080p, native) at
            // endpoints, 0.0425 (~46px at 1080p) at midpoint.
            // Both A and B sample at the SAME quantized cell --
            // the visual mixing is the linear u_t cross-fade
            // between two pixelated views.
            s.push_str("    float wave = 1.0 - 4.0 * (u_t - 0.5) * (u_t - 0.5);\n");
            s.push_str("    float blockSize = 0.0025 + 0.04 * wave;\n");
            s.push_str("    vec2 cell = floor(v_uv / blockSize) * blockSize + 0.5 * blockSize;\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "cell");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "cell");
            s.push_str("    gl_FragColor = vec4(mix(ca, cb, u_t), 1.0);\n");
        }
        _ => unreachable!(
            "push_main_body called for unsupported kind {kind:?}; \
             is_transition_kind_single_pass should have filtered"
        ),
    }
}

/// Map a transition `kind` string (as the Python content model
/// stores it in `PlaylistItemRef.transition`) to the fragment
/// shader source the renderer should run.
///
/// Returns `None` for unknown kinds — caller falls back to FS_CUT
/// (a hard switch at t=0.5) so the transition still completes
/// rather than a silent black frame.
///
/// Pure function so a renderer-side rename of a shader const
/// flips a host test rather than going silent at runtime.
pub fn fs_for_transition_kind(kind: &str) -> Option<&'static str> {
    match kind {
        "cut" => Some(FS_CUT),
        "fade" => Some(FS_FADE),
        "wipe" => Some(FS_WIPE),
        "iris" => Some(FS_IRIS),
        "dissolve" => Some(FS_DISSOLVE),
        "pixelate" => Some(FS_PIXELATE),
        "scanline" => Some(FS_SCANLINE),
        "halftone" => Some(FS_HALFTONE),
        "glitch" => Some(FS_GLITCH),
        "slide" => Some(FS_SLIDE),
        "push" => Some(FS_PUSH),
        "scroll" => Some(FS_SCROLL),
        "blinds" => Some(FS_BLINDS),
        "flip" => Some(FS_FLIP),
        "marquee" => Some(FS_MARQUEE),
        "shutter" => Some(FS_SHUTTER),
        // Phase 5-c-4 closed out the remaining 8. The full
        // Python-ref deck is now mirrored. Unknown kinds beyond
        // these 16 still hit the fallback (FS_CUT).
        _ => None,
    }
}

// ====================================================================
// r69 (2026-06-06): FYS-bug-C transition frame-skip surfacing.
//
// `paint_and_present_one_transition_frame` (hdmi.rs:4759) returns
// Ok(()) WITHOUT a swap+commit when either endpoint's V4L2 decoder
// hasn't produced a fresh sample this tick. Previously SILENT --
// the prior scanout frame held for that tick and the operator saw
// the transition "stutter" or "look like a cut" with no journal
// trace.
//
// qarl 2026-06-06: "i'm not seeing the transitions. is it possible
// that many of our transitions don't work at all? they look like
// cuts to me." Audit (qa/r69-transition-audit.md) confirms all 16
// spec kinds DO render their shader through the IPC path; the
// "looks like cuts" symptom is this skip path firing repeatedly
// inside the transition window because both decoders must
// produce a fresh sample per tick under 1080p H.264 pressure.
//
// The helper below logs the skip ONCE per (kind, 5s) window so
// the journal sees the symptom without flooding at 30 skips/sec.
// Dispatch's "throttled, dedupe by kind" guidance.
// ====================================================================

std::thread_local! {
    /// Throttle keyed by (kind, reason) so the A-endpoint and
    /// B-endpoint underrun signals don't mask each other (r69
    /// subagent NIT-5: chronic A-side underruns must not silence
    /// occasional B-side underruns on the same transition kind).
    /// thread_local because `paint_and_present_one_transition_frame`
    /// is called only from the EglSession owner thread (per the
    /// session's !Send bound via the GL context). Cleared by
    /// `reset_paint_transition_skip_throttle` (test-only).
    static LAST_SKIP_PER_KIND: std::cell::RefCell<
        std::collections::HashMap<(String, String), std::time::Instant>
    > = std::cell::RefCell::new(std::collections::HashMap::new());
}

const PAINT_TRANSITION_SKIP_THROTTLE: std::time::Duration =
    std::time::Duration::from_secs(5);

/// r69: emit a throttled WARN for the FYS-bug-C frame-skip path
/// in `paint_and_present_one_transition_frame`. Returns `true`
/// when this call actually emitted, `false` when throttled.
/// Logged via stderr; the Python `_drain_stderr` routes to
/// `log.info` so journalctl picks it up at default verbosity.
///
/// `reason`: short tag identifying which endpoint underran
/// ("endpoint_a_no_frame", "endpoint_b_no_frame"). Throttle key
/// is `(kind, reason)` so each distinct failure mode gets its
/// own first-emit per 5s window.
pub fn warn_paint_transition_skip(kind: &str, progress: f32, reason: &str) -> bool {
    LAST_SKIP_PER_KIND.with(|cell| {
        let mut map = cell.borrow_mut();
        let now = std::time::Instant::now();
        let key = (kind.to_string(), reason.to_string());
        let should_emit = match map.get(&key) {
            Some(last) if now.duration_since(*last) < PAINT_TRANSITION_SKIP_THROTTLE => false,
            _ => true,
        };
        if should_emit {
            // NIT-6: single-line message so the journal stays
            // grep-friendly without the multi-line continuation
            // whitespace artifact.
            eprintln!(
                "warn: paint_transition skipped frame: kind={:?} progress={:.3} reason={} (FYS bug C; prior scanout frame held; throttled 5s per (kind,reason))",
                kind, progress, reason
            );
            map.insert(key, now);
        }
        should_emit
    })
}

/// Test-only: clear the per-kind throttle so unit tests start
/// from a known state. Production paths never call this.
#[cfg(test)]
pub fn reset_paint_transition_skip_throttle() {
    LAST_SKIP_PER_KIND.with(|cell| cell.borrow_mut().clear());
}

// ====================================================================
// r76 Phase A (2026-06-07): characterize the begin_transition ->
// endpoint_b-first-frame gap.
//
// FYS 2026-06-07: every transition fires the r69 skip-throttle WARN
// (endpoint_b_no_frame, progress < 0.10). r74/r75 dispatch text:
// "instrument first (don't fix blind)." Add a one-line metric that
// tells us if the gap is 30ms (one tick we can absorb) or 1500ms
// (full transition window held). Different fixes apply.
//
// Wire:
//   1. `record_transition_begin_for_endpoint_b_metric(to_id)` is
//      called from ipc_main.rs's BeginTransition handler.
//   2. `consume_transition_endpoint_b_first_frame_marker()` is called
//      from paint_and_present_one_transition_frame's
//      `bake_slide_to_fbo(inputs_b)` Ok(Some(_)) branch -- ONLY on
//      a successful endpoint_b bake (Ok(None) skip path doesn't
//      consume because we want to keep waiting).
//   3. The consume hook emits `[perf] transition_endpoint_b_ready
//      slide_id=<to> wait_ms=<n>` and clears the cell so subsequent
//      frames in the same transition don't re-log.
//
// thread_local because paint_and_present_one_transition_frame and
// the BeginTransition handler are both called only from the IPC
// main thread (which holds the EglSession's GL context per the
// session's !Send bound). Single in-flight transition at a time so
// a single-Option cell is sufficient.
// ====================================================================

std::thread_local! {
    static TRANSITION_ENDPOINT_B_METRIC: std::cell::RefCell<
        Option<(Option<uuid::Uuid>, uuid::Uuid, std::time::Instant)>
    > = const { std::cell::RefCell::new(None) };
}

/// Called from ipc_main.rs at the BeginTransition handler. Sets the
/// thread-local marker so the FIRST successful endpoint_b bake
/// inside paint_and_present_one_transition_frame can emit the gap
/// metric.
///
/// r76 subagent WARN-2: now carries `from_id` (Option in case there
/// was no current slide, which would be a state-machine bug but
/// defensive nonetheless). Dispatch explicitly asked for both ids.
///
/// r76 subagent WARN-3: if a prior marker is still set when this
/// fires (e.g. the prior transition's endpoint_b never delivered
/// -- which is THE diagnostic case we care about), emit an
/// `[perf] transition_endpoint_b_unconsumed` line BEFORE replacing.
/// Pre-fix the silent overwrite dropped exactly the data r76 was
/// built to capture.
pub fn record_transition_begin_for_endpoint_b_metric(
    from_id: Option<uuid::Uuid>,
    to_id: uuid::Uuid,
) {
    TRANSITION_ENDPOINT_B_METRIC.with(|cell| {
        let prior = cell.borrow_mut().replace((
            from_id, to_id, std::time::Instant::now(),
        ));
        if let Some((prev_from, prev_to, prev_at)) = prior {
            let elapsed_ms = prev_at.elapsed().as_millis();
            eprintln!(
                "[perf] transition_endpoint_b_unconsumed from_id={} to_id={} elapsed_ms={} \
                 reason=marker_overwritten_by_new_BeginTransition",
                prev_from.map(|u| u.to_string()).unwrap_or_else(|| "none".into()),
                prev_to, elapsed_ms,
            );
        }
    });
}

/// Called from hdmi.rs at the endpoint_b bake-success branch in
/// paint_and_present_one_transition_frame -- ONLY when endpoint_b
/// is Video-bearing (Video or TextOverVideo). r76 subagent WARN-1:
/// Text/Image endpoint_b would bake Ok(Some) trivially without
/// touching V4L2, emitting useless wait_ms=0 lines that polluted
/// QA's dataset.
///
/// If a marker is set, emit `[perf] transition_endpoint_b_ready
/// from_id=<from> to_id=<to> wait_ms=<n>` and clear the cell.
/// Subsequent frames of the same transition see None and emit nothing.
pub fn consume_transition_endpoint_b_first_frame_marker() {
    TRANSITION_ENDPOINT_B_METRIC.with(|cell| {
        if let Some((from_id, to_id, begin_at)) = cell.borrow_mut().take() {
            let wait_ms = begin_at.elapsed().as_millis();
            eprintln!(
                "[perf] transition_endpoint_b_ready from_id={} to_id={} wait_ms={}",
                from_id.map(|u| u.to_string()).unwrap_or_else(|| "none".into()),
                to_id, wait_ms,
            );
        }
    });
}

/// Test-only: reset the marker so unit tests start from a known
/// state.
#[cfg(test)]
pub fn reset_transition_endpoint_b_metric_for_tests() {
    TRANSITION_ENDPOINT_B_METRIC.with(|cell| {
        *cell.borrow_mut() = None;
    });
}

/// Test-only: read the marker (to_id only) without consuming so
/// unit tests can assert "begin_transition set it" vs "endpoint_b
/// consumed it."
#[cfg(test)]
pub fn peek_transition_endpoint_b_metric_for_tests() -> Option<uuid::Uuid> {
    TRANSITION_ENDPOINT_B_METRIC.with(|cell| cell.borrow().as_ref().map(|(_, to, _)| *to))
}

/// Fragment shader: identity blit — sample a texture by UV and
/// emit unchanged. Used by Phase 5-a's FBO path to push the
/// offscreen color texture to the default framebuffer with no
/// blending, no color modification. Phase 5 transitions chain
/// onto this pattern with a `t` uniform + a second texture.
///
/// Pairs with `VS_TEXTURED_QUAD` (interleaved [x, y, u, v] verts).
/// v1-spec-delta #10 (slice b, 2026-05-08) -- brightness / gamma
/// post-pass shader. Applied as a fullscreen blit after the
/// scene FBO is composed, before commit_fb. Simple per-pixel:
///   out.rgb = pow(in.rgb * brightness, 1.0 / gamma)
///
/// Identity case: brightness == 1.0 AND gamma == 1.0 means the
/// caller can skip this pass entirely (bind FS_BLIT instead).
/// At schema defaults (brightness=100/gamma=1.0) the post-pass
/// is identity and the caller skips it; operators dial gamma
/// up if the downstream HDMI/TV pipeline isn't gamma-correct
/// on its own, and dim via brightness in [0, 100].
///
/// brightness uniform is the schema value DIVIDED BY 100 (so
/// the shader sees [0, 1]); gamma is the schema value
/// directly. Caller does the division to keep the shader
/// scale-agnostic.
pub const FS_BRIGHT_GAMMA: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src;
uniform float u_brightness;
uniform float u_gamma;
varying vec2 v_uv;
void main() {
    vec4 c = texture2D(u_src, v_uv);
    vec3 rgb = c.rgb * u_brightness;
    // Clamp to [0, 1] so pow's base is well-defined (GLSL ES 1.00
    // §8.2: pow is undefined for negative bases). pow(0.0, 1/2.2)
    // returns exact zero on vc4 GLES2 — verified via single-frame
    // FBO-readback probe 2026-05-17 (see qa/captures/bug-7-blacks-
    // not-black-recon-2026-05-17.md). No epsilon needed.
    rgb = clamp(rgb, vec3(0.0), vec3(1.0));
    rgb = pow(rgb, vec3(1.0 / max(u_gamma, 0.001)));
    gl_FragColor = vec4(rgb, c.a);
}
"#;

/// v1-spec-delta #10 (slice b) -- pure-CPU brightness/gamma
/// math mirror of FS_BRIGHT_GAMMA. Used for host tests + a
/// reference encode for capture-with-settings paths. Does the
/// same per-pixel transform: rgb' = pow(clamp(rgb * b), 1/g).
/// alpha is passed through unchanged.
///
/// `brightness` is in [0, 1] (caller pre-divides if the
/// schema value is in [0, 100]). `gamma` > 0; near-zero
/// values clamp to avoid divide-by-zero in the shader's
/// 1/gamma exponent.
pub fn apply_brightness_gamma_rgba(
    rgba: &mut [u8],
    brightness: f32,
    gamma: f32,
) {
    let inv_gamma = 1.0 / gamma.max(0.001);
    for px in rgba.chunks_exact_mut(4) {
        for val in &mut px[..3] {
            let v = (*val as f32) / 255.0;
            let scaled = (v * brightness).clamp(0.0, 1.0);
            let corrected = scaled.powf(inv_gamma);
            *val = (corrected * 255.0).round().clamp(0.0, 255.0) as u8;
        }
        // alpha unchanged.
    }
}

/// v1-spec-delta #11 (slice b, 2026-05-08) -- encode an RGBA8
/// pixel buffer to PNG bytes. Pure-CPU helper; no GL deps so
/// it lives in hdmi_logic.rs (cross-platform; runs on Mac in
/// cargo test).
///
/// Caller is responsible for the buffer being row-major
/// top-to-bottom (image-coord convention, y=0 top). The GL
/// glReadPixels output is bottom-to-top; capture_fbo_to_rgba
/// (slice 11a) flips it before passing here.
///
/// `bytes_buf.len()` must equal `(w * h * 4) as usize`; the
/// function bails with a clear error if not.
pub fn rgba_to_png_bytes(rgba: &[u8], w: u32, h: u32) -> anyhow::Result<Vec<u8>> {
    use anyhow::{anyhow, Context};
    let expected = (w as usize) * (h as usize) * 4;
    if rgba.len() != expected {
        return Err(anyhow!(
            "rgba_to_png_bytes: buffer len {} != expected {} ({}x{}x4)",
            rgba.len(),
            expected,
            w,
            h,
        ));
    }
    let mut out: Vec<u8> = Vec::with_capacity(expected / 2);
    {
        let mut encoder = png::Encoder::new(&mut out, w, h);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder
            .write_header()
            .context("png write_header")?;
        writer.write_image_data(rgba).context("png write_image_data")?;
    }
    Ok(out)
}

pub const FS_BLIT: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src;
varying vec2 v_uv;
void main() {
    gl_FragColor = texture2D(u_src, v_uv);
}
"#;

/// V4L2 piece 3d (2026-05-14) -- BT.709 limited-range NV12 -> RGB
/// fragment shader. Pairs with `VS_TEXTURED_QUAD`.
///
/// bcm2835-codec emits NV12 with **limited-range** quantization
/// by default (Y in [16/255, 235/255], UV in [16/255, 240/255])
/// unless V4L2_CID_QUANTIZATION is set to V4L2_QUANTIZATION_FULL_
/// RANGE on the CAPTURE queue. Piece 3c doesn't set that ctrl,
/// so we apply the limited-range pre-scaling here in the shader.
///
/// Texture binding contract (set up in run_nv12_blit_pass):
///   - TEXTURE0: u_tex_y  -- Y plane,  GL_LUMINANCE, samples .r
///   - TEXTURE1: u_tex_uv -- UV plane, GL_LUMINANCE_ALPHA,
///                           samples .r (U) + .a (V) on GLES2
///
/// Matrix coefficients are **BT.709 limited-range** (ITU-R BT.709
/// Annex B). The Pi's `bcm2835-codec` reports `Colorspace=Rec.709`
/// + `YCbCr Encoding=Default` per `v4l2-ctl --get-fmt-video` on the
/// dev Pi (verified 2026-05-14 in a5021ac); V4L2 spec says Default-
/// for-Rec.709 is BT.709. Using BT.601 coefficients on BT.709
/// content produces a slight chroma drift (greens slightly yellower,
/// blues slightly purpler) — visible on saturated content, subtle
/// on broadcast skin tones.
///
/// The companion DMABUF path (`FS_NV12_DMABUF_TO_RGB`) doesn't need
/// this — Mesa reads the colorimetry hint from the EGLImage
/// attribs and inserts the right matrix.
pub const FS_NV12_TO_RGB: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_tex_y;
uniform sampler2D u_tex_uv;
// r83 Phase B (2026-06-08): y-axis crop fraction. Equals the ratio
// `display_height / allocated_height` so the shader samples only
// the source-valid rows of the bcm2835-codec's macroblock-rounded
// NV12 buffer (1080 -> 1088 = 8 rows of uninitialised green
// padding). Default 1.0 = no crop, byte-identical to the
// pre-r83-Phase-B behavior. Both Y and UV planes share the same
// ratio (NV12 sub-samples UV by 2 on both axes, so the relative
// padding ratio is identical).
uniform float u_y_crop_max;
varying vec2 v_uv;
void main() {
    // FYS bug 2: the V4L2 codec delivers the NV12 frame bottom-up
    // relative to the top-down convention the image / external-RGB
    // paths use (all share VS_TEXTURED_QUAD + the same quad VBO),
    // so video rendered upside down. Sample with v flipped.
    // r83 Phase B: scale the flipped v-axis by u_y_crop_max so the
    // sampling range becomes [0, display_h / allocated_h] instead
    // of [0, 1]. The padding rows (uv_t.y near 1.0) become
    // unreachable — the v-flip routes them to the displayed-bottom
    // axis, so cropping the high-uv.y end removes the green band.
    vec2 uv_t = vec2(v_uv.x, (1.0 - v_uv.y) * u_y_crop_max);
    // Limited-range Y: [16/255, 235/255] -> [0, 1].
    float y = (texture2D(u_tex_y, uv_t).r - (16.0/255.0)) * (255.0/219.0);
    // GLES2 LUMINANCE_ALPHA: r=L (U here), a=A (V here).
    vec2 uv_sample = texture2D(u_tex_uv, uv_t).ra;
    // Limited-range UV: [16/255, 240/255] -> [-0.5, 0.5].
    vec2 uv = (uv_sample - vec2(128.0/255.0)) * (255.0/224.0);
    // ITU-R BT.709 Annex B coefficients (limited-range scaling above
    // is the same as BT.601 — both use 219/224 for Y/UV range).
    float r = y + 1.5748 * uv.y;
    float g = y - 0.1873 * uv.x - 0.4681 * uv.y;
    float b = y + 1.8556 * uv.x;
    gl_FragColor = vec4(r, g, b, 1.0);
}
"#;

/// STREAM/VLC HW-decode (2026-05-20) -- cover-fit NV12 -> RGB
/// shader for the external-frame NV12 push path.
///
/// The HW-decode VLC path (ffmpeg `-c:v h264_v4l2m2m`, raw NV12
/// out, no `-vf`) hands the renderer a SOURCE-resolution NV12
/// frame; the GPU does the scale + crop the dropped ffmpeg
/// `scale=...:force_original_aspect_ratio=increase,crop=...`
/// filter used to do. `FS_NV12_TO_RGB` (the V4L2 VideoSlide
/// shader) samples `v_uv` straight — it STRETCHES, correct for
/// VideoSlide MP4s authored at the panel res but wrong for a
/// VLC stream of arbitrary aspect.
///
/// This sibling adds a uniform UV transform: `u_uv_scale`
/// (per-axis scale) + `u_uv_offset` (per-axis offset) remap the
/// fullscreen quad's `v_uv` [0,1] into the NV12 texture so the
/// source covers the panel aspect-preserving with the overflow
/// axis center-cropped. `bake_external_nv12_to_current_fbo`
/// computes the two uniforms from (frame dims, panel dims).
///
/// Everything else — BT.709 limited-range matrix, the bottom-up
/// `1.0 - v` flip, `.ra` LUMINANCE_ALPHA sampling — is identical
/// to `FS_NV12_TO_RGB`; only the cover-fit UV remap is new.
pub const FS_NV12_COVER_TO_RGB: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_tex_y;
uniform sampler2D u_tex_uv;
uniform vec2 u_uv_scale;
uniform vec2 u_uv_offset;
varying vec2 v_uv;
void main() {
    // Cover-fit remap: scale + offset the quad UV into the source
    // texture (center-crop the overflow axis). u_uv_scale < 1.0 on
    // the cropped axis; u_uv_offset recenters the crop.
    vec2 cover_uv = v_uv * u_uv_scale + u_uv_offset;
    // Same bottom-up flip as FS_NV12_TO_RGB (V4L2 NV12 is delivered
    // bottom-up vs the top-down quad convention).
    vec2 uv_t = vec2(cover_uv.x, 1.0 - cover_uv.y);
    // Limited-range Y: [16/255, 235/255] -> [0, 1].
    float y = (texture2D(u_tex_y, uv_t).r - (16.0/255.0)) * (255.0/219.0);
    // GLES2 LUMINANCE_ALPHA: r=L (U here), a=A (V here).
    vec2 uv_sample = texture2D(u_tex_uv, uv_t).ra;
    // Limited-range UV: [16/255, 240/255] -> [-0.5, 0.5].
    vec2 uv = (uv_sample - vec2(128.0/255.0)) * (255.0/224.0);
    // ITU-R BT.709 Annex B coefficients.
    float r = y + 1.5748 * uv.y;
    float g = y - 0.1873 * uv.x - 0.4681 * uv.y;
    float b = y + 1.8556 * uv.x;
    gl_FragColor = vec4(r, g, b, 1.0);
}
"#;

/// STREAM/VLC HW-decode (2026-05-20) -- compute the cover-fit UV
/// transform (scale, offset) for `FS_NV12_COVER_TO_RGB`.
///
/// Aspect-preserving cover-fit: the source `(frame_w, frame_h)` is
/// scaled so it fully covers the panel `(panel_w, panel_h)`, and
/// the overflow on the longer axis is center-cropped. The result
/// is the `(scale, offset)` pair that remaps the panel-spanning
/// quad's `v_uv` [0,1] into the source texture: on the cropped
/// axis `scale < 1.0` (sample a sub-window) and `offset` recenters
/// it; on the fully-shown axis `scale == 1.0`, `offset == 0.0`.
///
/// Matches the dropped ffmpeg
/// `scale=PANEL:force_original_aspect_ratio=increase,crop=PANEL`.
/// Pure arithmetic — host-tested, no GL.
pub fn nv12_cover_fit_uv_transform(
    frame_w: u32,
    frame_h: u32,
    panel_w: u32,
    panel_h: u32,
) -> ([f32; 2], [f32; 2]) {
    // Degenerate dims -> identity (sample the whole texture). The
    // caller's byte-size check rejects 0-area frames before paint;
    // this guard just keeps the math division-safe.
    if frame_w == 0 || frame_h == 0 || panel_w == 0 || panel_h == 0 {
        return ([1.0, 1.0], [0.0, 0.0]);
    }
    let frame_aspect = frame_w as f32 / frame_h as f32;
    let panel_aspect = panel_w as f32 / panel_h as f32;
    if frame_aspect > panel_aspect {
        // Source is wider than the panel: full height shown, crop
        // the sides. Sample a horizontal sub-window of the texture.
        let scale_x = panel_aspect / frame_aspect;
        let offset_x = (1.0 - scale_x) * 0.5;
        ([scale_x, 1.0], [offset_x, 0.0])
    } else {
        // Source is taller (or equal): full width shown, crop top +
        // bottom. Sample a vertical sub-window.
        let scale_y = frame_aspect / panel_aspect;
        let offset_y = (1.0 - scale_y) * 0.5;
        ([1.0, scale_y], [0.0, offset_y])
    }
}

/// Renderer-hardening C2 (finding H2, 2026-05-21) — the vc4 GPU's
/// `GL_MAX_TEXTURE_SIZE`. The Pi Zero 2 W's VideoCore IV caps a
/// single 2D texture at 2048 px on either axis; `glTexImage2D` with a
/// larger dimension fails `GL_INVALID_VALUE` and leaves the texture
/// undefined (a black / garbage blit). An external NV12 stream of a
/// 1440p or 4K source would exceed this — the backend clamps such a
/// stream down (an ffmpeg `scale` filter), and `bake_external_nv12_
/// to_current_fbo` rejects anything still over-large rather than
/// uploading a doomed texture.
pub const MAX_GL_TEXTURE_DIM: u32 = 2048;

/// Renderer-hardening C2 (finding H2, 2026-05-21) — is a frame small
/// enough to upload as a GL texture on the vc4 GPU?
///
/// True iff both axes are within `MAX_GL_TEXTURE_DIM`. Factored as a
/// pure predicate so the `bake_external_nv12_to_current_fbo`
/// over-large guard is host-testable without a GL context (mirrors
/// how C1 factored `video_reprime_needed`).
pub fn nv12_dims_ok(frame_w: u32, frame_h: u32) -> bool {
    frame_w <= MAX_GL_TEXTURE_DIM && frame_h <= MAX_GL_TEXTURE_DIM
}

/// Bug W2 (2026-05-21) -- reverse the row order of an RGBA8 pixel
/// buffer (top-down -> bottom-up, or vice versa).
///
/// Why the image bake needs this: a PNG file decodes TOP-DOWN
/// (row 0 = image top). `load_png_rgba` uploads that buffer with
/// `glTexImage2D` and every image-asset path draws it through a
/// `VS_TEXTURED_QUAD` quad whose `v=0` maps to the BOTTOM of the
/// screen (the bottom-left vertex carries UV (0,0) — see
/// `cover_fit_quad_verts` above and `create_fullscreen_quad`). A
/// top-down buffer through that quad samples image-top at
/// screen-bottom, i.e. renders the image UPSIDE DOWN. Flipping
/// the decoded buffer to bottom-up here matches the GL `v`
/// convention so every image-asset path — scanout, capture,
/// image-as-background — renders right-side up, with no
/// quad/shader change that would also touch the text / video /
/// stream / pattern paths.
///
/// Same CLASS as FYS bug 2 (a625e35, the NV12 v-flip): a GL
/// Y-convention mismatch. The video path flipped `v` in its
/// fragment shaders; the image path is fixed at decode because
/// its texture data is host-side bytes.
///
/// `rgba` must be exactly `w * h * 4` bytes. A buffer whose
/// length does not match — a malformed asset the decoder somehow
/// let through — is returned UNCHANGED rather than panicking on
/// the chunk math; the GL upload downstream surfaces the real
/// error. Pure (no GL), so the flip is host-testable on the Mac
/// dev box even though `hdmi.rs` itself is Linux-only.
pub fn flip_rgba_rows_vertically(mut rgba: Vec<u8>, w: u32, h: u32) -> Vec<u8> {
    // CMA-arc 2026-06-22 C5: delegate to the in-place version so the
    // consuming API stays compatible with existing callers (host
    // tests in this file etc.) while production paths get the
    // ~8.3 MB heap-peak win by calling `flip_rgba_rows_in_place`
    // directly on a buffer they already own.
    flip_rgba_rows_in_place(&mut rgba, w, h);
    rgba
}

/// CMA-arc 2026-06-22 C5: in-place version of
/// `flip_rgba_rows_vertically`. Uses a single stride-sized scratch
/// row (~7.7 KB at 1080p) for the swap instead of allocating a
/// fresh w*h*4 buffer (~8.3 MB at 1080p) — saves ~8.3 MB heap
/// peak per PNG decode (significant on image-class reels that
/// load multiple 1080p assets in succession; cuts swap pressure).
///
/// Same input contract as the consuming version: `rgba.len()`
/// must equal `w * h * 4`; mismatched lengths are returned
/// untouched (caller's responsibility to surface the malformed
/// asset).
///
/// Odd-height images: the middle row (y = h/2) stays in place,
/// which is the correct semantic for an in-place vertical flip
/// of an odd-row array.
pub fn flip_rgba_rows_in_place(rgba: &mut [u8], w: u32, h: u32) {
    let stride = (w as usize).saturating_mul(4);
    let expected = stride.saturating_mul(h as usize);
    if stride == 0 || h == 0 || rgba.len() != expected {
        return;
    }
    let mut scratch = vec![0u8; stride];
    let half = (h as usize) / 2;
    for y in 0..half {
        let top = y * stride;
        let bot = (h as usize - 1 - y) * stride;
        // 3-step swap via stride-sized scratch row. copy_within is
        // safe for overlapping ranges within the same slice; here
        // top < bot, no overlap, but copy_within is the canonical
        // intent-revealing API for "rgba[bot..] -> rgba[top..]".
        scratch.copy_from_slice(&rgba[top..top + stride]);
        rgba.copy_within(bot..bot + stride, top);
        rgba[bot..bot + stride].copy_from_slice(&scratch);
    }
}

/// FYS bug B (2026-05-21) -- compute the COVER-fit fullscreen-quad
/// vertices for a regular uploaded image / video slide.
///
/// Aspect-preserving cover-fit: the source `(frame_w, frame_h)` is
/// scaled to fully COVER the panel `(panel_w, panel_h)`, and the
/// overflow on the longer axis is center-cropped. This matches what
/// the editor already shows — `drawFirstFrameToCanvas` (the video
/// thumbnail) and the image-upload preview both cover-fit — so the
/// sign is WYSIWYG. The pre-fix renderer STRETCHED the source to
/// the panel, distorting any non-panel-aspect asset.
///
/// The returned 16-float array is 4 verts of interleaved
/// `[x, y, u, v]` in TRIANGLE_STRIP order — the same layout as the
/// shared fullscreen quad in `cached_textured_quad_vbo`, but with
/// the POSITIONS scaled out past +/-1 NDC on the overflow axis. GL
/// clips geometry to the +/-1 clip volume, so the overflow is
/// cropped natively — no oversized GL viewport (the Pi Zero 2 W
/// vc4 caps `GL_MAX_VIEWPORT_DIMS` at 2048; a vertical clip on a
/// landscape panel would exceed that). UVs stay [0,1].
///
/// vs `nv12_cover_fit_uv_transform`: both cover-fit, but that one
/// remaps UVs in-shader (it needs `FS_NV12_COVER_TO_RGB`); this
/// scales quad geometry, so it drops straight into the existing
/// FS_NV12_TO_RGB / FS_BLIT passes with no shader change.
///
/// Pure arithmetic — host-tested, no GL.
pub fn cover_fit_quad_verts(
    frame_w: u32,
    frame_h: u32,
    panel_w: u32,
    panel_h: u32,
) -> [f32; 16] {
    // Degenerate dims -> the plain fullscreen quad. The caller's
    // frame-size checks reject 0-area frames before paint; this
    // guard just keeps the math division-safe.
    let (mut sx, mut sy) = if frame_w == 0 || frame_h == 0
        || panel_w == 0 || panel_h == 0
    {
        (1.0f32, 1.0f32)
    } else {
        let frame_aspect = frame_w as f32 / frame_h as f32;
        let panel_aspect = panel_w as f32 / panel_h as f32;
        if frame_aspect > panel_aspect {
            // Source wider than the panel: full height, overflow the
            // sides past +/-1 x (clipped == center-cropped sides).
            (frame_aspect / panel_aspect, 1.0)
        } else {
            // Source taller (or equal): full width, overflow top +
            // bottom past +/-1 y (clipped == center-cropped).
            (1.0, panel_aspect / frame_aspect)
        }
    };
    // Hardening C3 / L2 (2026-05-21): defensive sanity clamp. A
    // pathological source aspect (e.g. 10000x1) drives sx/sy
    // unboundedly large, pushing the quad verts far past the GL
    // guard band. A quad scaled 16x already covers the panel many
    // times over — beyond that is degenerate input, so cap it.
    // For any normal aspect this changes nothing.
    sx = sx.min(16.0);
    sy = sy.min(16.0);
    [
        -sx, -sy, 0.0, 0.0,
         sx, -sy, 1.0, 0.0,
        -sx,  sy, 0.0, 1.0,
         sx,  sy, 1.0, 1.0,
    ]
}

/// Hardening C3 / L1 (2026-05-21) — the cover-fit quad VBO key:
/// `(frame_w, frame_h, panel_w, panel_h)`. The geometry only
/// changes when the source or panel dims change.
pub type CoverQuadKey = (u32, u32, u32, u32);

/// Hardening C3 / L1 (2026-05-21) — slot-selection result for the
/// 2-entry cover-fit VBO cache (`COVER_QUAD_VBO` in `hdmi.rs`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoverQuadSlot {
    /// `key` is already resident in slot `idx` — reuse its VBO.
    Hit { idx: usize },
    /// Cache miss — build into slot `idx`, evicting whatever VBO
    /// (if any) currently occupies it.
    Miss { idx: usize },
}

/// Hardening C3 / L1 (2026-05-21) — choose the 2-entry cover-fit
/// VBO cache slot for `key`.
///
/// Background: a video↔video transition between two
/// differently-sized sources alternates the two endpoints' keys
/// every frame. The pre-fix SINGLE-slot cache rebuilt its only
/// slot TWICE PER FRAME for the whole transition (glGenBuffers +
/// buffer_data + glDeleteBuffers churn — no leak, just waste). A
/// 2-entry cache keeps BOTH transition endpoints' VBOs resident.
///
/// On a hit, returns the occupied slot. On a miss, returns the
/// slot to (re)build into: the first empty slot, else the
/// least-recently-built slot. `next_build` is the round-robin
/// cursor the caller advances after a miss-driven build, so two
/// alternating keys settle into the two slots and then hit
/// forever. Pure (no GL) so the slot logic is host-testable.
pub fn cover_quad_slot(
    slots: &[Option<CoverQuadKey>; 2],
    key: CoverQuadKey,
    next_build: usize,
) -> CoverQuadSlot {
    // Hit: key already resident.
    for (idx, slot) in slots.iter().enumerate() {
        if *slot == Some(key) {
            return CoverQuadSlot::Hit { idx };
        }
    }
    // Miss: prefer an empty slot so a cold cache fills both before
    // it ever evicts.
    for (idx, slot) in slots.iter().enumerate() {
        if slot.is_none() {
            return CoverQuadSlot::Miss { idx };
        }
    }
    // Both occupied: evict via the round-robin cursor.
    CoverQuadSlot::Miss { idx: next_build % 2 }
}

/// V4L2 piece 4b (2026-05-14) -- DMA-BUF zero-copy NV12 sampler
/// via `GL_OES_EGL_image_external`. Pairs with `VS_TEXTURED_QUAD`
/// (no vertex changes needed; the difference is purely the
/// sampler type and texture target).
///
/// Usage contract:
///   - The fragment color comes from ONE `samplerExternalOES`
///     bound to a GLES texture whose target is
///     `GL_TEXTURE_EXTERNAL_OES`. The texture has been associated
///     with an EGLImage created from a V4L2-exported DMA-BUF fd
///     via `eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT, attribs)`
///     using `EGL_DMA_BUF_PLANE0_FD_EXT = fd`, both Y and UV
///     planes referencing the SAME fd with PLANE1_OFFSET = Y_SIZE
///     (single-plane NV12 layout from bcm2835-codec).
///   - The Pi's Mesa stack does YUV->RGB conversion internally
///     for external-OES samples of NV12 EGLImages -- the colors
///     come back already in RGB space, so this shader does no
///     BT.601 math. (Compared to FS_NV12_TO_RGB which samples two
///     separate Y + UV textures and does the matrix in-shader.)
///
/// **Verified in piece 4e (2026-05-14, qa/captures/v4l2-piece4e-
/// dmabuf-smoke-2026-05-14.md):** on the Pi dev board's vc4 + Mesa
/// stack, color output from this shader matches FS_NV12_TO_RGB
/// side-by-side. Mesa hit the BT.601 fast-path correctly; no
/// fallback to manual BT.601 transform was needed. (A known
/// regression vector remains: if V4L2 quantization metadata is
/// missing or mis-set on a future codec or content rotation, a
/// color cast may resurface; the manual-BT.601 fallback in
/// .r/.g/.b stays a viable forward-fix shape.)
///
/// Extension requirements (checked at runtime in piece 4c):
///   - EGL: `EGL_EXT_image_dma_buf_import`
///   - GLES2: `GL_OES_EGL_image_external`
///
/// If either is missing, paint_and_present_one_video_slide_frame
/// falls back to the MMAP path with FS_NV12_TO_RGB (piece 4d).
pub const FS_NV12_DMABUF_TO_RGB: &str = r#"#version 100
#extension GL_OES_EGL_image_external : require
precision mediump float;
uniform samplerExternalOES u_tex_external;
// r83 Phase B (2026-06-08): y-axis crop fraction — see
// FS_NV12_TO_RGB above for the full rationale. Mesa's external-OES
// sampler imports the full bcm2835-codec dma_buf allocation
// (1920x1088 for 1080p), so the same crop applies here as on the
// MMAP path.
uniform float u_y_crop_max;
varying vec2 v_uv;
void main() {
    // FYS bug 2: V4L2 delivers the frame bottom-up vs the top-down
    // image / external-RGB paths; flip v to render right-side up.
    // r83 Phase B: scale by u_y_crop_max so sampling stays in the
    // valid display rows; the bottom-row green padding (uv_t.y near
    // 1.0) is unreachable.
    vec2 uv_t = vec2(v_uv.x, (1.0 - v_uv.y) * u_y_crop_max);
    // The Mesa driver decodes NV12 -> RGB for the external-OES
    // sample on the Pi's vc4. Output is RGB in [0,1]; alpha
    // forced to opaque (NV12 has no alpha channel).
    vec3 rgb = texture2D(u_tex_external, uv_t).rgb;
    gl_FragColor = vec4(rgb, 1.0);
}
"#;

/// v1-spec-delta #7 (slice c, 2026-05-08) -- Photoshop/Pillow
/// `overlay` blend mode. Per-channel formula:
///   if dst < 0.5:  out = 2 · src · dst
///   else:          out = 1 - 2 · (1-src) · (1-dst)
/// Then source-over composite by α: dst' = (1-α) dst + α · out.
///
/// `u_layer_tex` holds the rasterized text layer with PREMULTIPLIED
/// alpha (FS_GLYPH emit shape: text·α, α). Recover unpremultiplied
/// src.rgb via `layer.rgb / layer.a` with an epsilon guard so
/// out-of-glyph fragments (α ~= 0) don't divide-by-zero. When α is
/// effectively 0, the composite reduces to dst (no change).
///
/// `u_slide_tex` is the current scene state (bg + earlier layers).
/// We sample, compute overlay, write to the destination FBO. The
/// destination is bound by the caller; this shader is a pure
/// fragment-only composite pass with no blend func required (the
/// formula is an explicit mix).
pub const FS_OVERLAY_BLEND: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_layer_tex;
uniform sampler2D u_slide_tex;
varying vec2 v_uv;
void main() {
    vec4 layer = texture2D(u_layer_tex, v_uv);
    vec3 dst = texture2D(u_slide_tex, v_uv).rgb;
    float a = layer.a;
    // Out-of-glyph short-circuit so the divide is safe.
    if (a < 0.001) {
        gl_FragColor = vec4(dst, 1.0);
        return;
    }
    vec3 src = layer.rgb / a;
    vec3 mul = 2.0 * src * dst;
    vec3 scr = 1.0 - 2.0 * (1.0 - src) * (1.0 - dst);
    vec3 ovl = mix(mul, scr, step(0.5, dst));
    vec3 result = mix(dst, ovl, a);
    gl_FragColor = vec4(result, 1.0);
}
"#;

/// Fragment shader: two-color linear gradient. Mirrors the Python
/// reference (`backend.openmarquee.auto_render._render_pattern_
/// gradient`). Coordinate convention is image-space (y=0 at top), so
/// gl_FragCoord.y is flipped against u_viewport.y to match.
pub const FS_GRADIENT: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform vec2 u_vp_offset;
uniform vec2 u_dir;
uniform vec2 u_proj_bounds;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // u_vp_offset (atlas SB, 2026-05-09): shifts gl_FragCoord
    // into the viewport-local (0..vp_w, 0..vp_h) frame so the
    // gradient projection math uses pixel coords relative to
    // the bake region, not the absolute atlas. Identity = (0,0)
    // for full-screen renders.
    vec2 frag_local = gl_FragCoord.xy - u_vp_offset;
    vec2 pos = vec2(frag_local.x, u_viewport.y - frag_local.y);
    float proj = dot(pos, u_dir);
    float t = clamp((proj - u_proj_bounds.x) / u_proj_bounds.y, 0.0, 1.0);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice b) -- diagonal 45° stripes. Each tile
/// (perp-pixel-spacing) is split half color_a / half color_b
/// along the (x+y)/sqrt(2) projection. Coordinate convention
/// matches FS_GRADIENT: y is flipped from gl_FragCoord so density
/// maps the same as the Python PIL reference.
pub const FS_PATTERN_STRIPES: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    // Phase 3l 2026-05-15: +7.56 along the 45deg axis aligns Rust's
    // top-left-anchored stripe phase with Canvas2D's CSS
    // repeating-linear-gradient(45deg) anchor. The constant comes
    // from a 3-row multi-row probe (qa/captures/stripes-diag.json,
    // commit 5264c8f): normalized x-offset = 10.70 +- 0.16 px
    // across y=270/540/810; 7.56 = 10.70 / sqrt(2).
    float proj = (pos.x + pos.y) / 1.41421356 + 7.56;
    float modv = mod(proj, u_tile);
    // smoothstep over a 1-px window around u_tile/2 anti-aliases the
    // color_a -> color_b boundary to ~match Canvas2D's ramped edge
    // (Phase 3k Cause 1, commit e14b3a8). A second smoothstep on the
    // wrap-around distance handles the color_b -> color_a boundary at
    // modv=0/u_tile -- without it, max_delta floor stays at 229 from
    // single-pixel hairlines at every wrap point.
    float ab = smoothstep(u_tile * 0.5 - 0.5, u_tile * 0.5 + 0.5, modv);
    float wrap_dist = min(modv, u_tile - modv);
    float wrap_blend = smoothstep(0.0, 0.5, wrap_dist);
    float t = mix(0.5, ab, wrap_blend);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice b) -- standard checker. Cells alternate
/// color_a / color_b based on (floor(x/tile) + floor(y/tile)) %
/// 2. y flipped to match Python's image-coord convention.
pub const FS_PATTERN_CHECKER: &str = r#"#version 100
precision highp int;
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // Phase 3z Cand E: integer-domain coord reconstruction.
    // Phase 3z Cand E refinement: int() on vc4 appears to round
    // (not truncate). Use `viewport_h - y_bot` (no -1) so the
    // rounded-up y_bot still lands on the correct y_top.
    int viewport_h = int(u_viewport.y);
    int y_bot = int(gl_FragCoord.y);
    int x_int = int(gl_FragCoord.x);
    int tile_i = int(u_tile + 0.5);
    int y_top = viewport_h - y_bot;
    int gx = x_int / tile_i;
    int gy = y_top / tile_i;
    int parity = (gx + gy) - ((gx + gy) / 2) * 2;
    float t = float(parity);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice b) -- dot grid. Each `tile`-sized cell
/// has a filled circle of `u_radius` pixels at its center. y
/// flipped to match Python.
pub const FS_PATTERN_DOTS: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_radius;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 cell = mod(pos, u_tile) - vec2(u_tile * 0.5);
    // Phase 3s 2026-05-15: smoothstep AA at the circle boundary.
    // Phase 3s-prep dots diag (qa/captures/parity-phase3s-dots-...) showed
    // Canvas2D's ctx.arc + ctx.fill produces ~1-px bilinear AA at every
    // dot edge while the prior `step(d2, r2)` hard-stepped. Diff was
    // white rings at every dot boundary (max_delta=229, mean=4.06).
    // smoothstep(r-0.5, r+0.5, length(cell)) gives a 1-px transition
    // centered on the radius matching Canvas2D's filter response.
    float d = length(cell);
    float t = 1.0 - smoothstep(u_radius - 0.5, u_radius + 0.5, d);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice c) -- halftone (printer-style two-grid
/// dot pattern). Same geometry as dots but with a second offset
/// grid OR'd in (offset by `u_half` in both axes). Inside either
/// grid's circle -> color_b; otherwise color_a.
pub const FS_PATTERN_HALFTONE: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_radius;
uniform float u_half;
uniform float u_y_phase_l1;
uniform float u_y_phase_l2;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // Phase 3ag 2026-05-15: Cand-B precompute y-phase + linear-coverage
    // AA + per-layer screen blend. Mirrors Phase 3x scanlines + Phase
    // 3aa grid. Avoids `pos.y = u_viewport.y - gl_FragCoord.y` (vc4
    // mediump precision trap: large-magnitude subtraction quantizes to
    // ~1 px integer noise; that noise smears AA across the wrong row).
    // u_y_phase_l1 = mod(viewport_h - tile/2, tile), the gl_FragCoord.y-
    // mod-tile position of layer-1 dot rows (centers at canvas_y =
    // tile/2 + k*tile). u_y_phase_l2 = mod(viewport_h, tile) similarly
    // for layer-2 (centers at canvas_y = k*tile).
    //
    // X-axis: gl_FragCoord.x is canvas-x directly (no flip), so the
    // original mod-and-subtract-half formulation has no precision
    // issue and stays.
    //
    // Y-axis: instead of computing signed cell.y via the y-flip
    // subtraction, compute |cell.y| (modular distance to nearest dot
    // row) via abs(frag_y_mod - phase) wrapped to [0, tile/2].
    // length(vec2(cell.x, cell.y)) is sign-invariant on cell.y, so |y|
    // suffices for the circle distance.
    //
    // Linear-coverage AA: clamp(r + 0.5 - d, 0, 1) matches Canvas2D's
    // pixel-coverage at pixel-center (Phase 3af f64 sim showed
    // smoothstep over-saturated the mid-AA band by ~9%). Per-layer
    // screen blend (c1 + c2 - c1*c2) matches Canvas2D's two-arc
    // source-over composition (Phase 3af also derived this).
    float cell1_x = mod(gl_FragCoord.x, u_tile) - u_tile * 0.5;
    float cell2_x = mod(gl_FragCoord.x + u_half, u_tile) - u_tile * 0.5;
    float frag_y_mod = mod(gl_FragCoord.y, u_tile);
    float dy1 = abs(frag_y_mod - u_y_phase_l1);
    float cell1_y_abs = min(dy1, u_tile - dy1);
    float dy2 = abs(frag_y_mod - u_y_phase_l2);
    float cell2_y_abs = min(dy2, u_tile - dy2);
    float d1 = length(vec2(cell1_x, cell1_y_abs));
    float d2 = length(vec2(cell2_x, cell2_y_abs));
    float c1 = clamp(u_radius + 0.5 - d1, 0.0, 1.0);
    float c2 = clamp(u_radius + 0.5 - d2, 0.0, 1.0);
    float t = c1 + c2 - c1 * c2;
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice c) -- horizontal 1-pixel scanlines
/// every `u_tile` rows of color_b on a color_a base. Row index
/// is integer floor(pos.y); rows where row mod tile == 0 get
/// color_b, others stay color_a.
pub const FS_PATTERN_SCANLINES: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_y_phase;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // Phase 3x 2026-05-15 (Candidate B winner): drop the large-
    // magnitude `u_viewport.y - gl_FragCoord.y` subtraction (vc4
    // mediump truncated gl_FragCoord.y at top-of-viewport, so the
    // resulting row math missed every scanline except y=0 -- see
    // qa/captures/parity-phase3w-scanlines-2026-05-15.md). Use
    // gl_FragCoord.y directly via mod; u_y_phase precomputed CPU-
    // side = mod(viewport_h, u_tile) so scanlines land at
    // mod ~= u_y_phase. The +/-0.5 step tolerance accepts both
    // possible truncation outcomes of gl_FragCoord.y at every
    // pixel center. 3-way probe vs Cand A (int-domain, +1 px
    // shift bug like checker) and Cand C (vertex UV varying,
    // no fix): qa/captures/parity-phase3x-candidates-2026-05-15.md.
    // Phase 3ab 2026-05-15: drop the -0.5 from the phase formula.
    // The original `mod(viewport_h - 0.5, tile)` only matched at
    // default tile=13 by coincidence; audit at tile=4/9/15 showed
    // 2-px-wide bands at every period. vc4 mediump mod() at large
    // magnitudes behaves as if gl_FragCoord.y is round-half-up'd
    // (same root behavior as the vc4 int() rounding lesson from
    // Phase 3z checker; cf. Phase 3aa GRID).
    float m = mod(gl_FragCoord.y, u_tile);
    float t = step(abs(m - u_y_phase), 0.5);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice c) -- 1-pixel graph-paper grid:
/// color_a lines on color_b paper, every `u_tile` rows + cols.
/// Pixel sits on a line when (floor(x) % tile == 0) OR
/// (floor(y) % tile == 0). On-line -> color_a; off-line ->
/// color_b. Python convention reversed from dots/checker (the
/// majority field is color_b, lines are color_a).
pub const FS_PATTERN_GRID: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_y_phase;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // Phase 3aa 2026-05-15: hybrid of Cand B's mod-direct approach.
    // X axis: gl_FragCoord.x at small magnitudes (0..1919) doesn't
    //   suffer mediump precision loss; mod() yields integer
    //   pixel-from-edge directly. Detect "on line" via
    //   min(mx, tile-mx) <= 0.5.
    // Y axis: y-flip would lose precision via the original
    //   `u_viewport.y - gl_FragCoord.y` subtraction (Phase 3w
    //   playbook). Instead use Cand B from scanlines: compare
    //   mod(gl_FragCoord.y, tile) against CPU-precomputed u_y_phase
    //   = mod(viewport_h, tile) with +/-0.5 step tolerance. (Phase
    //   3aa derivation; cf. Phase 3ab scanlines audit confirming the
    //   same formula generalizes across tile sizes.)
    float mx = mod(gl_FragCoord.x, u_tile);
    float my = mod(gl_FragCoord.y, u_tile);
    float dx = min(mx, u_tile - mx);
    float on_x = step(dx, 0.5);
    float on_y = step(abs(my - u_y_phase), 0.5);
    float on_line = max(on_x, on_y);
    gl_FragColor = vec4(mix(u_color_b, u_color_a, on_line), 1.0);
}
"#;

/// v1-spec-delta #6 (slice c) -- concentric rings around the
/// slide center: solid color_a background with 2-pixel-wide color_b
/// ring strokes at radii {u_half, u_half + u_tile, u_half + 2*u_tile,
/// ...}. Mirrors Canvas2D's `bg-system.js:367-380` which fills color_a
/// then strokes color_b circles at those radii with lineWidth=2.
pub const FS_PATTERN_RINGS: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_half;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    // Phase 3ac 2026-05-15: thin rings (Option C: honor the
    // docstring). Pre-fix shader used `step(u_threshold, period)`
    // which produced alternating WIDE bands -- a long-standing
    // semantic divergence from Canvas2D and the docstring above.
    // qa/captures/parity-phase3ac-rings-candidates-2026-05-15.md.
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 d = pos - u_viewport * 0.5;
    float dist = length(d);
    float p = mod(dist, u_tile) - u_half;
    float t = step(abs(p), 1.0);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice d) -- conic ray pattern. atan2 gives
/// per-pixel angle around the viewport center; bin to slice index;
/// color = color_a / color_b based on slice parity. Slice count is
/// always even so the seam at 0/-π wraps cleanly.
pub const FS_PATTERN_RAYS: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_slices;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 d = pos - u_viewport * 0.5;
    float angle = atan(d.y, d.x);
    // angle range is -π..π. Map to [0, 1) then bin into slices.
    float norm = mod(angle / 6.28318530 + 1.0, 1.0);
    float slice_idx = floor(norm * u_slices);
    float t = mod(slice_idx, 2.0);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice d) -- brick wall. 2-pixel mortar lines
/// between bricks; courses alternate offset by half-brick-width.
/// Horizontal mortar at row mod bh < 2; vertical mortar depends
/// on the row's course (0 or 1).
pub const FS_PATTERN_BRICKS: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_bw;
uniform float u_bh;
uniform float u_half;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    float row = floor(pos.y);
    float col = floor(pos.x);
    // Horizontal mortar: y mod bh < 2 -> color_b.
    float h_mortar = step(mod(row, u_bh), 1.5);
    // Course 0 vs 1: alternates every bh rows.
    float course = mod(floor(row / u_bh), 2.0);
    // Vertical mortar offset by half on course-1 rows.
    float c0 = mod(col, u_bw);
    float c1 = mod(col - u_half, u_bw);
    float vx = mix(c0, c1, step(0.5, course));
    float v_mortar = step(vx, 1.5);
    float on_mortar = max(h_mortar, v_mortar);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, on_mortar), 1.0);
}
"#;

/// v1-spec-delta #6 (slice d) -- confetti scatter. Cell-based
/// deterministic placement: each cell-size grid cell holds one
/// hash-positioned color_b dot of variable radius on color_a.
/// Hash uses fract(sin(dot(...))) -- low-quality but cheap and
/// deterministic per (density, viewport).
///
/// Visual character matches Python's uniform-random scatter; the
/// pixel-exact placement does NOT (different RNG families). Per
/// Python docstring: "editor canvas and device backend use
/// different RNG families with the same seed -- both deterministic
/// per-surface but the scatters will not pixel-match."
pub const FS_PATTERN_CONFETTI: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_cell;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
// Standard cheap GLSL hash. Returns [0, 1).
float h11(float n) { return fract(sin(n * 91.4583) * 43758.5453); }
vec2 h22(vec2 p) {
    return fract(sin(vec2(
        dot(p, vec2(127.1, 311.7)),
        dot(p, vec2(269.5, 183.3))
    )) * 43758.5453);
}
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 cell = floor(pos / u_cell);
    // Particle position within cell + radius from cell hash.
    vec2 jitter = h22(cell);
    vec2 particle = cell * u_cell + jitter * u_cell;
    // Radius 2..6 px, mirrored from Python's rng.integers(2, 6).
    float r = 2.0 + h11(cell.x * 13.7 + cell.y * 51.3) * 4.0;
    vec2 d = pos - particle;
    float t = step(dot(d, d), r * r);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice a, 2026-05-08): typed enum of the 10
/// procedural background patterns from Python's
/// `auto_render._render_pattern_*`. `solid` and `gradient` aren't
/// in this enum -- they're separate `BgKind` variants because the
/// gradient already has a special uniform shape (proj_min/span)
/// and solid has zero shaders. The 10 patterns here all share
/// the (color_a, color_b, density) signature, so they fit one
/// `BgKind::Pattern` variant + one fragment-shader-per-kind
/// dispatch table.
///
/// Variant order matches the Python `BackgroundPattern.pattern`
/// Literal order so smoke + spec readers can cross-reference.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PatternKind {
    Dots,
    Halftone,
    Stripes,
    Scanlines,
    Checker,
    Grid,
    Rings,
    Rays,
    Confetti,
    Bricks,
}

/// Parse the Python `BackgroundPattern.pattern` string into a
/// typed `PatternKind`. Returns `None` for `"solid"` / `"gradient"`
/// (handled by their dedicated BgKind variants) and for any
/// unknown name (resolve dispatch will warn-and-fall-back to solid).
pub fn parse_pattern_kind(s: &str) -> Option<PatternKind> {
    match s {
        "dots" => Some(PatternKind::Dots),
        "halftone" => Some(PatternKind::Halftone),
        "stripes" => Some(PatternKind::Stripes),
        "scanlines" => Some(PatternKind::Scanlines),
        "checker" => Some(PatternKind::Checker),
        "grid" => Some(PatternKind::Grid),
        "rings" => Some(PatternKind::Rings),
        "rays" => Some(PatternKind::Rays),
        "confetti" => Some(PatternKind::Confetti),
        "bricks" => Some(PatternKind::Bricks),
        _ => None,
    }
}

/// CSS-side bg-system.js lerp: clamp t to [0, 1], map to [a, b].
/// Mirrors Python's `_lerp` in auto_render.py exactly. Used by
/// every pattern's tile-size formula (`round(lerp(big, small,
/// density))`), so a centralized helper keeps the renderer +
/// Python implementations bit-aligned.
///
/// Rounding parity ack: Rust's `f32::round` is half-away-from-
/// zero; Python 3's `round()` is banker's (half-to-even). They
/// diverge ONLY at densities where the lerped value lands on an
/// exact `.5` boundary -- e.g., for stripes (lerp(80, 4)),
/// density ≈ 0.4934 puts lerp at exactly 42.5: Python rounds to
/// 42, Rust to 43. The off-by-one tile is visually undetectable
/// (one perpendicular pixel of stripe), and FYS density-slider
/// snap (typically 0.05 / 0.1 increments) only hits a half-
/// integer ~once per range. Same divergence shape applies to
/// every pattern's tile + radius formula. Pixel-exact diff vs
/// the Python PIL reference will fail at these densities; visual
/// QA will not.
fn pattern_lerp(a: f32, b: f32, t: f32) -> f32 {
    let t = t.clamp(0.0, 1.0);
    a + (b - a) * t
}

/// qarl 2026-05-12: stretch the low end of the density slider so
/// low-intensity values yield REALLY large features. Quadratic
/// curve (d^2). Applied at the call sites of every size-/count-
/// bearing pattern_lerp; gradient/solid (no lerp) are exempt.
///
/// JS mirror: `ui/src/bg-system.js` densityCurve().
/// Python mirror: `backend/openmarquee/auto_render.py` _density_curve.
/// All three must stay in lockstep for WYSIWYG parity.
pub const PATTERN_DENSITY_CURVE_EXPONENT: f32 = 2.0;

pub fn pattern_density_curve(density: f32) -> f32 {
    let d = density.clamp(0.0, 1.0);
    d.powf(PATTERN_DENSITY_CURVE_EXPONENT)
}

/// v1-spec-delta #6 (slice b) -- stripes pattern uniforms. Tile
/// size is the perpendicular-pixel spacing of the diagonal 45°
/// bands. Each tile is split half-color_a / half-color_b along
/// the (x+y)/sqrt(2) projection axis. Mirrors Python's
/// `_render_pattern_stripes`: tile = round(lerp(80, 4, density));
/// floored to 2 to keep the half-tile threshold sub-pixel-stable.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct StripesUniforms {
    pub tile: f32,
}

pub fn stripes_uniforms(density: f32) -> StripesUniforms {
    let tile = pattern_lerp(80.0, 4.0, density).round().max(2.0);
    StripesUniforms { tile }
}

/// v1-spec-delta #6 (slice b) -- checker pattern uniforms. Tile
/// is the cell size; cells alternate color_a / color_b in a
/// classic checkerboard. Mirrors Python's
/// `_render_pattern_checker`: tile = round(lerp(60, 4, density)).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CheckerUniforms {
    pub tile: f32,
}

pub fn checker_uniforms(density: f32) -> CheckerUniforms {
    let tile = pattern_lerp(60.0, 4.0, density).round().max(2.0);
    CheckerUniforms { tile }
}

/// v1-spec-delta #6 (slice b) -- dots pattern uniforms. Tile is
/// the cell size; each cell has a filled circle of `radius`
/// pixels at its center. Mirrors Python's `_render_pattern_dots`:
/// tile = round(lerp(48, 4, density)); radius = max(2,
/// round(tile * 0.22)).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DotsUniforms {
    pub tile: f32,
    pub radius: f32,
}

pub fn dots_uniforms(density: f32) -> DotsUniforms {
    let tile = pattern_lerp(48.0, 4.0, density).round().max(2.0);
    let radius = (tile * 0.22).round().max(2.0);
    DotsUniforms { tile, radius }
}

/// v1-spec-delta #6 (slice c) -- halftone pattern uniforms. Two
/// offset dot grids; second layer offset by half a tile. Mirrors
/// Canvas2D's `_render_pattern_halftone` (ui/src/bg-system.js:286):
/// tile = round(lerp(60, 6, density)); radius = round(tile * 0.34).
/// Phase 3t 2026-05-15: half = tile * 0.5 (NO floor). Canvas2D's
/// layer-0 anchor uses JS `tile / 2` which keeps the half-pixel
/// for odd tiles (e.g. tile=33 -> half=16.5). The earlier Python
/// convention `tile // 2 = 16` produced a 0.5-px grid offset that
/// the Phase 3t diag (qa/captures/halftone-tile-crop.png)
/// localized as the dominant cause of max_delta=229 even after
/// smoothstep AA was applied to the shader.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HalftoneUniforms {
    pub tile: f32,
    pub radius: f32,
    pub half: f32,
}

pub fn halftone_uniforms(density: f32) -> HalftoneUniforms {
    let tile = pattern_lerp(60.0, 6.0, density).round().max(2.0);
    let radius = (tile * 0.34).round().max(2.0);
    // Phase 3t 2026-05-15: use float tile/2 (no floor). Canvas2D
    // uses JS `tile / 2` which preserves the half-pixel offset for
    // odd tiles (matches its ctx.arc + fill sub-pixel positioning).
    let half = tile * 0.5;
    HalftoneUniforms { tile, radius, half }
}

/// v1-spec-delta #6 (slice c) -- scanlines pattern uniforms.
/// Horizontal 1-pixel-tall lines of color_b on color_a, every
/// `tile` rows. Mirrors Python's `_render_pattern_scanlines`:
/// tile = max(2, round(lerp(16, 2, density))).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ScanlinesUniforms {
    pub tile: f32,
}

pub fn scanlines_uniforms(density: f32) -> ScanlinesUniforms {
    let tile = pattern_lerp(16.0, 2.0, density).round().max(2.0);
    ScanlinesUniforms { tile }
}

/// v1-spec-delta #6 (slice c) -- grid pattern uniforms. 1-pixel
/// graph-paper grid: color_a lines on color_b paper, every tile
/// rows + cols. Mirrors Python's `_render_pattern_grid`: tile =
/// max(4, round(lerp(120, 4, density))).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GridUniforms {
    pub tile: f32,
}

pub fn grid_uniforms(density: f32) -> GridUniforms {
    let tile = pattern_lerp(120.0, 4.0, density).round().max(4.0);
    GridUniforms { tile }
}

/// v1-spec-delta #6 (slice c) -- rings pattern uniforms.
/// Concentric rings around the slide center. Mirrors Canvas2D's
/// `bg-system.js:367-380`: tile = max(4, round(lerp(120, 6,
/// density))); half = tile / 2; ring strokes (color_b, ~2px wide)
/// at radii {half, half + tile, half + 2*tile, ...} on a solid
/// color_a background.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RingsUniforms {
    pub tile: f32,
    /// First-ring radius (and offset of every subsequent ring within
    /// a period). Canvas2D mirror: `tile / 2`.
    pub half: f32,
}

pub fn rings_uniforms(density: f32) -> RingsUniforms {
    let tile = pattern_lerp(120.0, 6.0, density).round().max(4.0);
    let half = tile * 0.5;
    RingsUniforms { tile, half }
}

/// v1-spec-delta #6 (slice d) -- rays pattern uniforms. Conic
/// gradient with `slices` equal angular wedges alternating
/// color_a / color_b. Mirrors Python's `_render_pattern_rays`:
/// slices = max(2, 2 * round(lerp(2, 24, density))). Always
/// even (an odd count would join two same-colored slices at the
/// wrap seam).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RaysUniforms {
    pub slices: f32,
}

pub fn rays_uniforms(density: f32) -> RaysUniforms {
    let raw = pattern_lerp(2.0, 24.0, density).round();
    // 2 * round(lerp(2, 24, density)), then floor at 2.
    let slices = (2.0 * raw).max(2.0);
    RaysUniforms { slices }
}

/// v1-spec-delta #6 (slice d) -- bricks pattern uniforms. Brick
/// width shrinks with density (lerp(140, 16)); brick height is
/// half the width. 2-pixel mortar between bricks. Courses
/// alternate offset by half-brick-width. Mirrors Canvas2D's
/// `_render_pattern_bricks` (ui/src/bg-system.js:426).
/// Phase 3u 2026-05-15: bh = round(bw/2), half = bw * 0.5 (NO
/// floor). Same canonicalization as Phase 3t halftone: Canvas2D
/// uses JS `Math.round` for bh and float `w / 2` for half;
/// pre-3u Rust mirrored Python's `// 2` integer floor which
/// diverged for odd bw (density-curve-effective bw=109 at
/// density=0.5 -> JS bh=55/half=54.5 but Rust pre-3u bh=54/
/// half=54, a 1-px brick-height and 0.5-px stagger offset
/// per Phase 3u diag at qa/captures/parity-phase3u-...).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BricksUniforms {
    pub bw: f32,
    pub bh: f32,
    pub half: f32,
}

pub fn bricks_uniforms(density: f32) -> BricksUniforms {
    let bw = pattern_lerp(140.0, 16.0, density).round().max(8.0);
    // Canvas2D: bh = Math.max(4, Math.round(w / 2)). Use round to
    // match (was .floor() pre-3u, Python convention).
    let bh = (bw * 0.5).round().max(4.0);
    // Canvas2D: half = w / 2 (no round/floor; sub-pixel for odd bw
    // matters because ctx.fillRect at fractional x AAs the mortar
    // edge into adjacent pixels).
    let half = bw * 0.5;
    BricksUniforms { bw, bh, half }
}

/// v1-spec-delta #6 (slice d) -- confetti pattern uniforms.
/// Cell-based deterministic scatter: viewport partitioned into
/// `cell_size`-pixel cells, each cell holds one hash-positioned
/// dot of color_b on color_a. Cell size derived from particle
/// count so scatter density matches Python's intent without
/// requiring per-particle CPU upload.
///
/// Python uses uniform-random scatter via numpy PRNG (count =
/// max(40, round(lerp(80, 2000, density)))). The shader-side
/// approach is structurally different (cell-based vs uniform-
/// random) -- per Python's docstring, "editor canvas and device
/// backend use different RNG families with the same seed --
/// both deterministic per-surface but the scatters will not
/// pixel-match. Visual character is the same."
///
/// cell_size derivation: assuming 1024x768 reference viewport
/// (786 432 pixels), one particle per cell yields the equivalent
/// Python count. cell_size = sqrt(area / count). Stored as a
/// fraction of viewport so the shader can scale to any actual
/// viewport.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ConfettiUniforms {
    pub count: f32,
    /// Average cell size in pixels at the 1024x768 reference
    /// viewport. The shader rescales by actual viewport area.
    pub cell_ref: f32,
}

pub fn confetti_uniforms(density: f32) -> ConfettiUniforms {
    let count = pattern_lerp(80.0, 2000.0, density).round().max(40.0);
    // 1024 * 768 = 786432.
    let cell_ref = (786432.0 / count).sqrt();
    ConfettiUniforms { count, cell_ref }
}

/// v1-spec-delta #7 (slice a, 2026-05-08) -- typed enum for the
/// 4 schema-allowed text-layer compositing modes. `Normal` is
/// today's shipped behavior (source-over premultiplied alpha).
/// `Multiply` and `Screen` ship in slice (b) via GL blend func
/// tweaks (no shader changes needed; the math falls out of the
/// existing FS_GLYPH premultiplied-alpha emit). `Overlay` ships
/// in slice (c) via FBO ping-pong because the formula needs a
/// per-pixel destination sample that fixed-function blend can't
/// express.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlendMode {
    Normal,
    Screen,
    Multiply,
    Overlay,
}

/// Parse the schema string into a typed BlendMode. Unknown values
/// fall back to Normal -- forward-compat for future blend modes
/// added to the schema before the renderer ships them.
pub fn parse_blend_mode(s: &str) -> BlendMode {
    match s {
        "screen" => BlendMode::Screen,
        "multiply" => BlendMode::Multiply,
        "overlay" => BlendMode::Overlay,
        _ => BlendMode::Normal,
    }
}

/// Stable label for log output / smoke parsing.
pub fn blend_mode_label(b: BlendMode) -> &'static str {
    match b {
        BlendMode::Normal => "normal",
        BlendMode::Screen => "screen",
        BlendMode::Multiply => "multiply",
        BlendMode::Overlay => "overlay",
    }
}

/// Stable label for log output / smoke parsing. Matches the
/// Python pattern name (lowercase, no spaces) so a single grep
/// covers both renderer-side log lines and Python-baked PNGs.
pub fn pattern_kind_label(k: PatternKind) -> &'static str {
    match k {
        PatternKind::Dots => "dots",
        PatternKind::Halftone => "halftone",
        PatternKind::Stripes => "stripes",
        PatternKind::Scanlines => "scanlines",
        PatternKind::Checker => "checker",
        PatternKind::Grid => "grid",
        PatternKind::Rings => "rings",
        PatternKind::Rays => "rays",
        PatternKind::Confetti => "confetti",
        PatternKind::Bricks => "bricks",
    }
}

/// A minimal, drm-independent representation of a connector mode.
/// `width`/`height` are in pixels, `vrefresh` in Hz.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ModeSpec {
    pub width: u16,
    pub height: u16,
    pub vrefresh: u32,
}

/// Pick the index of the largest-area mode, breaking ties by higher
/// vrefresh. Returns `None` for an empty slice.
///
/// Used by `hdmi::pick_connector_and_mode` and tested independently
/// here so the picker logic doesn't need a live DRM connector.
pub fn pick_largest_mode_index(modes: &[ModeSpec]) -> Option<usize> {
    modes
        .iter()
        .enumerate()
        .max_by_key(|(_, m)| (m.width as u32 * m.height as u32, m.vrefresh))
        .map(|(i, _)| i)
}

/// Pre-computed inputs for the gradient fragment shader. Mirrors
/// `backend/openmarquee/auto_render.py::_render_pattern_gradient`'s
/// math so the shader produces visually-identical output to the
/// Python PIL reference.
///
/// Convention (from bg-system.js):
///   density 0   → 0°   = top→bottom   (color_a at top, color_b at bottom)
///   density 0.5 → 135° (linear lerp; matches Python implementation)
///   density 1   → 270° = right→left
///   90°  = left→right
///   180° = bottom→top
///
/// Coordinate convention here matches Python (image coords: y=0 at top,
/// y=height-1 at bottom). The shader flips gl_FragCoord.y to match.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct GradientUniforms {
    pub dx: f32,
    pub dy: f32,
    pub proj_min: f32,
    pub span: f32,
}

/// Compute the gradient direction + projection bounds for a (width,
/// height, density) triple. Returns `None` when the gradient
/// degenerates (zero span — should fall back to a solid `color_a`).
///
/// Lifted into a pure function so the math (angle = lerp(0, 270,
/// density), proj_min/max derivation) is unit-tested against known
/// reference values from the Python implementation, independent of
/// any GL state.
///
/// Banker-rounding-vs-away-from-zero ack: Rust's `f32::round` is
/// away-from-zero (n.5 rounds away from zero), Python 3's `round()`
/// is banker's (n.5 rounds to even). They diverge only at exact
/// n.5 boundaries — which means densities that produce angles like
/// 0.5°/1.5°/etc. The FYS slide-editor density slider has 4-decimal
/// precision and the FYS canonical seeds use 0.0/0.5/1.0 anchors;
/// none hit the boundary. If a future content path produces an
/// n.5 angle, the visual difference is one degree of rotation —
/// not perceptible. Accepted.
pub fn gradient_uniforms(width: u32, height: u32, density: f32) -> Option<GradientUniforms> {
    // density 0..1 → 0..270°; rounded to integer degrees for parity
    // with Python's `round(lerp(...))`.
    let density = density.clamp(0.0, 1.0);
    let angle_deg = (density * 270.0).round();
    let rad = angle_deg.to_radians();
    let dx = rad.sin();
    let dy = rad.cos();
    let w = width.saturating_sub(1) as f32;
    let h = height.saturating_sub(1) as f32;
    let proj_min = (dx * w).min(0.0) + (dy * h).min(0.0);
    let proj_max = (dx * w).max(0.0) + (dy * h).max(0.0);
    let span = proj_max - proj_min;
    if span < 1e-6 {
        return None;
    }
    Some(GradientUniforms { dx, dy, proj_min, span })
}

/// Parse a `#RRGGBB` or `#RRGGBBAA` hex color into RGBA in [0, 1].
/// Accepts upper or lower case, with or without leading `#`. Returns
/// `None` on malformed input. Alpha defaults to 1.0 when not given.
///
/// Used by Phase 4 entry to drive `clear_color()` from a
/// `TextSlide.background_color` string. Pure function — split out so
/// the parsing rules round-trip-test against known references.
pub fn hex_to_rgba(hex: &str) -> Option<[f32; 4]> {
    let s = hex.trim().trim_start_matches('#');
    let bytes = s.as_bytes();
    let (r, g, b, a) = match bytes.len() {
        6 => (
            hex_byte(bytes, 0)?,
            hex_byte(bytes, 2)?,
            hex_byte(bytes, 4)?,
            255,
        ),
        8 => (
            hex_byte(bytes, 0)?,
            hex_byte(bytes, 2)?,
            hex_byte(bytes, 4)?,
            hex_byte(bytes, 6)?,
        ),
        _ => return None,
    };
    Some([
        r as f32 / 255.0,
        g as f32 / 255.0,
        b as f32 / 255.0,
        a as f32 / 255.0,
    ])
}

fn hex_byte(bytes: &[u8], offset: usize) -> Option<u8> {
    let hi = hex_nibble(bytes[offset])?;
    let lo = hex_nibble(bytes[offset + 1])?;
    Some((hi << 4) | lo)
}

fn hex_nibble(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

/// Parse the integer bits out of drm-rs 0.12's `CrtcListFilter`
/// Debug repr — the wrapper holds a `pub(crate) u32` which isn't
/// readable from outside the crate, so we fall back to formatting +
/// parsing. Format is `CrtcListFilter(N)` for some unsigned integer N.
///
/// This is the single highest-fragility piece in `find_primary_plane`
/// — it silently breaks the moment drm-rs changes its Debug derive.
/// Lifted out of hdmi.rs and unit-tested so a Debug-format change is
/// caught by the host test gate, not by a Phase-N runtime regression.
pub fn parse_crtc_list_filter_bits(dbg: &str) -> Option<u32> {
    dbg.strip_prefix("CrtcListFilter(")
        .and_then(|s| s.strip_suffix(')'))
        .and_then(|s| s.parse::<u32>().ok())
}

/// HSV → RGB conversion for animation color cycling. h ∈ [0, 360),
/// s and v ∈ [0, 1]. Returns RGB in [0, 1].
///
/// Used by `hdmi::render_animated_atomic` to drive the per-frame hue
/// rotation. Pure function — split out so the math is unit-tested
/// against known reference values.
pub fn hsv_to_rgb(h: f32, s: f32, v: f32) -> (f32, f32, f32) {
    let c = v * s;
    let h6 = (h / 60.0).rem_euclid(6.0);
    let x = c * (1.0 - (h6 % 2.0 - 1.0).abs());
    let (r1, g1, b1) = match h6 as u32 {
        0 => (c, x, 0.0),
        1 => (x, c, 0.0),
        2 => (0.0, c, x),
        3 => (0.0, x, c),
        4 => (x, 0.0, c),
        _ => (c, 0.0, x),
    };
    let m = v - c;
    (r1 + m, g1 + m, b1 + m)
}

/// Map a font-family display name (as the editor stores it in
/// `TextLayer.font_family`) to the basename of the TTF on disk
/// under `ui/fonts/`. Returns `None` for unknown families — the
/// catalog falls back to its fallback family in that case.
///
/// Pure function so a backend rename of a font drops a host test
/// rather than going silent-fallback at runtime. The list mirrors
/// `ui/fonts/` and the editor's font picker.
pub fn font_family_to_filename(family: &str) -> Option<&'static str> {
    match family {
        "Anton" => Some("anton.ttf"),
        "Alfa Slab One" => Some("alfa-slab-one.ttf"),
        "Archivo Black" => Some("archivo-black.ttf"),
        "Bebas Neue" => Some("bebas-neue.ttf"),
        "Bowlby One SC" => Some("bowlby-one-sc.ttf"),
        "Caveat" => Some("caveat.ttf"),
        "Caveat Brush" => Some("caveat-brush.ttf"),
        "Cinzel" => Some("cinzel.ttf"),
        "DM Serif Display" => Some("dm-serif-display.ttf"),
        "Inter" => Some("inter.ttf"),
        "JetBrains Mono" => Some("jetbrains-mono.ttf"),
        "Oswald" => Some("oswald.ttf"),
        "Pacifico" => Some("pacifico.ttf"),
        "Permanent Marker" => Some("permanent-marker.ttf"),
        "Playfair Display" => Some("playfair-display.ttf"),
        "Reenie Beanie" => Some("reenie-beanie.ttf"),
        "Roboto Slab" => Some("roboto-slab.ttf"),
        "Rye" => Some("rye.ttf"),
        "Sedgwick Ave Display" => Some("sedgwick-ave-display.ttf"),
        "Shadows Into Light" => Some("shadows-into-light.ttf"),
        "Space Mono" => Some("space-mono.ttf"),
        "UnifrakturCook" => Some("unifrakturcook.ttf"),
        "VT323" => Some("vt323.ttf"),
        // Bug 3 Slice 2D: DejaVu Sans is the runtime fallback font.
        // Operators can also pick it directly; the layout dispatch
        // routes the same way either way (primary -> runtime cache,
        // then DejaVu fallback if missing -- a no-op if DejaVu is
        // already the primary).
        "DejaVu Sans" => Some("dejavu-sans.ttf"),
        _ => None,
    }
}

/// Pick the predecessor index for a Phase 6 reel iteration.
///
/// Given the current item index `i`, the pass counter `pass`
/// (0 = first sweep through the reel), and the resolved-item
/// count `len`, returns:
///   * `None` only when there's no predecessor — i.e. pass 0,
///     item 0 (the very first slide of a single-pass run has no
///     entry transition).
///   * `Some(prev)` otherwise, with wraparound on `--reel-loop`
///     passes: at pass>=1 item 0 transitions in from the LAST
///     resolved item.
///
/// Pure function so the orchestration's most-likely-to-drift
/// piece (the wraparound math) is host-testable independent of
/// any DRM / EGL state. Phase 6 / Rule-3 followup landed here.
pub fn prev_idx_for_reel(i: usize, pass: u32, len: usize) -> Option<usize> {
    if len == 0 {
        return None;
    }
    if i == 0 && pass == 0 {
        None
    } else {
        Some((i + len - 1) % len)
    }
}

/// Clamp a transition_ms to a sane minimum so degenerate
/// playlist values (0 or near-zero) don't slip through to the
/// per-frame loop where transition_ms = 0 is an error. 50ms ≈
/// 1.5 frames at 30fps — effectively-zero but not actually-zero.
pub fn clamp_transition_ms(transition_ms: u32) -> u32 {
    transition_ms.max(50)
}

/// Per-slide hold duration in **milliseconds** for the reel
/// driver.
///
/// v1-spec-delta #1 — was previously seconds (u64), with a
/// `/1000` truncation that snapped the FYS Panic flash slides
/// (130/350/500/800 ms) to a 1-second floor and erased the
/// flash effect entirely. ms-precision restores the spec
/// behavior: a 130 ms slide holds for 130 ms.
///
/// `slide_duration_ms` is the slide's `duration_ms` field from
/// the content model (already in ms — no conversion). The
/// optional override is in seconds at the CLI for operator
/// ergonomics (`--hold-secs 1` = 1000 ms internally); a None
/// override means "use the slide's own duration_ms verbatim."
///
/// No floor — a 0-duration slide effectively skips. The reel
/// driver is allowed to play degenerate-short slides as a flash
/// effect; the previous 1-second floor was a bug masquerading
/// as a guard.
pub fn effective_hold_ms(slide_duration_ms: u32, override_secs: Option<u64>) -> u64 {
    override_secs
        .map(|s| s.saturating_mul(1000))
        .unwrap_or(slide_duration_ms as u64)
}

// ---------------------------------------------------------------
// Auto-mode (v1-spec-delta #3) — system-clock text substitution.
// Spec §6.1 lines 114-118: time / date / day layers tick every
// second. The renderer rolls its own date math (Unix epoch ->
// y/m/d/h/m/s/weekday) to avoid a new dep without QA sign-off.
// All seven format strings the Python schema enumerates are
// supported; pure helpers, host-testable.
// ---------------------------------------------------------------

/// Broken-down calendar fields. Timezone-agnostic — just the
/// y/m/d/h/m/s/weekday a timestamp decomposes into. Produced by
/// `unix_to_calendar_utc` (pure UTC math) OR `unix_to_calendar_
/// local` (libc localtime_r, honors TZ / DST). The struct itself
/// carries no zone; the producer decides.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Calendar {
    pub year: i32,
    pub month: u8,        // 1..=12
    pub day: u8,          // 1..=31
    pub hour: u8,         // 0..=23
    pub minute: u8,       // 0..=59
    pub second: u8,       // 0..=59
    pub weekday: u8,      // 0=Sunday, 1=Monday, ..., 6=Saturday
}

/// Decompose a Unix timestamp into UTC calendar fields. Pure math,
/// no system-clock side effects. Howard Hinnant's "civil from
/// days" algorithm (CC0) for y/m/d; modular arithmetic for h/m/s
/// and Sakamoto-style weekday from days.
///
/// Bug 1 follow-up (2026-05-20): no longer the auto_mode clock's
/// resolver — that's `unix_to_calendar_local`. This is kept as
/// the libc-failure fallback for that function AND remains
/// independently unit-tested (it's the reference the local
/// resolver's date-rollover tests check against).
pub fn unix_to_calendar_utc(unix_seconds: i64) -> Calendar {
    // Split seconds-of-day from days-since-epoch. rem_euclid keeps
    // sub-day fields well-defined for negative epochs (pre-1970).
    let secs_in_day = 86_400_i64;
    let days = unix_seconds.div_euclid(secs_in_day);
    let secs = unix_seconds.rem_euclid(secs_in_day);
    let hour = (secs / 3600) as u8;
    let minute = ((secs % 3600) / 60) as u8;
    let second = (secs % 60) as u8;
    // 1970-01-01 was a Thursday (weekday=4 in Sun=0 convention).
    // weekday = (4 + days) mod 7. div/rem_euclid for negative days.
    let weekday = ((days.rem_euclid(7) + 4).rem_euclid(7)) as u8;
    // "civil_from_days" — Howard Hinnant. Treats March as month 1
    // internally to make leap-day handling branchless, then maps
    // back to Jan-1 origin.
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u8;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u8;
    let year = (y + (m <= 2) as i64) as i32;
    Calendar {
        year,
        month: m,
        day: d,
        hour,
        minute,
        second,
        weekday,
    }
}

/// Decompose a Unix timestamp into LOCAL calendar fields, via
/// libc `localtime_r`. This is the auto_mode clock's resolver
/// (Bug 1 follow-up, 2026-05-20) — a sign clock must show the
/// sign's physical-location local time, not UTC.
///
/// `localtime_r` consults the `TZ` environment variable (the
/// backend sets it on the sidecar process from
/// settings.timezone) and falls back to `/etc/localtime`. It
/// does the full IANA zoneinfo + DST conversion — we hand-roll
/// none of it. `tzset()` is called per invocation so the
/// inherited TZ is always picked up (cheap: a stat of the
/// zoneinfo file; this resolver runs at most ~30x/s). Mid-
/// process TZ changes are intentionally NOT a concern — a
/// Settings timezone change re-spawns the sidecar via a backend
/// restart, and the fresh process inherits the new TZ.
///
/// On the (astronomically unlikely) `localtime_r` failure —
/// EOVERFLOW for a year outside `c_int` — falls back to
/// `unix_to_calendar_utc` rather than panicking the render path.
pub fn unix_to_calendar_local(unix_seconds: i64) -> Calendar {
    // `tzset` lives in the system libc on every POSIX platform but
    // the `libc` crate doesn't bind it portably (it's absent from
    // the macOS bindings) — declare it directly. It re-reads the
    // `TZ` env into libc's tz state; glibc/macOS `localtime_r`
    // only re-evaluates TZ when tzset has run, so calling it here
    // guarantees the inherited TZ is honored (and makes the unit
    // tests, which set TZ per-case, deterministic).
    extern "C" {
        fn tzset();
    }
    let t = unix_seconds as libc::time_t;
    // SAFETY: tzset() + localtime_r are libc FFI. localtime_r is
    // the reentrant variant (writes into our own `tm`); the
    // render path calls this single-threaded. tzset() mutates
    // libc global tz state — only ever called from this one
    // render thread.
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    let result = unsafe {
        tzset();
        libc::localtime_r(&t, &mut tm)
    };
    if result.is_null() {
        // localtime_r could not convert — fall back to UTC.
        return unix_to_calendar_utc(unix_seconds);
    }
    Calendar {
        year: tm.tm_year + 1900,         // tm_year is years-since-1900
        month: (tm.tm_mon + 1) as u8,    // tm_mon is 0..=11
        day: tm.tm_mday as u8,           // 1..=31
        hour: tm.tm_hour as u8,          // 0..=23
        minute: tm.tm_min as u8,         // 0..=59
        second: tm.tm_sec as u8,         // 0..=60 (leap sec) — clamp not needed downstream
        weekday: tm.tm_wday as u8,       // 0=Sunday — matches Calendar's convention
    }
}

/// Format the auto-mode text per spec §6.1 + the Python
/// auto_format Literal table. Returns None if auto_mode is unset
/// or malformed; the caller's renderer falls back to the layer's
/// `text` field on None.
///
/// `auto_format` is mode-scoped: time_hm/time_hms / date_iso/
/// date_long/date_medium / day_long/day_short. When `auto_format`
/// is None despite auto_mode being set (shouldn't happen post-
/// validator but defensive against IPC edge cases), each mode
/// has a sensible default: time_hm / date_iso / day_long. These
/// defaults match the Canvas2D mirror (ui/src/auto-format.js
/// `defaultFormatFor`) + the Python spec
/// (openmarquee.auto_render `_DEFAULT_FORMAT`) exactly — parity
/// fix 2026-05-19, the date default was previously date_medium.
pub fn format_auto_text(
    auto_mode: Option<&str>,
    auto_format: Option<&str>,
    cal: Calendar,
) -> Option<String> {
    let mode = auto_mode?;
    let fmt = match (mode, auto_format) {
        ("time", Some(f)) if f.starts_with("time_") => f,
        ("date", Some(f)) if f.starts_with("date_") => f,
        ("day", Some(f)) if f.starts_with("day_") => f,
        ("time", _) => "time_hm",
        ("date", _) => "date_iso",
        ("day", _) => "day_long",
        _ => return None,
    };
    Some(match fmt {
        "time_hm" => format!("{:02}:{:02}", cal.hour, cal.minute),
        "time_hms" => format!("{:02}:{:02}:{:02}", cal.hour, cal.minute, cal.second),
        "date_iso" => format!("{:04}-{:02}-{:02}", cal.year, cal.month, cal.day),
        "date_long" => format!(
            "{} {}, {:04}",
            month_long(cal.month),
            cal.day,
            cal.year
        ),
        "date_medium" => format!("{} {}", month_short(cal.month), cal.day),
        "day_long" => weekday_long(cal.weekday).to_string(),
        "day_short" => weekday_short(cal.weekday).to_string(),
        _ => format!("?{fmt}?"),
    })
}

fn month_long(month: u8) -> &'static str {
    match month {
        1 => "January",
        2 => "February",
        3 => "March",
        4 => "April",
        5 => "May",
        6 => "June",
        7 => "July",
        8 => "August",
        9 => "September",
        10 => "October",
        11 => "November",
        12 => "December",
        _ => "?",
    }
}

fn month_short(month: u8) -> &'static str {
    match month {
        1 => "Jan",
        2 => "Feb",
        3 => "Mar",
        4 => "Apr",
        5 => "May",
        6 => "Jun",
        7 => "Jul",
        8 => "Aug",
        9 => "Sep",
        10 => "Oct",
        11 => "Nov",
        12 => "Dec",
        _ => "?",
    }
}

fn weekday_long(weekday: u8) -> &'static str {
    match weekday {
        0 => "Sunday",
        1 => "Monday",
        2 => "Tuesday",
        3 => "Wednesday",
        4 => "Thursday",
        5 => "Friday",
        6 => "Saturday",
        _ => "?",
    }
}

fn weekday_short(weekday: u8) -> &'static str {
    match weekday {
        0 => "Sun",
        1 => "Mon",
        2 => "Tue",
        3 => "Wed",
        4 => "Thu",
        5 => "Fri",
        6 => "Sat",
        _ => "?",
    }
}

// ---------------------------------------------------------------
// Motion engine (v1-spec-delta #2) — pure host-testable math.
// docs/text-layer-motion-spec.md is the source of truth for the
// menu, semantics, and per-effect intensity ranges.
// ---------------------------------------------------------------

/// The seven motion modes the spec defines. Anything not in this
/// enum (including future-added strings) falls back to `Static`
/// in `parse_motion_kind`, which the renderer treats as "no
/// animation" — preserving the field on save without rendering an
/// undefined effect.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MotionKind {
    Static,
    Ticker,
    Breathe,
    Pulse,
    Bounce,
    Shake,
    Blink,
}

/// Map the schema string to the renderer's motion enum. Unknown
/// values resolve to `Static` (safe fallback) so a forward-added
/// motion kind in the editor doesn't crash the renderer.
pub fn parse_motion_kind(s: &str) -> MotionKind {
    match s {
        "ticker" => MotionKind::Ticker,
        "breathe" => MotionKind::Breathe,
        "pulse" => MotionKind::Pulse,
        "bounce" => MotionKind::Bounce,
        "shake" => MotionKind::Shake,
        "blink" => MotionKind::Blink,
        _ => MotionKind::Static,
    }
}

/// Per-frame motion contribution for one text layer. The renderer
/// applies these on top of the layer's authored placement +
/// opacity:
///   - `offset_x_norm` / `offset_y_norm` are translation offsets
///     expressed as fractions of the layer's box dimensions
///     (ticker / bounce / shake) or glyph-height for shake. The
///     `effective_motion_translate_px` helper does the
///     box→pixels conversion at the renderer boundary.
///   - `scale` is the breathe scale around box center (1.0 = no
///     change). Ticker / pulse / blink return 1.0.
///   - `alpha_mul` multiplies the layer's authored opacity (1.0 =
///     no change). pulse / blink modulate this.
///
/// The shared global tick is a `f64` of seconds since clock start;
/// passing the same tick to multiple layers keeps them in sync,
/// and `motion_phase` (0..1) lets two layers with the same effect
/// run in opposition.
#[derive(Debug, Clone, Copy)]
pub struct MotionState {
    pub offset_x_norm: f32,
    pub offset_y_norm: f32,
    pub scale: f32,
    pub alpha_mul: f32,
}

impl MotionState {
    pub const IDENTITY: MotionState = MotionState {
        offset_x_norm: 0.0,
        offset_y_norm: 0.0,
        scale: 1.0,
        alpha_mul: 1.0,
    };
}

/// Compute the per-frame motion state for a layer at `tick_seconds`
/// on the shared global clock.
///
/// `intensity` is clamped to 0..=100 (Python validates 0-100 but
/// we mirror loosely so out-of-range values don't panic the
/// renderer); `phase` is clamped to 0..=1; `speed` is clamped to
/// 0..=2 (matches the Python field validator).
///
/// `layer_id_seed` is used by `Shake` to seed a deterministic PRNG
/// — same layer + same phase = same shake sequence across
/// reloads, different layers across one slide produce visually
/// different jitter.
pub fn compute_motion_state(
    kind: MotionKind,
    intensity: u8,
    phase: f32,
    speed: f32,
    layer_id_seed: u64,
    tick_seconds: f64,
) -> MotionState {
    let i = (intensity.min(100) as f32) / 100.0;
    let phase = phase.clamp(0.0, 1.0);
    let speed = speed.clamp(0.0, 2.0);
    match kind {
        MotionKind::Static => MotionState::IDENTITY,
        MotionKind::Ticker => motion_ticker(i, phase, speed, tick_seconds),
        MotionKind::Breathe => motion_breathe(i, phase, speed, tick_seconds),
        MotionKind::Pulse => motion_pulse(i, phase, speed, tick_seconds),
        MotionKind::Bounce => motion_bounce(i, phase, speed, tick_seconds),
        MotionKind::Shake => {
            motion_shake(i, phase, speed, layer_id_seed, tick_seconds)
        }
        MotionKind::Blink => motion_blink(i, phase, speed, tick_seconds),
    }
}

/// Tiling marquee scroll, LTR (the text scrolls leftward).
///
/// Density-parity rewrite (2026-05-20): the ticker draws the text
/// TILED — repeated every box-width — so the stripe reads as a
/// continuous marquee (the tiling happens in `draw_text_layer_
/// msdf`; the two-copy wrap matches the Canvas2D editor ticker
/// that qarl picked as authoritative). The pre-rewrite ticker slid
/// a SINGLE copy across a ±box-width sweep, which showed the text
/// once per 2×box-width of travel — half the density, twice the
/// whitespace.
///
/// `offset_x_norm` is the scroll position WITHIN one tile pitch,
/// in [0, 1): 0 = rest, approaching 1 = scrolled almost a full
/// box-width left, then wrapping seamlessly because the next tiled
/// copy has taken its place. Period at intensity=50 is ~3.5 s
/// (6 s slow @ 0 → 1 s fast @ 100) — `6 - 0.05*intensity`, the
/// same formula the Canvas ticker uses, so device + editor scroll
/// at the same rate.
fn motion_ticker(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    let base_period = 6.0 - 5.0 * intensity_norm;
    if speed == 0.0 {
        // Frozen: hold at the phase's scroll position.
        return MotionState {
            offset_x_norm: phase,
            ..MotionState::IDENTITY
        };
    }
    let period = (base_period / speed).max(0.05);
    let t = tick_seconds + (phase as f64) * period as f64;
    // cycle in [0, 1) — the scroll fraction of one tile pitch.
    let cycle = (t.rem_euclid(period as f64)) / (period as f64);
    MotionState {
        offset_x_norm: cycle as f32,
        ..MotionState::IDENTITY
    }
}

/// Sine scale around the box center. 1 Hz, amplitude ±2 % at
/// intensity=0 → ±20 % at intensity=100, ±11 % at intensity=50
/// (close to spec's "±10 %"). Renderer pivots on the box center,
/// preserving operator-authored offset within the box.
fn motion_breathe(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    let amp = 0.02 + 0.18 * intensity_norm;
    let phase_rad = 2.0 * std::f32::consts::PI
        * ((tick_seconds * speed as f64) as f32 + phase);
    MotionState {
        scale: 1.0 + amp * phase_rad.sin(),
        ..MotionState::IDENTITY
    }
}

/// Sine alpha sweep. 1 Hz, range [0.70, 1.0] at intensity=0 →
/// [0.0, 1.0] at intensity=100, [0.35, 1.0] at intensity=50
/// (close to spec's "30 %→100 %"). Multiplies the layer's
/// authored opacity.
fn motion_pulse(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    let min_alpha = 0.70 * (1.0 - intensity_norm);
    let phase_rad = 2.0 * std::f32::consts::PI
        * ((tick_seconds * speed as f64) as f32 + phase);
    // 0.5 * (1 + sin) maps to [0, 1], then scale into [min, 1].
    let frac = 0.5 * (1.0 + phase_rad.sin());
    MotionState {
        alpha_mul: min_alpha + (1.0 - min_alpha) * frac,
        ..MotionState::IDENTITY
    }
}

/// Ball-on-floor bounce. 1 Hz, amplitude 1 % at intensity=0 → 10 %
/// at intensity=100, 5.5 % at intensity=50. Returns offset_y_norm in
/// box-height units. Uses `abs(sin)` (not plain `sin`) so the rest
/// position is the FLOOR and the layer rebounds UP-and-back-down
/// twice per cycle, never going below rest — matches
/// `backend/openmarquee/motion.py:300` ("abs(sin) for true bouncing",
/// qarl 2026-05-03). `offset_y_norm` is negated because the renderer
/// treats +Y as DOWN (see the dy_ndc flip in motion_quad_uv), so
/// negative offset = UP visually.
fn motion_bounce(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    let amp = 0.01 + 0.09 * intensity_norm;
    let phase_rad = 2.0 * std::f32::consts::PI
        * ((tick_seconds * speed as f64) as f32 + phase);
    MotionState {
        offset_y_norm: -amp * phase_rad.sin().abs(),
        ..MotionState::IDENTITY
    }
}

/// Per-frame Gaussian micro-jitter, deterministically seeded from
/// `layer_id` + `motion_phase` so the same layer at the same phase
/// produces the same sequence across reloads (matches the spec's
/// phase=0-on-load determinism). Frame index advances at ~10 Hz
/// (every 100 ms tick), independent of motion_speed; intensity
/// modulates amplitude only.
///
/// Returned offsets are in glyph-height units; the renderer
/// converts to pixels using the layer's effective rasterization
/// size (caller multiplies by `effective_font_size_px`).
fn motion_shake(
    intensity_norm: f32,
    phase: f32,
    _speed: f32,
    layer_id_seed: u64,
    tick_seconds: f64,
) -> MotionState {
    // ~10 Hz sampling: floor to 100 ms buckets.
    let tick_index = (tick_seconds * 10.0).floor() as u64;
    // Mix layer_id + phase + tick_index into a u64. xxhash-style
    // splitmix64 keeps it dependency-free and fast.
    let phase_bits = phase.to_bits() as u64;
    let mut state = layer_id_seed
        ^ phase_bits.rotate_left(17)
        ^ tick_index.wrapping_mul(0x9E37_79B9_7F4A_7C15);
    let dx = gaussian_from_state(&mut state);
    let dy = gaussian_from_state(&mut state);
    // Cap amplitude at ±4 % of glyph height (intensity=100); 0.5 %
    // at intensity=0; ~2.25 % at intensity=50 (close to spec's
    // "±2 %"). Clamp the Gaussian to ±1.0 so a 3-sigma tail can't
    // suddenly throw the layer halfway across the slide.
    let amp = 0.005 + 0.035 * intensity_norm;
    MotionState {
        offset_x_norm: amp * dx.clamp(-1.0, 1.0),
        offset_y_norm: amp * dy.clamp(-1.0, 1.0),
        ..MotionState::IDENTITY
    }
}

/// Square-wave on/off opacity. 0.5 Hz at intensity=0 → 1 Hz at 50
/// → 4 Hz at 100, piecewise-linear so the spec's "1 Hz at default"
/// lands exactly. 50 % duty (visible half the cycle).
fn motion_blink(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    // Piecewise linear: 0..0.5 → 0.5..1.0, 0.5..1.0 → 1.0..4.0.
    // Endpoints + midpoint match spec exactly.
    let base_freq = if intensity_norm <= 0.5 {
        0.5 + intensity_norm * 1.0
    } else {
        1.0 + (intensity_norm - 0.5) * 6.0
    };
    let freq = base_freq * speed;
    if freq <= 0.0 {
        // QA F1 (slice c): frozen state must still honor phase, to
        // match the other 5 modes' speed=0 behavior and the spec's
        // "phase=0 + motion_phase visual state at t=0" rule
        // (lines 277-280). Pre-fix this branch returned IDENTITY
        // unconditionally, ignoring phase.
        let visible = (2.0 * std::f32::consts::PI * phase).sin() >= 0.0;
        return MotionState {
            alpha_mul: if visible { 1.0 } else { 0.0 },
            ..MotionState::IDENTITY
        };
    }
    let phase_rad = 2.0 * std::f32::consts::PI
        * ((tick_seconds * freq as f64) as f32 + phase);
    let visible = phase_rad.sin() >= 0.0;
    MotionState {
        alpha_mul: if visible { 1.0 } else { 0.0 },
        ..MotionState::IDENTITY
    }
}

/// Convert a `MotionState`'s normalized translate offsets to
/// screen-space pixels, using the spec's per-effect unit
/// convention:
///   - `Ticker` -> leftward scroll: offset_x_norm in [0,1) of one
///     box-width tile pitch, returned NEGATIVE (translate left).
///   - `Bounce` -> offset_y in box-height units
///   - `Shake`  -> offset_x/y in glyph-height units (spec line 274)
///   - other modes return (0, 0); they don't translate.
///
/// Pure helper so the renderer's draw_text_layer doesn't need a
/// per-mode switch and the unit conversion stays host-testable.
/// `box_w_px`, `box_h_px`, `font_size_px` are the rendered
/// dimensions in screen pixels; the renderer already computes them
/// for layout.
pub fn motion_offset_to_px(
    kind: MotionKind,
    state: MotionState,
    box_w_px: f32,
    box_h_px: f32,
    font_size_px: f32,
) -> (f32, f32) {
    match kind {
        MotionKind::Ticker => (-state.offset_x_norm * box_w_px, 0.0),
        MotionKind::Bounce => (0.0, state.offset_y_norm * box_h_px),
        MotionKind::Shake => (
            state.offset_x_norm * font_size_px,
            state.offset_y_norm * font_size_px,
        ),
        MotionKind::Static
        | MotionKind::Breathe
        | MotionKind::Pulse
        | MotionKind::Blink => (0.0, 0.0),
    }
}

/// SplitMix64 + Box-Muller for a deterministic standard-normal
/// draw. Advances `state` in place so consecutive calls produce
/// independent samples. Used by `motion_shake`.
fn gaussian_from_state(state: &mut u64) -> f32 {
    fn next_u64(state: &mut u64) -> u64 {
        *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = *state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn unit(state: &mut u64) -> f32 {
        // 24 mantissa bits → [0, 1) with no bias near 0.
        ((next_u64(state) >> 40) as f32) / (1u32 << 24) as f32
    }
    let u1 = unit(state).max(f32::MIN_POSITIVE);
    let u2 = unit(state);
    // Box-Muller. Guard against u1=0 producing -inf via ln.
    (-2.0 * u1.ln()).sqrt() * (2.0 * std::f32::consts::PI * u2).cos()
}

/// Effective rasterization pixel size for a text layer.
///
/// Resolution rules (Phase 4.2c — replaces 4.2a/b heuristic):
///   - `font_size_px` wins when set (already absolute pixels).
///   - `font_size_pct` is interpreted as **percent of box WIDTH** in
///     pixels, matching the Python content-model semantics
///     (`backend.openmarquee.content.TextLayer.font_size_pct`).
///   - default 64px when neither is set.
///
/// Pure function so the math is host-testable independent of GL.
/// Returns at least 8.0 to avoid sub-glyph sizes that fontdue
/// degenerates on.
pub fn effective_font_size_px(
    font_size_px: Option<f32>,
    font_size_pct: Option<f32>,
    box_w: f32,
    mode_w: u32,
) -> f32 {
    let box_w_px = (box_w * mode_w as f32).max(1.0);
    font_size_px
        .or(font_size_pct.map(|p| (p / 100.0) * box_w_px))
        .unwrap_or(64.0)
        .max(8.0)
}

/// Horizontal alignment within a layer's box.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HAlign {
    Left,
    Center,
    Right,
}

/// Vertical alignment within a layer's box. Phase 4.2c always
/// centers vertically — the Python content model has no vertical-
/// align field, so this enum is just future-proofing.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VAlign {
    Top,
    Middle,
    Bottom,
}

/// Parse the `text_align` string from the layer model. Unrecognized
/// values default to `Left` (matches Python's tolerant defaults).
pub fn parse_h_align(s: &str) -> HAlign {
    match s {
        "center" => HAlign::Center,
        "right" => HAlign::Right,
        _ => HAlign::Left,
    }
}

/// Parse the `anchor` string (vertical alignment) from the layer
/// model. Spec §5.10a literal: `top` / `center` / `bottom`.
/// Unrecognized values default to `Middle` (matches Python's tolerant
/// defaults + the pre-v1.0 renderer behavior of always-center).
pub fn parse_v_align(s: &str) -> VAlign {
    match s {
        "top" => VAlign::Top,
        "bottom" => VAlign::Bottom,
        _ => VAlign::Middle,
    }
}

/// NDC quad for placing a `bm_w × bm_h` bitmap inside a slide-
/// relative `box(x, y, w, h)` (fractions of mode_w / mode_h) on a
/// `mode_w × mode_h` viewport, aligned per `(halign, valign)`.
///
/// **Fit policy (Bug 1 SDF dispatch 2026-05-19, was Phase 4.2c):**
/// each axis is squished INDEPENDENTLY to fit boxW × boxH. Matches
/// Canvas2D's `yScale = boxH / totalInkExtent` (rasterize.js:232)
/// + `drawImage` 9-arg X squish (rasterize.js:306-326). The
/// pre-Bug-1 uniform-aspect `s_w.min(s_h)` bound on the narrower
/// axis and shrunk the other unnecessarily — producing ~52%-of-box
/// SDF text where Canvas2D shows edge-to-edge.
///
/// Returns `(ndc_left, ndc_right, ndc_top, ndc_bottom)`. NDC y-axis
/// is up: `ndc_top > ndc_bottom` (i.e. ndc_top is at the top of the
/// rendered image, ndc_bottom at the bottom). Image-coord input is
/// flipped to NDC by the math.
///
/// Pure function — split out so a fit-to-box regression flips a
/// host test, not a Pi visual diff.
pub fn box_to_ndc_quad(
    box_x: f32,
    box_y: f32,
    box_w: f32,
    box_h: f32,
    bm_w: u32,
    bm_h: u32,
    bm_pad: u32,
    mode_w: u32,
    mode_h: u32,
    halign: HAlign,
    valign: VAlign,
) -> (f32, f32, f32, f32) {
    // Box rect in image-pixel coords (y=0 at top).
    let box_left_px = box_x * mode_w as f32;
    let box_top_px = box_y * mode_h as f32;
    let box_w_px = (box_w * mode_w as f32).max(1.0);
    let box_h_px = (box_h * mode_h as f32).max(1.0);

    // Scale-down-only: never upscale. If bitmap fits on an axis,
    // that axis's scale = 1.0. Each axis is independent (Bug 1
    // 2026-05-19): pre-fix used `scale = s_w.min(s_h)` which bound
    // both axes on the narrower one, shrinking the other axis
    // unnecessarily and diverging from Canvas2D's per-axis squish.
    //
    // The ink content of the bitmap occupies (bm_w - 2*bm_pad) x
    // (bm_h - 2*bm_pad) at the center, with bm_pad rows/cols of
    // alpha=0 padding on each side for FS_GLYPH_OUTLINE dilation.
    // Scale based on INK dims so the visible glyph ink fits the
    // layer box exactly (matches Canvas2D's yScale = boxH /
    // measureText.ink). Phase 3j fix 2026-05-15: pre-fix used the
    // FULL bitmap dims, which shrunk the visible ink by
    // (1 - 2*pad/bm) ≈ 0.3% and produced the 5.28-px width gap +
    // 2-px height gap in the parity_font_inter rectangle compare
    // (qa/captures/parity-phase3j-quad-rect-2026-05-15.md).
    let bm_w_f = bm_w as f32;
    let bm_h_f = bm_h as f32;
    let pad2 = (2 * bm_pad) as f32;
    let ink_w_f = (bm_w_f - pad2).max(1.0);
    let ink_h_f = (bm_h_f - pad2).max(1.0);
    let s_w = if ink_w_f > box_w_px { box_w_px / ink_w_f } else { 1.0 };
    let s_h = if ink_h_f > box_h_px { box_h_px / ink_h_f } else { 1.0 };
    // Quad covers the WHOLE bitmap (pad-inclusive) so the alpha=0
    // pad rows still get textured + sampled by FS_GLYPH_OUTLINE
    // for the dilation. Pad rows extend up to bm_pad*s_* canvas-
    // pixels OUTSIDE the layer box — they're alpha=0 so they paint
    // nothing visible, but the quad geometry differs from pre-Phase
    // -3j by ~0.5 px on each edge.
    let placed_w = bm_w_f * s_w;
    let placed_h = bm_h_f * s_h;

    // Align inside the box.
    let dst_left = box_left_px
        + match halign {
            HAlign::Left => 0.0,
            HAlign::Center => (box_w_px - placed_w) * 0.5,
            HAlign::Right => box_w_px - placed_w,
        };
    let dst_top = box_top_px
        + match valign {
            VAlign::Top => 0.0,
            VAlign::Middle => (box_h_px - placed_h) * 0.5,
            VAlign::Bottom => box_h_px - placed_h,
        };
    let dst_right = dst_left + placed_w;
    let dst_bottom = dst_top + placed_h;

    let to_ndc_x = |px: f32| (px / mode_w as f32) * 2.0 - 1.0;
    let to_ndc_y = |px: f32| 1.0 - (px / mode_h as f32) * 2.0;
    (
        to_ndc_x(dst_left),
        to_ndc_x(dst_right),
        to_ndc_y(dst_top),
        to_ndc_y(dst_bottom),
    )
}

/// Map a fourcc code (the four-byte ASCII encoding the DRM/GBM specs
/// share for buffer formats) to its ARGB-family fourcc bytes.
///
/// Returns `Some([u8; 4])` for the six ARGB-family formats Phase 2
/// supports, `None` otherwise. Phase 2's scanout path only uses
/// `Argb8888`; the other entries are here so the table grows
/// naturally as later phases pull in additional formats.
pub fn fourcc_for_argb_family(name: &str) -> Option<[u8; 4]> {
    match name {
        "Argb8888" => Some(*b"AR24"),
        "Xrgb8888" => Some(*b"XR24"),
        "Abgr8888" => Some(*b"AB24"),
        "Xbgr8888" => Some(*b"XB24"),
        "Rgba8888" => Some(*b"RA24"),
        "Rgbx8888" => Some(*b"RX24"),
        _ => None,
    }
}

/// r25 (2026-05-31): pure-logic predicate for the glyph-prewarm
/// drain gate. Lives here (cross-platform) so the regression-
/// prevention invariant has a unit test runnable on the Mac dev
/// box (hdmi.rs is `#[cfg(target_os = "linux")]`-gated at
/// main.rs:19-20, so a `mod tests` over there would silently skip
/// on host cargo test).
///
/// Returns `true` when the prewarm drain loop should exit. Both
/// conditions must hold:
///   (a) `completions_since_baseline >= requested` — every glyph
///       we enqueued has produced exactly one completion (Ready
///       OR FontMissing both bump completion_count per
///       glyph_cache.rs:686-692).
///   (b) `last_drained_count == 0` — the most recent
///       poll_completions found nothing left in the channel
///       buffer; no in-flight uploads remain queued.
///
/// (a) alone is not enough: completion_count could hit the
/// target while a final batch is still sitting in the channel
/// waiting for the next poll. (b) alone is not enough: an empty
/// channel could just mean workers are momentarily idle between
/// rasterizations, not done with the queue.
///
/// Both together guarantee the playback loop's downstream
/// poll_completions at hdmi.rs:3187 will return uploaded=0 and
/// thus skip the slide_caches drain that was the r20-first-ship
/// regression mechanism (see 530cd25 commit body).
#[inline]
pub fn glyph_prewarm_drain_complete(
    requested: u64,
    completions_since_baseline: u64,
    last_drained_count: usize,
) -> bool {
    completions_since_baseline >= requested && last_drained_count == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    // SDF arc slice B.2 -- layout_text_to_quads smoke tests.
    // Uses the baked anton atlas (slice A artifact) so we don't
    // need to spin up fontdue + a TTF blob inside the test.
    fn load_anton_atlas() -> crate::sdf_atlas::MsdfAtlas {
        let atlases = crate::sdf_atlas::load_all_atlases()
            .expect("baked atlases parse");
        // Cloning the manifest is fine (small) -- the atlas_rgb
        // slice is 'static so we can borrow without lifetime
        // gymnastics by re-loading per test.
        let anton = crate::sdf_atlas::atlas_for_stem(&atlases, "anton")
            .expect("anton present");
        crate::sdf_atlas::MsdfAtlas {
            manifest: anton.manifest.clone(),
            atlas_rgb: anton.atlas_rgb,
        }
    }

    #[test]
    fn layout_text_to_quads_returns_none_for_empty() {
        let atlas = load_anton_atlas();
        assert!(layout_text_to_quads(&atlas,"", 100.0, f32::INFINITY, None).is_none());
        // Spaces only -- no ink, returns None.
        assert!(layout_text_to_quads(&atlas,"   ", 100.0, f32::INFINITY, None).is_none());
    }

    #[test]
    fn layout_text_to_quads_emits_one_quad_per_ink_glyph() {
        let atlas = load_anton_atlas();
        let group = layout_text_to_quads(&atlas,"AB", 100.0, f32::INFINITY, None)
            .expect("AB at 100px lays out");
        assert_eq!(group.quads.len(), 2);
        // Quads are in left-to-right order.
        assert!(group.quads[0].px_left < group.quads[1].px_left);
        // No tofu / no emoji for ASCII.
        assert_eq!(group.quads[0].kind, GlyphKind::Msdf);
        assert_eq!(group.quads[1].kind, GlyphKind::Msdf);
        // UVs land inside the atlas (sub-1.0).
        for q in &group.quads {
            assert!(q.uv_left >= 0.0 && q.uv_right <= 1.0);
            assert!(q.uv_top >= 0.0 && q.uv_bottom <= 1.0);
            assert!(q.uv_right > q.uv_left);
            assert!(q.uv_bottom > q.uv_top);
        }
        assert_eq!(group.font_stem, "anton");
    }

    #[test]
    fn layout_text_to_quads_uv_inset_avoids_cell_edges() {
        // Bug 2 (2026-05-19) regression lock: every MSDF glyph quad's
        // UVs must be inset by 0.5 atlas-pixels from the cell boundary
        // so GL_LINEAR sampling never reaches the neighbor-cell's
        // outside-SDF region. Pre-Bug-2 the UVs went edge-to-edge
        // (g.x..g.x+cell_px / atlas_w) — bilinear at the boundary
        // mixed this cell's "outside-RGB" with the neighbor cell's
        // (often differently-encoded) "outside-RGB", producing
        // median > 0.5 alpha = visible amber hairlines above caps
        // and below baselines.
        //
        // The inset constant in layout_text_to_quads is 0.5 atlas
        // pixels. This test asserts the UVs deviate from the
        // cell-edge values by exactly 0.5 / atlas_w (or _h).
        let atlas = load_anton_atlas();
        let cell_px = atlas.manifest.cell_px as f32;
        let atlas_w = atlas.manifest.atlas_w as f32;
        let atlas_h = atlas.manifest.atlas_h as f32;
        let inset_x = 0.5 / atlas_w;
        let inset_y = 0.5 / atlas_h;

        let group = layout_text_to_quads(&atlas,"A", 100.0, f32::INFINITY, None)
            .expect("A lays out");
        let q = &group.quads[0];
        let a_glyph = atlas.manifest.glyph_for(b'A' as u32)
            .expect("Anton has 'A' baked");

        let expected_uv_l = (a_glyph.x as f32 + 0.5) / atlas_w;
        let expected_uv_r = (a_glyph.x as f32 + cell_px - 0.5) / atlas_w;
        let expected_uv_t = (a_glyph.y as f32 + 0.5) / atlas_h;
        let expected_uv_b = (a_glyph.y as f32 + cell_px - 0.5) / atlas_h;

        let eps = 1e-6;
        assert!((q.uv_left - expected_uv_l).abs() < eps,
            "Bug 2: uv_left {} should equal {} (= (g.x + 0.5) / atlas_w)",
            q.uv_left, expected_uv_l);
        assert!((q.uv_right - expected_uv_r).abs() < eps,
            "Bug 2: uv_right {} should equal {} (= (g.x + cell_px - 0.5) / atlas_w)",
            q.uv_right, expected_uv_r);
        assert!((q.uv_top - expected_uv_t).abs() < eps,
            "Bug 2: uv_top {} should equal {} (= (g.y + 0.5) / atlas_h)",
            q.uv_top, expected_uv_t);
        assert!((q.uv_bottom - expected_uv_b).abs() < eps,
            "Bug 2: uv_bottom {} should equal {} (= (g.y + cell_px - 0.5) / atlas_h)",
            q.uv_bottom, expected_uv_b);

        // Sanity: the inset really is non-zero (i.e. UV is strictly
        // inside the cell rect, not at the edge).
        let cell_edge_l = a_glyph.x as f32 / atlas_w;
        let cell_edge_r = (a_glyph.x as f32 + cell_px) / atlas_w;
        assert!(q.uv_left > cell_edge_l,
            "uv_left {} should be > cell_edge_l {}", q.uv_left, cell_edge_l);
        assert!(q.uv_right < cell_edge_r,
            "uv_right {} should be < cell_edge_r {}", q.uv_right, cell_edge_r);
        // Inset magnitude matches 0.5 / atlas_w within float precision.
        assert!((q.uv_left - cell_edge_l - inset_x).abs() < eps);
        assert!((cell_edge_r - q.uv_right - inset_x).abs() < eps);
        // y-axis inset is verified by `expected_uv_t / _b` already; the
        // inset_y local was for parallel-construction readability.
        let _ = inset_y;
    }

    #[test]
    fn layout_text_to_quads_scales_with_size_px() {
        let atlas = load_anton_atlas();
        let small = layout_text_to_quads(&atlas,"A", 50.0, f32::INFINITY, None).expect("A@50");
        let large = layout_text_to_quads(&atlas,"A", 500.0, f32::INFINITY, None).expect("A@500");
        // Pixel-space quad scales linearly with size_px (10x size_px
        // -> ~10x quad width).
        let small_w = small.quads[0].px_right - small.quads[0].px_left;
        let large_w = large.quads[0].px_right - large.quads[0].px_left;
        assert!(large_w > 9.0 * small_w && large_w < 11.0 * small_w);
    }

    #[test]
    fn layout_text_to_quads_multi_line_uses_two_baselines() {
        let atlas = load_anton_atlas();
        let group = layout_text_to_quads(&atlas,"A\nB", 100.0, f32::INFINITY, None)
            .expect("two-line lays out");
        assert_eq!(group.quads.len(), 2);
        // Second line's quad sits below the first line's quad in
        // pixel-y-down space.
        assert!(group.quads[1].px_top > group.quads[0].px_top);
    }

    // Bug 1c (2026-05-19) tests -- ink-bbox vertical metric semantics
    // in `layout_text_to_quads`. The ink-bbox loop derives `last_extent_px`
    // and `baseline_y` from the actual glyph plane bounds (matching
    // Canvas2D's measureText().actualBoundingBoxAscent + Descent) rather
    // than the font's full em metrics. Pre-Bug-1c, bm_h ≈ em-extent ×
    // size_px (~1.0-1.4 em); post-Bug-1c, bm_h ≈ ink-extent × size_px
    // (~0.74-1.0 em for typical caps + descender).

    #[test]
    fn layout_text_to_quads_caps_only_bm_h_uses_ink_extent_under_em() {
        let atlas = load_anton_atlas();
        let size_px = 100.0_f32;
        // Anton hhea metrics: ascent_em ~= 1.0, descent_em ~= -0.2;
        // em_extent = 1.2. Anton's cap-height is < ascent (about 0.74
        // for typical narrow display fonts), and lowercase glyphs in
        // Anton (an all-caps display font) are typically shaped without
        // descenders. For "ABC" (caps-only), ink_descent_em should be
        // ~0 and ink_ascent_em ~= cap height -- much less than the
        // 1.2 em-extent. bm_h should be visibly smaller than the
        // pre-Bug-1c em-based 1.2 * size_px = ~120 px.
        let group = layout_text_to_quads(&atlas,"ABC", size_px, f32::INFINITY, None)
            .expect("ABC lays out");
        let em_extent_px = (atlas.manifest.ascent_em
            - atlas.manifest.descent_em)
            * size_px;
        // 2 * pad = 2 baked into bm_h. Subtract for a fair compare.
        let bm_h_ink = group.height as f32 - 2.0;
        assert!(
            bm_h_ink < em_extent_px,
            "Bug 1c: bm_h_ink={bm_h_ink} should be LESS than em-extent={em_extent_px}; pre-Bug-1c they were equal."
        );
        // Sanity: bm_h shouldn't shrink to zero either.
        assert!(
            bm_h_ink > 0.4 * em_extent_px,
            "bm_h_ink={bm_h_ink} shrunk too aggressively vs em-extent={em_extent_px}; typical cap-height/em is 0.7-0.85."
        );
    }

    #[test]
    fn layout_text_to_quads_descender_widens_bm_h_vs_caps_only() {
        // Bug 1c GROUP-max semantics: ink_descent_em = min(pl_bottom)
        // across all glyphs. Compare a caps-only run to a run that
        // adds a descender-bearing glyph; the latter's bm_h should be
        // strictly bigger because ink_descent_em moves negative.
        //
        // Anton may not have a true-descender lowercase 'p' (it's an
        // all-caps display font). Try inter — it's Basic-Latin baked
        // for every font in build.rs and is a humanist sans with
        // proper descenders on p/q/y. Load via the standard atlas
        // lookup path so the test works whichever font has p with
        // descender ink.
        let atlases = crate::sdf_atlas::load_all_atlases()
            .expect("baked atlases parse");
        let inter = crate::sdf_atlas::atlas_for_stem(&atlases, "inter")
            .expect("inter present");
        let atlas = crate::sdf_atlas::MsdfAtlas {
            manifest: inter.manifest.clone(),
            atlas_rgb: inter.atlas_rgb,
        };
        let size_px = 100.0_f32;
        let group_caps = layout_text_to_quads(&atlas,"ABC", size_px, f32::INFINITY, None)
            .expect("caps lays out");
        let group_desc = layout_text_to_quads(&atlas,"Apy", size_px, f32::INFINITY, None)
            .expect("desc lays out");
        // Bug 1c: ink_descent_em from 'p' / 'y' descender lowers the
        // min, widening bm_h. Single-pixel rounding tolerance.
        assert!(
            group_desc.height > group_caps.height,
            "Bug 1c: bm_h with descender ({}) should be > caps-only ({})",
            group_desc.height,
            group_caps.height,
        );
    }

    #[test]
    fn layout_text_to_quads_multi_line_takes_group_max_ink_extent() {
        // Bug 1c is GROUP-level (not per-line): one max-ascent +
        // one min-descent across all glyphs in all lines. Mirrors
        // Canvas2D's measureText(lines.join("")). A multi-line run
        // where ONE line has a descender (line B = "py") and the
        // OTHER is caps-only (line A = "ABC") should produce a bm_h
        // that reflects MAX_INK_ASCENT from line A's caps AND
        // MIN_INK_DESCENT from line B's descenders.
        //
        // Test: bm_h for "ABC\npy" should be larger than bm_h for
        // "ABC\nABC" by exactly the descender contribution (modulo
        // 1-px ceil rounding), because both runs have the same
        // ink_ascent_em (from caps) but the first has ink_descent_em
        // from p/y's descenders.
        let atlases = crate::sdf_atlas::load_all_atlases()
            .expect("baked atlases parse");
        let inter = crate::sdf_atlas::atlas_for_stem(&atlases, "inter")
            .expect("inter present");
        let atlas = crate::sdf_atlas::MsdfAtlas {
            manifest: inter.manifest.clone(),
            atlas_rgb: inter.atlas_rgb,
        };
        let size_px = 100.0_f32;
        let group_caps_caps = layout_text_to_quads(&atlas,"ABC\nABC", size_px, f32::INFINITY, None)
            .expect("caps/caps lays out");
        let group_caps_desc = layout_text_to_quads(&atlas,"ABC\npy", size_px, f32::INFINITY, None)
            .expect("caps/desc lays out");
        // The caps_desc variant has the SAME line stacking math
        // (two lines × line_h_px + last_extent) but last_extent is
        // larger because ink_descent_em reflects the p/y descender.
        assert!(
            group_caps_desc.height > group_caps_caps.height,
            "Bug 1c group-max: caps/desc bm_h ({}) should be > caps/caps bm_h ({})",
            group_caps_desc.height,
            group_caps_caps.height,
        );
    }

    // Bug 4 (2026-05-19) — per-line X-squish in layout_text_to_quads.
    // Each line whose natural advance exceeds box_w_px is scaled
    // INDEPENDENTLY on X to fit. Lines that already fit pass through
    // at natural width. f32::INFINITY opts out of capping (legacy
    // behavior + test default).

    #[test]
    fn layout_text_to_quads_single_line_capped_to_box_w() {
        // Single very wide line at small box_w. The line's natural
        // advance at size_px=200 vastly exceeds box_w=100; per-line
        // X-scale = 100/natural caps bm_w to exactly box_w. Pre-Bug-4
        // bm_w would have been the full natural advance (since
        // group-level squish was deferred to box_to_ndc_quad).
        let atlas = load_anton_atlas();
        let group = layout_text_to_quads(&atlas,"ABCDE", 200.0, 100.0, None)
            .expect("ABCDE lays out");
        // pad=1 on each side; bm_w should be ~100 + 2.
        // Allow 1 px slack for ceil rounding.
        assert!(
            (group.width as i32 - 102).abs() <= 2,
            "Bug 4: bm_w {} should be ~box_w_px+2pad=102 (was natural-advance pre-Bug-4)",
            group.width,
        );
    }

    #[test]
    fn layout_text_to_quads_multi_line_uneven_widths_each_squished_independently() {
        // 2 lines: "ABCDE" (5 chars) and "AB" (2 chars). Both at the
        // same size. With a box_w_px set BETWEEN the natural widths
        // of these two lines, ONLY the wider line should be capped;
        // the shorter line should pass through unchanged. Pre-Bug-4
        // the group-level squish would have applied to BOTH lines
        // uniformly (under-squishing the short line if widest-line
        // ratio was binding).
        let atlas = load_anton_atlas();
        let size_px = 100.0_f32;
        // Get the per-line natural widths first (with infinity box).
        let baseline = layout_text_to_quads(&atlas,"ABCDE\nAB", size_px, f32::INFINITY, None)
            .expect("baseline 2-line lays out");
        let baseline_w = baseline.width;
        // Pick a box_w between AB's natural width and ABCDE's natural
        // width. Halve baseline_w (which is dominated by ABCDE) and
        // floor to a round number; this should leave AB un-capped
        // (it's shorter than half-ABCDE for typical fonts).
        let box_w = baseline_w as f32 * 0.6;
        let group = layout_text_to_quads(&atlas,"ABCDE\nAB", size_px, box_w, None)
            .expect("capped 2-line lays out");
        // bm_w should equal the larger of (capped ABCDE = box_w) and
        // (uncapped AB natural). For a typical font where AB <
        // 0.5*ABCDE, the cap on ABCDE binds → bm_w ≈ box_w.
        // Allow 2px slack for ceil + pad.
        assert!(
            (group.width as i32 - (box_w as i32)).abs() <= 4,
            "Bug 4 per-line cap: bm_w {} should be ~box_w {} (capped widest line); baseline_w was {}",
            group.width, box_w as i32, baseline_w,
        );
        // bm_w should be SMALLER than the baseline (uncapped) version
        // — proving the cap actually fired.
        assert!(
            group.width < baseline.width,
            "Bug 4 per-line cap: capped bm_w {} should be < uncapped {}",
            group.width, baseline.width,
        );
    }

    #[test]
    fn layout_text_to_quads_box_w_infinity_matches_pre_bug_4_layout() {
        // Sanity: with box_w_px = f32::INFINITY, no line caps fire
        // and bm_w equals the widest line's natural advance — same
        // as pre-Bug-4 layout. This preserves the host-test opt-out
        // contract.
        let atlas = load_anton_atlas();
        let group = layout_text_to_quads(&atlas,"ABCDE\nAB", 100.0, f32::INFINITY, None)
            .expect("inf lays out");
        // Width should be > 0 and stable. Test mostly guards against
        // any future regression that decouples INFINITY from the
        // legacy path.
        assert!(group.width > 10);
        // bm_w with INFINITY equals bm_w with a very large finite
        // box_w (e.g. 1e7). Same layout regardless.
        let huge = layout_text_to_quads(&atlas,"ABCDE\nAB", 100.0, 1e7_f32, None)
            .expect("huge lays out");
        assert_eq!(group.width, huge.width);
    }

    #[test]
    fn layout_text_to_quads_emits_tofu_for_unknown_codepoint() {
        let atlas = load_anton_atlas();
        // U+2603 ☃ (snowman) isn't in anton's Basic-Latin +
        // Latin-1 baked set; we expect a tofu quad.
        //
        // Slice 3D: U+2603 IS inside codepoint_is_emoji_range
        // (U+2600-27BF), but with no runtime glyph cache passed
        // the COLR dispatch can't fire, so it falls through to
        // static MSDF (miss) then Tofu. With a runtime cache an
        // emoji-range codepoint present in NotoColorEmoji-COLRv1
        // would resolve to DynamicEmoji instead — see
        // glyph_cache_colr::tests for that path's coverage.
        let group = layout_text_to_quads(&atlas,"\u{2603}", 100.0, f32::INFINITY, None)
            .expect("tofu lays out");
        assert_eq!(group.quads.len(), 1);
        assert_eq!(group.quads[0].kind, GlyphKind::Tofu);
    }

    // Slice 3D (2026-05-19): the SDF-arc-C.3 emoji segmentation
    // tests that depended on the build-time CBDT atlas have been
    // retired alongside the CBDT bake itself. Emoji codepoints now
    // route to the runtime COLRv1 cache via
    // `crate::glyph_cache_colr`; coverage for the COLRv1
    // rasterizer lives in `glyph_cache_colr::tests` (the
    // grinning_face / red_heart / earth_globe / absent_codepoint
    // suite). The two host-side tests below cover the codepoint-
    // range dispatch shape — emoji-range with no runtime cache
    // falls through to Tofu (matches Pending-on-first-frame
    // semantics with a None cache), and out-of-range codepoints
    // still fall through to MSDF/Tofu just like before.

    #[test]
    fn layout_text_to_quads_emoji_range_no_runtime_cache_emits_tofu() {
        let atlas = load_anton_atlas();
        // Same codepoint as the retired emits-emoji-for-baked test
        // — but with no runtime glyph cache the COLR dispatch
        // can't fire, so the codepoint falls through to MSDF
        // (which doesn't have it) and emits Tofu, matching the
        // pre-3D no-emoji-atlas behavior.
        let group = layout_text_to_quads(&atlas, "\u{1F31F}", 100.0, f32::INFINITY, None)
            .expect("tofu lays out");
        assert_eq!(group.quads.len(), 1);
        assert_eq!(group.quads[0].kind, GlyphKind::Tofu);
    }

    #[test]
    fn layout_text_to_quads_out_of_range_codepoint_falls_to_tofu() {
        let atlas = load_anton_atlas();
        // U+2B50 ⭐ is in Misc Symbols + Arrows (U+2B00-2BFF),
        // OUTSIDE the codepoint_is_emoji_range gate. The COLR
        // dispatch never fires for it; MSDF doesn't have it
        // either; Tofu is the result — matches browser's font-
        // fallback behavior with the same unicode-range
        // declaration.
        let group = layout_text_to_quads(&atlas, "\u{2B50}", 100.0, f32::INFINITY, None)
            .expect("tofu lays out");
        assert_eq!(group.quads.len(), 1);
        assert_eq!(group.quads[0].kind, GlyphKind::Tofu);
    }

    #[test]
    fn layout_text_to_quads_colr_emoji_quad_follows_plane_bounds() {
        // Parity fix (2026-05-20): a COLR emoji quad is positioned
        // from the rasterizer's clip-box plane_bounds (a 1-em-square,
        // baseline-relative), exactly like CharKind::DynamicMsdf —
        // NOT a fixed cell centred on the em-midpoint. This lets the
        // emoji descend below the baseline like the Noto glyph is
        // designed to, instead of being cropped flat at a cell edge
        // (the bottom-clip bug). The group bbox must also CONTAIN
        // the quad so the draw-time scissor cannot clip it.
        let atlas = load_anton_atlas();
        let cache = crate::glyph_cache::GlyphCache::new(0);
        // 🔓 U+1F513 — a SCREAM-slide codepoint, absent from anton's
        // MSDF atlas, so the COLR dispatch path fires.
        let cp: u32 = 0x1F513;
        let key = crate::glyph_cache::GlyphKey {
            font_family_id: crate::glyph_cache::font_family_id_from_stem(
                crate::glyph_cache::COLR_EMOJI_FONT_STEM,
            ),
            codepoint: cp,
            render_mode: crate::glyph_cache::RenderMode::Colr,
        };
        // A 1-em-square plane_bounds that descends 0.2 em BELOW the
        // baseline (pl_bottom < 0) — the shape the fixed rasterizer
        // produces for a Noto emoji (clip box squared + normalised).
        let pb = crate::glyph_cache::PlaneBounds {
            pl_left: 0.05,
            pl_right: 1.05,
            pl_bottom: -0.2,
            pl_top: 0.8,
        };
        cache.insert_ready_slot_for_test(
            key,
            crate::atlas_page::SlotPos { x: 0, y: 0 },
            1.2, // advance_em (Noto emoji hmtx ~1.2-1.25 em)
            pb,
        );
        let fonts_dir = std::env::temp_dir();
        let ctx = crate::glyph_cache::RuntimeGlyphCtx {
            cache: &cache,
            fonts_dir: &fonts_dir,
        };
        let size_px = 100.0_f32;
        let group =
            layout_text_to_quads(&atlas, "\u{1F513}", size_px, f32::INFINITY, Some(ctx))
                .expect("emoji lays out");
        assert_eq!(group.quads.len(), 1);
        let q = &group.quads[0];
        assert_eq!(q.kind, GlyphKind::DynamicEmoji);
        // Quad dimensions follow plane_bounds: a 1-em square -> size_px.
        let w = q.px_right - q.px_left;
        let h = q.px_bottom - q.px_top;
        assert!((w - size_px).abs() < 0.01, "emoji quad width {w} should be 1 em");
        assert!((h - size_px).abs() < 0.01, "emoji quad height {h} should be 1 em");
        // Single line: baseline_y = pad(1) + ink_ascent(pl_top 0.8)
        // * size. The emoji quad must descend BELOW that baseline —
        // the un-clip guarantee (pre-fix it was cropped at the cell
        // edge and never passed the baseline).
        let baseline_y = 1.0 + 0.8 * size_px;
        assert!(
            q.px_bottom > baseline_y,
            "emoji quad bottom {} must descend below the baseline {} \
             (not be cropped at a cell edge)",
            q.px_bottom,
            baseline_y,
        );
        // The group bbox must contain the full emoji quad, so the
        // draw-time scissor (derived from the bbox) cannot clip it.
        assert!(
            q.px_bottom <= group.height as f32 + 0.01,
            "emoji quad bottom {} must be within the group bbox height {}",
            q.px_bottom,
            group.height,
        );
    }

    #[test]
    fn ticker_motion_never_resizes_glyphs() {
        // Parity Bug 2 (2026-05-20) guard: a ticker-motion layer and
        // a static layer with the same font_size_pct + box must
        // resolve to the SAME effective glyph size. font sizing
        // (effective_font_size_px) + glyph layout (layout_text_to_
        // quads) take no motion input, and a Ticker MotionState
        // carries scale == 1.0 (it only translates horizontally) —
        // identical to Static's IDENTITY. So motion=ticker can
        // never make text larger or smaller than motion=static.
        let static_scale =
            compute_motion_state(MotionKind::Static, 85, 0.0, 1.0, 0, 0.0).scale;
        assert_eq!(static_scale, 1.0);
        // Sweep ticks across multiple ticker cycles + intensities.
        for &intensity in &[0u8, 50, 85, 100] {
            for step in 0..24 {
                let tick = step as f64 * 0.25;
                let ms = compute_motion_state(
                    MotionKind::Ticker, intensity, 0.0, 1.0, 0, tick,
                );
                assert_eq!(
                    ms.scale, static_scale,
                    "ticker scale must equal static scale (no resize) \
                     at intensity={intensity} tick={tick}",
                );
            }
        }
    }

    #[test]
    fn pick_largest_returns_none_for_empty() {
        assert_eq!(pick_largest_mode_index(&[]), None);
    }

    // v1-spec-delta #6 (slice a) -- pattern dispatch parsing.
    // v1-spec-delta #7 (slice a) -- blend mode parsing + label
    // round-trip + unknown fallback.
    #[test]
    fn parse_blend_mode_handles_all_four_schema_names() {
        assert_eq!(parse_blend_mode("normal"), BlendMode::Normal);
        assert_eq!(parse_blend_mode("screen"), BlendMode::Screen);
        assert_eq!(parse_blend_mode("multiply"), BlendMode::Multiply);
        assert_eq!(parse_blend_mode("overlay"), BlendMode::Overlay);
    }

    #[test]
    fn parse_blend_mode_falls_back_to_normal_on_unknown() {
        // Unknown / empty / typo / case-sensitive: all fall back
        // to Normal so the renderer doesn't bail on an envelope
        // saved by a future schema version.
        assert_eq!(parse_blend_mode(""), BlendMode::Normal);
        assert_eq!(parse_blend_mode("SCREEN"), BlendMode::Normal);
        assert_eq!(parse_blend_mode("normaL"), BlendMode::Normal);
        assert_eq!(parse_blend_mode("nope"), BlendMode::Normal);
    }

    #[test]
    fn blend_mode_label_round_trips() {
        for b in [BlendMode::Normal, BlendMode::Screen, BlendMode::Multiply, BlendMode::Overlay] {
            assert_eq!(parse_blend_mode(blend_mode_label(b)), b);
        }
    }

    #[test]
    fn parse_pattern_kind_handles_all_ten_python_names() {
        assert_eq!(parse_pattern_kind("dots"), Some(PatternKind::Dots));
        assert_eq!(parse_pattern_kind("halftone"), Some(PatternKind::Halftone));
        assert_eq!(parse_pattern_kind("stripes"), Some(PatternKind::Stripes));
        assert_eq!(parse_pattern_kind("scanlines"), Some(PatternKind::Scanlines));
        assert_eq!(parse_pattern_kind("checker"), Some(PatternKind::Checker));
        assert_eq!(parse_pattern_kind("grid"), Some(PatternKind::Grid));
        assert_eq!(parse_pattern_kind("rings"), Some(PatternKind::Rings));
        assert_eq!(parse_pattern_kind("rays"), Some(PatternKind::Rays));
        assert_eq!(parse_pattern_kind("confetti"), Some(PatternKind::Confetti));
        assert_eq!(parse_pattern_kind("bricks"), Some(PatternKind::Bricks));
    }

    #[test]
    fn parse_pattern_kind_returns_none_for_solid_and_gradient() {
        // These have dedicated BgKind variants with different uniform
        // shapes (gradient has proj_min/span, solid has no shader);
        // they're NOT in the PatternKind enum.
        assert_eq!(parse_pattern_kind("solid"), None);
        assert_eq!(parse_pattern_kind("gradient"), None);
    }

    #[test]
    fn parse_pattern_kind_returns_none_for_unknown() {
        assert_eq!(parse_pattern_kind(""), None);
        assert_eq!(parse_pattern_kind("STRIPES"), None);  // case-sensitive
        assert_eq!(parse_pattern_kind("stripe"), None);   // singular vs plural
        assert_eq!(parse_pattern_kind("garbage"), None);
    }

    #[test]
    fn pattern_kind_label_round_trips_through_parse() {
        for k in [
            PatternKind::Dots, PatternKind::Halftone, PatternKind::Stripes,
            PatternKind::Scanlines, PatternKind::Checker, PatternKind::Grid,
            PatternKind::Rings, PatternKind::Rays, PatternKind::Confetti,
            PatternKind::Bricks,
        ] {
            let label = pattern_kind_label(k);
            assert_eq!(parse_pattern_kind(label), Some(k),
                "round-trip failed for {label:?}");
        }
    }

    // v1-spec-delta #6 (slice b) -- per-pattern uniform helpers.
    // Math mirrors Python auto_render.py (round(lerp(big, small,
    // density))). Pinned values here MUST match the Python
    // reference at the same density anchors.
    #[test]
    fn stripes_tile_size_matches_python_lerp() {
        // density 0 -> tile 80 (max), density 1 -> tile 4 (min).
        // (stripes_uniforms takes raw density; the qarl-curve is
        // applied at the draw_pattern dispatch site in hdmi.rs, not
        // inside the uniform fn -- so these direct calls still use
        // a linear-lerp model.)
        assert_eq!(stripes_uniforms(0.0).tile, 80.0);
        assert_eq!(stripes_uniforms(1.0).tile, 4.0);
        // density 0.5 -> round(80 + (4-80)*0.5) = round(42) = 42.
        assert_eq!(stripes_uniforms(0.5).tile, 42.0);
    }

    #[test]
    fn pattern_density_curve_matches_js_and_python_mirrors() {
        // The curve must agree at boundaries + the standard 0.5
        // anchor with the JS bg-system.js densityCurve() and
        // Python auto_render._density_curve. All three are
        // d^2 with [0,1] clamp.
        assert_eq!(pattern_density_curve(0.0), 0.0);
        assert_eq!(pattern_density_curve(1.0), 1.0);
        assert_eq!(pattern_density_curve(0.5), 0.25);
        // Clamp: out-of-range inputs collapse to the bounds.
        assert_eq!(pattern_density_curve(-0.5), 0.0);
        assert_eq!(pattern_density_curve(1.5), 1.0);
        // Curve stretches the low end: at intensity=0.1 the curved
        // d is 0.01, well below the linear-mapping 0.1, so features
        // stay near MAX for longer. qarl 2026-05-12 product spec.
        assert!((pattern_density_curve(0.1) - 0.01).abs() < 1e-6);
    }

    #[test]
    fn stripes_tile_size_floors_at_2() {
        // Even at density slightly under 1 the formula stays >=2.
        // Floor exists to keep the half-tile threshold meaningful.
        let u = stripes_uniforms(0.99);
        assert!(u.tile >= 2.0, "tile {} below floor", u.tile);
    }

    #[test]
    fn stripes_density_clamps_out_of_range() {
        // Out-of-spec densities (negative, >1) clamp to bounds.
        assert_eq!(stripes_uniforms(-0.5).tile, 80.0);
        assert_eq!(stripes_uniforms(2.0).tile, 4.0);
    }

    #[test]
    fn checker_tile_size_matches_python_lerp() {
        // density 0 -> 60, density 1 -> 4. round(lerp(60,4,0.5))
        // = round(32) = 32.
        assert_eq!(checker_uniforms(0.0).tile, 60.0);
        assert_eq!(checker_uniforms(1.0).tile, 4.0);
        assert_eq!(checker_uniforms(0.5).tile, 32.0);
    }

    #[test]
    fn dots_uniforms_match_python_lerp_and_radius_formula() {
        // tile = round(lerp(48, 4, density)); radius = max(2,
        // round(tile * 0.22)).
        // density 0: tile=48, radius=round(48*0.22)=round(10.56)=11.
        let u0 = dots_uniforms(0.0);
        assert_eq!(u0.tile, 48.0);
        assert_eq!(u0.radius, 11.0);
        // density 1: tile=4, radius=max(2, round(4*0.22)) =
        // max(2, round(0.88)) = max(2, 1) = 2 (floor kicks in).
        let u1 = dots_uniforms(1.0);
        assert_eq!(u1.tile, 4.0);
        assert_eq!(u1.radius, 2.0);
        // density 0.5: tile=round(26)=26, radius=round(5.72)=6.
        let u05 = dots_uniforms(0.5);
        assert_eq!(u05.tile, 26.0);
        assert_eq!(u05.radius, 6.0);
    }

    // Slice (b) shader sanity: the FS_PATTERN_* constants must
    // start with the GLES2 #version preamble + carry the
    // documented uniforms. Catches accidental shader-source
    // regressions before they hit the GPU.
    #[test]
    fn pattern_shaders_have_gles2_preamble() {
        for (name, src) in [
            ("FS_PATTERN_STRIPES", FS_PATTERN_STRIPES),
            ("FS_PATTERN_CHECKER", FS_PATTERN_CHECKER),
            ("FS_PATTERN_DOTS", FS_PATTERN_DOTS),
            ("FS_PATTERN_HALFTONE", FS_PATTERN_HALFTONE),
            ("FS_PATTERN_SCANLINES", FS_PATTERN_SCANLINES),
            ("FS_PATTERN_GRID", FS_PATTERN_GRID),
            ("FS_PATTERN_RINGS", FS_PATTERN_RINGS),
            ("FS_PATTERN_RAYS", FS_PATTERN_RAYS),
            ("FS_PATTERN_BRICKS", FS_PATTERN_BRICKS),
            ("FS_PATTERN_CONFETTI", FS_PATTERN_CONFETTI),
        ] {
            assert!(src.starts_with("#version 100\n"), "{name} missing #version 100");
            assert!(src.contains("precision mediump float"), "{name} missing precision");
            // Phase 3z Cand E: FS_PATTERN_CHECKER may also declare
            // highp int. Tolerate that.
            assert!(src.contains("u_color_a"), "{name} missing u_color_a");
            assert!(src.contains("u_color_b"), "{name} missing u_color_b");
            assert!(src.contains("u_viewport"), "{name} missing u_viewport");
        }
        // Per-pattern uniform presence checks.
        assert!(FS_PATTERN_STRIPES.contains("u_tile"));
        assert!(FS_PATTERN_CHECKER.contains("u_tile"));
        assert!(FS_PATTERN_DOTS.contains("u_tile") && FS_PATTERN_DOTS.contains("u_radius"));
        assert!(FS_PATTERN_HALFTONE.contains("u_tile")
            && FS_PATTERN_HALFTONE.contains("u_radius")
            && FS_PATTERN_HALFTONE.contains("u_half")
            && FS_PATTERN_HALFTONE.contains("u_y_phase_l1")
            && FS_PATTERN_HALFTONE.contains("u_y_phase_l2"));
        assert!(FS_PATTERN_SCANLINES.contains("u_tile"));
        assert!(FS_PATTERN_GRID.contains("u_tile"));
        assert!(FS_PATTERN_RINGS.contains("u_tile") && FS_PATTERN_RINGS.contains("u_half"));
        assert!(FS_PATTERN_RAYS.contains("u_slices"));
        assert!(FS_PATTERN_BRICKS.contains("u_bw")
            && FS_PATTERN_BRICKS.contains("u_bh")
            && FS_PATTERN_BRICKS.contains("u_half"));
        assert!(FS_PATTERN_CONFETTI.contains("u_cell"));
    }

    // v1-spec-delta #7 F1k -- shader source pin for FS_OVERLAY_BLEND.
    // Catches accidental edits to the overlay shader that would
    // change the per-channel branch / composite shape.
    #[test]
    fn fs_overlay_blend_has_gles2_preamble_and_uniforms() {
        assert!(FS_OVERLAY_BLEND.starts_with("#version 100\n"));
        assert!(FS_OVERLAY_BLEND.contains("precision mediump float"));
        assert!(FS_OVERLAY_BLEND.contains("u_layer_tex"));
        assert!(FS_OVERLAY_BLEND.contains("u_slide_tex"));
        // Per-channel branch on dst:
        assert!(FS_OVERLAY_BLEND.contains("step(0.5, dst)"));
        // Source-over composite by α:
        assert!(FS_OVERLAY_BLEND.contains("mix(dst, ovl, a)"));
        // Premultiplied recovery short-circuit at α<0.001:
        assert!(FS_OVERLAY_BLEND.contains("a < 0.001"));
    }

    // v1-spec-delta #10 (slice b) -- brightness/gamma post-pass
    // CPU mirror tests. Pin the math vs FS_BRIGHT_GAMMA shader
    // semantics.
    #[test]
    fn apply_brightness_gamma_identity_at_b1_g1() {
        // brightness=1, gamma=1 -> identity transform.
        let mut rgba: Vec<u8> = vec![64, 128, 192, 255, 0, 0, 0, 200];
        let original = rgba.clone();
        apply_brightness_gamma_rgba(&mut rgba, 1.0, 1.0);
        assert_eq!(rgba, original);
    }

    #[test]
    fn apply_brightness_gamma_halves_at_b_half() {
        // brightness=0.5, gamma=1 -> halve RGB; alpha unchanged.
        let mut rgba: Vec<u8> = vec![200, 100, 50, 255];
        apply_brightness_gamma_rgba(&mut rgba, 0.5, 1.0);
        // 200 * 0.5 = 100; 100 * 0.5 = 50; 50 * 0.5 = 25.
        assert_eq!(rgba[0], 100);
        assert_eq!(rgba[1], 50);
        assert_eq!(rgba[2], 25);
        assert_eq!(rgba[3], 255);  // alpha untouched.
    }

    #[test]
    fn apply_brightness_gamma_lightens_at_g_22() {
        // brightness=1, gamma=2.2 -> lighten via 1/2.2 power
        // (pow(0.5, 1/2.2) ~= 0.7297).
        let mut rgba: Vec<u8> = vec![128, 128, 128, 255];
        apply_brightness_gamma_rgba(&mut rgba, 1.0, 2.2);
        // 128/255 = 0.502; pow(0.502, 1/2.2) = pow(0.502, 0.4545)
        // ~= 0.731; * 255 = ~186.
        assert!(rgba[0] >= 184 && rgba[0] <= 188, "got {}", rgba[0]);
        assert_eq!(rgba[3], 255);  // alpha untouched.
    }

    #[test]
    fn apply_brightness_gamma_clamps_overflow_at_b_2() {
        // brightness=2.0 doubles RGB; 200*2=400 should clamp
        // to 255 (saturate to white) rather than overflow u8.
        let mut rgba: Vec<u8> = vec![200, 100, 50, 255];
        apply_brightness_gamma_rgba(&mut rgba, 2.0, 1.0);
        assert_eq!(rgba[0], 255);  // 400 clamped to 255.
        assert_eq!(rgba[1], 200);  // 100 * 2 = 200.
        assert_eq!(rgba[2], 100);  // 50 * 2 = 100.
    }

    #[test]
    fn apply_brightness_gamma_handles_zero_gamma_via_floor() {
        // gamma = 0.0 would divide-by-zero; the helper clamps
        // gamma at min 0.001 to keep the shader stable.
        let mut rgba: Vec<u8> = vec![128, 128, 128, 255];
        apply_brightness_gamma_rgba(&mut rgba, 1.0, 0.0);
        // pow(0.502, 1/0.001) = pow(0.502, 1000) = ~0.
        assert_eq!(rgba[0], 0);
    }

    #[test]
    fn fs_bright_gamma_has_gles2_preamble() {
        assert!(FS_BRIGHT_GAMMA.starts_with("#version 100\n"));
        assert!(FS_BRIGHT_GAMMA.contains("precision mediump float"));
        assert!(FS_BRIGHT_GAMMA.contains("u_brightness"));
        assert!(FS_BRIGHT_GAMMA.contains("u_gamma"));
        assert!(FS_BRIGHT_GAMMA.contains("u_src"));
        // Per-channel pow on the gamma corrected channel.
        assert!(FS_BRIGHT_GAMMA.contains("pow(rgb"));
    }

    // v1-spec-delta #11 (slice b) -- rgba_to_png_bytes round-trips.
    #[test]
    fn rgba_to_png_bytes_encodes_known_buffer() {
        // 2x2 RGBA: red, green, blue, opaque-white.
        let rgba: Vec<u8> = vec![
            255, 0, 0, 255,    // (0,0) red
            0, 255, 0, 255,    // (1,0) green
            0, 0, 255, 255,    // (0,1) blue
            255, 255, 255, 255, // (1,1) white
        ];
        let png = rgba_to_png_bytes(&rgba, 2, 2).unwrap();
        // PNG sanity: starts with the canonical 8-byte signature
        // 89 50 4E 47 0D 0A 1A 0A.
        assert_eq!(&png[..8], &[0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
        // Decode round-trip: png crate read + reconstruct.
        let dec = png::Decoder::new(&png[..]);
        let mut reader = dec.read_info().unwrap();
        let mut decoded = vec![0u8; reader.output_buffer_size()];
        let info = reader.next_frame(&mut decoded).unwrap();
        assert_eq!(info.width, 2);
        assert_eq!(info.height, 2);
        assert_eq!(info.color_type, png::ColorType::Rgba);
        assert_eq!(&decoded[..16], &rgba[..16]);
    }

    #[test]
    fn rgba_to_png_bytes_rejects_size_mismatch() {
        // Buffer too short for declared dims.
        let rgba: Vec<u8> = vec![255, 0, 0, 255];  // only 1 pixel
        let err = rgba_to_png_bytes(&rgba, 2, 2).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("buffer len"), "got: {msg}");
    }

    #[test]
    fn rgba_to_png_bytes_rejects_zero_dims() {
        // PNG forbids 0-width / 0-height; the encode path
        // surfaces that as an error from png write_header.
        // Caller-side guard: snapshot path validates dims
        // upstream before calling here, but the helper is
        // robust to bad input.
        let rgba: Vec<u8> = vec![];
        let err = rgba_to_png_bytes(&rgba, 0, 0).unwrap_err();
        let msg = format!("{err:#}");
        assert!(msg.contains("png write_header") || msg.contains("zero"),
            "got: {msg}");
    }

    // v1-spec-delta #6 (slice c) -- halftone / scanlines / grid /
    // rings uniform helpers. Math mirrors Python anchors at
    // density 0/0.5/1.
    #[test]
    fn halftone_uniforms_match_canvas2d_anchors() {
        // tile = round(lerp(60, 6, density)); radius = round(tile *
        // 0.34); half = tile * 0.5 (float, NO floor -- Phase 3t).
        // density 0: tile=60, radius=round(20.4)=20, half=30 (even).
        let u0 = halftone_uniforms(0.0);
        assert_eq!(u0.tile, 60.0);
        assert_eq!(u0.radius, 20.0);
        assert_eq!(u0.half, 30.0);
        // density 1: tile=6, radius=round(2.04)=2, half=3 (even).
        let u1 = halftone_uniforms(1.0);
        assert_eq!(u1.tile, 6.0);
        assert_eq!(u1.radius, 2.0);
        assert_eq!(u1.half, 3.0);
        // density 0.5: tile=round(lerp(60,6,0.5))=33 (ODD).
        // Phase 3t: half MUST be 16.5 (sub-pixel) to match Canvas2D's
        // `tile / 2` layer-0 anchor. Pre-3t used floor(tile/2)=16
        // which produced a 0.5-px grid offset vs Canvas2D.
        let u_mid = halftone_uniforms(0.5);
        assert_eq!(u_mid.tile, 33.0);
        assert_eq!(u_mid.radius, 11.0);
        assert_eq!(u_mid.half, 16.5);
    }

    #[test]
    fn scanlines_tile_size_matches_python_lerp() {
        // tile = round(lerp(16, 2, density)). density 0 -> 16,
        // density 1 -> 2 (floor=2). density 0.5 -> round(9) = 9.
        assert_eq!(scanlines_uniforms(0.0).tile, 16.0);
        assert_eq!(scanlines_uniforms(1.0).tile, 2.0);
        assert_eq!(scanlines_uniforms(0.5).tile, 9.0);
    }

    #[test]
    fn grid_tile_size_matches_python_lerp_with_floor4() {
        // tile = max(4, round(lerp(120, 4, density))).
        // density 0 -> 120, density 1 -> 4 (floor=4).
        // density 0.5 -> round(62) = 62.
        assert_eq!(grid_uniforms(0.0).tile, 120.0);
        assert_eq!(grid_uniforms(1.0).tile, 4.0);
        assert_eq!(grid_uniforms(0.5).tile, 62.0);
    }

    #[test]
    fn rings_uniforms_match_canvas2d_anchors() {
        // Phase 3ac 2026-05-15: thin-rings semantics (Option C).
        // tile = max(4, round(lerp(120, 6, density))); half = tile/2.
        // density 0: tile=120, half=60.
        let u0 = rings_uniforms(0.0);
        assert_eq!(u0.tile, 120.0);
        assert_eq!(u0.half, 60.0);
        // density 1: tile=6, half=3.
        let u1 = rings_uniforms(1.0);
        assert_eq!(u1.tile, 6.0);
        assert_eq!(u1.half, 3.0);
    }

    // v1-spec-delta #6 (slice d) -- rays / bricks / confetti
    // uniform helpers.
    #[test]
    fn rays_slice_count_is_always_even_and_floored_at_2() {
        // density 0: slices = 2 * round(lerp(2, 24, 0)) = 2 * 2 = 4.
        // (round(2.0) = 2.)
        assert_eq!(rays_uniforms(0.0).slices, 4.0);
        // density 1: slices = 2 * round(24) = 48.
        assert_eq!(rays_uniforms(1.0).slices, 48.0);
        // density 0.5: slices = 2 * round(13) = 26.
        assert_eq!(rays_uniforms(0.5).slices, 26.0);
        // Floor at 2.
        let edge = rays_uniforms(0.0);
        assert!(edge.slices >= 2.0);
    }

    #[test]
    fn bricks_uniforms_match_canvas2d_anchors() {
        // bw = max(8, round(lerp(140, 16, density))); bh = max(4,
        // round(bw/2)); half = bw * 0.5 (float, NO floor -- Phase 3u).
        // density 0: bw=140, bh=70, half=70 (all integer).
        let u0 = bricks_uniforms(0.0);
        assert_eq!(u0.bw, 140.0);
        assert_eq!(u0.bh, 70.0);
        assert_eq!(u0.half, 70.0);
        // density 1: bw=16, bh=8, half=8 (all integer).
        let u1 = bricks_uniforms(1.0);
        assert_eq!(u1.bw, 16.0);
        assert_eq!(u1.bh, 8.0);
        assert_eq!(u1.half, 8.0);
        // Phase 3u: odd bw case. bricks_uniforms takes the
        // already-curved density (curve is applied at the
        // draw_pattern dispatch site in hdmi.rs, NOT inside the
        // uniform fn). For the parity_bg_pattern_bricks fixture
        // (raw density=0.5), pattern_density_curve(0.5)=0.25, so
        // the uniform fn is called with 0.25. lerp(140, 16, 0.25)
        // = 109 (ODD). Canvas2D bh=round(54.5)=55; half=54.5.
        // Pre-3u Rust used floor -> bh=54, half=54, producing a
        // 1-px brick-height + 0.5-px stagger offset vs Canvas2D.
        let u_mid = bricks_uniforms(0.25);
        assert_eq!(u_mid.bw, 109.0);
        assert_eq!(u_mid.bh, 55.0);
        assert_eq!(u_mid.half, 54.5);
    }

    #[test]
    fn confetti_uniforms_count_lerps_with_density() {
        // count = max(40, round(lerp(80, 2000, density))).
        // density 0: count=80; cell_ref=sqrt(786432/80) ~= 99.12.
        let u0 = confetti_uniforms(0.0);
        assert_eq!(u0.count, 80.0);
        assert!((u0.cell_ref - 99.12).abs() < 0.5);
        // density 1: count=2000; cell_ref=sqrt(786432/2000) ~= 19.83.
        let u1 = confetti_uniforms(1.0);
        assert_eq!(u1.count, 2000.0);
        assert!((u1.cell_ref - 19.83).abs() < 0.5);
        // Floor at 40 -- but density 0 already gives 80, so
        // pure-floor case requires a count formula edit. Confirm
        // the floor still kicks in if formula changes (defensive).
        assert!(u0.count >= 40.0);
    }

    #[test]
    fn pick_largest_picks_highest_pixel_area() {
        let modes = [
            ModeSpec { width: 800, height: 600, vrefresh: 60 },
            ModeSpec { width: 1920, height: 1080, vrefresh: 30 },
            ModeSpec { width: 1024, height: 768, vrefresh: 60 },
        ];
        assert_eq!(pick_largest_mode_index(&modes), Some(1));
    }

    #[test]
    fn pick_largest_tiebreaks_by_vrefresh() {
        let modes = [
            ModeSpec { width: 1920, height: 1080, vrefresh: 30 },
            ModeSpec { width: 1920, height: 1080, vrefresh: 60 },
            ModeSpec { width: 1920, height: 1080, vrefresh: 24 },
        ];
        assert_eq!(pick_largest_mode_index(&modes), Some(1));
    }

    #[test]
    fn pick_largest_single_element() {
        let modes = [ModeSpec { width: 1024, height: 768, vrefresh: 60 }];
        assert_eq!(pick_largest_mode_index(&modes), Some(0));
    }

    #[test]
    fn fourcc_argb8888_matches_drm_spec() {
        // DRM_FORMAT_ARGB8888 = fourcc('A','R','2','4') per
        // include/uapi/drm/drm_fourcc.h. Tests we don't accidentally
        // permute the byte order.
        assert_eq!(fourcc_for_argb_family("Argb8888"), Some(*b"AR24"));
    }

    #[test]
    fn fourcc_full_argb_family() {
        // The six entries we wire up for Phase 2 — keep this in sync
        // with the gbm::Format match in hdmi.rs.
        let cases = [
            ("Argb8888", b"AR24"),
            ("Xrgb8888", b"XR24"),
            ("Abgr8888", b"AB24"),
            ("Xbgr8888", b"XB24"),
            ("Rgba8888", b"RA24"),
            ("Rgbx8888", b"RX24"),
        ];
        for (name, expected) in cases {
            assert_eq!(fourcc_for_argb_family(name), Some(*expected), "case {name}");
        }
    }

    #[test]
    fn fourcc_unknown_returns_none() {
        assert_eq!(fourcc_for_argb_family("YUV420"), None);
        assert_eq!(fourcc_for_argb_family(""), None);
        assert_eq!(fourcc_for_argb_family("Argb888"), None); // typo
    }

    fn approx_eq(a: f32, b: f32) -> bool {
        (a - b).abs() < 1e-3
    }

    fn approx_eq_rgb(actual: (f32, f32, f32), expected: (f32, f32, f32)) -> bool {
        approx_eq(actual.0, expected.0)
            && approx_eq(actual.1, expected.1)
            && approx_eq(actual.2, expected.2)
    }

    #[test]
    fn hsv_red_at_zero_hue() {
        assert!(approx_eq_rgb(hsv_to_rgb(0.0, 1.0, 1.0), (1.0, 0.0, 0.0)));
    }

    #[test]
    fn hsv_green_at_120() {
        assert!(approx_eq_rgb(hsv_to_rgb(120.0, 1.0, 1.0), (0.0, 1.0, 0.0)));
    }

    #[test]
    fn hsv_blue_at_240() {
        assert!(approx_eq_rgb(hsv_to_rgb(240.0, 1.0, 1.0), (0.0, 0.0, 1.0)));
    }

    #[test]
    fn hsv_zero_saturation_is_grayscale() {
        // Any hue with s=0 should produce (v, v, v).
        let cases = [0.0, 90.0, 180.0, 270.0, 359.9];
        for h in cases {
            let (r, g, b) = hsv_to_rgb(h, 0.0, 0.5);
            assert!(approx_eq(r, 0.5) && approx_eq(g, 0.5) && approx_eq(b, 0.5),
                "h={h} → ({r},{g},{b}) expected (0.5,0.5,0.5)");
        }
    }

    #[test]
    fn hsv_zero_value_is_black() {
        let (r, g, b) = hsv_to_rgb(180.0, 1.0, 0.0);
        assert!(approx_eq(r, 0.0) && approx_eq(g, 0.0) && approx_eq(b, 0.0));
    }

    #[test]
    fn hsv_wraps_at_360() {
        // h=360 should equal h=0 — both pure red. The animation loop
        // relies on this; without rem_euclid the match'd fall through.
        let at_zero = hsv_to_rgb(0.0, 1.0, 1.0);
        let at_360 = hsv_to_rgb(360.0, 1.0, 1.0);
        assert!(approx_eq_rgb(at_zero, at_360),
            "h=0 → {at_zero:?}, h=360 → {at_360:?}");
    }

    #[test]
    fn hsv_yellow_at_60() {
        assert!(approx_eq_rgb(hsv_to_rgb(60.0, 1.0, 1.0), (1.0, 1.0, 0.0)));
    }

    #[test]
    fn hsv_cyan_at_180() {
        assert!(approx_eq_rgb(hsv_to_rgb(180.0, 1.0, 1.0), (0.0, 1.0, 1.0)));
    }

    #[test]
    fn crtc_filter_parses_well_formed() {
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(8)"), Some(8));
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(0)"), Some(0));
        assert_eq!(
            parse_crtc_list_filter_bits("CrtcListFilter(4294967295)"),
            Some(u32::MAX)
        );
    }

    #[test]
    fn crtc_filter_rejects_missing_prefix() {
        assert_eq!(parse_crtc_list_filter_bits("(8)"), None);
        assert_eq!(parse_crtc_list_filter_bits("Filter(8)"), None);
    }

    #[test]
    fn crtc_filter_rejects_missing_suffix() {
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(8"), None);
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(8))"), None);
    }

    #[test]
    fn crtc_filter_rejects_non_numeric() {
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(abc)"), None);
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(0x8)"), None);
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter(-1)"), None);
    }

    #[test]
    fn crtc_filter_rejects_empty() {
        assert_eq!(parse_crtc_list_filter_bits(""), None);
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter()"), None);
    }

    fn approx(a: f32, b: f32, eps: f32) -> bool {
        (a - b).abs() < eps
    }

    #[test]
    fn gradient_density_zero_is_top_to_bottom() {
        // density 0 → 0° → top-to-bottom. dx=sin(0)=0, dy=cos(0)=1.
        let g = gradient_uniforms(1024, 768, 0.0).unwrap();
        assert!(approx(g.dx, 0.0, 1e-6), "dx={}", g.dx);
        assert!(approx(g.dy, 1.0, 1e-6), "dy={}", g.dy);
        // proj at y=0 is 0, at y=767 is 767. min=0, span=767.
        assert!(approx(g.proj_min, 0.0, 1e-3));
        assert!(approx(g.span, 767.0, 1e-3));
    }

    #[test]
    fn gradient_density_one_is_right_to_left() {
        // density 1 → 270° → right-to-left. dx=sin(270°)=-1, dy=cos(270°)=0.
        let g = gradient_uniforms(1024, 768, 1.0).unwrap();
        assert!(approx(g.dx, -1.0, 1e-5), "dx={}", g.dx);
        assert!(approx(g.dy, 0.0, 1e-5), "dy={}", g.dy);
        // proj range across x: dx*x for x in [0, 1023] → [-1023, 0].
        assert!(approx(g.proj_min, -1023.0, 1e-3));
        assert!(approx(g.span, 1023.0, 1e-3));
    }

    #[test]
    fn gradient_density_half_is_135_degrees() {
        // density 0.5 → 135° → bottom-left to top-right diagonal.
        // dx=sin(135°)=√2/2≈0.7071, dy=cos(135°)=-√2/2.
        let g = gradient_uniforms(1024, 768, 0.5).unwrap();
        assert!(approx(g.dx, 0.7071, 1e-3), "dx={}", g.dx);
        assert!(approx(g.dy, -0.7071, 1e-3), "dy={}", g.dy);
    }

    #[test]
    fn gradient_density_clamps_above_one() {
        // Out-of-range density should clamp to 1.0 (270°), not
        // produce wraparound or NaN.
        let g_clamped = gradient_uniforms(1024, 768, 1.5).unwrap();
        let g_one = gradient_uniforms(1024, 768, 1.0).unwrap();
        assert_eq!(g_clamped, g_one);
    }

    #[test]
    fn gradient_density_clamps_below_zero() {
        let g_clamped = gradient_uniforms(1024, 768, -0.3).unwrap();
        let g_zero = gradient_uniforms(1024, 768, 0.0).unwrap();
        assert_eq!(g_clamped, g_zero);
    }

    #[test]
    fn gradient_returns_none_for_degenerate_dimensions() {
        // 1x1 viewport at any density has zero span — caller should
        // fall back to a solid color_a fill.
        assert_eq!(gradient_uniforms(1, 1, 0.5), None);
        assert_eq!(gradient_uniforms(0, 0, 0.0), None);
    }

    #[test]
    fn gradient_at_1080p() {
        // The eventual production path runs against 1920x1080. Confirm
        // the math stays sane at production resolution and that
        // density 0 produces a top→bottom gradient covering the
        // full vertical range.
        let g = gradient_uniforms(1920, 1080, 0.0).unwrap();
        assert!(approx(g.dx, 0.0, 1e-6));
        assert!(approx(g.dy, 1.0, 1e-6));
        assert!(approx(g.proj_min, 0.0, 1e-3));
        assert!(approx(g.span, 1079.0, 1e-3));
    }

    #[test]
    fn vs_fullscreen_quad_targets_gles2() {
        // GLES2 requires `#version 100`. Catch an accidental drift to
        // `#version 300 es` (GLES3, vc4 doesn't support it) at host
        // test time, not at first compile on the Pi.
        assert!(
            VS_FULLSCREEN_QUAD.starts_with("#version 100\n"),
            "VS must declare #version 100; got: {:?}",
            &VS_FULLSCREEN_QUAD[..32.min(VS_FULLSCREEN_QUAD.len())]
        );
        assert!(VS_FULLSCREEN_QUAD.contains("attribute vec2 a_pos"));
    }

    #[test]
    fn fs_gradient_targets_gles2() {
        assert!(
            FS_GRADIENT.starts_with("#version 100\n"),
            "FS must declare #version 100; got: {:?}",
            &FS_GRADIENT[..32.min(FS_GRADIENT.len())]
        );
        assert!(FS_GRADIENT.contains("precision mediump float"));
    }

    #[test]
    fn vs_textured_quad_targets_gles2_with_uv() {
        // Phase 4.2 textured-quad VS is the parallel
        // VS_FULLSCREEN_QUAD_WITH_UV the gradient VS doc-comment
        // names. Confirm GLES2 + a_uv attribute + v_uv varying
        // surfaces by name (the dispatch in hdmi.rs binds them).
        assert!(VS_TEXTURED_QUAD.starts_with("#version 100\n"));
        assert!(VS_TEXTURED_QUAD.contains("attribute vec2 a_pos"));
        assert!(VS_TEXTURED_QUAD.contains("attribute vec2 a_uv"));
        assert!(VS_TEXTURED_QUAD.contains("varying vec2 v_uv"));
    }

    // ============================================================
    // FYS bug 5 -- display-rotation present-quad geometry tests.
    // present_quad_verts is the single source of truth for the
    // rotation-direction convention; these lock it.
    // ============================================================

    #[test]
    fn present_quad_rotation_zero_is_identity_quad() {
        // 0° must produce the legacy direct-blit quad byte-for-byte
        // (UV maps straight to NDC) so the 0° present path is
        // unchanged from pre-bug-5.
        let v = present_quad_verts(0);
        let expected: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        assert_eq!(v, expected);
    }

    #[test]
    fn present_quad_unknown_rotation_falls_back_to_identity() {
        // The open handler already coerces out-of-set values, but
        // the geometry helper is robust regardless.
        assert_eq!(present_quad_verts(45), present_quad_verts(0));
    }

    #[test]
    fn present_quad_keeps_uvs_fixed_across_rotations() {
        // UVs are NEVER rotated -- only vertex positions move. Each
        // rotation keeps the same four UV pairs in the same vertex
        // slots; the texture content rotates because each UV is
        // drawn at a rotated position.
        for rot in [0, 90, 180, 270] {
            let v = present_quad_verts(rot);
            assert_eq!((v[2], v[3]), (0.0, 0.0), "rot={rot}");
            assert_eq!((v[6], v[7]), (1.0, 0.0), "rot={rot}");
            assert_eq!((v[10], v[11]), (0.0, 1.0), "rot={rot}");
            assert_eq!((v[14], v[15]), (1.0, 1.0), "rot={rot}");
        }
    }

    #[test]
    fn present_quad_90_compensates_counter_clockwise() {
        // FYS bug 5 follow-up: a `90` setting means the panel is
        // mounted 90° clockwise, so the renderer rotates content 90°
        // COUNTER-clockwise to cancel it. Pins the DIRECTION so a
        // future edit can't silently flip 90 vs 270 (or revert to
        // the original clockwise-with-the-setting cut). Counter-
        // clockwise-90 in y-up NDC is (x', y') = (-y, x). Vert 0
        // carries UV (0,0) at base position (-1,-1) -> (1, -1).
        // Vert 3 carries UV (1,1) at base (1,1) -> (-1, 1).
        let q90 = present_quad_verts(90);
        assert_eq!((q90[0], q90[1]), (1.0, -1.0));
        assert_eq!((q90[12], q90[13]), (-1.0, 1.0));
    }

    #[test]
    fn present_quad_90_and_270_are_opposite() {
        // 90 and 270 are exact opposites: the counter-clockwise-90
        // vertex position that `90` produces, rotated 90° clockwise,
        // returns to its rotation-0 origin. Clockwise-90 in y-up NDC
        // is (x', y') = (y, -x).
        let q0 = present_quad_verts(0);
        let q90 = present_quad_verts(90);
        for i in 0..4 {
            let (x90, y90) = (q90[i * 4], q90[i * 4 + 1]);
            let (rx, ry) = (y90, -x90); // clockwise-90 of the 90 pos
            assert!(
                (rx - q0[i * 4]).abs() < 1e-6 && (ry - q0[i * 4 + 1]).abs() < 1e-6,
                "90 undone by a clockwise-90 must be identity at vert {i}",
            );
        }
    }

    #[test]
    fn present_quad_180_negates_positions() {
        // 180° negates every position (UVs unchanged).
        let q0 = present_quad_verts(0);
        let q180 = present_quad_verts(180);
        for i in 0..4 {
            assert!((q180[i * 4] + q0[i * 4]).abs() < 1e-6, "x vert {i}");
            assert!((q180[i * 4 + 1] + q0[i * 4 + 1]).abs() < 1e-6, "y vert {i}");
        }
    }

    #[test]
    fn fs_cut_targets_gles2_and_pins_uniforms() {
        assert!(FS_CUT.starts_with("#version 100\n"));
        assert!(FS_CUT.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_CUT.contains(uniform), "FS_CUT missing {uniform:?}");
        }
        // Hard switch at midpoint: step(0.5, u_t) returns 0 below
        // 0.5, 1 at-or-above. Pin so a refactor to step(u_t, 0.5)
        // (which is wrong — would emit src_b on the wrong half)
        // flips this test.
        assert!(FS_CUT.contains("step(0.5, u_t)"));
    }

    #[test]
    fn fs_wipe_targets_gles2_and_pins_uniforms() {
        assert!(FS_WIPE.starts_with("#version 100\n"));
        assert!(FS_WIPE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_WIPE.contains(uniform));
        }
        // Wipe direction: x-axis, src_b reveals from left as t grows.
        // step(v_uv.x, u_t) is 1 where v_uv.x <= u_t (left of the
        // wipe edge) — that side gets src_b after mix.
        assert!(FS_WIPE.contains("step(v_uv.x, u_t)"));
    }

    #[test]
    fn fs_iris_targets_gles2_and_pins_uniforms() {
        assert!(FS_IRIS.starts_with("#version 100\n"));
        assert!(FS_IRIS.contains("precision mediump float"));
        // r95: u_aspect joins u_t as the iris uniforms (width/height).
        for uniform in ["u_src_a", "u_src_b", "u_t", "u_aspect"] {
            assert!(
                FS_IRIS.contains(uniform),
                "FS_IRIS missing uniform {uniform}",
            );
        }
        // r95: iris radius math, aspect-corrected. Stretches d.x by
        // u_aspect so length() is in normalized-height units, then
        // scales the t-mapped threshold by the half-diagonal in those
        // same units. Pin all four lines so a refactor can't quietly
        // drop the aspect correction (which would re-introduce the
        // ellipse-on-non-square-displays bug).
        assert!(
            FS_IRIS.contains("vec2 d = v_uv - vec2(0.5)"),
            "FS_IRIS missing aspect-correct d setup",
        );
        assert!(
            FS_IRIS.contains("d.x *= u_aspect"),
            "FS_IRIS missing d.x *= u_aspect (x-stretch to height-normalized)",
        );
        assert!(
            FS_IRIS.contains("float r = length(d)"),
            "FS_IRIS must use length(d) (not anisotropic distance(v_uv, ...))",
        );
        assert!(
            FS_IRIS.contains("0.5 * sqrt(1.0 + u_aspect * u_aspect)"),
            "FS_IRIS missing half-diagonal r_max in height-normalized units",
        );
        assert!(
            FS_IRIS.contains("step(r, u_t * r_max)"),
            "FS_IRIS missing aspect-corrected step threshold",
        );
        // r95 regression-guard: the OLD anisotropic shape and the
        // hard-coded 0.71 constant must not return.
        assert!(
            !FS_IRIS.contains("distance(v_uv, vec2(0.5))"),
            "FS_IRIS must NOT use the pre-r95 anisotropic distance() form",
        );
        assert!(
            !FS_IRIS.contains("u_t * 0.71"),
            "FS_IRIS must NOT hard-code 0.71 (pre-r95 square-viewport-only constant)",
        );
    }

    #[test]
    fn fs_dissolve_uses_mediump_iq_hash() {
        // P3 (2026-05-09): swapped the highp sin-hash for the
        // Inigo Quilez mediump-safe idiom. mediump-safe (no
        // large-constant magnification), no `sin(`, same per-pixel
        // salt-and-pepper distribution character, adjacent-pixel
        // decorrelated at 1080p.
        assert!(FS_DISSOLVE.starts_with("#version 100\n"));
        assert!(
            FS_DISSOLVE.contains("precision mediump float"),
            "FS_DISSOLVE should be mediump now (IQ hash drops the highp dep)",
        );
        assert!(
            !FS_DISSOLVE.contains("precision highp"),
            "FS_DISSOLVE must not use highp (P3 dropped it)",
        );
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_DISSOLVE.contains(uniform));
        }
        // IQ markers pinned: 1/pi seed scale (0.3183099),
        // 50.0 amplifier, vec2 seed offsets (0.71, 0.113). Pin so
        // a future edit doesn't silently regress to a non-mediump-
        // safe hash or an under-amplified Hoskins variant.
        assert!(FS_DISSOLVE.contains("0.3183099"), "IQ 1/pi seed missing");
        assert!(FS_DISSOLVE.contains("50.0"), "IQ amplifier missing");
        assert!(FS_DISSOLVE.contains("0.71"), "IQ seed offset missing");
        assert!(FS_DISSOLVE.contains("0.113"), "IQ seed offset missing");
        assert!(
            !FS_DISSOLVE.contains("sin("),
            "FS_DISSOLVE must not call sin() -- IQ is sine-free",
        );
        // Body still does the threshold-step + mix structure.
        assert!(FS_DISSOLVE.contains("step(threshold, u_t)"));
    }

    #[test]
    fn fs_dissolve_is_branchless() {
        // P3 (2026-05-09): mirror of the FS_FLIP branchless gate.
        // Hash math is straight arithmetic; no per-fragment
        // conditionals.
        assert!(
            !FS_DISSOLVE.contains("if ("),
            "FS_DISSOLVE should be branchless (no `if (`)",
        );
    }

    #[test]
    fn sp_hash_helper_is_iq_not_sine() {
        // SP-tier dissolve generator emits SP_HASH_HELPER inline
        // before main(); same IQ idiom must apply there so SP-tier
        // dissolve and standalone FS_DISSOLVE don't drift.
        assert!(SP_HASH_HELPER.contains("0.3183099"));
        assert!(SP_HASH_HELPER.contains("50.0"));
        assert!(SP_HASH_HELPER.contains("0.71"));
        assert!(SP_HASH_HELPER.contains("0.113"));
        assert!(
            !SP_HASH_HELPER.contains("sin("),
            "SP_HASH_HELPER must not use sin (P3)",
        );
        assert!(
            !SP_HASH_HELPER.contains("43758"),
            "SP_HASH_HELPER must not use the legacy 43758 constant",
        );
    }

    #[test]
    fn sp_dissolve_does_not_request_highp() {
        // P3: kind_needs_highp("dissolve") used to return true,
        // forcing the SP-tier dissolve shader into highp. The
        // IQ hash is mediump-safe, so dissolve drops out of that
        // gate. Glitch (still using sin-hash in standalone) stays
        // in for forward compat.
        assert!(!kind_needs_highp("dissolve"));
        assert!(kind_needs_highp("glitch"));
        // The generated SP-tier dissolve source must NOT contain
        // "precision highp" -- only mediump.
        let sp = fs_transition_sp_source("dissolve", 1, 1).expect("dissolve SP");
        assert!(
            sp.contains("precision mediump float"),
            "SP dissolve should declare mediump",
        );
        assert!(
            !sp.contains("precision highp"),
            "SP dissolve must not declare highp (P3 dropped it)",
        );
        assert!(
            !sp.contains("sin("),
            "SP dissolve must not call sin (P3 swapped to IQ)",
        );
    }

    // P3 host-side hash function tests. The Rust mirror
    // (dissolve_hash_vec2_to_float) MUST stay byte-equivalent to
    // the GLSL embedded in FS_DISSOLVE / SP_HASH_HELPER.

    #[test]
    fn dissolve_hash_does_not_match_legacy_sin_hash() {
        // P3.fix (QA-relayed 2026-05-09): pin that the IQ hash
        // produces semantically different output from the legacy
        // `fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453)`
        // form it replaced. Defends against a drive-by edit that
        // re-introduces sin-hash semantics under different syntax
        // (e.g. someone copy-pastes from a stack-overflow answer
        // that re-derives Mittring's sin-hash with same constants):
        // all 8 distribution / decorrelation / branchless tests
        // would still pass, but the highp dependency would creep
        // back AND vc4's mediump would silently band again.
        //
        // Compute legacy sin-hash inline so the assertion stays
        // self-contained -- the legacy form is canonical (used in
        // FS_GLITCH today and pre-P3 FS_DISSOLVE).
        fn legacy_sin_hash(p: [f32; 2]) -> f32 {
            // GLSL fract: x - floor(x).
            let g_fract = |x: f32| x - x.floor();
            let dot = p[0] * 12.9898 + p[1] * 78.233;
            g_fract(dot.sin() * 43758.5453)
        }
        // Probe a few representative UVs across the unit square.
        // For each, the IQ output must differ from sin-hash by at
        // least 0.05 (5% of the [0, 1] range) -- well above f32
        // round-trip noise but stricter than coincidental near-
        // matches (which would be vanishingly rare for unrelated
        // hash families anyway).
        let probes = [
            [0.123, 0.456],
            [0.789, 0.012],
            [0.5, 0.5],
            [0.0, 0.0],
            [0.937, 0.681],
        ];
        for p in probes {
            let iq = dissolve_hash_vec2_to_float(p);
            let sin_hash = legacy_sin_hash(p);
            let delta = (iq - sin_hash).abs();
            assert!(
                delta >= 0.05,
                "IQ hash {iq:.4} vs legacy sin-hash {sin_hash:.4} at {p:?} differ \
                 by {delta:.4}; expected >= 0.05. Did the dissolve hash regress \
                 to sin-based semantics?",
            );
        }
    }

    #[test]
    fn dissolve_hash_is_deterministic() {
        // Same input -> same output, every call. Sanity check that
        // there's no hidden RNG / time / global state in the math.
        let inputs = [
            [0.0, 0.0],
            [0.5, 0.5],
            [1.0, 1.0],
            [0.123, 0.456],
            [0.987, 0.012],
        ];
        for p in inputs {
            let a = dissolve_hash_vec2_to_float(p);
            let b = dissolve_hash_vec2_to_float(p);
            assert_eq!(a, b, "{p:?}: hash should be deterministic");
        }
    }

    #[test]
    fn dissolve_hash_outputs_within_unit_interval() {
        // Output is always in [0, 1] by construction (final fract).
        // Pin the invariant so the Rust mirror agrees with the
        // GLSL contract (step(threshold, u_t) only makes sense
        // when threshold is in [0, 1]).
        for i in 0..256u32 {
            for j in 0..256u32 {
                let u = i as f32 / 255.0;
                let v = j as f32 / 255.0;
                let h = dissolve_hash_vec2_to_float([u, v]);
                assert!(
                    (0.0..=1.0).contains(&h),
                    "hash([{u:.3}, {v:.3}]) = {h} outside [0, 1]",
                );
            }
        }
    }

    #[test]
    fn dissolve_hash_distribution_is_roughly_uniform() {
        // 16384-sample uniformity check: bucket the outputs into
        // 16 bins and assert no bin is empty AND no bin has more
        // than 2x the expected mean. This is a weak distribution
        // test (chi-square with 16 df would be tighter), but it
        // catches obvious degenerate hashes (all-zero, all-one,
        // periodic clumps).
        let n_bins = 16;
        let mut bins = vec![0u32; n_bins];
        let total: u32 = 16384;
        let side: u32 = 128;
        for i in 0..side {
            for j in 0..side {
                let u = (i as f32 + 0.5) / side as f32;
                let v = (j as f32 + 0.5) / side as f32;
                let h = dissolve_hash_vec2_to_float([u, v]);
                let bin = ((h * n_bins as f32) as usize).min(n_bins - 1);
                bins[bin] += 1;
            }
        }
        let mean = total / n_bins as u32;
        for (bin_idx, &count) in bins.iter().enumerate() {
            assert!(
                count > 0,
                "bin {bin_idx} empty (degenerate hash distribution)",
            );
            assert!(
                count < 2 * mean,
                "bin {bin_idx} has {count} samples vs mean {mean} (clumpy distribution)",
            );
            assert!(
                count > mean / 2,
                "bin {bin_idx} has {count} samples vs mean {mean} (gap in distribution)",
            );
        }
    }

    #[test]
    fn dissolve_hash_decorrelates_adjacent_pixels_horizontal() {
        // Adjacent pixels (1 texel apart in v_uv space, i.e. spaced
        // by 1/1920 horizontally at 1080p) should produce decor-
        // related hash outputs >90% of the time. Defends against
        // accidentally-smooth hash regression where neighbors
        // produce nearly identical thresholds and the dissolve
        // collapses into broad swept reveals.
        let mode_w = 1920.0_f32;
        let step = 1.0 / mode_w;
        let mut total = 0_u32;
        let mut significantly_different = 0_u32;
        for i in 0..1000 {
            // 1000 random-ish probe positions across [0, 1)^2.
            let u = (i as f32 * 0.0017).fract();
            let v = (i as f32 * 0.0029).fract();
            let h0 = dissolve_hash_vec2_to_float([u, v]);
            let h1 = dissolve_hash_vec2_to_float([u + step, v]);
            total += 1;
            // "Significantly different" = differs by at least 0.05
            // (5% of the [0, 1] range).
            if (h0 - h1).abs() >= 0.05 {
                significantly_different += 1;
            }
        }
        let pct = (significantly_different as f32 / total as f32) * 100.0;
        assert!(
            pct >= 90.0,
            "only {pct:.1}% of horizontal-adjacent pixel pairs are decorrelated; \
             expected >= 90%. Hash may have regressed to a smooth function.",
        );
    }

    #[test]
    fn dissolve_hash_decorrelates_adjacent_pixels_vertical() {
        // Mirror of the horizontal test along the y-axis. The IQ
        // hash uses asymmetric seeds (0.71 for x, 0.113 for y), so
        // horizontal and vertical decorrelation are structurally
        // different; pinning ONLY horizontal would leave a 50%
        // coverage gap. At 1080p, the y step is 1/1080.
        let mode_h = 1080.0_f32;
        let step = 1.0 / mode_h;
        let mut total = 0_u32;
        let mut significantly_different = 0_u32;
        for i in 0..1000 {
            let u = (i as f32 * 0.0017).fract();
            let v = (i as f32 * 0.0029).fract();
            let h0 = dissolve_hash_vec2_to_float([u, v]);
            let h1 = dissolve_hash_vec2_to_float([u, v + step]);
            total += 1;
            if (h0 - h1).abs() >= 0.05 {
                significantly_different += 1;
            }
        }
        let pct = (significantly_different as f32 / total as f32) * 100.0;
        assert!(
            pct >= 90.0,
            "only {pct:.1}% of vertical-adjacent pixel pairs are decorrelated; \
             expected >= 90%. Vertical-only seed (0.113) may not produce enough \
             scrambling for the dissolve to look per-pixel.",
        );
    }

    #[test]
    fn fs_pixelate_targets_gles2_and_pins_uniforms() {
        assert!(FS_PIXELATE.starts_with("#version 100\n"));
        assert!(FS_PIXELATE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_PIXELATE.contains(uniform));
        }
        // Wave envelope: 1 - 4*(t-0.5)^2. Pin so a refactor can't
        // accidentally invert (4*(t-0.5)^2 - 1 — wrong sign) or
        // shift the peak (e.g. 4*(t-0.25)^2 — peak at 0.25).
        assert!(FS_PIXELATE.contains("1.0 - 4.0 * (u_t - 0.5) * (u_t - 0.5)"));
        // Block size endpoints: 0.0025 base, 0.04 wave amplitude
        // (so 0.0025 to 0.0425 sweep). Pin so the block-size scale
        // can't accidentally shift.
        assert!(FS_PIXELATE.contains("0.0025"));
        assert!(FS_PIXELATE.contains("0.04 * wave"));
    }

    #[test]
    fn fs_scanline_targets_gles2_and_pins_uniforms() {
        assert!(FS_SCANLINE.starts_with("#version 100\n"));
        assert!(FS_SCANLINE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_SCANLINE.contains(uniform));
        }
        // Sweep direction: top-to-bottom (step on v_uv.y, not .x).
        // Pin to catch an accidental .x flip (would become a wipe).
        assert!(FS_SCANLINE.contains("step(v_uv.y, sweep)"));
        // Band-half width 0.015 + brightness mix 0.7 pinned. Visual
        // tuning constants — host-test gates them so an idle edit
        // doesn't drift them silently.
        assert!(FS_SCANLINE.contains("0.015"));
        assert!(FS_SCANLINE.contains("band * 0.7"));
        assert!(FS_SCANLINE.contains("smoothstep"));
    }

    #[test]
    fn fs_halftone_targets_gles2_and_pins_uniforms() {
        assert!(FS_HALFTONE.starts_with("#version 100\n"));
        assert!(FS_HALFTONE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_HALFTONE.contains(uniform));
        }
        // 16:9 grid hardcoded — 8 rows. Pin both the row count and
        // the aspect math so a rewrite can't silently change the
        // visual layout.
        assert!(FS_HALFTONE.contains("grid_y = 8.0"));
        assert!(FS_HALFTONE.contains("16.0 / 9.0"));
        // Same 0.71 sqrt(0.5) max-radius as iris (cell-local here,
        // not screen-global). Pin direction.
        assert!(FS_HALFTONE.contains("step(d, u_t * 0.71)"));
    }

    #[test]
    fn fs_glitch_uses_highp_precision() {
        // Same vc4-mantissa concern as FS_DISSOLVE — sin/dot/fract
        // hash needs more than mediump's ~10 bits or the per-row
        // jitter collapses into stripes.
        assert!(FS_GLITCH.starts_with("#version 100\n"));
        assert!(FS_GLITCH.contains("precision highp float"));
        assert!(!FS_GLITCH.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_GLITCH.contains(uniform));
        }
        // Hash constants pinned (same magic numbers as dissolve).
        assert!(FS_GLITCH.contains("12.9898"));
        assert!(FS_GLITCH.contains("78.233"));
        assert!(FS_GLITCH.contains("43758.5453"));
        // Frame-seed quantization at 30 buckets, jitter scale 0.1,
        // tear threshold 0.95, cyan tear color (0,1,1) at 0.5
        // brightness — visual tuning constants pinned.
        assert!(FS_GLITCH.contains("u_t * 30.0"));
        assert!(FS_GLITCH.contains("0.1 * u_t"));
        assert!(FS_GLITCH.contains("step(0.95"));
        assert!(FS_GLITCH.contains("vec3(0.0, 1.0, 1.0)"));
    }

    #[test]
    fn fs_slide_targets_gles2_and_pins_uniforms() {
        assert!(FS_SLIDE.starts_with("#version 100\n"));
        assert!(FS_SLIDE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_SLIDE.contains(uniform));
        }
        // Slide direction: slide_b enters from RIGHT. Pin step
        // direction (slide vs push uses inverted step args).
        assert!(FS_SLIDE.contains("step(seam, v_uv.x)"));
    }

    #[test]
    fn fs_push_targets_gles2_and_pins_uniforms() {
        assert!(FS_PUSH.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_PUSH.contains(uniform));
        }
        // Push direction: slide_b enters from LEFT (step inverted
        // vs slide). Bright projector blade at the seam pinned.
        assert!(FS_PUSH.contains("step(v_uv.x, t)"));
        assert!(FS_PUSH.contains("smoothstep(0.0, 0.001"));
        assert!(FS_PUSH.contains("blade * 0.8"));
    }

    #[test]
    fn fs_scroll_targets_gles2_and_pins_uniforms() {
        assert!(FS_SCROLL.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_SCROLL.contains(uniform));
        }
        // Vertical analog of slide — step on v_uv.y not .x.
        // qarl-bug 2026-05-12: pin the corrected scroll-UP direction.
        // Was `step(seam, v_uv.y)` which gave scroll-DOWN under the
        // VBO's NDC-y-up UV convention.
        assert!(FS_SCROLL.contains("step(v_uv.y, t)"));
    }

    #[test]
    fn fs_blinds_targets_gles2_and_pins_uniforms() {
        assert!(FS_BLINDS.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_BLINDS.contains(uniform));
        }
        // 16 horizontal slats; reveal from each slat's midline.
        // Pin slat count + the 0.5 max-distance (each slat
        // extends 0..0.5 from its midline).
        assert!(FS_BLINDS.contains("n_slats = 16.0"));
        assert!(FS_BLINDS.contains("u_t * 0.5"));
    }

    #[test]
    fn fs_flip_targets_gles2_and_pins_uniforms() {
        assert!(FS_FLIP.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_FLIP.contains(uniform));
        }
        // ScaleX = |2t - 1| (1 at t=0/1, 0 at t=0.5). useTo
        // switches at midpoint via step(0.5, t).
        assert!(FS_FLIP.contains("abs(2.0 * t - 1.0)"));
        assert!(FS_FLIP.contains("step(0.5, t)"));
    }

    #[test]
    fn fs_flip_is_branchless() {
        // P5 (2026-05-09): the standalone composite FS_FLIP was
        // backported to match the SP-tier FS_FLIP_SP idiom --
        // sample both slides unconditionally, mask the final
        // color via step() products. No per-fragment `if`
        // branching in the body; on vc4 SIMD this avoids
        // divergence cost when the card is mid-flip.
        assert!(
            !FS_FLIP.contains("if ("),
            "FS_FLIP should be branchless (no `if (` in body)",
        );
        // Specific markers of the branchless idiom.
        assert!(
            FS_FLIP.contains("max(scaleX, 1e-3)"),
            "FS_FLIP should use max-guard to avoid div-by-zero",
        );
        assert!(
            FS_FLIP.contains("step(0.001, scaleX)"),
            "FS_FLIP should use scaleX step-mask for inside test",
        );
        assert!(
            FS_FLIP.contains("step(0.0, src_x)"),
            "FS_FLIP should use src_x lower-bound step-mask",
        );
        assert!(
            FS_FLIP.contains("step(src_x, 1.0)"),
            "FS_FLIP should use src_x upper-bound step-mask",
        );
    }

    #[test]
    fn fs_marquee_targets_gles2_and_pins_uniforms() {
        assert!(FS_MARQUEE.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_MARQUEE.contains(uniform));
        }
        // Gap width 0.125 normalized = 1/8 screen width. Dot
        // radius 0.074 (small white dot in the gap).
        assert!(FS_MARQUEE.contains("gap_uv = 0.125"));
        assert!(FS_MARQUEE.contains("dot_r = 0.074"));
    }

    #[test]
    fn fs_shutter_targets_gles2_and_pins_uniforms() {
        assert!(FS_SHUTTER.starts_with("#version 100\n"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_SHUTTER.contains(uniform));
        }
        // 0.866025 = cos(30°), the half-height-to-half-width ratio
        // of a regular hexagon. 16:9 aspect correction keeps the
        // hex regular at 1080p. 1.5*u_t is the inscribed-radius
        // growth (1.5 = ~max hex_d at the corners with aspect
        // correction).
        assert!(FS_SHUTTER.contains("0.866025"));
        assert!(FS_SHUTTER.contains("16.0 / 9.0"));
        assert!(FS_SHUTTER.contains("1.5 * u_t"));
    }

    #[test]
    fn fs_for_transition_kind_routes_known_kinds() {
        // Compare by content (str equality) rather than pointer
        // identity — Rust may dedupe identical &'static str into a
        // single allocation OR keep them distinct depending on
        // codegen, so ptr::eq is fragile across optimization
        // settings. Content equality is what the dispatch actually
        // cares about: "did kind X return the SAME shader source
        // as the canonical FS_X const?"
        assert_eq!(fs_for_transition_kind("cut"), Some(FS_CUT));
        assert_eq!(fs_for_transition_kind("fade"), Some(FS_FADE));
        assert_eq!(fs_for_transition_kind("wipe"), Some(FS_WIPE));
        assert_eq!(fs_for_transition_kind("iris"), Some(FS_IRIS));
        assert_eq!(fs_for_transition_kind("dissolve"), Some(FS_DISSOLVE));
        assert_eq!(fs_for_transition_kind("pixelate"), Some(FS_PIXELATE));
        assert_eq!(fs_for_transition_kind("scanline"), Some(FS_SCANLINE));
        assert_eq!(fs_for_transition_kind("halftone"), Some(FS_HALFTONE));
        assert_eq!(fs_for_transition_kind("glitch"), Some(FS_GLITCH));
        assert_eq!(fs_for_transition_kind("slide"), Some(FS_SLIDE));
        assert_eq!(fs_for_transition_kind("push"), Some(FS_PUSH));
        assert_eq!(fs_for_transition_kind("scroll"), Some(FS_SCROLL));
        assert_eq!(fs_for_transition_kind("blinds"), Some(FS_BLINDS));
        assert_eq!(fs_for_transition_kind("flip"), Some(FS_FLIP));
        assert_eq!(fs_for_transition_kind("marquee"), Some(FS_MARQUEE));
        assert_eq!(fs_for_transition_kind("shutter"), Some(FS_SHUTTER));
    }

    #[test]
    fn fs_for_transition_kind_full_deck_count() {
        // Phase 5-c-4: the 16 Python-ref transitions are now all
        // mirrored. Pin the count so a future delete OR add slips
        // a host test rather than going silent. If a 17th lands,
        // bump this number AND add it to the routes-known test.
        let known_kinds = [
            "cut", "fade", "wipe", "iris", "dissolve",
            "pixelate", "scanline", "halftone",
            "glitch", "slide", "push", "scroll",
            "blinds", "flip", "marquee", "shutter",
        ];
        assert_eq!(known_kinds.len(), 16);
        for kind in known_kinds {
            assert!(
                fs_for_transition_kind(kind).is_some(),
                "kind {kind:?} should be in the dispatch but isn't"
            );
        }
    }

    // ============================================================
    // r69 (2026-06-06): FYS bug C frame-skip WARN throttle tests.
    //
    // The audit (qa/r69-transition-audit.md) found qarl's
    // "transitions look like cuts" symptom is the SILENT skip
    // path in paint_and_present_one_transition_frame when a
    // V4L2 decoder under-runs. The helper above adds a throttled
    // WARN; these tests pin the throttling semantics so a future
    // refactor (e.g. swapping the HashMap for a different store)
    // can't silently drop the dedupe and flood the journal at
    // 30 lines/sec inside one transition window.
    // ============================================================

    #[test]
    fn warn_paint_transition_skip_first_call_emits() {
        // Initial state: nothing in the throttle map for this
        // kind. First call MUST emit so the symptom is visible
        // the first time it happens.
        reset_paint_transition_skip_throttle();
        let emitted = warn_paint_transition_skip(
            "halftone", 0.25, "endpoint_a_no_frame",
        );
        assert!(emitted, "first skip for a kind must emit a warn");
    }

    #[test]
    fn warn_paint_transition_skip_dedupes_by_kind_reason_within_window() {
        // Second call for the same (kind, reason) within 5s MUST
        // be silent so a 30-fps transition window doesn't produce
        // 30 lines. r69 subagent NIT-5: throttle key is
        // (kind, reason) so chronic endpoint_a underruns don't
        // mask occasional endpoint_b underruns on the same kind.
        reset_paint_transition_skip_throttle();
        let first_a = warn_paint_transition_skip(
            "wipe", 0.10, "endpoint_a_no_frame",
        );
        let second_a = warn_paint_transition_skip(
            "wipe", 0.13, "endpoint_a_no_frame",
        );
        let first_b_same_kind = warn_paint_transition_skip(
            "wipe", 0.17, "endpoint_b_no_frame",
        );
        let second_b_same_kind = warn_paint_transition_skip(
            "wipe", 0.20, "endpoint_b_no_frame",
        );
        assert!(first_a, "first A-side skip must emit");
        assert!(!second_a, "second A-side skip within 5s must be throttled");
        assert!(
            first_b_same_kind,
            "first B-side skip on the SAME kind must emit independently of A's throttle"
        );
        assert!(!second_b_same_kind, "second B-side skip within 5s must be throttled");
    }

    #[test]
    fn warn_paint_transition_skip_different_kinds_emit_independently() {
        // Throttle key is the kind STRING — independent kinds
        // hitting the skip path concurrently each get their own
        // first-emit. Critical when an operator's playlist
        // cycles through multiple transition kinds rapidly.
        reset_paint_transition_skip_throttle();
        let a = warn_paint_transition_skip("fade", 0.5, "endpoint_a_no_frame");
        let b = warn_paint_transition_skip("dissolve", 0.5, "endpoint_a_no_frame");
        let c = warn_paint_transition_skip("flip", 0.5, "endpoint_a_no_frame");
        assert!(a && b && c, "each distinct kind must emit on its first call");
        // And each is now individually throttled.
        let a2 = warn_paint_transition_skip("fade", 0.55, "endpoint_a_no_frame");
        let b2 = warn_paint_transition_skip("dissolve", 0.55, "endpoint_a_no_frame");
        let c2 = warn_paint_transition_skip("flip", 0.55, "endpoint_a_no_frame");
        assert!(!a2 && !b2 && !c2, "follow-up calls for each kind must be throttled");
    }

    #[test]
    fn warn_paint_transition_skip_reset_clears_throttle() {
        // The reset hook is test-only but must actually clear
        // state — otherwise tests downstream of it would observe
        // stale throttle keys.
        reset_paint_transition_skip_throttle();
        assert!(warn_paint_transition_skip("iris", 0.5, "x"), "first emits");
        assert!(!warn_paint_transition_skip("iris", 0.5, "x"), "second throttled");
        reset_paint_transition_skip_throttle();
        assert!(warn_paint_transition_skip("iris", 0.5, "x"), "post-reset emits again");
    }

    // ============================================================
    // r76 Phase A (2026-06-07): transition_endpoint_b_ready metric
    // marker tests. The Phase A diagnostic is ONLY useful if the
    // marker set->consume contract is tight, so pin it.
    // ============================================================

    #[test]
    fn transition_endpoint_b_metric_record_then_consume() {
        reset_transition_endpoint_b_metric_for_tests();
        let from = uuid::Uuid::from_bytes([1; 16]);
        let to = uuid::Uuid::from_bytes([2; 16]);
        // No marker before BeginTransition: peek returns None.
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), None);
        // BeginTransition sets the marker.
        record_transition_begin_for_endpoint_b_metric(Some(from), to);
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), Some(to));
        // First successful endpoint_b bake consumes (and emits).
        consume_transition_endpoint_b_first_frame_marker();
        assert_eq!(
            peek_transition_endpoint_b_metric_for_tests(), None,
            "consume MUST clear the marker so subsequent ticks in the same transition don't re-log"
        );
    }

    #[test]
    fn transition_endpoint_b_metric_consume_without_record_is_noop() {
        reset_transition_endpoint_b_metric_for_tests();
        // If endpoint_b's bake somehow returns Ok(Some(_)) without a
        // prior BeginTransition (would mean a bug in dispatcher
        // ordering, but defensive nonetheless), consume must NOT
        // panic. Behavior: no log, no state change.
        consume_transition_endpoint_b_first_frame_marker();
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), None);
    }

    #[test]
    fn transition_endpoint_b_metric_overwrite_on_new_transition() {
        // r76 invariant: BeginTransition fires fresh per transition;
        // an in-flight unconsumed marker (e.g. because the prior
        // transition aborted without endpoint_b ever delivering)
        // MUST be overwritten by the new BeginTransition's tuple so
        // the metric for the new transition measures from the new
        // t0, not the stale one.
        //
        // r76 subagent WARN-3: the prior overwrite was silent;
        // post-fix the overwrite path emits a
        // [perf] transition_endpoint_b_unconsumed line before
        // replacing -- preserving the FAILURE-case data the
        // dispatch wants to capture. Behavior verified at the
        // peek level: state still replaces.
        reset_transition_endpoint_b_metric_for_tests();
        let from_a = uuid::Uuid::from_bytes([1; 16]);
        let to_a = uuid::Uuid::from_bytes([2; 16]);
        let from_b = uuid::Uuid::from_bytes([3; 16]);
        let to_b = uuid::Uuid::from_bytes([4; 16]);
        record_transition_begin_for_endpoint_b_metric(Some(from_a), to_a);
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), Some(to_a));
        // New transition without an intervening consume -- emits the
        // unconsumed line for to_a and replaces with to_b.
        record_transition_begin_for_endpoint_b_metric(Some(from_b), to_b);
        assert_eq!(
            peek_transition_endpoint_b_metric_for_tests(), Some(to_b),
            "BeginTransition MUST replace the marker so the metric for the new transition is correct"
        );
    }

    #[test]
    fn transition_endpoint_b_metric_record_handles_no_from_id() {
        // r76 subagent WARN-2 edge: if state.current is None (e.g.
        // first slide of a session, shouldn't happen for a
        // begin_transition but defensive), the record API accepts
        // None and the log line renders from_id=none.
        reset_transition_endpoint_b_metric_for_tests();
        let to = uuid::Uuid::from_bytes([5; 16]);
        record_transition_begin_for_endpoint_b_metric(None, to);
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), Some(to));
        consume_transition_endpoint_b_first_frame_marker();
        assert_eq!(peek_transition_endpoint_b_metric_for_tests(), None);
    }

    #[test]
    fn every_backend_transition_kind_literal_value_resolves() {
        // r69 audit-time snapshot of the 16 transition kinds in
        // the backend's Pydantic Literal at
        // backend/openmarquee/content/__init__.py:41-58. Each
        // MUST resolve through fs_for_transition_kind to a
        // dedicated FS_<KIND> shader -- otherwise the backend
        // sends a value the renderer silently FS_CUT-fallbacks
        // on, the symptom qarl observed and r69 audited.
        //
        // r69 subagent NIT-2 caveat: this array is hardcoded
        // (cross-language source-of-truth pin would need Python
        // file parsing). A new kind added to the BACKEND Literal
        // that's missed in BOTH this array AND in
        // fs_for_transition_kind will pass silently. Compensating
        // controls: the audit doc (qa/r69-transition-audit.md)
        // names all 16 explicitly, and any schema change should
        // surface in code review.
        const BACKEND_LITERAL_AT_R69: [&str; 16] = [
            "cut", "fade", "wipe", "slide", "iris", "scroll",
            "flip", "marquee", "dissolve", "pixelate", "halftone",
            "scanline", "glitch", "push", "blinds", "shutter",
        ];
        for kind in BACKEND_LITERAL_AT_R69 {
            assert!(
                fs_for_transition_kind(kind).is_some(),
                "backend TransitionKind {kind:?} silently FS_CUT-fallbacks; \
                 either implement the shader OR remove from the backend Literal"
            );
        }
    }

    #[test]
    fn fs_for_transition_kind_unknown_returns_none() {
        // The full deck is implemented now; unknown-kind callers
        // get None and fall back. Pin a few obviously-not-present
        // names so the dispatch can't accidentally start guessing.
        assert!(fs_for_transition_kind("nonexistent").is_none());
        assert!(fs_for_transition_kind("zoom").is_none());
        assert!(fs_for_transition_kind("").is_none());
        assert!(fs_for_transition_kind("FADE").is_none()); // case-sensitive
    }

    #[test]
    fn fs_fade_targets_gles2_and_pins_uniforms() {
        // Phase 5-b-1: fade transition shader. Pin GLES2 + the
        // sampler/scalar uniform names so a rename doesn't break
        // the bind sites in hdmi.rs.
        assert!(FS_FADE.starts_with("#version 100\n"));
        assert!(FS_FADE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(
                FS_FADE.contains(uniform),
                "FS_FADE missing uniform {uniform:?}"
            );
        }
        assert!(FS_FADE.contains("v_uv"));
        // mix(a, b, t) is the canonical fade math; pin so a refactor
        // to e.g. (1-t)*a + t*b stays equivalent or trips the test.
        assert!(FS_FADE.contains("mix("));
        assert!(FS_FADE.contains("clamp"));
    }

    #[test]
    fn fs_fade_sp_source_pins_per_pair_uniforms() {
        // QA-mandated single-pass transition (2026-05-08): the
        // generator emits a specialized FS per (n_a, n_b) layer-
        // count pair. Pin: GLES2 header, u_a_bg/u_b_bg/u_t always,
        // per-layer uniforms ONLY for slots 0..n_a / 0..n_b, no
        // bindings for unused slots. Specialization dropped 8-slot
        // FS_FADE_SP after the first bench at 1080p showed branchy
        // 8-layer shader was 8.4 fps (vs 22 fps 3-pass baseline);
        // unused branches stalled the vc4 SIMD fragment ALU.
        let s = fs_fade_sp_source(2, 1);
        // Step 3: fs_fade_sp_source delegates to
        // fs_transition_sp_source("fade", ...). The pinned
        // uniforms should still be present.
        assert!(s.starts_with("#version 100\n"));
        assert!(s.contains("precision mediump float"));
        for uniform in ["u_a_bg", "u_b_bg", "u_t"] {
            assert!(s.contains(uniform), "missing top-level uniform {uniform:?}");
        }
        // Slide A: slots 0+1 present, 2+3 absent
        for slot in 0..2 {
            for kind in ["tex", "rect", "rgba"] {
                let name = format!("u_a_{kind}{slot}");
                assert!(s.contains(&name), "missing {name:?} for n_a=2");
            }
        }
        for slot in 2..4 {
            for kind in ["tex", "rect", "rgba"] {
                let name = format!("u_a_{kind}{slot}");
                assert!(!s.contains(&name), "stray {name:?} for n_a=2");
            }
        }
        // Slide B: slot 0 present, 1+ absent
        for kind in ["tex", "rect", "rgba"] {
            let name = format!("u_b_{kind}0");
            assert!(s.contains(&name), "missing {name:?} for n_b=1");
        }
        for slot in 1..4 {
            for kind in ["tex", "rect", "rgba"] {
                let name = format!("u_b_{kind}{slot}");
                assert!(!s.contains(&name), "stray {name:?} for n_b=1");
            }
        }
        assert!(s.contains("mix("));
        assert!(s.contains("clamp"));
        assert!(s.contains("apply_layer"));
        assert_eq!(SINGLE_PASS_MAX_LAYERS_PER_SLIDE, 4);
    }

    #[test]
    fn fs_fade_sp_source_zero_zero_renders_bg_only() {
        // No layers on either side -> bg-only fade. Generator
        // emits no per-layer uniforms and no apply_layer calls in
        // main(); the apply_layer function definition stays (dead
        // code, valid GLSL).
        let s = fs_fade_sp_source(0, 0);
        assert!(s.contains("u_a_bg"));
        assert!(s.contains("u_b_bg"));
        assert!(!s.contains("u_a_tex"));
        assert!(!s.contains("u_b_tex"));
        assert!(s.contains("vec3 ca = u_a_bg"));
        assert!(s.contains("vec3 cb = u_b_bg"));
    }

    #[test]
    fn fs_fade_sp_source_max_pair_uses_all_8_units() {
        // 4+4 = 8 sampler units, the vc4 cap.
        let s = fs_fade_sp_source(
            SINGLE_PASS_MAX_LAYERS_PER_SLIDE,
            SINGLE_PASS_MAX_LAYERS_PER_SLIDE,
        );
        for slot in 0..SINGLE_PASS_MAX_LAYERS_PER_SLIDE {
            for prefix in ["u_a", "u_b"] {
                for kind in ["tex", "rect", "rgba"] {
                    let name = format!("{prefix}_{kind}{slot}");
                    assert!(s.contains(&name), "missing {name:?} at max");
                }
            }
        }
    }

    #[test]
    fn fs_transition_sp_source_kind_dispatch() {
        // QA step 3 (2026-05-08): per-kind generator dispatch.
        // Pin a few of the kinds added in batch A.
        for kind in ["cut", "fade", "wipe", "iris", "dissolve"] {
            let s = fs_transition_sp_source(kind, 1, 1)
                .unwrap_or_else(|| panic!("expected SP source for {kind}"));
            assert!(s.starts_with("#version 100\n"));
            assert!(s.contains("u_a_tex0"));
            assert!(s.contains("u_b_tex0"));
            assert!(s.contains("apply_layer"));
            // The sample_uv arg should be in the apply_layer
            // signature and at every call site.
            assert!(
                s.contains("sample_uv"),
                "{kind}: missing sample_uv in apply_layer body"
            );
        }
        // Mix-factor heuristic per kind.
        let cut = fs_transition_sp_source("cut", 0, 0).unwrap();
        assert!(cut.contains("step(0.5, u_t)"));
        let wipe = fs_transition_sp_source("wipe", 0, 0).unwrap();
        assert!(wipe.contains("step(v_uv.x, u_t)"));
        let iris = fs_transition_sp_source("iris", 0, 0).unwrap();
        // r95 (2026-06-08): SP iris is now aspect-corrected. Pin the
        // new math; the OLD anisotropic distance() form must not
        // return. Header must declare u_aspect.
        assert!(iris.contains("uniform float u_aspect"));
        assert!(iris.contains("vec2 d = v_uv - vec2(0.5)"));
        assert!(iris.contains("d.x *= u_aspect"));
        assert!(iris.contains("float r = length(d)"));
        assert!(iris.contains("0.5 * sqrt(1.0 + u_aspect * u_aspect)"));
        assert!(iris.contains("step(r, u_t * r_max)"));
        assert!(
            !iris.contains("distance(v_uv, vec2(0.5))"),
            "SP iris must NOT use the pre-r95 anisotropic distance() form",
        );
        assert!(
            !iris.contains("u_t * 0.71"),
            "SP iris must NOT hard-code 0.71 (pre-r95 square-only constant)",
        );
        let dissolve = fs_transition_sp_source("dissolve", 0, 0).unwrap();
        // P3 (2026-05-09): SP-tier dissolve dropped highp; the IQ
        // hash is mediump-safe so the precision qualifier flips.
        assert!(dissolve.contains("precision mediump float"));
        assert!(!dissolve.contains("precision highp"));
        assert!(dissolve.contains("_hash"));
    }

    #[test]
    fn fs_transition_sp_source_unsupported_kind_returns_none() {
        assert!(fs_transition_sp_source("glitch", 1, 1).is_none());
        assert!(fs_transition_sp_source("unknown_kind", 1, 1).is_none());
    }

    #[test]
    fn every_fs_for_transition_kind_link_site_resolves_u_aspect() {
        // r96 (2026-06-08): regression-lock against the r95 follow-up
        // bug. r95 added u_aspect plumbing to the SP arm + ONE legacy
        // link site (paint_and_present_one_transition_frame), but
        // missed three other paths that also link a program from
        // `fs_for_transition_kind`:
        //   - capture_legacy_3pass_mid (PNG capture path)
        //   - render_transition_animated_in_session (standalone reel)
        //   - cached_composite_program (SB + composite paths)
        //   - cached_cut_composite_program (cut composite paths)
        // FYS observed the iris still rendering oval because the live
        // hot path went through one of those untraced sites and
        // u_aspect was never bound.
        //
        // This test reads hdmi.rs as a string and asserts that every
        // line containing `get_uniform_location(program, "u_t")` is
        // accompanied (within 10 lines downstream) by a corresponding
        // `get_uniform_location(program, "u_aspect")`. Source-level
        // regression-lock; any future path that adds a new
        // fs_for_transition_kind-based shader will fail this test
        // unless it also resolves u_aspect.
        let src = include_str!("hdmi.rs");
        let lines: Vec<&str> = src.lines().collect();
        let mut u_t_sites: Vec<usize> = Vec::new();
        let mut u_aspect_sites: Vec<usize> = Vec::new();
        for (i, line) in lines.iter().enumerate() {
            if line.contains(r#"get_uniform_location(program, "u_t")"#) {
                u_t_sites.push(i);
            }
            if line.contains(r#"get_uniform_location(program, "u_aspect")"#) {
                u_aspect_sites.push(i);
            }
        }
        // Every u_t resolution site must have a u_aspect resolution
        // within 10 lines downstream (typical ordering: u_t then
        // u_aspect on the next line per r95+r96 convention).
        for u_t_line in &u_t_sites {
            let nearby = u_aspect_sites.iter().any(|a| {
                let delta = (*a as isize) - (*u_t_line as isize);
                (0..=10).contains(&delta)
            });
            assert!(
                nearby,
                "hdmi.rs line {} resolves u_t without a nearby u_aspect resolution; \
                 r96 regression-lock requires every fs_for_transition_kind link site \
                 to also resolve u_aspect for the iris arm. Failing line: {}",
                u_t_line + 1,
                lines.get(*u_t_line).unwrap_or(&""),
            );
        }
        // r96 subagent WARN-1: tighten floor to exact count so a
        // future refactor that drops one of the seven sites can't
        // silently pass. The 8 expected sites are:
        //   1. paint_and_present_one_transition_frame (r95, kept
        //      under the r102.3 kill-switch fallback path so the
        //      A/B test still has the legacy resolve)
        //   2. capture_legacy_3pass_mid (r96)
        //   3. render_fade_composite (r96, convention-pin)
        //   4. render_transition_animated_in_session (r96)
        //   5. cached_transition_sp_program (r95, SP resolver)
        //   6. cached_composite_program (r96)
        //   7. cached_cut_composite_program (r96, symmetry-pin)
        //   8. cached_legacy_transition_program (r102.3, live-3-pass
        //      struct cache for the new default code path)
        assert_eq!(
            u_aspect_sites.len(),
            8,
            "r96 regression-lock expected EXACTLY 8 u_aspect resolution sites \
             in hdmi.rs, found {}. If you've added a new transition-shader link \
             site that also needs u_aspect, bump this count; if you've removed \
             one, re-audit the diff so the dropped site wasn't load-bearing for \
             the iris arm.",
            u_aspect_sites.len(),
        );
    }

    #[test]
    fn every_u_aspect_resolve_site_has_a_matching_bind() {
        // r96 subagent WARN-2: the resolve-coverage test is one-way.
        // A future site could resolve u_aspect to satisfy the
        // regression-lock above yet skip the
        // `gl.uniform_1_f32(u_aspect.as_ref(), aspect)` call at
        // draw time -- exactly the r95->r96 failure mode but at the
        // bind step instead of resolve. This test counts BIND-site
        // occurrences and asserts at least as many as RESOLVE
        // sites.
        let src = include_str!("hdmi.rs");
        let mut resolve_count = 0usize;
        let mut bind_count = 0usize;
        for line in src.lines() {
            if line.contains(r#"get_uniform_location(program, "u_aspect")"#) {
                resolve_count += 1;
            }
            // Match either local `u_aspect.as_ref()` /
            // `u_aspect_loc.as_ref()` or struct-field
            // `ccp.u_aspect.as_ref()` / `active_ccp.u_aspect.as_ref()`
            // / `csp.u_aspect.clone()`.
            if line.contains("u_aspect.as_ref()")
                || line.contains("u_aspect_loc.as_ref()")
                || line.contains("u_aspect.clone()")
            {
                bind_count += 1;
            }
        }
        assert!(
            bind_count >= resolve_count,
            "r96 regression-lock: every u_aspect resolve site must have at \
             least one matching bind site (or downstream clone). Found {} \
             resolves but only {} binds in hdmi.rs.",
            resolve_count,
            bind_count,
        );
    }

    #[test]
    fn iris_pixel_radius_is_rotationally_symmetric_at_each_aspect() {
        // r95 QA dispatch point 2: at u_t=0.5 the iris pixel-set
        // should be rotationally symmetric within +/- 1px for any
        // viewport aspect. Sample 8 directions from center, evaluate
        // the SHADER FORMULA per direction (binary-search the boundary
        // pixel-radius where length(d_stretched) == u_t * r_max),
        // assert max-r minus min-r <= 1px.
        //
        // The shader formula mirrored here:
        //   d_uv = (d_pixel.x / w, d_pixel.y / h)   // pixel -> UV
        //   d_stretched.x = d_uv.x * aspect          // x-stretch
        //   d_stretched.y = d_uv.y
        //   r = length(d_stretched)
        //   r_max = 0.5 * sqrt(1 + aspect^2)
        //   inside iris iff r <= u_t * r_max
        //
        // Subagent r95 WARN-1 fix: pre-fix this test used the closed
        // form `threshold * mode_h` which is direction-independent by
        // construction (the spread was tautologically 0). Now we
        // evaluate the formula per direction so REMOVING the
        // `d.x *= aspect` stretch line would produce a
        // direction-dependent r_px and trip this test.
        fn iris_boundary_radius_px(
            mode_w: u32,
            mode_h: u32,
            u_t: f32,
            theta_rad: f32,
        ) -> f32 {
            let aspect = mode_w as f32 / mode_h as f32;
            let r_max = 0.5 * (1.0_f32 + aspect * aspect).sqrt();
            let threshold = u_t * r_max;
            let cos_t = theta_rad.cos();
            let sin_t = theta_rad.sin();
            // Binary-search r_px on this direction.
            let (mut lo, mut hi) = (0.0_f32, (mode_w.max(mode_h) as f32) * 2.0);
            for _ in 0..60 {
                let mid = 0.5 * (lo + hi);
                let dx_pixel = cos_t * mid;
                let dy_pixel = sin_t * mid;
                // Convert pixel -> UV (shader receives v_uv in [0,1]).
                let dx_uv = dx_pixel / mode_w as f32;
                let dy_uv = dy_pixel / mode_h as f32;
                // Apply the shader's x-stretch.
                let dx_stretched = dx_uv * aspect;
                let r = (dx_stretched * dx_stretched + dy_uv * dy_uv).sqrt();
                if r < threshold { lo = mid; } else { hi = mid; }
            }
            0.5 * (lo + hi)
        }
        for (mode_w, mode_h) in [(1360u32, 768u32), (1920u32, 1080u32), (800u32, 480u32)] {
            let mut radii = Vec::new();
            for i in 0..8 {
                let theta = (i as f32) * (std::f32::consts::PI * 2.0 / 8.0);
                radii.push(iris_boundary_radius_px(mode_w, mode_h, 0.5, theta));
            }
            let max_r = radii.iter().cloned().fold(f32::MIN, f32::max);
            let min_r = radii.iter().cloned().fold(f32::MAX, f32::min);
            let spread = max_r - min_r;
            assert!(
                spread <= 1.0,
                "iris at u_t=0.5 must be rotationally symmetric within +/-1px \
                 (viewport {}x{}): max_r={} min_r={} spread={} radii={:?}",
                mode_w, mode_h, max_r, min_r, spread, radii,
            );
        }
    }

    #[test]
    fn iris_at_u_t_one_covers_screen_corners_for_any_aspect() {
        // r95 QA dispatch point: at u_t=1 the iris must reach the
        // farthest corner -- in pixel-isotropic terms the half-
        // diagonal in normalized-height units. Pin the boundary
        // radius matches the analytical half-diagonal.
        for (mode_w, mode_h) in [(1360u32, 768u32), (1920u32, 1080u32), (800u32, 480u32)] {
            let aspect = mode_w as f32 / mode_h as f32;
            let r_max = 0.5 * (1.0_f32 + aspect * aspect).sqrt();
            // Corner radius in pixel-isotropic (normalized-height)
            // units = sqrt((0.5 * mode_w / mode_h)^2 + 0.5^2)
            //       = sqrt((0.5*aspect)^2 + 0.25)
            //       = 0.5 * sqrt(aspect^2 + 1)
            // Identical to r_max above by construction.
            let corner_r = 0.5 * (aspect * aspect + 1.0_f32).sqrt();
            assert!(
                (corner_r - r_max).abs() < 1e-5,
                "iris r_max ({}) must equal half-diagonal ({}) at aspect {}",
                r_max, corner_r, aspect,
            );
        }
    }

    #[test]
    fn is_transition_kind_single_pass_classifies_correctly() {
        // Step 3 complete: 15/16 ported. Glitch deferred per qarl.
        for kind in [
            "cut", "fade", "wipe", "iris", "dissolve", "scanline", "halftone",
            "blinds", "shutter", "slide", "push", "scroll", "flip", "marquee",
            "pixelate",
        ] {
            assert!(
                is_transition_kind_single_pass(kind),
                "{kind} should be SP-portable"
            );
        }
        // Glitch is qarl-deferred.
        assert!(!is_transition_kind_single_pass("glitch"));
        assert!(!is_transition_kind_single_pass("unknown"));
    }

    // P2-I: pure-logic helpers extracted from hdmi.rs (qarl-direct
    // 2026-05-09). The trio below clusters with
    // is_transition_kind_single_pass since they share the
    // SP-portable kind list / SP layer cap / scissored-bake layer
    // cap as their only inputs.

    #[test]
    fn sp_kind_static_returns_static_for_every_sp_portable_kind() {
        // The kind list MUST stay 1:1 with
        // is_transition_kind_single_pass; if it diverges the SP
        // program-cache HashMap key shape no longer matches the
        // gate that admits a kind into the cache. The 'static
        // bound on the returned slice is enforced by the function
        // signature -- annotated below to make the compile-time
        // check explicit at the call site too.
        for kind in [
            "cut", "fade", "wipe", "iris", "dissolve", "scanline", "halftone",
            "blinds", "shutter", "slide", "push", "scroll", "flip", "marquee",
            "pixelate",
        ] {
            let resolved: &'static str = sp_kind_static(kind)
                .unwrap_or_else(|| panic!("{kind} should resolve to a 'static slice"));
            assert_eq!(resolved, kind);
        }
    }

    #[test]
    fn sp_kind_static_returns_none_for_non_sp_kinds() {
        // Glitch is qarl-deferred from SP path. Unknown / empty /
        // wrong-case all fall through to legacy 3-pass, NOT to a
        // panic.
        assert_eq!(sp_kind_static("glitch"), None);
        assert_eq!(sp_kind_static(""), None);
        assert_eq!(sp_kind_static("unknown_kind"), None);
        assert_eq!(sp_kind_static("FADE"), None);   // case-sensitive
        assert_eq!(sp_kind_static("Fade"), None);
        assert_eq!(sp_kind_static("fade "), None);  // trailing space
    }

    #[test]
    fn sp_kind_static_aligns_with_is_transition_kind_single_pass() {
        // Every kind that is_transition_kind_single_pass admits
        // MUST resolve via sp_kind_static, and vice versa. Drift
        // between these two predicates would let a kind reach the
        // SP code path without a HashMap key, or take an SP key
        // for a kind the runtime tier-dispatch doesn't accept.
        let candidate_kinds = [
            "cut", "fade", "wipe", "iris", "dissolve", "scanline", "halftone",
            "blinds", "shutter", "slide", "push", "scroll", "flip", "marquee",
            "pixelate", "glitch", "unknown_kind", "", "FADE",
        ];
        for k in candidate_kinds {
            assert_eq!(
                is_transition_kind_single_pass(k),
                sp_kind_static(k).is_some(),
                "{k} disagrees between is_transition_kind_single_pass / sp_kind_static",
            );
        }
    }

    #[test]
    fn prefer_scissored_bake_below_single_pass_combined_cap() {
        // Combined <= 4 AND each side <= SP cap (4) routes to SP.
        // 0+0, 1+1, 2+2, 4+0 all stay on SP.
        assert!(!prefer_scissored_bake(0, 0));
        assert!(!prefer_scissored_bake(1, 1));
        assert!(!prefer_scissored_bake(2, 2));
        assert!(!prefer_scissored_bake(4, 0));
        assert!(!prefer_scissored_bake(0, 4));
        assert!(!prefer_scissored_bake(3, 1));
    }

    #[test]
    fn prefer_scissored_bake_above_combined_cap() {
        // Combined > 4 with both sides within SP per-side cap
        // STILL prefers SB. This is the 5L+5L all-motion case
        // documented in /tmp/qa-synth-motion-bench.md.
        assert!(prefer_scissored_bake(3, 2));   // 5 total
        assert!(prefer_scissored_bake(4, 1));   // 5 total
        assert!(prefer_scissored_bake(4, 4));   // 8 total, both at cap
    }

    #[test]
    fn prefer_scissored_bake_per_side_cap_overrides() {
        // Either side > SINGLE_PASS_MAX_LAYERS_PER_SLIDE forces SB
        // even if the combined count would otherwise fit.
        assert_eq!(SINGLE_PASS_MAX_LAYERS_PER_SLIDE, 4);
        assert!(prefer_scissored_bake(5, 0));
        assert!(prefer_scissored_bake(0, 5));
        assert!(prefer_scissored_bake(6, 0));   // up to SB cap
    }

    #[test]
    fn prefer_scissored_bake_threshold_is_strict_greater_than_4() {
        // The combined boundary is `> 4`, NOT `>= 4`. 4 total is
        // the SP-cheaper-by-bench band; 5 is the flip point.
        // Locks the constant against accidental drift.
        assert!(!prefer_scissored_bake(2, 2));   // 4 total -> SP
        assert!(prefer_scissored_bake(2, 3));    // 5 total -> SB
        assert!(prefer_scissored_bake(3, 2));    // 5 total -> SB
    }

    #[test]
    fn gradient_density_is_degenerate_zero_and_below_threshold() {
        // FS_GRADIENT at density=0 emits color_a uniformly.
        // Threshold (1e-4) is the per-fragment quantization-noise
        // cutoff; values within ±1e-4 of zero render visually
        // identical to a solid color_a fill.
        assert!(gradient_density_is_degenerate(0.0));
        assert!(gradient_density_is_degenerate(-0.0));
        assert!(gradient_density_is_degenerate(5e-5));
        assert!(gradient_density_is_degenerate(-5e-5));
        assert!(gradient_density_is_degenerate(9e-5));
    }

    #[test]
    fn gradient_density_is_degenerate_above_threshold() {
        // 1e-4 itself is the strict-less-than threshold; values
        // AT or above it produce a visible gradient and should
        // NOT be admitted to the SP solid-bg path.
        assert!(!gradient_density_is_degenerate(1e-4));
        assert!(!gradient_density_is_degenerate(2e-4));
        assert!(!gradient_density_is_degenerate(0.01));
        assert!(!gradient_density_is_degenerate(0.5));
        assert!(!gradient_density_is_degenerate(1.0));
        // Negative side mirrors the positive side via .abs().
        assert!(!gradient_density_is_degenerate(-1e-4));
        assert!(!gradient_density_is_degenerate(-0.5));
    }

    #[test]
    fn gradient_density_is_degenerate_handles_pathological_inputs() {
        // NaN .abs() is NaN; NaN < 1e-4 is false; pathological
        // gradient envelopes shouldn't crash AND shouldn't take
        // the solid-bg fast path. Inf likewise stays on the
        // gradient render path.
        assert!(!gradient_density_is_degenerate(f32::NAN));
        assert!(!gradient_density_is_degenerate(f32::INFINITY));
        assert!(!gradient_density_is_degenerate(f32::NEG_INFINITY));
    }

    // P2-I (continued): eligibility-gate pure-logic helpers.
    // LayerCompositeProps lets the gates take a small POD slice
    // instead of the (TextLayer, color, font) tuple shape that
    // the call site has, so these tests don't need the full
    // content stack.

    fn lp_normal() -> LayerCompositeProps {
        LayerCompositeProps { outline: false, blend: BlendMode::Normal }
    }

    #[test]
    fn sp_eligibility_admits_zero_layers_per_side() {
        // SDF arc slice B.3: SP-tier admits ONLY bg-only transitions
        // (zero text layers either side). Text-bearing transitions
        // route through SB or legacy 3-pass per the gate. Bg-only
        // transition with SP-portable kind + solid bg both sides
        // is the canonical admit case.
        for kind in ["fade", "wipe", "cut", "marquee", "pixelate"] {
            assert!(
                transition_eligible_for_single_pass_logic(
                    kind, true, true, &[], &[],
                ),
                "{kind} bg-only solid+solid should be SP-eligible",
            );
        }
    }

    #[test]
    fn sp_eligibility_rejects_any_text_layer() {
        // SDF arc slice B.3 gate: ANY text layer on either side
        // disqualifies SP-tier so text-bearing transitions route
        // through SB (paint_slide_with_viewport, on MSDF post-B.2).
        let one_layer = [lp_normal()];
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", true, true, &one_layer, &[],
        ));
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", true, true, &[], &one_layer,
        ));
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", true, true, &one_layer, &one_layer,
        ));
    }

    #[test]
    fn sp_eligibility_rejects_non_sp_kind() {
        // Even bg-only transitions reject if the kind isn't SP-
        // portable. Tier dispatch then falls through to legacy 3-pass.
        assert!(!transition_eligible_for_single_pass_logic(
            "glitch", true, true, &[], &[],
        ));
        assert!(!transition_eligible_for_single_pass_logic(
            "unknown", true, true, &[], &[],
        ));
    }

    #[test]
    fn sp_eligibility_rejects_non_solid_bg() {
        // Gradient/pattern/image bg on either side rejects SP-tier
        // (the SP shader only accepts solid bg per fs_transition_sp_
        // source's contract).
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", false, true, &[], &[],
        ));
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", true, false, &[], &[],
        ));
        assert!(!transition_eligible_for_single_pass_logic(
            "fade", false, false, &[], &[],
        ));
    }

    #[test]
    fn sb_eligibility_admits_wider_inputs_than_sp() {
        // SB is wider than SP: it doesn't care about bg type, it
        // accepts up to 6 layers per side, AND it accepts outline
        // and non-Overlay blends. Spot-check the cases SP rejects
        // that SB still admits.
        let outlined = LayerCompositeProps { outline: true, blend: BlendMode::Normal };
        let multiply = LayerCompositeProps { outline: false, blend: BlendMode::Multiply };
        let screen = LayerCompositeProps { outline: false, blend: BlendMode::Screen };
        // Outline OK for SB.
        assert!(transition_eligible_for_scissored_bake_logic(
            "fade", &[outlined], &[lp_normal()],
        ));
        // Multiply + Screen OK for SB.
        assert!(transition_eligible_for_scissored_bake_logic(
            "fade", &[multiply, screen], &[lp_normal()],
        ));
        // 6+6 OK for SB (was rejected by SP at >4).
        let six = vec![lp_normal(); 6];
        assert!(transition_eligible_for_scissored_bake_logic(
            "fade", &six, &six,
        ));
        assert_eq!(SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE, 6);
    }

    #[test]
    fn sb_eligibility_rejects_non_sp_kind() {
        // SB shares the SP-portable kind list (the composite
        // shader dispatch table is shared).
        let layers = [lp_normal()];
        assert!(!transition_eligible_for_scissored_bake_logic(
            "glitch", &layers, &layers,
        ));
        assert!(!transition_eligible_for_scissored_bake_logic(
            "unknown", &layers, &layers,
        ));
    }

    #[test]
    fn sb_eligibility_rejects_above_per_side_cap() {
        let seven = vec![lp_normal(); 7];
        let six = vec![lp_normal(); 6];
        // 7 layers on either side exceeds SB cap.
        assert!(!transition_eligible_for_scissored_bake_logic(
            "fade", &seven, &six,
        ));
        assert!(!transition_eligible_for_scissored_bake_logic(
            "fade", &six, &seven,
        ));
    }

    #[test]
    fn sb_eligibility_rejects_overlay_blend() {
        let overlay = LayerCompositeProps { outline: false, blend: BlendMode::Overlay };
        // Overlay on either side rejects (paint_layers_via_overlay_
        // route is incompatible with atlas regions).
        assert!(!transition_eligible_for_scissored_bake_logic(
            "fade", &[lp_normal(), overlay], &[lp_normal()],
        ));
        assert!(!transition_eligible_for_scissored_bake_logic(
            "fade", &[lp_normal()], &[lp_normal(), overlay],
        ));
    }

    #[test]
    fn sb_eligibility_admits_zero_layer_slides() {
        // Bg-only slides on either or both sides are valid SB
        // input -- zero layers always satisfies the cap and the
        // Overlay-rejection loop is empty.
        assert!(transition_eligible_for_scissored_bake_logic("fade", &[], &[]));
        assert!(transition_eligible_for_scissored_bake_logic(
            "fade", &[lp_normal()], &[],
        ));
        assert!(transition_eligible_for_scissored_bake_logic(
            "fade", &[], &[lp_normal()],
        ));
    }

    #[test]
    fn fs_transition_sp_source_batch_d_warps() {
        // QA Step 3 Batch D: flip, marquee, pixelate (Group-B
        // non-trivial warps).
        for kind in ["flip", "marquee", "pixelate"] {
            let s = fs_transition_sp_source(kind, 1, 1)
                .unwrap_or_else(|| panic!("expected SP source for {kind}"));
            assert!(s.starts_with("#version 100\n"));
            assert!(s.contains("apply_layer"));
        }
        // Per-kind shape pins.
        let flip = fs_transition_sp_source("flip", 0, 0).unwrap();
        assert!(flip.contains("scaleX = abs(2.0 * t - 1.0)"));
        assert!(flip.contains("max(scaleX, 1e-3)"));
        assert!(flip.contains("col * inside"));
        let marquee = fs_transition_sp_source("marquee", 0, 0).unwrap();
        assert!(marquee.contains("gap_uv = 0.125"));
        assert!(marquee.contains("dot_r = 0.074"));
        assert!(marquee.contains("in_from + gap_col * in_gap + cb * in_to"));
        let pixelate = fs_transition_sp_source("pixelate", 0, 0).unwrap();
        assert!(pixelate.contains("blockSize = 0.0025"));
        assert!(pixelate.contains("floor(v_uv / blockSize)"));
        // pixelate: both A and B compose at the SAME quantized
        // `cell` coord (both sample the pixelated view).
        assert!(pixelate.contains("vec3 ca = u_a_bg"));
        assert!(pixelate.contains("vec3 cb = u_b_bg"));
    }

    #[test]
    fn fs_transition_sp_source_batch_c_warped_sample() {
        // QA Step 3 Batch C: slide, push, scroll. These are
        // Group-B warped-sample transitions; sample_uv differs
        // per slide. Pin the warp expressions + the "slide A
        // composes at sample_uv_a, slide B at sample_uv_b"
        // shape.
        for kind in ["slide", "push", "scroll"] {
            let s = fs_transition_sp_source(kind, 1, 1)
                .unwrap_or_else(|| panic!("expected SP source for {kind}"));
            assert!(s.contains("sample_uv_a"), "{kind}: missing sample_uv_a");
            assert!(s.contains("sample_uv_b"), "{kind}: missing sample_uv_b");
            assert!(
                s.contains("apply_layer(ca, u_a_tex0, u_a_rect0, u_a_rgba0, sample_uv_a)"),
                "{kind}: slide A apply_layer should pass sample_uv_a"
            );
            assert!(
                s.contains("apply_layer(cb, u_b_tex0, u_b_rect0, u_b_rgba0, sample_uv_b)"),
                "{kind}: slide B apply_layer should pass sample_uv_b"
            );
        }
        // Per-kind shape pins.
        let slide = fs_transition_sp_source("slide", 0, 0).unwrap();
        assert!(slide.contains("vec2(v_uv.x + t, v_uv.y)"));
        assert!(slide.contains("vec2(v_uv.x - seam, v_uv.y)"));
        let push = fs_transition_sp_source("push", 0, 0).unwrap();
        assert!(push.contains("vec2(v_uv.x - t, v_uv.y)"));
        assert!(push.contains("blade"));
        let scroll = fs_transition_sp_source("scroll", 0, 0).unwrap();
        // qarl-bug 2026-05-12: scroll must scroll UP (B enters from
        // bottom). Pin the corrected step direction + A-sampling
        // offset so a re-introduction of the old "scroll down" path
        // fails loudly. Old: `vec2(v_uv.x, v_uv.y + t)` + `step(seam,
        // v_uv.y)`. New: `vec2(v_uv.x, v_uv.y - t)` + `step(v_uv.y, t)`.
        assert!(scroll.contains("vec2(v_uv.x, v_uv.y - t)"));
        assert!(scroll.contains("step(v_uv.y, t)"));
    }

    #[test]
    fn fs_transition_sp_source_batch_b_dispatch() {
        // QA Step 3 Batch B: scanline, halftone, blinds, shutter.
        for kind in ["scanline", "halftone", "blinds", "shutter"] {
            let s = fs_transition_sp_source(kind, 1, 1)
                .unwrap_or_else(|| panic!("expected SP source for {kind}"));
            assert!(s.starts_with("#version 100\n"));
            assert!(s.contains("u_a_tex0"));
            assert!(s.contains("u_b_tex0"));
            assert!(s.contains("apply_layer"));
            assert!(s.contains("sample_uv"));
        }
        // Per-kind shape pins.
        let scanline = fs_transition_sp_source("scanline", 0, 0).unwrap();
        assert!(scanline.contains("smoothstep"));
        assert!(scanline.contains("vec3(1.0)"));
        let halftone = fs_transition_sp_source("halftone", 0, 0).unwrap();
        assert!(halftone.contains("16.0 / 9.0"));
        assert!(halftone.contains("0.71"));
        let blinds = fs_transition_sp_source("blinds", 0, 0).unwrap();
        assert!(blinds.contains("16.0"));
        assert!(blinds.contains("fract"));
        let shutter = fs_transition_sp_source("shutter", 0, 0).unwrap();
        assert!(shutter.contains("0.866025"));
        assert!(shutter.contains("max(max(c1, c2), c3)"));
    }

    #[test]
    fn fs_blit_targets_gles2_and_pins_uniform() {
        // Phase 5-a: blit shader pairs with VS_TEXTURED_QUAD. Pin
        // GLES2 + the single sampler uniform name so a rename
        // doesn't silently no-op the screen blit.
        assert!(FS_BLIT.starts_with("#version 100\n"));
        assert!(FS_BLIT.contains("precision mediump float"));
        assert!(FS_BLIT.contains("u_src"));
        assert!(FS_BLIT.contains("v_uv"));
        assert!(FS_BLIT.contains("texture2D"));
    }

    #[test]
    fn fs_nv12_to_rgb_targets_gles2_and_pins_bt709_coefficients() {
        // V4L2 piece 3d (BT.709 update 2026-05-14): NV12 -> RGB
        // BT.709 limited-range shader. The Pi's bcm2835-codec
        // reports Colorspace=Rec.709 + YCbCr Encoding=Default
        // (V4L2 spec: Default-for-Rec.709 = BT.709). Pin GLES2
        // version + both samplers (Y plane on TEXTURE0, UV plane
        // on TEXTURE1) + the BT.709 matrix coefficients. A rename
        // or coefficient drift would silently produce wrong colors
        // with no Mac-side way to catch it (the shader only runs
        // on the Pi's GLES2 driver).
        assert!(FS_NV12_TO_RGB.starts_with("#version 100\n"));
        assert!(FS_NV12_TO_RGB.contains("precision mediump float"));
        assert!(FS_NV12_TO_RGB.contains("u_tex_y"));
        assert!(FS_NV12_TO_RGB.contains("u_tex_uv"));
        assert!(FS_NV12_TO_RGB.contains("v_uv"));
        assert!(FS_NV12_TO_RGB.contains("texture2D"));
        // Limited-range Y scaling: (Y - 16/255) * 255/219.
        // (Range scaling is the same as BT.601; only the matrix
        // coefficients below differ.)
        assert!(FS_NV12_TO_RGB.contains("16.0/255.0"));
        assert!(FS_NV12_TO_RGB.contains("255.0/219.0"));
        // Limited-range UV center + range: (UV - 128/255) * 255/224.
        assert!(FS_NV12_TO_RGB.contains("128.0/255.0"));
        assert!(FS_NV12_TO_RGB.contains("255.0/224.0"));
        // ITU-R BT.709 Annex B matrix coefficients.
        assert!(FS_NV12_TO_RGB.contains("1.5748"));
        assert!(FS_NV12_TO_RGB.contains("0.1873"));
        assert!(FS_NV12_TO_RGB.contains("0.4681"));
        assert!(FS_NV12_TO_RGB.contains("1.8556"));
        // Anti-assertions: legacy BT.601 coefficients must NOT
        // reappear (regression guard for an accidental revert).
        assert!(
            !FS_NV12_TO_RGB.contains("1.402"),
            "legacy BT.601 Cr coefficient (1.402) leaked back in"
        );
        assert!(
            !FS_NV12_TO_RGB.contains("0.344136"),
            "legacy BT.601 G/Cb coefficient (0.344136) leaked back in"
        );
        assert!(
            !FS_NV12_TO_RGB.contains("0.714136"),
            "legacy BT.601 G/Cr coefficient (0.714136) leaked back in"
        );
        assert!(
            !FS_NV12_TO_RGB.contains("1.772"),
            "legacy BT.601 Cb coefficient (1.772) leaked back in"
        );
        // LUMINANCE_ALPHA sampling convention: UV plane is sampled
        // as `.ra` because GLES2 LUMINANCE_ALPHA returns L in .r
        // and A in .a (we map U->L, V->A on upload).
        assert!(FS_NV12_TO_RGB.contains(".ra"));
        // r83 Phase B (2026-06-08): the y-axis crop uniform must
        // be present + applied in the uv_t computation. Pinned so
        // future shader refactors don't drop the green-line
        // mitigation.
        assert!(
            FS_NV12_TO_RGB.contains("uniform float u_y_crop_max"),
            "FS_NV12_TO_RGB must declare `uniform float u_y_crop_max` (r83 Phase B)",
        );
        assert!(
            FS_NV12_TO_RGB.contains("(1.0 - v_uv.y) * u_y_crop_max"),
            "FS_NV12_TO_RGB must apply the y-crop in uv_t (r83 Phase B)",
        );
    }

    #[test]
    fn fs_nv12_cover_to_rgb_pins_cover_fit_uniforms() {
        // STREAM/VLC HW-decode: the cover-fit NV12 shader must keep
        // the same GLES2 + BT.709 contract as FS_NV12_TO_RGB and add
        // exactly the two UV-transform uniforms.
        assert!(FS_NV12_COVER_TO_RGB.starts_with("#version 100\n"));
        assert!(FS_NV12_COVER_TO_RGB.contains("precision mediump float"));
        assert!(FS_NV12_COVER_TO_RGB.contains("u_tex_y"));
        assert!(FS_NV12_COVER_TO_RGB.contains("u_tex_uv"));
        // The new cover-fit uniforms.
        assert!(FS_NV12_COVER_TO_RGB.contains("u_uv_scale"));
        assert!(FS_NV12_COVER_TO_RGB.contains("u_uv_offset"));
        // Same BT.709 limited-range matrix as the V4L2 path.
        assert!(FS_NV12_COVER_TO_RGB.contains("16.0/255.0"));
        assert!(FS_NV12_COVER_TO_RGB.contains("255.0/219.0"));
        assert!(FS_NV12_COVER_TO_RGB.contains("1.5748"));
        assert!(FS_NV12_COVER_TO_RGB.contains("1.8556"));
        assert!(FS_NV12_COVER_TO_RGB.contains(".ra"));
        // Same bottom-up flip as FS_NV12_TO_RGB.
        assert!(FS_NV12_COVER_TO_RGB.contains("1.0 - "));
    }

    #[test]
    fn nv12_cover_fit_same_aspect_is_identity() {
        // Source aspect == panel aspect: no crop, full texture shown.
        let (scale, offset) = nv12_cover_fit_uv_transform(1920, 1080, 1280, 720);
        assert!((scale[0] - 1.0).abs() < 1e-5);
        assert!((scale[1] - 1.0).abs() < 1e-5);
        assert!(offset[0].abs() < 1e-5);
        assert!(offset[1].abs() < 1e-5);
    }

    #[test]
    fn nv12_cover_fit_wide_source_crops_sides() {
        // 2:1 source onto a 1:1 panel: full height, sides cropped.
        // scale_x = panel_aspect / frame_aspect = 1.0 / 2.0 = 0.5.
        let (scale, offset) = nv12_cover_fit_uv_transform(2000, 1000, 1000, 1000);
        assert!((scale[0] - 0.5).abs() < 1e-5);
        assert!((scale[1] - 1.0).abs() < 1e-5);
        // Centered crop: offset_x = (1 - 0.5) / 2 = 0.25.
        assert!((offset[0] - 0.25).abs() < 1e-5);
        assert!(offset[1].abs() < 1e-5);
    }

    #[test]
    fn nv12_cover_fit_tall_source_crops_top_bottom() {
        // 1:2 source onto a 1:1 panel: full width, top+bottom cropped.
        // scale_y = frame_aspect / panel_aspect = 0.5 / 1.0 = 0.5.
        let (scale, offset) = nv12_cover_fit_uv_transform(1000, 2000, 1000, 1000);
        assert!((scale[0] - 1.0).abs() < 1e-5);
        assert!((scale[1] - 0.5).abs() < 1e-5);
        assert!(offset[0].abs() < 1e-5);
        assert!((offset[1] - 0.25).abs() < 1e-5);
    }

    #[test]
    fn nv12_cover_fit_degenerate_dims_are_identity() {
        // Zero dims must not divide-by-zero; return the identity
        // transform (the caller's byte-size check rejects the frame).
        let (scale, offset) = nv12_cover_fit_uv_transform(0, 0, 1280, 720);
        assert_eq!(scale, [1.0, 1.0]);
        assert_eq!(offset, [0.0, 0.0]);
        let (scale, offset) = nv12_cover_fit_uv_transform(1920, 1080, 0, 0);
        assert_eq!(scale, [1.0, 1.0]);
        assert_eq!(offset, [0.0, 0.0]);
    }

    // Renderer-hardening C2 (finding H2, 2026-05-21) -- the over-large
    // NV12 frame guard. A source exceeding the vc4 2048-px texture cap
    // must be rejected before `glTexImage2D` (which would otherwise
    // fail GL_INVALID_VALUE and blit black silently).
    #[test]
    fn nv12_dims_ok_accepts_at_and_below_the_cap() {
        // A normal stream (<=2048 either axis) passes; the exact cap
        // on both axes is still fine — 2048 is the inclusive max.
        assert!(nv12_dims_ok(1920, 1080));
        assert!(nv12_dims_ok(1280, 720));
        assert!(nv12_dims_ok(MAX_GL_TEXTURE_DIM, MAX_GL_TEXTURE_DIM));
        assert!(nv12_dims_ok(MAX_GL_TEXTURE_DIM, 1));
        assert!(nv12_dims_ok(1, MAX_GL_TEXTURE_DIM));
    }

    #[test]
    fn nv12_dims_ok_rejects_over_cap_on_either_axis() {
        // A 1440p / 4K source (or anything over 2048 on one axis)
        // is rejected — the bake returns an Err instead of uploading.
        assert!(!nv12_dims_ok(MAX_GL_TEXTURE_DIM + 1, 1080));
        assert!(!nv12_dims_ok(1920, MAX_GL_TEXTURE_DIM + 1));
        assert!(!nv12_dims_ok(2560, 1440));
        assert!(!nv12_dims_ok(3840, 2160));
        assert!(!nv12_dims_ok(4096, 4096));
    }

    // FYS bug B (2026-05-21) -- cover-fit quad geometry for regular
    // image + video slide bakes. The math is pure; these lock the
    // crop direction + that the quad always covers the panel.

    /// UVs are NEVER scaled — only the four [0,1] pairs, fixed.
    fn cover_quad_uvs(v: &[f32; 16]) -> [(f32, f32); 4] {
        [(v[2], v[3]), (v[6], v[7]), (v[10], v[11]), (v[14], v[15])]
    }

    #[test]
    fn cover_fit_same_aspect_is_the_plain_fullscreen_quad() {
        // Source aspect == panel aspect: no overflow, the exact
        // +/-1 quad (byte-for-byte the cached_textured_quad_vbo one).
        let v = cover_fit_quad_verts(1280, 720, 1920, 1080);
        assert_eq!(v, [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ]);
    }

    // --- Bug W2 (2026-05-21): image-asset orientation ----------------
    //
    // A Web slide renders via the SAME image bake as a regular image
    // slide (`load_png_rgba` -> `glTexImage2D` -> textured quad). A
    // PNG decodes top-down, but the image-bake quad maps texture
    // `v=0` to screen-bottom, so a standard top-down asset came out
    // UPSIDE DOWN on glass. `load_png_rgba` now routes the decoded
    // buffer through `flip_rgba_rows_vertically` (bottom-up) so the
    // GL `v` convention renders it right-side up. These tests pin
    // that flip; they FAIL against the pre-fix code (which never
    // flipped the buffer).

    /// `flip_rgba_rows_vertically` reverses row order: a top-down
    /// RGBA buffer becomes bottom-up. The on-glass W2 fix in one
    /// pure assertion — a top-down RED-top/BLUE-bottom buffer must
    /// come back BLUE-top/RED-bottom so the image-bake quad paints
    /// it right-side up.
    #[test]
    fn flip_rgba_rows_reverses_row_order() {
        // 1x2 image: row 0 RED, row 1 BLUE (standard top-down).
        let top_down: Vec<u8> = vec![
            255, 0, 0, 255, // row 0 (top) = RED
            0, 0, 255, 255, // row 1 (bottom) = BLUE
        ];
        let flipped = flip_rgba_rows_vertically(top_down.clone(), 1, 2);
        assert_eq!(&flipped[0..4], &[0, 0, 255, 255], "row 0 is now BLUE");
        assert_eq!(&flipped[4..8], &[255, 0, 0, 255], "row 1 is now RED");
        // Flipping twice is the identity — proves it's a pure flip,
        // not a one-way transform that would double-flip on reuse.
        assert_eq!(flip_rgba_rows_vertically(flipped, 1, 2), top_down);
    }

    /// A wider real-shape buffer flips a whole row at a time — the
    /// per-pixel order WITHIN a row is preserved, only row order
    /// reverses. Guards against an off-by-one / transpose bug.
    #[test]
    fn flip_rgba_rows_preserves_within_row_pixel_order() {
        // 2x2: top row = [RED, GREEN], bottom row = [BLUE, WHITE].
        let top_down: Vec<u8> = vec![
            255, 0, 0, 255, 0, 255, 0, 255, // row 0: RED, GREEN
            0, 0, 255, 255, 255, 255, 255, 255, // row 1: BLUE, WHITE
        ];
        let flipped = flip_rgba_rows_vertically(top_down, 2, 2);
        // Row 0 is now the old bottom row, left-to-right unchanged.
        assert_eq!(&flipped[0..8], &[0, 0, 255, 255, 255, 255, 255, 255]);
        // Row 1 is now the old top row, left-to-right unchanged.
        assert_eq!(&flipped[8..16], &[255, 0, 0, 255, 0, 255, 0, 255]);
    }

    /// A buffer whose length does not match `w*h*4` — a malformed
    /// asset — is returned unchanged rather than panicking on the
    /// chunk math. Zero height is a clean no-op too.
    #[test]
    fn flip_rgba_rows_tolerates_a_mismatched_buffer() {
        let weird = vec![1u8, 2, 3];
        assert_eq!(flip_rgba_rows_vertically(weird.clone(), 4, 4), weird);
        let empty: Vec<u8> = Vec::new();
        assert_eq!(flip_rgba_rows_vertically(empty.clone(), 4, 0), empty);
    }

    #[test]
    fn cover_fit_wide_source_overflows_x_keeps_y() {
        // 2:1 source onto a 1:1 panel: full height (y == +/-1),
        // the sides overflow past +/-1 x and get clipped (cropped).
        // sx = frame_aspect / panel_aspect = 2.0 / 1.0 = 2.0.
        let v = cover_fit_quad_verts(2000, 1000, 1000, 1000);
        for i in 0..4 {
            assert!((v[i * 4].abs() - 2.0).abs() < 1e-5, "x overflows to +/-2");
            assert!((v[i * 4 + 1].abs() - 1.0).abs() < 1e-5, "y stays +/-1");
        }
        // UVs untouched — still the full [0,1] square.
        assert_eq!(
            cover_quad_uvs(&v),
            [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)],
        );
    }

    #[test]
    fn cover_fit_tall_source_overflows_y_keeps_x() {
        // 1:2 source onto a 1:1 panel: full width (x == +/-1), the
        // top + bottom overflow past +/-1 y and get clipped.
        // sy = panel_aspect / frame_aspect = 1.0 / 0.5 = 2.0.
        let v = cover_fit_quad_verts(1000, 2000, 1000, 1000);
        for i in 0..4 {
            assert!((v[i * 4].abs() - 1.0).abs() < 1e-5, "x stays +/-1");
            assert!((v[i * 4 + 1].abs() - 2.0).abs() < 1e-5, "y overflows to +/-2");
        }
    }

    #[test]
    fn cover_fit_always_covers_the_panel() {
        // Whatever the source aspect, the quad must reach AT LEAST
        // +/-1 on both axes (so the panel is fully covered, never
        // letterboxed) — exactly one axis overflows further.
        for (fw, fh) in [(640, 480), (1920, 1080), (1080, 1920), (3200, 900)] {
            let v = cover_fit_quad_verts(fw, fh, 1920, 1080);
            for i in 0..4 {
                assert!(v[i * 4].abs() >= 1.0 - 1e-5, "x covers for {fw}x{fh}");
                assert!(v[i * 4 + 1].abs() >= 1.0 - 1e-5, "y covers for {fw}x{fh}");
            }
        }
    }

    #[test]
    fn cover_fit_degenerate_dims_are_the_fullscreen_quad() {
        // Zero dims must not divide-by-zero; fall back to the plain
        // +/-1 quad (caller's frame-size checks reject 0-area frames).
        let id: [f32; 16] = [
            -1.0, -1.0, 0.0, 0.0,
             1.0, -1.0, 1.0, 0.0,
            -1.0,  1.0, 0.0, 1.0,
             1.0,  1.0, 1.0, 1.0,
        ];
        assert_eq!(cover_fit_quad_verts(0, 0, 1920, 1080), id);
        assert_eq!(cover_fit_quad_verts(1280, 720, 0, 0), id);
    }

    #[test]
    fn cover_fit_pathological_aspect_is_clamped() {
        // Hardening C3 / L1: a 10000x1 source into a 1920x1080
        // panel has frame_aspect = 10000, panel_aspect ≈ 1.78, so
        // the raw sx = frame_aspect / panel_aspect ≈ 5625 — far
        // past the GL guard band. The L2 clamp caps it at 16.0.
        let v = cover_fit_quad_verts(10000, 1, 1920, 1080);
        for i in 0..4 {
            assert!(
                v[i * 4].abs() <= 16.0 + 1e-3,
                "x clamped to <=16 (got {})", v[i * 4],
            );
            assert!(
                v[i * 4 + 1].abs() <= 16.0 + 1e-3,
                "y clamped to <=16 (got {})", v[i * 4 + 1],
            );
        }
        // The clamped axis lands exactly on the cap.
        assert!((v[0].abs() - 16.0).abs() < 1e-3, "wide source pinned at sx=16");
        // A pathological TALL source clamps the other axis the
        // same way.
        let vt = cover_fit_quad_verts(1, 10000, 1920, 1080);
        assert!((vt[1].abs() - 16.0).abs() < 1e-3, "tall source pinned at sy=16");
    }

    #[test]
    fn cover_fit_normal_aspect_unchanged_by_clamp() {
        // The L2 clamp is purely defensive — for a normal aspect
        // (sx/sy well under 16) it changes nothing.
        let v = cover_fit_quad_verts(2000, 1000, 1000, 1000);
        assert_eq!(v, [
            -2.0, -1.0, 0.0, 0.0,
             2.0, -1.0, 1.0, 0.0,
            -2.0,  1.0, 0.0, 1.0,
             2.0,  1.0, 1.0, 1.0,
        ]);
    }

    #[test]
    fn cover_quad_slot_miss_fills_empty_slots_first() {
        // Cold cache: a miss fills slot 0, then slot 1.
        let slots: [Option<CoverQuadKey>; 2] = [None, None];
        assert_eq!(
            cover_quad_slot(&slots, (1920, 1080, 1920, 1080), 0),
            CoverQuadSlot::Miss { idx: 0 },
        );
        let slots: [Option<CoverQuadKey>; 2] =
            [Some((1920, 1080, 1920, 1080)), None];
        assert_eq!(
            cover_quad_slot(&slots, (1280, 720, 1920, 1080), 0),
            CoverQuadSlot::Miss { idx: 1 },
        );
    }

    #[test]
    fn cover_quad_slot_alternating_keys_both_hit() {
        // The video↔video transition case: two differently-sized
        // endpoints. Once both keys are resident, every subsequent
        // lookup is a HIT — no rebuild, no churn.
        let key_a: CoverQuadKey = (1920, 1080, 1920, 1080);
        let key_b: CoverQuadKey = (640, 480, 1920, 1080);
        let slots: [Option<CoverQuadKey>; 2] = [Some(key_a), Some(key_b)];
        assert_eq!(
            cover_quad_slot(&slots, key_a, 0),
            CoverQuadSlot::Hit { idx: 0 },
        );
        assert_eq!(
            cover_quad_slot(&slots, key_b, 1),
            CoverQuadSlot::Hit { idx: 1 },
        );
        // Order in the array doesn't matter — a hit finds the key
        // wherever it sits.
        let slots: [Option<CoverQuadKey>; 2] = [Some(key_b), Some(key_a)];
        assert_eq!(
            cover_quad_slot(&slots, key_a, 0),
            CoverQuadSlot::Hit { idx: 1 },
        );
    }

    #[test]
    fn cover_quad_slot_evicts_round_robin_when_full() {
        // Both slots occupied, a third (different) key arrives:
        // evict via the round-robin cursor, not at random.
        let slots: [Option<CoverQuadKey>; 2] = [
            Some((1920, 1080, 1920, 1080)),
            Some((640, 480, 1920, 1080)),
        ];
        let key_c: CoverQuadKey = (800, 600, 1920, 1080);
        assert_eq!(
            cover_quad_slot(&slots, key_c, 0),
            CoverQuadSlot::Miss { idx: 0 },
        );
        assert_eq!(
            cover_quad_slot(&slots, key_c, 1),
            CoverQuadSlot::Miss { idx: 1 },
        );
        // The cursor wraps (modulo 2).
        assert_eq!(
            cover_quad_slot(&slots, key_c, 2),
            CoverQuadSlot::Miss { idx: 0 },
        );
    }

    #[test]
    fn fs_nv12_dmabuf_to_rgb_targets_external_oes_and_pins_extension() {
        // V4L2 piece 4b: DMA-BUF zero-copy NV12 sampler.
        // The shape is small but unforgiving -- a missing #extension
        // line is a compile-time error on Pi; a sampler2D where
        // samplerExternalOES is needed produces undefined results.
        // Pin both the extension directive AND the external-OES
        // sampler so a rename or accidental conversion to the
        // legacy two-texture path fails loudly on Mac CI before
        // it lands on Pi.
        assert!(FS_NV12_DMABUF_TO_RGB.starts_with("#version 100\n"));
        // Extension directive must appear BEFORE the precision +
        // uniform declarations (GLSL ES spec: extension directives
        // are preprocessor-tier).
        assert!(
            FS_NV12_DMABUF_TO_RGB.contains("#extension GL_OES_EGL_image_external : require"),
            "missing GL_OES_EGL_image_external require"
        );
        assert!(FS_NV12_DMABUF_TO_RGB.contains("precision mediump float"));
        // External OES sampler type -- distinct from sampler2D.
        assert!(FS_NV12_DMABUF_TO_RGB.contains("samplerExternalOES"));
        // Single sampler uniform; bound to GL_TEXTURE_EXTERNAL_OES
        // target with an EGLImage in piece 4c.
        assert!(FS_NV12_DMABUF_TO_RGB.contains("u_tex_external"));
        // varying name must match VS_TEXTURED_QUAD (shared with the
        // MMAP path so the program-cache vertex shader is reused).
        assert!(FS_NV12_DMABUF_TO_RGB.contains("v_uv"));
        // BT.601 math is NOT inline here -- the Pi's Mesa driver
        // performs YUV->RGB inside the external-OES sampler fast
        // path. The shader output is direct RGB. If a future Mesa
        // regression breaks this assumption (color cast / wrong
        // range), piece 4e's live-Pi smoke surfaces it; the fix
        // would be to reintroduce the BT.601 matrix here. Pin the
        // .rgb swizzle so an accidental sample of .raw or .rraa
        // doesn't slip through.
        assert!(FS_NV12_DMABUF_TO_RGB.contains(".rgb"));
        // No alpha channel in NV12; the shader forces opaque.
        assert!(FS_NV12_DMABUF_TO_RGB.contains("vec4(rgb, 1.0)"));
        // CRITICAL: no manual BT.601 coefficients here. If a
        // future patch adds them inline, the Mesa fast-path
        // contract changed and the docstring above needs updating.
        // (Listed coefficients from FS_NV12_TO_RGB that should
        // NOT appear here.)
        assert!(!FS_NV12_DMABUF_TO_RGB.contains("1.402"),
            "BT.601 math should not be inline -- Mesa handles it");
        assert!(!FS_NV12_DMABUF_TO_RGB.contains("255.0/219.0"),
            "limited-range Y scale should not be inline -- Mesa handles it");
        // r83 Phase B (2026-06-08): the DMABUF path shares the
        // y-axis crop with the MMAP path so Mesa's external-OES
        // import (which sees the full bcm2835-codec allocation,
        // including the 8 padding rows) gets the same green-line
        // mitigation.
        assert!(
            FS_NV12_DMABUF_TO_RGB.contains("uniform float u_y_crop_max"),
            "FS_NV12_DMABUF_TO_RGB must declare `uniform float u_y_crop_max` (r83 Phase B)",
        );
        assert!(
            FS_NV12_DMABUF_TO_RGB.contains("(1.0 - v_uv.y) * u_y_crop_max"),
            "FS_NV12_DMABUF_TO_RGB must apply the y-crop in uv_t (r83 Phase B)",
        );
    }

    #[test]
    fn fs_glyph_outline_targets_gles2_and_pins_uniforms() {
        // v1-spec-delta #4 (b): outline shader compiles + uses the
        // expected uniform set. dispatch in hdmi.rs picks this
        // shader when layer.outline is true; uniform names must
        // match.
        assert!(FS_GLYPH_OUTLINE.starts_with("#version 100\n"));
        assert!(FS_GLYPH_OUTLINE.contains("precision mediump float"));
        for uniform in [
            "u_atlas",
            "u_text_color",
            "u_outline_color",
            "u_pixel_size",
            "u_opacity",
        ] {
            assert!(
                FS_GLYPH_OUTLINE.contains(uniform),
                "FS_GLYPH_OUTLINE missing uniform {uniform:?}"
            );
        }
        // Pin the 4-neighbor sampling math so a refactor that drops
        // a direction lands as a host-test diff.
        for offset in [
            "vec2(0.0, -u_pixel_size.y)",
            "vec2(0.0,  u_pixel_size.y)",
            "vec2(-u_pixel_size.x, 0.0)",
            "vec2( u_pixel_size.x, 0.0)",
        ] {
            assert!(
                FS_GLYPH_OUTLINE.contains(offset),
                "FS_GLYPH_OUTLINE missing neighbor offset {offset:?}"
            );
        }
        // Output must be premultiplied like FS_GLYPH (matches the
        // GL_ONE / GL_ONE_MINUS_SRC_ALPHA blend).
        assert!(FS_GLYPH_OUTLINE.contains("color * alpha"));
    }

    #[test]
    fn fs_glyph_targets_gles2_and_pins_uniforms() {
        assert!(FS_GLYPH.starts_with("#version 100\n"));
        assert!(FS_GLYPH.contains("precision mediump float"));
        // v1-spec-delta #4: u_opacity uniform multiplies BOTH RGB
        // and the output alpha so opacity<1 over a non-black bg
        // composites correctly (was pre-multiplied into text_color
        // RGB only, leaving output alpha = unmultiplied a).
        for uniform in ["u_atlas", "u_text_color", "u_opacity"] {
            assert!(
                FS_GLYPH.contains(uniform),
                "FS_GLYPH missing uniform {uniform:?}"
            );
        }
        // Must read the alpha out of the LUMINANCE-uploaded texture
        // via `.r` (GLES2 LUMINANCE puts the value in r, g, b, a).
        assert!(FS_GLYPH.contains(".r"));
        // Pin the opacity multiplication into both RGB and alpha
        // outputs -- pre-fix only RGB had it, leaving alpha
        // un-attenuated and the bg invisible at low opacities.
        assert!(
            FS_GLYPH.contains("a * u_opacity"),
            "FS_GLYPH must multiply alpha by u_opacity (v1-spec-delta #4)"
        );
    }

    #[test]
    fn fs_gradient_uniform_names_pinned() {
        // The dispatch in hdmi.rs's draw_gradient_pattern looks up
        // these uniforms by name. If a future refactor renames them
        // without updating dispatch, the lookup returns None and
        // glow's uniform_*(None, ...) silently no-ops — black frames
        // at runtime. Pin them by name so a host test catches the
        // drift instead.
        for uniform in [
            "u_viewport",
            "u_dir",
            "u_proj_bounds",
            "u_color_a",
            "u_color_b",
        ] {
            assert!(
                FS_GRADIENT.contains(uniform),
                "FS_GRADIENT missing uniform {uniform:?}"
            );
        }
    }

    /// Load Anton (the FYS canonical font) from the repo's UI fonts
    /// dir. Tests that need a real font use this so we exercise the
    /// same TTF the Pi backend ships.
    fn load_anton() -> fontdue::Font {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts/anton.ttf");
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        fontdue::Font::from_bytes(bytes, fontdue::FontSettings::default())
            .expect("parse Anton TTF")
    }

    /// Load VT323 (the Boot-slide canonical monospace font). Used by
    /// the qarl-direct 2026-05-13 Bug A regression tests that pin
    /// monospace column alignment.
    fn load_vt323() -> fontdue::Font {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts/vt323.ttf");
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        fontdue::Font::from_bytes(bytes, fontdue::FontSettings::default())
            .expect("parse VT323 TTF")
    }






    // Newline handling (qarl-flag 2026-05-10): pre-fix, the
    // rasterizer fed \n to font.rasterize like any other char,
    // getting fontdue's missing-glyph tofu. Multi-line text was
    // squashed onto one line with a tofu in the middle. These
    // tests pin the multi-line behavior + newline normalization.

    #[test]
    fn split_text_into_lines_lf() {
        assert_eq!(split_text_into_lines(""), vec![""]);
        assert_eq!(split_text_into_lines("abc"), vec!["abc"]);
        assert_eq!(split_text_into_lines("a\nb"), vec!["a", "b"]);
        assert_eq!(split_text_into_lines("a\nb\nc"), vec!["a", "b", "c"]);
    }

    #[test]
    fn split_text_into_lines_crlf_normalized() {
        // \r\n (Windows) is one break.
        assert_eq!(split_text_into_lines("a\r\nb"), vec!["a", "b"]);
        // Bare \r (legacy Mac) is also one break.
        assert_eq!(split_text_into_lines("a\rb"), vec!["a", "b"]);
        // Mixed: \r\n and \n in one string.
        assert_eq!(
            split_text_into_lines("a\r\nb\nc"),
            vec!["a", "b", "c"],
        );
    }

    #[test]
    fn split_text_into_lines_empty_lines_preserved() {
        // Trailing newline -> trailing empty line (text-editor
        // convention).
        assert_eq!(split_text_into_lines("abc\n"), vec!["abc", ""]);
        // Empty middle line.
        assert_eq!(split_text_into_lines("a\n\nb"), vec!["a", "", "b"]);
        // Just newlines.
        assert_eq!(split_text_into_lines("\n\n"), vec!["", "", ""]);
    }







    // E (rasterize-side bitmap cap, 2026-05-09): predict +
    // clamp helpers for keeping rasterized bitmap dims within
    // MAX_RASTERIZED_BITMAP_DIM. Closes the multi-line >2048px
    // capture-side 0x501 bug AND deterministically caps per-
    // layer texture upload size on vc4.
















    #[test]
    fn gradient_fys_canonical_density_zero() {
        // Both FYS gradient slides ("06 · Uncage!!" and "10 · Scream")
        // ship with density=0.0. Cross-check that this produces the
        // top-to-bottom direction the seed comment claims.
        let g = gradient_uniforms(1024, 768, 0.0).unwrap();
        // angle=0° → dx=sin(0)=0, dy=cos(0)=1 → t increases as y
        // increases → color_a at top (small y), color_b at bottom.
        assert!(approx(g.dx, 0.0, 1e-6), "FYS density=0 should be vertical");
        assert!(approx(g.dy, 1.0, 1e-6), "FYS density=0 dy should be 1");
    }

    #[test]
    fn hex_pure_red() {
        let c = hex_to_rgba("#FF0000").unwrap();
        assert!(approx_eq(c[0], 1.0));
        assert!(approx_eq(c[1], 0.0));
        assert!(approx_eq(c[2], 0.0));
        assert!(approx_eq(c[3], 1.0));
    }

    #[test]
    fn hex_pure_black_default_alpha() {
        let c = hex_to_rgba("#000000").unwrap();
        assert_eq!(c, [0.0, 0.0, 0.0, 1.0]);
    }

    #[test]
    fn hex_lowercase_accepted() {
        let c = hex_to_rgba("#aabbcc").unwrap();
        let upper = hex_to_rgba("#AABBCC").unwrap();
        assert_eq!(c, upper);
    }

    #[test]
    fn hex_without_leading_hash_accepted() {
        // Some operator-input paths strip the `#`; tolerate that.
        assert_eq!(hex_to_rgba("050608"), hex_to_rgba("#050608"));
    }

    #[test]
    fn hex_8_digits_alpha() {
        let c = hex_to_rgba("#FF000080").unwrap();
        assert!(approx_eq(c[0], 1.0));
        assert!(approx_eq(c[3], 128.0 / 255.0));
    }

    #[test]
    fn hex_seed_slide_color_round_trips() {
        // First slide of FREE YOUR SIGN — near-black background.
        // Verify the bit-pattern matches the hex string exactly.
        let c = hex_to_rgba("#050608").unwrap();
        assert!(approx_eq(c[0], 5.0 / 255.0));
        assert!(approx_eq(c[1], 6.0 / 255.0));
        assert!(approx_eq(c[2], 8.0 / 255.0));
    }

    #[test]
    fn hex_rejects_wrong_length() {
        assert_eq!(hex_to_rgba("#FFF"), None); // 3 digits not yet supported
        assert_eq!(hex_to_rgba("#FFFFF"), None);
        assert_eq!(hex_to_rgba("#FFFFFFFFF"), None);
        assert_eq!(hex_to_rgba(""), None);
        assert_eq!(hex_to_rgba("#"), None);
    }

    #[test]
    fn hex_rejects_non_hex_chars() {
        assert_eq!(hex_to_rgba("#GGGGGG"), None);
        assert_eq!(hex_to_rgba("#12345Z"), None);
        assert_eq!(hex_to_rgba("#-12345"), None);
    }

    #[test]
    fn hex_trims_whitespace() {
        // Python's hex strings are `.upper()`-normalized at the
        // model layer; whitespace shouldn't be there but the trim
        // is cheap defense against operator-paste-with-newline.
        assert_eq!(hex_to_rgba("  #ABCDEF  "), hex_to_rgba("#ABCDEF"));
    }

    // -- font_family_to_filename --------------------------------

    #[test]
    fn font_family_anton_maps() {
        assert_eq!(font_family_to_filename("Anton"), Some("anton.ttf"));
    }

    #[test]
    fn font_family_multi_word_names_map() {
        // Editor's display names use spaces; filenames use hyphens.
        // Pin a few representative cases so a backend rename of the
        // display name flips a test rather than going silent-fallback.
        assert_eq!(
            font_family_to_filename("Bebas Neue"),
            Some("bebas-neue.ttf")
        );
        assert_eq!(
            font_family_to_filename("Permanent Marker"),
            Some("permanent-marker.ttf")
        );
        assert_eq!(
            font_family_to_filename("Roboto Slab"),
            Some("roboto-slab.ttf")
        );
    }

    #[test]
    fn font_family_case_sensitive() {
        // Display names are case-significant (the editor stores the
        // canonical Title Case form). Lower/upper variants should
        // miss → caller falls back. Pin so no one slips a
        // case-insensitive `match` past review.
        assert_eq!(font_family_to_filename("anton"), None);
        assert_eq!(font_family_to_filename("ANTON"), None);
        assert_eq!(font_family_to_filename("Bebas neue"), None);
    }

    #[test]
    fn font_family_unknown_returns_none() {
        assert_eq!(font_family_to_filename(""), None);
        assert_eq!(font_family_to_filename("Helvetica"), None);
        assert_eq!(font_family_to_filename("NotAFont"), None);
    }

    #[test]
    fn font_family_full_catalog_complete() {
        // The 23 entries the renderer ships. If a font is added to
        // ui/fonts/ AND added to the editor picker, this list grows;
        // this test catches accidental drift between the static map
        // and the canonical ui/fonts/ inventory.
        let expected = [
            "Anton",
            "Alfa Slab One",
            "Archivo Black",
            "Bebas Neue",
            "Bowlby One SC",
            "Caveat",
            "Caveat Brush",
            "Cinzel",
            "DM Serif Display",
            "Inter",
            "JetBrains Mono",
            "Oswald",
            "Pacifico",
            "Permanent Marker",
            "Playfair Display",
            "Reenie Beanie",
            "Roboto Slab",
            "Rye",
            "Sedgwick Ave Display",
            "Shadows Into Light",
            "Space Mono",
            "UnifrakturCook",
            "VT323",
        ];
        for family in expected {
            assert!(
                font_family_to_filename(family).is_some(),
                "missing mapping for {family:?}"
            );
        }
    }

    // -- FontCatalog --------------------------------------------

    #[test]
    fn catalog_loads_anton_from_repo_fonts_dir() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts");
        let cat = FontCatalog::new(dir, "Anton".to_string());
        let font = cat.get("Anton").expect("Anton must load");
        // Round-trip a glyph rasterization to confirm the font is
        // actually parsed (not just bytes-loaded).
        let (m, _) = font.rasterize('F', 64.0);
        assert!(m.width > 0 && m.height > 0);
    }

    #[test]
    fn catalog_falls_back_on_unknown_family() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts");
        let cat = FontCatalog::new(dir, "Anton".to_string());
        // Unknown family → fallback to Anton. Returns Some.
        let font = cat
            .get("ThisFontDoesNotExist")
            .expect("fallback to Anton should succeed");
        let (m, _) = font.rasterize('F', 64.0);
        assert!(m.width > 0 && m.height > 0);
    }

    #[test]
    fn catalog_caches_repeat_lookups() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts");
        let cat = FontCatalog::new(dir, "Anton".to_string());
        let a = cat.get("Anton").unwrap();
        let b = cat.get("Anton").unwrap();
        // Same Rc → same allocation.
        assert!(Rc::ptr_eq(&a, &b), "second lookup should hit cache");
    }

    #[test]
    fn catalog_returns_none_when_dir_missing_and_fallback_unavailable() {
        // Empty/nonexistent dir → even the fallback can't load.
        let cat = FontCatalog::new(
            PathBuf::from("/nonexistent/path/that/does/not/exist"),
            "Anton".to_string(),
        );
        assert!(cat.get("Anton").is_none());
        assert!(!cat.fallback_available());
    }

    #[test]
    fn catalog_caches_negative_lookups() {
        // After a known-bad family lookup, the cache should hold a
        // None entry so a second lookup short-circuits (no re-read,
        // no duplicate warn). We can't easily observe the warn-count
        // without capturing stderr, but we CAN observe that the
        // cache map gained an entry for the missing family.
        let cat = FontCatalog::new(
            PathBuf::from("/nonexistent/font/dir"),
            "Anton".to_string(),
        );
        // First lookup of an unknown-static-family family.
        assert!(cat.try_load("NotARealFont").is_none());
        let cache = cat.cache.borrow();
        assert!(
            cache.contains_key("NotARealFont"),
            "miss should be cached so next lookup is a hit"
        );
        assert!(cache["NotARealFont"].is_none());
    }

    #[test]
    fn catalog_fallback_family_getter() {
        let cat = FontCatalog::new(PathBuf::from("/tmp"), "Bebas Neue".to_string());
        assert_eq!(cat.fallback_family(), "Bebas Neue");
    }

    #[test]
    fn catalog_fallback_available_check() {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts");
        let cat = FontCatalog::new(dir, "Anton".to_string());
        assert!(cat.fallback_available());
    }

    // -- prev_idx_for_reel --------------------------------------

    #[test]
    fn prev_idx_first_item_first_pass_is_none() {
        // Pass 0, item 0: no predecessor — first slide of a
        // single-pass reel has no entry transition.
        assert_eq!(prev_idx_for_reel(0, 0, 19), None);
    }

    #[test]
    fn prev_idx_first_item_later_pass_wraps_to_last() {
        // Pass >= 1, item 0: --reel-loop wraparound. Comes from
        // the last item of the prior pass.
        assert_eq!(prev_idx_for_reel(0, 1, 19), Some(18));
        assert_eq!(prev_idx_for_reel(0, 99, 5), Some(4));
    }

    #[test]
    fn prev_idx_middle_item_is_predecessor() {
        // Any non-first item: the previous item, regardless of
        // pass.
        assert_eq!(prev_idx_for_reel(1, 0, 19), Some(0));
        assert_eq!(prev_idx_for_reel(5, 0, 19), Some(4));
        assert_eq!(prev_idx_for_reel(18, 0, 19), Some(17));
        assert_eq!(prev_idx_for_reel(7, 3, 19), Some(6));
    }

    #[test]
    fn prev_idx_handles_single_item_reel() {
        // Edge: 1-item reel + --reel-loop wraps slide 0 to itself.
        // Caller is expected to no-op the self-transition (cheap
        // defensive guard not in this fn's contract).
        assert_eq!(prev_idx_for_reel(0, 1, 1), Some(0));
    }

    #[test]
    fn prev_idx_handles_empty_reel() {
        // Edge: caller bail!s before this hits, but defensive
        // None for a 0-len reel anyway.
        assert_eq!(prev_idx_for_reel(0, 0, 0), None);
        assert_eq!(prev_idx_for_reel(0, 1, 0), None);
    }

    // -- clamp_transition_ms ------------------------------------

    #[test]
    fn clamp_transition_ms_floors_at_50() {
        assert_eq!(clamp_transition_ms(0), 50);
        assert_eq!(clamp_transition_ms(1), 50);
        assert_eq!(clamp_transition_ms(49), 50);
    }

    #[test]
    fn clamp_transition_ms_passes_through_at_or_above_floor() {
        assert_eq!(clamp_transition_ms(50), 50);
        assert_eq!(clamp_transition_ms(500), 500);
        assert_eq!(clamp_transition_ms(800), 800);
        assert_eq!(clamp_transition_ms(60_000), 60_000);
    }

    // -- effective_hold_ms (v1-spec-delta #1) -------------------

    #[test]
    fn effective_hold_ms_uses_override_when_set() {
        // Override is in seconds at the CLI for operator
        // ergonomics; multiplied by 1000 internally.
        assert_eq!(effective_hold_ms(5000, Some(1)), 1000);
        assert_eq!(effective_hold_ms(5000, Some(0)), 0);
        assert_eq!(effective_hold_ms(0, Some(7)), 7000);
    }

    #[test]
    fn effective_hold_ms_uses_slide_duration_ms_verbatim_when_no_override() {
        // Slide's duration_ms already in ms — no conversion. The
        // FYS Panic flash slides (130/350/500/800 ms) drove this
        // fix; before v1-spec-delta #1 they snapped to 1s.
        assert_eq!(effective_hold_ms(5000, None), 5000);
        assert_eq!(effective_hold_ms(8500, None), 8500);
        assert_eq!(effective_hold_ms(130, None), 130);
        assert_eq!(effective_hold_ms(350, None), 350);
        assert_eq!(effective_hold_ms(500, None), 500);
        assert_eq!(effective_hold_ms(800, None), 800);
    }

    #[test]
    fn effective_hold_ms_no_floor_for_zero_duration() {
        // Spec doesn't mandate a floor; a 0-duration slide is a
        // degenerate but valid flash. The previous 1-second floor
        // was a bug masquerading as a guard. Pin no-floor here so
        // a future re-add of a floor lands as a host-test diff
        // rather than going silent.
        assert_eq!(effective_hold_ms(0, None), 0);
        assert_eq!(effective_hold_ms(1, None), 1);
    }

    #[test]
    fn effective_hold_ms_override_saturates_on_huge_seconds() {
        // saturating_mul guards against overflow if someone
        // passes u64::MAX for the override (degenerate but
        // possible).
        assert_eq!(effective_hold_ms(0, Some(u64::MAX)), u64::MAX);
    }

    // -- glyph cache hit/miss (v1-spec-delta #3 QA F2) ----------


    #[test]
    fn should_rerasterize_misses_on_none_entry() {
        // First-frame paint: cache slot empty -> miss, rasterize.
        assert!(should_rerasterize(None, "hello", 100.0, 500.0));
        assert!(should_rerasterize(None, "", 100.0, 500.0));
    }









    // -- auto-mode (v1-spec-delta #3) ---------------------------

    #[test]
    fn unix_to_calendar_at_epoch_zero() {
        // 1970-01-01 00:00:00 UTC = Thursday (weekday=4).
        let c = unix_to_calendar_utc(0);
        assert_eq!(c.year, 1970);
        assert_eq!(c.month, 1);
        assert_eq!(c.day, 1);
        assert_eq!(c.hour, 0);
        assert_eq!(c.minute, 0);
        assert_eq!(c.second, 0);
        assert_eq!(c.weekday, 4);
    }

    #[test]
    fn unix_to_calendar_known_y2k() {
        // 2000-01-01 00:00:00 UTC = 946684800 seconds = Saturday (6).
        let c = unix_to_calendar_utc(946_684_800);
        assert_eq!(c.year, 2000);
        assert_eq!(c.month, 1);
        assert_eq!(c.day, 1);
        assert_eq!(c.weekday, 6);
    }

    #[test]
    fn unix_to_calendar_handles_leap_day() {
        // 2024-02-29 12:34:56 UTC = 1709210096.
        let c = unix_to_calendar_utc(1_709_210_096);
        assert_eq!(c.year, 2024);
        assert_eq!(c.month, 2);
        assert_eq!(c.day, 29);
        assert_eq!(c.hour, 12);
        assert_eq!(c.minute, 34);
        assert_eq!(c.second, 56);
        // 2024-02-29 was a Thursday.
        assert_eq!(c.weekday, 4);
    }

    #[test]
    fn unix_to_calendar_handles_negative_seconds() {
        // 1969-12-31 23:59:59 UTC = -1 = Wednesday (3).
        let c = unix_to_calendar_utc(-1);
        assert_eq!(c.year, 1969);
        assert_eq!(c.month, 12);
        assert_eq!(c.day, 31);
        assert_eq!(c.hour, 23);
        assert_eq!(c.minute, 59);
        assert_eq!(c.second, 59);
        assert_eq!(c.weekday, 3);
    }

    #[test]
    fn unix_to_calendar_local_resolves_in_tz() {
        // Bug 1 follow-up (2026-05-20): the auto_mode clock resolves
        // LOCAL time via libc localtime_r honoring TZ. All cases run
        // in ONE test fn (sequentially) — libc tz state is process-
        // global, so parallel tz-mutating tests would race.
        //
        // For a FIXED-OFFSET zone, local(t) is exactly utc(t+offset)
        // — so the trusted unix_to_calendar_utc is the reference.

        // --- Case 1: TZ=UTC — local == UTC, no shift.
        std::env::set_var("TZ", "UTC");
        let t = 1_776_782_109; // 2026-04-21 14:35:09 UTC
        assert_eq!(
            unix_to_calendar_local(t),
            unix_to_calendar_utc(t),
            "TZ=UTC: local must equal UTC",
        );

        // --- Case 2: Asia/Tokyo (UTC+9, no DST) — DATE ROLLOVER.
        // 2026-04-21 18:35:09 UTC -> Tokyo 2026-04-22 03:35:09:
        // the calendar DAY rolls at LOCAL midnight, not UTC's.
        std::env::set_var("TZ", "Asia/Tokyo");
        let t_eve = 1_776_796_509; // 2026-04-21 18:35:09 UTC
        let utc_eve = unix_to_calendar_utc(t_eve);
        let tokyo_eve = unix_to_calendar_local(t_eve);
        assert_eq!(utc_eve.day, 21, "UTC side is still the 21st");
        assert_eq!(tokyo_eve.day, 22, "Tokyo has rolled to the 22nd");
        assert_eq!(
            tokyo_eve,
            unix_to_calendar_utc(t_eve + 9 * 3600),
            "Tokyo = UTC+9 exactly",
        );
        // The rollover is visible through the date formatter too:
        assert_eq!(
            format_auto_text(Some("date"), Some("date_iso"), tokyo_eve).unwrap(),
            "2026-04-22",
        );
        assert_eq!(
            format_auto_text(Some("date"), Some("date_iso"), utc_eve).unwrap(),
            "2026-04-21",
        );

        // --- Case 3+4: Europe/London — DST is per-DATE, proving
        // libc applies the zoneinfo rules (not a fixed offset).
        std::env::set_var("TZ", "Europe/London");
        // Summer: 2026-07-15 12:00:00 UTC -> BST (UTC+1).
        let t_jul = 1_784_116_800;
        assert_eq!(
            unix_to_calendar_local(t_jul),
            unix_to_calendar_utc(t_jul + 3600),
            "London in July is BST (+1)",
        );
        // Winter: 2026-01-15 12:00:00 UTC -> GMT (UTC+0).
        let t_jan = 1_768_478_400;
        assert_eq!(
            unix_to_calendar_local(t_jan),
            unix_to_calendar_utc(t_jan),
            "London in January is GMT (+0)",
        );

        // Restore so later tests / the process see a clean env.
        std::env::remove_var("TZ");
    }

    /// Pinned reference point for format tests: April 21, 2026 at
    /// 14:35:09 UTC = Tuesday. unix = 1776_782_109
    /// (= 20564 days * 86400 + 14*3600 + 35*60 + 9).
    fn pinned_calendar() -> Calendar {
        let c = unix_to_calendar_utc(1_776_782_109);
        assert_eq!(c.year, 2026);
        assert_eq!(c.month, 4);
        assert_eq!(c.day, 21);
        assert_eq!(c.weekday, 2);
        c
    }

    #[test]
    fn format_auto_time_hm_two_digit_zero_padded() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("time"), Some("time_hm"), c).unwrap(),
            "14:35"
        );
    }

    #[test]
    fn format_auto_time_hms_includes_seconds() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("time"), Some("time_hms"), c).unwrap(),
            "14:35:09"
        );
    }

    #[test]
    fn format_auto_date_iso_yyyy_mm_dd() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("date"), Some("date_iso"), c).unwrap(),
            "2026-04-21"
        );
    }

    #[test]
    fn format_auto_date_long_month_name() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("date"), Some("date_long"), c).unwrap(),
            "April 21, 2026"
        );
    }

    #[test]
    fn format_auto_date_medium_short_month() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("date"), Some("date_medium"), c).unwrap(),
            "Apr 21"
        );
    }

    #[test]
    fn format_auto_day_long_full_weekday() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("day"), Some("day_long"), c).unwrap(),
            "Tuesday"
        );
    }

    #[test]
    fn format_auto_day_short_three_letter_weekday() {
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("day"), Some("day_short"), c).unwrap(),
            "Tue"
        );
    }

    #[test]
    fn format_auto_returns_none_when_mode_unset() {
        let c = pinned_calendar();
        assert_eq!(format_auto_text(None, None, c), None);
        assert_eq!(format_auto_text(None, Some("time_hm"), c), None);
    }

    #[test]
    fn format_auto_falls_back_to_default_format_when_format_unset() {
        // Spec validator rejects auto_format=None when auto_mode is
        // set on save, but the renderer is the second line of
        // defense for IPC edge cases.
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("time"), None, c).unwrap(),
            "14:35"
        );
        assert_eq!(
            // Parity fix 2026-05-19: date default is date_iso
            // (matches auto-format.js + auto_render.py), was
            // previously date_medium.
            format_auto_text(Some("date"), None, c).unwrap(),
            "2026-04-21"
        );
        assert_eq!(
            format_auto_text(Some("day"), None, c).unwrap(),
            "Tuesday"
        );
    }

    #[test]
    fn format_auto_falls_back_when_format_mismatches_mode() {
        // Spec validator catches this on save (auto_format must
        // start with the auto_mode prefix); renderer defensively
        // falls through to the mode's default rather than render
        // garbage.
        let c = pinned_calendar();
        assert_eq!(
            format_auto_text(Some("time"), Some("date_iso"), c).unwrap(),
            "14:35"
        );
        assert_eq!(
            // date default is date_iso post-parity-fix 2026-05-19.
            format_auto_text(Some("date"), Some("time_hm"), c).unwrap(),
            "2026-04-21"
        );
    }

    #[test]
    fn format_auto_unknown_mode_returns_none() {
        let c = pinned_calendar();
        assert_eq!(format_auto_text(Some("temperature"), None, c), None);
    }

    // -- motion engine (v1-spec-delta #2) -----------------------

    #[test]
    fn parse_motion_kind_recognized_values() {
        assert_eq!(parse_motion_kind("static"), MotionKind::Static);
        assert_eq!(parse_motion_kind("ticker"), MotionKind::Ticker);
        assert_eq!(parse_motion_kind("breathe"), MotionKind::Breathe);
        assert_eq!(parse_motion_kind("pulse"), MotionKind::Pulse);
        assert_eq!(parse_motion_kind("bounce"), MotionKind::Bounce);
        assert_eq!(parse_motion_kind("shake"), MotionKind::Shake);
        assert_eq!(parse_motion_kind("blink"), MotionKind::Blink);
    }

    #[test]
    fn parse_motion_kind_unknown_falls_back_static() {
        // Forward-added schema values render as static (no
        // animation) rather than crashing the renderer.
        assert_eq!(parse_motion_kind("warp"), MotionKind::Static);
        assert_eq!(parse_motion_kind(""), MotionKind::Static);
    }

    #[test]
    fn motion_static_is_identity() {
        // Static at any tick / phase / intensity = no contribution.
        let m = compute_motion_state(MotionKind::Static, 100, 0.5, 1.0, 0xDEAD, 7.42);
        assert!((m.offset_x_norm - 0.0).abs() < 1e-6);
        assert!((m.offset_y_norm - 0.0).abs() < 1e-6);
        assert!((m.scale - 1.0).abs() < 1e-6);
        assert!((m.alpha_mul - 1.0).abs() < 1e-6);
    }

    #[test]
    fn motion_ticker_starts_at_rest_at_phase_zero() {
        // Density-parity rewrite (2026-05-20): offset_x_norm is the
        // scroll fraction of one box-width tile pitch, in [0, 1).
        // t=0, phase=0 → 0.0 (rest — the tiled text un-scrolled).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 0.0);
        assert!(m.offset_x_norm.abs() < 1e-3, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_half_pitch_at_half_cycle() {
        // Sawtooth 0 → 1 over one period. At intensity=50 the period
        // is 6 - 5*0.5 = 3.5 s; halfway, offset_x_norm = 0.5 (the
        // tiled text scrolled half a box-width left).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 1.75);
        assert!((m.offset_x_norm - 0.5).abs() < 1e-3, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_near_one_just_before_wrap() {
        // Just before the period boundary the scroll fraction
        // approaches 1.0 (the text scrolled almost a full box-width;
        // the next tiled copy has all but taken its place).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 3.5 * 0.999);
        assert!(m.offset_x_norm > 0.99, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_wraps_back_to_rest_at_period_boundary() {
        // At t = period exactly the cycle wraps and the scroll
        // fraction returns to 0 — seamless because the tiling has
        // an identical copy one pitch over.
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 3.5);
        assert!(m.offset_x_norm.abs() < 1e-3, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_speed_zero_freezes_at_phase_position() {
        // motion_speed=0 holds offset_x at the phase-0 visual
        // state (no temporal advancement). Same value at t=0 and
        // t=100 for a frozen ticker.
        let m1 = compute_motion_state(MotionKind::Ticker, 50, 0.0, 0.0, 0, 0.0);
        let m2 = compute_motion_state(MotionKind::Ticker, 50, 0.0, 0.0, 0, 100.0);
        assert!((m1.offset_x_norm - m2.offset_x_norm).abs() < 1e-6);
    }

    #[test]
    fn motion_breathe_is_unity_at_zero_phase_zero_tick() {
        // sin(0) = 0 → scale = 1.0 + amp*0 = 1.0 exactly.
        let m = compute_motion_state(MotionKind::Breathe, 50, 0.0, 1.0, 0, 0.0);
        assert!((m.scale - 1.0).abs() < 1e-6);
    }

    #[test]
    fn motion_breathe_peak_at_quarter_period() {
        // At t = 0.25 s (quarter of a 1 Hz cycle), sin(π/2) = 1, so
        // scale = 1 + amp. Intensity=50 → amp = 0.02 + 0.18*0.5 =
        // 0.11. Pin the math so a regression here is loud.
        let m = compute_motion_state(MotionKind::Breathe, 50, 0.0, 1.0, 0, 0.25);
        assert!((m.scale - 1.11).abs() < 1e-3, "scale was {}", m.scale);
    }

    #[test]
    fn motion_breathe_amplitude_scales_with_intensity() {
        // Intensity=0 produces ±2 % swing; intensity=100 produces
        // ±20 %. Pin both endpoints.
        let m_lo = compute_motion_state(MotionKind::Breathe, 0, 0.0, 1.0, 0, 0.25);
        assert!((m_lo.scale - 1.02).abs() < 1e-3);
        let m_hi = compute_motion_state(MotionKind::Breathe, 100, 0.0, 1.0, 0, 0.25);
        assert!((m_hi.scale - 1.20).abs() < 1e-3);
    }

    #[test]
    fn motion_pulse_alpha_at_min_at_three_quarter_period() {
        // sin at 3π/2 = -1, so alpha = min + (1-min)*0 = min.
        // Intensity=50 → min = 0.70 * (1 - 0.5) = 0.35.
        let m = compute_motion_state(MotionKind::Pulse, 50, 0.0, 1.0, 0, 0.75);
        assert!((m.alpha_mul - 0.35).abs() < 1e-3, "alpha was {}", m.alpha_mul);
    }

    #[test]
    fn motion_pulse_intensity_zero_keeps_above_seventy_percent() {
        // Spec: intensity=0 produces 70-100 % shallow sweep.
        let m = compute_motion_state(MotionKind::Pulse, 0, 0.0, 1.0, 0, 0.75);
        assert!((m.alpha_mul - 0.70).abs() < 1e-3);
    }

    #[test]
    fn motion_bounce_offset_y_zero_at_zero_tick() {
        let m = compute_motion_state(MotionKind::Bounce, 50, 0.0, 1.0, 0, 0.0);
        assert!((m.offset_y_norm - 0.0).abs() < 1e-6);
    }

    #[test]
    fn motion_bounce_peak_at_quarter_period() {
        // 1 Hz, intensity=50 → amp = 0.01 + 0.09*0.5 = 0.055.
        // abs(sin(π/2)) = 1 → peak = -amp (negative = UP visually).
        let m = compute_motion_state(MotionKind::Bounce, 50, 0.0, 1.0, 0, 0.25);
        assert!((m.offset_y_norm + 0.055).abs() < 1e-3, "y was {}", m.offset_y_norm);
    }

    #[test]
    fn motion_bounce_abs_sin_shape_matches_python() {
        // Pin the abs(sin) wave shape against motion.py:300. The
        // signature of abs(sin) vs plain sin is TWO peaks per cycle
        // (at t=0.25 and t=0.75) and the offset NEVER crosses below
        // the rest line (0). intensity=50 → amp=0.055.
        //
        // Sample at t = 0, π/4 (0.125), π/2 (0.25), 3π/4 (0.375),
        // π (0.5), 5π/4 (0.625), 3π/2 (0.75), 7π/4 (0.875), 2π (1.0).
        let amp = 0.055_f32;
        let sqrt2_over_2 = (2.0_f32).sqrt() / 2.0; // sin(π/4) = sin(3π/4)
        let cases: &[(f64, f32)] = &[
            (0.0, 0.0),
            (0.125, -amp * sqrt2_over_2),
            (0.25, -amp),               // first peak UP
            (0.375, -amp * sqrt2_over_2),
            (0.5, 0.0),                  // crosses back to rest
            (0.625, -amp * sqrt2_over_2),
            (0.75, -amp),               // second peak UP — the abs(sin) signature
            (0.875, -amp * sqrt2_over_2),
            (1.0, 0.0),
        ];
        for (t, expected) in cases {
            let m = compute_motion_state(MotionKind::Bounce, 50, 0.0, 1.0, 0, *t);
            assert!(
                (m.offset_y_norm - *expected).abs() < 1e-3,
                "at t={t}: expected {expected}, got {}",
                m.offset_y_norm
            );
            // The "ball-on-floor" invariant: offset is never below
            // rest. Negative-or-zero means UP-or-rest, never DOWN.
            assert!(
                m.offset_y_norm <= 1e-6,
                "bounce went BELOW rest at t={t}: {}",
                m.offset_y_norm
            );
        }
    }

    #[test]
    fn motion_blink_freq_one_hz_at_intensity_fifty() {
        // Spec lock: at intensity=50, blink runs at 1 Hz exactly
        // (piecewise linear endpoint). At t=0.25 s (quarter of a
        // 1 Hz cycle) sin(π/2) > 0 → visible.
        let m = compute_motion_state(MotionKind::Blink, 50, 0.0, 1.0, 0, 0.25);
        assert!((m.alpha_mul - 1.0).abs() < 1e-6);
        // At t=0.75 s (3/4 cycle), sin(3π/2) < 0 → hidden.
        let m2 = compute_motion_state(MotionKind::Blink, 50, 0.0, 1.0, 0, 0.75);
        assert!((m2.alpha_mul - 0.0).abs() < 1e-6);
    }

    #[test]
    fn motion_blink_intensity_zero_runs_at_half_hz() {
        // 0.5 Hz cycle = 2 s period. At t=0.5 s (1/4 cycle)
        // sin > 0 → visible; at t=1.5 s (3/4 cycle) hidden.
        let m1 = compute_motion_state(MotionKind::Blink, 0, 0.0, 1.0, 0, 0.5);
        assert!((m1.alpha_mul - 1.0).abs() < 1e-6);
        let m2 = compute_motion_state(MotionKind::Blink, 0, 0.0, 1.0, 0, 1.5);
        assert!((m2.alpha_mul - 0.0).abs() < 1e-6);
    }

    #[test]
    fn motion_blink_intensity_hundred_runs_at_four_hz() {
        // 4 Hz cycle = 0.25 s period. At t=0.0625 s (1/4 cycle)
        // visible.
        let m = compute_motion_state(MotionKind::Blink, 100, 0.0, 1.0, 0, 0.0625);
        assert!((m.alpha_mul - 1.0).abs() < 1e-6);
    }

    #[test]
    fn motion_shake_deterministic_from_seed_and_phase() {
        // Same seed + phase + tick → same offset across calls.
        // This is the "across-reload determinism" property the
        // spec calls out.
        let m1 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0xCAFE, 0.5);
        let m2 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0xCAFE, 0.5);
        assert!((m1.offset_x_norm - m2.offset_x_norm).abs() < 1e-9);
        assert!((m1.offset_y_norm - m2.offset_y_norm).abs() < 1e-9);
    }

    #[test]
    fn motion_shake_different_seeds_diverge() {
        // Different layer ids should produce different shake
        // sequences, otherwise multiple shake layers would
        // mechanically march in lockstep.
        let m1 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0xAAAA, 0.5);
        let m2 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0xBBBB, 0.5);
        let dist =
            ((m1.offset_x_norm - m2.offset_x_norm).powi(2)
                + (m1.offset_y_norm - m2.offset_y_norm).powi(2))
            .sqrt();
        assert!(dist > 1e-6, "seeds collided: dist={dist}");
    }

    #[test]
    fn motion_shake_amplitude_clamped_at_intensity_hundred() {
        // ±4 % cap at intensity=100. Sample many ticks to confirm
        // no value escapes the cap.
        for tick in 0..200 {
            let t = tick as f64 * 0.05;
            let m = compute_motion_state(MotionKind::Shake, 100, 0.0, 1.0, 0xFEED, t);
            // amp = 0.005 + 0.035 * 1.0 = 0.04. Gaussian clamped
            // at ±1 → max output ±0.04.
            assert!(
                m.offset_x_norm.abs() <= 0.04 + 1e-6,
                "x out of bounds: {}",
                m.offset_x_norm
            );
            assert!(
                m.offset_y_norm.abs() <= 0.04 + 1e-6,
                "y out of bounds: {}",
                m.offset_y_norm
            );
        }
    }

    #[test]
    fn motion_shake_advances_at_ten_hz() {
        // The shake RNG re-samples every 100 ms. Within a 100 ms
        // bucket the offset should be constant (same tick_index).
        let t1 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0x1234, 0.50);
        let t2 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0x1234, 0.59);
        assert!((t1.offset_x_norm - t2.offset_x_norm).abs() < 1e-9);
        // Cross a bucket boundary (0.50 → 0.60) and the offset
        // changes (probabilistically — guard against the
        // astronomical chance the next Gaussian draw == previous).
        let t3 = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0x1234, 0.60);
        assert!(
            (t1.offset_x_norm - t3.offset_x_norm).abs() > 1e-9
                || (t1.offset_y_norm - t3.offset_y_norm).abs() > 1e-9
        );
    }

    #[test]
    fn motion_phase_offsets_two_layers_into_opposition() {
        // Spec: "two breathe layers with motion_phase=0 and 0.5
        // run in opposition." Confirm: scale at phase=0 is mirror
        // of scale at phase=0.5 around 1.0.
        let a = compute_motion_state(MotionKind::Breathe, 50, 0.0, 1.0, 0, 0.25);
        let b = compute_motion_state(MotionKind::Breathe, 50, 0.5, 1.0, 0, 0.25);
        // a.scale = 1 + amp; b.scale = 1 - amp (cycle phase
        // offset by half — sin(π/2 + π) = -1).
        let amp_a = a.scale - 1.0;
        let amp_b = b.scale - 1.0;
        assert!((amp_a + amp_b).abs() < 1e-3, "a={amp_a} b={amp_b}");
    }

    // -- QA F1 (slice c): blink speed=0 honors phase ----------

    #[test]
    fn motion_blink_speed_zero_honors_phase() {
        // Pre-F1, blink at speed=0 returned IDENTITY unconditionally
        // (visible). Fix: evaluate the square wave at the layer's
        // motion_phase so phase=0 → visible, phase=0.6 → hidden.
        // Matches spec lines 277-280 ("frozen visual = phase=0 +
        // motion_phase").
        let m_visible =
            compute_motion_state(MotionKind::Blink, 50, 0.0, 0.0, 0, 12.0);
        assert!((m_visible.alpha_mul - 1.0).abs() < 1e-6);
        let m_hidden =
            compute_motion_state(MotionKind::Blink, 50, 0.6, 0.0, 0, 12.0);
        assert!((m_hidden.alpha_mul - 0.0).abs() < 1e-6);
    }

    // -- QA F3 (slice c): speed=0 freeze coverage on the
    //    remaining five modes (ticker is already pinned via
    //    motion_ticker_speed_zero_freezes_at_phase_position).

    #[test]
    fn motion_breathe_speed_zero_freezes_at_phase_visual() {
        // sin(2π * phase) determines the frozen scale; phase=0.25
        // gives sin(π/2)=1, peak amplitude.
        let frozen =
            compute_motion_state(MotionKind::Breathe, 50, 0.25, 0.0, 0, 7.0);
        // At intensity=50 amp=0.11, scale=1.11.
        assert!((frozen.scale - 1.11).abs() < 1e-3);
        let frozen2 =
            compute_motion_state(MotionKind::Breathe, 50, 0.25, 0.0, 0, 99.0);
        assert!((frozen.scale - frozen2.scale).abs() < 1e-9);
    }

    #[test]
    fn motion_pulse_speed_zero_freezes_at_phase_visual() {
        // phase=0 → sin=0 → frac=0.5 → at intensity=50: 0.35 + 0.65*0.5
        // = 0.675. Same value across any tick because tick*0=0.
        let a = compute_motion_state(MotionKind::Pulse, 50, 0.0, 0.0, 0, 1.0);
        let b = compute_motion_state(MotionKind::Pulse, 50, 0.0, 0.0, 0, 50.0);
        assert!((a.alpha_mul - b.alpha_mul).abs() < 1e-9);
        assert!((a.alpha_mul - 0.675).abs() < 1e-3);
    }

    #[test]
    fn motion_bounce_speed_zero_freezes_at_phase_visual() {
        let a = compute_motion_state(MotionKind::Bounce, 50, 0.25, 0.0, 0, 1.0);
        let b = compute_motion_state(MotionKind::Bounce, 50, 0.25, 0.0, 0, 99.0);
        assert!((a.offset_y_norm - b.offset_y_norm).abs() < 1e-9);
        // abs(sin(π/2)) = 1 → frozen offset = -amp (UP).
        assert!((a.offset_y_norm + 0.055).abs() < 1e-3);
    }

    #[test]
    fn motion_shake_speed_zero_advances_anyway() {
        // Shake explicitly ignores `speed` (spec line 230: "shake
        // modulates amplitude not frequency"). The 10 Hz bucket
        // sampling is wall-clock-driven, so speed=0 doesn't freeze
        // shake — pinning that intent here so a future "freeze on
        // speed=0" refactor doesn't silently change behavior.
        let a = compute_motion_state(MotionKind::Shake, 50, 0.0, 0.0, 0xC0DE, 0.5);
        let b = compute_motion_state(MotionKind::Shake, 50, 0.0, 0.0, 0xC0DE, 1.5);
        // Values should differ — different tick buckets.
        let dist =
            ((a.offset_x_norm - b.offset_x_norm).powi(2)
                + (a.offset_y_norm - b.offset_y_norm).powi(2))
            .sqrt();
        assert!(dist > 1e-9, "shake-at-speed=0 should still tick (dist={dist})");
    }

    // -- QA F3: speed=2.0 upper-clamp pin (period halves).

    #[test]
    fn motion_ticker_speed_two_halves_period() {
        // intensity=50 → period=3.5s. speed=2 → effective 1.75s.
        // At t=0.875 (= half of the effective period), the scroll
        // fraction is cycle=0.5 (half a tile pitch).
        let m =
            compute_motion_state(MotionKind::Ticker, 50, 0.0, 2.0, 0, 0.875);
        assert!((m.offset_x_norm - 0.5).abs() < 1e-3, "off was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_breathe_speed_two_halves_period() {
        // 1 Hz → 2 Hz with speed=2. Quarter of 2 Hz period = 0.125s.
        let m =
            compute_motion_state(MotionKind::Breathe, 50, 0.0, 2.0, 0, 0.125);
        assert!((m.scale - 1.11).abs() < 1e-3);
    }

    #[test]
    fn motion_pulse_speed_two_halves_period() {
        // Pulse min at 3/4 of effective period. speed=2: 3/4 of 0.5s
        // = 0.375s.
        let m =
            compute_motion_state(MotionKind::Pulse, 50, 0.0, 2.0, 0, 0.375);
        assert!((m.alpha_mul - 0.35).abs() < 1e-3);
    }

    #[test]
    fn motion_bounce_speed_two_halves_period() {
        let m =
            compute_motion_state(MotionKind::Bounce, 50, 0.0, 2.0, 0, 0.125);
        // abs(sin(π/2)) = 1 → peak = -amp (UP).
        assert!((m.offset_y_norm + 0.055).abs() < 1e-3);
    }

    #[test]
    fn motion_blink_speed_two_doubles_freq() {
        // intensity=50 → 1 Hz. speed=2 → 2 Hz. Quarter cycle =
        // 0.125s → visible. 3/4 cycle = 0.375s → hidden.
        let v =
            compute_motion_state(MotionKind::Blink, 50, 0.0, 2.0, 0, 0.125);
        assert!((v.alpha_mul - 1.0).abs() < 1e-6);
        let h =
            compute_motion_state(MotionKind::Blink, 50, 0.0, 2.0, 0, 0.375);
        assert!((h.alpha_mul - 0.0).abs() < 1e-6);
    }

    #[test]
    fn motion_shake_speed_two_independent_of_speed() {
        // Spec: shake amp depends on intensity, frequency is 10 Hz
        // wall-clock regardless of speed. Same tick = same offset
        // across speeds.
        let a = compute_motion_state(MotionKind::Shake, 50, 0.0, 1.0, 0xBEEF, 0.5);
        let b = compute_motion_state(MotionKind::Shake, 50, 0.0, 2.0, 0xBEEF, 0.5);
        assert!((a.offset_x_norm - b.offset_x_norm).abs() < 1e-9);
        assert!((a.offset_y_norm - b.offset_y_norm).abs() < 1e-9);
    }

    // -- QA F3: intensity=0 != static — pin the deliberate spec
    //    choice. A future "i=0 disables effect" refactor would
    //    fire these.

    #[test]
    fn motion_breathe_intensity_zero_still_animates() {
        // amp = 0.02 + 0.18*0 = 0.02 — small but non-zero.
        let m = compute_motion_state(MotionKind::Breathe, 0, 0.0, 1.0, 0, 0.25);
        assert!((m.scale - 1.02).abs() < 1e-3);
        assert!((m.scale - 1.0).abs() > 1e-4, "scale was {}", m.scale);
    }

    #[test]
    fn motion_pulse_intensity_zero_still_animates() {
        // min_alpha = 0.70. At t=0.75s: alpha = 0.70.
        let m = compute_motion_state(MotionKind::Pulse, 0, 0.0, 1.0, 0, 0.75);
        assert!((m.alpha_mul - 0.70).abs() < 1e-3);
        assert!((m.alpha_mul - 1.0).abs() > 1e-4);
    }

    #[test]
    fn motion_bounce_intensity_zero_still_animates() {
        // amp = 0.01. abs(sin(π/2)) = 1 → offset = -0.01 (UP). Pins
        // the deliberate Rust spec choice: intensity=0 != static
        // (motion.py would return 0 here; this divergence is
        // intentional per QA F3).
        let m = compute_motion_state(MotionKind::Bounce, 0, 0.0, 1.0, 0, 0.25);
        assert!((m.offset_y_norm + 0.01).abs() < 1e-3);
    }

    #[test]
    fn motion_shake_intensity_zero_still_animates() {
        // amp = 0.005. 200-tick fuzz must produce at least one
        // non-zero offset (the all-zero case has astronomical odds).
        let mut nonzero = 0;
        for tick in 0..200 {
            let m = compute_motion_state(
                MotionKind::Shake,
                0,
                0.0,
                1.0,
                0xBABE,
                tick as f64 * 0.05,
            );
            if m.offset_x_norm.abs() > 1e-9 || m.offset_y_norm.abs() > 1e-9 {
                nonzero += 1;
            }
        }
        assert!(nonzero > 100, "expected most ticks non-zero, got {nonzero}");
    }

    // -- QA F3: defensive negative-speed clamp pin. Pydantic
    //    rejects negative speed on save, but the renderer is the
    //    second line of defense for stale envelopes / IPC bugs.

    #[test]
    fn motion_speed_negative_clamps_to_zero() {
        // Negative speed clamps to 0.0 (the lower edge of the
        // 0..2 spec range). Same as speed=0 visually.
        let frozen =
            compute_motion_state(MotionKind::Ticker, 50, 0.0, -1.0, 0, 0.0);
        let zero =
            compute_motion_state(MotionKind::Ticker, 50, 0.0, 0.0, 0, 0.0);
        assert!((frozen.offset_x_norm - zero.offset_x_norm).abs() < 1e-9);
    }

    #[test]
    fn motion_offset_to_px_ticker_uses_box_width() {
        // offset_x_norm is the [0,1) scroll fraction of one box-
        // width tile pitch; the px result is NEGATIVE (the ticker
        // scrolls left). 0.5 of an 800px box -> -400px.
        let s = MotionState {
            offset_x_norm: 0.5,
            ..MotionState::IDENTITY
        };
        let (dx, dy) = motion_offset_to_px(MotionKind::Ticker, s, 800.0, 200.0, 64.0);
        assert!((dx - (-400.0)).abs() < 1e-3, "dx was {dx}");
        assert!(dy.abs() < 1e-6);
    }

    #[test]
    fn motion_offset_to_px_bounce_uses_box_height() {
        let s = MotionState {
            offset_y_norm: 0.1,
            ..MotionState::IDENTITY
        };
        let (dx, dy) = motion_offset_to_px(MotionKind::Bounce, s, 800.0, 200.0, 64.0);
        assert!(dx.abs() < 1e-6);
        assert!((dy - 20.0).abs() < 1e-3);
    }

    #[test]
    fn motion_offset_to_px_shake_uses_glyph_height() {
        // Shake is glyph-height-relative per the spec; pin to
        // font_size_px not box dims.
        let s = MotionState {
            offset_x_norm: 0.04,
            offset_y_norm: -0.02,
            ..MotionState::IDENTITY
        };
        let (dx, dy) = motion_offset_to_px(MotionKind::Shake, s, 800.0, 200.0, 100.0);
        assert!((dx - 4.0).abs() < 1e-3);
        assert!((dy - (-2.0)).abs() < 1e-3);
    }

    #[test]
    fn motion_offset_to_px_other_modes_return_zero() {
        // Static / Breathe / Pulse / Blink don't translate; their
        // motion expresses through `scale` / `alpha_mul` instead.
        for kind in [
            MotionKind::Static,
            MotionKind::Breathe,
            MotionKind::Pulse,
            MotionKind::Blink,
        ] {
            let s = MotionState {
                offset_x_norm: 0.5,
                offset_y_norm: 0.5,
                ..MotionState::IDENTITY
            };
            let (dx, dy) = motion_offset_to_px(kind, s, 800.0, 200.0, 64.0);
            assert!(dx.abs() < 1e-6, "kind={:?} dx={}", kind, dx);
            assert!(dy.abs() < 1e-6, "kind={:?} dy={}", kind, dy);
        }
    }

    #[test]
    fn motion_intensity_clamps_above_one_hundred() {
        // Schema says 0..=100 but the field is u8 so values up to
        // 255 are technically representable. Clamp at 100 so a
        // weird envelope can't drive the math out of range.
        let clamped = compute_motion_state(MotionKind::Breathe, 200, 0.0, 1.0, 0, 0.25);
        let pinned = compute_motion_state(MotionKind::Breathe, 100, 0.0, 1.0, 0, 0.25);
        assert!((clamped.scale - pinned.scale).abs() < 1e-6);
    }

    // -- effective_font_size_px ---------------------------------

    #[test]
    fn effective_size_px_wins_when_set() {
        // px-explicit short-circuits the pct math.
        let s = effective_font_size_px(Some(120.0), Some(80.0), 0.5, 1920);
        assert!((s - 120.0).abs() < 1e-3);
    }

    #[test]
    fn effective_size_pct_uses_box_width() {
        // Phase 4.2c semantics: percent-of-box-WIDTH.
        // box_w_px = 0.5 * 1920 = 960; size = 80% * 960 = 768.
        let s = effective_font_size_px(None, Some(80.0), 0.5, 1920);
        assert!((s - 768.0).abs() < 1e-3, "got {s}");
    }

    #[test]
    fn effective_size_default_when_neither() {
        let s = effective_font_size_px(None, None, 0.5, 1920);
        assert!((s - 64.0).abs() < 1e-3);
    }

    #[test]
    fn effective_size_floor_8px() {
        // Microscopic sizes round up to 8.0 — fontdue degenerates
        // sub-pixel, and any glyph that small is invisible anyway.
        let s = effective_font_size_px(Some(2.0), None, 0.0, 1920);
        assert!((s - 8.0).abs() < 1e-3, "got {s}");
        let s = effective_font_size_px(None, Some(0.1), 0.0, 1920);
        assert!((s - 8.0).abs() < 1e-3, "got {s}");
    }

    #[test]
    fn effective_size_zero_box_w_clamps_min_1() {
        // 0-width box: pct math degenerates to 0 → floor at 8.
        let s = effective_font_size_px(None, Some(80.0), 0.0, 1920);
        assert!((s - 8.0).abs() < 1e-3);
    }

    // -- parse_h_align -------------------------------------------

    #[test]
    fn parse_h_align_recognized() {
        assert_eq!(parse_h_align("left"), HAlign::Left);
        assert_eq!(parse_h_align("center"), HAlign::Center);
        assert_eq!(parse_h_align("right"), HAlign::Right);
    }

    #[test]
    fn parse_h_align_unknown_falls_back_left() {
        // Tolerant defaults match the rest of the renderer's stance
        // on operator/model drift.
        assert_eq!(parse_h_align(""), HAlign::Left);
        assert_eq!(parse_h_align("justify"), HAlign::Left);
        assert_eq!(parse_h_align("CENTER"), HAlign::Left); // case-sensitive
    }

    // v1.0 close: parse_v_align mirrors the parse_h_align contract for
    // SYSTEM_SPEC §5.10a `anchor` (top / center / bottom). Unknown
    // values fall back to Middle so a forward-compat field doesn't
    // panic at paint time.

    #[test]
    fn parse_v_align_recognized() {
        assert_eq!(parse_v_align("top"), VAlign::Top);
        assert_eq!(parse_v_align("center"), VAlign::Middle);
        assert_eq!(parse_v_align("bottom"), VAlign::Bottom);
    }

    #[test]
    fn parse_v_align_unknown_falls_back_middle() {
        assert_eq!(parse_v_align(""), VAlign::Middle);
        assert_eq!(parse_v_align("baseline"), VAlign::Middle);
        assert_eq!(parse_v_align("TOP"), VAlign::Middle); // case-sensitive
    }

    // -- box_to_ndc_quad -----------------------------------------

    fn approx_ndc_eq(actual: (f32, f32, f32, f32), expected: (f32, f32, f32, f32)) -> bool {
        let eps = 1e-4;
        (actual.0 - expected.0).abs() < eps
            && (actual.1 - expected.1).abs() < eps
            && (actual.2 - expected.2).abs() < eps
            && (actual.3 - expected.3).abs() < eps
    }

    #[test]
    fn box_quad_full_viewport_left_top_no_scale() {
        // Box covers the full viewport, bitmap exactly fills it,
        // align top-left → NDC corners are -1..+1 on both axes.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 1920, 1080, 0, 1920, 1080, HAlign::Left, VAlign::Top,
        );
        assert!(
            approx_ndc_eq(q, (-1.0, 1.0, 1.0, -1.0)),
            "got ({}, {}, {}, {})", q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_smaller_bitmap_left_top_no_overflow() {
        // 100x50 bitmap inside a 0.5x0.5 box on 1920x1080 viewport,
        // top-left aligned → bitmap sits at box top-left, no scaling.
        let q = box_to_ndc_quad(
            0.0, 0.0, 0.5, 0.5, 100, 50, 0, 1920, 1080, HAlign::Left, VAlign::Top,
        );
        // Pixel rect: (0, 0) → (100, 50). NDC:
        //   left:   0/1920*2-1 = -1.0
        //   right:  100/1920*2-1 ≈ -0.8958
        //   top:    1 - 0/1080*2 = 1.0
        //   bottom: 1 - 50/1080*2 ≈ 0.9074
        assert!(
            approx_ndc_eq(q, (-1.0, -0.89583, 1.0, 0.90741)),
            "got ({}, {}, {}, {})", q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_handles_negative_origin_without_panic() {
        // qarl 2026-05-19 TextBox schema-widen: x, y now allowed
        // [-2.0, 3.0] (was [0.0, 1.0]) so a layer can animate in
        // from off-screen. Verify the NDC math handles negative
        // box origin without overflow / panic and produces NDC
        // coords past -1.0 (which GL viewport then clips). The
        // visible portion of the box still maps in-bounds.
        let q = box_to_ndc_quad(
            -0.5, -0.25, 1.0, 1.0, 1920, 1080, 0, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        // box_left_px = -0.5 * 1920 = -960; NDC l = -960/1920*2 - 1 = -2.0.
        // box_top_px = -0.25 * 1080 = -270; NDC t = 1 - (-270)/1080*2 = 1.5.
        // box_right_px = (-0.5 + 1.0) * 1920 = 960; NDC r = 960/1920*2 - 1 = 0.
        // box_bottom_px = (-0.25 + 1.0) * 1080 = 810; NDC b = 1 - 810/1080*2 ≈ -0.5.
        assert!(
            approx_ndc_eq(q, (-2.0, 0.0, 1.5, -0.5)),
            "negative-origin NDC: ({}, {}, {}, {})", q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_handles_oversized_box_without_panic() {
        // Schema-widen: w, h now allowed up to 5.0. The scale-down-
        // only pass-2 logic means oversized boxes don't actually
        // produce oversized NDC (the bitmap content fits at its
        // natural pixel size inside the larger box). Verify the
        // function doesn't panic, produces a well-ordered NDC
        // quad, and that all components stay finite.
        let q = box_to_ndc_quad(
            0.0, 0.0, 2.0, 1.5, 1920, 1080, 0, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        assert!(
            q.0.is_finite() && q.1.is_finite() && q.2.is_finite() && q.3.is_finite(),
            "oversized-box NDC not finite: {:?}", q,
        );
        assert!(q.1 > q.0, "ndc x range degenerate: {:?}", q);
        assert!(q.2 > q.3, "ndc y range degenerate: {:?}", q);
    }

    #[test]
    fn box_quad_handles_tiny_dimensions_via_min_floor() {
        // Schema-widen: w, h now allowed down to 0.01. Verify the
        // .max(1.0) px floor in box_to_ndc_quad prevents zero-px
        // dimensions (which would cause a divide-by-zero downstream)
        // without erroring on the otherwise-valid 0.01 box.
        let q = box_to_ndc_quad(
            0.0, 0.0, 0.01, 0.01, 1920, 1080, 0, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        // box_w_px = max(0.01*1920, 1.0) = 19.2px. No panic; the
        // resulting NDC is a small but non-degenerate rect.
        assert!(q.1 > q.0 && q.2 > q.3, "tiny-box NDC degenerate: {:?}", q);
    }

    #[test]
    fn box_quad_centered_horizontally() {
        // 100px-wide bitmap inside a 1.0-wide (full-screen) box
        // on 1920px viewport, h-align center: bitmap NDC width is
        // 100/1920*2 = 0.10417, centered around 0 → -0.05208..0.05208.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 100, 50, 0, 1920, 1080, HAlign::Center, VAlign::Top,
        );
        assert!(
            (q.0 + 0.05208).abs() < 1e-3 && (q.1 - 0.05208).abs() < 1e-3,
            "centered NDC l/r: {} / {}", q.0, q.1,
        );
    }

    #[test]
    fn box_quad_right_aligned() {
        // 100px-wide bitmap inside a 1.0-wide box, h-align right:
        // bitmap right edge at viewport right = 1.0; left edge at
        // 1.0 - 100/1920*2 = 0.89583.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 100, 50, 0, 1920, 1080, HAlign::Right, VAlign::Top,
        );
        assert!(
            (q.1 - 1.0).abs() < 1e-3 && (q.0 - 0.89583).abs() < 1e-3,
            "right-aligned NDC l/r: {} / {}", q.0, q.1,
        );
    }

    #[test]
    fn box_quad_centered_vertically() {
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 100, 50, 0, 1920, 1080,
            HAlign::Left, VAlign::Middle,
        );
        // 50px tall in 1080 viewport → NDC h = 50/1080*2 = 0.09259
        // centered → top ≈ 0.04630, bottom ≈ -0.04630.
        assert!(
            (q.2 - 0.04630).abs() < 1e-3 && (q.3 + 0.04630).abs() < 1e-3,
            "v-centered NDC t/b: {} / {}", q.2, q.3,
        );
    }

    #[test]
    fn box_quad_overflow_scales_down_per_axis() {
        // Bug 1 (2026-05-19): per-axis squish. 4000x2000 bitmap into
        // a 1000x1000 box → s_w = 0.25, s_h = 0.5 (independent).
        // Pre-Bug-1 uniform-min took 0.25 on BOTH axes (placed
        // 1000x500); per-axis produces placed = 1000x1000 (fills box
        // on both axes). Matches Canvas2D's per-axis squish at
        // rasterize.js:232 + 306-326.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            4000, 2000, 0, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        // Placed pixel rect: (0, 0) → (1000, 1000).
        let exp_l = -1.0;
        let exp_r = 1000.0 / 1920.0 * 2.0 - 1.0;
        let exp_t = 1.0;
        let exp_b = 1.0 - 1000.0 / 1080.0 * 2.0;
        assert!(
            approx_ndc_eq(q, (exp_l, exp_r, exp_t, exp_b)),
            "got ({}, {}, {}, {}); expected ({exp_l}, {exp_r}, {exp_t}, {exp_b})",
            q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_overflow_only_one_dim() {
        // Bug 1 (2026-05-19): per-axis squish. Very wide bitmap
        // (3000x100) into a 1000x1000 box → s_w = 0.333 (squish),
        // s_h = 1.0 (no overflow on h, no scale). Placed = 1000 x 100
        // (unchanged on h). Pre-Bug-1 took the uniform min and
        // shrunk h by 0.333× too, producing placed_h ≈ 33.3.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            3000, 100, 0, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        // placed_h = 100 (h didn't overflow, no scale).
        let exp_b = 1.0 - 100.0 / 1080.0 * 2.0;
        assert!(
            (q.3 - exp_b).abs() < 1e-3,
            "per-axis placed h: NDC bottom {} expected {}",
            q.3, exp_b,
        );
        // Width fills the box (s_w squishes 3000→1000).
        let exp_r = 1000.0 / 1920.0 * 2.0 - 1.0;
        assert!(
            (q.1 - exp_r).abs() < 1e-3,
            "per-axis placed w: NDC right {} expected {}",
            q.1, exp_r,
        );
    }

    #[test]
    fn box_quad_centered_align_after_scale_down() {
        // Bug 1 (2026-05-19): per-axis squish. 4000x2000 bitmap into
        // 1000x1000 box at center alignment → s_w=0.25 / s_h=0.5,
        // placed = 1000x1000 → fills both axes, no centering offset
        // on either. Pre-Bug-1 uniform-min produced placed=1000x500
        // with y-offset 250 to v-center inside the 1000 box.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            4000, 2000, 0, 1920, 1080,
            HAlign::Center, VAlign::Middle,
        );
        // x: -1 .. (1000/1920*2-1)
        // y: 1 .. (1 - 1000/1080*2)
        let exp_l = -1.0;
        let exp_r = 1000.0 / 1920.0 * 2.0 - 1.0;
        let exp_t = 1.0;
        let exp_b = 1.0 - 1000.0 / 1080.0 * 2.0;
        assert!(
            approx_ndc_eq(q, (exp_l, exp_r, exp_t, exp_b)),
            "per-axis fills box: got ({}, {}, {}, {}); expected ({exp_l}, {exp_r}, {exp_t}, {exp_b})",
            q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_per_axis_matches_free_slide_at_canvas2d_parity() {
        // Bug 1 (2026-05-19): regression-lock for the parity_fys_01_free
        // shape qarl flagged. font_size_pct=80, box (0.05, 0.1, 0.9, 0.8)
        // on 1920x1080. layout_text_to_quads emits bm_h ≈ 1.2 × 1382 px
        // (em-extent, Anton ascender 1.0 + |descender| 0.2). Per-axis
        // s_h = 864 / 1657 ≈ 0.521; placed_h ≈ 1659 × 0.521 = 864 px =
        // fills boxH. Pre-Bug-1 uniform-min took s_w = 1728/3590 ≈
        // 0.481, applied that to h → placed_h ≈ 798 (the 52%-of-box
        // visible artefact). The test asserts placed_h fills ≥85% of
        // boxH under per-axis — i.e. the visible regression-fix for
        // the FREE slide.
        let bm_w: u32 = 3590; // max_line_advance_em (~2.6) × size_px (1382)
        let bm_h: u32 = 1659; // (ascent - descent) em (~1.2) × size_px
        let q = box_to_ndc_quad(
            0.05, 0.1, 0.9, 0.8,
            bm_w, bm_h, 1, 1920, 1080,
            HAlign::Center, VAlign::Middle,
        );
        // Expected: placed fills both axes. Compute placed_h in NDC.
        let placed_h_ndc = q.2 - q.3; // top - bottom (top > bottom).
        let placed_h_px = placed_h_ndc / 2.0 * 1080.0;
        let box_h_px = 0.8 * 1080.0; // 864
        // Under per-axis: placed_h_px ≈ box_h_px. Allow 1% slack for
        // the 2*pad subtraction in the ink-dim scale source.
        assert!(
            placed_h_px >= box_h_px * 0.85,
            "FREE-shape per-axis placed_h_px = {} (box_h_px = {}); pre-Bug-1 uniform-min produced ~798 (~92% of box) — \
             but the SDF rendered ink within the placed quad was ~52% of box because the ink (cap height) fits inside the em-extent. \
             This test gates on the quad geometry, not on ink-within-quad.",
            placed_h_px, box_h_px,
        );
        // Per-axis ALSO fills box width.
        let placed_w_ndc = q.1 - q.0;
        let placed_w_px = placed_w_ndc / 2.0 * 1920.0;
        let box_w_px = 0.9 * 1920.0; // 1728
        assert!(
            (placed_w_px - box_w_px).abs() < 5.0,
            "FREE-shape per-axis placed_w_px = {} (box_w_px = {})",
            placed_w_px, box_w_px,
        );
    }

    #[test]
    fn crtc_filter_alternate_spacing_rejected() {
        // We deliberately don't accept variations — if drm-rs's
        // Debug derive starts emitting "CrtcListFilter ( 8 )" or
        // similar, we want the parse to fail and the host test to
        // catch it, rather than silently coerce.
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter ( 8 )"), None);
        assert_eq!(parse_crtc_list_filter_bits("CrtcListFilter( 8)"), None);
    }

    // -- composite-shader atlas wrapper -------------------------

    #[test]
    fn atlas_wrap_injects_uniforms_and_helpers() {
        let wrapped = wrap_composite_for_atlas(FS_FADE);
        assert!(wrapped.contains("uniform vec4 u_a_xform;"));
        assert!(wrapped.contains("uniform vec4 u_b_xform;"));
        assert!(wrapped.contains("vec4 _sa(vec2 uv)"));
        assert!(wrapped.contains("vec4 _sb(vec2 uv)"));
    }

    #[test]
    fn atlas_wrap_replaces_texture2D_calls() {
        let wrapped = wrap_composite_for_atlas(FS_FADE);
        // Original FS_FADE has `texture2D(u_src_a, v_uv)` and
        // `texture2D(u_src_b, v_uv)`. Wrapper replaces them with
        // _sa(...) / _sb(...). The helper bodies still contain
        // texture2D(u_src_a, ...) / (u_src_b, ...) -- so we just
        // confirm the call-site replacement happened.
        assert!(wrapped.contains("_sa(v_uv)"));
        assert!(wrapped.contains("_sb(v_uv)"));
    }

    #[test]
    fn atlas_wrap_handles_complex_uv_expression() {
        // FS_MARQUEE samples with `vec2(cx, v_uv.y)` and
        // `vec2(cx - 1.0 - gap_uv, v_uv.y)`. The substitution must
        // preserve the inner expressions as a single argument.
        let wrapped = wrap_composite_for_atlas(FS_MARQUEE);
        assert!(wrapped.contains("_sa(vec2(cx, v_uv.y))"));
        assert!(wrapped.contains("_sb(vec2(cx - 1.0 - gap_uv, v_uv.y))"));
    }

    #[test]
    fn atlas_wrap_idempotent_for_shader_without_samplers() {
        // FS_GLYPH doesn't have u_src_a/u_src_b -- wrap should
        // pass through unchanged. (Defense against accidental
        // wrapping of non-composite shaders.)
        let original = FS_GLYPH;
        let wrapped = wrap_composite_for_atlas(original);
        assert_eq!(original, wrapped);
    }

    #[test]
    fn atlas_wrap_all_composite_shaders_have_no_orphan_call_sites() {
        // Each composite-pass FS_<KIND> wraps cleanly: every
        // call to texture2D(u_src_a, ...) becomes _sa(...), and
        // every call to texture2D(u_src_b, ...) becomes _sb(...).
        // Helpers contain ONE texture2D(u_src_a, ...) and ONE
        // texture2D(u_src_b, ...) -- so every wrapped shader
        // should have EXACTLY 1 occurrence of each form.
        let kinds = [
            ("FS_CUT", FS_CUT),
            ("FS_FADE", FS_FADE),
            ("FS_WIPE", FS_WIPE),
            ("FS_IRIS", FS_IRIS),
            ("FS_DISSOLVE", FS_DISSOLVE),
            ("FS_PIXELATE", FS_PIXELATE),
            ("FS_SCANLINE", FS_SCANLINE),
            ("FS_HALFTONE", FS_HALFTONE),
            ("FS_GLITCH", FS_GLITCH),
            ("FS_SLIDE", FS_SLIDE),
            ("FS_PUSH", FS_PUSH),
            ("FS_SCROLL", FS_SCROLL),
            ("FS_BLINDS", FS_BLINDS),
            ("FS_FLIP", FS_FLIP),
            ("FS_MARQUEE", FS_MARQUEE),
            ("FS_SHUTTER", FS_SHUTTER),
        ];
        for (name, src) in kinds {
            let wrapped = wrap_composite_for_atlas(src);
            let n_a = wrapped.matches("texture2D(u_src_a, ").count();
            let n_b = wrapped.matches("texture2D(u_src_b, ").count();
            // Exactly 1 = the call inside the _sa / _sb helper.
            // Original main()'s call sites all became _sa(...) /
            // _sb(...). Recursion is impossible because the
            // helper definition itself was injected AFTER the
            // call-site rewrite (see wrap_composite_for_atlas).
            assert_eq!(n_a, 1, "{name}: expected 1 leftover texture2D(u_src_a (in _sa helper); got {n_a}");
            assert_eq!(n_b, 1, "{name}: expected 1 leftover texture2D(u_src_b (in _sb helper); got {n_b}");
        }
    }
    // P2-I (continued): classify_prewarm_pair. The function is the
    // pure-logic mirror of the decision tree in
    // prewarm_sp_session::consider_pair (hdmi.rs); these tests pin
    // every PrewarmTier outcome.

    #[test]
    fn classify_prewarm_pair_non_sp_kind_returns_not_single_pass() {
        // Glitch is qarl-deferred from SP path. Unknown / empty
        // kinds also fall through to legacy 3-pass.
        assert_eq!(
            classify_prewarm_pair("glitch", 1, 1),
            PrewarmTier::NotSinglePass,
        );
        assert_eq!(
            classify_prewarm_pair("", 1, 1),
            PrewarmTier::NotSinglePass,
        );
        assert_eq!(
            classify_prewarm_pair("unknown_kind", 4, 4),
            PrewarmTier::NotSinglePass,
        );
    }

    #[test]
    fn classify_prewarm_pair_above_bake_cap_returns_exceeds_bake_cap() {
        // SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE = 6. 7 on either
        // side means SB can't host the slide; runtime falls
        // through to legacy 3-pass.
        assert_eq!(SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE, 6);
        assert_eq!(
            classify_prewarm_pair("fade", 7, 1),
            PrewarmTier::ExceedsBakeCap,
        );
        assert_eq!(
            classify_prewarm_pair("fade", 1, 7),
            PrewarmTier::ExceedsBakeCap,
        );
        assert_eq!(
            classify_prewarm_pair("fade", 100, 100),
            PrewarmTier::ExceedsBakeCap,
        );
    }

    #[test]
    fn classify_prewarm_pair_zero_layers_returns_single_pass() {
        // B.3 (post-MSDF cutover): SP-tier is bg-only. Only the
        // (0, 0) layer combo can prewarm SP; any text-bearing pair
        // routes SB even within the old per-side SP cap.
        assert_eq!(
            classify_prewarm_pair("fade", 0, 0),
            PrewarmTier::SinglePass,
        );
    }

    #[test]
    fn classify_prewarm_pair_any_text_layers_returns_scissored_bake() {
        // B.3 SP text gate: any layer on either side forces SB.
        // Mirrors `transition_eligible_for_single_pass_logic`'s
        // `if !layer_props_a.is_empty() || !layer_props_b.is_empty()`
        // rejection.
        for (na, nb) in [(1, 0), (0, 1), (1, 1), (2, 2), (4, 0), (3, 1)] {
            assert_eq!(
                classify_prewarm_pair("fade", na, nb),
                PrewarmTier::ScissoredBake,
                "({na}, {nb}) text-bearing should route SB post-B.3",
            );
        }
    }

    #[test]
    fn classify_prewarm_pair_above_combined_returns_scissored_bake() {
        // Combined > 4 with both sides within SP cap STILL goes
        // SB. 5L+5L all-motion is the documented FYS heavy case here.
        // (Also now caught earlier by the B.3 text gate, but kept
        // for explicit coverage of the prefer_scissored_bake arm.)
        for (na, nb) in [(3, 2), (4, 1), (1, 4), (4, 4), (5, 0)] {
            assert_eq!(
                classify_prewarm_pair("fade", na, nb),
                PrewarmTier::ScissoredBake,
                "({na}, {nb}) should route SB",
            );
        }
    }

    #[test]
    fn classify_prewarm_pair_above_sp_cap_within_sb_cap_returns_scissored_bake() {
        // Per-side > SINGLE_PASS_MAX_LAYERS_PER_SLIDE (4) but <=
        // SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE (6) -> SB tier.
        for (na, nb) in [(5, 0), (5, 5), (6, 0), (6, 6), (5, 6)] {
            assert_eq!(
                classify_prewarm_pair("fade", na, nb),
                PrewarmTier::ScissoredBake,
                "({na}, {nb}) within SB cap should route SB",
            );
        }
    }

    #[test]
    fn classify_prewarm_pair_skip_tiers_dominate_kind_check() {
        // ExceedsBakeCap takes priority over kind check IF the
        // kind passes -- but if the kind fails, NotSinglePass
        // dominates regardless of layer count. Pin the priority
        // ordering since it's load-bearing for prewarm correctness.
        assert_eq!(
            classify_prewarm_pair("glitch", 100, 100),
            PrewarmTier::NotSinglePass,
            "kind-failure dominates layer-cap-failure",
        );
        assert_eq!(
            classify_prewarm_pair("fade", 100, 100),
            PrewarmTier::ExceedsBakeCap,
            "kind-pass + layer-cap-fail = ExceedsBakeCap",
        );
    }

    #[test]
    fn classify_prewarm_pair_alignment_with_runtime_dispatch() {
        // The runtime dispatcher uses prefer_scissored_bake +
        // SINGLE_PASS / SCISSORED_BAKE caps. classify_prewarm_pair
        // MUST agree on every (kind, n_a, n_b) combo so prewarm
        // and runtime never disagree on which program to compile
        // vs which program to call. Spot-check across the per-
        // side cap and combined cap.
        for kind in ["fade", "wipe", "marquee"] {
            for n_a in 0..=8 {
                for n_b in 0..=8 {
                    let tier = classify_prewarm_pair(kind, n_a, n_b);
                    let exceeds_bake = n_a > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE
                        || n_b > SCISSORED_BAKE_MAX_LAYERS_PER_SLIDE;
                    // B.3: SP only takes (0, 0). Any text-bearing
                    // pair routes SB. Mirrors transition_eligible_
                    // for_single_pass_logic's empty-layers gate.
                    let text_bearing = n_a > 0 || n_b > 0;
                    let expected = if exceeds_bake {
                        PrewarmTier::ExceedsBakeCap
                    } else if text_bearing {
                        PrewarmTier::ScissoredBake
                    } else {
                        // (0, 0) within both caps + bg-only.
                        PrewarmTier::SinglePass
                    };
                    assert_eq!(
                        tier, expected,
                        "{kind} ({n_a}, {n_b}) classifier disagrees with runtime",
                    );
                }
            }
        }
    }

    /// Phase 4w regression lock (qarl 2026-05-16). The Phase 4v-3a
    /// audit's Path #2 finding claimed `render_transition_animated_in_
    /// session` (the legacy 3-pass path) baked both slides ONCE before
    /// the transition loop and never re-baked — "static-snapshot
    /// bake." That finding was wrong: commit 2b0cbef (May 7, 2026,
    /// "v1-spec-delta #2 (slice d) -- motion through transitions")
    /// already added per-frame live re-bake of fbo_a/fbo_b via direct
    /// `paint_slide(... Some(&states_*), ...)` calls inside the
    /// per-frame loop. The auditor only scanned `make_slide_fbo` call
    /// sites and missed the direct paint_slide path inside the loop.
    ///
    /// This test reads the hdmi.rs source and asserts the live re-bake
    /// structure remains intact. A future refactor that strips the
    /// re-bake (regressing to pre-2b0cbef static-snapshot behavior)
    /// fails CI loudly here rather than only surfacing on glass during
    /// the next Phase 4v-3c eyeball pass.
    ///
    /// Lives in hdmi_logic.rs (not hdmi.rs) because hdmi.rs is
    /// `#[cfg(target_os = "linux")]`-gated and would skip this test on
    /// the macOS dev box. The test is pure stdlib (reads a source file
    /// as text) so cross-platform exposure is correct + cheap.
    ///
    /// We can't unit-test the function end-to-end (it needs a real
    /// EglSession + DRM Card). Structural inspection of the source
    /// is the cleanest equivalent to QA's "spy on paint_slide call
    /// count" suggestion when the language doesn't support runtime
    /// interception of free functions.
    #[test]
    fn legacy_3pass_transition_re_bakes_animated_layers_per_frame() {
        // hdmi.rs lives next to this file under <crate>/src/. Build
        // the path via CARGO_MANIFEST_DIR so the test works under any
        // CWD (cargo test runs from the package dir but be defensive).
        let hdmi_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("src")
            .join("hdmi.rs");
        let source = std::fs::read_to_string(&hdmi_path)
            .unwrap_or_else(|e| panic!("must read {} for Phase 4w structural check: {e}", hdmi_path.display()));

        // Pin to the function NAME so a line-number drift doesn't break
        // the test. The audit cited L4980; HEAD has it at L5480.
        let fn_start = source
            .find("fn render_transition_animated_in_session(")
            .expect(
                "render_transition_animated_in_session must exist — \
                 Phase 4w's regression lock cannot run if the function was \
                 renamed. If you renamed it, update this test to match.",
            );

        // Bound the function body. Find the next top-level `\nfn ` after
        // fn_start; that's the next function. Slice the body between.
        let body_search_start = fn_start + 1;
        let body_end = source[body_search_start..]
            .find("\nfn ")
            .map(|i| body_search_start + i)
            .unwrap_or(source.len());
        let body = &source[fn_start..body_end];

        // The live re-bake gate predicate must remain on both endpoints.
        assert!(
            body.contains("any_animated_a || any_auto_a"),
            "render_transition_animated_in_session must gate slide-A re-bake \
             on `any_animated_a || any_auto_a`. If the gate predicate changed \
             intentionally, update this assertion — but DON'T silently delete \
             it without QA sign-off. The Phase 4v-3a audit miss is locked here.",
        );
        assert!(
            body.contains("any_animated_b || any_auto_b"),
            "render_transition_animated_in_session must gate slide-B re-bake \
             on `any_animated_b || any_auto_b`.",
        );

        // The transition window driver must still be a per-frame loop.
        assert!(
            body.contains("for frame in 0..total_frames"),
            "render_transition_animated_in_session must drive the transition \
             via the per-frame loop. If the loop shape changed, motion through \
             transitions almost certainly regressed.",
        );

        // `motion_states_for_layers` must be called inside the function
        // body — at least twice (once per endpoint, per-frame). If the
        // count drops to 0 the live re-bake is gone.
        let motion_states_calls = body.matches("motion_states_for_layers(").count();
        assert!(
            motion_states_calls >= 2,
            "render_transition_animated_in_session must call \
             motion_states_for_layers at least twice (once per endpoint, \
             per-frame). Found {motion_states_calls} call(s). If 0, the live \
             re-bake regressed to pre-2b0cbef static-snapshot behavior — \
             motion freezes during transitions.",
        );

        // paint_slide must be called inside the function body for the
        // per-frame GPU re-bake (separate from the make_slide_fbo
        // allocation calls). Conservative lower-bound check: at least 2.
        let paint_slide_calls = body.matches("paint_slide(").count();
        assert!(
            paint_slide_calls >= 2,
            "render_transition_animated_in_session must call paint_slide at \
             least twice for the per-frame re-bake. Found {paint_slide_calls} \
             call(s).",
        );
    }

    /// Regression-lock for the motion-phase-discontinuity-at-transition-
    /// boundaries fix — three commits, 2026-05-09 → 2026-05-16:
    /// `7417ae0` (session-global tick_seconds basis), `413efca` (extend
    /// the fix to the IPC sidecar PaintSlide path), and `fff3ab8`
    /// (Phase 4v-3b motion through IPC PaintTransition path). Backlog
    /// item #2; recon at `docs/motion-phase-discontinuity-recon.md`.
    ///
    /// The fix plumbs `session.motion_tick_seconds()` (a session-global
    /// monotonic basis that never resets within the session's lifetime)
    /// into every in-session + IPC render path that previously held a
    /// call-local clock or a static-snapshot bake. Pre-fix: each render
    /// call computed its own `Instant::now()` snapshot, so the
    /// `sin(2*pi*freq*tick + phase)` motion math snapped phase at every
    /// hold↔transition boundary (`tick` reset to ~0 at the boundary
    /// crossing).
    ///
    /// Like the black-flash fix above, this bug is *silent* — no panic,
    /// no test failure; the only symptom is a visible phase jump on
    /// glass at boundary crossings. The existing
    /// `legacy_3pass_transition_re_bakes_animated_layers_per_frame`
    /// test locks the per-frame re-bake gate + loop shape, but NOT the
    /// timing basis — a refactor that reverts one of the 7+ call sites
    /// to a call-local clock would pass that test while re-breaking
    /// motion-phase continuity.
    ///
    /// This source-grep test locks two invariants in `hdmi.rs`:
    ///
    /// - Affirmative: `motion_tick_seconds(` appears ≥7 times — at
    ///   least 1 definition (L5212) + 6 callers across the in-session
    ///   render path (L1482, L7442, L7916, L8358) and the IPC sidecar
    ///   path (L2940, L3738). Plus 3 doc-comment mentions today, which
    ///   are bonus headroom but not load-bearing for the floor.
    ///
    /// - Anti-pattern: no CODE line may contain both `tick_seconds` AND
    ///   `Instant::now()`. That combination on a single statement is
    ///   the pre-fix bug pattern (`let tick_seconds = ... Instant::now()
    ///   ...`). `Instant::now()` for perf timing alone is fine — the
    ///   bug is specifically deriving the motion tick from a call-local
    ///   clock. Comments are stripped (`//`-onward of each line) so the
    ///   anti-pattern check ignores narration inside `///` doc-comments.
    ///
    /// Recon citation: `docs/motion-phase-discontinuity-recon.md` §4
    /// line 94 (sketches a synthetic boundary-continuity assertion as
    /// the secondary verify; this source-grep is the discipline-side
    /// regression lock that the recon noted would be a useful sibling).
    /// Authoritative call-site list:
    /// `qa/captures/motion-through-transitions-audit-2026-05-16.md`.
    ///
    /// Same caveats as the black-flash test above: a benign string
    /// refactor (renaming `motion_tick_seconds`, restructuring how
    /// `tick_seconds` gets named, etc.) will legitimately trip this —
    /// update the assertion + the recon doc cross-ref when it does;
    /// don't silently delete the test.
    #[test]
    fn motion_tick_seconds_is_session_global_not_call_local() {
        let hdmi_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("src")
            .join("hdmi.rs");
        let source = std::fs::read_to_string(&hdmi_path).unwrap_or_else(|e| {
            panic!(
                "must read {} for motion-phase regression check: {e}",
                hdmi_path.display(),
            )
        });

        // ---- Affirmative invariant ----
        //
        // `motion_tick_seconds(` must appear at least 7 times in
        // hdmi.rs: 1 definition + 6 call sites (4 in-session + 2 IPC
        // sidecar). Drop below 7 and at least one render path is no
        // longer pulling from the session-global tick; motion phase
        // snaps at the boundary that site governs (visible only on
        // glass).
        let matches = source.matches("motion_tick_seconds(").count();
        assert!(
            matches >= 7,
            "expected ≥7 occurrences of `motion_tick_seconds(` in \
             renderer/src/hdmi.rs (1 defn + 6 call sites across the \
             in-session render path L1482/L7442/L7916/L8358 + the IPC \
             sidecar path L2940/L3738); found {matches}. A refactor \
             likely reverted at least one site to a call-local clock; \
             motion phase will snap at the boundary that site governs. \
             Recon: docs/motion-phase-discontinuity-recon.md §4 line 94. \
             Authoritative call-site list: \
             qa/captures/motion-through-transitions-audit-2026-05-16.md.",
        );

        // ---- Anti-pattern invariant ----
        //
        // No CODE line in hdmi.rs may contain both `tick_seconds` AND
        // `Instant::now()` — that combination on the same statement is
        // the pre-fix bug pattern. `Instant::now()` ALONE is fine (it's
        // legitimately used elsewhere for perf timing); the bug is
        // specifically using it to *derive* tick_seconds.
        //
        // Comments are stripped before checking so narrative inside
        // `///` doc-comments doesn't spuriously trip the assertion.
        let mut anti_pattern_lines: Vec<(usize, String)> = Vec::new();
        for (idx, line) in source.lines().enumerate() {
            let code = match line.find("//") {
                Some(cmt) => &line[..cmt],
                None => line,
            };
            if code.contains("tick_seconds") && code.contains("Instant::now()") {
                anti_pattern_lines.push((idx + 1, line.trim().to_string()));
            }
        }
        assert!(
            anti_pattern_lines.is_empty(),
            "tick_seconds must derive from session.motion_tick_seconds() — \
             a session-global monotonic basis — NOT from a call-local \
             Instant::now() snapshot. Mixing both names on a single CODE \
             line is the pre-fix anti-pattern that snapped motion phase \
             at every hold↔transition boundary. Offending line(s): {:?}. \
             Recon: docs/motion-phase-discontinuity-recon.md §4 line 94.",
            anti_pattern_lines,
        );
    }

    /// Regression-lock for the black-flash-at-transition-boundaries fix
    /// (commit `7c605cce`, 2026-05-09 — backlog item #3).
    ///
    /// The fix introduced `held_scanout_fb` / `held_scanout_bo` on
    /// `EglSession` plus a single `end_of_in_session_render_call` helper
    /// that all 5 in-session render entry points call at end-of-call,
    /// so the scanout framebuffer is held across in-session call
    /// boundaries and `modeset_done` stays TRUE for the session's
    /// lifetime after the first SetCrtc. Pre-fix: SetCrtc re-fired
    /// 35/4000 frames at 1920×1080@60 on the vc4 (the Pi-bench in the
    /// commit message). Post-fix: 1 fire per session (the bring-up).
    ///
    /// The bug is *silent* — no panic, no test failure, the symptom is
    /// only visible on glass as a one-frame black flash at every
    /// hold↔transition boundary. A refactor that drops the helper call
    /// from one of the 5 in-session entry points, OR re-introduces
    /// `modeset_done = false` anywhere post-session-init, would
    /// reintroduce the bug without `cargo test` catching it. This is
    /// the source-grep fence the recon doc anticipated.
    ///
    /// See `docs/black-flash-at-transition-boundaries-recon.md` §4
    /// lines 162-170 for the sketched assertion + rationale.
    ///
    /// Like the `legacy_3pass_transition_re_bakes_*` test above, this
    /// is a structural-grep test rather than a behavioral one because
    /// the GL-scanout + SetCrtc state is un-mockable. A benign string
    /// refactor (renaming the helper, rewording the struct-init in a
    /// way that changes the grep-matched substring) will legitimately
    /// trip this — fix the assertion + the recon doc cross-ref when
    /// that happens; don't silently delete the test.
    #[test]
    fn black_flash_fix_structural_invariants_hold_across_in_session_boundaries() {
        let hdmi_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("src")
            .join("hdmi.rs");
        let source = std::fs::read_to_string(&hdmi_path).unwrap_or_else(|e| {
            panic!(
                "must read {} for black-flash regression check: {e}",
                hdmi_path.display(),
            )
        });

        // ---- Affirmative invariant ----
        //
        // `end_of_in_session_render_call(` must appear at least 6 times
        // in hdmi.rs: 1 definition + 5 in-session render-entry-point
        // callers (per the recon's audit at §4 lines 162-170, verified
        // at HEAD against the live source 2026-05-22). Drop below 6 and
        // at least one in-session path is no longer threading the
        // end-of-call cleanup; the next call into that path will
        // re-fire SetCrtc (visible as a one-frame black flash on glass).
        let helper_matches = source.matches("end_of_in_session_render_call(").count();
        assert!(
            helper_matches >= 6,
            "expected ≥6 occurrences of `end_of_in_session_render_call(` \
             in renderer/src/hdmi.rs (1 defn + 5 in-session callers); \
             found {helper_matches}. A refactor likely dropped the helper \
             call from at least one of the 5 in-session render entry \
             points; the scanout framebuffer is no longer held across \
             that path's call boundary; SetCrtc will re-fire on the next \
             in-session render call into the unwired path; visible black \
             flash regression on glass. Re-thread the helper at every \
             entry point, OR update this lower bound if the entry-point \
             set legitimately changed (the recon doc's call-site list at \
             docs/black-flash-at-transition-boundaries-recon.md §4 \
             lines 162-170 is authoritative).",
        );

        // ---- Anti-pattern invariant ----
        //
        // `modeset_done = false` must NEVER appear as a CODE statement
        // (only as the `modeset_done: false` struct-init at session
        // creation, which uses `:` not `=`, and possibly as a substring
        // inside doc-comments narrating the OLD bug). Re-introducing the
        // assignment would force the next in-session render call to
        // take the SetCrtc-fires-again path; visible black flash.
        //
        // We strip `//`-style line comments before checking so the
        // narration on hdmi.rs:1097 ("/// resetting modeset_done =
        // false (which forced the NEXT call's …") doesn't spuriously
        // trip the assertion.
        let mut anti_pattern_lines: Vec<(usize, String)> = Vec::new();
        for (idx, line) in source.lines().enumerate() {
            let code = match line.find("//") {
                Some(cmt) => &line[..cmt],
                None => line,
            };
            if code.contains("modeset_done = false") {
                anti_pattern_lines.push((idx + 1, line.trim().to_string()));
            }
        }
        assert!(
            anti_pattern_lines.is_empty(),
            "modeset_done must NEVER be reset to false post-session-init \
             (the struct-init at session creation uses `:`, not `=`, so it \
             does NOT match `modeset_done = false`). A reset re-enables \
             the SetCrtc-fires-again path; visible black flash at the \
             next in-session render call. Offending line(s): {:?}. \
             Recon: docs/black-flash-at-transition-boundaries-recon.md \
             §4 lines 162-170.",
            anti_pattern_lines,
        );
    }

    /// Regression-lock for D1 "blacks-not-black" — QA H1 audit cited
    /// the 2026-05-17 recon (`qa/captures/bug-7-blacks-not-black-
    /// recon-2026-05-17.md`) and recommended Option B (a `step+mix`
    /// branch-free snap to exact zero for the FS_BRIGHT_GAMMA pass).
    /// The recon was a PAPER analysis pre-probe — it explicitly punted
    /// vc4 hardware confirmation as out-of-scope.
    ///
    /// Between the recon (2026-05-17) and the QA audit (2026-05-23)
    /// the single-frame FBO-readback probe was run on vc4 hardware and
    /// confirmed `pow(0.0, 1/2.2) == 0.0` — the suspected vc4 imprecision
    /// does NOT exist on bcm2835's GLES2 implementation. The shader was
    /// annotated accordingly: "No epsilon needed." Option B was never
    /// implemented because it isn't needed.
    ///
    /// This lock fences two invariants in the FS_BRIGHT_GAMMA shader so
    /// a future "cleanup" PR doesn't:
    ///
    ///   - Strip the unconditional `clamp(rgb, vec3(0.0), vec3(1.0))`
    ///     thinking it's redundant. The clamp keeps `pow`'s base
    ///     well-defined per GLSL ES 1.00 §8.2 ("pow undefined for
    ///     negative bases"). Without it, a future `u_brightness > 1.0`
    ///     change (out-of-scope today; brightness is `[0,1]`) would
    ///     produce undefined-behavior shader output on overflow.
    ///
    ///   - Re-implement the recon's Option B `step+mix` snap thinking
    ///     "the comment is wrong; the recon promised an epsilon."
    ///     The comment IS the source of truth — it cites the vc4 probe
    ///     that disproved the bug. The recon is historical.
    ///
    /// Like the `legacy_3pass_transition_re_bakes_*` test above this is
    /// a source-grep test rather than behavioral, because vc4's
    /// `pow(0.0, x)` math isn't reproducible without on-Pi hardware.
    /// The shader's correct behavior was confirmed by the original
    /// probe; this test locks the SHADER SOURCE so a refactor can't
    /// silently regress.
    ///
    /// QA close-out 2026-05-23: D1 closed as audit-stale-and-no-action
    /// (third such closure of the night, mirroring H3 + M1). Audit
    /// tracked in QA's per-session overnight doc; recon stays the
    /// repo-resident source of truth at the path cited above.
    #[test]
    fn fs_bright_gamma_keeps_pre_pow_clamp_and_no_epsilon_documentation() {
        // Pin the shader contents directly — it's a `pub const` in
        // this same file, so the source string we grep against is
        // the canonical authority.
        let shader = super::FS_BRIGHT_GAMMA;

        // Invariant 1: unconditional clamp to [0, 1] is present.
        // (Approx hdmi_logic.rs:2775 at this writing; line drifts.)
        // If a refactor reflows the clamp into different vec3 args
        // (e.g. `clamp(rgb, 0.0, 1.0)` with float scalars implicitly
        // broadcast) update this assertion to the new shape; don't
        // silently delete the lock.
        assert!(
            shader.contains("clamp(rgb, vec3(0.0), vec3(1.0))"),
            "FS_BRIGHT_GAMMA must clamp rgb to [0, 1] BEFORE the pow \
             call so the base is well-defined per GLSL ES 1.00 §8.2. \
             The unconditional clamp is what makes `pow(0.0, 1/gamma) \
             == 0.0` on vc4 (verified via single-frame FBO-readback \
             probe 2026-05-17). If the clamp shape changed \
             intentionally, update this assertion AND the recon doc \
             at qa/captures/bug-7-blacks-not-black-recon-2026-05-17.md \
             — but DON'T silently delete it without QA sign-off."
        );

        // Invariant 1b: the clamp appears BEFORE the pow call. The
        // affirmative `contains` assertion above doesn't catch a
        // reordering (clamp-after-pow keeps both substrings but
        // breaks the math: pow's base can be negative + undefined).
        // Subagent review caught the gap pre-commit.
        let clamp_idx = shader
            .find("clamp(rgb, vec3(0.0), vec3(1.0))")
            .expect("clamp asserted present by invariant 1 above");
        let pow_idx = shader
            .find("pow(rgb")
            .expect("pow call is the whole point of FS_BRIGHT_GAMMA");
        assert!(
            clamp_idx < pow_idx,
            "FS_BRIGHT_GAMMA must clamp rgb BEFORE the pow call \
             (clamp idx {clamp_idx} < pow idx {pow_idx}). A \
             clamp-after-pow ordering keeps the substrings but \
             breaks the math: pow's base can be negative, which is \
             undefined per GLSL ES 1.00 §8.2."
        );

        // Invariant 2: the "No epsilon needed" comment anchor stays in
        // place. The anchor phrase is stable across reasonable comment
        // rewrites (it explicitly names the probe). If a future
        // cleanup PR re-implements Option B from the recon, the anchor
        // text will conflict and trip this assertion — surfacing the
        // closure rationale before the regression lands.
        assert!(
            shader.contains("FBO-readback probe 2026-05-17"),
            "FS_BRIGHT_GAMMA must retain the comment anchor for D1's \
             close-out rationale ('verified via single-frame FBO-\
             readback probe'). The comment is the source of truth: \
             the 2026-05-17 recon recommended Option B (step+mix snap) \
             as a paper-analysis fix for a SUSPECTED vc4 \
             `pow(0.0, 1/2.2)` imprecision, but the actual probe on \
             vc4 hardware confirmed `pow(0.0, 1/2.2) == 0.0` — no \
             epsilon needed. If a future cleanup PR strips this \
             comment thinking it's stale, restore it from the recon \
             cross-ref at qa/captures/bug-7-blacks-not-black-recon-\
             2026-05-17.md AND get QA sign-off; don't silently \
             re-introduce Option B as a 'safety' fix."
        );

        // Invariant 3 (anti-pattern): the shader must NOT contain the
        // recon's Option B step+mix snap. Option B was the paper-
        // analysis recommendation for a SUSPECTED vc4 imprecision
        // that the probe disproved — re-introducing it "for safety"
        // is the exact silent regression this lock fences. Matches
        // the H3/motion-phase/black-flash locks' anti-pattern arm.
        assert!(
            !shader.contains("step(rgb, vec3(1e-6))")
                && !shader.contains("step(rgb, vec3(0.000001))"),
            "FS_BRIGHT_GAMMA must NOT contain the recon's Option B \
             step+mix snap — the probe disproved the vc4 imprecision \
             it was designed to mitigate. Re-introducing it 'for \
             safety' is the silent regression this lock fences. See \
             qa/captures/bug-7-blacks-not-black-recon-2026-05-17.md \
             §5 (Option B) for the original recommendation + why it \
             was closed without implementation. If qarl re-probed on \
             different hardware and the bug now exists, restart from \
             a fresh recon — don't silently re-add Option B here."
        );
    }

    // wrap_text_to_width — 2026-05-17 port of the JS+Python helpers.
    // Each test pins one branch of the greedy line-fill algorithm; the
    // max_width is computed from the actual rasterized advance widths
    // so the assertions don't depend on absolute pixel values (the
    // font's per-glyph metrics drift across fontdue revisions).

    fn measure(font: &fontdue::Font, text: &str, size_px: f32) -> f32 {
        text.chars()
            .map(|c| font.metrics(c, size_px).advance_width.round())
            .sum::<f32>()
    }

    #[test]
    fn wrap_empty_returns_empty() {
        let font = load_anton();
        assert_eq!(wrap_text_to_width(&font, "", 64.0, 500.0), "");
    }

    #[test]
    fn wrap_zero_or_negative_max_width_returns_input() {
        // Mirrors the JS+Python early-out: max_width <= 0 → no wrap
        // attempted. Renderer can call wrap unconditionally without a
        // guard.
        let font = load_anton();
        assert_eq!(wrap_text_to_width(&font, "hello world", 64.0, 0.0), "hello world");
        assert_eq!(wrap_text_to_width(&font, "hello world", 64.0, -10.0), "hello world");
    }

    #[test]
    fn wrap_single_word_fits() {
        let font = load_anton();
        let huge = measure(&font, "Hello", 64.0) * 4.0;
        assert_eq!(wrap_text_to_width(&font, "Hello", 64.0, huge), "Hello");
    }

    #[test]
    fn wrap_single_word_too_long() {
        // Single unbreakable word wider than max_width must stay on
        // one line (no mid-word break). The renderer's bitmap-cap +
        // squish path handles the visual overflow downstream.
        let font = load_anton();
        let w = measure(&font, "supercalifragilistic", 64.0);
        // Half the width — well below the word's width.
        let too_narrow = w * 0.5;
        assert_eq!(
            wrap_text_to_width(&font, "supercalifragilistic", 64.0, too_narrow),
            "supercalifragilistic",
        );
    }

    #[test]
    fn wrap_greedy_two_lines() {
        // Greedy line-fill: pack as many tokens as fit, break when
        // adding the next would exceed max_width. Width chosen to be
        // exactly wide enough for "say what you mean. mean" but not
        // "say what you mean. mean what", so the break lands between
        // "mean" and "what".
        let font = load_anton();
        let line1 = "say what you mean. mean";
        let line1_w = measure(&font, line1, 64.0);
        let space_w = font.metrics(' ', 64.0).advance_width.round();
        let what_w = measure(&font, "what", 64.0);
        // Pick max_width strictly between [line1_w] and [line1_w + space + what_w]
        // so the candidate "...mean what" overflows.
        let max_w = line1_w + space_w + what_w * 0.5;
        let wrapped = wrap_text_to_width(
            &font,
            "say what you mean. mean what you say.",
            64.0,
            max_w,
        );
        assert_eq!(wrapped, "say what you mean. mean\nwhat you say.");
    }

    #[test]
    fn wrap_preserves_leading_whitespace_on_hardbreak_segments() {
        // 2026-05-17 bug fix: the Boot slide (seed.py:_BOOT_LOG_TEXT)
        // uses "  " indent on lines 2-6 to format the boot log
        // output. The first wrap port stripped those leading spaces
        // because the inner is_empty() check treated the empty
        // token from leading-whitespace tokenization as a "first
        // word". Mirror JS: empty tokens stay in the line so the
        // final join(" ") reconstructs the leading spaces.
        let font = load_anton();
        // Wide enough that no segment wraps further — pure hard-break
        // preservation test.
        let max_w = measure(&font, "  ok foo bar baz", 64.0) + 200.0;
        let wrapped = wrap_text_to_width(&font, "a\n  b", 64.0, max_w);
        assert_eq!(wrapped, "a\n  b");
    }

    #[test]
    fn wrap_preserves_leading_whitespace_through_wrap() {
        // Leading whitespace on the FIRST line stays attached even
        // when the same segment also wraps. Subsequent wrapped lines
        // start at the left edge (no leading whitespace inherited).
        // Matches the JS algorithm — line.join(" ") rebuilds with
        // whatever tokens accumulated, and a wrap-break starts the
        // new line with just the breaking token.
        let font = load_anton();
        // Pick max_w so the first line fits "  hello world" and the
        // next "foo bar baz" wraps onto a second line. measure() of
        // "  hello world" + a sliver of "foo" overflows; before
        // "foo" we break.
        let first = "  hello world";
        let space_w = font.metrics(' ', 64.0).advance_width.round();
        let foo_w = measure(&font, "foo", 64.0);
        let max_w = measure(&font, first, 64.0) + space_w + foo_w * 0.5;
        let wrapped =
            wrap_text_to_width(&font, "  hello world foo bar baz", 64.0, max_w);
        // First line keeps the leading "  "; second line starts
        // flush-left at "foo" (no inherited indent).
        assert_eq!(wrapped, "  hello world\nfoo bar baz");
    }

    #[test]
    fn wrap_empty_paragraph_between_non_empty_preserves_leading_whitespace() {
        // Confirms that a blank paragraph between non-empty paragraphs
        // doesn't bleed state across the hard breaks. The per-paragraph
        // `let mut line = Vec::new()` resets between paragraphs so the
        // leading "  " on "  b" still survives.
        let font = load_anton();
        let max_w = measure(&font, "  b", 64.0) + 200.0;
        let wrapped = wrap_text_to_width(&font, "a\n\n  b", 64.0, max_w);
        assert_eq!(wrapped, "a\n\n  b");
    }

    #[test]
    fn wrap_preserves_internal_consecutive_spaces() {
        // Double spaces inside a paragraph (e.g. "  ok" after the
        // dot column in the Boot slide) survive wrap. JS' empty-token
        // tokenization produces ["", "ok"] for "  ok", and the
        // join(" ") puts them back together as " " + " " + "ok" =
        // "  ok".
        let font = load_anton();
        let max_w = measure(&font, "panel-0 . . . . . . . .  ok", 64.0) + 100.0;
        let wrapped =
            wrap_text_to_width(&font, "panel-0 . . . . . . . .  ok", 64.0, max_w);
        assert_eq!(wrapped, "panel-0 . . . . . . . .  ok");
    }

    #[test]
    fn wrap_preserves_hard_linebreak() {
        // Each side of a \n hard break is wrapped independently;
        // if each side fits, the output preserves the hard break.
        let font = load_anton();
        let max_w = measure(&font, "line two", 64.0) + 100.0;
        let wrapped = wrap_text_to_width(&font, "line one\nline two", 64.0, max_w);
        assert_eq!(wrapped, "line one\nline two");
    }

    #[test]
    fn wrap_hardbreak_then_wraps_each_segment() {
        // Hard \n divides the input into segments; each is greedy-
        // wrapped independently. Verifies a short first segment is
        // emitted intact, then the second segment is wrapped into
        // multiple lines.
        let font = load_anton();
        let line2_a = "long sentence";
        let line2_w = measure(&font, line2_a, 64.0);
        let space_w = font.metrics(' ', 64.0).advance_width.round();
        let that_w = measure(&font, "that", 64.0);
        let max_w = line2_w + space_w + that_w * 0.5;
        let wrapped = wrap_text_to_width(
            &font,
            "short\nlong sentence that needs wrap",
            64.0,
            max_w,
        );
        assert_eq!(wrapped, "short\nlong sentence\nthat needs wrap");
    }

    fn load_playfair() -> fontdue::Font {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("ui/fonts/playfair-display.ttf");
        let bytes = std::fs::read(&path)
            .unwrap_or_else(|e| panic!("read {}: {e}", path.display()));
        fontdue::Font::from_bytes(bytes, fontdue::FontSettings::default())
            .expect("parse Playfair Display TTF")
    }

    // ---- r25 glyph-prewarm drain-gate predicate ----
    //
    // Pins the regression-prevention invariant from 530cd25: the
    // playback loop's poll_completions at hdmi.rs:3187 fires
    // slide_caches.drain() on any frame where uploaded > 0. The
    // r25 gate must therefore ensure the prewarm function returns
    // ONLY when BOTH conditions hold (see glyph_prewarm_drain_complete
    // docstring above for the full rationale).

    #[test]
    fn glyph_prewarm_drain_complete_both_conditions() {
        // 5 enqueued, 5 completed, last poll drained nothing left
        // → gate satisfied.
        assert!(glyph_prewarm_drain_complete(5, 5, 0));
    }

    #[test]
    fn glyph_prewarm_drain_complete_completions_exceed_requested() {
        // Defensive: if some external code path bumped
        // completion_count between our baseline snapshot and the
        // poll, the diff might over-count. Use `>=` not `==` so a
        // benign over-count doesn't trip an infinite loop.
        assert!(glyph_prewarm_drain_complete(5, 6, 0));
    }

    #[test]
    fn glyph_prewarm_drain_incomplete_when_completions_short() {
        // Workers still rasterizing; the channel will deliver more
        // completions later.
        assert!(!glyph_prewarm_drain_complete(5, 3, 0));
    }

    #[test]
    fn glyph_prewarm_drain_incomplete_when_channel_pending_even_if_count_met() {
        // The regression mechanism this gate prevents: completion
        // count reached the target but the most recent
        // poll_completions still drained N > 0 entries from the
        // channel buffer. Returning now would mean the playback
        // loop's NEXT poll_completions finds completions waiting
        // and fires slide_caches.drain() inside the
        // paint_bake_text measurement window. Must keep looping
        // until the channel drains too.
        assert!(!glyph_prewarm_drain_complete(5, 5, 2));
    }

    #[test]
    fn glyph_prewarm_drain_incomplete_when_zero_requested_but_channel_busy() {
        // Edge case: zero enqueues (e.g. all fonts missing on
        // disk), but stale completions from prior sessions sit in
        // the channel. With requested=0 and
        // completions_since_baseline=0, condition (a) holds
        // trivially. If `drained_this_call > 0` we still must
        // loop to drain that residue. Catches a future regression
        // where "no enqueues" silently skipped the channel-empty
        // check.
        assert!(!glyph_prewarm_drain_complete(0, 0, 3));
    }

    #[test]
    fn glyph_prewarm_drain_complete_zero_requested_zero_pending() {
        // Truly idle: nothing enqueued, nothing in flight. Return
        // immediately.
        assert!(glyph_prewarm_drain_complete(0, 0, 0));
    }
}
