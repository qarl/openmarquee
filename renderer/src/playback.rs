//! v1-spec-delta #9 (slice a, 2026-05-08) -- tick-driven playback
//! state machine. Pure logic, cross-platform, no GL or DRM. The
//! playback loop in `backend/openmarquee/playback.py` (or any
//! IPC-sidecar caller) drives this via begin_slide /
//! begin_transition / advance, and the renderer's GL-bound layer
//! translates the AdvanceCommand returned by advance() into a
//! paint_slide / paint_transition call.
//!
//! Inversion of control vs the existing self-paced render APIs:
//!   - render_slide / render_animated_slide / render_transition_
//!     animated each sleep+loop internally to "drive" a slide
//!     for hold_ms / transition_ms.
//!   - PlaybackState is OUTER-driven: the caller calls advance(t)
//!     at wall-clock t and gets back what to paint NOW. No
//!     internal sleeps. The caller is responsible for pacing.
//!
//! Slice (a) is pure state-machine logic with host tests. Slice
//! (b) defines the 7-op contract over this state. Slice (c)
//! wires the JSON-line IPC dispatcher. Slice (d) preps Python-
//! side integration. Slice (e) adds Pi-smoke gates.

use uuid::Uuid;

/// Per-slide context tracked between begin_slide and the advance
/// that completes the slide's hold.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SlideContext {
    pub slide_id: Uuid,
    /// Wall-clock ms when the slide started.
    pub t0_ms: u64,
    /// How long the slide should be held before the caller
    /// transitions to the next item.
    pub duration_ms: u32,
}

/// Per-transition context tracked between begin_transition and
/// the advance that completes the transition's blend.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TransitionContext {
    pub from_slide_id: Uuid,
    pub to_slide: SlideContext,
    /// Transition kind (cut / fade / wipe / ... per spec §11).
    pub kind: String,
    /// Wall-clock ms when the transition started.
    pub t0_ms: u64,
    /// How long the transition's blend should take.
    pub transition_ms: u32,
}

/// Tick-driven playback state. Holds either a current slide or
/// a pending transition (or both, with the transition taking
/// priority during its blend window). Created once per renderer
/// lifecycle; reset on close.
#[derive(Debug, Clone, Default)]
pub struct PlaybackState {
    /// The slide currently being held / scanned out. None means
    /// the renderer hasn't been given a slide yet.
    pub current: Option<SlideContext>,
    /// In-flight transition. When Some, advance() drives the
    /// blend; when its duration elapses, to_slide promotes to
    /// current and pending clears.
    pub pending: Option<TransitionContext>,
}

/// Result of advance(t_ms) -- tells the renderer what to paint
/// (and at what stage). The GL-bound layer translates this to
/// actual paint_slide / paint_transition calls.
#[derive(Debug, Clone, PartialEq)]
pub enum AdvanceCommand {
    /// Renderer has no slide loaded; paint nothing (or hold
    /// previous frame). Caller should call begin_slide before
    /// the next advance.
    Idle,
    /// Paint the current slide at progress t_in_slide_ms (ms
    /// since slide entry). Useful for animated slides whose
    /// motion advances per-frame.
    PaintSlide {
        slide_id: Uuid,
        t_in_slide_ms: u64,
    },
    /// Paint a transition between from -> to at normalized
    /// progress in [0.0, 1.0]. The renderer's transition path
    /// uses progress to interpolate the blend shader.
    PaintTransition {
        from: Uuid,
        to: Uuid,
        kind: String,
        progress: f32,
    },
    /// Current slide's duration_ms has elapsed; caller should
    /// either begin_transition or begin_slide the next item.
    /// The PaintSlide for the final frame was already returned
    /// on a prior advance; this command surfaces the boundary
    /// without re-painting.
    SlideComplete {
        slide_id: Uuid,
    },
}

impl PlaybackState {
    pub fn new() -> Self {
        Self::default()
    }

    /// Begin a slide presentation. Drops any existing transition
    /// (begin_transition is the slot for that) but is tolerant
    /// of being called over a current slide -- previous slide is
    /// simply replaced. Caller is responsible for ordering
    /// (typically begin_transition first, then begin_slide as
    /// the to_slide promotes).
    pub fn begin_slide(&mut self, slide_id: Uuid, t0_ms: u64, duration_ms: u32) {
        self.current = Some(SlideContext {
            slide_id,
            t0_ms,
            duration_ms,
        });
        self.pending = None;
    }

