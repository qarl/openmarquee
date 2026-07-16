//! Colorlight compositor — Phase-1 prep, hardware-independent.
//!
//! Defines the `Compositor` trait: produce a card-native RGB888 frame
//! (128×96 by default) that flows into `colorlight::encode_to_sink` →
//! `FrameSink`. This module ships ONE implementation — `CpuCompositor`,
//! a `tiny_skia`-backed deterministic rasterizer of a small set of
//! `TestPattern` fiducials for dev iteration + CI-testable end-to-end
//! coverage of the whole pipeline (compositor → encoder → sink).
//!
//! ## Why a trait instead of one concrete compositor
//!
//! Admin's PR-B0 guardrail (2026-07-15): #B0.5 will land a second
//! implementation — `HeadlessGpuCompositor` — that runs the real
//! GBM+EGL Pattern A on the Pi (design doc §5). Two implementations
//! justifies the abstraction; the `Compositor` trait is defined
//! NOW so the second implementation slots in behind the same seam
//! with no caller change. Mirrors PR #88's `FrameSink` shape:
//! `FrameSink` consumes frames; `Compositor` produces them.
//!
//! ## Scope humility
//!
//! `CpuCompositor` renders **test patterns**, not real content. Its
//! purpose is:
//!   - deterministic input for encoder/sink round-trip tests,
//!   - Mac dev PNG-out mode for visual iteration without hardware,
//!   - a testable stand-in for the GPU compositor on non-Linux
//!     targets (or when the GPU compositor's bring-up fails on
//!     Linux) — see `colorlight_gpu_compositor::HeadlessGpuCompositor`
//!     (PR #B0.5) for the Linux GPU sibling behind the same
//!     `Compositor` trait.
//!
//! For **Phase 1**, PR #B1 wires either compositor into a real
//! `AF_PACKET`-backed streaming pump (`ipc_main.rs::run_colorlight_
//! stream_pump`) so Colorlight hardware first-light validates the
//! whole pipeline (compositor → encoder → PacketSink → wire →
//! receiver card → HUB75 panel).
//!
//! Wiring the existing renderer paint pipeline's real-slide output
//! THROUGH a `Compositor` implementation is a separate architecture
//! conversation deferred to **#B2+**.  Design-doc §5's Pattern A
//! already assumes a headless GBM+EGL compositor context distinct
//! from HDMI's DRM-scanout one; the #B2 conversation is about how
//! playlist content feeds INTO that headless context, not about
//! tapping the HDMI paint path.

use tiny_skia::{Color, Paint, PixmapMut, Rect, Transform};

// ── Public config surface ────────────────────────────────────────────────

/// Card-native default width — matches Colorlight design-doc §2
/// (chain=3 × panel_w=32).
pub const DEFAULT_CARD_WIDTH_PX: u32 = 128;
/// Card-native default height — matches Colorlight design-doc §2
/// (parallel=4 × panel_h=32).
pub const DEFAULT_CARD_HEIGHT_PX: u32 = 96;

/// A test pattern the CPU compositor can render.  Every variant
/// produces a **deterministic** output at a given (width, height) —
/// two runs with the same variant + dims yield byte-identical
/// framebuffers.  Determinism matters for CI-golden byte-exact
/// assertions.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TestPattern {
    /// All-zero (black) fill — the calibration frame the card holds
    /// at startup + between latch-loss.
    SolidBlack,
    /// All-0xFF (white) fill — reveals dead pixels + max-brightness
    /// clamp bugs.
    SolidWhite,
    /// Solid red — pins the R channel byte position on the wire
    /// (BGR vs RGB drift is a top per-panel diagnostic per design
    /// doc §10).
    SolidRed,
    /// Solid green — pins the G channel byte position.
    SolidGreen,
    /// Solid blue — pins the B channel byte position.
    SolidBlue,
    /// 8×8 checkerboard — every-other 8-px cell is white, rest black.
    /// Reveals per-pixel byte-ordering + module-boundary drift.
    Checkerboard8,
    /// Horizontal red→green→blue gradient across the card width.
    /// Reveals per-column drift + gamma/brightness LUT effects
    /// (a naive linear encode looks washed/crushed even if the sign
    /// lights, per design-doc §6 Layer-1 pattern list).
    Gradient,
}

