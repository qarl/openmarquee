//! Pixel oracle shared by the M0 / M1 V4L2 decoder probes.
//!
//! Per QA's M1 dispatch (2026-06-17): the M0 probe's `fresh=30/30`
//! proved DQBUF returned kernel-valid buffers but didn't verify the
//! pixel content — exactly the false-positive trap this project
//! has hit before (REQBUFS-ok + 0-real-frames with the Main/no-B
//! encode requirement; "valid buffers, BLACK/garbage pixels, ~0
//! errors"). The oracle here closes that gap with two cheap checks
//! per frame:
//!
//! 1. NOT all-zero / not a single constant value — catches BLACK,
//!    solid-green, or uninitialized buffers.
//! 2. Consecutive checksums DIFFER — catches stuck / duplicate
//!    frames (the "frozen incoming" signature).
//!
//! Implementation: FNV-1a hash over a STRIDED sample of the Y
//! plane bytes (cap at ~1024 samples so the per-frame cost stays
//! single-µs even on a 1080p luma plane = 2 MB). The strided sample
//! also tracks whether all sampled bytes are equal (the "single
//! constant value" detector — covers solid-black, solid-green,
//! solid-grey, anything fill-patterned).
//!
//! Shared via `#[path = "../probe_oracle.rs"]` in each probe bin so
//! M0 and M1 are guaranteed to use the SAME oracle (no drift
//! between the two probes' notion of "fresh pixels").

#![cfg(target_os = "linux")]

/// Per-frame verdict from `PixelOracle::check`.
#[derive(Debug, Clone, Copy)]
pub struct FrameVerdict {
    /// True if the sampled Y-plane bytes are all identical
    /// (all-zero / solid-anything fill pattern). The "BLACK"
    /// category in the report.
    pub all_constant: bool,
    /// True if this frame's checksum differs from the previous
    /// frame's. First frame is always considered distinct
    /// (no prior to compare against).
    pub distinct: bool,
    /// FNV-1a hash of the strided Y-plane sample. Useful for
    /// per-frame debug logging if needed.
    pub hash: u64,
}

impl FrameVerdict {
    /// True iff the frame passes BOTH checks (non-constant AND
    /// distinct from the prior). This is the per-frame
    /// pixel-ok criterion the M1 VERDICT uses.
    pub fn pixel_ok(&self) -> bool {
        !self.all_constant && self.distinct
    }
}

/// Accumulated oracle stats across a sequence of frames.
#[derive(Debug, Default)]
pub struct PixelOracle {
    prev_hash: Option<u64>,
    /// Total frames checked.
    pub total: u32,
    /// Frames where `pixel_ok()` was true.
    pub pixel_ok: u32,
    /// Frames where the strided sample was all-constant (BLACK,
    /// solid-color, or uninitialized fill pattern).
    pub black: u32,
    /// Frames where the checksum differs from the prior frame.
    /// The first frame counts as distinct (no prior).
    pub distinct: u32,
    /// Frames where the checksum matched the prior frame
    /// (stuck/duplicate signature).
    pub stuck: u32,
}

impl PixelOracle {
    pub fn new() -> Self {
        Self::default()
    }

    /// Process one frame's Y-plane bytes. Returns the per-frame
    /// verdict + updates internal counters. Idempotent across
    /// calls (you can resume an oracle mid-stream by reusing the
    /// same instance).
    pub fn check(&mut self, y_plane: &[u8]) -> FrameVerdict {
        self.total = self.total.saturating_add(1);
        let (hash, all_constant) = y_plane_signature(y_plane);
        let distinct = self.prev_hash.map_or(true, |prev| prev != hash);
        if all_constant {
            self.black = self.black.saturating_add(1);
        }
        if distinct {
            self.distinct = self.distinct.saturating_add(1);
        } else {
            self.stuck = self.stuck.saturating_add(1);
        }
        if !all_constant && distinct {
            self.pixel_ok = self.pixel_ok.saturating_add(1);
        }
        self.prev_hash = Some(hash);
        FrameVerdict { all_constant, distinct, hash }
    }

