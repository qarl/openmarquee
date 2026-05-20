//! Bug 3 Slice 3A.rev (2026-05-19): runtime COLRv1 vector emoji
//! rasterization for the dynamic glyph cache.
//!
//! Mirrors the shape of `glyph_cache::rasterize_msdf_cell` for the
//! MSDF path but reads the font via `skrifa::FontRef` and traverses
//! the COLRv1 paint tree via `ColorGlyph::paint` into a tiny-skia
//! `Pixmap`. Output is a fixed-cell RGBA8 buffer the GL upload path
//! can blit into the dynamic atlas page exactly like the static
//! CBDT atlas cells.
//!
//! swash was the initial pick per the QA dispatch text, but swash
//! 0.2.7's COLR parser is COLRv0-only (reads `numBaseGlyphRecords`
//! / `baseGlyphRecordsOffset` directly from the v0 header layout
//! in swash/src/scale/color.rs; no v1 paint-tree support). Pivoted
//! to skrifa + tiny-skia per QA Path A approval: skrifa exposes
//! a full v1 paint-tree traversal via the ColorPainter trait, and
//! tiny-skia covers all four COLRv1 brush variants (Solid, Linear,
//! Radial, Sweep) plus push_layer composite modes.
//!
//! Slice 3A is skeleton + worker dispatch only — no caller emits
//! `MissRequest` with `RenderMode::Colr` until Slice 3B wires the
//! `layout_text_to_quads` dispatch hook for emoji codepoints that
//! miss the build-time CBDT atlas. Slice 3D retires the CBDT bake.

use std::path::Path;

use skrifa::{FontRef, GlyphId, MetadataProvider};
use skrifa::color::{
    Brush, ColorPainter, ColorStop, CompositeMode, Extend, Transform as SkrifaTransform,
};
use skrifa::instance::{LocationRef, Size};
use skrifa::outline::{DrawSettings, OutlinePen};
use skrifa::raw::types::BoundingBox;
use tiny_skia as ts;

use crate::glyph_cache::{PlaneBounds, RasterOutput};

/// Atlas cell dimensions for COLRv1-rasterized emoji. 96 px matches
/// the existing CBDT bake (build.rs `EMOJI_CELL_PX`) so the dynamic
/// atlas page layout + Slice 3B's `CharKind::DynamicEmoji` dispatch
/// can reuse the same per-cell UV / pixel math. Slice 3C may revisit
/// if HDMI signage at 1080p wants larger source PPEM.
pub(crate) const COLR_CELL_PX: u32 = 96;

