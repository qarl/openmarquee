//! Emoji codepoint range helper (post-Slice-3D).
//!
//! Pre-Slice-3D this module owned the build-time CBDT atlas
//! bake (~64 MB RGBA) plus the runtime layout-side dispatch
//! helpers (atlas_entry_for_codepoint, EmojiAtlas, EmojiAtlasEntry).
//! Slice 3D retired the CBDT bake — emoji codepoints now route
//! to the Slice 3B runtime COLRv1 cache (via
//! `crate::glyph_cache_colr`) directly. The static CBDT atlas
//! pages, manifests, and `include_bytes!` are gone.
//!
//! Only [`codepoint_is_emoji_range`] survives because the layout
//! dispatch in `hdmi_logic.rs` still uses it to decide which
//! codepoints to TRY against the COLRv1 cache vs route straight
//! to the MSDF / DynamicMsdf chain. The range is independent of
//! which emoji font happens to be loaded — it mirrors the
//! `unicode-range` declaration in `ui/styles.css` so renderer-
//! side and Canvas2D-side parity is preserved.

/// True when `cp` is within the codepoint ranges the emoji
/// system covers: the supplementary-plane emoji block
/// (U+1F000-1FFFF) plus the Dingbats / Misc Symbols block
/// (U+2600-27BF). Codepoints outside these (e.g. U+2B50 ⭐ in
/// Misc Symbols and Arrows) fall through to MSDF/tofu, matching
/// the browser's font-fallback behavior under the same
/// `unicode-range` declaration.
pub fn codepoint_is_emoji_range(cp: u32) -> bool {
    (0x1F000..=0x1FFFF).contains(&cp) || (0x2600..=0x27BF).contains(&cp)
}

#[cfg(test)]
mod tests {
    use super::codepoint_is_emoji_range;

    #[test]
    fn supplementary_plane_emoji_in_range() {
        // 🔓 U+1F513, 🫵 U+1FAF5, 🪧 U+1FAA7 — current SCREAM
        // codepoints. All inside U+1F000-1FFFF.
        assert!(codepoint_is_emoji_range(0x1F513));
        assert!(codepoint_is_emoji_range(0x1FAF5));
        assert!(codepoint_is_emoji_range(0x1FAA7));
    }

    #[test]
    fn dingbats_block_in_range() {
        // ✨ U+2728, ☀ U+2600, ✓ U+2713 — Dingbats / Misc Symbols.
        assert!(codepoint_is_emoji_range(0x2728));
        assert!(codepoint_is_emoji_range(0x2600));
        assert!(codepoint_is_emoji_range(0x2713));
    }

    #[test]
    fn out_of_range_codepoints_excluded() {
        // U+2B50 ⭐ (Misc Symbols + Arrows, past U+27BF) — out.
        assert!(!codepoint_is_emoji_range(0x2B50));
        // U+25CF ● (Geometric Shapes — Boot bullet) — out.
        assert!(!codepoint_is_emoji_range(0x25CF));
        // U+0041 'A' — ASCII text. Out.
        assert!(!codepoint_is_emoji_range(0x0041));
    }
}
