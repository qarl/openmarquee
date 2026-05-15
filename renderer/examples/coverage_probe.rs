// Phase 3n: emit per-glyph fontdue::Font::rasterize output for "INTER"
// at several size_px values. Goal: byte-compare this output against the
// equivalent probe in renderer-wasm/examples/coverage_probe.rs to
// confirm fontdue 0.9.3 produces byte-identical coverage tables from
// both crate contexts.
//
// Both renderer and renderer-wasm pin fontdue = "0.9" → 0.9.3 (verified
// in both Cargo.lock files: same crate version, same registry
// checksum 2e57e16b3fe8ff4364c0661fdaac543fb38b29ea9bc9c2f45612d90adf931d2b).
// FontSettings::default() is used at Font::from_bytes on both sides
// (renderer/src/hdmi_logic.rs:91, renderer-wasm/src/lib.rs:93).
// fontdue's rasterizer is pure-Rust, deterministic, no SIMD/FMA fusion
// per Phase 3i analysis. So this probe and its renderer-wasm twin MUST
// emit byte-identical hashes; the empirical compare lets us write that
// down as a measurement, not a claim.
//
// Hash is FNV-1a 64-bit (5-line inline impl) to avoid adding sha2 as
// a dep just for a probe.
//
// Run with:
//   cargo run --release --example coverage_probe \
//       --manifest-path renderer/Cargo.toml -- ui/fonts/inter.ttf
//
// Output: JSON to stdout. Pipe to qa/captures/coverage-probe-renderer-2026-05-15.json.

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

    let text = "INTER";
    // Sizes: 1037 (HEAD Rust full-size pre-Phase-3f), 297 (canvas-
    // pixel-dim effective size for parity_font_inter post-Phase-3f:
    // 1037 * 216 / 754.46 ≈ 297), 216 (box-height pixel target),
    // 100 (small control), 24 (matches WASM unit tests).
    let sizes_px: Vec<f32> = vec![1037.0, 297.0, 216.0, 100.0, 24.0];

    println!("{{");
    println!("  \"probe\": \"renderer/examples/coverage_probe.rs\",");
    println!("  \"ttf\": {:?},", ttf_path.display().to_string());
    println!("  \"ttf_size_bytes\": {},", bytes.len());
    println!("  \"ttf_fnv1a_64\": \"{:016x}\",", ttf_fnv);
    println!("  \"fontdue_version\": \"0.9.3\",");
    println!("  \"font_settings\": \"FontSettings::default()\",");
    println!("  \"text\": {:?},", text);
    println!("  \"runs\": [");
    for (i, &size_px) in sizes_px.iter().enumerate() {
        let mut per_glyph_json = Vec::new();
        for ch in text.chars() {
            let (m, alpha) = font.rasterize(ch, size_px);
            let bm_fnv = fnv1a_64(&alpha);
            let head_hex: String = alpha
                .iter()
                .take(32)
                .map(|b| format!("{:02x}", b))
                .collect();
            let tail_start = alpha.len().saturating_sub(32);
            let tail_hex: String = alpha[tail_start..]
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect();
            per_glyph_json.push(format!(
                "      {{\"ch\": {:?}, \"width\": {}, \"height\": {}, \"xmin\": {}, \"ymin\": {}, \"advance_width\": {:.6}, \"bitmap_len\": {}, \"bitmap_fnv1a_64\": \"{:016x}\", \"bitmap_head_32\": {:?}, \"bitmap_tail_32\": {:?}}}",
                ch,
                m.width,
                m.height,
                m.xmin,
                m.ymin,
                m.advance_width,
                alpha.len(),
                bm_fnv,
                head_hex,
                tail_hex,
            ));
        }
        println!("    {{");
        println!("      \"size_px\": {},", size_px);
        println!("      \"glyphs\": [");
        println!("{}", per_glyph_json.join(",\n"));
        println!("      ]");
        if i + 1 < sizes_px.len() {
            println!("    }},");
        } else {
            println!("    }}");
        }
    }
    println!("  ]");
    println!("}}");
}
