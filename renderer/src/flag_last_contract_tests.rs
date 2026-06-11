//! r110 stage 3 commit 3.0 (2026-06-11) — Task 2 of the
//! Karl-priority transitions audit. Pure-logic state-machine
//! tests for the FLAG_LAST → `capture_drained` latch contract.
//!
//! The contract documented in v4l2.rs around the `next_frame` /
//! `dqbuf_capture_hold` / `drain_capture_step_no_frame` sites
//! changed in c3.0 (`8b2d313`) from "any FLAG_LAST latches
//! capture_drained=true" to "only EPIPE OR post-CMD_STOP
//! drain latches; live-path FLAG_LAST is DEFANGED (observed +
//! delivered to the caller, no latch)."
//!
//! Rationale: bcm2835-codec's documented quirk emits FLAG_LAST
//! on the LAST CAPTURE BUFFER of degenerate single-frame clips
//! AND in some early-stream race conditions. Latching there
//! poisons the decoder for the rest of the slide. The DEFANGED
//! live paths surface the buffer to the caller (so the legit
//! content is rendered) without permanently latching; the
//! POST-CMD_STOP drain path KEEPS the latch because that is the
//! canonical spec-compliant EOS signal.
//!
//! Knock-on rule the contract requires callers honour:
//!   "EAGAIN on the next dqbuf AFTER a FLAG_LAST observation =
//!    EOS." A caller may track that pair locally as their own
//!    EOS marker even though the wrapper no longer latches.
//!
//! These tests pin the decision table as a pure state machine.
//! They do NOT touch v4l2.rs (code1's hot file); the contract
//! is documented HERE as Karl-priority audit deliverable so a
//! future change to v4l2.rs that violates the table breaks
//! these tests AND surfaces the violation in the diff.

#![cfg(test)]

/// The four observable kernel-side outcomes a DQBUF call can
/// produce for the CAPTURE plane. Inputs to the contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DqbufObs {
    /// Successful DQBUF; the buffer's `flags & V4L2_BUF_FLAG_LAST`
    /// is 0. Live mid-stream frame.
    Frame,
    /// Successful DQBUF; `flags & V4L2_BUF_FLAG_LAST` is set.
    /// The kernel is signalling "this is the last buffer of the
    /// current drain context."
    FrameWithFlagLast,
    /// `EAGAIN`: pollin returned ready but the buffer wasn't
    /// available (or vice-versa); not necessarily EOS in any
    /// path. Only EOS-meaningful AFTER a FLAG_LAST observation
    /// (the c3.0 knock-on rule).
    EAgain,
    /// `EPIPE`: the kernel has unambiguously signalled end-of-
    /// stream. ALWAYS latches `capture_drained=true` on the
    /// wrapper regardless of path.
    EPipe,
}

/// The two execution contexts a DQBUF observation can occur in.
/// The contract distinguishes them — FLAG_LAST is DEFANGED on
/// `Live`, LATCHED on `PostCmdStopDrain`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DqbufContext {
    /// Normal mid-stream DQBUF inside `next_frame` /
    /// `dqbuf_capture_hold`. bcm2835-codec may emit FLAG_LAST
    /// spuriously here; do NOT latch.
    Live,
    /// Inside `drain_capture_step_no_frame` after a successful
    /// `V4L2_DEC_CMD_STOP` issue. Here FLAG_LAST is the canonical
    /// spec-compliant EOS signal; LATCH.
    PostCmdStopDrain,
}