/// Rasterize one codepoint to a `COLR_CELL_PX^2` RGBA cell via
/// skrifa's COLRv1 paint-tree traversal + tiny-skia. Returns:
///   Ok(Some(out)) — successful rasterization
///   Ok(None)      — font is loadable but lacks this codepoint OR
///                   has no v1 (or v0) color glyph for it
///   Err(e)        — I/O or font-parse error
pub(crate) fn rasterize_colr_cell(
    font_path: &Path,
    codepoint: u32,
) -> Result<Option<RasterOutput>, anyhow::Error> {
    let ttf_bytes = std::fs::read(font_path)
        .map_err(|e| anyhow::anyhow!("read TTF {:?}: {e}", font_path))?;
    let font = FontRef::new(&ttf_bytes)
        .map_err(|e| anyhow::anyhow!("parse TTF {:?}: {e:?}", font_path))?;

    let glyph_id = match font.charmap().map(codepoint) {
        Some(g) if g != GlyphId::NOTDEF => g,
        // None (no cmap entry) OR mapping to GID 0 both mean
        // the font lacks a real glyph for this codepoint.
        _ => return Ok(None),
    };

    let metrics = font.metrics(Size::unscaled(), LocationRef::default());
    let upem = metrics.units_per_em as f32;
    if upem <= 0.0 {
        return Err(anyhow::anyhow!(
            "TTF {:?}: units_per_em is non-positive ({upem})",
            font_path,
        ));
    }

    // Resolve the color-glyph entry (prefers COLRv1 over v0). If the
    // codepoint exists in cmap but has no color representation (e.g.
    // a fallback glyph), return None so the dispatch ladder falls
    // through to MSDF / Tofu.
    let color_glyph = match font.color_glyphs().get(glyph_id) {
        Some(cg) => cg,
        None => return Ok(None),
    };

    // Output target.
    let mut pixmap = ts::Pixmap::new(COLR_CELL_PX, COLR_CELL_PX)
        .ok_or_else(|| anyhow::anyhow!("Pixmap::new({COLR_CELL_PX}) failed"))?;

    // Initial transform: skrifa's paint callbacks emit ops in FONT
    // UNITS (Y-up). Map them into pixmap pixel space (Y-down,
    // origin top-left) by scaling by ppem/upem and Y-flipping so
    // baseline (font y=0) lands at canvas y=ppem, and the top of
    // the em-box (font y=upem) lands at canvas y=0. Glyphs that
    // extend above the cap or below the baseline beyond the em-box
    // get cropped by the pixmap — acceptable since the dynamic
    // atlas slot is fixed-size; Slice 3C can revisit if a glyph
    // family wants more headroom.
    let ppem = COLR_CELL_PX as f32;
    let scale = ppem / upem;
    let root_transform = ts::Transform::from_row(scale, 0.0, 0.0, -scale, 0.0, ppem);

    // Spool the paint tree into the painter.
    let mut painter = TinySkiaColorPainter::new(&mut pixmap, &font, ppem, upem, root_transform);
    color_glyph
        .paint(LocationRef::default(), &mut painter)
        .map_err(|e| anyhow::anyhow!("ColorGlyph::paint: {e:?}"))?;

    // Advance width — in em-units, matches the MSDF side's
    // RasterOutput.advance_em convention.
    let glyph_metrics = font.glyph_metrics(Size::unscaled(), LocationRef::default());
    let advance_em = glyph_metrics.advance_width(glyph_id).unwrap_or(0.0) / upem;

    // Plane bounds — prefer the glyph's COLRv1 clipbox if present
    // (defines the precise drawable region per the spec). Otherwise
    // fall back to the entire em-box so the dispatch site at least
    // has a non-degenerate value to scale by. Slice 3B can refine
    // by reading actual painted-pixel extents if the clipbox proves
    // too generous in practice.
    let clipbox_em = color_glyph
        .bounding_box(LocationRef::default(), Size::unscaled())
        .map(|bb| em_bounds_from(bb, upem))
        .unwrap_or(PlaneBounds {
            pl_left: 0.0,
            pl_right: 1.0,
            pl_bottom: 0.0,
            pl_top: 1.0,
        });

    let rgba_bytes = pixmap.data().to_vec();

    Ok(Some(RasterOutput {
        rgba_bytes,
        cell_px: COLR_CELL_PX,
        advance_em,
        plane_bounds: clipbox_em,
    }))
}

fn em_bounds_from(bb: BoundingBox<f32>, upem: f32) -> PlaneBounds {
    PlaneBounds {
        pl_left: bb.x_min / upem,
        pl_right: bb.x_max / upem,
        pl_top: bb.y_max / upem,
        pl_bottom: bb.y_min / upem,
    }
}

// ---------- TinySkiaColorPainter ----------