    /// Begin a transition from the current slide to a new slide.
    /// The new slide is held in pending.to_slide; once the
    /// transition completes, advance() promotes it to current.
    /// Errors if there's no current slide (can't transition from
    /// nothing).
    pub fn begin_transition(
        &mut self,
        to_slide_id: Uuid,
        to_duration_ms: u32,
        kind: &str,
        transition_ms: u32,
        t0_ms: u64,
    ) -> Result<(), &'static str> {
        let from_slide_id = match &self.current {
            Some(c) => c.slide_id,
            None => return Err("begin_transition requires a current slide"),
        };
        self.pending = Some(TransitionContext {
            from_slide_id,
            to_slide: SlideContext {
                slide_id: to_slide_id,
                t0_ms: t0_ms.saturating_add(transition_ms as u64),
                duration_ms: to_duration_ms,
            },
            kind: kind.to_string(),
            t0_ms,
            transition_ms,
        });
        Ok(())
    }

    /// Compute what to paint at wall-clock t_ms. Promotes
    /// to_slide on transition completion. Doesn't actually
    /// paint -- that's the GL-bound layer's job.
    pub fn advance(&mut self, t_ms: u64) -> AdvanceCommand {
        // Transition takes precedence during its blend window.
        if let Some(transition) = self.pending.clone() {
            let elapsed = t_ms.saturating_sub(transition.t0_ms);
            if elapsed >= transition.transition_ms as u64 {
                // Transition complete; promote to_slide.
                self.current = Some(transition.to_slide.clone());
                self.pending = None;
                // Caller paints the new slide on its next advance
                // call; this advance returns PaintSlide so the
                // first frame of the new slide is rendered NOW
                // without an extra round-trip.
                return AdvanceCommand::PaintSlide {
                    slide_id: transition.to_slide.slide_id,
                    t_in_slide_ms: 0,
                };
            }
            let progress = elapsed as f32 / transition.transition_ms as f32;
            return AdvanceCommand::PaintTransition {
                from: transition.from_slide_id,
                to: transition.to_slide.slide_id,
                kind: transition.kind,
                progress: progress.clamp(0.0, 1.0),
            };
        }
        if let Some(slide) = self.current.clone() {
            let elapsed = t_ms.saturating_sub(slide.t0_ms);
            if elapsed >= slide.duration_ms as u64 {
                return AdvanceCommand::SlideComplete {
                    slide_id: slide.slide_id,
                };
            }
            return AdvanceCommand::PaintSlide {
                slide_id: slide.slide_id,
                t_in_slide_ms: elapsed,
            };
        }
        AdvanceCommand::Idle
    }

    /// Reset to the empty state. Used on `close` (op 7) to
    /// release the playback context cleanly.
    pub fn reset(&mut self) {
        self.current = None;
        self.pending = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn uuid(n: u8) -> Uuid {
        Uuid::from_bytes([n; 16])
    }

    #[test]
    fn new_state_is_idle() {
        let mut s = PlaybackState::new();
        assert_eq!(s.advance(0), AdvanceCommand::Idle);
        assert_eq!(s.advance(1_000_000), AdvanceCommand::Idle);
    }

    #[test]
    fn begin_slide_then_advance_paints_slide_with_relative_t() {
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 1000, 5000);
        // At slide entry, t_in_slide_ms = 0.
        assert_eq!(
            s.advance(1000),
            AdvanceCommand::PaintSlide { slide_id: uuid(1), t_in_slide_ms: 0 }
        );
        // Mid-hold, t_in_slide_ms = 2000.
        assert_eq!(
            s.advance(3000),
            AdvanceCommand::PaintSlide { slide_id: uuid(1), t_in_slide_ms: 2000 }
        );
    }

    #[test]
    fn slide_completes_at_or_after_duration() {
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 0, 1000);
        // Just before duration -> still painting.
        assert_eq!(
            s.advance(999),
            AdvanceCommand::PaintSlide { slide_id: uuid(1), t_in_slide_ms: 999 }
        );
        // At exactly duration -> SlideComplete.
        assert_eq!(s.advance(1000), AdvanceCommand::SlideComplete { slide_id: uuid(1) });
        // After duration -> still SlideComplete (caller hasn't
        // begun_slide / begun_transition yet).
        assert_eq!(s.advance(2000), AdvanceCommand::SlideComplete { slide_id: uuid(1) });
    }

    #[test]
    fn begin_transition_requires_current_slide() {
        let mut s = PlaybackState::new();
        let err = s.begin_transition(uuid(2), 5000, "fade", 800, 1000).unwrap_err();
        assert!(err.contains("current slide"));
    }

    #[test]
    fn transition_paints_progress_then_promotes_to_slide() {
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 0, 1_000_000);  // long-held bg.
        s.begin_transition(uuid(2), 5000, "fade", 800, 1000).unwrap();
        // Mid-transition, progress=0.5.
        match s.advance(1400) {
            AdvanceCommand::PaintTransition { from, to, kind, progress } => {
                assert_eq!(from, uuid(1));
                assert_eq!(to, uuid(2));
                assert_eq!(kind, "fade");
                assert!((progress - 0.5).abs() < 0.001);
            }
            other => panic!("expected PaintTransition, got {other:?}"),
        }
        // At transition end, to_slide promotes; PaintSlide for
        // the new slide at t_in_slide_ms=0.
        assert_eq!(
            s.advance(1800),
            AdvanceCommand::PaintSlide { slide_id: uuid(2), t_in_slide_ms: 0 }
        );
        // After promotion, advance(2000) paints uuid(2) at
        // t_in_slide_ms = 200 (since to_slide.t0_ms was set to
        // 1000 + 800 = 1800 by begin_transition).
        assert_eq!(
            s.advance(2000),
            AdvanceCommand::PaintSlide { slide_id: uuid(2), t_in_slide_ms: 200 }
        );
    }

    #[test]
    fn transition_progress_clamps_to_unit_range() {
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 0, 1_000_000);
        s.begin_transition(uuid(2), 5000, "wipe", 1000, 100).unwrap();
        // Just-after-start: progress ~= 0.0.
        match s.advance(100) {
            AdvanceCommand::PaintTransition { progress, .. } => {
                assert_eq!(progress, 0.0);
            }
            other => panic!("expected PaintTransition, got {other:?}"),
        }
        // Just-before-end: progress < 1.0.
        match s.advance(1099) {
            AdvanceCommand::PaintTransition { progress, .. } => {
                assert!(progress < 1.0);
                assert!(progress > 0.99);
            }
            other => panic!("expected PaintTransition, got {other:?}"),
        }
    }

    #[test]
    fn reset_returns_to_idle() {
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 0, 5000);
        assert!(matches!(s.advance(100), AdvanceCommand::PaintSlide { .. }));
        s.reset();
        assert_eq!(s.advance(100), AdvanceCommand::Idle);
    }

    #[test]
    fn begin_slide_during_transition_replaces_pending() {
        // Belt-and-suspenders: caller can recover from a botched
        // transition by calling begin_slide directly. This is
        // not the canonical path -- the playback loop usually
        // lets transitions complete -- but defensive coding
        // here means a malformed playback driver doesn't wedge
        // the renderer.
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 0, 5000);
        s.begin_transition(uuid(2), 5000, "fade", 800, 1000).unwrap();
        // Force a reset to slide(3) mid-transition.
        s.begin_slide(uuid(3), 2000, 5000);
        assert!(s.pending.is_none());
        assert_eq!(
            s.advance(2500),
            AdvanceCommand::PaintSlide { slide_id: uuid(3), t_in_slide_ms: 500 }
        );
    }

    #[test]
    fn t_before_t0_ms_floors_at_zero() {
        // saturating_sub guards against malformed timestamps
        // (e.g., out-of-order advance calls). The renderer
        // paints t_in_slide_ms=0 rather than panicking.
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 1000, 5000);
        assert_eq!(
            s.advance(500),
            AdvanceCommand::PaintSlide { slide_id: uuid(1), t_in_slide_ms: 0 }
        );
    }

    #[test]
    fn zero_duration_slide_completes_immediately() {
        // Edge case: a slide with duration_ms=0 SlideCompletes
        // on the first advance at-or-after t0_ms.
        let mut s = PlaybackState::new();
        s.begin_slide(uuid(1), 100, 0);
        assert_eq!(
            s.advance(100),
            AdvanceCommand::SlideComplete { slide_id: uuid(1) }
        );
    }
}
