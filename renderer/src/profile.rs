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

/// Per-phase aggregate over a sample window. All fields are
/// nanoseconds; the `_ns` suffix anchors units at the type level so
/// downstream consumers don't have to remember (or guess) what the
/// raw u64 means. Empty input maps to `PhaseStats::default()` (all
/// zeros) -- callers treat that as the "no data" sentinel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct PhaseStats {
    pub sum_ns: u64,
    pub mean_ns: u64,
    pub p50_ns: u64,
    pub p95_ns: u64,
    pub p99_ns: u64,
    pub max_ns: u64,
}

/// 0-indexed nearest-rank percentile (Wikipedia "Nearest-rank method"):
/// rank = ceil(pct/100 * n), 0-indexed = rank - 1, clamped to n-1.
///
/// Integer-safe ceil via the (a + b - 1) / b idiom. saturating_sub
/// guards the pct=0 / n=1 corner where rank would otherwise underflow.
fn nearest_rank(sorted: &[u64], pct: u64) -> u64 {
    let n = sorted.len();
    let idx = ((pct * n as u64 + 99) / 100).saturating_sub(1) as usize;
    sorted[idx.min(n - 1)]
}

/// Compute aggregate stats over a sample window. Returns ns in every
/// field. Empty input returns `PhaseStats::default()` (all zeros).
///
/// Percentiles use nearest-rank, 0-indexed. The previous formula
/// `s[(n * pct) / 100]` treated Rust's 0-based slice indexing as if
/// it were 1-based rank, inflating every percentile reading by ~1
/// position -- e.g. for sorted 1..=100, the old p99 returned the max
/// (100) instead of the 99th-smallest (99); for small windows (n=20)
/// p95 collapsed entirely into max. Downstream soak-gate decisions in
/// ipc_main.rs were reading those inflated numbers; reported soak p99
/// will be measurably lower post-fix without any underlying perf
/// change.
pub fn summarize_samples(samples: &[u64]) -> PhaseStats {
    if samples.is_empty() {
        return PhaseStats::default();
    }
    let mut s = samples.to_vec();
    s.sort_unstable();
    let n = s.len();
    let sum_ns: u64 = s.iter().sum();
    PhaseStats {
        sum_ns,
        mean_ns: sum_ns / n as u64,
        p50_ns: nearest_rank(&s, 50),
        p95_ns: nearest_rank(&s, 95),
        p99_ns: nearest_rank(&s, 99),
        max_ns: *s.last().unwrap(),
    }
}