/// ColorPainter impl that drives a tiny-skia Pixmap. Maintains the
/// transform / clip / layer stacks the COLRv1 paint-tree traversal
/// requires; the current frame's transform is the product of all
/// pushed transforms in order, current clip is the topmost mask,
/// current draw target is the topmost layer's Pixmap (or the root
/// Pixmap if no layer is pushed).
struct TinySkiaColorPainter<'a, 'font> {
    /// The root pixmap (Slice 3A's final-output cell).
    root: &'a mut ts::Pixmap,
    /// Source font, held for outline extraction in push_clip_glyph
    /// and palette lookup in fill().
    font: &'a FontRef<'font>,
    /// Outline glyph collection (for clip glyph paths).
    outlines: skrifa::outline::OutlineGlyphCollection<'font>,
    /// PPEM (= COLR_CELL_PX as f32). Skrifa emits in font units;
    /// outline paths come in scaled units when we ask for them via
    /// DrawSettings::Unhinted(Size::new(ppem), location).
    ppem: f32,
    /// Em units per em (1024, 2048, etc).
    upem: f32,
    /// Current accumulated transform = product of `transform_stack`.
    /// Stored separately to avoid recomputing per fill().
    transform: ts::Transform,
    transform_stack: Vec<ts::Transform>,
    /// Clip stack. Each frame is the alpha-mask snapshot at the
    /// time of push_clip_*; pop_clip restores the previous.
    clip_stack: Vec<Option<ts::Mask>>,
    /// Layer stack. Each frame: (offscreen pixmap to draw into,
    /// composite mode to merge down on pop).
    layer_stack: Vec<LayerFrame>,
}

struct LayerFrame {
    pixmap: ts::Pixmap,
    composite_mode: CompositeMode,
}

impl<'a, 'font> TinySkiaColorPainter<'a, 'font> {
    fn new(
        root: &'a mut ts::Pixmap,
        font: &'a FontRef<'font>,
        ppem: f32,
        upem: f32,
        root_transform: ts::Transform,
    ) -> Self {
        let outlines = font.outline_glyphs();
        Self {
            root,
            font,
            outlines,
            ppem,
            upem,
            transform: root_transform,
            transform_stack: Vec::new(),
            clip_stack: vec![None],
            layer_stack: Vec::new(),
        }
    }

    /// Current draw target — the topmost layer's pixmap, or the
    /// root pixmap if no layer is active.
    fn target(&mut self) -> ts::PixmapMut<'_> {
        if let Some(layer) = self.layer_stack.last_mut() {
            layer.pixmap.as_mut()
        } else {
            self.root.as_mut()
        }
    }

    /// Current clip mask, if any.
    fn current_clip(&self) -> Option<&ts::Mask> {
        self.clip_stack.last().and_then(|c| c.as_ref())
    }

    /// Build a tiny-skia Path for the given glyph at the current PPEM,
    /// returning None if the glyph has no outline (composite-only,
    /// empty, etc).
    fn build_glyph_path(&mut self, glyph_id: GlyphId) -> Option<ts::Path> {
        let outline = self.outlines.get(glyph_id)?;
        let mut pen = PathBuilderPen::new();
        // Request unscaled outline so the points come back in font
        // units; the root transform handles the ppem scaling.
        let settings = DrawSettings::unhinted(Size::unscaled(), LocationRef::default());
        outline.draw(settings, &mut pen).ok()?;
        pen.into_path()
    }
}

/// Convert skrifa's column-order Transform into a tiny-skia row-
/// order Transform. Skrifa: x' = xx*x + xy*y + dx, y' = yx*x + yy*y
/// + dy. tiny-skia `from_row(sx, ky, kx, sy, tx, ty)` applies:
/// x' = sx*x + kx*y + tx, y' = ky*x + sy*y + ty. Mapping is direct:
/// xx→sx, xy→kx, dx→tx, yx→ky, yy→sy, dy→ty.
fn ts_transform_from(t: SkrifaTransform) -> ts::Transform {
    ts::Transform::from_row(t.xx, t.yx, t.xy, t.yy, t.dx, t.dy)
}

impl<'a, 'font> ColorPainter for TinySkiaColorPainter<'a, 'font> {
    fn push_transform(&mut self, transform: SkrifaTransform) {
        let local = ts_transform_from(transform);
        self.transform_stack.push(self.transform);
        // skrifa concatenation: current_after = current_before * local
        // (apply local FIRST, then current). tiny-skia `pre_concat`
        // does exactly that: result.map_point(p) = self.map(local.map(p)).
        self.transform = self.transform.pre_concat(local);
    }

    fn pop_transform(&mut self) {
        if let Some(t) = self.transform_stack.pop() {
            self.transform = t;
        }
    }

