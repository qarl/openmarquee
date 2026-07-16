//! Colorlight headless GPU compositor — Linux-only Pattern A.
//!
//! Second implementation of the `Compositor` trait shipped in PR #90;
//! the first was `CpuCompositor` (tiny_skia-backed, cross-platform).
//! This one runs the real design-doc §5 "Pattern A" bring-up: GBM +
//! EGL + GLES2, MINUS DRM modeset/page-flip.  Composites a solid
//! `TestPattern` via `glClear` on the current GBM surface, reads
//! back via `glReadPixels` (RGBA), strips alpha, hands the caller a
//! card-native RGB888 buffer.
//!
//! ## Two-callers scoping
//!
//! Shares the GBM+EGL bring-up chain with `hdmi.rs`'s DRM scanout
//! path via `crate::egl_bringup::HeadlessEgl` (extracted in PR #B0.5
//! Commit 1).  Any behavior regression in the shared primitive would
//! land on both callers.
//!
//! ## Phase-0 scope humility
//!
//! Supports the solid-colour `TestPattern` variants only
//! (`SolidBlack/White/Red/Green/Blue`).  `Checkerboard8` and
//! `Gradient` return `CompositorError::Backend(...)` with an operator
//! message directing them to `CpuCompositor` (which does support
//! those patterns via tiny_skia CPU rasterization).  Rationale:
//! solid colours only need `glClear`; patterned rendering needs a
//! fragment shader + textured quad, which balloons this module past
//! the Phase-0 "prove GBM+EGL bring-up + readback works" ceiling
//! admin scoped for #B0.5.  Real-content compositing lands in #B1
//! when the paint pipeline wires its output into a `Compositor`.

use crate::colorlight_compositor::{Compositor, CompositorError, TestPattern};
use crate::egl_bringup::{EglBringUpSpec, HeadlessEgl};
use crate::Card;
use anyhow::Result;

/// Card-native default width, mirrors
/// `colorlight_compositor::DEFAULT_CARD_WIDTH_PX`.
pub const DEFAULT_CARD_WIDTH_PX: u32 = crate::colorlight_compositor::DEFAULT_CARD_WIDTH_PX;
/// Card-native default height, mirrors
/// `colorlight_compositor::DEFAULT_CARD_HEIGHT_PX`.
pub const DEFAULT_CARD_HEIGHT_PX: u32 = crate::colorlight_compositor::DEFAULT_CARD_HEIGHT_PX;

/// Real GPU-backed Colorlight compositor.  Owns the paired
/// `HeadlessEgl` (bring-up + tear-down via Drop).  Renders per
/// `produce_frame` by `glClear`-ing the current GBM surface to the
/// pattern's colour, then `glReadPixels`-ing back to RGB888.
///
/// Constructed with a `Card` — passes the DRM fd through to
/// `egl_bringup::bring_up_egl` for the `gbm_create_device` call.
/// The `Card` doesn't need CRTC / connector / modeset permission;
/// it just needs to open the DRM node (`/dev/dri/card0` typically).
pub struct HeadlessGpuCompositor {
    egl: HeadlessEgl,
    width: u32,
    height: u32,
    pattern: TestPattern,
}

impl HeadlessGpuCompositor {
    /// Construct at arbitrary dims.  Fails cleanly if GBM/EGL bring-
    /// up fails (no DRM node, permission denied, no vc4 driver,
    /// etc.) — caller should treat the error as "GPU compositor
    /// unavailable, fall back to CpuCompositor" per the arm's
    /// `--output colorlight` skeleton.
    pub fn new(
        width: u32,
        height: u32,
        pattern: TestPattern,
        card: &Card,
    ) -> Result<Self, CompositorError> {
        if width == 0 || height == 0 {
            return Err(CompositorError::InvalidDimensions {
                w: width,
                h: height,
                reason: "zero dimension",
            });
        }
        // Match `CpuCompositor::new`'s ceiling.  Belt-and-braces:
        // `produce_frame` allocates `w*h*4` as u32; a bad caller
        // asking for u32::MAX × u32::MAX would wrap the allocation
        // sizing (17e9 > u32::MAX) → `read_pixels` writes past the
        // Vec = UB.  Card-native max under Colorlight spec is a few
        // hundred px; 4096 is a defensible ceiling.
        const MAX_DIM: u32 = 4096;
        if width > MAX_DIM || height > MAX_DIM {
            return Err(CompositorError::InvalidDimensions {
                w: width,
                h: height,
                reason: "exceeds MAX_DIM (4096)",
            });
        }
        let spec = EglBringUpSpec::for_headless_compositor(width, height);
        let egl = HeadlessEgl::new(&spec, card).map_err(|e| {
            CompositorError::Backend(format!("headless EGL bring-up: {e}"))
        })?;
        Ok(Self {
            egl,
            width,
            height,
            pattern,
        })
    }

    /// The 128×96 card-native default.  Mirrors
    /// `CpuCompositor::card_default` but returns `Result` because
    /// the GPU bring-up can fail at runtime (unlike the CPU path).
    pub fn card_default(pattern: TestPattern, card: &Card) -> Result<Self, CompositorError> {
        Self::new(
            DEFAULT_CARD_WIDTH_PX,
            DEFAULT_CARD_HEIGHT_PX,
            pattern,
            card,
        )
    }

    /// Read-only pattern peek (used by the arm-fill diagnostic).
    pub fn pattern(&self) -> TestPattern {
        self.pattern
    }

