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

    // SDF arc slice C.1 -- emoji color-bitmap atlas. Bake Noto Color
    // Emoji CBDT into RGBA8 atlas pages alongside the MSDF atlases.
    // Skips on missing noto-color-emoji.ttf (graceful: the runtime
    // emoji path simply has no atlas to bind and renders tofu).
    let noto_path = fonts_dir.join("noto-color-emoji.ttf");
    if noto_path.exists() {
        println!("cargo:rerun-if-changed={}", noto_path.display());
        match bake_emoji_atlas(&noto_path, &atlases_dir) {
            Ok(report) => {
                println!(
                    "cargo:warning=EMOJI: baked {} codepoints across {} atlas pages ({}x{} tiles)",
                    report.codepoints, report.pages, EMOJI_CELL_PX, EMOJI_CELL_PX,
                );
            }
            Err(e) => {
                println!("cargo:warning=EMOJI: bake FAILED: {e}");
            }
        }
    } else {
        println!(
            "cargo:warning=EMOJI: noto-color-emoji.ttf not at {} (skipping bake)",
            noto_path.display()
        );
    }
}

// =====================================================================
// SDF arc slice C.1 -- emoji color-bitmap atlas.
//
// Noto Color Emoji ships CBDT (Color Bitmap Data) at PPEM 109 with
// most glyphs at 128x128 and a few wider (~136x128). We extract each
// codepoint in U+1F000-1FFFF + U+2600-27BF (the Unicode ranges the
// editor's @font-face rule covers, per ui/styles.css:46-47), skip
// U+FE0F + skin-tone modifiers (U+1F3FB-1F3FF) + ZWJ sequences
// (handled by iterating single codepoints only), decode each PNG,
// resample to a 96x96 RGBA cell, and pack cells into 2048x2048
// RGBA8 atlas pages. Pages are PNG-encoded for binary size: raw
// ~64 MB vs ~25 MB compressed.
//
// Runtime side (slice C.2): include_bytes! each .epng + the index
// JSON; decode at session bring-up into GL_RGBA8 textures; the
// layout-side (slice C.3) maps codepoint -> (page, row, col) and
// emits a quad per glyph keyed by per-emoji aspect-ratio + advance
// from the index.
// =====================================================================

const EMOJI_CELL_PX: u32 = 96;
const EMOJI_ATLAS_DIM: u32 = 2048;
const EMOJI_NOTO_PPEM: u16 = 128;
/// Maximum atlas pages the runtime is willing to include_bytes!.
/// Must match `crate::sdf_atlas_emoji::MAX_EMOJI_PAGES`. Sized for
/// the recon's worst-case ~3500 codepoint estimate (8 pages * 441
/// cells = 3528). When fewer pages are actually used, build.rs
/// emits empty placeholder files for the unused slots so
/// include_bytes! always resolves; the runtime trims via
/// `manifest.pages`.
const EMOJI_MAX_PAGES: u32 = 8;

#[derive(serde::Serialize)]
struct EmojiAtlasEntry {
    cp: u32,
    page: u32,
    /// Top-left atlas-pixel position of this codepoint's 96x96 cell.
    x: u32,
    y: u32,
    /// Source raster's natural width/height (pre-resample), in
    /// CBDT pixels at EMOJI_NOTO_PPEM. Runtime layout uses the
    /// aspect ratio to position the glyph in its on-screen box.
    src_w: u32,
    src_h: u32,
    /// CBDT-reported advance width in CBDT pixels. Most Noto emoji
    /// are square-ish (advance == src_w) but the wider strikes
    /// (~136 px wide on 128 ppem) need this for proper text-run
    /// layout.
    advance_px: u32,
}

#[derive(serde::Serialize)]
struct EmojiAtlasManifest {
    /// Stem of the source font ("noto-color-emoji"). Matches the
    /// runtime stem used by font_family_to_filename for emoji
    /// resolution.
    font: String,
    cell_px: u32,
    atlas_dim: u32,
    pages: u32,
    /// CBDT bitmap strike (ppem) we resampled from. Recorded for
    /// diagnostics; the runtime doesn't need it post-resample.
    source_ppem: u16,
    entries: Vec<EmojiAtlasEntry>,
}

struct EmojiBakeReport {
    codepoints: usize,
    pages: u32,
}

