//! HUB75-direct transport — Phase 0 skeleton.
//!
//! Pattern-mirrors `hdmi.rs` / `colorlight_logic.rs` split: pure logic
//! lives in `hub75_logic.rs`; this module owns the eventual GPIO
//! transport. Phase 0 (this commit) is the API skeleton only — the
//! `GpioSink` wraps `hub75_logic::serialize_frame` and counts frames
//! but does NOT yet dispatch to hzeller's `rpi-rgb-led-matrix` C++
//! library. That FFI backend lands in a follow-up commit once the
//! `--features hub75-direct` cross-build gate (including a sysroot
//! with librgbmatrix build deps) is proven.
//!
//! Split rationale (design doc §4): keeping the skeleton unconditional
//! (no `#[cfg]` gate yet) lets the Mac `cargo test` gate exercise the
//! pre-processing pipeline end-to-end through the sink. When the FFI
//! backend lands, this module becomes:
//!
//! ```text
//! #[cfg(all(target_os = "linux", feature = "hub75-direct"))]  → real FFI
//! #[cfg(not(all(target_os = "linux", feature = "hub75-direct")))]  → this skeleton
//! ```
//!
//! Same "no-op stub off Linux" shape as `hdmi.rs`. See design doc §9
//! for the phased plan.

use crate::hub75_logic::{serialize_frame, Hub75Config, SerializeError};

/// Return type — a real error, not `SerializeError` alone, so future
/// FFI errors (permission denied, GPIO already in use, SCHED_FIFO
/// refused) slot in without a breaking-change API bump. Design doc §8
/// covers the "never wedge the renderer" contract enforced at the arm
/// (main.rs) — `GpioSink::new` returning `Err` is the arm's cue to
/// log + no-op-continue, mirroring Colorlight §8.
#[derive(Debug)]
pub enum GpioSinkError {
    /// The provided `Hub75Config` failed `validate()` — see the
    /// inner `SerializeError` for the specific bound violated.
    InvalidConfig(SerializeError),
    /// A per-frame pre-processing error (dimension mismatch etc.).
    /// Recoverable — the next frame may succeed if the caller
    /// switches to the correct card-native size.
    SerializeFailed(SerializeError),
    /// Phase-1 placeholder for FFI errors (backend unavailable,
    /// permission denied, SCHED_FIFO refused, panel disconnected).
    /// Not emitted by the Phase-0 skeleton.
    #[allow(dead_code)]
    TransportUnavailable(&'static str),
}

impl std::fmt::Display for GpioSinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidConfig(e) => write!(f, "hub75 config invalid: {e:?}"),
            Self::SerializeFailed(e) => write!(f, "hub75 serialize failed: {e:?}"),
            Self::TransportUnavailable(msg) => write!(f, "hub75 transport unavailable: {msg}"),
        }
    }
}

impl std::error::Error for GpioSinkError {}

/// One-shot sink for card-native RGB888 frames. Owns the config and
/// (in Phase 1) the hzeller `LedMatrix` handle.
///
/// The Phase-0 skeleton pre-processes each frame via
/// `hub75_logic::serialize_frame` (so the LUT/color-order pipeline is
/// exercised end-to-end) but discards the wire-ready bytes at the FFI
/// boundary. When the backend lands, the discard becomes
/// `LedMatrix::set_image(&wire)`.
#[derive(Debug)]
pub struct GpioSink {
    cfg: Hub75Config,
    frames_blit: u64,
}

impl GpioSink {
    /// Construct a sink from a validated config. Fails on any
    /// `Hub75Config::validate()` violation — no side effects if it
    /// errors so the caller can safely fall back to a diagnostic.
    pub fn new(cfg: Hub75Config) -> Result<Self, GpioSinkError> {
        cfg.validate().map_err(GpioSinkError::InvalidConfig)?;
        Ok(Self {
            cfg,
            frames_blit: 0,
        })
    }

    /// Blit one card-native RGB888 frame. `canvas_rgb` MUST be
    /// exactly `card_width_px · card_height_px · 3` bytes (see
    /// `Hub75Config` for the derived dims); a mismatch returns
    /// `SerializeFailed` and does not mutate the sink counter.
    ///
    /// **Phase 0 skeleton behavior:** pre-processes the frame via
    /// `hub75_logic::serialize_frame` but DROPS the wire bytes at
    /// the FFI boundary — no pixels light up until Phase 1 links
    /// hzeller's `rpi-rgb-led-matrix`. See the module docstring for
    /// the phased plan. The counter still advances so callers can
    /// prove the pre-processing pipeline is alive.
    ///
    /// TODO (Phase 1): `serialize_frame` re-runs `validate()` per
    /// call; at 30 Hz this is wasted work since `new` already
    /// validated the immutable config. Consider a
    /// `serialize_frame_unchecked` fast-path.
    pub fn blit(&mut self, canvas_rgb: &[u8]) -> Result<(), GpioSinkError> {
        let wire = serialize_frame(canvas_rgb, &self.cfg).map_err(GpioSinkError::SerializeFailed)?;
        // Phase-1 wiring point: hand `wire` to `LedMatrix::set_image`.
        // Until then, drop-on-the-floor so the pipeline is exercised
        // without needing the FFI backend linked.
        let _ = wire;
        self.frames_blit += 1;
        Ok(())
    }