/// Errors a `Compositor` implementation can surface.  Kept small so
/// the trait stays object-safe + new implementations pick the
/// closest variant.
#[derive(Debug)]
pub enum CompositorError {
    /// Requested dimensions are outside the compositor's supported
    /// range (e.g. zero-width, or a `HeadlessGpuCompositor`'s max
    /// GBM-surface bound at Phase-1).
    InvalidDimensions { w: u32, h: u32, reason: &'static str },
    /// A backend-specific setup or paint step failed at runtime.
    /// Carries a String so a real (Linux/EGL/tiny_skia) failure can
    /// attach its own message without an API bump.
    Backend(String),
}

impl std::fmt::Display for CompositorError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidDimensions { w, h, reason } => {
                write!(f, "colorlight compositor: invalid dims {w}x{h} ({reason})")
            }
            Self::Backend(m) => write!(f, "colorlight compositor: backend failure ({m})"),
        }
    }
}

impl std::error::Error for CompositorError {}

// ── Compositor trait ────────────────────────────────────────────────────

/// A frame-producer that hands the Colorlight encoder a card-native
/// RGB888 buffer per invocation.
///
/// Object-safe: no generic methods, no `Self` returns, no `Sized`
/// bound.  Callers can hold `&mut dyn Compositor` / `Box<dyn
/// Compositor>` at every layer, so swapping `CpuCompositor` for the
/// Phase-1 `HeadlessGpuCompositor` is a one-line change at the
/// pipeline seam.
///
/// The returned `Vec<u8>` MUST be exactly `card_width_px() *
/// card_height_px() * 3` bytes, row-major, R,G,B — the exact shape
/// `colorlight_logic::serialize_frame` expects.
pub trait Compositor {
    /// Compose one frame + return it as a fresh owned `Vec<u8>`.
    /// The compositor MAY internally buffer / cache; the returned
    /// vec is the caller's to consume (typically by handing it
    /// straight to `colorlight::encode_to_sink`).
    fn produce_frame(&mut self) -> Result<Vec<u8>, CompositorError>;

    /// Card-native width in pixels. Fixed at construction; callers
    /// can pre-size buffers.
    fn card_width_px(&self) -> u32;

    /// Card-native height in pixels.
    fn card_height_px(&self) -> u32;
}

// ── CPU implementation (tiny_skia-backed) ────────────────────────────────

/// Cross-platform `Compositor` — rasterizes a `TestPattern` via
/// `tiny_skia` (already a crate dep for the COLRv1 emoji rasterizer,
/// so no new dependency).
///
/// Deterministic: same pattern + same dims → same bytes.  Zero GPU
/// dependency, zero DRM/EGL setup — runs on macOS in `cargo test`
/// unchanged from how it runs on the Pi.
pub struct CpuCompositor {
    width: u32,
    height: u32,
    pattern: TestPattern,
}

impl CpuCompositor {
    /// Constructor.  Rejects zero-dim inputs immediately.
    pub fn new(
        width: u32,
        height: u32,
        pattern: TestPattern,
    ) -> Result<Self, CompositorError> {
        if width == 0 || height == 0 {
            return Err(CompositorError::InvalidDimensions {
                w: width,
                h: height,
                reason: "zero dimension",
            });
        }
        // `Pixmap` requires reasonable dims; cap at a defensible
        // maximum so a bad env can't ask for a 65536² allocation.
        // The card-native max under the Colorlight spec is a few
        // hundred px on each side; 4096 is a very generous ceiling.
        const MAX_DIM: u32 = 4096;
        if width > MAX_DIM || height > MAX_DIM {
            return Err(CompositorError::InvalidDimensions {
                w: width,
                h: height,
                reason: "exceeds MAX_DIM (4096)",
            });
        }
        Ok(Self {
            width,
            height,
            pattern,
        })
    }

    /// The 128×96 card-native default with an operator-chosen pattern.
    pub fn card_default(pattern: TestPattern) -> Self {
        // The dim-bounds are pinned to constants; unwrap is safe.
        Self::new(DEFAULT_CARD_WIDTH_PX, DEFAULT_CARD_HEIGHT_PX, pattern).expect(
            "DEFAULT_CARD_{WIDTH,HEIGHT}_PX are within bounds by construction",
        )
    }

    /// Read-only pattern peek (used by the arm-fill diagnostic).
    pub fn pattern(&self) -> TestPattern {
        self.pattern
    }

