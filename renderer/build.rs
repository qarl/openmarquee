// SDF arc slice A -- MSDF atlas generation at build time.
//
// Walks ../ui/fonts/*.ttf (8 FYS fonts + the rest of the bundled
// set; Noto Color Emoji is skipped here -- emoji gets its own
// color-bitmap atlas in slice C), generates a 48x48 MSDF cell per
// glyph for a curated codepoint set (Basic Latin + Latin-1
// Supplement = ~190 codepoints), packs the cells into a per-font
// atlas, and emits two artifacts per font to OUT_DIR:
//
//   - <font_stem>.msdf:           raw RGB888 atlas bytes (atlas_w *
//                                  atlas_h * 3), atlas dims live
//                                  in the .codepoints.json sibling.
//   - <font_stem>.codepoints.json: { atlas_w, atlas_h, cell_px,
//                                  range_px, units_per_em, ascent_em,
//                                  descent_em, line_gap_em, glyphs: [
//                                    {cp, x, y, advance_em,
//                                     plane bounds in em units} ] }.
//
// Runtime code in src/ (slice B) will `include_bytes!` /
// `include_str!` these at compile time so the binary is
// self-contained (no atlas regen on cold start, no disk cache).
//
// SDF arc Slice A spec; recon §8 ("build-time atlas baking;
// Mapbox/Three.js/Bevy pattern"). Pre-approved per QA dispatch
// 2026-05-17.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use msdfgen::{
    Bitmap, FillRule, FontExt, MsdfGeneratorConfig, Range, Rgb,
};
use ttf_parser::Face;

/// MSDF cell side length in atlas pixels. Per recon §4 + §5 +
/// QA-accepted assumption flag #2: 48 covers our 1382 px FYS worst
/// case at ~28x upscale, within MSDF's soft ceiling.
const CELL_PX: u32 = 48;

/// SDF range in pixels (msdfgen's "px" range). 4 px is the canonical
/// msdfgen default at 32-64 px cells; tightening below ~3 starts
/// clipping AA edge falloff. Mapbox uses 8 px at 24 pt; we're at
/// 48 px so 4 is the rough scale.
const RANGE_PX: f64 = 4.0;

/// Edge-coloring threshold passed to `Shape::edge_coloring_simple`.
/// msdfgen default; controls when adjacent edges get assigned the
/// same channel. 3.0 is Chlumsky's recommendation.
const EDGE_COLORING_ANGLE_THRESHOLD: f64 = 3.0;

/// Per-glyph seed for the edge-coloring randomization. Fixed so the
/// atlas is deterministic across builds.
const EDGE_COLORING_SEED: u64 = 0;

/// Codepoints baked into the atlas. Basic Latin (printable) +
/// Latin-1 Supplement (printable). ~190 codepoints; covers the FYS
/// reel + the operator-typed character set on US keyboards. Unknown
/// codepoints render as tofu boxes at runtime (slice B work).
fn bake_codepoints() -> Vec<u32> {
    let mut cps = Vec::new();
    for cp in 0x20u32..=0x7E { cps.push(cp); } // Basic Latin printable
    for cp in 0xA0u32..=0xFF { cps.push(cp); } // Latin-1 Supplement
    cps
}

/// Font catalog: every .ttf in ui/fonts/ except the emoji font.
/// Emoji is slice C (color-bitmap, not MSDF).
fn collect_fonts(fonts_dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    let entries = match fs::read_dir(fonts_dir) {
        Ok(e) => e,
        Err(_) => return out,
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = match path.file_name().and_then(|s| s.to_str()) {
            Some(s) => s.to_string(),
            None => continue,
        };
        if !name.ends_with(".ttf") { continue; }
        if name.contains("noto-color-emoji") { continue; }
        // Bug 3 Slice 2D: DejaVu Sans is the runtime fallback font.
        // Skipping the static bake keeps the build-time atlas size
        // unchanged (5918 codepoints would balloon the bake by ~30x).
        // Fallback codepoints reach the GPU via the runtime cache
        // (dynamic atlas page) on first encounter.
        if name == "dejavu-sans.ttf" { continue; }
        out.push(path);
    }
    out.sort();
    out
}

#[derive(serde::Serialize)]
struct GlyphEntry {
    cp: u32,
    /// Atlas pixel position (top-left) of this glyph's MSDF cell.
    x: u32,
    y: u32,
    /// Horizontal advance width in em units.
    advance_em: f32,
    /// Plane bounds in em units. The cell's atlas corners
    /// (0,0)..(CELL_PX,CELL_PX) map back through the framing into
    /// shape-space, divided by upem -> em. Runtime scales the
    /// rendered quad by the requested font size in px.
    pl_left: f32,
    pl_bottom: f32,
    pl_right: f32,
    pl_top: f32,
}

#[derive(serde::Serialize)]
struct AtlasManifest {
    font: String,
    cell_px: u32,
    range_px: f64,
    atlas_w: u32,
    atlas_h: u32,
    units_per_em: u16,
    ascent_em: f32,
    descent_em: f32,
    line_gap_em: f32,
    glyphs: Vec<GlyphEntry>,
}