    /// Count of frames the sink has accepted since construction.
    ///
    /// **Phase 0 skeleton:** increments after `serialize_frame`
    /// succeeds (there is no transport yet, so "sent" and
    /// "pre-processed" are the same event).
    ///
    /// **Phase 1 contract:** MUST increment only AFTER the FFI
    /// transport accepts the frame — pre-process success alone is
    /// no longer enough. This keeps operator diagnostics honest:
    /// a panel-disconnected sign that keeps pre-processing must
    /// NOT report climbing frame counts. Callers should treat this
    /// as "frames actually delivered to the transport."
    #[inline]
    pub fn frames_sent(&self) -> u64 {
        self.frames_blit
    }

    /// Read-only view of the config the sink was built with. Useful
    /// for the arm-fill diagnostic ("about to blit at card_w×card_h,
    /// pwm_bits=N, color=X").
    #[inline]
    pub fn config(&self) -> &Hub75Config {
        &self.cfg
    }
}

// ── Tests ────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn card_native_solid(cfg: &Hub75Config, r: u8, g: u8, b: u8) -> Vec<u8> {
        let n = cfg.card_width_px() * cfg.card_height_px();
        let mut v = Vec::with_capacity(n * 3);
        for _ in 0..n {
            v.extend_from_slice(&[r, g, b]);
        }
        v
    }

    #[test]
    fn new_validates_config_and_fails_gracefully() {
        let mut cfg = Hub75Config::fallback_1chain();
        cfg.pwm_bits = 12; // one over the hzeller ceiling
        let err = GpioSink::new(cfg).unwrap_err();
        assert!(
            matches!(err, GpioSinkError::InvalidConfig(SerializeError::ConfigOutOfRange { .. })),
            "expected InvalidConfig(ConfigOutOfRange), got {err:?}"
        );
    }

    #[test]
    fn new_succeeds_on_fallback_config() {
        let sink = GpioSink::new(Hub75Config::fallback_1chain()).unwrap();
        assert_eq!(sink.frames_sent(), 0);
        assert_eq!(sink.config().card_width_px(), 96);
        assert_eq!(sink.config().card_height_px(), 32);
    }

    #[test]
    fn blit_advances_frame_counter_on_success() {
        let mut sink = GpioSink::new(Hub75Config::fallback_1chain()).unwrap();
        let fb = card_native_solid(sink.config(), 10, 20, 30);
        sink.blit(&fb).unwrap();
        sink.blit(&fb).unwrap();
        sink.blit(&fb).unwrap();
        assert_eq!(sink.frames_sent(), 3);
    }

    #[test]
    fn blit_wrong_size_errors_and_does_not_advance_counter() {
        // Pipeline discipline: a per-frame error is recoverable — the
        // next frame with the right dims must go through. Counter must
        // NOT advance on the failing blit so operator diagnostics
        // reflect what actually landed on the panel.
        let mut sink = GpioSink::new(Hub75Config::fallback_1chain()).unwrap();
        let good = card_native_solid(sink.config(), 5, 6, 7);
        sink.blit(&good).unwrap();
        assert_eq!(sink.frames_sent(), 1);

        let bad = vec![0u8; 100]; // wrong size
        let err = sink.blit(&bad).unwrap_err();
        assert!(
            matches!(err, GpioSinkError::SerializeFailed(SerializeError::DimensionMismatch { .. })),
            "expected DimensionMismatch, got {err:?}"
        );
        assert_eq!(
            sink.frames_sent(),
            1,
            "failed blit must NOT advance the frame counter"
        );

        // Recovery: a correctly-sized frame after the failure lands cleanly.
        sink.blit(&good).unwrap();
        assert_eq!(sink.frames_sent(), 2);
    }

    #[test]
    fn error_display_covers_every_variant() {
        // The arm-fill diagnostic renders these; every variant must give
        // an operator something they can act on, not just "Err". A typo
        // in any Display format string would ship silently and land as
        // garbage in a real incident — cover all three arms.
        let inv = GpioSinkError::InvalidConfig(SerializeError::InvalidGeometry("panel dims"));
        let s = format!("{inv}");
        assert!(s.contains("hub75 config invalid"), "InvalidConfig: {s}");
        assert!(s.contains("InvalidGeometry"), "InvalidConfig inner: {s}");

        let ser = GpioSinkError::SerializeFailed(SerializeError::DimensionMismatch {
            expected_bytes: 100,
            got_bytes: 50,
        });
        let s = format!("{ser}");
        assert!(s.contains("hub75 serialize failed"), "SerializeFailed: {s}");
        assert!(s.contains("DimensionMismatch"), "SerializeFailed inner: {s}");

        // Phase-1 path — operators use this to distinguish
        // perm-denied from SCHED_FIFO-refused from panel-disconnected,
        // so its Display MUST include the inner message.
        let tr = GpioSinkError::TransportUnavailable("SCHED_FIFO refused (no CAP_SYS_NICE)");
        let s = format!("{tr}");
        assert!(s.contains("hub75 transport unavailable"), "TransportUnavailable: {s}");
        assert!(s.contains("SCHED_FIFO refused"), "TransportUnavailable inner: {s}");
    }
}