/// Format the accumulated histogram into a stable text table.
/// One line per phase: `phase  n=N  p50=…us  p95=…us  p99=…us
/// max=…us`. Ordered by phase name (BTreeMap iteration is stable).
/// Empty state -> `"profile: disabled"`; enabled-but-no-frames-yet
/// -> `"profile: no samples (frames_remaining=N)"`. Caller does not
/// have to know storage internals.
///
/// Units: microseconds (perf budget at 30fps = 33333µs; one-µs
/// resolution matches what an operator can act on; rounding ns to µs
/// strips a digit of noise from the GC-quantized Instant readings).
pub fn dump_text() -> String {
    let s = match slot().lock() {
        Ok(s) => s,
        Err(_) => return "profile: lock poisoned".to_string(),
    };
    let p = match s.as_ref() {
        Some(p) => p,
        None => return "profile: disabled".to_string(),
    };
    if p.samples.is_empty() {
        return format!(
            "profile: no samples (frames_remaining={})",
            p.frames_remaining
        );
    }
    let mut out = String::new();
    out.push_str(&format!(
        "profile: frames_remaining={}\n",
        p.frames_remaining
    ));
    for (phase, samples) in &p.samples {
        let stats = summarize_samples(samples);
        out.push_str(&format!(
            "{phase}  n={}  p50={}us  p95={}us  p99={}us  max={}us\n",
            samples.len(),
            stats.p50_ns / 1_000,
            stats.p95_ns / 1_000,
            stats.p99_ns / 1_000,
            stats.max_ns / 1_000,
        ));
    }
    out
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
        let stats = summarize_samples(samples);
        eprintln!(
            "[profile] | {phase} | {} | {:.2} | {:.3} | {:.3} | {:.3} | {:.3} | {:.3} |",
            samples.len(),
            stats.sum_ns as f64 / 1e6,
            stats.mean_ns as f64 / 1e6,
            stats.p50_ns as f64 / 1e6,
            stats.p95_ns as f64 / 1e6,
            stats.p99_ns as f64 / 1e6,
            stats.max_ns as f64 / 1e6,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn summarize_empty_returns_zeros() {
        assert_eq!(summarize_samples(&[]), PhaseStats::default());
    }

    #[test]
    fn summarize_single_sample() {
        let stats = summarize_samples(&[1000]);
        assert_eq!(stats.sum_ns, 1000);
        assert_eq!(stats.mean_ns, 1000);
        assert_eq!(stats.p50_ns, 1000);
        assert_eq!(stats.p95_ns, 1000);
        assert_eq!(stats.p99_ns, 1000);
        assert_eq!(stats.max_ns, 1000);
    }

    #[test]
    fn summarize_sorted_percentiles() {
        // 100 samples: 1..=100. Nearest-rank, 0-indexed:
        //   p50 idx = ceil(50/100 * 100) - 1 = 50 - 1 = 49 -> s[49] = 50
        //   p95 idx = ceil(95/100 * 100) - 1 = 95 - 1 = 94 -> s[94] = 95
        //   p99 idx = ceil(99/100 * 100) - 1 = 99 - 1 = 98 -> s[98] = 99
        // (The pre-2026-05-25 buggy formula returned 51, 96, 100 here --
        // it indexed into the 0-based slice as if positions were 1-based
        // ranks, inflating every reading by one position.)
        let s: Vec<u64> = (1..=100).collect();
        let stats = summarize_samples(&s);
        assert_eq!(stats.sum_ns, 5050);
        assert_eq!(stats.mean_ns, 50);
        assert_eq!(stats.p50_ns, 50);
        assert_eq!(stats.p95_ns, 95);
        assert_eq!(stats.p99_ns, 99);
        assert_eq!(stats.max_ns, 100);
    }

    #[test]
    fn summarize_unsorted_input_sorts() {
        // Nearest-rank p50 for n=5: idx = ceil(50/100 * 5) - 1 = 3-1 = 2
        // -> sorted[2] = 3.
        let s = vec![5, 1, 3, 2, 4];
        let stats = summarize_samples(&s);
        assert_eq!(stats.p50_ns, 3);
        assert_eq!(stats.max_ns, 5);
    }

    #[test]
    fn summarize_n_eq_1_all_percentiles_collapse_to_sole_sample() {
        // Edge: nearest-rank ceil((pct*1+99)/100).saturating_sub(1) = 0
        // for every pct in [0, 99], so the lone sample IS every
        // percentile. Locks the saturating_sub guard.
        let stats = summarize_samples(&[42_000]);
        assert_eq!(stats.p50_ns, 42_000);
        assert_eq!(stats.p95_ns, 42_000);
        assert_eq!(stats.p99_ns, 42_000);
    }

    #[test]
    fn dump_text_disabled_state() {
        // Run-isolated: dump_text reads the global PROFILE state, which
        // is process-shared. Other tests don't enable/disable, so default
        // is `None`. If a future test enables without disabling, this
        // asserts the contract once at module load.
        let out = dump_text();
        // Don't assert exact string -- another test running in parallel
        // could have enable()'d. Either "disabled" or "no samples" or
        // "frames_remaining=" is acceptable as long as it's well-formed.
        assert!(
            out.starts_with("profile: ") || out.contains("p50=") || out.contains("frames_remaining="),
            "dump_text returned unexpected shape: {out:?}"
        );
    }

    #[test]
    fn summarize_n_eq_20_no_collapse_into_max() {
        // Regression-lock: the pre-fix formula s[(n*pct)/100] for n=20
        // gave p50=s[10]=11, p95=s[19]=20=max, p99=s[19]=20=max -- p95
        // and p99 collapsed into max, losing all signal. Nearest-rank
        // (this fix) keeps them distinct:
        //   p50 idx = ceil(0.50 * 20) - 1 = 10 - 1 = 9 -> s[9]  = 10
        //   p95 idx = ceil(0.95 * 20) - 1 = 19 - 1 = 18 -> s[18] = 19
        //   p99 idx = ceil(0.99 * 20) - 1 = 20 - 1 = 19 -> s[19] = 20
        // p95 and p99 are now distinct AND p95 < max.
        let s: Vec<u64> = (1..=20).collect();
        let stats = summarize_samples(&s);
        assert_eq!(stats.p50_ns, 10);
        assert_eq!(stats.p95_ns, 19);
        assert_eq!(stats.p99_ns, 20);
        assert_ne!(stats.p95_ns, stats.max_ns, "p95 must not collapse into max");
    }
}
