//! SDF arc slice B -- runtime side of the MSDF atlas pipeline.
//!
//! Slice A's `build.rs` bakes one MSDF atlas per font under
//! `OUT_DIR/sdf-atlases/<font_stem>.msdf` (raw RGB888 bytes) plus a
//! sibling `<font_stem>.codepoints.json` manifest. This module
//! `include_bytes!` / `include_str!`'s those at compile time and
//! exposes them via a lookup table keyed on font family stem.
//!
//! The expected consumer (slice B.2 / the cutover) is a glyph-quad
//! layout pass that:
//!   1. Resolves a layer's `font_family` to a font stem
//!      ("Anton" -> "anton") via [`font_family_to_filename`] (in
//!      `hdmi_logic`). The same mapping the existing fontdue path
//!      uses.
//!   2. Looks up the matching [`MsdfAtlas`] via [`atlas_for_stem`].
//!   3. For each glyph in the layer's text, looks up the
//!      [`GlyphEntry`] (atlas UVs + em-relative plane bounds +
//!      advance) and emits a textured quad.
//!
//! Slice B.1 (this commit) ships only the data plumbing; no caller
//! is wired up yet. Slice B.2 does the layout cutover + GL texture
//! upload + shader binding.
//!
//! ## Atlas blob format
//!
//! `<stem>.msdf` is `atlas_w * atlas_h * 3` raw RGB888 bytes,
//! row-major, top-left origin. Dimensions come from the manifest.
//!
//! ## Manifest schema
//!
//! See [`AtlasManifest`] -- mirrors the serde::Serialize struct
//! emitted by `build.rs::AtlasManifest`. The two structs must stay
//! in sync; serde_json on the runtime side does the field-by-field
//! mapping.

use serde::Deserialize;

/// Per-glyph atlas entry. Matches `build.rs::GlyphEntry` byte-for-byte
/// via serde field names.
#[derive(Debug, Clone, Deserialize)]
pub struct GlyphEntry {
    /// Unicode codepoint.
    pub cp: u32,
    /// Atlas pixel position (top-left) of this glyph's MSDF cell.
    pub x: u32,
    pub y: u32,
    /// Horizontal advance width in em units. Multiply by the
    /// on-screen font size in px to get advance in pixels.
    pub advance_em: f32,
    /// Plane bounds in em units. The on-screen quad covers
    /// (pl_left, pl_bottom)..(pl_right, pl_top) measured in em
    /// relative to the glyph origin (baseline x = 0, baseline y = 0
    /// rising up). Runtime scales these by the requested font size
    /// in px.
    pub pl_left: f32,
    pub pl_bottom: f32,
    pub pl_right: f32,
    pub pl_top: f32,
}

/// One font's MSDF atlas manifest. Matches `build.rs::AtlasManifest`
/// via serde field names.
#[derive(Debug, Clone, Deserialize)]
pub struct AtlasManifest {
    /// Font filename stem ("anton", "bebas-neue", etc.).
    pub font: String,
    /// MSDF cell side length in atlas pixels.
    pub cell_px: u32,
    /// SDF range in atlas pixels (typically 4 -- see build.rs's
    /// SDF_RANGE_PX). Used by build-time baking; runtime shaders
    /// (both FWIDTH and FIXED variants) don't sample this directly
    /// -- FWIDTH derives AA from screen-space gradient, FIXED uses
    /// a uniform aa_width. Kept on the manifest for diagnostics
    /// and parity with msdfgen's atlas-info conventions.
    pub range_px: f64,
    /// Atlas dimensions in pixels.
    pub atlas_w: u32,
    pub atlas_h: u32,
    /// Font's units_per_em.
    pub units_per_em: u16,
    /// Vertical metrics in em units. Runtime uses these to position
    /// baselines and compute line height.
    pub ascent_em: f32,
    pub descent_em: f32,
    pub line_gap_em: f32,
    pub glyphs: Vec<GlyphEntry>,
}

impl AtlasManifest {
    /// Look up a glyph entry by codepoint. Linear scan (~190
    /// codepoints per font; cache-friendly + sub-microsecond on the
    /// Pi Zero 2 W). Slice B.2 may switch to a sorted lookup if a
    /// hot reel materializes; today it's not on the perf critical
    /// path -- layout runs once per slide change, not per frame.
    pub fn glyph_for(&self, cp: u32) -> Option<&GlyphEntry> {
        self.glyphs.iter().find(|g| g.cp == cp)
    }

