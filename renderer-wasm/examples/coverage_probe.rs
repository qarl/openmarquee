// Phase 3n: renderer-wasm twin of renderer/examples/coverage_probe.rs.
//
// Emits per-glyph fontdue::Font::rasterize output for "INTER" at several
// size_px values via the renderer-wasm crate's fontdue dep (pinned to
// 0.9 → resolves to 0.9.3, registry checksum
// 2e57e16b3fe8ff4364c0661fdaac543fb38b29ea9bc9c2f45612d90adf931d2b — same
// as renderer/Cargo.lock).
//
// The probe code below is byte-for-byte identical to
// renderer/examples/coverage_probe.rs (other than the "probe" field).
// The whole point of the twin is to verify that fontdue produces
// byte-identical output when invoked from the renderer-wasm crate
// context vs the renderer crate context — which it MUST, given:
//
//   - Same library version (0.9.3, same registry checksum)
//   - Same FontSettings::default()
//   - Same input font_bytes (passed via argv)
//   - Same f32 size_px input
//   - No SIMD, no FMA, no platform-conditional code in fontdue's
//     rasterizer (verified Phase 3i, advance_probe.rs:1-12)
//
// Build natively (no wasm32 target needed — examples compile to native
// bins even on cdylib crates):
//   cargo run --release --example coverage_probe \
//       --manifest-path renderer-wasm/Cargo.toml -- ../ui/fonts/inter.ttf
//
// Output: JSON to stdout. Pipe to qa/captures/coverage-probe-wasm-2026-05-15.json.

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
        .unwrap_or_else(|| "../ui/fonts/inter.ttf".to_string());
    let ttf_path = PathBuf::from(ttf_path);

    let bytes = fs::read(&ttf_path).unwrap_or_else(|e| {
        eprintln!("failed to read {}: {e}", ttf_path.display());
        std::process::exit(1)
    });
    let ttf_fnv = fnv1a_64(&bytes);
    let font = fontdue::Font::from_bytes(bytes.as_slice(), fontdue::FontSettings::default())
        .expect("fontdue parse");

    let text = "INTER";
    let sizes_px: Vec<f32> = vec![1037.0, 297.0, 216.0, 100.0, 24.0];

    println!("{{");
    println!("  \"probe\": \"renderer-wasm/examples/coverage_probe.rs\",");
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
