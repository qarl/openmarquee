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

    // Second pass: blit each glyph at (cursor_x + glyph_xmin,
    // baseline_y - (glyph_ymin + glyph_height)).
    let mut data = vec![0u8; (line_w * line_h) as usize];
    let mut cursor_x = 0.0_f32;
    for (m, alpha) in &glyphs {
        let glyph_x = (cursor_x + m.xmin as f32).round() as i32;
        let glyph_top = baseline_y - m.ymin - m.height as i32;
        for gy in 0..m.height as i32 {
            let dst_y = glyph_top + gy;
            if dst_y < 0 || dst_y as u32 >= line_h {
                continue;
            }
            for gx in 0..m.width as i32 {
                let dst_x = glyph_x + gx;
                if dst_x < 0 || dst_x as u32 >= line_w {
                    continue;
                }
                let src = alpha[(gy as usize) * m.width + gx as usize];
                if src == 0 {
                    continue;
                }
                let idx = (dst_y as u32 * line_w + dst_x as u32) as usize;
                // Glyphs in a single line don't overlap (fontdue
                // emits non-overlapping bboxes per glyph), so a
                // direct write is safe — no max/saturate needed.
                data[idx] = src;
            }
        }
        cursor_x += m.advance_width;
    }
    Some(AlphaBitmap {
        width: line_w,
        height: line_h,
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
varying vec2 v_uv;
void main() {
    float a = texture2D(u_atlas, v_uv).r;
    gl_FragColor = vec4(u_text_color * a, a);
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
pub const FS_BLIT: &str = r#"#version 100
precision mediump float;
uniform sampler2D u_src;
varying vec2 v_uv;
void main() {
    gl_FragColor = texture2D(u_src, v_uv);
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
    fn fs_glyph_targets_gles2_and_pins_uniforms() {
        assert!(FS_GLYPH.starts_with("#version 100\n"));
        assert!(FS_GLYPH.contains("precision mediump float"));
        for uniform in ["u_atlas", "u_text_color"] {
            assert!(
                FS_GLYPH.contains(uniform),
                "FS_GLYPH missing uniform {uniform:?}"
            );
        }
        // Must read the alpha out of the LUMINANCE-uploaded texture
        // via `.r` (GLES2 LUMINANCE puts the value in r, g, b, a).
        assert!(FS_GLYPH.contains(".r"));
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
