//! renderer-wasm — Canvas2D-side text rasterizer using the same
//! fontdue version + math the Pi-side Rust renderer uses. Closes the
//! per-engine glyph AA + per-glyph kerning divergence flagged in
//! 4cbd08b (max_delta=250 residual after the line-height fix landed).
//!
//! # Phase 0 (this commit)
//!
//! Minimal API: `rasterize_text(text, font_bytes, size_px, color_rgba)`
//! → RGBA pixel buffer + width/height. Caller is JS (ui/src/
//! rasterize.js) which has already done line-break + box-positioning
//! math; this fn just rasterizes a SINGLE line.
//!
//! Layout / centering / multi-line handling stays in JS — paintLayer
//! already produces the per-line baseline coordinates per the
//! 4cbd08b alphabetic-baseline + integer-stride rewrite. Phase 1
//! (next dispatch) replaces JS's per-line `ctx.fillText` call with a
//! `putImageData` of the bitmap this fn produces.
//!
//! # Why fontdue
//!
//! Renderer crate pins `fontdue = "0.9"` (see `renderer/Cargo.toml`).
//! Pinning the SAME version here is the byte-for-byte parity contract.
//! `fontdue::Font::metrics()` for advance widths + `rasterize()` for
//! coverage masks both produce identical output across native + wasm
//! targets (per fontdue's `#![cfg_attr(not(test), no_std)]` runtime).
//!
//! # Coordinate system
//!
//! Output buffer is row-major top-to-bottom, RGBA8 per pixel. Caller
//! treats it like an HTMLCanvasElement.getImageData buffer.
//!
//! # Determinism
//!
//! No RNG, no time, no system calls. Same input → same output bytes,
//! verifiable via host-side cargo test (this crate's tests run on the
//! native target; the wasm target is artifact-only).

use fontdue::{Font, FontSettings};
use wasm_bindgen::prelude::*;

/// Rasterize a single line of `text` at `size_px` using the TTF/OTF
/// font in `font_bytes`. Returns a flat RGBA8 buffer (row-major top-
/// to-bottom) representing the glyph ink masked to `color_rgba` —
/// transparent (alpha 0) outside the glyph, full-color inside, with
/// per-pixel alpha from fontdue's coverage values.
///
/// Width = sum of per-glyph `advance_width` rounded per-step (matches
/// renderer-side rounding at hdmi_logic.rs:245). Height = ascent +
/// descent of the worst-case glyph in the line.
///
/// Returns `Some(packed)` where the first 8 bytes are
/// `[width_lo, width_hi, width_b2, width_b3, height_lo, height_hi,
/// height_b2, height_b3]` (little-endian u32s) followed by the pixel
/// data. Returns `None` if the text contains no inked glyphs (all
/// whitespace) — matches the Rust renderer's `has_ink` guard.
///
/// Phase-0 simplification: single-line only. Multi-line + line-break
/// math stays in JS (per the dispatch's "WASM does per-line
/// rasterization only" scope).
#[wasm_bindgen]
pub fn rasterize_text(
    text: &str,
    font_bytes: &[u8],
    size_px: f32,
    color_r: u8,
    color_g: u8,
    color_b: u8,
    color_a: u8,
) -> Option<Vec<u8>> {
    if text.is_empty() {
        return None;
    }
    let font = Font::from_bytes(font_bytes, FontSettings::default()).ok()?;
    rasterize_inner(&font, text, size_px, [color_r, color_g, color_b, color_a])
}

