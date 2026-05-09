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

/// 8-bit grayscale-alpha bitmap. Output of the text-layout pass; the
/// renderer uploads it as a GL_ALPHA texture for the glyph fragment
/// shader to sample.
#[derive(Debug, Clone)]
pub struct AlphaBitmap {
    pub width: u32,
    pub height: u32,
    /// Row-major, top-left-origin. Length = width * height. Each
    /// byte is 0..=255 = transparent..=opaque.
    pub data: Vec<u8>,
}

/// v1-spec-delta #3 (slice b cache): per-layer rasterized-bitmap
/// cache. Each entry holds the (resolved_text, AlphaBitmap) for
/// one layer. When the resolved text is unchanged across frames
/// (motion-only animations or the 29 frames between auto_mode
/// second-bucket boundaries), the expensive fontdue rasterization
/// is skipped and the cached bitmap is reused. Cache miss = text
/// changed = re-rasterize.
///
/// Vec parallel to text_layers; len matches. Initialized to None
/// at slide-render entry; populated lazily on first paint.
pub type GlyphCache = Vec<Option<CachedGlyph>>;

#[derive(Debug)]
pub struct CachedGlyph {
    pub text: String,
    /// qarl-direct perf-profile (2026-05-08): cache the size we
    /// rasterized at, so a size change (box.w / mode_w shrink)
    /// invalidates the cache. Pre-fix the cache keyed only on
    /// text — a layout-changing edit silently kept the stale
    /// bitmap. With the parallel TextureCache landing, the GL
    /// texture would also stay stale; fixing both at the
    /// CachedGlyph level invalidates them in lockstep via
    /// paint_slide's existing rasterize-stage logic.
    pub size_px: f32,
    pub bitmap: AlphaBitmap,
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
) -> bool {
    match cache_entry {
        Some(cached) => cached.text != resolved_text || cached.size_px != size_px,
        None => true,
    }
}

/// Lay out a single line of `text` rasterized at `size_px`. Each glyph
/// is rasterized via `fontdue` and blitted onto a single grayscale
/// bitmap whose width is the sum of glyph advances and whose height
/// is the max ascent + descent across the line. No wrapping, no
/// kerning beyond the font's natural metrics, no bidi.
///
/// Returns `None` if the resulting bitmap would be empty (e.g. empty
/// text, or every char rasterized to a 0×0 box like a single space).
///
/// Phase 4.2a: simple single-line layout. Phase 4.2c will pull in
/// multiline + alignment when the FYS slides that need them land.
pub fn layout_text_to_alpha(font: &fontdue::Font, text: &str, size_px: f32) -> Option<AlphaBitmap> {
    if text.is_empty() {
        return None;
    }

    // First pass: rasterize each glyph + measure the line's bbox.
    // We measure ascent/descent in the font's own units to size the
    // canvas. fontdue exposes BOTH a float OutlineBounds (`m.bounds`)
    // and integer pixel offsets (`m.xmin`, `m.ymin`); the latter are
    // pre-snapped to the bitmap rows the rasterizer actually wrote,
    // so using bounds + .round() introduces an off-by-one between
    // the placement and the bitmap (descender hairline gaps,
    // ascender AA-edge clip). Phase 4.2b QA-flagged R1 fix.
    //
    // `m.ymin` semantics: distance from baseline to the BOTTOM of
    // the glyph bitmap, in pixels, with y-up. Negative for
    // descenders (g, j, p, q, y) — the bottom of the bitmap sits
    // below the baseline.
    let mut glyphs: Vec<(fontdue::Metrics, Vec<u8>)> = Vec::with_capacity(text.chars().count());
    let mut total_advance = 0.0_f32;
    let mut max_ascent = 0_i32;  // pixels above baseline
    let mut min_descent = 0_i32; // pixels below baseline (m.ymin is ≤ 0 typically)
    for ch in text.chars() {
        let (m, alpha) = font.rasterize(ch, size_px);
        // ascent_above_baseline = ymin + height (top of bitmap
        // relative to baseline, y-up).
        let ascent = m.ymin + m.height as i32;
        max_ascent = max_ascent.max(ascent);
        min_descent = min_descent.min(m.ymin);
        total_advance += m.advance_width;
        glyphs.push((m, alpha));
    }
    let line_w = total_advance.ceil() as u32;
    let line_h = (max_ascent - min_descent).max(0) as u32;
    if line_w == 0 || line_h == 0 {
        return None;
    }
    let baseline_y: i32 = max_ascent; // pixels from top of canvas to baseline (y-down)

    // v1-spec-delta #4 (slice b/d, QA review fix): pad the output
    // bitmap by 1 pixel on all four sides. The padding rows stay
    // alpha=0 -- invisible to FS_GLYPH (which only samples the
    // center texel). FS_GLYPH_OUTLINE dilates the alpha mask by
    // 1 pixel via 4-neighbor sampling; without padding, the
    // boundary texels' neighbors clip via CLAMP_TO_EDGE which
    // returns the edge inked texel and produces dilated == center
    // at the bitmap edges (no visible exterior outline ring,
    // outline only on INTERIOR shapes like the counter of an "O").
    // Padding gives the dilation room to grow into transparent
    // pixels and produce the visible exterior ring.
    let pad: u32 = 1;
    let bm_w = line_w + 2 * pad;
    let bm_h = line_h + 2 * pad;

    // Second pass: blit each glyph at (cursor_x + glyph_xmin + pad,
    // baseline_y - (glyph_ymin + glyph_height) + pad).
    let mut data = vec![0u8; (bm_w * bm_h) as usize];
    let mut cursor_x = 0.0_f32;
    for (m, alpha) in &glyphs {
        let glyph_x = (cursor_x + m.xmin as f32).round() as i32 + pad as i32;
        let glyph_top = baseline_y - m.ymin - m.height as i32 + pad as i32;
        for gy in 0..m.height as i32 {
            let dst_y = glyph_top + gy;
            if dst_y < 0 || dst_y as u32 >= bm_h {
                continue;
            }
            for gx in 0..m.width as i32 {
                let dst_x = glyph_x + gx;
                if dst_x < 0 || dst_x as u32 >= bm_w {
                    continue;
                }
                let src = alpha[(gy as usize) * m.width + gx as usize];
                if src == 0 {
                    continue;
                }
                let idx = (dst_y as u32 * bm_w + dst_x as u32) as usize;
                // Glyphs in a single line don't overlap (fontdue
                // emits non-overlapping bboxes per glyph), so a
                // direct write is safe — no max/saturate needed.
                data[idx] = src;
            }
        }
        cursor_x += m.advance_width;
    }
    Some(AlphaBitmap {
        width: bm_w,
        height: bm_h,
        data,
    })
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

/// Fragment shader: horizontal wipe — slide_b reveals from the left
/// edge with a hard line at x=t. Mirrors backend.openmarquee
/// .rendering.shader_compositor's `_FRAGMENT_WIPE` minus the motion-
/// overlay logic (we don't render motion overlays from the renderer
/// side at this phase). Pairs with VS_TEXTURED_QUAD.
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
/// expands from screen center to the corners. The `0.71` factor is
/// `sqrt(0.5)` (≈ 0.7071), the diagonal distance from center
/// (0.5, 0.5) to the corner (1, 1) in normalized [0, 1] UV space —
/// so at u_t=1 the circle exactly covers the screen. Mirrors Python
/// ref `_FRAGMENT_IRIS`.
pub const FS_IRIS: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    vec4 a = texture2D(u_src_a, v_uv);
    vec4 b = texture2D(u_src_b, v_uv);
    float r = distance(v_uv, vec2(0.5));
    float mask = step(r, u_t * 0.71);
    gl_FragColor = mix(a, b, mask);
}
"#;