    fn push_clip_glyph(&mut self, glyph_id: GlyphId) {
        // Build the glyph's outline as a path in font units, then
        // bake into a Mask at pixmap resolution using the current
        // transform. Mask::fill_path applies the transform — no
        // need to pre-transform the path.
        let path = match self.build_glyph_path(glyph_id) {
            Some(p) => p,
            None => {
                // Empty / missing outline. Push a None mask so
                // pop_clip's positional bookkeeping stays right;
                // subsequent fills will not draw anywhere (which
                // is the COLRv1 semantic for a degenerate clip).
                self.clip_stack.push(None);
                return;
            }
        };

        let (w, h) = (self.root.width(), self.root.height());
        let mut mask = ts::Mask::new(w, h).expect("Mask::new size matches root pixmap");
        mask.fill_path(&path, ts::FillRule::Winding, true, self.transform);
        self.clip_stack.push(Some(mask));
    }

    fn push_clip_box(&mut self, clip_box: BoundingBox<f32>) {
        // BoundingBox is in font units. Convert to a rect path,
        // then apply current_transform via Mask::fill_path. Y-flip
        // is already part of the root transform.
        let mut pb = ts::PathBuilder::new();
        pb.move_to(clip_box.x_min, clip_box.y_min);
        pb.line_to(clip_box.x_max, clip_box.y_min);
        pb.line_to(clip_box.x_max, clip_box.y_max);
        pb.line_to(clip_box.x_min, clip_box.y_max);
        pb.close();
        let path = match pb.finish() {
            Some(p) => p,
            None => {
                self.clip_stack.push(None);
                return;
            }
        };

        let (w, h) = (self.root.width(), self.root.height());
        let mut mask = ts::Mask::new(w, h).expect("Mask::new size matches root pixmap");
        mask.fill_path(&path, ts::FillRule::Winding, true, self.transform);
        self.clip_stack.push(Some(mask));
    }

    fn pop_clip(&mut self) {
        if self.clip_stack.len() > 1 {
            self.clip_stack.pop();
        }
        // Always preserve at least one (root) clip slot; Slice 3A
        // pushes a None at construction so push/pop pairs balance.
    }

    fn fill(&mut self, brush: Brush<'_>) {
        // Paint the current clip with the brush. tiny-skia gradients
        // accept a transform on construction (the brush coordinate
        // system is font units, the canvas is pixmap pixels — so the
        // current self.transform exactly maps them); solid fills are
        // transform-agnostic.
        let target_w = self.root.width() as f32;
        let target_h = self.root.height() as f32;
        let full_rect = match ts::Rect::from_xywh(0.0, 0.0, target_w, target_h) {
            Some(r) => r,
            None => return,
        };

        let brush_transform = self.transform;
        let shader = match brush {
            Brush::Solid {
                palette_index,
                alpha,
            } => Some(ts::Shader::SolidColor(
                color_from_cpal(self.font, palette_index, alpha)
                    .unwrap_or_else(|| ts::Color::from_rgba8(128, 128, 128, 255)),
            )),
            Brush::LinearGradient {
                p0,
                p1,
                color_stops,
                extend,
            } => make_linear_gradient(
                self.font,
                (p0.x, p0.y),
                (p1.x, p1.y),
                color_stops,
                extend,
                brush_transform,
            ),
            Brush::RadialGradient {
                c0,
                r0,
                c1,
                r1,
                color_stops,
                extend,
            } => make_radial_gradient(
                self.font,
                (c0.x, c0.y),
                r0,
                (c1.x, c1.y),
                r1,
                color_stops,
                extend,
                brush_transform,
            ),
            Brush::SweepGradient {
                c0,
                start_angle,
                end_angle,
                color_stops,
                extend,
            } => make_sweep_gradient(
                self.font,
                (c0.x, c0.y),
                start_angle,
                end_angle,
                color_stops,
                extend,
                brush_transform,
            ),
        };

        let shader = match shader {
            Some(s) => s,
            None => return,
        };

        let mut paint = ts::Paint::default();
        paint.anti_alias = true;
        paint.shader = shader;

        // Compute clip clone BEFORE re-borrowing self for target() —
        // mask_owned no longer borrows self once we have an owned
        // Option<Mask>.
        let mask_owned: Option<ts::Mask> = self.clip_stack.last().and_then(|c| c.clone());
        let mut target = if let Some(layer) = self.layer_stack.last_mut() {
            layer.pixmap.as_mut()
        } else {
            self.root.as_mut()
        };
        target.fill_rect(full_rect, &paint, ts::Transform::identity(), mask_owned.as_ref());
    }

