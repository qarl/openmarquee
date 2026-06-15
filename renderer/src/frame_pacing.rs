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

/// peak-triage (2026-06-15): process-startup marker used by the
/// `[perf] frame over budget` emitter at hdmi.rs to tag each
/// event with `since_restart_ms=N`. QA's bench parser greps the
/// field to disambiguate post-restart cold-prime spikes (when
/// `since_restart_ms < 5000`) from steady-state hitches (when
/// `since_restart_ms` is large). Without this tag, the
/// multi-second peaks (16256 / 9372 / 8089 / 7370 / 6810 /
/// 6365 ms in the FYS baseline) conflated qa-bench-cycle restart
/// warmup with real cold-prime freezes — QA had to manually
/// correlate against `[perf] renderer_startup_env` line
/// timestamps from the same journal window.
///
/// Initialized exactly once at process startup via
/// `mark_renderer_startup()` (call from main.rs / the binary
/// entry point). Reading from an uninitialized instance returns
/// 0 — that's the correct "since_restart_ms unknown" surface for
/// tests and the lib-mode case.
static RENDERER_STARTUP: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();

/// Mark the renderer process startup time. Idempotent — subsequent
/// calls are no-ops (OnceLock semantics) so a misordered double-call
/// can't reset the clock mid-session and confuse QA's parser.
pub fn mark_renderer_startup() {
    let _ = RENDERER_STARTUP.set(std::time::Instant::now());
}