    /// Solid-colour → `(r, g, b, a)` triple for `glClearColor`.  For
    /// non-solid patterns returns `None` so the caller can surface
    /// a specific `Backend` error naming the shader-less limitation.
    fn solid_clear_color(pattern: TestPattern) -> Option<(f32, f32, f32, f32)> {
        match pattern {
            TestPattern::SolidBlack => Some((0.0, 0.0, 0.0, 1.0)),
            TestPattern::SolidWhite => Some((1.0, 1.0, 1.0, 1.0)),
            TestPattern::SolidRed => Some((1.0, 0.0, 0.0, 1.0)),
            TestPattern::SolidGreen => Some((0.0, 1.0, 0.0, 1.0)),
            TestPattern::SolidBlue => Some((0.0, 0.0, 1.0, 1.0)),
            TestPattern::Checkerboard8 | TestPattern::Gradient => None,
        }
    }
}

impl Compositor for HeadlessGpuCompositor {
    fn produce_frame(&mut self) -> Result<Vec<u8>, CompositorError> {
        use glow::HasContext;
        let (r, g, b, a) = Self::solid_clear_color(self.pattern).ok_or_else(|| {
            CompositorError::Backend(format!(
                "HeadlessGpuCompositor cannot render {:?} in Phase 0 (only solid \
                 colours supported via glClear); use CpuCompositor for patterned \
                 rasterization or land a fragment-shader path in a follow-up",
                self.pattern
            ))
        })?;
        let gl = &self.egl.handles().gl;
        // glReadPixels in GLES2 requires GL_RGBA (4 bytes/px) — read
        // that then strip alpha to hand the caller RGB888.  The size
        // is card-native (128×96 = 49152 RGBA bytes = 36864 RGB
        // bytes), so allocation is trivial per frame.
        let mut rgba = vec![0u8; (self.width * self.height * 4) as usize];
        unsafe {
            // Sink any pending GL errors accumulated before this call
            // so the post-block `get_error` narrows any surfaced
            // error to the block itself (viewport/clear/finish/
            // read_pixels).  Bounded loop — spec allows up to 8
            // simultaneous error flags but no real driver stacks
            // more than a handful.
            for _ in 0..16 {
                if gl.get_error() == glow::NO_ERROR {
                    break;
                }
            }
            gl.viewport(0, 0, self.width as i32, self.height as i32);
            gl.clear_color(r, g, b, a);
            gl.clear(glow::COLOR_BUFFER_BIT);
            // Force GPU flush so read_pixels sees the cleared frame,
            // not a queued command.  Cheap on a solid clear.
            gl.finish();
            gl.read_pixels(
                0,
                0,
                self.width as i32,
                self.height as i32,
                glow::RGBA,
                glow::UNSIGNED_BYTE,
                glow::PixelPackData::Slice(&mut rgba),
            );
            let err = gl.get_error();
            if err != glow::NO_ERROR {
                // The error could have originated in ANY of the
                // viewport/clear/finish/read_pixels calls above
                // (get_error is a driver-side latched flag; only the
                // FIRST error since the last get_error is reported).
                // Message wording reflects that.
                return Err(CompositorError::Backend(format!(
                    "GL error 0x{err:x} in headless compose block (viewport/clear/finish/read_pixels)"
                )));
            }
        }
        // Strip alpha → RGB888.
        //
        // Y-flip note: `glReadPixels` reads BOTTOM-UP in GLES2
        // (y=0 is the bottom row of the framebuffer), whereas the
        // Colorlight encoder + card-native RGB888 is TOP-DOWN
        // (y=0 is the top row).  For SOLID-colour patterns this is
        // invisible (every row is identical).  When a future
        // fragment-shader path lands to render non-solid patterns,
        // it MUST either flip the shader's y coordinate or reverse
        // the row order during this strip.  Documented here so a
        // future dev spots the constraint before adding gradient
        // shader support.
        let mut rgb = Vec::with_capacity((self.width * self.height * 3) as usize);
        for chunk in rgba.chunks_exact(4) {
            rgb.extend_from_slice(&chunk[..3]);
        }
        Ok(rgb)
    }

    fn card_width_px(&self) -> u32 {
        self.width
    }

    fn card_height_px(&self) -> u32 {
        self.height
    }
}

// ── Tests ────────────────────────────────────────────────────────────────
//
// `HeadlessGpuCompositor` requires a real DRM device (`/dev/dri/*`)
// to bring up GBM+EGL, so its `produce_frame` path CANNOT be
// exercised in CI (macOS lacks GBM entirely; Linux CI runners
// typically don't have vc4 or a card device).  What we CAN test on
// any host:
//
// - `solid_clear_color` — the pure per-pattern lookup, no GL touch.
// - Constructor input validation (zero dims) — also no GL touch.
//
// The full compositor → encoder → sink pipeline is exercised by
// `CpuCompositor` tests already (PR #90) via the shared `Compositor`
// trait boundary.  Any regression in the trait contract lands there.

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn solid_clear_color_covers_every_solid_pattern() {
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::SolidBlack),
            Some((0.0, 0.0, 0.0, 1.0))
        );
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::SolidWhite),
            Some((1.0, 1.0, 1.0, 1.0))
        );
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::SolidRed),
            Some((1.0, 0.0, 0.0, 1.0))
        );
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::SolidGreen),
            Some((0.0, 1.0, 0.0, 1.0))
        );
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::SolidBlue),
            Some((0.0, 0.0, 1.0, 1.0))
        );
    }

    #[test]
    fn solid_clear_color_none_for_shader_patterns() {
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::Checkerboard8),
            None
        );
        assert_eq!(
            HeadlessGpuCompositor::solid_clear_color(TestPattern::Gradient),
            None
        );
    }
}
