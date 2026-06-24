// Bug 3 Slice 1 (2026-05-19) — runtime glyph atlas page.
//
// Holds a 2048×2048 RGBA8 GPU texture + a simple grid allocator at
// CELL_PX-sized slots. The grid layout matches the build-time static
// MSDF atlases (sdf_atlas_gl.rs) so the FS_MSDF_FIXED shader can sample
// dynamic-cache slots with the same UV-from-atlas-xy math.
//
// Sub-texture upload via glTexSubImage2D was validated on vc4 by the
// gl_subtexture_smoke module (commit d99834d, all 15 checks PASS at
// 48×48 sub-region @ (40,40) and 47×48 sub-region @ (200,200) with
// UNPACK_ALIGNMENT=1).
//
// Slot allocation (Slice 3C, 2026-05-20): a bump allocator backed by
// a free-list. allocate_slot pops a recycled index from the free-list
// first, else bumps the high-water cursor; free_slot returns an index
// to the free-list. The GlyphCache's LRU eviction (glyph_cache.rs)
// drives free_slot when a page fills — so a slide with enough distinct
// emoji codepoints to exhaust the 441-slot COLR page (or the 1764-slot
// MSDF page) recycles cold cells instead of wedging.

use anyhow::Result;
#[cfg(target_os = "linux")]
use anyhow::anyhow;
#[cfg(target_os = "linux")]
use glow::HasContext;

/// Page dimensions match vc4's 2048×2048 texture cap.
pub const ATLAS_DIM: u32 = 2048;

/// One slot in a page; `(x, y)` are top-left in atlas pixels.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct SlotPos {
    pub x: u32,
    pub y: u32,
}

/// GL pixel format for the page's backing texture. RGBA8 = the
/// historical default (kept for color glyphs / COLR); Rgb8 drops the
/// alpha byte and matches what the static slice-B MSDF atlases
/// already use (sdf_atlas_gl.rs:67-79 uploads GL_RGB / RGB888;
/// hdmi_logic.rs:1276-1278 documents the .rgb-only sampling).
///
/// MSDF (Chlumsky-style multi-channel SDF) samples only the RGB
/// channels — the alpha byte was always padding. Dropping it saves
/// 25% CMA per page (4 MB at 2048×2048).
#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum AtlasFormat {
    /// 4 bytes/pixel — color glyphs (COLR/CBDT). Premultiplied alpha
    /// is load-bearing here; the COLR rasterizer emits genuine RGBA.
    Rgba8,
    /// 3 bytes/pixel — MSDF only. Saves 4 MB CMA vs Rgba8 at
    /// 2048×2048. All four MSDF shaders sample `.rgb`; the alpha
    /// byte was a no-op pad.
    Rgb8,
}

impl AtlasFormat {
    /// Bytes per pixel for this format. Used by `upload_slot` for
    /// the byte-slice length check + the implicit row stride.
    pub const fn bytes_per_pixel(self) -> u32 {
        match self {
            AtlasFormat::Rgba8 => 4,
            AtlasFormat::Rgb8 => 3,
        }
    }
}

/// A single GPU-resident atlas page. Lifetime tied to the EGL session
/// that created it; AtlasPage::delete must run BEFORE the GL context
/// tears down.
pub struct AtlasPage {
    /// GL texture name. None before allocate_texture is called and
    /// after delete is called. Mac builds skip glow entirely; on
    /// non-Linux the field is a marker that's always None.
    #[cfg(target_os = "linux")]
    texture: Option<glow::NativeTexture>,
    cell_px: u32,
    cols: u32,
    rows: u32,
    /// Bump allocator high-water cursor. The next never-yet-used
    /// slot is at index `next_slot_idx`; the cursor only ever rises.
    next_slot_idx: u32,
    /// Slice 3C: recycled slot indices. allocate_slot pops here
    /// before bumping the cursor; free_slot pushes here. Drained
    /// LIFO — order doesn't matter, every free index is equivalent.
    free_list: Vec<u32>,
    /// GL pixel format for the backing texture. Selected at
    /// construction so `allocate_texture` + `upload_slot` know
    /// whether each cell carries 3 or 4 bytes per pixel. The
    /// default `new(cell_px)` constructor picks Rgba8 for back-
    /// compat with color-glyph callers; MSDF callers pick Rgb8
    /// explicitly via `new_rgb8`.
    format: AtlasFormat,
}