    /// Convenience: nominal line height in em (ascent - descent +
    /// line_gap). Matches Canvas2D's default line-height for
    /// `font-size: 1em` with no explicit line-height set.
    pub fn line_height_em(&self) -> f32 {
        self.ascent_em - self.descent_em + self.line_gap_em
    }
}

/// One font's atlas + RGB888 atlas blob. The runtime composes these
/// into the rest of the renderer's text pipeline (slice B.2).
pub struct MsdfAtlas {
    pub manifest: AtlasManifest,
    /// Raw RGB888 atlas bytes; len = atlas_w * atlas_h * 3.
    pub atlas_rgb: &'static [u8],
}

/// Compile-time-baked MSDF atlases for every font in
/// `ui/fonts/*.ttf` EXCEPT the color-emoji fonts
/// (`noto-color-emoji.ttf` / `noto-color-emoji-colrv1.ttf` — both
/// skipped because MSDF can't represent the multi-color glyph
/// data they carry). Slice 3D retired the static CBDT bake of
/// the former; the latter is the runtime COLRv1 source loaded by
/// `crate::glyph_cache_colr`.
///
/// Built into the binary via `include_bytes!` / `include_str!` from
/// `OUT_DIR/sdf-atlases/`.
///
/// The literal `concat!(env!("OUT_DIR"), ...)` paths resolve at
/// `cargo build` time per-target, so cross-compile to aarch64 picks
/// up the same set of atlases that the host build generated.
macro_rules! include_atlas {
    ($stem:literal) => {
        (
            $stem,
            include_str!(concat!(
                env!("OUT_DIR"),
                "/sdf-atlases/",
                $stem,
                ".codepoints.json"
            )),
            include_bytes!(concat!(
                env!("OUT_DIR"),
                "/sdf-atlases/",
                $stem,
                ".msdf"
            )) as &'static [u8],
        )
    };
}

/// (font_stem, manifest_json, atlas_rgb_bytes) for every baked font.
///
/// `build.rs` emits these to OUT_DIR; cargo guarantees both the
/// build script and the main crate share the same OUT_DIR for the
/// target being built. No filesystem walk at runtime.
///
/// Order is lexicographic by font stem to match `build.rs::collect_fonts`
/// (which sorts by filename so the bake order is deterministic). The
/// `atlas_for_stem` lookup is linear; sorting matters only for the
/// `eprintln!` ordering in `upload_all`.
pub const RAW_ATLASES: &[(&str, &str, &'static [u8])] = &[
    include_atlas!("alfa-slab-one"),
    include_atlas!("anton"),
    include_atlas!("archivo-black"),
    include_atlas!("bebas-neue"),
    include_atlas!("bowlby-one-sc"),
    include_atlas!("caveat"),
    include_atlas!("caveat-brush"),
    include_atlas!("cinzel"),
    include_atlas!("dm-serif-display"),
    include_atlas!("inter"),
    include_atlas!("jetbrains-mono"),
    include_atlas!("oswald"),
    include_atlas!("pacifico"),
    include_atlas!("permanent-marker"),
    include_atlas!("playfair-display"),
    include_atlas!("reenie-beanie"),
    include_atlas!("roboto-slab"),
    include_atlas!("rye"),
    include_atlas!("sedgwick-ave-display"),
    include_atlas!("shadows-into-light"),
    include_atlas!("space-mono"),
    include_atlas!("unifrakturcook"),
    include_atlas!("vt323"),
];

/// Parse every baked atlas's manifest JSON into an [`MsdfAtlas`].
/// One-shot at GL init; no per-frame cost. Returns parsed atlases
/// in the same order as [`RAW_ATLASES`].
///
/// Errors out the first invalid manifest -- if the JSON shape ever
/// drifts between `build.rs` and this module, we want a hard fail
/// at the entry point, not a deferred surprise in the glyph layout
/// pass.
pub fn load_all_atlases() -> Result<Vec<MsdfAtlas>, AtlasLoadError> {
    let mut out = Vec::with_capacity(RAW_ATLASES.len());
    for (stem, json, rgb) in RAW_ATLASES {
        let manifest: AtlasManifest = serde_json::from_str(json)
            .map_err(|e| AtlasLoadError::ManifestParse {
                stem: (*stem).to_string(),
                err: e.to_string(),
            })?;
        let expected_bytes = (manifest.atlas_w * manifest.atlas_h * 3) as usize;
        if rgb.len() != expected_bytes {
            return Err(AtlasLoadError::AtlasSize {
                stem: (*stem).to_string(),
                expected: expected_bytes,
                got: rgb.len(),
            });
        }
        out.push(MsdfAtlas {
            manifest,
            atlas_rgb: rgb,
        });
    }
    Ok(out)
}

/// Find a baked atlas by font stem (matches `font_family_to_filename`'s
/// output sans the `.ttf` suffix). Returns `None` for fonts that
/// weren't baked (e.g. `noto-color-emoji`, slice C) OR for
/// unrecognized family names.
///
/// Linear scan; 23 atlases. The slice B.2 cutover may add a
/// HashMap if it materializes on the layout hot path.
pub fn atlas_for_stem<'a>(atlases: &'a [MsdfAtlas], stem: &str) -> Option<&'a MsdfAtlas> {
    atlases.iter().find(|a| a.manifest.font == stem)
}