/// Fragment shader: dissolve — per-pixel reveal threshold sampled
/// from a hash of v_uv. Each pixel "rolls a die" once and reveals
/// when u_t crosses its threshold. Mirrors Python ref
/// `_FRAGMENT_DISSOLVE`.
///
/// **Precision note**: the Python ref uses `highp` throughout the
/// preamble specifically because the hash math (sin/dot/fract on
/// large constants) collapses on vc4's mediump (~10-bit mantissa).
/// Match that here — every other transition can stay mediump, but
/// dissolve needs the higher precision or it stripes/banded on Pi.
pub const FS_DISSOLVE: &str = r#"#version 100
precision highp float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
float _hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
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
/// enters from the bottom as slide_a rolls up off the top. Mirrors
/// Python ref `_FRAGMENT_SCROLL`.
pub const FS_SCROLL: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src_a;
uniform sampler2D u_src_b;
uniform float u_t;
varying vec2 v_uv;
void main() {
    float t = u_t;
    float seam = 1.0 - t;
    float onTo = step(seam, v_uv.y);
    vec2 fromUV = vec2(v_uv.x, v_uv.y + t);
    vec2 toUV = vec2(v_uv.x, v_uv.y - seam);
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
    vec4 col = vec4(0.0, 0.0, 0.0, 1.0);
    if (scaleX > 0.001) {
        float src_x = (v_uv.x - 0.5) / scaleX + 0.5;
        if (src_x >= 0.0 && src_x <= 1.0) {
            vec2 uv = vec2(src_x, v_uv.y);
            vec4 a = texture2D(u_src_a, uv);
            vec4 b = texture2D(u_src_b, uv);
            col = mix(a, b, useTo);
        }
    }
    gl_FragColor = col;
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

/// Fragment shader: linear cross-fade between two textures by `u_t`.
/// Mirrors backend.openmarquee.rendering.shader_compositor's
/// `_FRAGMENT_FADE`: at t=0 emits src_a, at t=1 emits src_b,
/// linearly interpolated between. Phase 5-b-1 — first transition.
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
    // dissolve + glitch hash math collapses on vc4's mediump
    // (~10-bit mantissa). Glitch isn't ported yet (qarl-deferred);
    // keep the gate for forward compat.
    matches!(kind, "dissolve" | "glitch")
}

fn kind_needs_hash(kind: &str) -> bool {
    matches!(kind, "dissolve" | "glitch")
}

