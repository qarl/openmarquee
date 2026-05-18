//! SDF arc slice C.2 -- emoji color-bitmap atlas (cross-platform side).
//!
//! Companion to [`crate::sdf_atlas`] but for the emoji color path:
//! Noto Color Emoji's CBDT bitmaps were extracted at build time
//! into 96x96 RGBA8 cells and packed into a small set of PNG-
//! compressed atlas pages (slice C.1). This module owns the parse
//! + lookup side; the GL upload happens in
//! [`crate::sdf_atlas_emoji_gl`] on Linux.
//!
//! Layout-side (slice C.3) calls [`atlas_entry_for_codepoint`] to
//! resolve a codepoint to its (page index, atlas-pixel position,
//! source dims). The runtime then emits an emoji quad with
//! atlas-UV bounds + the page's GL texture handle into
//! [`crate::hdmi::draw_text_layer_msdf`]'s draw batch.

use serde::Deserialize;

/// One emoji glyph's atlas placement + source dimensions. Mirrors
/// `build.rs::EmojiAtlasEntry`.
#[derive(Debug, Clone, Copy, Deserialize)]
pub struct EmojiAtlasEntry {
    pub cp: u32,
    pub page: u32,
    /// Top-left atlas-pixel position of this codepoint's 96x96 cell.
    pub x: u32,
    pub y: u32,
    /// Source raster's natural width/height (pre-resample), in
    /// CBDT pixels at PPEM 128. Used by C.3 layout for aspect
    /// ratio + emoji-quad sizing inside a text run.
    pub src_w: u32,
    pub src_h: u32,
    /// CBDT-reported advance width in CBDT pixels.
    pub advance_px: u32,
}

/// Parsed emoji atlas manifest. Mirrors `build.rs::EmojiAtlasManifest`.
#[derive(Debug, Clone, Deserialize)]
pub struct EmojiAtlasManifest {
    pub font: String,
    pub cell_px: u32,
    pub atlas_dim: u32,
    pub pages: u32,
    pub source_ppem: u16,
    pub entries: Vec<EmojiAtlasEntry>,
}

/// One emoji color-bitmap atlas. Holds the parsed manifest plus
/// the raw PNG bytes for each page (decoded into GL textures on
/// Linux via `sdf_atlas_emoji_gl`).
///
/// Construct via [`load_emoji_atlas`].
pub struct EmojiAtlas {
    pub manifest: EmojiAtlasManifest,
    /// Per-page PNG bytes. `pages[N]` is the PNG payload for atlas
    /// page N (the `.epng` file emitted by build.rs).
    pub pages_png: Vec<&'static [u8]>,
}

/// Maximum atlas pages this runtime will include_bytes!. Must match
/// `build.rs::EMOJI_MAX_PAGES`. Sized for the recon's worst-case
/// ~3500 codepoint estimate (8 pages * 441 cells/page = 3528).
/// build.rs writes empty placeholders for unused slots so the
/// `include_bytes!` array below always resolves; the runtime
/// trims via `manifest.pages`.
pub const MAX_EMOJI_PAGES: usize = 8;

const RAW_EMOJI_MANIFEST: &str = include_str!(concat!(
    env!("OUT_DIR"),
    "/sdf-atlases/noto-color-emoji.json"
));

const RAW_EMOJI_PAGES: [&[u8]; MAX_EMOJI_PAGES] = [
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-0.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-1.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-2.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-3.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-4.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-5.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-6.epng")),
    include_bytes!(concat!(env!("OUT_DIR"), "/sdf-atlases/noto-color-emoji-7.epng")),
];

/// Parse the baked emoji manifest + slice the page byte arrays.
/// One-shot at GL init; no per-frame cost. Returns `None` only on
/// manifest-parse failure (which would indicate a build-side
/// schema drift — fail loud).
pub fn load_emoji_atlas() -> Result<EmojiAtlas, String> {
    let manifest: EmojiAtlasManifest = serde_json::from_str(RAW_EMOJI_MANIFEST)
        .map_err(|e| format!("parse emoji manifest: {e}"))?;
    let n = manifest.pages as usize;
    if n > RAW_EMOJI_PAGES.len() {
        return Err(format!(
            "emoji manifest claims {} pages but only {} are baked",
            n,
            RAW_EMOJI_PAGES.len()
        ));
    }
    let pages_png: Vec<&'static [u8]> = RAW_EMOJI_PAGES[..n].to_vec();
    Ok(EmojiAtlas {
        manifest,
        pages_png,
    })
}

/// Find an atlas entry by Unicode codepoint. `None` for codepoints
/// not in the baked set (e.g. ZWJ-only compound bases, U+FE0F,
/// skin-tone modifiers, or anything outside the
/// U+1F000-1FFFF + U+2600-27BF ranges).
pub fn atlas_entry_for_codepoint<'a>(
    atlas: &'a EmojiAtlas,
    cp: u32,
) -> Option<&'a EmojiAtlasEntry> {
    // Linear scan; ~1300 entries is sub-microsecond on the Pi Zero 2 W
    // and emoji segmentation is per-text-change, not per-frame. If a
    // hot reel materializes this can switch to a sorted lookup.
    atlas.manifest.entries.iter().find(|e| e.cp == cp)
}

/// True when `cp` is within the codepoint ranges the emoji atlas
/// covers (U+1F000-1FFFF + U+2600-27BF). Returns true even for
/// codepoints inside the ranges that aren't in `entries` (e.g.
/// holes in Noto's coverage); use [`atlas_entry_for_codepoint`]
/// for the strict per-glyph hit test. Used by the C.3 layout
/// segmentation to decide which path a run goes through.
#[allow(dead_code)] // wired by C.3
pub fn codepoint_is_emoji_range(cp: u32) -> bool {
    (0x1F000..=0x1FFFF).contains(&cp) || (0x2600..=0x27BF).contains(&cp)
}
