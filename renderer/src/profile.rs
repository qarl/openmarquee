//! v1-spec-delta perf-profile (qarl-direct 2026-05-08): per-frame
//! phase timing for the per-frame render hot path. Disabled by
//! default; enabled by `--profile-frames N` CLI flag (caps frame
//! count and dumps a histogram on exit).
//!
//! Cross-platform pure-data module so the histogram math is host-
//! testable. Time source is `std::time::Instant` which uses
//! CLOCK_MONOTONIC on Linux (sub-µs resolution).
//!
//! Threading: single global Mutex protects the sample storage.
//! The renderer is single-threaded at this point so contention is
//! a non-issue.

use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};

/// Per-phase nanosecond samples. BTreeMap so summary output is
/// stable-ordered by phase name regardless of insertion order.
type SampleStore = BTreeMap<&'static str, Vec<u64>>;

/// Number of frames left to capture. None = disabled. Some(0) =
/// budget exhausted, stop recording (the loop driver checks this).
struct ProfileState {
    samples: SampleStore,
    frames_remaining: u32,
}

static PROFILE: OnceLock<Mutex<Option<ProfileState>>> = OnceLock::new();

fn slot() -> &'static Mutex<Option<ProfileState>> {
    PROFILE.get_or_init(|| Mutex::new(None))
}

/// Enable profiling with a frame budget. Subsequent calls to
/// `record_phase` accumulate samples; `frame_complete` decrements
/// the budget; `summarize` prints the histogram.
pub fn enable(frames: u32) {
    let mut s = slot().lock().unwrap();
    *s = Some(ProfileState {
        samples: BTreeMap::new(),
        frames_remaining: frames,
    });
}

pub fn is_enabled() -> bool {
    slot().lock().map(|s| s.is_some()).unwrap_or(false)
}

/// Frames remaining in the budget. 0 = stop. None = disabled.
pub fn frames_remaining() -> Option<u32> {
    slot().lock().ok()?.as_ref().map(|p| p.frames_remaining)
}

/// Record a phase sample (nanoseconds). No-op when disabled or
/// budget exhausted -- caller does not need to gate.
pub fn record_phase(phase: &'static str, ns: u64) {
    if let Ok(mut s) = slot().lock() {
        if let Some(p) = s.as_mut() {
            if p.frames_remaining > 0 {
                p.samples.entry(phase).or_default().push(ns);
            }
        }
    }
}

/// Decrement frame budget. Caller invokes at end of each captured
/// frame. When budget hits 0, subsequent record_phase calls drop.
pub fn frame_complete() {
    if let Ok(mut s) = slot().lock() {
        if let Some(p) = s.as_mut() {
            if p.frames_remaining > 0 {
                p.frames_remaining -= 1;
            }
        }
    }
}

/// Compute (sum, mean, p50, p95, p99, max) for a sorted sample list.
/// Returns ns. Empty list returns all zeros.
pub fn summarize_samples(samples: &[u64]) -> (u64, u64, u64, u64, u64, u64) {
    if samples.is_empty() {
        return (0, 0, 0, 0, 0, 0);
    }
    let mut s = samples.to_vec();
    s.sort_unstable();
    let n = s.len();
    let sum: u64 = s.iter().sum();
    let mean = sum / n as u64;
    let p50 = s[n / 2];
    let p95 = s[(n * 95) / 100];
    let p99 = s[((n * 99) / 100).min(n - 1)];
    let max = *s.last().unwrap();
    (sum, mean, p50, p95, p99, max)
}

/// Dump the accumulated histogram to stderr in markdown table form.
/// Idempotent: subsequent calls with no new samples re-print the
/// same table. Caller ALSO clears via `disable` if a fresh capture
/// is needed.
pub fn summarize() {
    let s = match slot().lock() {
        Ok(s) => s,
        Err(_) => return,
    };
    let p = match s.as_ref() {
        Some(p) => p,
        None => return,
    };
    eprintln!("[profile] phase histogram (all values in ms):");
    eprintln!("[profile] | phase | n | sum | mean | p50 | p95 | p99 | max |");
    eprintln!("[profile] |-------|---|-----|------|-----|-----|-----|-----|");
    for (phase, samples) in &p.samples {
        let (sum, mean, p50, p95, p99, max) = summarize_samples(samples);
        eprintln!(
            "[profile] | {phase} | {} | {:.2} | {:.3} | {:.3} | {:.3} | {:.3} | {:.3} |",
            samples.len(),
            sum as f64 / 1e6,
            mean as f64 / 1e6,
            p50 as f64 / 1e6,
            p95 as f64 / 1e6,
            p99 as f64 / 1e6,
            max as f64 / 1e6,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summarize_empty_returns_zeros() {
        assert_eq!(summarize_samples(&[]), (0, 0, 0, 0, 0, 0));
    }

    #[test]
    fn summarize_single_sample() {
        let (sum, mean, p50, p95, p99, max) = summarize_samples(&[1000]);
        assert_eq!(sum, 1000);
        assert_eq!(mean, 1000);
        assert_eq!(p50, 1000);
        assert_eq!(p95, 1000);
        assert_eq!(p99, 1000);
        assert_eq!(max, 1000);
    }

    #[test]
    fn summarize_sorted_percentiles() {
        // 100 samples: 1..=100. p50 = 50, p95 = 95, p99 = 99,
        // max = 100, mean = 50, sum = 5050.
        let s: Vec<u64> = (1..=100).collect();
        let (sum, mean, p50, p95, p99, max) = summarize_samples(&s);
        assert_eq!(sum, 5050);
        assert_eq!(mean, 50);
        assert_eq!(p50, 51); // index 50 in 0-indexed
        assert_eq!(p95, 96);
        assert_eq!(p99, 100); // index 99
        assert_eq!(max, 100);
    }

    #[test]
    fn summarize_unsorted_input_sorts() {
        let s = vec![5, 1, 3, 2, 4];
        let (_, _, p50, _, _, max) = summarize_samples(&s);
        assert_eq!(p50, 3);
        assert_eq!(max, 5);
    }
}
