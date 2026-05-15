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

// Phase 3j extension: also dump the screen-quad math for the
// parity_font_inter fixture so it can be compared to Canvas2D's
// drawImage rect (captured separately via Playwright in
// scripts/parity/quad_rect_probe.py). Mirrors hdmi_logic::
// box_to_ndc_quad's scale-down-only + centered halign + middle
// valign math for the fixture's authored box.

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
    println!("  ],");

    // Phase 3j: parity_font_inter screen-quad math.
    // box.x=0.05, box.y=0.4, box.w=0.9, box.h=0.2; mode=1920x1080;
    // halign=Center, valign=Middle (default for compute_layer_uv_rect_logic).
    // Bitmap dims at size_px=1037: 2*pad + last_line_extent  for height,
    // max_line_w + 2*pad for width. For "INTER" at 1037 the probe above
    // emits ymin=0, height=755, total_advance_rounded=3018. So:
    //   bm_w = 3018 + 2*pad(1) = 3020
    //   bm_h = 2*pad(1) + (max_ascent - min_descent) = 2 + 755 = 757
    let mode_w = 1920.0_f32;
    let mode_h = 1080.0_f32;
    let box_x = 0.05_f32;
    let box_y = 0.4_f32;
    let box_w = 0.9_f32;
    let box_h = 0.2_f32;
    let bm_w = 3020.0_f32;
    let bm_h = 757.0_f32;
    let box_left_px = box_x * mode_w;
    let box_top_px = box_y * mode_h;
    let box_w_px = (box_w * mode_w).max(1.0);
    let box_h_px = (box_h * mode_h).max(1.0);
    let s_w = if bm_w > box_w_px { box_w_px / bm_w } else { 1.0 };
    let s_h = if bm_h > box_h_px { box_h_px / bm_h } else { 1.0 };
    let scale = s_w.min(s_h);
    let placed_w = bm_w * scale;
    let placed_h = bm_h * scale;
    let dst_left = box_left_px + (box_w_px - placed_w) * 0.5;
    let dst_top = box_top_px + (box_h_px - placed_h) * 0.5;
    let dst_right = dst_left + placed_w;
    let dst_bottom = dst_top + placed_h;
    println!("  \"phase3j_rust_quad_parity_font_inter\": {{");
    println!("    \"inputs\": {{\"box_x\":{},\"box_y\":{},\"box_w\":{},\"box_h\":{},\"bm_w\":{},\"bm_h\":{},\"mode_w\":{},\"mode_h\":{}}},", box_x, box_y, box_w, box_h, bm_w as u32, bm_h as u32, mode_w as u32, mode_h as u32);
    println!("    \"scale\": {:.10},", scale);
    println!("    \"placed_w_px\": {:.6},", placed_w);
    println!("    \"placed_h_px\": {:.6},", placed_h);
    println!("    \"dst_left_px_unrounded\": {:.6},", dst_left);
    println!("    \"dst_top_px_unrounded\": {:.6},", dst_top);
    println!("    \"dst_right_px_unrounded\": {:.6},", dst_right);
    println!("    \"dst_bottom_px_unrounded\": {:.6},", dst_bottom);
    println!("    \"dst_left_px_rounded\": {},", dst_left.round() as i32);
    println!("    \"dst_top_px_rounded\": {},", dst_top.round() as i32);
    println!("    \"dst_right_px_rounded\": {},", dst_right.round() as i32);
    println!("    \"dst_bottom_px_rounded\": {}", dst_bottom.round() as i32);
    println!("  }}");
    println!("}}");
}