    fn push_layer(&mut self, composite_mode: CompositeMode) {
        let (w, h) = (self.root.width(), self.root.height());
        let pixmap = ts::Pixmap::new(w, h).expect("Pixmap::new for layer");
        self.layer_stack.push(LayerFrame {
            pixmap,
            composite_mode,
        });
    }

    fn pop_layer(&mut self) {
        let frame = match self.layer_stack.pop() {
            Some(f) => f,
            None => return,
        };
        let blend_mode = ts_blend_for(frame.composite_mode);
        let pixmap_paint = ts::PixmapPaint {
            opacity: 1.0,
            blend_mode,
            quality: ts::FilterQuality::Bilinear,
        };
        let src = frame.pixmap.as_ref();
        let mask_owned = self.current_clip().cloned();
        let mut target = self.target();
        target.draw_pixmap(
            0,
            0,
            src,
            &pixmap_paint,
            ts::Transform::identity(),
            mask_owned.as_ref(),
        );
    }
}

fn ts_blend_for(mode: CompositeMode) -> ts::BlendMode {
    // COLRv1 composite modes map mostly 1:1 with tiny-skia blend
    // modes. Modes tiny-skia lacks fall back to SrcOver — they're
    // rare in real-world emoji fonts; Slice 3C can revisit if any
    // common emoji is observed to need them.
    match mode {
        CompositeMode::Clear => ts::BlendMode::Clear,
        CompositeMode::Src => ts::BlendMode::Source,
        CompositeMode::Dest => ts::BlendMode::Destination,
        CompositeMode::SrcOver => ts::BlendMode::SourceOver,
        CompositeMode::DestOver => ts::BlendMode::DestinationOver,
        CompositeMode::SrcIn => ts::BlendMode::SourceIn,
        CompositeMode::DestIn => ts::BlendMode::DestinationIn,
        CompositeMode::SrcOut => ts::BlendMode::SourceOut,
        CompositeMode::DestOut => ts::BlendMode::DestinationOut,
        CompositeMode::SrcAtop => ts::BlendMode::SourceAtop,
        CompositeMode::DestAtop => ts::BlendMode::DestinationAtop,
        CompositeMode::Xor => ts::BlendMode::Xor,
        CompositeMode::Plus => ts::BlendMode::Plus,
        CompositeMode::Screen => ts::BlendMode::Screen,
        CompositeMode::Overlay => ts::BlendMode::Overlay,
        CompositeMode::Darken => ts::BlendMode::Darken,
        CompositeMode::Lighten => ts::BlendMode::Lighten,
        CompositeMode::ColorDodge => ts::BlendMode::ColorDodge,
        CompositeMode::ColorBurn => ts::BlendMode::ColorBurn,
        CompositeMode::HardLight => ts::BlendMode::HardLight,
        CompositeMode::SoftLight => ts::BlendMode::SoftLight,
        CompositeMode::Difference => ts::BlendMode::Difference,
        CompositeMode::Exclusion => ts::BlendMode::Exclusion,
        CompositeMode::Multiply => ts::BlendMode::Multiply,
        CompositeMode::HslHue => ts::BlendMode::Hue,
        CompositeMode::HslSaturation => ts::BlendMode::Saturation,
        CompositeMode::HslColor => ts::BlendMode::Color,
        CompositeMode::HslLuminosity => ts::BlendMode::Luminosity,
        // skrifa marks CompositeMode #[non_exhaustive]; any future
        // variants fall back to SrcOver (the COLRv1 spec's default).
        _ => ts::BlendMode::SourceOver,
    }
}

