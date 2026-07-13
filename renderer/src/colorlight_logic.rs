//! Colorlight 5A-75B packet serializer — PURE, host-testable (no socket, no HW).
//!
//! Turns an RGB888 framebuffer + a [`ColorlightConfig`] into the ordered list of
//! **complete Layer-2 Ethernet frames** the card expects, so the (Linux-only)
//! transport is a thin `AF_PACKET` blast and ALL correctness lives here where the
//! Mac `cargo test` gate can reach it. This mirrors the `hdmi_logic.rs` / `hdmi.rs`
//! split.
//!
//! Protocol byte-exact from FPP `src/channeloutput/ColorLight-5a-75.cpp/.h` +
//! hkubota's RE (see `docs/colorlight-backend-design-2026-07-12.md` §2). Per frame:
//! **brightness `0x0A` → pixel rows `0x55` (ascending, no bank interleave) → latch
//! `0x01`**. The EtherType field is `opcode<<8 | data[0]`.
//!
//! ## What is and isn't pinned here
//! Framing (opcodes, magic bytes `0x08 0x88`, header layout, packet order,
//! EtherType, dest MAC) is wiring-INDEPENDENT and asserted directly by the tests.
//! The pixel→(row,position) **remap is wiring-DEPENDENT** and cannot be trusted
//! until qarl's physical cabling is blessed at Phase A (design doc §7). Until then
//! the remap uses a documented default and is only proven for round-trip
//! self-consistency by the Layer-2 loopback test — which, per the design doc,
//! does NOT prove the wire format is real-Colorlight-correct (only golden-pcap
//! conformance does).

// Wire constants — FPP ColorLight-5a-75.{cpp,h}.
pub const DEFAULT_DEST_MAC: [u8; 6] = [0x11, 0x22, 0x33, 0x44, 0x55, 0x66];
pub const DEFAULT_SRC_MAC: [u8; 6] = [0x22, 0x22, 0x33, 0x44, 0x55, 0x66];

const OPCODE_BRIGHTNESS: u8 = 0x0A;
const OPCODE_ROW: u8 = 0x55;
const OPCODE_LATCH: u8 = 0x01;

const ROW_MAGIC_A: u8 = 0x08; // wire offset 19 (FPP.cpp:606) — constant, meaning unknown
const ROW_MAGIC_B: u8 = 0x88; // wire offset 20 (FPP.cpp:607) — constant, meaning unknown

/// Max pixels a single `0x55` packet can carry inside a 1500-byte MTU
/// (FPP `CL_MAX_PIXL_PER_PACKET`): 497·3 + 21-byte header = 1512-byte frame,
/// 1498-byte L2 payload ≤ 1500. Rows wider than this split into multiple
/// packets advancing the pixel-offset field.
pub const MAX_PIXELS_PER_PACKET: usize = 497;

const ETH_HEADER_LEN: usize = 14; // 6 dst + 6 src + 2 EtherType

/// Physical channel order the payload bytes are emitted in. LED panels commonly
/// swap; this is a per-panel config, verified against pure-R/G/B goldens at Phase A.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ColorOrder {
    Rgb,
    Bgr,
    Grb,
    Gbr,
    Rbg,
    Brg,
}

impl ColorOrder {
    /// Reorder an (r,g,b) triple into the wire byte order.
    #[inline]
    fn apply(self, r: u8, g: u8, b: u8) -> [u8; 3] {
        match self {
            ColorOrder::Rgb => [r, g, b],
            ColorOrder::Bgr => [b, g, r],
            ColorOrder::Grb => [g, r, b],
            ColorOrder::Gbr => [g, b, r],
            ColorOrder::Rbg => [r, b, g],
            ColorOrder::Brg => [b, r, g],
        }
    }
}

/// Errors from serialization — never panic / UB on bad input (QA §8.3).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SerializeError {
    /// Framebuffer length ≠ width·height·3.
    DimensionMismatch {
        expected_bytes: usize,
        got_bytes: usize,
    },
    /// Canvas dims don't satisfy the (Phase-0 default transpose) remap:
    /// `width == card_rows` (outputs·panel_h) and `height == row_width_px`
    /// (chain·panel_w). Surfaced, not silently mis-mapped (QA §8.3). This is the
    /// "transposed" geometry the research flagged (128w×96h canvas ↔ 96w×128t card).
    GeometryMismatch {
        canvas: (u16, u16),
        card: (usize, usize),
    },
    /// A geometry parameter is zero / would divide by zero.
    InvalidGeometry(&'static str),
    /// This config's `wiring_revision` ≠ the blessed fixture's — the mapping
    /// goldens cannot be trusted against a drifted wiring (design doc §7 C.1).
    WiringRevisionMismatch { config: String, expected: String },
}