fn bake_emoji_atlas(
    ttf_path: &Path,
    atlases_dir: &Path,
) -> Result<EmojiBakeReport, String> {
    let bytes = fs::read(ttf_path)
        .map_err(|e| format!("read noto: {e}"))?;
    let face = ttf_parser::Face::parse(&bytes, 0)
        .map_err(|e| format!("parse noto: {e:?}"))?;
    let upem = face.units_per_em() as u32;

    let tiles_per_side: u32 = EMOJI_ATLAS_DIM / EMOJI_CELL_PX;     // 21
    let tiles_per_page: u32 = tiles_per_side * tiles_per_side;     // 441

    // Per-page RGBA byte buffer. Pages allocated lazily as we fill.
    let mut pages: Vec<Vec<u8>> = Vec::new();
    let mut entries: Vec<EmojiAtlasEntry> = Vec::new();

    // Iterate the two emoji codepoint ranges from ui/styles.css.
    // Skip variation selector + skin-tone modifiers per slice C
    // Q4 (acceptable for v1; documented as a follow-up in
    // SYSTEM_SPEC slice C.4).
    let ranges = [(0x1F000u32, 0x1FFFFu32), (0x2600u32, 0x27BFu32)];
    let should_skip = |cp: u32| -> bool {
        cp == 0xFE0F                              // text-vs-emoji presentation
            || (0x1F3FB..=0x1F3FF).contains(&cp)  // skin-tone modifiers
        // NOTE: regional-indicator letters (U+1F1E6-1F1FF) are NOT skipped;
        // they're 26 standalone "boxed letter" glyphs in Noto and the
        // dispatch's "skip compound sequences" rule applies to ZWJ /
        // flag-pair compounds, not to the indicators themselves. Baking
        // them costs ~6% of one atlas page and avoids tofu when a user
        // types a bare regional indicator (e.g. testing flag rendering
        // before ZWJ support lands).
    };

    for (lo, hi) in ranges {
        for cp in lo..=hi {
            if should_skip(cp) { continue; }
            let Some(ch) = char::from_u32(cp) else { continue; };
            let Some(gid) = face.glyph_index(ch) else { continue; };
            let Some(raster) = face.glyph_raster_image(gid, EMOJI_NOTO_PPEM) else {
                // Codepoint exists in cmap but has no CBDT strike (rare).
                continue;
            };
            if !matches!(raster.format, ttf_parser::RasterImageFormat::PNG) {
                continue;
            }

            // Decode the PNG payload.
            let (decoded_rgba, src_w, src_h) = decode_png_rgba(raster.data)
                .map_err(|e| format!("decode PNG cp={cp:04X}: {e}"))?;

            // Resample to EMOJI_CELL_PX x EMOJI_CELL_PX with uniform
            // scale + transparent letterboxing so non-square emoji
            // (~136x128 wide ones) keep aspect.
            let tile = resample_to_cell(
                &decoded_rgba, src_w, src_h, EMOJI_CELL_PX,
            );
            let idx = entries.len() as u32;
            let page = idx / tiles_per_page;
            let local = idx % tiles_per_page;
            let cell_row = local / tiles_per_side;
            let cell_col = local % tiles_per_side;
            let x_px = cell_col * EMOJI_CELL_PX;
            let y_px = cell_row * EMOJI_CELL_PX;

            // Ensure the destination page exists + blit.
            while pages.len() as u32 <= page {
                pages.push(vec![0u8; (EMOJI_ATLAS_DIM * EMOJI_ATLAS_DIM * 4) as usize]);
            }
            blit_cell_into_atlas(
                &tile, &mut pages[page as usize],
                EMOJI_CELL_PX, EMOJI_ATLAS_DIM, x_px, y_px,
            );

            // SDF arc C.1 follow-up: derive advance_px from
            // face.glyph_hor_advance (font-canonical) instead of
            // src_w (the CBDT PNG width, which is approximately
            // but not exactly the glyph advance for many emoji).
            // Falls back to src_w if the font reports a 0 advance
            // for this glyph (defensive; Noto entries always have
            // non-zero advances). Scaled font-units -> CBDT pixels:
            //   adv_px = (adv_units * EMOJI_NOTO_PPEM + upem/2) / upem
            // (the +upem/2 rounds half-up before integer division).
            let adv_units = face.glyph_hor_advance(gid).unwrap_or(0) as u32;
            let advance_px = if adv_units > 0 {
                (adv_units * EMOJI_NOTO_PPEM as u32 + upem / 2) / upem
            } else {
                src_w as u32
            };
            entries.push(EmojiAtlasEntry {
                cp,
                page,
                x: x_px,
                y: y_px,
                src_w: src_w as u32,
                src_h: src_h as u32,
                advance_px,
            });
        }
    }

    if pages.len() as u32 > EMOJI_MAX_PAGES {
        return Err(format!(
            "emoji bake produced {} pages but EMOJI_MAX_PAGES is {}; \
             grow the constant + the parallel array in \
             sdf_atlas_emoji.rs and try again",
            pages.len(),
            EMOJI_MAX_PAGES,
        ));
    }

    // Encode each used page as PNG + write to OUT_DIR. PNG
    // compression turns ~16 MB raw RGBA pages into 2-3 MB on disk;
    // the runtime decodes at session bring-up.
    for (page_idx, page_rgba) in pages.iter().enumerate() {
        let png_path = atlases_dir
            .join(format!("noto-color-emoji-{page_idx}.epng"));
        let mut png_out: Vec<u8> = Vec::new();
        {
            let mut enc = png::Encoder::new(
                &mut png_out, EMOJI_ATLAS_DIM, EMOJI_ATLAS_DIM,
            );
            enc.set_color(png::ColorType::Rgba);
            enc.set_depth(png::BitDepth::Eight);
            let mut writer = enc.write_header()
                .map_err(|e| format!("png header page {page_idx}: {e}"))?;
            writer.write_image_data(page_rgba)
                .map_err(|e| format!("png data page {page_idx}: {e}"))?;
        }
        fs::write(&png_path, &png_out)
            .map_err(|e| format!("write {}: {e}", png_path.display()))?;
    }

    // include_bytes! resolves at compile time and needs every slot
    // to point at a real file. Emit empty .epng files for unused
    // slots so the runtime's parallel array resolves consistently
    // regardless of how many pages the actual bake used.
    for page_idx in (pages.len() as u32)..EMOJI_MAX_PAGES {
        let png_path = atlases_dir
            .join(format!("noto-color-emoji-{page_idx}.epng"));
        fs::write(&png_path, b"")
            .map_err(|e| format!("write empty placeholder {}: {e}", png_path.display()))?;
    }

    // Write the codepoint -> (page, x, y, src_*) index.
    let manifest = EmojiAtlasManifest {
        font: "noto-color-emoji".to_string(),
        cell_px: EMOJI_CELL_PX,
        atlas_dim: EMOJI_ATLAS_DIM,
        pages: pages.len() as u32,
        source_ppem: EMOJI_NOTO_PPEM,
        entries,
    };
    let manifest_json = serde_json::to_string_pretty(&manifest)
        .map_err(|e| format!("serialize emoji manifest: {e}"))?;
    let manifest_path = atlases_dir.join("noto-color-emoji.json");
    fs::write(&manifest_path, &manifest_json)
        .map_err(|e| format!("write emoji manifest: {e}"))?;

    Ok(EmojiBakeReport {
        codepoints: manifest.entries.len(),
        pages: pages.len() as u32,
    })
}

