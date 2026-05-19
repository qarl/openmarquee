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
// Slot allocation is a simple bump allocator for Slice 1; once a page
// fills, allocate_slot returns None. LRU eviction is a Slice 1.x
// follow-up triggered by first observed cache pressure (the projected
// working set for FYS is <50 dynamic codepoints, well under one
// 1764-slot page's capacity, so eviction is non-urgent).

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
    /// Bump allocator cursor. Next slot is at index `next_slot_idx`.
    /// allocate_slot returns None when next_slot_idx == cols * rows.
    next_slot_idx: u32,
}

impl AtlasPage {
    /// Construct an empty page with the given cell size. Does NOT
    /// touch GL state; call allocate_texture once the GL context is
    /// current to upload an empty (transparent-black) backing
    /// texture.
    pub fn new(cell_px: u32) -> Self {
        let cols = ATLAS_DIM / cell_px;
        let rows = ATLAS_DIM / cell_px;
        Self {
            #[cfg(target_os = "linux")]
            texture: None,
            cell_px,
            cols,
            rows,
            next_slot_idx: 0,
        }
    }

    /// Create the underlying GL texture (RGBA8, 2048×2048, GL_LINEAR
    /// sampling, GL_CLAMP_TO_EDGE wrap) and clear it to fully
    /// transparent. Idempotent: re-calling on an already-allocated
    /// page is a no-op.
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
            // Allocate texture storage with transparent-black pixels.
            // For 2048×2048×4 bytes = 16 MB; allocated once per page
            // lifetime; subsequent updates use glTexSubImage2D.
            let zero = vec![0u8; (ATLAS_DIM * ATLAS_DIM * 4) as usize];
            gl.tex_image_2d(
                glow::TEXTURE_2D, 0,
                glow::RGBA as i32,
                ATLAS_DIM as i32, ATLAS_DIM as i32, 0,
                glow::RGBA, glow::UNSIGNED_BYTE,
                Some(&zero),
            );
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

    /// Reserve the next free slot in the page. Returns None when the
    /// page is full (cols * rows slots claimed). Caller is responsible
    /// for actually uploading pixels via upload_slot.
    pub fn allocate_slot(&mut self) -> Option<SlotPos> {
        if self.next_slot_idx >= self.cols * self.rows {
            return None;
        }
        let idx = self.next_slot_idx;
        self.next_slot_idx += 1;
        Some(SlotPos {
            x: (idx % self.cols) * self.cell_px,
            y: (idx / self.cols) * self.cell_px,
        })
    }

    /// Upload `rgba_bytes` (width*height*4 bytes, top-left origin) into
    /// the texture region at (x, y). Caller is responsible for matching
    /// width/height to the slot's cell_px. Returns error if the GL
    /// texture isn't allocated yet OR if glTexSubImage2D errored.
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
        rgba_bytes: &[u8],
    ) -> Result<()> {
        let tex = self.texture.ok_or_else(|| {
            anyhow!("upload_slot: AtlasPage has no texture (allocate_texture first)")
        })?;
        let expected = (width * height * 4) as usize;
        if rgba_bytes.len() != expected {
            return Err(anyhow!(
                "upload_slot: byte slice len {} != width*height*4 = {}",
                rgba_bytes.len(), expected,
            ));
        }
        if x + width > ATLAS_DIM || y + height > ATLAS_DIM {
            return Err(anyhow!(
                "upload_slot: ({x}, {y}, {width}, {height}) exceeds {ATLAS_DIM}x{ATLAS_DIM}",
            ));
        }
        unsafe {
            gl.bind_texture(glow::TEXTURE_2D, Some(tex));
            // UNPACK_ALIGNMENT=1 to accept arbitrary row widths
            // (validated by smoke for 47-wide sub-regions). Restore
            // after.
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 1);
            gl.tex_sub_image_2d(
                glow::TEXTURE_2D, 0,
                x as i32, y as i32,
                width as i32, height as i32,
                glow::RGBA, glow::UNSIGNED_BYTE,
                glow::PixelUnpackData::Slice(rgba_bytes),
            );
            gl.pixel_store_i32(glow::UNPACK_ALIGNMENT, 4);
            let err = gl.get_error();
            if err != glow::NO_ERROR {
                return Err(anyhow!("upload_slot: GL error 0x{err:x}"));
            }
        }
        Ok(())
    }

    /// True if every slot is claimed.
    pub fn is_full(&self) -> bool {
        self.next_slot_idx >= self.cols * self.rows
    }

    /// How many slots have been allocated.
    pub fn allocated_count(&self) -> u32 {
        self.next_slot_idx
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
    fn slot_pos_inequality_works() {
        assert_eq!(SlotPos { x: 0, y: 0 }, SlotPos { x: 0, y: 0 });
        assert_ne!(SlotPos { x: 0, y: 0 }, SlotPos { x: 48, y: 0 });
        assert_ne!(SlotPos { x: 0, y: 0 }, SlotPos { x: 0, y: 48 });
    }
}