/// All driver geometry + color config. NOTHING hard-coded (design doc §4). The
/// `wiring_revision` + the remap params are pinned to qarl's blessed FPP config
/// at Phase A and are IMMUTABLE thereafter without a re-bless (design doc §7 C.1).
#[derive(Clone, Debug)]
pub struct ColorlightConfig {
    pub width: u16,   // canvas width  (128)
    pub height: u16,  // canvas height (96)
    pub panel_w: u16, // module width  (32)
    pub panel_h: u16, // module height (32)
    pub outputs: u16, // parallel HUB75 chains used (4)
    pub chain: u16,   // modules per chain (3)
    pub color_order: ColorOrder,
    /// Optional per-channel gamma/brightness LUT (design doc §10.4). `None` = linear
    /// identity. The real LUT is pinned from FPP's gradient golden at Phase A.
    pub gamma_lut: Option<[u8; 256]>,
    /// Frame brightness 0–255 (the `0x0A` packet, per-channel R/G/B set equal).
    pub brightness: u8,
    pub dest_mac: [u8; 6],
    pub src_mac: [u8; 6],
    /// FPP reverses chain order by default (`chain = longestChain-1 - panel.chain`).
    pub chain_reversed: bool,
    /// Documented wiring-revision tag; a change here == a physical rewire that
    /// invalidates the mapping goldens (design doc §7 C.2).
    pub wiring_revision: String,
}

impl ColorlightConfig {
    /// The 128×96 / 32×32 / parallel=4 / chain=3 target, RGB, linear, default MACs.
    /// Geometry params match qarl's sign; the remap orientation is a DEFAULT to be
    /// pinned at Phase-A blessing.
    pub fn thinksign_default() -> Self {
        Self {
            width: 128,
            height: 96,
            panel_w: 32,
            panel_h: 32,
            outputs: 4,
            chain: 3,
            color_order: ColorOrder::Rgb,
            gamma_lut: None,
            brightness: 255,
            dest_mac: DEFAULT_DEST_MAC,
            src_mac: DEFAULT_SRC_MAC,
            chain_reversed: false,
            wiring_revision: "unblessed-phase0-default".to_string(),
        }
    }

    /// Number of `0x55` row packets = outputs · panel_h (FPP `m_rows`).
    #[inline]
    pub fn card_rows(&self) -> usize {
        self.outputs as usize * self.panel_h as usize
    }

    /// Pixels per row = chain · panel_w (FPP `m_rowSize/3`).
    #[inline]
    pub fn row_width_px(&self) -> usize {
        self.chain as usize * self.panel_w as usize
    }

    /// Check the geometry is internally consistent + satisfies the transpose remap.
    /// Called by `serialize_frame` (so a bad config never produces garbage packets)
    /// and by the Phase-2 `config_from_env` (so a bad env config fails at startup,
    /// not on the first frame).
    pub fn validate(&self) -> Result<(), SerializeError> {
        if self.width == 0 || self.height == 0 || self.panel_w == 0 || self.panel_h == 0 {
            return Err(SerializeError::InvalidGeometry("zero dimension"));
        }
        if self.outputs == 0 || self.chain == 0 {
            return Err(SerializeError::InvalidGeometry("zero outputs/chain"));
        }
        // Transpose remap constraint: width == card_rows, height == row_width_px.
        let card = (self.card_rows(), self.row_width_px());
        if (self.width as usize, self.height as usize) != card {
            return Err(SerializeError::GeometryMismatch {
                canvas: (self.width, self.height),
                card,
            });
        }
        Ok(())
    }

    /// Assert this config's wiring revision matches a blessed fixture's.
    ///
    /// The Phase-1 golden-conformance harness calls this with the fixture's
    /// `meta.json` `wiring_revision` BEFORE comparing packets, so a config that
    /// has drifted from the blessed physical wiring fails LOUDLY rather than
    /// silently producing a mis-mapped "pass" against a reference for different
    /// cabling (design doc §7 C.1 — the one way the golden scheme fails quietly).
    /// The field is inert without a consumer; this is that consumer, landed now
    /// so the Phase-1 harness slots into pre-built machinery.
    pub fn check_wiring_revision(&self, expected: &str) -> Result<(), SerializeError> {
        if self.wiring_revision != expected {
            return Err(SerializeError::WiringRevisionMismatch {
                config: self.wiring_revision.clone(),
                expected: expected.to_string(),
            });
        }
        Ok(())
    }
}

