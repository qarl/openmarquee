// Bug 3 Slice 2 prep (2026-05-19) — bench msdfgen runtime latency.
//
// Loads every .ttf in ../ui/fonts/ (relative to renderer crate root,
// per the build.rs convention) and times the per-glyph MSDF
// generation at CELL_PX = 48, RANGE_PX = 4.0, matching the bake
// settings in build.rs.
//
// For each glyph generated, records the wall-clock duration of:
//   shape.generate_msdf + shape.correct_sign + shape.correct_msdf_error
// (the three-call sequence build.rs uses). Excludes file I/O, atlas
// packing, and codepoint discovery.
//
// Skips:
//   - noto-color-emoji.ttf (CBDT bitmaps, not vector outlines)
//   - missing-glyph codepoints (face.glyph_index returns None)
//   - degenerate shapes (zero-area bounds; autoframe returns None)
//
// Reports p50 / p95 / p99 + min / max + count, on stdout.
//
// Build host (Mac): `cargo run --release --example bench_msdfgen
// --manifest-path renderer/Cargo.toml`
//
// Pi cross-build (verified 2026-05-19 via zigbuild from
// /tmp/renderer-build per the virtiofs cargo workaround):
//   cd /tmp/renderer-build && cargo zigbuild --release \
//       --example bench_msdfgen --target aarch64-unknown-linux-gnu
//   scp target/aarch64-unknown-linux-gnu/release/examples/\
//       bench_msdfgen openmarquee@openMarqueeDev:/tmp/bench_msdfgen
//
// Pi font-staging caveat: env!("CARGO_MANIFEST_DIR") bakes in the
// BUILD-host path at compile time (e.g. /private/tmp/renderer-build).
// On the Pi, the binary will look for fonts at that exact path's
// sibling ui/fonts dir. Pre-stage with:
//   rsync -a ui/fonts/ openmarquee@openMarqueeDev:/private/tmp/ui/fonts/
// then run. (For host-only bench this is automatic.)

use std::fs;
use std::path::PathBuf;
use std::time::Instant;

use msdfgen::{
    Bitmap, FillRule, FontExt, MsdfGeneratorConfig, Range, Rgb,
};
use ttf_parser::Face;

const CELL_PX: u32 = 48;
const RANGE_PX: f64 = 4.0;
const EDGE_COLORING_ANGLE_THRESHOLD: f64 = 3.0;
const EDGE_COLORING_SEED: u64 = 0;

fn main() {
    let fonts_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("renderer crate has a parent")
        .join("ui")
        .join("fonts");

    let mut entries: Vec<PathBuf> = fs::read_dir(&fonts_dir)
        .expect("read ui/fonts dir")
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.extension().and_then(|e| e.to_str()) == Some("ttf")
                && !p.file_name().map_or(true, |n| {
                    n.to_string_lossy().contains("noto-color-emoji")
                })
        })
        .collect();
    entries.sort();

    let mut durations_us: Vec<u64> = Vec::new();
    let mut total_glyphs: u32 = 0;
    let mut skipped_degenerate: u32 = 0;

    for path in &entries {
        let bytes = fs::read(path).expect("read .ttf");
        let face = match Face::parse(&bytes, 0) {
            Ok(f) => f,
            Err(_) => continue,
        };

        // Test codepoint set: Basic Latin printable (0x20..=0x7E) +
        // Latin-1 Supplement (0xA0..=0xFF), matching build.rs's
        // bake_codepoints(). Per qa-Jimmy dispatch, CJK / Devanagari /
        // Arabic high-complexity glyphs are NOT covered because the
        // bundled fonts don't include those scripts; this bench is a
        // baseline for ASCII + Latin-1 complexity. See bench summary
        // for the limitation note.
        let codepoints: Vec<u32> = (0x20u32..=0x7E)
            .chain(0xA0u32..=0xFF)
            .collect();

        let font_name = path
            .file_stem()
            .and_then(|s| s.to_str())
            .unwrap_or("?");
        let font_start = Instant::now();
        let mut font_count: u32 = 0;

        for cp in &codepoints {
            let Some(gid) = face.glyph_index(char::from_u32(*cp).unwrap_or('\u{0}')) else {
                continue;
            };
            let Some(mut shape) = face.glyph_shape(gid) else {
                continue;
            };
            if !shape.validate() {
                continue;
            }
            shape.normalize();
            let bound = shape.get_bound();
            let Some(framing) = bound.autoframe(
                CELL_PX, CELL_PX, Range::Px(RANGE_PX), None,
            ) else {
                skipped_degenerate += 1;
                continue;
            };
            shape.edge_coloring_simple(
                EDGE_COLORING_ANGLE_THRESHOLD,
                EDGE_COLORING_SEED,
            );
            let mut bitmap: Bitmap<Rgb<f32>> = Bitmap::new(CELL_PX, CELL_PX);
            let cfg = MsdfGeneratorConfig::default();
            let t0 = Instant::now();
            shape.generate_msdf(&mut bitmap, &framing, &cfg);
            shape.correct_sign(&mut bitmap, &framing, FillRule::default());
            shape.correct_msdf_error(&mut bitmap, &framing, &cfg);
            let elapsed = t0.elapsed();
            durations_us.push(elapsed.as_micros() as u64);
            total_glyphs += 1;
            font_count += 1;
        }

        eprintln!(
            "  {:30} {:>3} glyphs in {:>6.0} ms",
            font_name,
            font_count,
            font_start.elapsed().as_secs_f64() * 1000.0,
        );
    }

    durations_us.sort_unstable();
    let n = durations_us.len();
    if n == 0 {
        println!("FAIL: no glyphs generated; check ui/fonts/");
        std::process::exit(1);
    }

    let pct = |p: f64| -> u64 {
        let idx = ((n as f64) * p / 100.0).clamp(0.0, (n - 1) as f64) as usize;
        durations_us[idx]
    };
    let p50 = pct(50.0);
    let p95 = pct(95.0);
    let p99 = pct(99.0);
    let min = durations_us[0];
    let max = durations_us[n - 1];
    let sum: u64 = durations_us.iter().sum();
    let mean = sum / n as u64;

    println!();
    println!("msdfgen runtime latency bench");
    println!("  fonts tested:       {}", entries.len());
    println!("  glyphs generated:   {}", total_glyphs);
    println!("  degenerate skipped: {}", skipped_degenerate);
    println!("  cell:               {}×{} px", CELL_PX, CELL_PX);
    println!("  range:              {} px", RANGE_PX);
    println!();
    println!("  latency (microseconds):");
    println!("    min:   {:>8}", min);
    println!("    p50:   {:>8}", p50);
    println!("    p95:   {:>8}", p95);
    println!("    p99:   {:>8}", p99);
    println!("    max:   {:>8}", max);
    println!("    mean:  {:>8}", mean);
    println!();
    println!("  latency (milliseconds):");
    println!("    p50:   {:>6.2} ms", p50 as f64 / 1000.0);
    println!("    p95:   {:>6.2} ms", p95 as f64 / 1000.0);
    println!("    p99:   {:>6.2} ms", p99 as f64 / 1000.0);
    println!("    max:   {:>6.2} ms", max as f64 / 1000.0);
}
