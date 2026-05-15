// Phase 3i diagnostic — emit per-glyph advance_widths from fontdue 0.9.3
// for "INTER" at several size_px values. Both the renderer crate and the
// renderer-wasm crate pin `fontdue = "0.9"` and resolve to fontdue 0.9.3
// from the same crates.io registry (verified: both Cargo.lock entries
// list version 0.9.3, source crates-io). fontdue's `Font::metrics(ch,
// size_px)` reduces to a single deterministic f32 multiply
// (`advance_width = scale * glyph.advance_width`) plus floor/ceil to
// integer for the bbox fields. No platform-conditional code, no SIMD,
// no FMA fusion. IEEE-754 round-to-nearest-even is bit-exact across
// x86_64/aarch64/wasm32, so this single Rust-native probe represents
// BOTH renderers' fontdue calls byte-for-byte — running it under
// renderer-wasm/wasm32 would emit identical bytes.
//
// Run with:
//   cargo run --release --example advance_probe \
//       --manifest-path renderer/Cargo.toml -- ui/fonts/inter.ttf
//
// Output: JSON to stdout, one object per (text, size_px), with the
// per-glyph advance + cumulative cursor at integer rounding. Pipe to
// qa/captures/advance-byte-compare-2026-05-15.json.

use std::env;
use std::fs;
use std::path::PathBuf;

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
    let font = fontdue::Font::from_bytes(bytes.as_slice(), fontdue::FontSettings::default())
        .expect("fontdue parse");

    let text = "INTER";
    // Sizes covering: 1037 (HEAD Rust full-size), ~213 (Canvas2D
    // effective at yScale<1 for font_inter fixture, derived from
    // ctx.measureText), and a few near-integer points for context.
    let sizes_px: Vec<f32> = vec![1037.0, 215.7, 213.0, 200.0, 100.0];

    println!("{{");
    println!("  \"ttf\": {:?},", ttf_path.display().to_string());
    println!("  \"ttf_size_bytes\": {},", bytes.len());
    println!("  \"fontdue_version\": \"0.9.3 (pinned in both renderer and renderer-wasm Cargo.toml)\",");
    println!("  \"text\": {:?},", text);
    println!("  \"runs\": [");
    for (i, &size_px) in sizes_px.iter().enumerate() {
        let mut cursor_unrounded = 0.0_f32;
        let mut cursor_rounded = 0.0_f32;
        let mut per_glyph = Vec::new();
        for ch in text.chars() {
            let m = font.metrics(ch, size_px);
            let advance = m.advance_width;
            let advance_rounded = advance.round();
            cursor_unrounded += advance;
            cursor_rounded += advance_rounded;
            per_glyph.push(format!(
                "      {{\"ch\": {:?}, \"advance\": {:.6}, \"advance_rounded\": {}, \"cursor_unrounded_after\": {:.6}, \"cursor_rounded_after\": {}, \"ymin\": {}, \"height\": {}}}",
                ch,
                advance,
                advance_rounded as i32,
                cursor_unrounded,
                cursor_rounded as i32,
                m.ymin,
                m.height,
            ));
        }
        println!("    {{");
        println!("      \"size_px\": {},", size_px);
        println!("      \"glyphs\": [");
        println!("{}", per_glyph.join(",\n"));
        println!("      ],");
        println!("      \"total_advance_unrounded\": {:.6},", cursor_unrounded);
        println!("      \"total_advance_rounded\": {}", cursor_rounded as i32);
        if i + 1 < sizes_px.len() {
            println!("    }},");
        } else {
            println!("    }}");
        }
    }
    println!("  ]");
    println!("}}");
}