/// Map a card (row, position-in-row) to a canvas (x, y).
///
/// **Phase-0 DEFAULT = transpose** (`x = row`, `y = pos`), with configurable chain
/// reversal. Rationale: card-space is `card_rows × row_width_px` = 128×96, the
/// canvas is 128×96 transposed, and the natural physical reading of parallel=4 /
/// chain=3 is 4 side-by-side 32-px-wide outputs (the 128-px axis), each a 3-module
/// chain running down the 96-px axis — i.e. `card_row == canvas_x`, `pos == canvas_y`.
/// This is a first-guess ONLY: the true per-module orientation is pinned at
/// Phase-A blessing (design doc §7). Bijective, so the Layer-2 loopback round-trips.
/// `validate()` enforces `width == card_rows` and `height == row_width_px`, so
/// `row < width` and `pos < height` always hold here (no bounds surprise).
#[inline]
fn card_to_canvas(row: usize, pos: usize, cfg: &ColorlightConfig) -> (usize, usize) {
    let x = row;
    let y = if cfg.chain_reversed {
        cfg.row_width_px() - 1 - pos
    } else {
        pos
    };
    (x, y)
}

/// Serialize one RGB888 canvas frame into the ordered Colorlight L2 frames:
/// `[brightness(0x0A), row0(0x55), … rowN-1, latch(0x01)]`.
///
/// `fb` is `width·height·3` bytes, row-major, R,G,B. Returns FULL L2 frames
/// (14-byte Ethernet header included) so the transport is a raw blast and Layer-1
/// pcap conformance can verify the EtherType + dest MAC directly (QA §D.1).
///
/// Takes `fb` by shared ref: the serializer never mutates or aliases it, so the
/// integration is responsible for passing a STABLE snapshot (copy the `glReadPixels`
/// buffer before calling) to avoid the mid-frame tear race (design doc §8).
pub fn serialize_frame(fb: &[u8], cfg: &ColorlightConfig) -> Result<Vec<Vec<u8>>, SerializeError> {
    cfg.validate()?;
    let expected = cfg.width as usize * cfg.height as usize * 3;
    if fb.len() != expected {
        return Err(SerializeError::DimensionMismatch {
            expected_bytes: expected,
            got_bytes: fb.len(),
        });
    }

    let mut frames: Vec<Vec<u8>> = Vec::new();
    frames.push(build_brightness(cfg));

    let width_px = cfg.width as usize;
    let row_w = cfg.row_width_px();
    for row in 0..cfg.card_rows() {
        // Gather this card-row's pixels (row_w of them) in wire color order + LUT.
        let mut payload = Vec::with_capacity(row_w * 3);
        for pos in 0..row_w {
            let (x, y) = card_to_canvas(row, pos, cfg);
            let idx = (y * width_px + x) * 3;
            let r = lut(cfg, fb[idx]);
            let g = lut(cfg, fb[idx + 1]);
            let b = lut(cfg, fb[idx + 2]);
            payload.extend_from_slice(&cfg.color_order.apply(r, g, b));
        }
        // Split into ≤497-px packets, advancing the pixel-offset field.
        let mut px_off = 0usize;
        while px_off < row_w {
            let n = (row_w - px_off).min(MAX_PIXELS_PER_PACKET);
            frames.push(build_row_packet(
                cfg,
                row,
                px_off,
                n,
                &payload[px_off * 3..(px_off + n) * 3],
            ));
            px_off += n;
        }
    }

    frames.push(build_latch(cfg));
    Ok(frames)
}

#[inline]
fn lut(cfg: &ColorlightConfig, v: u8) -> u8 {
    match &cfg.gamma_lut {
        Some(t) => t[v as usize],
        None => v,
    }
}

/// Write the 14-byte Ethernet header (dst MAC, src MAC, EtherType = `opcode<<8 | data0`).
fn eth_header(cfg: &ColorlightConfig, opcode: u8, data0: u8) -> Vec<u8> {
    let mut f = Vec::with_capacity(ETH_HEADER_LEN);
    f.extend_from_slice(&cfg.dest_mac);
    f.extend_from_slice(&cfg.src_mac);
    f.push(opcode); // wire offset 12 (EtherType high byte)
    f.push(data0); // wire offset 13 (EtherType low byte = data[0])
    f
}

/// Brightness packet `0x0A` (77 B): off 13/14/15 = R/G/B brightness, off 16 = 0xFF,
/// rest zero (FPP.cpp:537-550).
fn build_brightness(cfg: &ColorlightConfig) -> Vec<u8> {
    // data0 (off 13) = R brightness → EtherType low byte.
    let mut f = eth_header(cfg, OPCODE_BRIGHTNESS, cfg.brightness);
    f.push(cfg.brightness); // off 14 = G
    f.push(cfg.brightness); // off 15 = B
    f.push(0xFF); // off 16
    f.resize(77, 0); // zero pad to CL_BRIG_PACKET_SIZE
    f
}