#[derive(Debug)]
pub enum AtlasLoadError {
    ManifestParse { stem: String, err: String },
    AtlasSize { stem: String, expected: usize, got: usize },
}

impl std::fmt::Display for AtlasLoadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            AtlasLoadError::ManifestParse { stem, err } => {
                write!(f, "msdf manifest parse failed for {stem}: {err}")
            }
            AtlasLoadError::AtlasSize { stem, expected, got } => {
                write!(
                    f,
                    "msdf atlas size mismatch for {stem}: expected {expected} bytes, \
                     got {got} (manifest atlas_w * atlas_h * 3 should match \
                     .msdf blob length)"
                )
            }
        }
    }
}

impl std::error::Error for AtlasLoadError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn baked_atlases_present_and_parse_clean() {
        // Sanity: every entry in RAW_ATLASES has non-empty JSON +
        // non-empty atlas bytes. Catches the "build.rs didn't run
        // because OUT_DIR was stale" failure mode early.
        for (stem, json, rgb) in RAW_ATLASES {
            assert!(!json.is_empty(), "manifest for {stem} is empty");
            assert!(!rgb.is_empty(), "atlas blob for {stem} is empty");
        }
        let atlases = load_all_atlases().expect("all baked atlases parse");
        assert_eq!(atlases.len(), RAW_ATLASES.len());
        // Every atlas has at least one glyph and metric vertical
        // metrics (ascent > 0, descent < 0 or = 0).
        for a in &atlases {
            assert!(!a.manifest.glyphs.is_empty(),
                "atlas {} has no glyphs", a.manifest.font);
            assert!(a.manifest.ascent_em > 0.0,
                "atlas {} has non-positive ascent", a.manifest.font);
        }
    }

    #[test]
    fn lookup_by_stem_hits_for_known_font() {
        let atlases = load_all_atlases().expect("parse");
        let anton = atlas_for_stem(&atlases, "anton").expect("anton atlas present");
        assert_eq!(anton.manifest.font, "anton");
        // 191 codepoints per recon: Basic Latin printable + Latin-1
        // Supplement printable. Some fonts skip a few codepoints if
        // the .ttf doesn't carry them; lower bound is conservative.
        assert!(anton.manifest.glyphs.len() >= 150);
    }

    #[test]
    fn lookup_returns_none_for_unknown_stem() {
        let atlases = load_all_atlases().expect("parse");
        assert!(atlas_for_stem(&atlases, "no-such-font").is_none());
        // Emoji is intentionally not baked here (slice C territory).
        assert!(atlas_for_stem(&atlases, "noto-color-emoji").is_none());
    }

    #[test]
    fn glyph_for_resolves_letter_a() {
        let atlases = load_all_atlases().expect("parse");
        let anton = atlas_for_stem(&atlases, "anton").expect("anton present");
        let a = anton.manifest.glyph_for('A' as u32).expect("anton has 'A'");
        // Plane bounds for 'A' should be reasonable em-relative
        // values (well within [-1, +2] em).
        assert!(a.pl_left.abs() < 2.0);
        assert!(a.pl_top < 2.0 && a.pl_top > 0.0);
        assert!(a.advance_em > 0.0 && a.advance_em < 2.0);
    }
}