/// Decode a PNG payload into RGBA8 bytes + dims. Uses the same
/// `png` crate the runtime uses for PNG-related work so the
/// decode behavior matches.
fn decode_png_rgba(data: &[u8]) -> Result<(Vec<u8>, u16, u16), String> {
    let mut decoder = png::Decoder::new(data);
    // Empirically Noto Color Emoji ships its CBDT payloads as indexed-
    // color PNGs with a tRNS alpha chunk (one entry per palette index).
    // EXPAND tells the png decoder to expand any sub-RGBA format
    // (indexed / 1-2-4 bit grayscale / etc.) up to 8-bit; ALPHA adds
    // an alpha channel where the source has none. Together they
    // normalize every Noto glyph payload to RGBA8 in one decode pass.
    decoder.set_transformations(
        png::Transformations::EXPAND | png::Transformations::ALPHA,
    );
    let mut reader = decoder.read_info()
        .map_err(|e| format!("png read_info: {e}"))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf)
        .map_err(|e| format!("png next_frame: {e}"))?;
    buf.truncate(info.buffer_size());

    let w = info.width as u16;
    let h = info.height as u16;
    // Post-transformation color_type should be Rgba (palette/indexed
    // expanded; alpha synthesized for non-alpha sources). Grayscale
    // sources EXPAND to RGB then ALPHA adds the channel; indexed
    // EXPAND yields RGB (or RGBA when tRNS is present) and ALPHA
    // ensures we end at RGBA.
    let rgba = match info.color_type {
        png::ColorType::Rgba => buf,
        png::ColorType::Rgb => {
            // ALPHA should have added alpha; defensive fallback.
            let mut out = Vec::with_capacity(buf.len() * 4 / 3);
            for px in buf.chunks_exact(3) {
                out.extend_from_slice(&[px[0], px[1], px[2], 255]);
            }
            out
        }
        png::ColorType::GrayscaleAlpha => {
            let mut out = Vec::with_capacity(buf.len() * 2);
            for px in buf.chunks_exact(2) {
                out.extend_from_slice(&[px[0], px[0], px[0], px[1]]);
            }
            out
        }
        png::ColorType::Grayscale => {
            let mut out = Vec::with_capacity(buf.len() * 4);
            for &v in &buf {
                out.extend_from_slice(&[v, v, v, 255]);
            }
            out
        }
        png::ColorType::Indexed => {
            // EXPAND should have unpacked this; if it didn't, the
            // png crate API changed. Fail loud.
            return Err(
                "indexed PNG survived EXPAND transformation (png crate API drift?)"
                    .to_string()
            );
        }
    };
    Ok((rgba, w, h))
}