/// Pixel-row packet `0x55` (FPP.cpp:583-612). Header (abs offsets):
/// 12=0x55, 13-14=row# BE, 15-16=pixel-offset BE, 17-18=pixel-count BE,
/// 19=0x08, 20=0x88, 21…=payload (3 B/px, already color-ordered).
fn build_row_packet(
    cfg: &ColorlightConfig,
    row: usize,
    pixel_offset: usize,
    pixel_count: usize,
    payload: &[u8],
) -> Vec<u8> {
    // off 13 (data0 / EtherType low byte) = row# high byte.
    let mut f = eth_header(cfg, OPCODE_ROW, (row >> 8) as u8);
    f.push((row & 0xff) as u8); // 14: row# low
    f.push((pixel_offset >> 8) as u8); // 15: offset high (BE)
    f.push((pixel_offset & 0xff) as u8); // 16: offset low
    f.push((pixel_count >> 8) as u8); // 17: count high (BE)
    f.push((pixel_count & 0xff) as u8); // 18: count low
    f.push(ROW_MAGIC_A); // 19
    f.push(ROW_MAGIC_B); // 20
    f.extend_from_slice(payload); // 21…
    f
}

/// Display/sync/latch packet `0x01` (112 B) — commits the streamed buffer to the
/// LEDs (FPP.cpp:654-671). Fixed sender-type bytes off13=0x07 + off36=0x05 are
/// the PC/Netcard values (a hardware S2 sender emits 0x00 for both); brightness at
/// off 35/38/39/40. All to be re-confirmed against the Phase-1 latch golden (§8:
/// latch bytes are firmware-sensitive).
fn build_latch(cfg: &ColorlightConfig) -> Vec<u8> {
    let mut f = eth_header(cfg, OPCODE_LATCH, 0x07); // off13 data0 = 0x07 (PC sender)
    f.resize(112, 0); // CL_SYNC_PACKET_SIZE
    f[35] = cfg.brightness;
    f[36] = 0x05; // fixed PC-sender byte (FPP.cpp:666-671; 0x00 from an S2 sender)
    f[38] = cfg.brightness;
    f[39] = cfg.brightness;
    f[40] = cfg.brightness;
    f
}

// ── Layer-2 loopback emulator ────────────────────────────────────────────────
//
// A software receiver: parses our L2 frames per spec, reconstructs the canvas.
// CALIBRATION (design doc §6): this proves only that our encode/decode is
// self-consistent + lossless — NOT that the wire format matches a real Colorlight.
// Only Layer-1 golden-pcap conformance proves protocol correctness.

/// Parse a stream of our frames back into a canvas RGB888 buffer. Mirrors the
/// serializer inverse: reads `0x55` rows, undoes color order + LUT + remap.
/// (Test-only helper — not compiled into the shipping binary.)
#[cfg(test)]
fn parse_frames_to_canvas(
    frames: &[Vec<u8>],
    cfg: &ColorlightConfig,
) -> Result<Vec<u8>, SerializeError> {
    cfg.validate()?;
    let width_px = cfg.width as usize;
    let mut canvas = vec![0u8; cfg.width as usize * cfg.height as usize * 3];
    let inv_lut = cfg.gamma_lut.map(invert_lut);
    // A valid 0x55 frame is header(21) + payload; anything shorter than the full
    // header can't be a row packet. Guard against short/malformed frames so the
    // rig can safely ingest EXTERNAL pcaps (FPP captures) at Layer 1, not just our
    // own well-formed output.
    const ROW_HEADER_END: usize = 21;
    for f in frames {
        if f.len() < ROW_HEADER_END || f[12] != OPCODE_ROW {
            continue; // brightness / latch / too-short — not usable pixel data
        }
        let row = ((f[13] as usize) << 8) | f[14] as usize;
        let px_off = ((f[15] as usize) << 8) | f[16] as usize;
        let count = ((f[17] as usize) << 8) | f[18] as usize;
        let body = &f[ROW_HEADER_END..];
        // Clamp to the pixels actually present (a truncated capture must not panic).
        let count = count.min(body.len() / 3);
        for i in 0..count {
            let (wr, wg, wb) = (body[i * 3], body[i * 3 + 1], body[i * 3 + 2]);
            let (mut r, mut g, mut b) = unapply_color_order(cfg.color_order, wr, wg, wb);
            if let Some(inv) = &inv_lut {
                r = inv[r as usize];
                g = inv[g as usize];
                b = inv[b as usize];
            }
            let (x, y) = card_to_canvas(row, px_off + i, cfg);
            let idx = (y * width_px + x) * 3;
            canvas[idx] = r;
            canvas[idx + 1] = g;
            canvas[idx + 2] = b;
        }
    }
    Ok(canvas)
}