    /// Render + return raw RGBA bytes (4 per pixel) — the direct
    /// output of `tiny_skia`, exposed so a caller that wants alpha
    /// (e.g. blending) can skip the RGB888 conversion.  The
    /// `Compositor::produce_frame` path calls this then drops alpha
    /// to hand the encoder RGB888.
    pub fn produce_rgba(&self) -> Vec<u8> {
        let (w, h) = (self.width, self.height);
        let mut buf = vec![0u8; (w * h * 4) as usize];
        // SAFETY: buf.len() == w*h*4 by construction; PixmapMut
        // borrows for the drawing lifetime and does not outlive `buf`.
        let mut pixmap = PixmapMut::from_bytes(&mut buf, w, h)
            .expect("PixmapMut construction with correctly-sized buffer");
        self.paint(&mut pixmap);
        buf
    }

    fn paint(&self, pixmap: &mut PixmapMut<'_>) {
        let (w, h) = (self.width as f32, self.height as f32);
        match self.pattern {
            TestPattern::SolidBlack => pixmap.fill(Color::from_rgba8(0, 0, 0, 255)),
            TestPattern::SolidWhite => {
                pixmap.fill(Color::from_rgba8(255, 255, 255, 255))
            }
            TestPattern::SolidRed => pixmap.fill(Color::from_rgba8(255, 0, 0, 255)),
            TestPattern::SolidGreen => pixmap.fill(Color::from_rgba8(0, 255, 0, 255)),
            TestPattern::SolidBlue => pixmap.fill(Color::from_rgba8(0, 0, 255, 255)),
            TestPattern::Checkerboard8 => {
                pixmap.fill(Color::from_rgba8(0, 0, 0, 255));
                let mut white = Paint::default();
                white.set_color_rgba8(255, 255, 255, 255);
                let cell = 8u32;
                for cy in 0..self.height / cell {
                    for cx in 0..self.width / cell {
                        if (cx + cy) % 2 == 0 {
                            let x = (cx * cell) as f32;
                            let y = (cy * cell) as f32;
                            let rect =
                                Rect::from_xywh(x, y, cell as f32, cell as f32)
                                    .expect("cell dims positive");
                            pixmap.fill_rect(rect, &white, Transform::identity(), None);
                        }
                    }
                }
            }
            TestPattern::Gradient => {
                // Per-column stripe: R across the first third of the
                // width, G across the middle third, B across the last
                // third.  Not a smooth blend on purpose — solid fills
                // are the closest thing to "byte-exact" tiny_skia
                // gives us.  NOTE: for non-integer widths (e.g. 128/
                // 3 ≈ 42.67), `fill_rect`'s coverage on the two
                // stripe-boundary columns is anti-aliased and CAN
                // drift across `tiny_skia` versions.  The tests
                // sample only pixels FAR from boundaries; a full-
                // frame byte-golden of this pattern would need
                // per-version blessing (call out here so a future
                // dev doesn't add one and get bit).
                pixmap.fill(Color::from_rgba8(0, 0, 0, 255));
                let mut r = Paint::default();
                r.set_color_rgba8(255, 0, 0, 255);
                let mut g = Paint::default();
                g.set_color_rgba8(0, 255, 0, 255);
                let mut b = Paint::default();
                b.set_color_rgba8(0, 0, 255, 255);
                let third = w / 3.0;
                for (i, paint) in [&r, &g, &b].iter().enumerate() {
                    let x = i as f32 * third;
                    let rect = Rect::from_xywh(x, 0.0, third, h)
                        .expect("gradient stripe dims positive");
                    pixmap.fill_rect(rect, paint, Transform::identity(), None);
                }
            }
        }
    }
}

