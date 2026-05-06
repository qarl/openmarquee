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