#[cfg(test)]
fn unapply_color_order(order: ColorOrder, a: u8, b: u8, c: u8) -> (u8, u8, u8) {
    match order {
        ColorOrder::Rgb => (a, b, c),
        ColorOrder::Bgr => (c, b, a),
        ColorOrder::Grb => (b, a, c),
        ColorOrder::Gbr => (c, a, b),
        ColorOrder::Rbg => (a, c, b),
        ColorOrder::Brg => (b, c, a),
    }
}

#[cfg(test)]
fn invert_lut(t: [u8; 256]) -> [u8; 256] {
    // Only valid for a monotonic bijective LUT; the tests use identity/monotone.
    let mut inv = [0u8; 256];
    for (i, &v) in t.iter().enumerate() {
        inv[v as usize] = i as u8;
    }
    inv
}

#[cfg(test)]
mod tests {
    use super::*;

    fn solid(cfg: &ColorlightConfig, r: u8, g: u8, b: u8) -> Vec<u8> {
        let n = cfg.width as usize * cfg.height as usize;
        let mut v = Vec::with_capacity(n * 3);
        for _ in 0..n {
            v.extend_from_slice(&[r, g, b]);
        }
        v
    }

    /// Framebuffer whose every pixel is a distinct function of its canvas index —
    /// used where a solid fill would mask a slice/offset bug (the payload must
    /// actually differ across a packet boundary for a roundtrip to catch it).
    fn positional(cfg: &ColorlightConfig) -> Vec<u8> {
        let n = cfg.width as usize * cfg.height as usize;
        let mut v = Vec::with_capacity(n * 3);
        for k in 0..n {
            v.extend_from_slice(&[
                (k & 0xff) as u8,
                ((k >> 8) & 0xff) as u8,
                ((k >> 16) & 0xff) as u8,
            ]);
        }
        v
    }

    // ── Structure / Layer-1 framing (wiring-INDEPENDENT; safe to assert now) ──

    #[test]
    fn frame_order_is_brightness_then_rows_then_latch() {
        let cfg = ColorlightConfig::thinksign_default();
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        // 1 brightness + 128 rows (96px each, single-packet) + 1 latch.
        assert_eq!(frames.len(), 1 + cfg.card_rows() + 1);
        assert_eq!(frames[0][12], OPCODE_BRIGHTNESS);
        assert_eq!(*frames.last().unwrap().get(12).unwrap(), OPCODE_LATCH);
        // rows ascending 0..127.
        for (i, f) in frames[1..=cfg.card_rows()].iter().enumerate() {
            assert_eq!(f[12], OPCODE_ROW, "packet {i} not a row");
            let row = ((f[13] as usize) << 8) | f[14] as usize;
            assert_eq!(row, i, "rows must be ascending");
        }
    }

    #[test]
    fn every_frame_carries_full_l2_header_and_ethertype() {
        let cfg = ColorlightConfig::thinksign_default();
        let frames = serialize_frame(&solid(&cfg, 1, 2, 3), &cfg).unwrap();
        for f in &frames {
            assert_eq!(&f[0..6], &DEFAULT_DEST_MAC, "dest MAC");
            assert_eq!(&f[6..12], &DEFAULT_SRC_MAC, "src MAC");
            // EtherType = opcode<<8 | data0 (bytes 12,13 are already that).
            assert!(matches!(
                f[12],
                OPCODE_BRIGHTNESS | OPCODE_ROW | OPCODE_LATCH
            ));
        }
    }

    #[test]
    fn brightness_packet_exact_bytes() {
        // The loopback skips non-0x55 frames, so pin the brightness bytes directly
        // (guards the one-off-shift class of bug the review flagged).
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.brightness = 0xC1; // hkubota's "50%" example value
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        let b = &frames[0];
        assert_eq!(b[12], OPCODE_BRIGHTNESS);
        assert_eq!(
            [b[13], b[14], b[15]],
            [0xC1, 0xC1, 0xC1],
            "R/G/B at off 13/14/15"
        );
        assert_eq!(b[16], 0xFF, "off 16 fixed 0xFF");
        assert_eq!(b.len(), 77, "CL_BRIG_PACKET_SIZE");
    }

    #[test]
    fn latch_packet_exact_bytes() {
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.brightness = 0xAB;
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        let l = frames.last().unwrap();
        assert_eq!(l[12], OPCODE_LATCH);
        assert_eq!(l[13], 0x07, "off 13 = 0x07 (PC sender)");
        assert_eq!(l[35], 0xAB, "off 35 brightness");
        assert_eq!(l[36], 0x05, "off 36 fixed PC-sender byte");
        assert_eq!(
            [l[38], l[39], l[40]],
            [0xAB, 0xAB, 0xAB],
            "off 38/39/40 R/G/B"
        );
        assert_eq!(l.len(), 112, "CL_SYNC_PACKET_SIZE");
    }