impl Compositor for CpuCompositor {
    fn produce_frame(&mut self) -> Result<Vec<u8>, CompositorError> {
        let rgba = self.produce_rgba();
        // RGBA → RGB888 by dropping alpha.
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

// ── PNG dump helper (Mac dev iteration + first-light diagnostic) ────────

/// Write an RGB888 buffer to a PNG file — the Mac dev PNG-out mode.
///
/// The `png` crate encoder is given the buffer as-is (RGB colour
/// type + 8-bit depth) so bytes round-trip unchanged.  Overwrites
/// any existing file at `path`.  Rejects mismatched buffer sizes
/// AND overflow-prone dims (`width * height * 3` guarded via
/// `checked_mul`) so an adversarial caller can't slip a corrupt
/// short buffer past the length check.
pub fn write_rgb888_png(
    rgb: &[u8],
    width: u32,
    height: u32,
    path: &std::path::Path,
) -> Result<(), std::io::Error> {
    let expected = (width as usize)
        .checked_mul(height as usize)
        .and_then(|wh| wh.checked_mul(3))
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                format!(
                    "colorlight png dump: {width}x{height} would overflow usize"
                ),
            )
        })?;
    if rgb.len() != expected {
        return Err(std::io::Error::new(
            std::io::ErrorKind::InvalidInput,
            format!(
                "colorlight png dump: expected {} bytes for {}x{} RGB, got {}",
                expected,
                width,
                height,
                rgb.len()
            ),
        ));
    }
    let file = std::fs::File::create(path)?;
    let w = std::io::BufWriter::new(file);
    let mut encoder = png::Encoder::new(w, width, height);
    encoder.set_color(png::ColorType::Rgb);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder
        .write_header()
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
    writer
        .write_image_data(rgb)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::Other, e.to_string()))?;
    Ok(())
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use crate::colorlight::{encode_to_sink, VecSink};
    use crate::colorlight_logic::ColorlightConfig;

    // ── CpuCompositor: config validation ─────────────────────────────

    #[test]
    fn cpu_compositor_zero_dim_is_invalid() {
        assert!(matches!(
            CpuCompositor::new(0, 96, TestPattern::SolidBlack),
            Err(CompositorError::InvalidDimensions { .. })
        ));
        assert!(matches!(
            CpuCompositor::new(128, 0, TestPattern::SolidBlack),
            Err(CompositorError::InvalidDimensions { .. })
        ));
    }

    #[test]
    fn cpu_compositor_oversize_dim_is_invalid() {
        assert!(matches!(
            CpuCompositor::new(5000, 96, TestPattern::SolidBlack),
            Err(CompositorError::InvalidDimensions { .. })
        ));
    }

    #[test]
    fn cpu_compositor_card_default_is_128_by_96() {
        let c = CpuCompositor::card_default(TestPattern::SolidBlack);
        assert_eq!(c.card_width_px(), 128);
        assert_eq!(c.card_height_px(), 96);
    }

    // ── produce_frame: byte counts + endpoints ───────────────────────

    #[test]
    fn produce_frame_returns_card_native_rgb888_byte_count() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidBlack);
        let frame = c.produce_frame().unwrap();
        assert_eq!(frame.len(), (128 * 96 * 3) as usize);
    }

    #[test]
    fn solid_black_pattern_is_all_zeros() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidBlack);
        let frame = c.produce_frame().unwrap();
        assert!(frame.iter().all(|&b| b == 0));
    }

    #[test]
    fn solid_white_pattern_is_all_0xff() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidWhite);
        let frame = c.produce_frame().unwrap();
        assert!(frame.iter().all(|&b| b == 0xFF));
    }

    #[test]
    fn solid_red_pattern_first_triple_is_255_0_0() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidRed);
        let frame = c.produce_frame().unwrap();
        assert_eq!(&frame[0..3], &[255, 0, 0], "first triple should be R");
        // And every triple across the frame.
        for triple in frame.chunks_exact(3) {
            assert_eq!(triple, &[255, 0, 0]);
        }
    }

    #[test]
    fn solid_green_pattern_first_triple_is_0_255_0() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidGreen);
        let frame = c.produce_frame().unwrap();
        assert_eq!(&frame[0..3], &[0, 255, 0]);
    }

    #[test]
    fn solid_blue_pattern_first_triple_is_0_0_255() {
        let mut c = CpuCompositor::card_default(TestPattern::SolidBlue);
        let frame = c.produce_frame().unwrap();
        assert_eq!(&frame[0..3], &[0, 0, 255]);
    }

    // ── Determinism ──────────────────────────────────────────────────

    #[test]
    fn same_pattern_produces_byte_identical_frames() {
        // Determinism is the load-bearing property: CI-golden byte-
        // exact assertions rely on two runs producing the same bytes.
        // Test every pattern.
        for pattern in [
            TestPattern::SolidBlack,
            TestPattern::SolidWhite,
            TestPattern::SolidRed,
            TestPattern::SolidGreen,
            TestPattern::SolidBlue,
            TestPattern::Checkerboard8,
            TestPattern::Gradient,
        ] {
            let mut c1 = CpuCompositor::card_default(pattern);
            let mut c2 = CpuCompositor::card_default(pattern);
            assert_eq!(
                c1.produce_frame().unwrap(),
                c2.produce_frame().unwrap(),
                "{pattern:?} produced non-deterministic frames"
            );
        }
    }

    // ── Checkerboard + Gradient: shape checks ────────────────────────

    #[test]
    fn checkerboard_top_left_cell_is_white() {
        // Cell (0,0) at cx+cy=0 → even → white per the pattern
        // definition.  Pin the pattern orientation so a future tweak
        // to the fill loop can't silently invert.
        let mut c = CpuCompositor::card_default(TestPattern::Checkerboard8);
        let frame = c.produce_frame().unwrap();
        // Pixel (0,0) is the first triple.
        assert_eq!(&frame[0..3], &[255, 255, 255]);
        // Pixel (8,0) is at byte offset 8*3=24 — first pixel of the
        // NEXT cell horizontally, which should be black.
        assert_eq!(&frame[24..27], &[0, 0, 0]);
    }

    #[test]
    fn gradient_first_third_is_red_last_third_is_blue() {
        let mut c = CpuCompositor::card_default(TestPattern::Gradient);
        let frame = c.produce_frame().unwrap();
        // First pixel (x=0, y=0): R stripe.
        assert_eq!(&frame[0..3], &[255, 0, 0]);
        // Last pixel of the LAST row (x=127, y=95): B stripe.  Byte
        // index = (95*128 + 127) * 3.
        let last = ((95 * 128 + 127) * 3) as usize;
        assert_eq!(&frame[last..last + 3], &[0, 0, 255]);
        // Middle-ish pixel (x=64, y=48): G stripe (x=64 is in the
        // middle third since 128/3 ≈ 42.67, 128*2/3 ≈ 85.33).
        let mid = ((48 * 128 + 64) * 3) as usize;
        assert_eq!(&frame[mid..mid + 3], &[0, 255, 0]);
    }

    // ── End-to-end integration: compositor → encoder → sink ──────────
    //
    // This is the round-trip that PR #B0 exists to enable.  Feeds
    // the composited RGB888 into the shipped Colorlight encoder +
    // VecSink; asserts the sink received the shape a real
    // PacketSink would receive on Day 1 with hardware.

    #[test]
    fn cpu_compositor_to_encoder_to_vec_sink_solid_black() {
        // 1 brightness + card_rows(=128) row packets + 1 latch = 130.
        let mut c = CpuCompositor::card_default(TestPattern::SolidBlack);
        let fb = c.produce_frame().unwrap();
        let cfg = ColorlightConfig::thinksign_default();
        let mut sink = VecSink::new();
        let count = encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        assert_eq!(count, 130);
        assert_eq!(sink.frame_count(), 130);
    }

    #[test]
    fn cpu_compositor_to_encoder_to_vec_sink_solid_red_lands_in_payload() {
        // Pure-R canvas + default RGB order → row0's first payload
        // triple (wire off 21..24) is 255,0,0.  Proves the compositor
        // → encoder → sink pipe preserves colour end-to-end.
        let mut c = CpuCompositor::card_default(TestPattern::SolidRed);
        let fb = c.produce_frame().unwrap();
        let cfg = ColorlightConfig::thinksign_default();
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        // Frame 0 is brightness; frame 1 is row 0.
        let row0 = &sink.frames()[1];
        assert_eq!(&row0[21..24], &[255, 0, 0], "row0 first triple should be R");
    }

    #[test]
    fn cpu_compositor_gradient_first_row_lands_R_in_sink_payload() {
        // Row 0 of the compositor output = the top pixel row of the
        // gradient = every column in row 0 is either R/G/B depending
        // on x.  The encoder transpose (canvas 128w×96h → card 96w
        // ×128t) means card-row 0 corresponds to canvas-col 0 across
        // all 96 canvas-rows.  Canvas col 0 → R stripe → the entire
        // row0 payload should be R triples.
        let mut c = CpuCompositor::card_default(TestPattern::Gradient);
        let fb = c.produce_frame().unwrap();
        let cfg = ColorlightConfig::thinksign_default();
        let mut sink = VecSink::new();
        encode_to_sink(&fb, &cfg, &mut sink).unwrap();
        let row0 = &sink.frames()[1];
        // Payload is 96 pixels × 3 bytes = 288 bytes starting at
        // offset 21.  Every triple in payload should be (255,0,0).
        let payload = &row0[21..21 + 96 * 3];
        for (i, triple) in payload.chunks_exact(3).enumerate() {
            assert_eq!(triple, &[255, 0, 0], "row0 payload pixel {i} not R");
        }
    }

    // ── CompositorError Display ──────────────────────────────────────

    #[test]
    fn compositor_error_display_covers_both_variants() {
        let d = CompositorError::InvalidDimensions {
            w: 0,
            h: 96,
            reason: "test",
        };
        let s = format!("{d}");
        assert!(s.contains("invalid dims"), "InvalidDimensions: {s}");
        assert!(s.contains("test"), "InvalidDimensions inner: {s}");

        let b = CompositorError::Backend("gpu unavailable".to_string());
        let s = format!("{b}");
        assert!(s.contains("backend failure"), "Backend: {s}");
        assert!(s.contains("gpu unavailable"), "Backend inner: {s}");
    }

    // ── PNG dump ─────────────────────────────────────────────────────

    #[test]
    fn write_rgb888_png_rejects_wrong_size_buffer() {
        // Use `tempfile::NamedTempFile` so parallel test shells + a
        // mid-test panic can't collide on / leak a fixed path.
        let tmp = tempfile::NamedTempFile::new().expect("mk tempfile");
        let err = write_rgb888_png(&[0u8; 10], 128, 96, tmp.path()).unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
        assert!(err.to_string().contains("expected"));
    }

    #[test]
    fn write_rgb888_png_rejects_overflow_dims() {
        // Adversarial dims: width*height*3 overflows usize on 32-bit,
        // and even on 64-bit width=u32::MAX × height=u32::MAX × 3
        // overflows.  Must be caught via `checked_mul`, not a wrapped
        // small `expected` that lets a corrupt short buffer through.
        let tmp = tempfile::NamedTempFile::new().expect("mk tempfile");
        let err = write_rgb888_png(&[0u8; 10], u32::MAX, u32::MAX, tmp.path())
            .unwrap_err();
        assert_eq!(err.kind(), std::io::ErrorKind::InvalidInput);
        assert!(err.to_string().contains("overflow"));
    }

    #[test]
    fn write_rgb888_png_round_trips_bytes_via_decode() {
        // Write a compositor frame → PNG → decode PNG → assert the
        // decoded RGB matches the input.  Proves the PNG dump path
        // is byte-preserving (RGB in = RGB out, no colour-space or
        // padding shenanigans).
        let mut c = CpuCompositor::card_default(TestPattern::Checkerboard8);
        let fb = c.produce_frame().unwrap();
        let tmp = tempfile::NamedTempFile::new().expect("mk tempfile");
        write_rgb888_png(&fb, 128, 96, tmp.path()).unwrap();

        let file = std::fs::File::open(tmp.path()).unwrap();
        let decoder = png::Decoder::new(std::io::BufReader::new(file));
        let mut reader = decoder.read_info().unwrap();
        let mut decoded = vec![0u8; reader.output_buffer_size()];
        let info = reader.next_frame(&mut decoded).unwrap();
        decoded.truncate(info.buffer_size());

        assert_eq!(info.color_type, png::ColorType::Rgb);
        assert_eq!(info.width, 128);
        assert_eq!(info.height, 96);
        assert_eq!(decoded, fb, "PNG round-trip must preserve bytes exactly");
    }

    // ── Compositor is object-safe (compile-time assertion) ───────────

    #[test]
    fn compositor_trait_is_object_safe() {
        // If `Compositor` ever grows a non-object-safe method (a
        // generic method, a `Self` return, a `where Self: Sized`
        // bound), THIS line stops compiling — a real regression
        // guard, not just a hopeful docstring.
        let mut c = CpuCompositor::card_default(TestPattern::SolidBlack);
        let dyn_ref: &mut dyn Compositor = &mut c;
        let frame = dyn_ref.produce_frame().unwrap();
        assert_eq!(frame.len(), (128 * 96 * 3) as usize);
    }
}