/// Bake one font's atlas. Returns (atlas RGB888 bytes, manifest).
fn bake_one_font(ttf_path: &Path, codepoints: &[u32]) -> Option<(Vec<u8>, AtlasManifest)> {
    let ttf_bytes = fs::read(ttf_path).ok()?;
    let face = Face::parse(&ttf_bytes, 0).ok()?;

    let upem = face.units_per_em();
    let ascent_em = face.ascender() as f32 / upem as f32;
    let descent_em = face.descender() as f32 / upem as f32;
    let line_gap_em = face.line_gap() as f32 / upem as f32;

    // Resolve codepoint -> glyph_id, dropping codepoints this font
    // doesn't have. We pack the atlas to the kept count, not the
    // input count.
    let mut kept: Vec<(u32, ttf_parser::GlyphId)> = Vec::new();
    for &cp in codepoints {
        if let Some(c) = char::from_u32(cp) {
            if let Some(gid) = face.glyph_index(c) {
                kept.push((cp, gid));
            }
        }
    }

    let n = kept.len() as u32;
    if n == 0 { return None; }

    // Square-ish grid sized to N. ceil(sqrt(N)) cols x ceil(N/cols) rows.
    // ~190 codepoints -> 14x14 grid (196 cells) -> 672x672 atlas
    // RGB = ~1.3 MB per font.
    let cells_per_row = (n as f32).sqrt().ceil() as u32;
    let rows = (n + cells_per_row - 1) / cells_per_row;
    let atlas_w = cells_per_row * CELL_PX;
    let atlas_h = rows * CELL_PX;

    // RGB888 atlas, row-major, top-left origin.
    let mut atlas = vec![0u8; (atlas_w * atlas_h * 3) as usize];

    let mut glyphs = Vec::with_capacity(kept.len());

    for (i, (cp, gid)) in kept.iter().enumerate() {
        // Extract the glyph's outline as an msdfgen Shape. Missing
        // outline (e.g., the space glyph) -> emit zero-filled cell
        // + empty plane bounds; the runtime treats it as "advance
        // only, no draw."
        let Some(mut shape) = face.glyph_shape(*gid) else {
            let advance = face.glyph_hor_advance(*gid).unwrap_or(0) as f32 / upem as f32;
            let col = (i as u32) % cells_per_row;
            let row = (i as u32) / cells_per_row;
            glyphs.push(GlyphEntry {
                cp: *cp,
                x: col * CELL_PX,
                y: row * CELL_PX,
                advance_em: advance,
                pl_left: 0.0,
                pl_bottom: 0.0,
                pl_right: 0.0,
                pl_top: 0.0,
            });
            continue;
        };

        // msdfgen wants the shape validated + normalized first.
        if !shape.validate() { continue; }
        shape.normalize();

        let bound = shape.get_bound();

        let Some(framing) = bound.autoframe(
            CELL_PX,
            CELL_PX,
            Range::Px(RANGE_PX),
            None,
        ) else {
            continue; // degenerate (zero-area) shape
        };

        // Edge-color the shape so msdfgen's three channels can
        // each own an edge run -- this is what makes MSDF
        // reconstruct sharp corners.
        shape.edge_coloring_simple(EDGE_COLORING_ANGLE_THRESHOLD, EDGE_COLORING_SEED);

        // Generate MSDF into a CELL_PX^2 RGB f32 bitmap.
        let mut bitmap: Bitmap<Rgb<f32>> = Bitmap::new(CELL_PX, CELL_PX);
        let cfg = MsdfGeneratorConfig::default();
        shape.generate_msdf(&mut bitmap, &framing, &cfg);
        shape.correct_sign(&mut bitmap, &framing, FillRule::default());
        shape.correct_msdf_error(&mut bitmap, &framing, &cfg);

        // Place the cell into the atlas. msdfgen's bitmap origin is
        // bottom-left (C++ msdfgen convention); we flip Y on copy
        // so the atlas is top-left origin (matches GL texture upload
        // convention with our existing FBO sampling).
        let col = (i as u32) % cells_per_row;
        let row = (i as u32) / cells_per_row;
        let cell_x = col * CELL_PX;
        let cell_y = row * CELL_PX;
        for y in 0..CELL_PX {
            for x in 0..CELL_PX {
                let src_y = CELL_PX - 1 - y;
                let px = bitmap.pixel(x, src_y);
                let dst = ((cell_y + y) * atlas_w + (cell_x + x)) as usize * 3;
                atlas[dst]     = unorm8(px.r);
                atlas[dst + 1] = unorm8(px.g);
                atlas[dst + 2] = unorm8(px.b);
            }
        }

        // Plane bounds: msdfgen's Projection maps shape->pixel as
        //   pixel = scale * (shape + translate)
        // so inversely
        //   shape = pixel / scale - translate
        // The cell's atlas corners (0,0)..(CELL_PX,CELL_PX) project
        // back to shape-space coords (in font units, since
        // ttf-parser feeds the shape in raw font units). Dividing
        // by upem turns the result into em.
        let sx = framing.projection.scale.x;
        let sy = framing.projection.scale.y;
        let tx = framing.projection.translate.x;
        let ty = framing.projection.translate.y;
        let upem_f = upem as f32;

        let shape_l = 0.0 / sx - tx;
        let shape_r = CELL_PX as f64 / sx - tx;
        let shape_b = 0.0 / sy - ty;
        let shape_t = CELL_PX as f64 / sy - ty;

        let advance = face.glyph_hor_advance(*gid).unwrap_or(0) as f32 / upem_f;

        glyphs.push(GlyphEntry {
            cp: *cp,
            x: cell_x,
            y: cell_y,
            advance_em: advance,
            pl_left:   (shape_l as f32) / upem_f,
            pl_bottom: (shape_b as f32) / upem_f,
            pl_right:  (shape_r as f32) / upem_f,
            pl_top:    (shape_t as f32) / upem_f,
        });
    }

    let font_stem = ttf_path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("unknown")
        .to_string();

    let manifest = AtlasManifest {
        font: font_stem,
        cell_px: CELL_PX,
        range_px: RANGE_PX,
        atlas_w,
        atlas_h,
        units_per_em: upem,
        ascent_em,
        descent_em,
        line_gap_em,
        glyphs,
    };

    Some((atlas, manifest))
}