    #[test]
    fn row_packet_has_magic_and_correct_count() {
        let cfg = ColorlightConfig::thinksign_default();
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        let row0 = &frames[1];
        assert_eq!(row0[19], ROW_MAGIC_A);
        assert_eq!(row0[20], ROW_MAGIC_B);
        let count = ((row0[17] as usize) << 8) | row0[18] as usize;
        assert_eq!(count, cfg.row_width_px(), "96 px per row");
        assert_eq!(row0.len(), 21 + cfg.row_width_px() * 3);
    }

    #[test]
    fn channel_order_lands_in_the_right_wire_bytes() {
        // Pure-R input under BGR must put R (255) in the 3rd payload byte.
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.color_order = ColorOrder::Bgr;
        let frames = serialize_frame(&solid(&cfg, 255, 0, 0), &cfg).unwrap();
        let row0 = &frames[1];
        assert_eq!(&row0[21..24], &[0, 0, 255], "BGR: R in byte 3");
    }

    #[test]
    fn all_six_color_orders_are_exact_inverses() {
        // apply() and unapply_color_order() are two separate hand-written 6-arm
        // tables; only BGR was proven above. Pin every order as an exact inverse so
        // a future edit to one table can't silently break the loopback's decode
        // (QA catch #3).
        let samples = [(10u8, 200, 30), (0, 0, 0), (255, 128, 1), (7, 7, 7)];
        for order in [
            ColorOrder::Rgb,
            ColorOrder::Bgr,
            ColorOrder::Grb,
            ColorOrder::Gbr,
            ColorOrder::Rbg,
            ColorOrder::Brg,
        ] {
            for &(r, g, b) in &samples {
                let w = order.apply(r, g, b);
                assert_eq!(
                    unapply_color_order(order, w[0], w[1], w[2]),
                    (r, g, b),
                    "{order:?} apply→unapply not identity"
                );
            }
        }
    }

    #[test]
    fn non_identity_lut_maps_the_payload_bytes() {
        // The gamma_lut path was dead-untested: every other test uses
        // thinksign_default() → gamma_lut: None, so `t[v]` (and invert_lut in the
        // loopback) never ran (QA's top catch #1). Build an UNMISTAKABLE table (not
        // a formula): identity with three overrides, so a byte-exact wire assert
        // proves a real lookup, not arithmetic that happens to match.
        let mut cfg = ColorlightConfig::thinksign_default();
        let mut t: [u8; 256] = core::array::from_fn(|i| i as u8);
        t[10] = 222;
        t[200] = 13;
        t[30] = 99;
        t[255] = 7; // make the brightness-not-LUT'd assert below actually bite:
                    // default brightness is 255, so a regression that routed it
                    // through the LUT would show 7, not 255.
        cfg.gamma_lut = Some(t);
        // Input pixel (10, 200, 30) → LUT → (222, 13, 99), RGB order → wire bytes.
        let frames = serialize_frame(&solid(&cfg, 10, 200, 30), &cfg).unwrap();
        assert_eq!(
            &frames[1][21..24],
            &[222, 13, 99],
            "each channel routed through the LUT on the wire"
        );
        // Brightness/latch must NOT be LUT'd — only the pixel payload is. With
        // t[255]=7 and brightness=255, this fails if the brightness byte is ever
        // routed through the table.
        assert_eq!(frames[0][13], 255, "brightness is not LUT-mapped");
    }

    #[test]
    fn all_white_payload_and_brightness_endpoints_are_byte_exact() {
        // 255 → 0xFF on the wire (no stray clamp/scale) and brightness 0/255
        // endpoints land exactly (LUT-endpoint / off-by-one guard, QA A.3).
        let cfg = ColorlightConfig::thinksign_default();
        let white = serialize_frame(&solid(&cfg, 255, 255, 255), &cfg).unwrap();
        assert_eq!(
            &white[1][21..24],
            &[0xFF, 0xFF, 0xFF],
            "white pixel is 0xFF on the wire"
        );

        let mut dark = ColorlightConfig::thinksign_default();
        dark.brightness = 0;
        let f0 = serialize_frame(&solid(&dark, 0, 0, 0), &dark).unwrap();
        assert_eq!(
            [f0[0][13], f0[0][14], f0[0][15]],
            [0, 0, 0],
            "brightness 0 endpoint in the 0x0A packet"
        );
        assert_eq!(f0.last().unwrap()[35], 0, "latch carries brightness 0");

        let mut full = ColorlightConfig::thinksign_default();
        full.brightness = 255;
        let f255 = serialize_frame(&solid(&full, 0, 0, 0), &full).unwrap();
        assert_eq!(
            [f255[0][13], f255[0][14], f255[0][15]],
            [255, 255, 255],
            "brightness 255 endpoint"
        );
    }