fn color_from_cpal(font: &FontRef<'_>, palette_index: u16, alpha: f32) -> Option<ts::Color> {
    // Palette 0 is the default per the OpenType CPAL spec. Bind
    // the ColorPalettes temporary so palette.colors()'s borrow
    // (which points into ColorPalette<'a>'s sub_array) outlives
    // the slice access.
    let palettes = font.color_palettes();
    let palette = palettes.get(0)?;
    let entry = palette.colors().get(palette_index as usize).copied()?;
    let r = entry.red;
    let g = entry.green;
    let b = entry.blue;
    let a = ((entry.alpha as f32 / 255.0) * alpha.clamp(0.0, 1.0) * 255.0).round() as u8;
    Some(ts::Color::from_rgba8(r, g, b, a))
}

fn make_gradient_stops(font: &FontRef<'_>, stops: &[ColorStop]) -> Vec<ts::GradientStop> {
    stops
        .iter()
        .filter_map(|s| {
            let color = color_from_cpal(font, s.palette_index, s.alpha)?;
            Some(ts::GradientStop::new(s.offset, color))
        })
        .collect()
}

fn ts_spread_mode_for(extend: Extend) -> ts::SpreadMode {
    match extend {
        Extend::Pad => ts::SpreadMode::Pad,
        Extend::Repeat => ts::SpreadMode::Repeat,
        Extend::Reflect => ts::SpreadMode::Reflect,
        // Extend::Unknown (future variants) — default to Pad per
        // COLRv1 spec recommendation for unrecognized modes.
        _ => ts::SpreadMode::Pad,
    }
}

fn make_linear_gradient(
    font: &FontRef<'_>,
    p0: (f32, f32),
    p1: (f32, f32),
    stops: &[ColorStop],
    extend: Extend,
    transform: ts::Transform,
) -> Option<ts::Shader<'static>> {
    let ts_stops = make_gradient_stops(font, stops);
    if ts_stops.is_empty() {
        return None;
    }
    ts::LinearGradient::new(
        ts::Point::from_xy(p0.0, p0.1),
        ts::Point::from_xy(p1.0, p1.1),
        ts_stops,
        ts_spread_mode_for(extend),
        transform,
    )
}

fn make_radial_gradient(
    font: &FontRef<'_>,
    c0: (f32, f32),
    r0: f32,
    c1: (f32, f32),
    r1: f32,
    stops: &[ColorStop],
    extend: Extend,
    transform: ts::Transform,
) -> Option<ts::Shader<'static>> {
    let ts_stops = make_gradient_stops(font, stops);
    if ts_stops.is_empty() {
        return None;
    }
    // tiny-skia's RadialGradient::new signature is
    // (start_point, start_radius, end_point, end_radius, stops,
    // mode, transform). r0 can be negative per skrifa docs;
    // tiny-skia rejects negative radii (returns None), so clamp
    // at 0 — the gradient will degenerate gracefully via tiny-
    // skia's internal handling.
    ts::RadialGradient::new(
        ts::Point::from_xy(c0.0, c0.1),
        r0.max(0.0),
        ts::Point::from_xy(c1.0, c1.1),
        r1.max(0.0),
        ts_stops,
        ts_spread_mode_for(extend),
        transform,
    )
}

fn make_sweep_gradient(
    font: &FontRef<'_>,
    c0: (f32, f32),
    start_angle: f32,
    end_angle: f32,
    stops: &[ColorStop],
    extend: Extend,
    transform: ts::Transform,
) -> Option<ts::Shader<'static>> {
    let ts_stops = make_gradient_stops(font, stops);
    if ts_stops.is_empty() {
        return None;
    }
    // Skrifa sweep angles are in degrees, clockwise (per ColorPainter
    // trait docs). tiny-skia SweepGradient::new takes (center,
    // start_angle, end_angle, stops, mode, transform) with degrees,
    // clockwise from the +x axis — matching convention exactly.
    ts::SweepGradient::new(
        ts::Point::from_xy(c0.0, c0.1),
        start_angle,
        end_angle,
        ts_stops,
        ts_spread_mode_for(extend),
        transform,
    )
}

// ---------- OutlinePen → tiny-skia PathBuilder shim ----------