/// Native-callable inner fn — same logic, no wasm_bindgen wrapper, so
/// host-side cargo tests can exercise it without the JS bridge.
fn rasterize_inner(
    font: &Font,
    text: &str,
    size_px: f32,
    color: [u8; 4],
) -> Option<Vec<u8>> {
    // First pass: measure + collect glyph bitmaps. Mirrors
    // hdmi_logic.rs:layout_text_to_alpha but for a SINGLE line.
    let mut line_advance = 0.0_f32;
    let mut max_ascent = 0_i32;
    let mut min_descent = 0_i32;
    let mut glyphs: Vec<(fontdue::Metrics, Vec<u8>)> = Vec::with_capacity(text.len());
    for ch in text.chars() {
        let (m, alpha) = font.rasterize(ch, size_px);
        let ascent = m.ymin + m.height as i32;
        max_ascent = max_ascent.max(ascent);
        min_descent = min_descent.min(m.ymin);
        // Round per-step matching hdmi_logic.rs:245 (qarl-direct
        // 2026-05-13 Bug A fix for VT323 monospace alignment).
        line_advance += m.advance_width.round();
        glyphs.push((m, alpha));
    }
    let has_ink = max_ascent > 0 || min_descent < 0;
    if !has_ink {
        return None;
    }
    // Bitmap dims. Single-line case so bm_h = ascent + descent
    // (no inter-line stride). No padding — caller positions by
    // exact pixel coords + the canvas does its own AA at the
    // boundary if needed.
    let line_w = line_advance.round() as u32;
    let line_h = (max_ascent - min_descent).max(0) as u32;
    if line_w == 0 || line_h == 0 {
        return None;
    }
    // Header (8 bytes: width u32 LE, height u32 LE) + RGBA pixel
    // data (line_w * line_h * 4 bytes).
    let pixel_bytes = (line_w as usize) * (line_h as usize) * 4;
    let mut out = vec![0u8; 8 + pixel_bytes];
    out[0..4].copy_from_slice(&line_w.to_le_bytes());
    out[4..8].copy_from_slice(&line_h.to_le_bytes());
    let pixels = &mut out[8..];

    // Second pass: blit each glyph at its baseline-relative position.
    // Baseline_y in bitmap coords = max_ascent (so glyphs with
    // height < max_ascent + 0 descent leave the top rows blank).
    let mut cursor_x = 0.0_f32;
    for (m, alpha) in &glyphs {
        let glyph_x = (cursor_x + m.xmin as f32).round() as i32;
        let glyph_top = max_ascent - m.ymin - m.height as i32;
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
                let cov = alpha[(gy as usize) * m.width + (gx as usize)];
                if cov == 0 {
                    continue;
                }
                let idx = ((dst_y as usize) * (line_w as usize) + (dst_x as usize)) * 4;
                // Coverage modulates the color's alpha. RGB stays as
                // the requested color so anti-aliased edges sample
                // the canvas underneath correctly (straight alpha
                // source-over composition by the JS putImageData
                // caller).
                let modulated_a = ((cov as u16 * color[3] as u16) / 255) as u8;
                // OVER existing pixel (in case of overlapping glyphs
                // from negative xmin / kerning). Straight alpha:
                // out = src.rgba over dst.rgba.
                let dst_a = pixels[idx + 3] as u16;
                if dst_a == 0 {
                    pixels[idx] = color[0];
                    pixels[idx + 1] = color[1];
                    pixels[idx + 2] = color[2];
                    pixels[idx + 3] = modulated_a;
                } else {
                    // Source-over: take the MAX coverage so a glyph
                    // overlapping another doesn't darken the union
                    // (the rasterizer outputs INK, not partial
                    // composition).
                    if modulated_a > pixels[idx + 3] {
                        pixels[idx + 3] = modulated_a;
                    }
                }
            }
        }
        cursor_x += m.advance_width.round();
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    // VT323 is the Boot fixture's font (parity-audit target). We use
    // a tiny embedded built-in TTF for the host tests to avoid
    // shipping a fixture font file; if it's needed for visual parity
    // tests, callers can load the real VT323 bytes from
    // ui/fonts/vt323.ttf.

    #[test]
    fn empty_text_returns_none() {
        let result = rasterize_inner(
            &dummy_font(),
            "",
            24.0,
            [255, 255, 255, 255],
        );
        assert!(result.is_none());
    }

    #[test]
    fn whitespace_only_returns_none() {
        // " " glyph has no ink → has_ink == false → None.
        let result = rasterize_inner(
            &dummy_font(),
            "   ",
            24.0,
            [255, 255, 255, 255],
        );
        assert!(result.is_none());
    }

    #[test]
    fn renders_inked_text_with_header() {
        let result = rasterize_inner(
            &dummy_font(),
            "A",
            32.0,
            [255, 0, 0, 255],
        );
        let buf = result.expect("'A' should rasterize");
        assert!(buf.len() >= 8, "header must be 8 bytes");
        let w = u32::from_le_bytes([buf[0], buf[1], buf[2], buf[3]]);
        let h = u32::from_le_bytes([buf[4], buf[5], buf[6], buf[7]]);
        assert!(w > 0 && h > 0, "non-empty bitmap");
        let pixel_bytes = (w as usize) * (h as usize) * 4;
        assert_eq!(buf.len(), 8 + pixel_bytes, "header + RGBA buffer");
        // At least one pixel should be inked (non-zero alpha).
        let inked = buf[8..].chunks_exact(4).any(|p| p[3] > 0);
        assert!(inked, "'A' produced no inked pixels");
    }

    #[test]
    fn determinism_same_input_same_output() {
        let a = rasterize_inner(
            &dummy_font(),
            "TEST",
            24.0,
            [128, 200, 64, 255],
        )
        .unwrap();
        let b = rasterize_inner(
            &dummy_font(),
            "TEST",
            24.0,
            [128, 200, 64, 255],
        )
        .unwrap();
        assert_eq!(a, b, "rasterize_inner must be deterministic");
    }

    /// Test-only fallback font: load whatever TTF is in ui/fonts/.
    /// Host tests run from the workspace root via `cargo test`.
    fn dummy_font() -> Font {
        let candidates = [
            "../ui/fonts/vt323.ttf",
            "../ui/fonts/inter.ttf",
            "ui/fonts/vt323.ttf",
        ];
        for path in &candidates {
            if let Ok(bytes) = std::fs::read(path) {
                return Font::from_bytes(bytes, FontSettings::default()).unwrap();
            }
        }
        panic!("no fixture font found in ui/fonts/");
    }
}