    /// Compact per-side report string for the VERDICT line.
    ///
    /// Format: `pixel_ok=NN/NN distinct=NN black=NN stuck=NN`
    pub fn report(&self, label: &str) -> String {
        format!(
            "{label}_pixel_ok={}/{} {label}_distinct={} {label}_black={} {label}_stuck={}",
            self.pixel_ok,
            self.total,
            self.distinct,
            self.black,
            self.stuck,
        )
    }
}

/// FNV-1a hash over a strided sample of `y` + whether all sampled
/// bytes are identical. Cap the sample at ~1024 bytes so the
/// per-frame cost stays single-µs even on a 1080p luma plane
/// (2 MB).
///
/// `(hash, all_same)` — `all_same == true` means the sampled bytes
/// were all the same value (BLACK / solid-fill detector). On an
/// empty `y` we return `(FNV_OFFSET, true)` so the caller treats
/// it as a fail (all_constant), which matches the intuition that
/// a zero-byte luma plane is degenerate.
pub fn y_plane_signature(y: &[u8]) -> (u64, bool) {
    const FNV_OFFSET: u64 = 14_695_981_039_346_656_037;
    const FNV_PRIME: u64 = 1_099_511_628_211;
    const MAX_SAMPLES: usize = 1024;

    if y.is_empty() {
        return (FNV_OFFSET, true);
    }

    let stride = (y.len() / MAX_SAMPLES).max(1);
    let mut hash: u64 = FNV_OFFSET;
    let mut first: Option<u8> = None;
    let mut all_same = true;
    let mut i = 0;
    while i < y.len() {
        let b = y[i];
        match first {
            None => first = Some(b),
            Some(f) if f != b => all_same = false,
            _ => {}
        }
        hash ^= b as u64;
        hash = hash.wrapping_mul(FNV_PRIME);
        i += stride;
    }
    (hash, all_same)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_zero_y_plane_is_constant() {
        let y = vec![0u8; 1920 * 1080];
        let (_hash, all_same) = y_plane_signature(&y);
        assert!(all_same);
    }

    #[test]
    fn all_green_y_plane_is_constant() {
        // Solid value 128 (mid-grey luma) is the failure mode that
        // catches "kernel returns a buffer but the codec wrote a
        // single fill pattern" — the all_constant check fires.
        let y = vec![128u8; 1920 * 1080];
        let (_hash, all_same) = y_plane_signature(&y);
        assert!(all_same);
    }

    #[test]
    fn varied_y_plane_is_not_constant() {
        let y: Vec<u8> = (0..(1920 * 1080)).map(|i| (i % 256) as u8).collect();
        let (_hash, all_same) = y_plane_signature(&y);
        assert!(!all_same);
    }

    #[test]
    fn oracle_counts_distinct_and_stuck() {
        let mut oracle = PixelOracle::new();
        let frame_a: Vec<u8> = (0..(1920 * 1080)).map(|i| (i % 256) as u8).collect();
        let frame_b: Vec<u8> = (0..(1920 * 1080)).map(|i| ((i + 1) % 256) as u8).collect();

        // Frame A: distinct (no prior), not constant.
        let v1 = oracle.check(&frame_a);
        assert!(v1.distinct);
        assert!(!v1.all_constant);

        // Same as A → stuck.
        let v2 = oracle.check(&frame_a);
        assert!(!v2.distinct);

        // B → distinct again.
        let v3 = oracle.check(&frame_b);
        assert!(v3.distinct);

        assert_eq!(oracle.total, 3);
        assert_eq!(oracle.distinct, 2);
        assert_eq!(oracle.stuck, 1);
        assert_eq!(oracle.black, 0);
    }

    #[test]
    fn oracle_counts_black() {
        let mut oracle = PixelOracle::new();
        let y_zero = vec![0u8; 1920 * 1080];
        let v = oracle.check(&y_zero);
        assert!(v.all_constant);
        assert_eq!(oracle.black, 1);
    }
}