/// f32 -> u8, clamping into [0, 255]. msdfgen emits f32 distance
/// values centered roughly on 0.5; the conventional unorm8 mapping
/// is `(v * 255).clamp(0, 255)`.
///
/// SDF arc slice A follow-up: matches msdfgen-atlas-gen's truncating
/// `clamp(int(v * 256), 0, 255)` for bit-exact parity with the
/// canonical reference encoder. Sub-LSB difference vs the prior
/// round-to-nearest `(v*255+0.5).floor()` — irrelevant for AA
/// quality but useful if QA later wants to diff our atlas bytes
/// against an msdfgen-atlas-gen-produced one.
fn unorm8(v: f32) -> u8 {
    let scaled = (v * 256.0).floor();
    if scaled < 0.0 { 0 } else if scaled > 255.0 { 255 } else { scaled as u8 }
}

fn main() {
    let manifest_dir = PathBuf::from(env_or("CARGO_MANIFEST_DIR", "."));
    // renderer/ -> code/ -> code/ui/fonts.
    let fonts_dir = manifest_dir
        .parent()
        .expect("renderer parent dir")
        .join("ui/fonts");

    let out_dir = PathBuf::from(env_or("OUT_DIR", "target/build-out"));
    let atlases_dir = out_dir.join("sdf-atlases");
    fs::create_dir_all(&atlases_dir).expect("create OUT_DIR/sdf-atlases");

    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={}", fonts_dir.display());

    let codepoints = bake_codepoints();
    let fonts = collect_fonts(&fonts_dir);

    let mut index: BTreeMap<String, String> = BTreeMap::new();

    for ttf in &fonts {
        let stem = ttf.file_stem().and_then(|s| s.to_str()).unwrap_or("unknown");
        match bake_one_font(ttf, &codepoints) {
            Some((atlas_bytes, manifest)) => {
                let bin_path = atlases_dir.join(format!("{stem}.msdf"));
                let json_path = atlases_dir.join(format!("{stem}.codepoints.json"));
                fs::write(&bin_path, &atlas_bytes).expect("write atlas bin");
                let json = serde_json::to_string_pretty(&manifest)
                    .expect("serialize manifest");
                fs::write(&json_path, &json).expect("write manifest json");
                index.insert(
                    stem.to_string(),
                    format!("{}x{}", manifest.atlas_w, manifest.atlas_h),
                );
                println!(
                    "cargo:warning=SDF: baked {} ({} glyphs, {}x{} atlas, {} bytes)",
                    stem,
                    manifest.glyphs.len(),
                    manifest.atlas_w,
                    manifest.atlas_h,
                    atlas_bytes.len(),
                );
            }
            None => {
                println!("cargo:warning=SDF: SKIPPED {stem} (parse/empty)");
            }
        }
    }

    let index_path = atlases_dir.join("index.json");
    let index_json = serde_json::to_string_pretty(&index).expect("serialize index");
    fs::write(&index_path, &index_json).expect("write index.json");

    // Slice 3D (2026-05-19): the SDF-arc-C.1 CBDT bake of Noto
    // Color Emoji is retired. The runtime layout dispatch now
    // routes emoji codepoints to the runtime COLRv1 cache
    // (see src/glyph_cache_colr.rs) via the Slice 3B dispatch
    // hook. NotoColorEmoji-COLRv1.ttf ships in ui/fonts/
    // alongside the MSDF fonts and is loaded on demand.
}


fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}
