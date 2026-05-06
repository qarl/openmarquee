//! Pure-logic helpers shared by the HDMI bring-up path.
//!
//! Lives in its own module because `hdmi.rs` links against
//! drm/gbm/EGL which are Linux-only at link time. This module is
//! cross-platform — it compiles on macOS so `cargo test` can run on
//! the dev box and exercise these functions without a real DRM stack.

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