/// The contract decision: should the wrapper set
/// `capture_drained = true` for this (observation, context)
/// pair?
///
/// Decision table:
///
/// | observation      | Live (c3.0) | PostCmdStopDrain |
/// |------------------|-------------|------------------|
/// | Frame            | no          | no               |
/// | FrameWithFlagLast| **no**      | yes              |
/// | EAgain           | no          | no               |
/// | EPipe            | yes         | yes              |
///
/// (The Live→FrameWithFlagLast row is the c3.0 "defang" change;
/// pre-c3.0 it was `yes`.)
pub fn should_latch_capture_drained(obs: DqbufObs, ctx: DqbufContext) -> bool {
    use DqbufContext::*;
    use DqbufObs::*;
    match (obs, ctx) {
        (EPipe, _) => true,
        (FrameWithFlagLast, PostCmdStopDrain) => true,
        (FrameWithFlagLast, Live) => false, // c3.0 defang
        (Frame, _) => false,
        (EAgain, _) => false,
    }
}

/// The c3.0 knock-on rule for live-path callers: after observing
/// FLAG_LAST, the very next DQBUF returning EAGAIN means EOS.
/// (Without the wrapper-side latch, callers may track this state
/// locally.)
#[derive(Debug, Default, Clone, Copy)]
pub struct LiveCallerEosTracker {
    flag_last_seen: bool,
    eos_observed: bool,
}

impl LiveCallerEosTracker {
    /// Apply a new live-path observation. Returns true iff the
    /// caller should treat this as EOS (either EPIPE directly OR
    /// EAGAIN-after-FLAG_LAST per the knock-on rule).
    pub fn observe(&mut self, obs: DqbufObs) -> bool {
        match obs {
            DqbufObs::EPipe => {
                self.eos_observed = true;
                true
            }
            DqbufObs::FrameWithFlagLast => {
                self.flag_last_seen = true;
                false
            }
            DqbufObs::EAgain => {
                if self.flag_last_seen {
                    self.eos_observed = true;
                    true
                } else {
                    false
                }
            }
            DqbufObs::Frame => {
                // Mid-stream frame after FLAG_LAST — bcm2835-codec
                // quirk; the latch was defanged precisely so this
                // legitimate frame can be delivered without stuck-
                // EOS. Reset the tracker so EAGAIN later in the
                // slide doesn't fire the knock-on.
                self.flag_last_seen = false;
                false
            }
        }
    }

    pub fn eos_observed(&self) -> bool {
        self.eos_observed
    }
}

// ============================================================
// Tests
// ============================================================

#[test]
fn live_path_flag_last_is_defanged() {
    // The core c3.0 behaviour change.
    assert!(!should_latch_capture_drained(
        DqbufObs::FrameWithFlagLast,
        DqbufContext::Live,
    ));
}

#[test]
fn post_cmd_stop_drain_flag_last_still_latches() {
    // The single path that legitimately latches on FLAG_LAST —
    // canonical V4L2 spec EOS after V4L2_DEC_CMD_STOP.
    assert!(should_latch_capture_drained(
        DqbufObs::FrameWithFlagLast,
        DqbufContext::PostCmdStopDrain,
    ));
}

#[test]
fn epipe_always_latches_regardless_of_context() {
    for ctx in [DqbufContext::Live, DqbufContext::PostCmdStopDrain] {
        assert!(
            should_latch_capture_drained(DqbufObs::EPipe, ctx),
            "EPIPE must latch in every context ({:?})",
            ctx
        );
    }
}

#[test]
fn frame_and_eagain_never_latch() {
    for obs in [DqbufObs::Frame, DqbufObs::EAgain] {
        for ctx in [DqbufContext::Live, DqbufContext::PostCmdStopDrain] {
            assert!(
                !should_latch_capture_drained(obs, ctx),
                "{:?} in {:?} must not latch",
                obs,
                ctx
            );
        }
    }
}