impl AtlasPage {
    /// Construct an empty RGBA8 page with the given cell size. Does
    /// NOT touch GL state; call allocate_texture once the GL context
    /// is current to upload an empty backing texture.
    ///
    /// RGBA8 is the historical default and stays the right choice
    /// for color-glyph pages (COLR / CBDT). MSDF callers should use
    /// `new_rgb8` instead — the alpha byte was always unused and
    /// dropping it saves 4 MB CMA per page.
    pub fn new(cell_px: u32) -> Self {
        Self::with_format(cell_px, AtlasFormat::Rgba8)
    }

    /// Construct an empty RGB8 page. Use for MSDF dynamic-glyph
    /// pages: all four MSDF shaders sample `.rgb` only, so the
    /// alpha byte was always padding. 2048×2048 → 12 MB CMA
    /// (vs 16 MB for RGBA8). The static slice-B MSDF atlases
    /// already use RGB888 on this vc4 GPU (sdf_atlas_gl.rs:67-79),
    /// so the format is empirically proven.
    pub fn new_rgb8(cell_px: u32) -> Self {
        Self::with_format(cell_px, AtlasFormat::Rgb8)
    }

    fn with_format(cell_px: u32, format: AtlasFormat) -> Self {
        let cols = ATLAS_DIM / cell_px;
        let rows = ATLAS_DIM / cell_px;
        Self {
            #[cfg(target_os = "linux")]
            texture: None,
            cell_px,
            cols,
            rows,
            next_slot_idx: 0,
            free_list: Vec::new(),
            format,
        }
    }

    /// The page's GL pixel format. Test-visible so cross-module
    /// tests can assert the MSDF page is RGB8 and the COLR page
    /// stays RGBA8.
    pub fn format(&self) -> AtlasFormat {
        self.format
    }

    /// Index → (x, y) top-left in atlas pixels, row-major.
    fn slot_pos_for_index(&self, idx: u32) -> SlotPos {
        SlotPos {
            x: (idx % self.cols) * self.cell_px,
            y: (idx / self.cols) * self.cell_px,
        }
    }

