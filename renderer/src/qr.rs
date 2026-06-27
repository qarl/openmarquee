//! PR3 (2026-06-27) QR-code bitmap generation for the SETUP +
//! DEGRADED onboarding cards.
//!
//! Spec §"The display is the onboarding UI" requires a wifi-join QR
//! code on the marquee so the operator scans with their phone and
//! the openMarquee-Setup AP joins automatically. The payload is the
//! `WIFI:T:WPA;S:<ssid>;P:<pin>;;` URI; iOS Camera + Android Camera
//! both natively recognise it.
//!
//! Wraps the `qrcode` crate (kennytm/0.14, pure-Rust MIT/Apache) +
//! returns a square boolean bitmap the GLES2 layer turns into a
//! white-with-black-modules texture for the card's white panel.
//!
//! Pure module — host-runnable on macOS (no GL deps); the GLES2
//! upload happens in `system_card.rs`.
//!
//! Error correction: QR Level M (15% recovery). Spec §"display is
//! the onboarding UI" requires scannable from ~3m; M is the
//! standard trade-off (L is fine across the room but degrades on
//! camera/glass glare; H wastes modules). A 25-char WIFI: URI fits
//! in V2 (25×25) at level M.

use qrcode::{EcLevel, QrCode};

/// A square boolean bitmap. `true` = black module, `false` = white
/// background. `width == height == self.size`; row-major.
#[derive(Debug, Clone)]
pub struct QrBitmap {
    pub size: usize,
    pub pixels: Vec<bool>,
}

impl QrBitmap {
    /// Return the module (true = black) at (x, y). Returns false
    /// for out-of-bounds (defensive — the renderer's draw shouldn't
    /// request these but a paranoid bounds check is cheap).
    pub fn module(&self, x: usize, y: usize) -> bool {
        if x >= self.size || y >= self.size {
            return false;
        }
        self.pixels[y * self.size + x]
    }

    /// Total module count (size × size). Convenience for tests.
    pub fn module_count(&self) -> usize {
        self.size * self.size
    }
}

/// Encode `payload` as a QR code at error-correction level M and
/// return the boolean bitmap. Returns Err if the payload is too
/// large to fit any QR version (the `qrcode` crate's upper bound
/// is V40 / 177×177).
///
/// Typical onboarding WIFI: payload `WIFI:T:WPA;S:openMarquee-Setup;P:4827;;`
/// = ~36 chars, fits V2 (25×25 modules) at level M.
pub fn encode_qr(payload: &str) -> Result<QrBitmap, String> {
    let code = QrCode::with_error_correction_level(payload.as_bytes(), EcLevel::M)
        .map_err(|e| format!("QR encode failed for {}-byte payload: {}", payload.len(), e))?;
    let bools = code.to_colors();
    let size = code.width();
    // qrcode's `to_colors()` returns one Color per module; map to
    // bool where Dark = true.
    let pixels: Vec<bool> = bools.into_iter().map(|c| c == qrcode::Color::Dark).collect();
    debug_assert_eq!(pixels.len(), size * size);
    Ok(QrBitmap { size, pixels })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A 0-char payload encodes to the smallest QR (V1, 21×21).
    /// Smoke test that the wrapper round-trips a trivial input.
    #[test]
    fn encodes_empty_payload() {
        let qr = encode_qr("").expect("empty payload should encode");
        assert!(qr.size >= 21, "QR V1 is 21×21; got size={}", qr.size);
        assert_eq!(qr.pixels.len(), qr.size * qr.size);
    }

    /// The canonical onboarding payload encodes + carries the
    /// finder patterns (the three corner squares are the QR
    /// hallmark). We don't decode here — that would require a QR
    /// reader; instead we check structural invariants.
    #[test]
    fn encodes_canonical_wifi_uri() {
        let payload = "WIFI:T:WPA;S:openMarquee-Setup;P:4827;;";
        let qr = encode_qr(payload).expect("WIFI: URI should encode");
        // V2 (25×25) is the expected version at level M for ~36 chars;
        // tolerate V2 or larger if the crate's encoder picks a roomier
        // version for the EC layer.
        assert!(qr.size >= 21);
        // Finder pattern smoke: the top-left 7×7 should be the
        // canonical solid border (modules 0..7 in row 0 must include
        // the top edge of the finder pattern, which is all true).
        let top_row: Vec<bool> = (0..7).map(|x| qr.module(x, 0)).collect();
        assert!(
            top_row.iter().all(|&m| m),
            "top-left finder pattern's top row must be all dark; got {:?}",
            top_row
        );
        // Top-right finder pattern: same shape at (size-7, 0).
        let top_right_row: Vec<bool> = (0..7).map(|x| qr.module(qr.size - 7 + x, 0)).collect();
        assert!(
            top_right_row.iter().all(|&m| m),
            "top-right finder pattern's top row must be all dark; got {:?}",
            top_right_row
        );
        // Bottom-left finder pattern: same shape at (0, size-7).
        let bottom_left_row: Vec<bool> = (0..7).map(|x| qr.module(x, qr.size - 7)).collect();
        assert!(
            bottom_left_row.iter().all(|&m| m),
            "bottom-left finder pattern's top row must be all dark; got {:?}",
            bottom_left_row
        );
    }

    /// module() must be bounds-safe so the renderer's draw loop
    /// can't panic on a logic error.
    #[test]
    fn module_oob_returns_false() {
        let qr = encode_qr("test").expect("encodes");
        assert!(!qr.module(qr.size, 0));
        assert!(!qr.module(0, qr.size));
        assert!(!qr.module(usize::MAX, usize::MAX));
    }

    #[test]
    fn module_count_matches_size_squared() {
        let qr = encode_qr("abc").expect("encodes");
        assert_eq!(qr.module_count(), qr.size * qr.size);
    }
}