const SP_HASH_HELPER: &str = r#"
float _hash(vec2 p) {
    return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453);
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
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "v_uv");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "v_uv");
            s.push_str("    float r = distance(v_uv, vec2(0.5));\n");
            s.push_str("    float mask = step(r, u_t * 0.71);\n");
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
            // A rolls up off the top. Note v_uv.y is bottom-up
            // (NDC convention from VS_TEXTURED_QUAD); the legacy
            // FS_SCROLL used the same convention so the math
            // ports verbatim.
            s.push_str("    float t = u_t;\n");
            s.push_str("    float seam = 1.0 - t;\n");
            s.push_str("    vec2 sample_uv_a = vec2(v_uv.x, v_uv.y + t);\n");
            s.push_str("    vec2 sample_uv_b = vec2(v_uv.x, v_uv.y - seam);\n");
            s.push_str("    vec3 ca = u_a_bg;\n");
            push_compose_chain(s, "u_a", "ca", n_a, "sample_uv_a");
            s.push_str("    vec3 cb = u_b_bg;\n");
            push_compose_chain(s, "u_b", "cb", n_b, "sample_uv_b");
            s.push_str("    float on_to = step(seam, v_uv.y);\n");
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
/// At spec defaults (brightness=100/gamma=2.2 in the schema's
/// 100-scale + 2.2 anchor) the renderer applies a real
/// sRGB-ish gamma correction; operators can dim via
/// brightness in [0, 100].
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
    // Avoid pow(0, x) edge cases via a tiny epsilon. GLSL's
    // pow is undefined for negative bases; clamping rgb to
    // [0, 1+eps] keeps it well-defined.
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
        for i in 0..3 {
            let v = (px[i] as f32) / 255.0;
            let scaled = (v * brightness).clamp(0.0, 1.0);
            let corrected = scaled.powf(inv_gamma);
            px[i] = (corrected * 255.0).round().clamp(0.0, 255.0) as u8;
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
uniform vec2 u_dir;
uniform vec2 u_proj_bounds;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
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
    float proj = (pos.x + pos.y) / 1.41421356;
    float modv = mod(proj, u_tile);
    float t = step(u_tile * 0.5, modv);
    gl_FragColor = vec4(mix(u_color_a, u_color_b, t), 1.0);
}
"#;

/// v1-spec-delta #6 (slice b) -- standard checker. Cells alternate
/// color_a / color_b based on (floor(x/tile) + floor(y/tile)) %
/// 2. y flipped to match Python's image-coord convention.
pub const FS_PATTERN_CHECKER: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    float gx = floor(pos.x / u_tile);
    float gy = floor(pos.y / u_tile);
    float t = mod(gx + gy, 2.0);
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
    float d2 = dot(cell, cell);
    float r2 = u_radius * u_radius;
    float t = step(d2, r2);
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
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 cell1 = mod(pos, u_tile) - vec2(u_tile * 0.5);
    vec2 cell2 = mod(pos + vec2(u_half), u_tile) - vec2(u_tile * 0.5);
    float r2 = u_radius * u_radius;
    float d_min2 = min(dot(cell1, cell1), dot(cell2, cell2));
    float t = step(d_min2, r2);
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
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    float row = floor(pos.y);
    // step(mod(row, tile), 0.5) is 1 when mod == 0, 0 otherwise.
    float t = step(mod(row, u_tile), 0.5);
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
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    float on_x = step(mod(floor(pos.x), u_tile), 0.5);
    float on_y = step(mod(floor(pos.y), u_tile), 0.5);
    float on_line = max(on_x, on_y);
    gl_FragColor = vec4(mix(u_color_b, u_color_a, on_line), 1.0);
}
"#;

/// v1-spec-delta #6 (slice c) -- concentric rings around the
/// slide center. Period-`u_tile` repetition: each period has a
/// color_a band of (half-2) pixels followed by a 2-pixel
/// color_b ring. Center at viewport midpoint.
pub const FS_PATTERN_RINGS: &str = r#"#version 100
precision mediump float;
uniform vec2 u_viewport;
uniform float u_tile;
uniform float u_threshold;
uniform vec3 u_color_a;
uniform vec3 u_color_b;
void main() {
    vec2 pos = vec2(gl_FragCoord.x, u_viewport.y - gl_FragCoord.y);
    vec2 d = pos - u_viewport * 0.5;
    float dist = length(d);
    float period = mod(dist, u_tile);
    float t = step(u_threshold, period);
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
/// Python's `_render_pattern_halftone`: tile = round(lerp(60, 6,
/// density)); radius = round(tile * 0.34); half = tile // 2.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct HalftoneUniforms {
    pub tile: f32,
    pub radius: f32,
    pub half: f32,
}

pub fn halftone_uniforms(density: f32) -> HalftoneUniforms {
    let tile = pattern_lerp(60.0, 6.0, density).round().max(2.0);
    let radius = (tile * 0.34).round().max(2.0);
    // Python uses `tile // 2` (integer floor divide). Mirror with
    // floor(tile / 2) to match exactly even at odd tile sizes.
    let half = (tile * 0.5).floor();
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
/// Concentric rings around the slide center. Mirrors Python's
/// `_render_pattern_rings`: tile = max(4, round(lerp(120, 6,
/// density))); half = tile // 2; ring of `2` pixels at period
/// boundary, color_a band of half-2 pixels.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RingsUniforms {
    pub tile: f32,
    /// Threshold within each period above which color_b shows.
    /// Python: half - 2, where half = tile // 2.
    pub threshold: f32,
}

pub fn rings_uniforms(density: f32) -> RingsUniforms {
    let tile = pattern_lerp(120.0, 6.0, density).round().max(4.0);
    let half = (tile * 0.5).floor();
    // Python clamps the band so threshold could be 0 at tile=4
    // (half=2, half-2=0). At threshold=0, every pixel inside the
    // ring period >= 0 is color_b -- which means the whole tile
    // is color_b except the exact period-boundary pixel. Visually
    // this is "very dense rings" -- correct per Python.
    let threshold = (half - 2.0).max(0.0);
    RingsUniforms { tile, threshold }
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
/// alternate offset by half-brick-width. Mirrors Python's
/// `_render_pattern_bricks`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BricksUniforms {
    pub bw: f32,
    pub bh: f32,
    pub half: f32,
}