struct PathBuilderPen {
    pb: ts::PathBuilder,
}

impl PathBuilderPen {
    fn new() -> Self {
        Self {
            pb: ts::PathBuilder::new(),
        }
    }

    fn into_path(self) -> Option<ts::Path> {
        self.pb.finish()
    }
}

impl OutlinePen for PathBuilderPen {
    fn move_to(&mut self, x: f32, y: f32) {
        self.pb.move_to(x, y);
    }
    fn line_to(&mut self, x: f32, y: f32) {
        self.pb.line_to(x, y);
    }
    fn quad_to(&mut self, cx0: f32, cy0: f32, x: f32, y: f32) {
        self.pb.quad_to(cx0, cy0, x, y);
    }
    fn curve_to(&mut self, cx0: f32, cy0: f32, cx1: f32, cy1: f32, x: f32, y: f32) {
        self.pb.cubic_to(cx0, cy0, cx1, cy1, x, y);
    }
    fn close(&mut self) {
        self.pb.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// Path to the COLRv1 emoji font. May not exist in a fresh
    /// checkout that hasn't run setup.sh; tests skip in that case
    /// rather than fail, mirroring how the existing CBDT-side bake
    /// gates on the same font being installed.
    fn colrv1_font_path() -> PathBuf {
        let manifest = env!("CARGO_MANIFEST_DIR");
        PathBuf::from(manifest)
            .join("..")
            .join("ui")
            .join("fonts")
            .join("noto-color-emoji-colrv1.ttf")
    }

    #[test]
    fn rasterizes_grinning_face_via_colrv1() {
        let path = colrv1_font_path();
        if !path.exists() {
            eprintln!(
                "skip: {:?} absent (setup.sh hasn't fetched the COLRv1 font)",
                path
            );
            return;
        }
        let out = rasterize_colr_cell(&path, 0x1F600)
            .expect("rasterize should not error on a known-good codepoint")
            .expect("U+1F600 grinning face should rasterize via COLRv1");

        assert_eq!(out.cell_px, COLR_CELL_PX);
        assert_eq!(
            out.rgba_bytes.len() as u32,
            COLR_CELL_PX * COLR_CELL_PX * 4,
        );
        assert!(
            out.advance_em > 0.0,
            "advance_em should be positive; got {}",
            out.advance_em,
        );

        // Sanity: paint tree rendered something. Either some pixel
        // has non-zero alpha (color was painted) OR the cell is
        // entirely transparent which would be a degenerate paint
        // tree and a real bug.
        let any_nonzero_alpha = out.rgba_bytes.chunks_exact(4).any(|p| p[3] != 0);
        assert!(
            any_nonzero_alpha,
            "rasterized cell should have non-zero alpha somewhere",
        );

        // Plane bounds describe a non-degenerate box in em-units.
        assert!(
            out.plane_bounds.pl_left < out.plane_bounds.pl_right,
            "plane_bounds horizontal range degenerate: {:?}",
            out.plane_bounds,
        );
        assert!(
            out.plane_bounds.pl_bottom < out.plane_bounds.pl_top,
            "plane_bounds vertical range degenerate: {:?}",
            out.plane_bounds,
        );
    }

    #[test]
    fn rasterizes_red_heart_via_colrv1() {
        // Second emoji to exercise a different paint tree shape.
        // U+2764 ❤ heart — typically a single solid-fill paint over
        // a glyph clip; lighter on gradients than the grinning face
        // but still validates the dispatch + ColorPainter wiring.
        let path = colrv1_font_path();
        if !path.exists() {
            return;
        }
        let out = rasterize_colr_cell(&path, 0x2764)
            .expect("rasterize should not error")
            .expect("U+2764 heart should rasterize via COLRv1");
        assert_eq!(out.cell_px, COLR_CELL_PX);
        let any_nonzero_alpha = out.rgba_bytes.chunks_exact(4).any(|p| p[3] != 0);
        assert!(any_nonzero_alpha, "heart should paint somewhere");
    }

    #[test]
    fn rasterizes_earth_globe_via_colrv1() {
        // Third emoji to exercise radial+linear gradient paint
        // trees. U+1F30D 🌍 globe is heavily gradient-shaded in
        // Noto Color Emoji; if the gradient mapping is wrong the
        // alpha channel will be all-or-nothing rather than a
        // shaded mid-tone.
        let path = colrv1_font_path();
        if !path.exists() {
            return;
        }
        let out = rasterize_colr_cell(&path, 0x1F30D)
            .expect("rasterize should not error")
            .expect("U+1F30D globe should rasterize via COLRv1");
        assert_eq!(out.cell_px, COLR_CELL_PX);
        let any_nonzero_alpha = out.rgba_bytes.chunks_exact(4).any(|p| p[3] != 0);
        assert!(any_nonzero_alpha, "globe should paint somewhere");
        // Sanity: a gradient-shaded glyph should yield INTERMEDIATE
        // alpha values, not just 0 and 255 (which any solid-fill
        // glyph would produce). >=3 distinct alphas is the minimum
        // that requires actual gradient stop interpolation; a flat
        // solid fill on a glyph clip is exactly 2 alphas.
        let unique_alphas: std::collections::HashSet<u8> = out
            .rgba_bytes
            .chunks_exact(4)
            .map(|p| p[3])
            .collect();
        assert!(
            unique_alphas.len() >= 3,
            "expected gradient interpolation (>=3 distinct alpha values), got {:?}",
            unique_alphas,
        );
    }

    #[test]
    fn returns_none_for_codepoint_absent_from_emoji_font() {
        let path = colrv1_font_path();
        if !path.exists() {
            return;
        }
        // U+25CF (BLACK CIRCLE, geometric shape) is NOT in
        // NotoColorEmoji-COLRv1; the existing dispatch ladder routes
        // it to DejaVu Sans via MSDF instead. Confirms the absent-
        // codepoint return path.
        let result = rasterize_colr_cell(&path, 0x25CF).unwrap();
        assert!(
            result.is_none(),
            "U+25CF (●) should not be present in NotoColorEmoji-COLRv1",
        );
    }

    #[test]
    fn rasterizes_every_demo_reel_emoji_codepoint() {
        // Slice 3D (2026-05-19): after CBDT retirement, EVERY emoji
        // on the FYS demo reel is served by this COLRv1 path. The
        // only emoji-bearing slide is SCREAM (slide 10), text
        // "🔓 🫵 🪧". Verify all three codepoints rasterize so the
        // cutover can't silently regress one of them to Tofu.
        //   🔓 U+1F513 OPEN LOCK
        //   🫵 U+1FAF5 INDEX POINTING AT THE VIEWER (Unicode 14)
        //   🪧 U+1FAA7 PLACARD (Unicode 14)
        let path = colrv1_font_path();
        if !path.exists() {
            return;
        }
        for &(cp, name) in &[
            (0x1F513_u32, "🔓 open lock"),
            (0x1FAF5_u32, "🫵 index pointing at viewer"),
            (0x1FAA7_u32, "🪧 placard"),
        ] {
            let out = rasterize_colr_cell(&path, cp)
                .unwrap_or_else(|e| panic!("rasterize errored for {name} (U+{cp:04X}): {e}"))
                .unwrap_or_else(|| {
                    panic!(
                        "{name} (U+{cp:04X}) returned None — NotoColorEmoji-COLRv1 \
                         lacks this glyph; the demo reel would show Tofu for it",
                    )
                });
            assert_eq!(out.cell_px, COLR_CELL_PX, "{name}: wrong cell_px");
            assert_eq!(
                out.rgba_bytes.len() as u32,
                COLR_CELL_PX * COLR_CELL_PX * 4,
                "{name}: wrong rgba buffer length",
            );
            let any_nonzero_alpha = out.rgba_bytes.chunks_exact(4).any(|p| p[3] != 0);
            assert!(any_nonzero_alpha, "{name}: rasterized cell is fully transparent");
            assert!(out.advance_em > 0.0, "{name}: non-positive advance_em");
        }
    }
}