    /// Create the underlying GL texture (2048×2048, GL_LINEAR
    /// sampling, GL_CLAMP_TO_EDGE wrap, format determined by
    /// `self.format`: RGBA8 for color glyphs, RGB8 for MSDF).
    /// Idempotent: re-calling on an already-allocated page is a
    /// no-op.
    #[cfg(target_os = "linux")]
    pub fn allocate_texture(&mut self, gl: &glow::Context) -> Result<()> {
        if self.texture.is_some() {
            return Ok(());
        }
        unsafe {
            let tex = gl
                .create_texture()
                .map_err(|e| anyhow!("create_texture: {e}"))?;
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            // Allocate texture storage without uploading initial
            // pixels (GLES2 lets pixels=NULL just reserve driver-
            // side storage). 16 MB CPU heap + 16 MB upload at
            // bring-up was eating 150-300 ms per EglSession on Pi
            // Zero 2 W (smoke regression caught 2026-05-19, slice
            // 1 part B). Uninitialized contents are fine: every
            // dynamic slot is written via glTexSubImage2D before
            // it's sampled, and unwritten slots are never sampled
            // (allocate_slot tracks the high-water mark).
            //
            // 2026-06-24 RGB8 drop: format is now selected at
            // construction. RGB8 saves 4 MB CMA at 2048×2048 vs
            // RGBA8; vc4 supports both equivalently for MSDF
            // sampling (the static slice-B atlases have used RGB8
            // since the SDF arc shipped).
            let gl_fmt = match self.format {
                AtlasFormat::Rgba8 => glow::RGBA,
                AtlasFormat::Rgb8 => glow::RGB,
            };
            // Row alignment for the NULL upload: GLES2's
            // UNPACK_ALIGNMENT default is 4. The texture
            // allocation itself doesn't read pixels (None), but a
            // few drivers still consult the alignment to size
            // their internal padding. Set it to 1 for RGB8 so the
            // driver doesn't reserve a wider row stride than we'll
            // upload via upload_slot. Restore after.
            if self.format == AtlasFormat::Rgb8 {
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            }
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0,
                gl_fmt as i32,
                ATLAS_DIM as i32, ATLAS_DIM as i32, 0,
                gl_fmt, glow::UNSIGNED_BYTE,
                None,
            );
            if self.format == AtlasFormat::Rgb8 {
                gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            }
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MIN_FILTER, glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_MAG_FILTER, glow::LINEAR as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_S, glow::CLAMP_TO_EDGE as i32,
            );
            gl.tex_parameter_i32(
                glow::TEXTURE_2D, glow::TEXTURE_WRAP_T, glow::CLAMP_TO_EDGE as i32,
            );
            let err = gl.get_error();
            if err != glow::NO_ERROR {
                gl.delete_texture(tex);
                return Err(anyhow!("AtlasPage::allocate_texture: GL error 0x{err:x}"));
            }
            self.texture = Some(tex);
        }
        Ok(())
    }

    /// Delete the GL texture. Must be called BEFORE the GL context
    /// tears down or the texture leaks. After delete(), the page is
    /// inert; further upload_slot calls error.
    #[cfg(target_os = "linux")]
    pub fn delete(&mut self, gl: &glow::Context) {
        if let Some(tex) = self.texture.take() {
            unsafe { gl.delete_texture(tex); }
        }
    }

    /// Reserve a slot. Recycled (freed) slots are handed out first;
    /// otherwise the high-water cursor bumps. Returns None only when
    /// every slot is occupied AND none is free — the caller (Slice
    /// 3C: GlyphCache::poll_completions) responds by evicting an LRU
    /// slot via free_slot and retrying. Caller uploads pixels via
    /// upload_slot.
    pub fn allocate_slot(&mut self) -> Option<SlotPos> {
        if let Some(idx) = self.free_list.pop() {
            return Some(self.slot_pos_for_index(idx));
        }
        if self.next_slot_idx >= self.cols * self.rows {
            return None;
        }
        let idx = self.next_slot_idx;
        self.next_slot_idx += 1;
        Some(self.slot_pos_for_index(idx))
    }

    /// Return a slot to the free pool so a future allocate_slot can
    /// reuse its cell. Slice 3C eviction calls this on the LRU
    /// victim's slot. The cell's stale pixels are left as-is — every
    /// slot is fully overwritten by upload_slot before it is next
    /// sampled, so the garbage is never visible.
    ///
    /// Contract: `slot` must be a position previously returned by
    /// allocate_slot and not already freed. Double-freeing would
    /// hand the same cell to two glyphs; the GlyphCache guarantees
    /// one free per evicted Ready slot.
    pub fn free_slot(&mut self, slot: SlotPos) {
        let idx = (slot.y / self.cell_px) * self.cols + (slot.x / self.cell_px);
        self.free_list.push(idx);
    }

    /// Upload `pixels` (width*height*bpp bytes, top-left origin)
    /// into the texture region at (x, y), where bpp is 4 for RGBA8
    /// pages and 3 for RGB8 pages. Caller is responsible for
    /// matching width/height to the slot's cell_px. Returns error
    /// if the GL texture isn't allocated yet OR if glTexSubImage2D
    /// errored.
    ///
    /// Sub-texture upload semantics validated on vc4 by
    /// gl_subtexture_smoke (commit d99834d).
    #[cfg(target_os = "linux")]
    pub fn upload_slot(
        &self,
        gl: &glow::Context,
        x: u32,
        y: u32,
        width: u32,
        height: u32,
        pixels: &[u8],
    ) -> Result<()> {
        let tex = self.texture.ok_or_else(|| {
            anyhow!("upload_slot: AtlasPage has no texture (allocate_texture first)")
        })?;
        let bpp = self.format.bytes_per_pixel();
        let expected = (width * height * bpp) as usize;
        if pixels.len() != expected {
            return Err(anyhow!(
                "upload_slot: byte slice len {} != width*height*{bpp} = {} (format={:?})",
                pixels.len(), expected, self.format,
            ));
        }
        if x + width > ATLAS_DIM || y + height > ATLAS_DIM {
            return Err(anyhow!(
                "upload_slot: ({x}, {y}, {width}, {height}) exceeds {ATLAS_DIM}x{ATLAS_DIM}",
            ));
        }
        let gl_fmt = match self.format {
            AtlasFormat::Rgba8 => glow::RGBA,
            AtlasFormat::Rgb8 => glow::RGB,
        };
        unsafe {
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            // UNPACK_ALIGNMENT=1 to accept arbitrary row widths
            // (validated by smoke for 47-wide sub-regions; RGB8
            // cells have stride width*3 which is rarely 4-aligned
            // so the explicit 1 is mandatory for the new format).
            // Restore after.
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_sub_image_2d(
                glow::TEXTURE_2D, 0,
                x as i32, y as i32,
                width as i32, height as i32,
                gl_fmt, glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(pixels),
            );
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            let err = gl.get_error();
            if err != glow::NO_ERROR {
                return Err(anyhow!("upload_slot: GL error 0x{err:x}"));
            }
        }
        Ok(())
    }

    /// True if every slot is occupied AND none is free — the next
    /// allocate_slot would return None. False whenever a freed slot
    /// is available for recycling.
    pub fn is_full(&self) -> bool {
        self.next_slot_idx >= self.cols * self.rows && self.free_list.is_empty()
    }

    /// How many slots are currently occupied (high-water cursor
    /// minus the recycled-but-not-yet-reused free pool).
    pub fn allocated_count(&self) -> u32 {
        self.next_slot_idx - self.free_list.len() as u32
    }

    /// Total slot capacity (cols * rows).
    pub fn capacity(&self) -> u32 {
        self.cols * self.rows
    }

    /// GL texture name; None until allocate_texture runs.
    #[cfg(target_os = "linux")]
    pub fn texture(&self) -> Option<glow::NativeTexture> {
        self.texture
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn page_constructed_with_cell_px_48_has_expected_dims() {
        let page = AtlasPage::new(48);
        // 2048 / 48 = 42.66 → 42 cols/rows
        assert_eq!(page.cols, 42);
        assert_eq!(page.rows, 42);
        assert_eq!(page.cell_px, 48);
        assert_eq!(page.capacity(), 42 * 42);
        assert_eq!(page.allocated_count(), 0);
        assert!(!page.is_full());
        #[cfg(target_os = "linux")]
        assert!(page.texture().is_none());
    }

    #[test]
    fn default_constructor_picks_rgba8_format() {
        // Back-compat: existing callers expect RGBA8 (COLR /
        // CBDT). Switching that silently to RGB8 would break the
        // alpha channel that color glyphs depend on.
        assert_eq!(AtlasPage::new(48).format(), AtlasFormat::Rgba8);
        assert_eq!(AtlasPage::new(96).format(), AtlasFormat::Rgba8);
    }

    #[test]
    fn new_rgb8_constructor_picks_rgb8_format() {
        // MSDF dynamic page selects RGB8 explicitly; the alpha
        // byte was unused (all four MSDF shaders sample .rgb).
        assert_eq!(AtlasPage::new_rgb8(48).format(), AtlasFormat::Rgb8);
    }

    #[test]
    fn rgb8_format_bytes_per_pixel_is_three() {
        // Headline saving: 25% per-cell vs Rgba8. At 2048×2048
        // that's 16 MB → 12 MB.
        assert_eq!(AtlasFormat::Rgba8.bytes_per_pixel(), 4);
        assert_eq!(AtlasFormat::Rgb8.bytes_per_pixel(), 3);
    }

    #[test]
    fn page_constructed_with_cell_px_96_emoji_size_has_expected_dims() {
        let page = AtlasPage::new(96);
        // 2048 / 96 = 21.33 → 21 cols/rows
        assert_eq!(page.cols, 21);
        assert_eq!(page.rows, 21);
        assert_eq!(page.capacity(), 441);
    }

    #[test]
    fn allocate_slot_returns_grid_positions_in_row_major_order() {
        let mut page = AtlasPage::new(48);
        let s0 = page.allocate_slot().unwrap();
        assert_eq!(s0, SlotPos { x: 0, y: 0 });
        let s1 = page.allocate_slot().unwrap();
        assert_eq!(s1, SlotPos { x: 48, y: 0 });
        // Consume the rest of row 0 (40 more slots, indices 2..=41).
        let mut last_in_row_0 = None;
        for _ in 0..40 {
            last_in_row_0 = page.allocate_slot();
        }
        // After indices 0..=41 we've filled row 0; next slot wraps to
        // (0, 48).
        assert_eq!(last_in_row_0.unwrap(), SlotPos { x: 41 * 48, y: 0 });
        let s_next_row = page.allocate_slot().unwrap();
        assert_eq!(s_next_row, SlotPos { x: 0, y: 48 });
    }

    #[test]
    fn allocate_slot_returns_none_when_full() {
        let mut page = AtlasPage::new(48);
        for _ in 0..page.capacity() {
            assert!(page.allocate_slot().is_some());
        }
        assert!(page.is_full());
        assert!(page.allocate_slot().is_none());
        assert_eq!(page.allocated_count(), 42 * 42);
    }

    #[test]
    fn allocated_count_tracks_consumed_slots() {
        let mut page = AtlasPage::new(48);
        assert_eq!(page.allocated_count(), 0);
        page.allocate_slot();
        page.allocate_slot();
        page.allocate_slot();
        assert_eq!(page.allocated_count(), 3);
    }

    // upload_slot's bounds + size checks are testable without a GL
    // context because they error before touching gl.tex_sub_image_2d.
    // Skipping the actual-upload tests; those need an EGL session and
    // are covered by gl_subtexture_smoke + the runtime integration.

    #[test]
    fn free_slot_recycles_for_next_allocate() {
        // Slice 3C: a freed slot is handed back by the next
        // allocate_slot before the high-water cursor bumps.
        let mut page = AtlasPage::new(96);
        let s0 = page.allocate_slot().unwrap();
        let s1 = page.allocate_slot().unwrap();
        assert_eq!(page.allocated_count(), 2);
        // Free the first slot; allocate again -> reuses s0's cell.
        page.free_slot(s0);
        assert_eq!(page.allocated_count(), 1);
        let reused = page.allocate_slot().unwrap();
        assert_eq!(reused, s0, "allocate must recycle the freed cell");
        assert_eq!(page.allocated_count(), 2);
        // s1 was never freed and must not be re-handed-out.
        let s2 = page.allocate_slot().unwrap();
        assert_ne!(s2, s1);
        assert_ne!(s2, s0);
    }

    #[test]
    fn full_page_unwedges_after_a_free() {
        // The Slice 3C core promise: a full page is not a dead end.
        let mut page = AtlasPage::new(96); // 21x21 = 441 slots
        let mut slots = Vec::new();
        for _ in 0..page.capacity() {
            slots.push(page.allocate_slot().unwrap());
        }
        assert!(page.is_full());
        assert!(page.allocate_slot().is_none());
        // Free one cold slot -> page is no longer full -> allocate
        // succeeds, handing back exactly the freed cell.
        let victim = slots[100];
        page.free_slot(victim);
        assert!(!page.is_full());
        assert_eq!(page.allocate_slot().unwrap(), victim);
        assert!(page.is_full());
    }

    #[test]
    fn slot_pos_inequality_works() {
        assert_eq!(SlotPos { x: 0, y: 0 }, SlotPos { x: 0, y: 0 });
        assert_ne!(SlotPos { x: 0, y: 0 }, SlotPos { x: 48, y: 0 });
        assert_ne!(SlotPos { x: 0, y: 0 }, SlotPos { x: 0, y: 48 });
    }
}
