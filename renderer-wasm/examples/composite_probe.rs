// Phase 3o: emit the renderer-wasm crate's COMPOSITE line bitmap for
// "I" at parity_font_inter effective size (297.0). The blit logic
// below is a VERBATIM copy of rasterize_inner from
// renderer-wasm/src/lib.rs:99-213 (single-line case, with the
// alpha channel extracted from RGBA → 1-byte-per-pixel). Phase 3o
// snapshot — if rasterize_inner changes, retire this probe rather
// than diverging.
//
// renderer-wasm exposes `[lib] crate-type = ["cdylib"]` only (no
// rlib), so we can't import rasterize_inner directly from an
// example. The two viable shapes are: (a) add "rlib" to crate-type
// — small config change; (b) inline. (b) matches the existing
// precedent (coverage_probe.rs calls fontdue directly without
// importing from the lib).
//
// Color is fixed at white opaque (255,255,255,255) for direct
// byte-compare with the renderer (grayscale) output; modulated_a =
// (cov * 255) / 255 = cov, so the alpha channel of the RGBA output
// equals the raw coverage value.
//
// Run with:
//   cargo run --release --example composite_probe \
//       --manifest-path renderer-wasm/Cargo.toml -- ../ui/fonts/inter.ttf

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

// Verbatim from renderer-wasm/src/lib.rs:99-213 (rasterize_inner),
// single-line case. Returns (bm_w, bm_h, alpha) where alpha is the
// extracted 1-byte-per-pixel alpha channel (not the 4-bpp RGBA).
fn rasterize_inner_inline(
    font: &fontdue::Font,
    text: &str,
    size_px: f32,
) -> Option<(u32, u32, Vec<u8>)> {
    let color: [u8; 4] = [255, 255, 255, 255];
    let mut line_advance = 0.0_f32;
    let mut max_ascent = 0_i32;
    let mut min_descent = 0_i32;
    let mut glyphs: Vec<(fontdue::Metrics, Vec<u8>)> = Vec::with_capacity(text.len());
    for ch in text.chars() {
        let (m, alpha) = font.rasterize(ch, size_px);
        let ascent = m.ymin + m.height as i32;
        max_ascent = max_ascent.max(ascent);
        min_descent = min_descent.min(m.ymin);
        line_advance += m.advance_width.round();
        glyphs.push((m, alpha));
    }
    let has_ink = max_ascent > 0 || min_descent < 0;
    if !has_ink {
        return None;
    }
    const PAD: u32 = 1;
    let line_w = line_advance.round() as u32;
    let line_h = (max_ascent - min_descent).max(0) as u32;
    if line_w == 0 || line_h == 0 {
        return None;
    }
    let bm_w = line_w + 2 * PAD;
    let bm_h = line_h + 2 * PAD;
    let mut pixels = vec![0u8; (bm_w as usize) * (bm_h as usize) * 4];

    let mut cursor_x = 0.0_f32;
    for (m, alpha) in &glyphs {
        let glyph_x = (cursor_x + m.xmin as f32).round() as i32 + PAD as i32;
        let glyph_top = PAD as i32 + max_ascent - m.ymin - m.height as i32;
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
                let cov = alpha[(gy as usize) * m.width + (gx as usize)];
                if cov == 0 {
                    continue;
                }
                let idx = ((dst_y as usize) * (bm_w as usize) + (dst_x as usize)) * 4;
                let modulated_a = ((cov as u16 * color[3] as u16) / 255) as u8;
                let dst_a = pixels[idx + 3] as u16;
                if dst_a == 0 {
                    pixels[idx] = color[0];
                    pixels[idx + 1] = color[1];
                    pixels[idx + 2] = color[2];
                    pixels[idx + 3] = modulated_a;
                } else if modulated_a > pixels[idx + 3] {
                    pixels[idx + 3] = modulated_a;
                }
            }
        }
        cursor_x += m.advance_width.round();
    }

    // Extract alpha channel for direct byte-compare with renderer side.
    let alpha_only: Vec<u8> = pixels.chunks_exact(4).map(|px| px[3]).collect();
    Some((bm_w, bm_h, alpha_only))
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let ttf_path = args
        .get(1)
        .cloned()
        .unwrap_or_else(|| "../ui/fonts/inter.ttf".to_string());
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
    let (bm_w, bm_h, alpha) =
        rasterize_inner_inline(&font, text, size_px).expect("non-empty composite");

    let bm_fnv = fnv1a_64(&alpha);
    let head_hex: String = alpha.iter().take(32).map(|b| format!("{:02x}", b)).collect();
    let tail_start = alpha.len().saturating_sub(32);
    let tail_hex: String = alpha[tail_start..]
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect();

    println!("{{");
    println!("  \"probe\": \"renderer-wasm/examples/composite_probe.rs\",");
    println!("  \"ttf\": {:?},", ttf_path.display().to_string());
    println!("  \"ttf_fnv1a_64\": \"{:016x}\",", ttf_fnv);
    println!("  \"text\": {:?},", text);
    println!("  \"size_px\": {},", size_px);
    println!("  \"bm_w\": {},", bm_w);
    println!("  \"bm_h\": {},", bm_h);
    println!("  \"bm_len\": {},", alpha.len());
    println!("  \"bm_fnv1a_64\": \"{:016x}\",", bm_fnv);
    println!("  \"bm_head_32\": {:?},", head_hex);
    println!("  \"bm_tail_32\": {:?},", tail_hex);
    println!("  \"bm_alpha_b64\": {:?}", b64(&alpha));
    println!("}}");
}