    // ── Layer-2 loopback (self-consistency ONLY — not wire correctness) ──

    #[test]
    fn loopback_roundtrips_a_pattern_bit_exact() {
        let cfg = ColorlightConfig::thinksign_default();
        // A deterministic non-trivial pattern.
        let n = cfg.width as usize * cfg.height as usize;
        let mut fb = Vec::with_capacity(n * 3);
        for i in 0..n {
            fb.extend_from_slice(&[(i & 0xff) as u8, (i >> 3) as u8, (i >> 5) as u8]);
        }
        let frames = serialize_frame(&fb, &cfg).unwrap();
        let back = parse_frames_to_canvas(&frames, &cfg).unwrap();
        assert_eq!(back, fb, "serialize→parse must be lossless");
    }

    #[test]
    fn loopback_roundtrips_with_bgr_and_chain_reversed() {
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.color_order = ColorOrder::Bgr;
        cfg.chain_reversed = true;
        let fb = solid(&cfg, 10, 200, 30);
        let frames = serialize_frame(&fb, &cfg).unwrap();
        assert_eq!(parse_frames_to_canvas(&frames, &cfg).unwrap(), fb);
    }

    #[test]
    fn loopback_roundtrips_through_a_bijective_lut() {
        // Exercise the invert_lut decode path (dead without a LUT — QA catch #1b).
        // invert_lut only inverts a BIJECTIVE table, so use v→255-v (a real
        // non-identity remap, strictly monotone hence bijective). A production
        // gamma curve has plateaus (non-bijective) and is apply-only — the encode
        // byte test above covers that; this proves the encode/decode inverse holds
        // when the LUT is invertible, so the loopback stays trustworthy with a LUT set.
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.gamma_lut = Some(core::array::from_fn(|i| (255 - i) as u8));
        // A position-dependent pattern so the LUT is exercised across the value range.
        let fb = positional(&cfg);
        let frames = serialize_frame(&fb, &cfg).unwrap();
        assert_eq!(
            parse_frames_to_canvas(&frames, &cfg).unwrap(),
            fb,
            "encode-through-LUT then decode-through-invert-LUT is lossless"
        );
    }

    // ── Layer-3 cadence ──

    #[test]
    fn cadence_packet_counts() {
        let cfg = ColorlightConfig::thinksign_default();
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        let rows = frames.iter().filter(|f| f[12] == OPCODE_ROW).count();
        let bright = frames.iter().filter(|f| f[12] == OPCODE_BRIGHTNESS).count();
        let latch = frames.iter().filter(|f| f[12] == OPCODE_LATCH).count();
        assert_eq!((bright, rows, latch), (1, 128, 1));
    }

    // ── B.2 synthetic tests: code paths the 128×96 config NEVER exercises ──
    //    (goldens structurally CANNOT catch these — QA's top review catches.)

    #[test]
    fn synthetic_multi_packet_row_split_over_497px() {
        // Force a row wider than one packet: chain=20 · panel_w=32 = 640 px > 497.
        // Transpose-valid dims: width==card_rows(=1·32=32), height==row_width(=640).
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.width = 32;
        cfg.height = 640;
        cfg.outputs = 1; // card_rows = 1·32 = 32
        cfg.chain = 20; // 20·32 = 640 px row
        assert_eq!(cfg.row_width_px(), 640);
        assert_eq!(cfg.card_rows(), 32);
        // Position-dependent payload (NOT a solid): every pixel is distinct, so a
        // slice off-by-one / duplicate-slice in the split (packet 2 must carry
        // pixels 497..639, not 496.. or 498..) actually diverges. A solid fill
        // would round-trip green even with a broken slice (QA catch #2).
        let fb = positional(&cfg);
        let frames = serialize_frame(&fb, &cfg).unwrap();
        // Each of the 32 rows splits into ceil(640/497)=2 packets.
        let row_pkts: Vec<&Vec<u8>> = frames.iter().filter(|f| f[12] == OPCODE_ROW).collect();
        assert_eq!(row_pkts.len(), 32 * 2, "each 640px row = 2 packets");
        // Row 0's two packets: offset 0 count 497, then offset 497 count 143.
        let p0 = row_pkts[0];
        let off0 = ((p0[15] as usize) << 8) | p0[16] as usize;
        let cnt0 = ((p0[17] as usize) << 8) | p0[18] as usize;
        assert_eq!((off0, cnt0), (0, 497));
        let p1 = row_pkts[1];
        let off1 = ((p1[15] as usize) << 8) | p1[16] as usize;
        let cnt1 = ((p1[17] as usize) << 8) | p1[18] as usize;
        assert_eq!(
            (off1, cnt1),
            (497, 143),
            "offset advances by 497, remainder 143"
        );
        // Directly pin the slice boundary: packet 2's first payload pixel must be
        // canvas pixel at card position (row 0, pos 497) — RGB order, no LUT, so the
        // wire bytes equal the raw fb pixel. An off-by-one lands 496 or 498 here.
        let (x, y) = card_to_canvas(0, 497, &cfg);
        let k = y * cfg.width as usize + x;
        assert_eq!(
            &p1[21..24],
            &fb[k * 3..k * 3 + 3],
            "packet 2 starts at pixel 497, not 496/498"
        );
        // And the whole position-dependent frame round-trips bit-exact.
        assert_eq!(parse_frames_to_canvas(&frames, &cfg).unwrap(), fb);
    }

