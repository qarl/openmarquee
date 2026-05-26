//! Frame-pacing utilities for the renderer present loop.
//!
//! `[perf]` r1 (2026-05-26): pure-data predicate for "did this
//! frame miss the 30fps deadline". Lives outside hdmi.rs /
//! hdmi_logic.rs so the predicate is host-testable on every
//! platform (those files are Linux-only). The 36ms threshold =
//! 33.33ms (30fps) + ~3ms grace, mirroring the QA dispatch's
//! hard rule and the frame-pacing comments at hdmi_logic.rs:4223.
//!
//! `commit_fb` (hdmi.rs:966) is the single chokepoint every
//! painted frame passes through. After each commit it stamps
//! `EglSession::last_present_at` and consults `over_budget_ms`
//! to decide whether to bump `frames_over_budget_total` and
//! emit a rate-limited `[perf]` warn log. Steady-state cost is
//! one Instant subtract + one millisecond divide + one compare
//! — sub-µs per frame on the Pi Zero 2 W.
//!
//! Complementary to `profile.rs`'s per-phase histogram (which
//! answers "where is the time going within a frame"); this
//! module answers "how often are we over the per-frame budget".

/// 30fps target = 33.33ms per frame. Add ~3ms grace → 36ms is
/// the over-budget threshold. Strict `>` comparison: 36ms
/// exactly is at-budget, not over.
///
/// `#[allow(dead_code)]` because the production consumer
/// (`EglSession::record_present` in hdmi.rs) lives behind
/// `#[cfg(target_os = "linux")]`. On macOS the constant is
/// reachable only through the test module — `cargo check` (which
/// compiles production only) sees it as unused. Same precedent
/// as `IpcPaintMetrics` at ipc_main.rs:127.
#[allow(dead_code)]
pub const FRAME_BUDGET_MS: u64 = 36;

/// Returns `Some(delta_ms)` if `now - prev > budget_ms`, else `None`.
///
/// Pure data; no allocations; single subtract + millisecond
/// conversion + compare. `now` ≥ `prev` is enforced via
/// `saturating_duration_since` so a clock anomaly (clamped to
/// zero) cannot return a spurious `Some`.
///
/// See `FRAME_BUDGET_MS` for the `#[allow(dead_code)]` rationale.
#[allow(dead_code)]
#[inline]
pub fn over_budget_ms(
    prev: std::time::Instant,
    now: std::time::Instant,
    budget_ms: u64,
) -> Option<u64> {
    let delta_ms = now.saturating_duration_since(prev).as_millis() as u64;
    if delta_ms > budget_ms {
        Some(delta_ms)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    #[test]
    fn zero_delta_is_under_budget() {
        let t0 = Instant::now();
        assert_eq!(over_budget_ms(t0, t0, FRAME_BUDGET_MS), None);
    }

    #[test]
    fn delta_35ms_under_budget_returns_none() {
        let t0 = Instant::now();
        let t1 = t0 + Duration::from_millis(35);
        assert_eq!(over_budget_ms(t0, t1, FRAME_BUDGET_MS), None);
    }

    #[test]
    fn delta_36ms_at_budget_returns_none() {
        // Strict `>` boundary: exactly 36ms is at-budget, not over.
        let t0 = Instant::now();
        let t1 = t0 + Duration::from_millis(36);
        assert_eq!(over_budget_ms(t0, t1, FRAME_BUDGET_MS), None);
    }

    #[test]
    fn delta_37ms_one_over_returns_some_37() {
        let t0 = Instant::now();
        let t1 = t0 + Duration::from_millis(37);
        assert_eq!(over_budget_ms(t0, t1, FRAME_BUDGET_MS), Some(37));
    }

    #[test]
    fn delta_100ms_far_over_returns_some_100() {
        let t0 = Instant::now();
        let t1 = t0 + Duration::from_millis(100);
        assert_eq!(over_budget_ms(t0, t1, FRAME_BUDGET_MS), Some(100));
    }

    #[test]
    fn now_before_prev_clamps_to_zero_returns_none() {
        // saturating_duration_since clamps a negative delta to 0;
        // 0 is not > budget so the result is None. Defends against
        // a clock anomaly on consecutive Instant reads (shouldn't
        // happen on Linux monotonic clock, but the predicate stays
        // sound regardless).
        let t1 = Instant::now();
        let t0 = t1 + Duration::from_millis(50);
        assert_eq!(over_budget_ms(t0, t1, FRAME_BUDGET_MS), None);
    }

    #[test]
    fn custom_budget_ms_respected() {
        // Predicate doesn't hardcode FRAME_BUDGET_MS; a 10ms-budget
        // caller would see Some at 20ms, None at 5ms. Keeps the
        // predicate testable independent of the production constant.
        let t0 = Instant::now();
        let t_short = t0 + Duration::from_millis(5);
        let t_long = t0 + Duration::from_millis(20);
        assert_eq!(over_budget_ms(t0, t_short, 10), None);
        assert_eq!(over_budget_ms(t0, t_long, 10), Some(20));
    }
}
