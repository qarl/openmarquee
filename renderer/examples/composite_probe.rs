// Phase 3o: emit the renderer-crate COMPOSITE line bitmap for "I" at
// parity_font_inter effective size (297.0). The blit logic below is
// a VERBATIM copy of the single-line case of layout_text_to_alpha in
// renderer/src/hdmi_logic.rs:413-512 (Phase 3o snapshot — if either
// source changes, retire this probe rather than diverging).
//
// Renderer crate currently exposes only [[bin]] (no [lib]) so we
// can't `use openmarquee_render::hdmi_logic::layout_text_to_alpha`
// from an example. The two viable shapes are: (a) add a [lib]
// target — a real config change; (b) inline. (b) matches the
// existing precedent (advance_probe.rs, coverage_probe.rs both call
// fontdue directly without importing from the lib).
//
// Output: JSON to stdout. The bm_alpha_b64 field carries the full
// alpha buffer for byte-compare against the renderer-wasm twin.
//
// Run with:
//   cargo run --release --example composite_probe \
//       --manifest-path renderer/Cargo.toml -- ui/fonts/inter.ttf

use std::env;
use std::fs;
use std::path::PathBuf;

fn fnv1a_64(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x100000001b3);
    }
    h
}

fn b64(data: &[u8]) -> String {
    const ALPHA: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(((data.len() + 2) / 3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0];
        let b1 = if chunk.len() > 1 { chunk[1] } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] } else { 0 };
        out.push(ALPHA[(b0 >> 2) as usize] as char);
        out.push(ALPHA[(((b0 & 0x03) << 4) | (b1 >> 4)) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHA[(((b1 & 0x0f) << 2) | (b2 >> 6)) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(ALPHA[(b2 & 0x3f) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

// Verbatim from renderer/src/hdmi_logic.rs layout_text_to_alpha
// (single-line case). Returns (bm_w, bm_h, alpha_data).
fn layout_single_line_inline(
    font: &fontdue::Font,
    text: &str,
    size_px: f32,
) -> Option<(u32, u32, Vec<u8>)> {
    // First pass: rasterize each glyph + measure global bbox.
    let mut glyphs: Vec<(fontdue::Metrics, Vec<u8>)> = Vec::with_capacity(text.len());
    let mut line_advance = 0.0_f32;
    let mut max_ascent = 0_i32;
    let mut min_descent = 0_i32;
    let mut any_glyph = false;
    for ch in text.chars() {
        let (m, alpha) = font.rasterize(ch, size_px);
        let ascent = m.ymin + m.height as i32;
        max_ascent = max_ascent.max(ascent);
        min_descent = min_descent.min(m.ymin);
        line_advance += m.advance_width.round();
        glyphs.push((m, alpha));
        any_glyph = true;
    }
    let has_ink = max_ascent > 0 || min_descent < 0;
    if !any_glyph || !has_ink {
        return None;
    }
    // hdmi_logic.rs:446 -- `as u32` (truncate), NOT `.round() as u32`.
    // For "I" at 297 the per-step advance rounds to integer so it
    // doesn't matter; flagged here for the cross-crate comparison.
    let line_w = line_advance as u32;
    let line_h_unused = (size_px * 1.1).round() as u32;
    if line_w == 0 || line_h_unused == 0 {
        return None;
    }
    let pad: u32 = 1;
    let bm_w = line_w + 2 * pad;
    let last_line_extent = (max_ascent - min_descent).max(0) as u32;
    // Single-line: bm_h = 2*pad + last_line_extent
    let bm_h = 2 * pad + last_line_extent;
    let mut data = vec![0u8; (bm_w * bm_h) as usize];

    let baseline_y = pad as i32 + max_ascent;
    let mut cursor_x = 0.0_f32;
    for (m, alpha) in &glyphs {
        let glyph_x = (cursor_x + m.xmin as f32).round() as i32 + pad as i32;
        let glyph_top = baseline_y - m.ymin - m.height as i32;
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
                data[idx] = src;
            }
        }
        cursor_x += m.advance_width.round();
    }
    Some((bm_w, bm_h, data))
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let ttf_path = args
        .get(1)
        .cloned()
        .unwrap_or_else(|| "ui/fonts/inter.ttf".to_string());
    let ttf_path = PathBuf::from(ttf_path);

    let bytes = fs::read(&ttf_path).unwrap_or_else(|e| {
        eprintln!("failed to read {}: {e}", ttf_path.display());
        std::process::exit(1)
    });
    let ttf_fnv = fnv1a_64(&bytes);
    let font = fontdue::Font::from_bytes(bytes.as_slice(), fontdue::FontSettings::default())
        .expect("fontdue parse");

    let text = "I";
    let size_px = 297.0_f32;
    let (bm_w, bm_h, data) =
        layout_single_line_inline(&font, text, size_px).expect("non-empty composite");

    let bm_fnv = fnv1a_64(&data);
    let head_hex: String = data.iter().take(32).map(|b| format!("{:02x}", b)).collect();
    let tail_start = data.len().saturating_sub(32);
    let tail_hex: String = data[tail_start..]
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect();

    println!("{{");
    println!("  \"probe\": \"renderer/examples/composite_probe.rs\",");
    println!("  \"ttf\": {:?},", ttf_path.display().to_string());
    println!("  \"ttf_fnv1a_64\": \"{:016x}\",", ttf_fnv);
    println!("  \"text\": {:?},", text);
    println!("  \"size_px\": {},", size_px);
    println!("  \"bm_w\": {},", bm_w);
    println!("  \"bm_h\": {},", bm_h);
    println!("  \"bm_len\": {},", data.len());
    println!("  \"bm_fnv1a_64\": \"{:016x}\",", bm_fnv);
    println!("  \"bm_head_32\": {:?},", head_hex);
    println!("  \"bm_tail_32\": {:?},", tail_hex);
    println!("  \"bm_alpha_b64\": {:?}", b64(&data));
    println!("}}");
}