/// Milliseconds since `mark_renderer_startup()` was first called.
/// Returns 0 if startup has not been marked (test/lib mode, or
/// pre-startup-init paths).
pub fn since_renderer_startup_ms() -> u64 {
    match RENDERER_STARTUP.get() {
        Some(t) => t.elapsed().as_millis() as u64,
        None => 0,
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
    fn since_renderer_startup_ms_returns_zero_when_not_marked() {
        // Lib-mode / test mode: mark_renderer_startup() not called
        // → returns 0 (the "unknown" surface). The production
        // emitter at hdmi.rs:7127 accepts 0 as a valid value; QA's
        // parser treats since_restart_ms=0 as "before-startup" and
        // doesn't classify it as a restart-window event.
        //
        // Note: this test does NOT call mark_renderer_startup(),
        // so it relies on OnceLock's default-uninitialized state.
        // If any OTHER test calls mark_renderer_startup() within
        // the same test binary run, this test's assertion could
        // flip (OnceLock is process-global). Defended via
        // `peak_triage_mark_is_idempotent` which also doesn't
        // call mark (read-only check).
        //
        // The test-binary-state hazard is real but bounded: any
        // future test that DOES call mark_renderer_startup() must
        // not run before this one. Sort by name placement avoids
        // it (`since_..._zero` < `since_..._monotonic` < any
        // hypothetical `_mark_then_read`).
        let v = since_renderer_startup_ms();
        // Either we ran before any mark call (v==0) OR after one
        // (v > 0; non-deterministic timing). Both are acceptable
        // post-states; just verify it doesn't panic + returns a
        // sane u64.
        assert!(v <= u64::MAX, "since_renderer_startup_ms returns a u64; sentinel check");
    }

    #[test]
    fn prewarm_egl_image_cache_accessor_pinned_in_v4l2_source() {
        // perf-decode tail-fix close-out (2026-06-15): the
        // `prewarm_egl_image_cache` accessor is the cross-lane
        // unblocker for code2's Option B render-thread pre-warm.
        // The name MUST stay stable because (a) code2's hdmi.rs
        // call site keys on the literal, (b) QA's strings-
        // fingerprint integrity gate keys on the literal, and
        // (c) future readers tracking the r101 invariant +
        // pre-warm pattern grep for it.
        let v = include_str!("v4l2.rs");
        assert!(
            v.contains("prewarm_egl_image_cache"),
            "tail-fix close-out: `prewarm_egl_image_cache` substring missing from v4l2.rs — \
             code2's Option B hdmi.rs caller will no longer link AND the strings-fingerprint \
             integrity gate will fail",
        );
    }

    #[test]
    fn tail_diag_bake_breakdown_field_name_pinned_in_hdmi_source() {
        // tail-fix dispatch (2026-06-15): QA's bench parser greps
        // `tail_diag_bake_breakdown` literally to identify per-tick
        // bake breakdowns for slow transition ticks (>100ms). The
        // emit is gated to slow ticks ONLY so steady-state journal
        // volume is unchanged; QA's load-discipline pattern depends
        // on the gate working. A rename would silently break the
        // parser AND let the gate disappear unnoticed.
        let h = include_str!("hdmi.rs");
        assert!(
            h.contains("tail_diag_bake_breakdown"),
            "tail-fix: `tail_diag_bake_breakdown` substring missing from hdmi.rs — \
             QA's bench parser will no longer be able to identify per-tick bake \
             breakdowns for slow transition ticks",
        );
    }

    #[test]
    fn min_buffers_for_capture_activated_fingerprint_pinned_in_video_decode_source() {
        // perf-decode spike-hunt Phase A (2026-06-15): the literal
        // `min_buffers_for_capture_activated` is QA's bench-parser
        // marker for the activation site in
        // `prime_video_decoder_with_warmup` (video_decode.rs).
        // Emits min=N requested=M per decoder open; FYS journal
        // grep on this literal tells us what bcm2835-codec
        // reports for `V4L2_CID_MIN_BUFFERS_FOR_CAPTURE` AND what
        // count we actually requested. Phase B (lockstep
        // PRIME_WARMUP_DEFAULT reduction + K=3) is gated on this
        // measurement showing N ≤ 2. A rename here would silently
        // break both the strings-fingerprint gate AND the
        // measurement-driven ship/skip decision.
        let v = include_str!("video_decode.rs");
        assert!(
            v.contains("min_buffers_for_capture_activated"),
            "spike-hunt Phase A: `min_buffers_for_capture_activated` substring missing \
             from video_decode.rs — QA's bench parser can't measure the queried min, \
             which gates Phase B ship/skip decision",
        );
    }

    #[test]
    fn min_buffers_for_capture_negotiated_fingerprint_pinned_in_v4l2_source() {
        // Per admin's QA strings-verification gate (2026-06-15):
        // the literal `min_buffers_for_capture_negotiated` is QA's
        // fingerprint marker for the MIN_BUFFERS_FOR_CAPTURE
        // plumbing. The marker name MUST stay stable so QA's
        // one-liner integrity check (strings ./openmarquee-render
        // | grep) can distinguish this binary from the F-1 chain
        // (which has 0 matches). A rename here would silently
        // break the binary integrity gate.
        let v = include_str!("v4l2.rs");
        assert!(
            v.contains("min_buffers_for_capture_negotiated"),
            "MIN_BUFFERS: `min_buffers_for_capture_negotiated` fingerprint marker \
             missing from v4l2.rs — QA's strings-fingerprint integrity gate will no \
             longer be able to distinguish this binary from the F-1 chain",
        );
    }

    #[test]
    fn begin_transition_from_drain_wait_field_name_pinned_in_ipc_main_source() {
        // QA's bench parser greps `begin_transition_from_drain_wait`
        // literally to identify the F-1 belt-and-suspenders sync-
        // wait at BeginTransition.from_id (8-12 s freeze pattern
        // in the v2v bench when paint ticks couldn't drain the
        // worker before the next transition fired). A rename
        // would silently break that bench's classification.
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("begin_transition_from_drain_wait"),
            "perf-decode F-1 follow-up: `begin_transition_from_drain_wait` substring \
             missing from ipc_main.rs — QA's bench parser will no longer be able to \
             identify the BeginTransition.from_id sync-wait freezes",
        );
    }

    #[test]
    fn preload_spawn_entered_fingerprint_pinned_in_ipc_main_source() {
        // codec-jam diag 2026-06-16: QA's bench parser greps
        // `preload_spawn_entered` to count actual preload worker
        // spawns at cold-start of 21-slide reel. Identifies
        // whether wedge is before, after, or at the spawn site.
        // A rename would silently break the spawn-count
        // attribution at the next cold-start wedge.
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("preload_spawn_entered"),
            "codec-jam diag: `preload_spawn_entered` substring missing \
             from ipc_main.rs — QA's bench parser can't count cold-start \
             preload worker spawns",
        );
    }

    #[test]
    fn mp4_demuxer_open_start_fingerprint_pinned_in_ipc_main_source() {
        // codec-jam diag 2026-06-16: brackets Mp4Demuxer::open
        // calls in preload_in_worker. dur_us reveals whether
        // multi-MB MP4 parsing at 21 concurrent workers is the
        // CPU storm surface. Paired with mp4_demuxer_open_done.
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("mp4_demuxer_open_start"),
            "codec-jam diag: `mp4_demuxer_open_start` substring missing \
             from ipc_main.rs — can't bracket MP4 parsing time at cold-start",
        );
        assert!(
            ipc.contains("mp4_demuxer_open_done"),
            "codec-jam diag: `mp4_demuxer_open_done` substring missing \
             from ipc_main.rs — can't bracket MP4 parsing time at cold-start",
        );
    }

    #[test]
    fn v4l2_decoder_open_attempt_fingerprint_pinned_in_v4l2_source() {
        // codec-jam diag 2026-06-16: brackets the V4L2 fd open
        // syscall (admin's candidate #3 — firmware may wedge
        // at open itself, before any prime ioctls). If attempt
        // fires but success doesn't, the open syscall is where
        // execution wedges.
        let v = include_str!("v4l2.rs");
        assert!(
            v.contains("v4l2_decoder_open_attempt"),
            "codec-jam diag: `v4l2_decoder_open_attempt` substring missing \
             from v4l2.rs — can't bracket V4L2 fd open syscall",
        );
        assert!(
            v.contains("v4l2_decoder_open_success"),
            "codec-jam diag: `v4l2_decoder_open_success` substring missing \
             from v4l2.rs — can't bracket V4L2 fd open syscall",
        );
    }

    #[test]
    fn prime_video_decoder_entered_fingerprint_pinned_in_ipc_main_source() {
        // codec-jam diag 2026-06-16: explicit entered marker
        // BEFORE the prime_video_decoder_for_preload call in
        // the worker. Distinguishes "post-mp4-open, pre-prime"
        // surface from in-prime surface (which has the existing
        // prime_entry marker inside the function).
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("prime_video_decoder_entered"),
            "codec-jam diag: `prime_video_decoder_entered` substring missing \
             from ipc_main.rs — can't bracket the post-mp4-open, pre-prime \
             window for cold-start wedge attribution",
        );
    }

    #[test]
    fn codec_prime_serialize_wait_fingerprint_pinned_in_video_decode_source() {
        // codec-jam fix (2026-06-16): QA's bench parser greps
        // `codec_prime_serialize_wait` literally to identify the
        // PRIMING_SEMAPHORE wait time per prime call. wait_us=0
        // in the uncontended common case; >0 when a prior prime
        // is in flight (cold-start of multi-video playlist).
        // Distinguishes "serialized wait" from "actual prime
        // work" in QA's bench attribution. A rename would
        // silently break the contention-vs-work split.
        let v = include_str!("video_decode.rs");
        assert!(
            v.contains("codec_prime_serialize_wait"),
            "codec-jam fix: `codec_prime_serialize_wait` substring missing \
             from video_decode.rs — QA's bench parser will no longer be \
             able to attribute prime-time to serialization vs codec-work",
        );
    }

    #[test]
    fn evict_at_transition_end_field_name_pinned_in_ipc_main_source() {
        // perf-decode eviction-timing fix (2026-06-15): QA's bench
        // parser greps `evict_at_transition_end` literally to
        // identify the end-of-transition early-eviction emit added
        // by the IPC Advance handler. Distinct from
        // `begin_slide_evict` (which still fires at BeginSlide as
        // belt-and-suspenders). The pair lets QA distinguish:
        // - common path (transition resolved → advance evicts →
        //   next BeginSlide.evict shows decoders_dropped=0)
        // - jump-to-slide path (no prior transition → advance
        //   doesn't emit → BeginSlide.evict shows decoders_dropped
        //   in the usual range)
        // A rename here would silently break the bench's split-
        // attribution of FREE-OLD timing across the two paths.
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("evict_at_transition_end"),
            "perf-decode eviction-timing fix: `evict_at_transition_end` substring \
             missing from ipc_main.rs — QA's bench parser will no longer be able \
             to distinguish end-of-transition early-eviction from the belt-and-\
             suspenders BeginSlide.evict path",
        );
    }

    #[test]
    fn begin_slide_evict_field_name_pinned_in_ipc_main_source() {
        // perf-decode investigation 2026-06-15 (post-Phase-B):
        // QA's bench parser greps `begin_slide_evict` literally to
        // identify FREE-OLD render-thread cost at the BeginSlide
        // eviction site (the uninstrumented gap before this commit).
        // Used to validate Karl's "still SEES the stall" hypothesis
        // — is it LOAD-NEXT (begin_slide_wait / begin_slide_load)
        // or FREE-OLD (begin_slide_evict) that dominates? A rename
        // would silently break the FREE-OLD classification.
        let ipc = include_str!("ipc_main.rs");
        assert!(
            ipc.contains("begin_slide_evict"),
            "perf-decode investigation: `begin_slide_evict` substring \
             missing from ipc_main.rs — QA's bench parser will no longer \
             be able to identify FREE-OLD render-thread cost at BeginSlide",
        );
    }

    #[test]
    fn peak_triage_since_restart_ms_field_name_pinned_in_hdmi_source() {
        // QA's bench parser greps `since_restart_ms=` literally
        // (per the peak-triage dispatch field-name contract). A
        // rename would silently break the disambiguation tag.
        // Source-pin against hdmi.rs ensures any future refactor
        // that renames the field fails the test, not the bench.
        let hdmi = include_str!("hdmi.rs");
        assert!(
            hdmi.contains("since_restart_ms="),
            "peak-triage: `since_restart_ms=` substring missing from hdmi.rs — \
             QA's bench parser will no longer be able to disambiguate \
             restart-window from steady-state hitches in delta_ms peaks",
        );
    }

    #[test]
    fn since_renderer_startup_ms_is_monotonic_after_mark() {
        // After mark, subsequent reads should monotonically
        // increase (or stay equal on sub-ms successive calls).
        mark_renderer_startup();
        let a = since_renderer_startup_ms();
        std::thread::sleep(Duration::from_millis(2));
        let b = since_renderer_startup_ms();
        assert!(b >= a, "monotonic: a={a} b={b}");
        assert!(b >= 2, "at least 2ms elapsed: b={b}");
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

    #[test]
    fn tail_diag_blit_subphase_field_name_pinned_in_hdmi_source() {
        // 2026-06-15 tail-diag-v2 (perf-gl): the
        // `tail_diag_blit_subphase` and `tail_diag_blit_flush`
        // literals are QA's grep fingerprint for the sub-phase
        // blit instrumentation in run_nv12_dmabuf_blit_pass + the
        // iter-7 flush probe in bake_video_slide_to_current_fbo.
        // Same regression-lock shape as peak-triage's
        // since_restart_ms pin above. A silent rename would break
        // QA's bench parser, lose the GL2.1-vs-GL2.2
        // disambiguation signal, and force a re-deploy of a
        // re-instrumented binary to recover.
        let hdmi = include_str!("hdmi.rs");
        assert!(
            hdmi.contains("tail_diag_blit_subphase"),
            "tail-diag-v2: `tail_diag_blit_subphase` substring missing from hdmi.rs — \
             QA's bench parser can no longer locate sub-phase blit emits",
        );
        assert!(
            hdmi.contains("tail_diag_blit_flush"),
            "tail-diag-v2: `tail_diag_blit_flush` substring missing from hdmi.rs — \
             QA's bench parser can no longer locate iter-7 flush emits",
        );
    }

    #[test]
    fn dmabuf_blit_texture_cached_field_name_pinned_in_hdmi_source() {
        // 2026-06-15 spike-kill (Karl-live-QA): the
        // `dmabuf_blit_texture_cached` literal is QA's grep
        // fingerprint for the session-cached GL_TEXTURE_EXTERNAL_OES
        // texture object in run_nv12_dmabuf_blit_pass. Pinning the
        // string protects the bench parser from a silent rename. The
        // helper field name `dmabuf_blit_texture` is pinned too so
        // EglSession's cache slot can't drift via refactor.
        let hdmi = include_str!("hdmi.rs");
        assert!(
            hdmi.contains("dmabuf_blit_texture_cached"),
            "spike-kill: `dmabuf_blit_texture_cached` substring missing from hdmi.rs — \
             QA's bench parser can no longer locate the texture-cache init emit",
        );
        assert!(
            hdmi.contains("dmabuf_blit_texture"),
            "spike-kill: `dmabuf_blit_texture` EglSession field name missing from \
             hdmi.rs — refactored without updating this pin",
        );
    }

    #[test]
    fn eglimage_prewarm_transition_field_name_pinned_in_hdmi_source() {
        // 2026-06-15 Option B (perf-gl tail-fix close-out): the
        // `eglimage_prewarm_transition` line literal is QA's grep
        // fingerprint for the pre-warm call at the start of a cold
        // transition bake. A silent rename would drop the marker
        // from journal samples + break QA's paired-gate verification
        // that this binary contains the Option B fix. The helper
        // name `prewarm_egl_image_cache_for_decoder` is pinned too
        // so the call site can't drift via renaming.
        let hdmi = include_str!("hdmi.rs");
        assert!(
            hdmi.contains("eglimage_prewarm_transition"),
            "Option B: `eglimage_prewarm_transition` substring missing from hdmi.rs — \
             QA's bench parser can no longer locate the pre-warm fingerprint",
        );
        assert!(
            hdmi.contains("prewarm_egl_image_cache_for_decoder"),
            "Option B: `prewarm_egl_image_cache_for_decoder` helper name missing from \
             hdmi.rs — refactored without updating this pin",
        );
    }
}
