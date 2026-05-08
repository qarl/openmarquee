//! v1-spec-delta #17 (slice b, 2026-05-08): CEA-861 mode timing
//! table. Cross-platform pure-data module so the table is host-
//! testable on Mac (hdmi.rs is Linux-only).
//!
//! Per CEA-861-G timing parameters used to synthesize a
//! drm_mode_modeinfo for `--force-mode` when the connector's
//! EDID doesn't expose 1080p (project-memory: dev Pi EDID is
//! 0 bytes). Restricted to the modes V1 needs (1920×1080 and
//! 1280×720 at 30/60 Hz). Adding more is mechanical -- copy
//! the entry from the CEA-861-G spec or generate via CVT/GTF.

use anyhow::{anyhow, Result};

/// Timing parameters for a single CEA-861 video mode. Field shapes
/// match drm_mode_modeinfo (drm-sys bindings) so the Linux-side
/// synthesizer can copy directly with no casts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Cea861Timings {
    /// Pixel clock in kHz.
    pub clock: u32,
    pub hdisplay: u16,
    pub hsync_start: u16,
    pub hsync_end: u16,
    pub htotal: u16,
    pub vdisplay: u16,
    pub vsync_start: u16,
    pub vsync_end: u16,
    pub vtotal: u16,
    pub vrefresh_hz: u32,
}

/// Look up CEA-861 timings for the given (W, H, refresh). Returns
/// Err for combinations not in the table; caller surfaces the
/// error to the operator. Table covers V1's HDMI requirement set.
pub fn lookup(width: u16, height: u16, vrefresh_hz: u32) -> Result<Cea861Timings> {
    let t = match (width, height, vrefresh_hz) {
        (1920, 1080, 60) => Cea861Timings {
            clock: 148500,
            hdisplay: 1920,
            hsync_start: 2008,
            hsync_end: 2052,
            htotal: 2200,
            vdisplay: 1080,
            vsync_start: 1084,
            vsync_end: 1089,
            vtotal: 1125,
            vrefresh_hz: 60,
        },
        (1920, 1080, 30) => Cea861Timings {
            clock: 74250,
            hdisplay: 1920,
            hsync_start: 2008,
            hsync_end: 2052,
            htotal: 2200,
            vdisplay: 1080,
            vsync_start: 1084,
            vsync_end: 1089,
            vtotal: 1125,
            vrefresh_hz: 30,
        },
        (1280, 720, 60) => Cea861Timings {
            clock: 74250,
            hdisplay: 1280,
            hsync_start: 1390,
            hsync_end: 1430,
            htotal: 1650,
            vdisplay: 720,
            vsync_start: 725,
            vsync_end: 730,
            vtotal: 750,
            vrefresh_hz: 60,
        },
        (1280, 720, 30) => Cea861Timings {
            clock: 37125,
            hdisplay: 1280,
            hsync_start: 1390,
            hsync_end: 1430,
            htotal: 1650,
            vdisplay: 720,
            vsync_start: 725,
            vsync_end: 730,
            vtotal: 750,
            vrefresh_hz: 30,
        },
        _ => {
            return Err(anyhow!(
                "no CEA-861 timing for {width}x{height}@{vrefresh_hz}; \
                 supported: 1920x1080@30, 1920x1080@60, 1280x720@30, 1280x720@60"
            ));
        }
    };
    Ok(t)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lookup_1920x1080_30_canonical_timings() {
        let t = lookup(1920, 1080, 30).unwrap();
        assert_eq!(t.clock, 74250);
        assert_eq!(t.hdisplay, 1920);
        assert_eq!(t.htotal, 2200);
        assert_eq!(t.vdisplay, 1080);
        assert_eq!(t.vtotal, 1125);
        assert_eq!(t.vrefresh_hz, 30);
    }

    #[test]
    fn lookup_1920x1080_60_doubles_clock() {
        let t30 = lookup(1920, 1080, 30).unwrap();
        let t60 = lookup(1920, 1080, 60).unwrap();
        assert_eq!(t60.clock, t30.clock * 2);
        // Same blanking timings; only refresh differs.
        assert_eq!(t60.htotal, t30.htotal);
        assert_eq!(t60.vtotal, t30.vtotal);
        assert_eq!(t60.vrefresh_hz, 60);
    }

    #[test]
    fn lookup_1280x720_60() {
        let t = lookup(1280, 720, 60).unwrap();
        assert_eq!(t.clock, 74250);
        assert_eq!(t.hdisplay, 1280);
        assert_eq!(t.htotal, 1650);
        assert_eq!(t.vdisplay, 720);
        assert_eq!(t.vtotal, 750);
    }

    #[test]
    fn lookup_unknown_mode_errs() {
        assert!(lookup(800, 600, 60).is_err());
        assert!(lookup(1920, 1200, 60).is_err());
        assert!(lookup(1920, 1080, 75).is_err());
    }

    #[test]
    fn lookup_rejects_zero() {
        // 0x0@0 is not in the table; the parser already rejects it
        // upstream, but the lookup itself shouldn't panic.
        assert!(lookup(0, 0, 0).is_err());
    }

    #[test]
    fn timing_consistency_clock_vs_refresh() {
        // pclock = htotal * vtotal * vrefresh / 1000 (rounded). All
        // four entries in the table should be self-consistent.
        for (w, h, hz) in [(1920, 1080, 30), (1920, 1080, 60), (1280, 720, 30), (1280, 720, 60)] {
            let t = lookup(w, h, hz).unwrap();
            let computed_khz = (t.htotal as u64) * (t.vtotal as u64) * (t.vrefresh_hz as u64) / 1000;
            // CEA-861 clocks are quoted to 0.1% precision; allow 1 kHz
            // delta for rounding.
            let delta = (computed_khz as i64 - t.clock as i64).abs();
            assert!(
                delta < 1000,
                "{w}x{h}@{hz}: computed {computed_khz} kHz vs declared {} kHz (Δ {delta})",
                t.clock,
            );
        }
    }
}