    #[test]
    fn synthetic_big_endian_row_number_over_255() {
        // Production rows are 0-127 (high byte always 0), so a BE-vs-LE bug is
        // INVISIBLE to every capturable golden. Force row# > 255: outputs=16 ·
        // panel_h=32 = 512 card rows. Transpose-valid dims: width==card_rows(512),
        // height==row_width(=1·32=32).
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.width = 512;
        cfg.height = 32;
        cfg.panel_w = 32;
        cfg.panel_h = 32;
        cfg.outputs = 16; // card_rows = 16·32 = 512
        cfg.chain = 1; // 1·32 = 32 px per row
        assert_eq!(cfg.card_rows(), 512);
        assert_eq!(cfg.row_width_px(), 32);
        let frames = serialize_frame(&solid(&cfg, 0, 0, 0), &cfg).unwrap();
        let rows: Vec<&Vec<u8>> = frames.iter().filter(|f| f[12] == OPCODE_ROW).collect();
        // Row 300 = 0x012C: high byte 0x01 at off 13, low 0x2C at off 14.
        let r300 = rows[300];
        assert_eq!(r300[13], 0x01, "row# high byte (BE) at off 13");
        assert_eq!(r300[14], 0x2C, "row# low byte at off 14");
        // Row 511 = 0x01FF.
        assert_eq!((rows[511][13], rows[511][14]), (0x01, 0xFF));
    }

    // ── Robustness / edge cases (QA §8) ──

    #[test]
    fn dimension_mismatch_is_an_error_not_a_panic() {
        let cfg = ColorlightConfig::thinksign_default();
        let too_small = vec![0u8; 10];
        assert!(matches!(
            serialize_frame(&too_small, &cfg),
            Err(SerializeError::DimensionMismatch { .. })
        ));
    }

    #[test]
    fn non_bijective_geometry_is_an_error() {
        let mut cfg = ColorlightConfig::thinksign_default();
        cfg.chain = 2; // 2·32=64px row × 128 rows = 8192 ≠ 128·96=12288 canvas px
        assert!(matches!(
            serialize_frame(&solid_for_dims(128, 96), &cfg),
            Err(SerializeError::GeometryMismatch { .. })
        ));
    }

    #[test]
    fn serializer_does_not_alias_input_snapshot() {
        // serialize takes &fb and never mutates it — the mid-frame-tear contract
        // (design doc §8) is "caller passes a stable snapshot". Prove the output
        // is a pure function of the bytes at call time (mutating a COPY after
        // doesn't change what we produced).
        let cfg = ColorlightConfig::thinksign_default();
        let fb = solid(&cfg, 5, 6, 7);
        let a = serialize_frame(&fb, &cfg).unwrap();
        let mut fb2 = fb.clone();
        fb2[0] = 200; // mutate a separate buffer
        let b = serialize_frame(&fb, &cfg).unwrap(); // same original fb
        assert_eq!(a, b, "output depends only on the passed snapshot");
        assert_eq!(fb[0], 5, "serialize must not mutate the input");
    }

    #[test]
    fn check_wiring_revision_gates_on_mismatch() {
        // The wiring_revision field is inert without a consumer; check_wiring_revision
        // is that consumer (design doc §7 C.1). The Phase-1 golden harness calls it
        // with the fixture's meta.wiring_revision so a config that drifted from the
        // blessed cabling FAILS LOUD instead of "passing" against the wrong reference.
        let cfg = ColorlightConfig::thinksign_default(); // "unblessed-phase0-default"
        assert!(cfg
            .check_wiring_revision("unblessed-phase0-default")
            .is_ok());
        assert!(matches!(
            cfg.check_wiring_revision("thinksign-blessed-2026-07-20"),
            Err(SerializeError::WiringRevisionMismatch { .. })
        ));
    }

    fn solid_for_dims(w: usize, h: usize) -> Vec<u8> {
        vec![0u8; w * h * 3]
    }
}