pub fn bricks_uniforms(density: f32) -> BricksUniforms {
    let bw = pattern_lerp(140.0, 16.0, density).round().max(8.0);
    // Python: bh = max(4, bw // 2). Mirror with floor.
    let bh = (bw * 0.5).floor().max(4.0);
    let half = (bw * 0.5).floor();
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

/// Calendar fields decomposed from a Unix timestamp (seconds
/// since 1970-01-01 UTC). Naive UTC math; future slices can layer
/// timezone awareness on top.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CalendarUtc {
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
pub fn unix_to_calendar_utc(unix_seconds: i64) -> CalendarUtc {
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
    CalendarUtc {
        year,
        month: m,
        day: d,
        hour,
        minute,
        second,
        weekday,
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
/// has a sensible default: time_hm / date_medium / day_long.
pub fn format_auto_text(
    auto_mode: Option<&str>,
    auto_format: Option<&str>,
    cal: CalendarUtc,
) -> Option<String> {
    let mode = auto_mode?;
    let fmt = match (mode, auto_format) {
        ("time", Some(f)) if f.starts_with("time_") => f,
        ("date", Some(f)) if f.starts_with("date_") => f,
        ("day", Some(f)) if f.starts_with("day_") => f,
        ("time", _) => "time_hm",
        ("date", _) => "date_medium",
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

/// Linear horizontal travel, LTR (text enters from the right edge,
/// exits left). Period at intensity=50 is ~3.5 s; ranges from 6 s
/// slow to 1 s fast over 0..100. Returns offset_x_norm in
/// [-1, +1] units of the box width — the renderer converts to
/// pixels using the actual box dim.
fn motion_ticker(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    // Period: 6 s @ 0  →  1 s @ 100. At 50: 3.5 s (close to spec's
    // ~3 s). Linear interp keeps the math obvious; the spec
    // explicitly tolerates approximate timing because operators
    // can't perceive sub-second period differences.
    let base_period = 6.0 - 5.0 * intensity_norm as f32;
    if speed == 0.0 {
        // Frozen: hold at phase=0 visual state (entry edge).
        return MotionState {
            offset_x_norm: 1.0 - 2.0 * phase,
            ..MotionState::IDENTITY
        };
    }
    let period = (base_period / speed).max(0.05);
    let t = tick_seconds + (phase as f64) * period as f64;
    let cycle = (t.rem_euclid(period as f64)) / (period as f64);
    // Offset goes +1 → -1 over one cycle (right-edge → left-edge).
    let offset_x = 1.0 - 2.0 * cycle as f32;
    MotionState {
        offset_x_norm: offset_x,
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

/// Sine vertical bob. 1 Hz, amplitude ±1 % at intensity=0 → ±10 %
/// at intensity=100, ±5.5 % at intensity=50 (close to spec's
/// "±5 %"). Returns offset_y_norm in box-height units.
fn motion_bounce(intensity_norm: f32, phase: f32, speed: f32, tick_seconds: f64) -> MotionState {
    let amp = 0.01 + 0.09 * intensity_norm;
    let phase_rad = 2.0 * std::f32::consts::PI
        * ((tick_seconds * speed as f64) as f32 + phase);
    MotionState {
        offset_y_norm: amp * phase_rad.sin(),
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
///   - `Ticker` -> offset_x in box-width units
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
        MotionKind::Ticker => (state.offset_x_norm * box_w_px, 0.0),
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

/// NDC quad for placing a `bm_w × bm_h` bitmap inside a slide-
/// relative `box(x, y, w, h)` (fractions of mode_w / mode_h) on a
/// `mode_w × mode_h` viewport, aligned per `(halign, valign)`.
///
/// **Fit policy (Phase 4.2c):** if the bitmap fits inside the box at
/// its rasterized size, place it without scaling. If it overflows
/// the box width or height, uniformly scale-down (preserving aspect)
/// so the scaled bitmap fits. The bitmap is then aligned within the
/// (possibly larger) box per the alignment knobs.
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

    // Scale-down-only: never upscale. If bitmap fits, scale=1.0.
    let bm_w_f = bm_w as f32;
    let bm_h_f = bm_h as f32;
    let s_w = if bm_w_f > box_w_px { box_w_px / bm_w_f } else { 1.0 };
    let s_h = if bm_h_f > box_h_px { box_h_px / bm_h_f } else { 1.0 };
    let scale = s_w.min(s_h);
    let placed_w = bm_w_f * scale;
    let placed_h = bm_h_f * scale;

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

#[cfg(test)]
mod tests {
    use super::*;

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
        assert_eq!(stripes_uniforms(0.0).tile, 80.0);
        assert_eq!(stripes_uniforms(1.0).tile, 4.0);
        // density 0.5 -> round(80 + (4-80)*0.5) = round(42) = 42.
        assert_eq!(stripes_uniforms(0.5).tile, 42.0);
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
            && FS_PATTERN_HALFTONE.contains("u_half"));
        assert!(FS_PATTERN_SCANLINES.contains("u_tile"));
        assert!(FS_PATTERN_GRID.contains("u_tile"));
        assert!(FS_PATTERN_RINGS.contains("u_tile") && FS_PATTERN_RINGS.contains("u_threshold"));
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
    fn halftone_uniforms_match_python_anchors() {
        // tile = round(lerp(60, 6, density)); radius = round(tile *
        // 0.34); half = floor(tile / 2).
        // density 0: tile=60, radius=round(20.4)=20, half=30.
        let u0 = halftone_uniforms(0.0);
        assert_eq!(u0.tile, 60.0);
        assert_eq!(u0.radius, 20.0);
        assert_eq!(u0.half, 30.0);
        // density 1: tile=6, radius=round(2.04)=2, half=3.
        let u1 = halftone_uniforms(1.0);
        assert_eq!(u1.tile, 6.0);
        assert_eq!(u1.radius, 2.0);
        assert_eq!(u1.half, 3.0);
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
    fn rings_uniforms_match_python_anchors() {
        // tile = max(4, round(lerp(120, 6, density))); half = floor
        // (tile/2); threshold = max(0, half-2).
        // density 0: tile=120, half=60, threshold=58.
        let u0 = rings_uniforms(0.0);
        assert_eq!(u0.tile, 120.0);
        assert_eq!(u0.threshold, 58.0);
        // density 1: tile=6, half=3, threshold=1.
        let u1 = rings_uniforms(1.0);
        assert_eq!(u1.tile, 6.0);
        assert_eq!(u1.threshold, 1.0);
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
    fn bricks_uniforms_match_python_anchors() {
        // bw = max(8, round(lerp(140, 16, density))); bh = max(4,
        // floor(bw/2)); half = floor(bw/2).
        // density 0: bw=140, bh=70, half=70.
        let u0 = bricks_uniforms(0.0);
        assert_eq!(u0.bw, 140.0);
        assert_eq!(u0.bh, 70.0);
        assert_eq!(u0.half, 70.0);
        // density 1: bw=16, bh=8, half=8.
        let u1 = bricks_uniforms(1.0);
        assert_eq!(u1.bw, 16.0);
        assert_eq!(u1.bh, 8.0);
        assert_eq!(u1.half, 8.0);
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
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_IRIS.contains(uniform));
        }
        // Iris radius math: distance from center (0.5, 0.5),
        // compared to t * 0.71 (≈ sqrt(0.5), the diagonal half-
        // length). Pin so a refactor can't accidentally swap to
        // step(t * 0.71, r) (inverted) or drop the 0.71 (would
        // leave a strip of slide_a in the corners at t=1).
        assert!(FS_IRIS.contains("distance(v_uv, vec2(0.5))"));
        assert!(FS_IRIS.contains("step(r, u_t * 0.71)"));
    }

    #[test]
    fn fs_dissolve_uses_highp_precision() {
        // Critical for vc4 — the sin/dot/fract hash needs more than
        // the ~10-bit mantissa of mediump or the threshold values
        // collapse and the dissolve goes banded. Pin at host-test
        // time so a copy-paste from another shader can't downgrade
        // it.
        assert!(FS_DISSOLVE.starts_with("#version 100\n"));
        assert!(FS_DISSOLVE.contains("precision highp float"));
        assert!(!FS_DISSOLVE.contains("precision mediump float"));
        for uniform in ["u_src_a", "u_src_b", "u_t"] {
            assert!(FS_DISSOLVE.contains(uniform));
        }
        // Hash structure pinned: sin(dot(p, vec2(...))) * big
        // constant, fract'd. Pin the magic constants so a future
        // edit doesn't silently change the noise pattern.
        assert!(FS_DISSOLVE.contains("12.9898"));
        assert!(FS_DISSOLVE.contains("78.233"));
        assert!(FS_DISSOLVE.contains("43758.5453"));
        assert!(FS_DISSOLVE.contains("step(threshold, u_t)"));
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
        assert!(FS_SCROLL.contains("step(seam, v_uv.y)"));
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
        assert!(iris.contains("distance(v_uv, vec2(0.5))"));
        let dissolve = fs_transition_sp_source("dissolve", 0, 0).unwrap();
        assert!(dissolve.contains("precision highp float"));
        assert!(dissolve.contains("_hash"));
    }

    #[test]
    fn fs_transition_sp_source_unsupported_kind_returns_none() {
        assert!(fs_transition_sp_source("glitch", 1, 1).is_none());
        assert!(fs_transition_sp_source("unknown_kind", 1, 1).is_none());
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
        assert!(scroll.contains("vec2(v_uv.x, v_uv.y + t)"));
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

    #[test]
    fn layout_has_one_pixel_transparent_margin() {
        // v1-spec-delta #4 (slice b/d, QA review fix): layout
        // pads the output bitmap by 1 pixel on all sides so the
        // FS_GLYPH_OUTLINE shader's 4-neighbor dilation has room
        // to grow beyond the inked extent. Verify the outermost
        // row + col of pixels are all alpha=0.
        let font = load_anton();
        let bm = layout_text_to_alpha(&font, "F", 64.0).expect("F bitmap");
        // Top + bottom rows
        for x in 0..bm.width {
            assert_eq!(
                bm.data[x as usize], 0,
                "top-row pixel ({x}, 0) must be padding (alpha=0)"
            );
            let bottom_idx = ((bm.height - 1) * bm.width + x) as usize;
            assert_eq!(
                bm.data[bottom_idx], 0,
                "bottom-row pixel ({x}, {}) must be padding (alpha=0)",
                bm.height - 1
            );
        }
        // Left + right columns
        for y in 0..bm.height {
            let left = (y * bm.width) as usize;
            assert_eq!(
                bm.data[left], 0,
                "left-col pixel (0, {y}) must be padding (alpha=0)"
            );
            let right = (y * bm.width + bm.width - 1) as usize;
            assert_eq!(
                bm.data[right], 0,
                "right-col pixel ({}, {y}) must be padding (alpha=0)",
                bm.width - 1
            );
        }
    }

    #[test]
    fn layout_empty_text_returns_none() {
        let font = load_anton();
        assert!(layout_text_to_alpha(&font, "", 64.0).is_none());
    }

    #[test]
    fn layout_single_char_produces_nonempty_bitmap() {
        let font = load_anton();
        let bm = layout_text_to_alpha(&font, "F", 64.0).expect("F bitmap");
        assert!(bm.width > 0, "F width should be > 0");
        assert!(bm.height > 0, "F height should be > 0");
        assert_eq!(
            bm.data.len() as u32,
            bm.width * bm.height,
            "data length must equal width*height"
        );
        // At 64px Anton, F should be roughly half as wide as tall —
        // sanity check on order of magnitude.
        assert!(bm.height >= 30 && bm.height <= 80, "h={}", bm.height);
        // F has ink. The rasterized bitmap should have at least
        // *some* non-zero pixels.
        assert!(
            bm.data.iter().any(|&p| p > 0),
            "F should have at least one non-zero pixel"
        );
    }

    #[test]
    fn layout_multi_char_widens_with_advance() {
        let font = load_anton();
        let f = layout_text_to_alpha(&font, "F", 64.0).unwrap();
        let free = layout_text_to_alpha(&font, "FREE", 64.0).unwrap();
        // "FREE" must be wider than "F" alone — at least 2x for a
        // 4-letter word with no kerning weirdness.
        assert!(
            free.width > 2 * f.width,
            "FREE width {} should be at least 2x F width {}",
            free.width,
            f.width
        );
        // Heights should be in the same ballpark — both lines have
        // ascender + maybe descender from the wider variant.
        assert!(
            (free.height as i32 - f.height as i32).abs() <= 2,
            "FREE h={} F h={} should match within ±2px",
            free.height,
            f.height
        );
    }

    #[test]
    fn layout_descender_taller_than_non_descender() {
        // Phase 4.2b R1: a descender (g/j/p/q/y) extends below the
        // baseline. Its bitmap height should be strictly greater
        // than a non-descender of the same nominal size, AND the
        // ink should land in the lower portion of the canvas.
        // Using m.bounds.ymin + .round() (pre-fix) loses up to
        // 1px of descender vs the integer m.ymin path; this test
        // pins the integer-snapped behavior.
        let font = load_anton();
        let f = layout_text_to_alpha(&font, "F", 64.0).expect("F bitmap");
        // Anton's lowercase descenders are tame compared to a
        // serif font, but g/p still descend below the baseline.
        for ch in ["g", "p", "y"] {
            let bm = layout_text_to_alpha(&font, ch, 64.0)
                .unwrap_or_else(|| panic!("descender {ch:?} bitmap"));
            assert!(
                bm.height >= f.height,
                "descender {ch:?} h={} should be >= F h={} (descender extends below baseline)",
                bm.height, f.height,
            );
            // Ink should appear in the bottom half of the canvas
            // (descender body sits below F's baseline).
            let bottom_half_has_ink = bm
                .data
                .iter()
                .skip((bm.width * bm.height / 2) as usize)
                .any(|&p| p > 0);
            assert!(
                bottom_half_has_ink,
                "descender {ch:?} should have ink in bottom half of bitmap",
            );
        }
    }

    #[test]
    fn layout_descender_with_caps_extends_below() {
        // Mixed-case word: "Pgy" combines a cap (P, full ascender)
        // with two descenders (g, y). The bitmap height should
        // exceed the cap-only width "PPP" since descenders push
        // the canvas down beyond the baseline.
        let font = load_anton();
        let caps = layout_text_to_alpha(&font, "PPP", 64.0).unwrap();
        let mixed = layout_text_to_alpha(&font, "Pgy", 64.0).unwrap();
        assert!(
            mixed.height > caps.height,
            "mixed h={} should be > all-caps h={} (descenders extend canvas)",
            mixed.height, caps.height,
        );
    }

    #[test]
    fn layout_whitespace_only_returns_none() {
        // R4: a space-only string has zero ink and should yield
        // None — caller falls back / skips the layer rather than
        // uploading a 0-byte texture. (Tab glyphs are font-
        // dependent — Anton rasterizes \t to a non-empty bitmap;
        // we don't assert anything about non-space whitespace.)
        let font = load_anton();
        assert!(layout_text_to_alpha(&font, " ", 64.0).is_none());
        assert!(layout_text_to_alpha(&font, "   ", 64.0).is_none());
    }

    #[test]
    fn layout_size_scales_bitmap() {
        let font = load_anton();
        let small = layout_text_to_alpha(&font, "F", 32.0).unwrap();
        let big = layout_text_to_alpha(&font, "F", 128.0).unwrap();
        // 4x size should yield ~4x dimensions (within ±5% rounding).
        let ratio_w = big.width as f32 / small.width as f32;
        let ratio_h = big.height as f32 / small.height as f32;
        assert!(
            (3.5..=4.5).contains(&ratio_w),
            "width ratio {ratio_w} should be ~4"
        );
        assert!(
            (3.5..=4.5).contains(&ratio_h),
            "height ratio {ratio_h} should be ~4"
        );
    }

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

    fn dummy_bitmap() -> AlphaBitmap {
        AlphaBitmap {
            width: 1,
            height: 1,
            data: vec![0],
        }
    }

    #[test]
    fn should_rerasterize_misses_on_none_entry() {
        // First-frame paint: cache slot empty -> miss, rasterize.
        assert!(should_rerasterize(None, "hello", 100.0));
        assert!(should_rerasterize(None, "", 100.0));
    }

    #[test]
    fn should_rerasterize_hits_on_matching_text() {
        // Steady-state on a motion-only path: resolved_text doesn't
        // change between frames, cache hit, skip fontdue.
        let cached = CachedGlyph {
            text: "hello".to_string(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(!should_rerasterize(Some(&cached), "hello", 100.0));
    }

    #[test]
    fn should_rerasterize_misses_on_differing_text() {
        // auto_mode=time second-bucket boundary: text changes from
        // "14:35:09" to "14:35:10", cache miss, re-rasterize.
        let cached = CachedGlyph {
            text: "14:35:09".to_string(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(should_rerasterize(Some(&cached), "14:35:10", 100.0));
    }

    #[test]
    fn should_rerasterize_handles_empty_string_match() {
        // Degenerate but valid: empty text on both sides -> hit.
        // (layout_text_to_alpha returns None for empty input so
        // this case is unreachable in practice, but the helper is
        // pure and shouldn't special-case it.)
        let cached = CachedGlyph {
            text: String::new(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(!should_rerasterize(Some(&cached), "", 100.0));
    }

    #[test]
    fn should_rerasterize_handles_empty_to_nonempty_transition() {
        // Cached empty, resolved non-empty -> miss. Catches a
        // degenerate edge where a paint happened with empty text
        // and the next frame has real content.
        let cached = CachedGlyph {
            text: String::new(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(should_rerasterize(Some(&cached), "anything", 100.0));
    }

    #[test]
    fn should_rerasterize_distinguishes_unicode_canonical_forms() {
        // Pure byte-comparison: NFC vs NFD of the same character
        // are different cache keys (renders differently if fontdue
        // shapes them differently). Pinning the byte-equality
        // semantic so a future "smart" comparator that normalizes
        // doesn't silently change behavior.
        let cached = CachedGlyph {
            // "café" in NFC (U+00E9)
            text: "caf\u{00E9}".to_string(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        // Same string in NFD: "cafe" + combining acute (U+0301).
        let nfd = "cafe\u{0301}";
        assert!(should_rerasterize(Some(&cached), nfd, 100.0));
    }

    #[test]
    fn should_rerasterize_misses_on_size_change() {
        // qarl-direct perf-profile (2026-05-08): same text, smaller
        // size_px (e.g. box.w shrunk by an editor edit). Pre-fix
        // the cache hit silently — rendering the old large bitmap
        // at the new small layout. Now correctly invalidates.
        let cached = CachedGlyph {
            text: "hello".to_string(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(should_rerasterize(Some(&cached), "hello", 80.0));
        assert!(should_rerasterize(Some(&cached), "hello", 120.0));
    }

    #[test]
    fn should_rerasterize_hits_on_exact_size_match() {
        // Same text + same size_px -> hit. Bitmap is reusable.
        let cached = CachedGlyph {
            text: "hello".to_string(),
            size_px: 100.0,
            bitmap: dummy_bitmap(),
        };
        assert!(!should_rerasterize(Some(&cached), "hello", 100.0));
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

    /// Pinned reference point for format tests: April 21, 2026 at
    /// 14:35:09 UTC = Tuesday. unix = 1776_782_109
    /// (= 20564 days * 86400 + 14*3600 + 35*60 + 9).
    fn pinned_calendar() -> CalendarUtc {
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
            format_auto_text(Some("date"), None, c).unwrap(),
            "Apr 21"
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
            format_auto_text(Some("date"), Some("time_hm"), c).unwrap(),
            "Apr 21"
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
    fn motion_ticker_starts_right_at_phase_zero() {
        // t=0, phase=0 → offset_x_norm = +1.0 (text positioned at
        // the right edge, about to enter from there per LTR).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 0.0);
        assert!((m.offset_x_norm - 1.0).abs() < 1e-3);
    }

    #[test]
    fn motion_ticker_sawtooth_zero_at_half_cycle() {
        // Sawtooth offset: +1 → -1 over one period (linear), wraps
        // back to +1 at period boundary. At intensity=50 the period
        // is 6 - 5*0.5 = 3.5 s; halfway through, offset_x_norm = 0
        // (text centered between entry and exit).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 1.75);
        assert!(m.offset_x_norm.abs() < 1e-3, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_sawtooth_near_minus_one_just_before_wrap() {
        // Just before the period boundary, offset is asymptotically
        // approaching -1 (text fully exited left). Period = 3.5 s;
        // sample at t = 0.999 * period.
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 3.5 * 0.999);
        assert!(m.offset_x_norm < -0.99, "offset was {}", m.offset_x_norm);
    }

    #[test]
    fn motion_ticker_wraps_back_to_right_at_period_boundary() {
        // At t = period exactly, the cycle wraps and offset jumps
        // back to +1 (text re-enters from right edge).
        let m = compute_motion_state(MotionKind::Ticker, 50, 0.0, 1.0, 0, 3.5);
        assert!((m.offset_x_norm - 1.0).abs() < 1e-3);
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
        let m = compute_motion_state(MotionKind::Bounce, 50, 0.0, 1.0, 0, 0.25);
        assert!((m.offset_y_norm - 0.055).abs() < 1e-3, "y was {}", m.offset_y_norm);
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
        assert!((a.offset_y_norm - 0.055).abs() < 1e-3);
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
        // At t=0.875 (= half of effective period), expect cycle=0.5
        // → offset=0.0.
        let m =
            compute_motion_state(MotionKind::Ticker, 50, 0.0, 2.0, 0, 0.875);
        assert!(m.offset_x_norm.abs() < 1e-3, "off was {}", m.offset_x_norm);
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
        assert!((m.offset_y_norm - 0.055).abs() < 1e-3);
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
        // amp = 0.01.
        let m = compute_motion_state(MotionKind::Bounce, 0, 0.0, 1.0, 0, 0.25);
        assert!((m.offset_y_norm - 0.01).abs() < 1e-3);
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
        let s = MotionState {
            offset_x_norm: 0.5,
            ..MotionState::IDENTITY
        };
        let (dx, dy) = motion_offset_to_px(MotionKind::Ticker, s, 800.0, 200.0, 64.0);
        assert!((dx - 400.0).abs() < 1e-3);
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
            0.0, 0.0, 1.0, 1.0, 1920, 1080, 1920, 1080, HAlign::Left, VAlign::Top,
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
            0.0, 0.0, 0.5, 0.5, 100, 50, 1920, 1080, HAlign::Left, VAlign::Top,
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
    fn box_quad_centered_horizontally() {
        // 100px-wide bitmap inside a 1.0-wide (full-screen) box
        // on 1920px viewport, h-align center: bitmap NDC width is
        // 100/1920*2 = 0.10417, centered around 0 → -0.05208..0.05208.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 100, 50, 1920, 1080, HAlign::Center, VAlign::Top,
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
            0.0, 0.0, 1.0, 1.0, 100, 50, 1920, 1080, HAlign::Right, VAlign::Top,
        );
        assert!(
            (q.1 - 1.0).abs() < 1e-3 && (q.0 - 0.89583).abs() < 1e-3,
            "right-aligned NDC l/r: {} / {}", q.0, q.1,
        );
    }

    #[test]
    fn box_quad_centered_vertically() {
        let q = box_to_ndc_quad(
            0.0, 0.0, 1.0, 1.0, 100, 50, 1920, 1080,
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
    fn box_quad_overflow_scales_down_uniformly() {
        // 4000x2000 bitmap into a 1000x1000 box → scale = min(0.25, 0.5) = 0.25,
        // so placed = 1000x500. Top-left at box origin.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            4000, 2000, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        // Placed pixel rect: (0, 0) → (1000, 500).
        let exp_l = -1.0;
        let exp_r = 1000.0 / 1920.0 * 2.0 - 1.0;
        let exp_t = 1.0;
        let exp_b = 1.0 - 500.0 / 1080.0 * 2.0;
        assert!(
            approx_ndc_eq(q, (exp_l, exp_r, exp_t, exp_b)),
            "got ({}, {}, {}, {}); expected ({exp_l}, {exp_r}, {exp_t}, {exp_b})",
            q.0, q.1, q.2, q.3,
        );
    }

    #[test]
    fn box_quad_overflow_only_one_dim() {
        // Very wide bitmap (3000x100) into a 1000x1000 box →
        // s_w = 1000/3000 ≈ 0.333, s_h = 1.0 (no overflow on h),
        // scale = 0.333. Placed = 1000 x 33.3.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            3000, 100, 1920, 1080,
            HAlign::Left, VAlign::Top,
        );
        let placed_h = 100.0 / 3.0;
        let exp_b = 1.0 - placed_h / 1080.0 * 2.0;
        assert!(
            (q.3 - exp_b).abs() < 1e-2,
            "scaled placed h: NDC bottom {} expected {}",
            q.3, exp_b,
        );
    }

    #[test]
    fn box_quad_centered_align_after_scale_down() {
        // 4000x2000 bitmap into 1000x1000 box at center alignment.
        // scale = 0.25 → placed = 1000x500. Box is 1000x1000.
        // After centering: x-offset = 0 (placed_w == box_w),
        //                  y-offset = (1000-500)/2 = 250.
        let q = box_to_ndc_quad(
            0.0, 0.0, 1000.0 / 1920.0, 1000.0 / 1080.0,
            4000, 2000, 1920, 1080,
            HAlign::Center, VAlign::Middle,
        );
        // x: placed fills box → -1 .. (1000/1920*2-1)
        // y: top at 250px → 1 - 250/1080*2 ≈ 0.5370
        let exp_t = 1.0 - 250.0 / 1080.0 * 2.0;
        let exp_b = 1.0 - 750.0 / 1080.0 * 2.0;
        assert!(
            (q.2 - exp_t).abs() < 1e-3 && (q.3 - exp_b).abs() < 1e-3,
            "v-centered after scale: NDC t/b {} / {} expected {} / {}",
            q.2, q.3, exp_t, exp_b,
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
}