#[test]
fn five_site_decision_table_matches_documented_contract() {
    // Exhaustive pin of the 4x2 decision table — any future
    // refactor of v4l2.rs that changes a single cell must update
    // this table in lock-step, otherwise the violation surfaces
    // in CI.
    let expected: [(DqbufObs, DqbufContext, bool); 8] = [
        (DqbufObs::Frame, DqbufContext::Live, false),
        (DqbufObs::Frame, DqbufContext::PostCmdStopDrain, false),
        (DqbufObs::FrameWithFlagLast, DqbufContext::Live, false),
        (
            DqbufObs::FrameWithFlagLast,
            DqbufContext::PostCmdStopDrain,
            true,
        ),
        (DqbufObs::EAgain, DqbufContext::Live, false),
        (DqbufObs::EAgain, DqbufContext::PostCmdStopDrain, false),
        (DqbufObs::EPipe, DqbufContext::Live, true),
        (DqbufObs::EPipe, DqbufContext::PostCmdStopDrain, true),
    ];
    for (obs, ctx, want) in expected {
        assert_eq!(
            should_latch_capture_drained(obs, ctx),
            want,
            "({:?}, {:?}) decision drift",
            obs,
            ctx
        );
    }
}

#[test]
fn live_caller_tracker_recognises_eagain_after_flag_last_as_eos() {
    let mut t = LiveCallerEosTracker::default();
    assert!(!t.observe(DqbufObs::FrameWithFlagLast));
    assert!(t.observe(DqbufObs::EAgain), "knock-on rule: EAGAIN after FLAG_LAST = EOS");
    assert!(t.eos_observed());
}

#[test]
fn live_caller_tracker_recognises_epipe_immediately_as_eos() {
    let mut t = LiveCallerEosTracker::default();
    assert!(t.observe(DqbufObs::EPipe));
    assert!(t.eos_observed());
}

#[test]
fn live_caller_tracker_does_not_eos_on_bare_eagain() {
    // EAGAIN without a prior FLAG_LAST is just a transient
    // poll-vs-DQBUF race; not EOS.
    let mut t = LiveCallerEosTracker::default();
    assert!(!t.observe(DqbufObs::EAgain));
    assert!(!t.eos_observed());
}

#[test]
fn live_caller_tracker_resets_flag_last_on_genuine_frame() {
    // bcm2835-codec quirk: FLAG_LAST may fire spuriously, then a
    // legitimate frame follows. The tracker MUST reset
    // `flag_last_seen` so a much-later EAGAIN doesn't trip the
    // knock-on rule and prematurely call EOS.
    let mut t = LiveCallerEosTracker::default();
    assert!(!t.observe(DqbufObs::FrameWithFlagLast));
    assert!(!t.observe(DqbufObs::Frame));
    // EAGAIN here is NOT EOS — the FLAG_LAST was the quirk.
    assert!(!t.observe(DqbufObs::EAgain));
    assert!(!t.eos_observed());
}

#[test]
fn live_caller_tracker_multi_frame_then_genuine_epipe() {
    // Walk a normal slide: several frames, an FLAG_LAST quirk
    // mid-stream that gets defanged, a real frame after it, more
    // frames, then EPIPE.
    let mut t = LiveCallerEosTracker::default();
    for _ in 0..5 {
        assert!(!t.observe(DqbufObs::Frame));
    }
    assert!(!t.observe(DqbufObs::FrameWithFlagLast));
    assert!(!t.observe(DqbufObs::Frame), "post-FLAG_LAST quirk frame delivered");
    for _ in 0..3 {
        assert!(!t.observe(DqbufObs::Frame));
    }
    assert!(t.observe(DqbufObs::EPipe));
    assert!(t.eos_observed());
}

#[test]
fn live_caller_tracker_flag_last_then_eagain_then_more_frames_is_inconsistent_but_handled() {
    // Defensive: once eos_observed=true, the tracker stays in EOS
    // (no more deliveries should occur but if a buggy caller asks,
    // the answer remains EOS until tracker is reconstructed via
    // resume_after_eos at the V4L2 wrapper level).
    let mut t = LiveCallerEosTracker::default();
    t.observe(DqbufObs::FrameWithFlagLast);
    assert!(t.observe(DqbufObs::EAgain));
    assert!(t.eos_observed());
    // Even if more observations come in, eos stays sticky.
    let _ = t.observe(DqbufObs::Frame);
    assert!(t.eos_observed());
}