/// Resample a source RGBA buffer to a `cell` x `cell` RGBA tile
/// with uniform-scale fit + transparent letterboxing. Preserves
/// aspect ratio so wide emoji (~136x128) don't visually distort.
///
/// SDF arc slice C.1 follow-up: bilinear sampler (was box-filter
/// averaging). Bilinear handles both downscale (96 cell from 128
/// source, ~1.33x) and upscale (small symbols from the U+2600
/// range) cleanly. Box-filter averages all source pixels overlapping
/// each destination cell, which is correct for downscale but
/// degenerates to nearest-neighbor on upscale. Bilinear is the
/// safe single-implementation choice across both directions.
fn resample_to_cell(src: &[u8], src_w: u16, src_h: u16, cell: u32) -> Vec<u8> {
    let cell_px = cell as i32;
    let sw = src_w as i32;
    let sh = src_h as i32;
    // Uniform scale: fit longest side to cell.
    let scale = (cell_px as f32 / sw.max(sh) as f32).min(1.0);
    let dst_w = ((sw as f32 * scale).round() as i32).max(1);
    let dst_h = ((sh as f32 * scale).round() as i32).max(1);
    let off_x = (cell_px - dst_w) / 2;
    let off_y = (cell_px - dst_h) / 2;
    let mut out = vec![0u8; (cell * cell * 4) as usize];

    let sample = |sx: i32, sy: i32| -> [u32; 4] {
        let sx = sx.clamp(0, sw - 1);
        let sy = sy.clamp(0, sh - 1);
        let si = ((sy * sw + sx) * 4) as usize;
        [
            src[si] as u32,
            src[si + 1] as u32,
            src[si + 2] as u32,
            src[si + 3] as u32,
        ]
    };

    for dy in 0..dst_h {
        // Map destination pixel CENTER to source space. The -0.5
        // shift centers each pixel within its area instead of at
        // its top-left corner; standard bilinear convention.
        let sy_f = (dy as f32 + 0.5) * sh as f32 / dst_h as f32 - 0.5;
        let sy0 = sy_f.floor() as i32;
        let sy1 = sy0 + 1;
        let fy = sy_f - sy0 as f32;
        for dx in 0..dst_w {
            let sx_f = (dx as f32 + 0.5) * sw as f32 / dst_w as f32 - 0.5;
            let sx0 = sx_f.floor() as i32;
            let sx1 = sx0 + 1;
            let fx = sx_f - sx0 as f32;

            let p00 = sample(sx0, sy0);
            let p10 = sample(sx1, sy0);
            let p01 = sample(sx0, sy1);
            let p11 = sample(sx1, sy1);

            let mut out_px = [0u8; 4];
            for c in 0..4 {
                let top = p00[c] as f32 * (1.0 - fx) + p10[c] as f32 * fx;
                let bot = p01[c] as f32 * (1.0 - fx) + p11[c] as f32 * fx;
                let val = top * (1.0 - fy) + bot * fy;
                out_px[c] = val.round().clamp(0.0, 255.0) as u8;
            }

            let dx_atlas = off_x + dx;
            let dy_atlas = off_y + dy;
            if dx_atlas < 0
                || dx_atlas >= cell_px
                || dy_atlas < 0
                || dy_atlas >= cell_px
            {
                continue;
            }
            let di = ((dy_atlas * cell_px + dx_atlas) * 4) as usize;
            out[di..di + 4].copy_from_slice(&out_px);
        }
    }
    out
}

/// Blit a `cell` x `cell` RGBA tile into the atlas at (x, y).
fn blit_cell_into_atlas(
    tile: &[u8], atlas: &mut [u8],
    cell_px: u32, atlas_dim: u32, x: u32, y: u32,
) {
    for row in 0..cell_px {
        let src_off = (row * cell_px * 4) as usize;
        let dst_off = (((y + row) * atlas_dim + x) * 4) as usize;
        let len = (cell_px * 4) as usize;
        atlas[dst_off..dst_off + len]
            .copy_from_slice(&tile[src_off..src_off + len]);
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}
