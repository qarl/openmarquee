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

use serde::{Deserialize, Serialize};
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

// ============================================================
// v1-spec-delta #9 (slice b, 2026-05-08) -- 7-op IPC contract
// per spec §10. Request/response types for the JSON-line
// protocol the IPC sidecar dispatcher will read from stdin and
// write to stdout. Pure-data types -- no I/O, no dispatch
// logic. Slice (c) wires the actual stdin/stdout JSON-line
// loop over these types.
//
// Wire format: one request per stdin line, one response per
// stdout line. Both are JSON. The protocol is request-response
// synchronous (no pipelining); the caller reads each response
// before sending the next request. This matches the playback
// loop's natural cadence (one advance per frame budget) and
// avoids out-of-order failures.
// ============================================================

/// Tagged request envelope. The `op` field discriminates among
/// the 7 ops; `params` is the per-op payload. serde's tagged-
/// enum representation handles both encode and decode.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "op", content = "params", rename_all = "snake_case")]
pub enum IpcRequest {
    /// Op 1: construct + open. Sets up the renderer's output
    /// (DRM card / Mock framebuffer / HUB75) and dimensions.
    /// Subsequent ops assume open() has succeeded.
    Open(OpenParams),
    /// Op 2: begin a slide presentation at wall-clock t0_ms.
    /// duration_ms is how long the caller intends to hold the
    /// slide before transitioning; the renderer uses it for
    /// completion detection but does NOT auto-transition.
    BeginSlide(BeginSlideParams),
    /// Op 3: advance to wall-clock t_ms. Returns AdvanceResult
    /// telling the caller what was painted (or "idle" /
    /// "slide_complete").
    Advance(AdvanceParams),
    /// Op 4: begin a transition from current slide to a new
    /// slide. Equivalent to "stage the next slide and run the
    /// blend"; advance() drives the actual per-frame paint.
    BeginTransition(BeginTransitionParams),
    /// Op 5: capture the current screen as a PNG written to
    /// the given path. Spec §7.3.
    Capture(CaptureParams),
    /// Op 6: apply new settings without losing playback state.
    /// Currently scopes to rotation / brightness / gamma per
    /// spec §6.3.
    Reconfigure(ReconfigureParams),
    /// Op 8 (STREAM/VLC slice 2.5): flip the sidecar into
    /// external-frame pump mode. After this op the sidecar reads
    /// length-prefixed RGB888 frames off the dedicated binary FD
    /// (OPENMARQUEE_FRAME_FD) — [u32-BE length][payload], a
    /// length of 0 is the end sentinel — painting each one
    /// fullscreen until the sentinel returns it to the JSON-op
    /// loop. Source-agnostic: the frames are "RGB from some
    /// external producer" (the Python ffmpeg/RTSP pump today, a
    /// headless browser later — see STREAM_VLC_PROPOSAL §10).
    BeginExternalFrames(BeginExternalFramesParams),
    /// Op 7: release everything. Caller is expected to close
    /// the IPC pipe after receiving the response.
    Close,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct OpenParams {
    /// Output target name. One of `hdmi` / `mock` / `hub75` /
    /// `ws2812b` per spec §5. Slice (c) only wires `hdmi`;
    /// other targets warn-and-bail until their phases land.
    pub output: String,
    /// Optional DRM card path override. None falls back to
    /// the existing /dev/dri/card{1,0} scan.
    #[serde(default)]
    pub drm_card: Option<String>,
    /// Optional content_root override. Required for slides
    /// with background_image_slide_id / type=image / type=
    /// video.
    #[serde(default)]
    pub content_root: Option<String>,
    /// FYS bug 5 -- display rotation in degrees. One of
    /// 0 / 90 / 180 / 270; any other value is treated as 0 by
    /// the open handler (with a warning). `#[serde(default)]`
    /// makes it 0 when absent, so pre-rotation Python callers
    /// stay wire-compatible.
    #[serde(default)]
    pub rotation: i32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BeginSlideParams {
    pub slide_id: Uuid,
    pub t0_ms: u64,
    pub duration_ms: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct AdvanceParams {
    pub t_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BeginTransitionParams {
    pub to_slide_id: Uuid,
    pub to_duration_ms: u32,
    pub kind: String,
    pub transition_ms: u32,
    pub t0_ms: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct CaptureParams {
    /// Filesystem path to write the PNG. Caller is responsible
    /// for the directory existing + write permissions.
    pub path: String,
}

/// STREAM/VLC HW-decode (2026-05-20): the external-frame pixel
/// format, declared ONCE per begin_external_frames pump session
/// (one producer = one format — NOT per-frame).
///
/// `Rgb888` is the default (`#[serde(default)]` on the field) so
/// a producer that omits `pixel_format` — today's RGB888 VLC
/// pumps before this change, the future webpage BrowserSource —
/// stays wire-compatible. `Nv12` is the HW-decode VLC path:
/// ffmpeg `-c:v h264_v4l2m2m` emits raw source-resolution NV12
/// (no `-vf` swscale), 1.5 bytes/px, and the renderer does the
/// cover-fit scale + NV12->RGB on the GPU.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
#[serde(rename_all = "snake_case")]
pub enum ExternalPixelFormat {
    /// Row-major RGB888, `width * height * 3` bytes/frame. The
    /// `width`/`height` are the renderer panel dims.
    #[default]
    Rgb888,
    /// Planar NV12 (Y plane + interleaved UV plane), `width *
    /// height * 3 / 2` bytes/frame. The `width`/`height` are the
    /// SOURCE video dims; the renderer cover-fit-scales onto the
    /// panel.
    Nv12,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct BeginExternalFramesParams {
    /// Width of each pushed frame, in pixels. For `rgb888` this is
    /// the renderer panel width (every frame `width*height*3`
    /// bytes); for `nv12` it is the SOURCE video width (every
    /// frame `width*height*3/2` bytes) and the renderer cover-fit-
    /// scales onto the panel.
    pub width: u32,
    /// Height of each pushed frame, in pixels. Panel height for
    /// `rgb888`, source video height for `nv12`.
    pub height: u32,
    /// Pixel format of every frame in this pump session. Declared
    /// once (one producer = one format), NOT per-frame.
    /// `#[serde(default)]` -> `rgb888` when absent, so pre-HW-
    /// decode producers stay wire-compatible.
    #[serde(default)]
    pub pixel_format: ExternalPixelFormat,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ReconfigureParams {
    /// Rotation in degrees, multiple of 90. Spec §6.3.
    #[serde(default)]
    pub rotation: Option<i32>,
    /// Brightness scalar in [0.0, 1.0]. None = no change.
    #[serde(default)]
    pub brightness: Option<f32>,
    /// Gamma scalar > 0. None = no change.
    #[serde(default)]
    pub gamma: Option<f32>,
}

/// Tagged response envelope. `ok` is the outcome flag; on
/// success `result` carries the per-op return data; on
/// failure `error` carries a human-readable diagnostic. The
/// caller dispatches on `ok` first, then on `result.command`
/// for advance() responses.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum IpcResponse {
    /// Successful op completion. Per-op return data in
    /// `result`. For ops with no meaningful return (begin_*,
    /// reconfigure, close), result is OpResult::Empty.
    Ok { result: OpResult },
    /// Op failed. `error` is a human-readable message; the
    /// caller decides whether to retry, fail-the-frame, or
    /// abort the renderer process.
    Err { error: String },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "command", rename_all = "snake_case")]
pub enum OpResult {
    /// open() result: the negotiated output dimensions in
    /// pixels. The caller can use these to scale slide assets
    /// before sending content.
    OpenOk { mode_w: u32, mode_h: u32 },
    /// advance() result: PaintSlide. The renderer painted the
    /// current slide at progress t_in_slide_ms.
    PaintSlide { slide_id: Uuid, t_in_slide_ms: u64 },
    /// advance() result: PaintTransition. The renderer
    /// painted the blend at progress in [0, 1].
    PaintTransition {
        from: Uuid,
        to: Uuid,
        kind: String,
        progress: f32,
    },
    /// advance() result: SlideComplete. The slide's
    /// duration_ms has elapsed; caller should begin_
    /// transition or begin_slide.
    SlideComplete { slide_id: Uuid },
    /// advance() result: Idle. No slide loaded. Caller must
    /// begin_slide before further advance() calls produce
    /// useful output.
    Idle,
    /// capture() result: bytes written to path.
    CaptureOk { path: String, bytes: u64 },
    /// Empty result for ops without meaningful return data
    /// (begin_slide / begin_transition / reconfigure / close).
    Empty,
}

/// Helper: convert the pure-state-machine AdvanceCommand to the
/// IPC OpResult shape. Used by slice (c)'s dispatcher.
pub fn advance_command_to_op_result(cmd: AdvanceCommand) -> OpResult {
    match cmd {
        AdvanceCommand::Idle => OpResult::Idle,
        AdvanceCommand::PaintSlide { slide_id, t_in_slide_ms } => {
            OpResult::PaintSlide { slide_id, t_in_slide_ms }
        }
        AdvanceCommand::PaintTransition { from, to, kind, progress } => {
            OpResult::PaintTransition { from, to, kind, progress }
        }
        AdvanceCommand::SlideComplete { slide_id } => {
            OpResult::SlideComplete { slide_id }
        }
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

    // ============================================================
    // v1-spec-delta #9 (slice b) -- IPC contract serde tests.
    // Locks the wire format so the Python playback.py side can
    // depend on stable JSON shapes. Each test round-trips a
    // request/response variant and asserts both the encoded JSON
    // shape AND that decode(encode(x)) == x.
    // ============================================================

    fn roundtrip_request(req: &IpcRequest) {
        let encoded = serde_json::to_string(req).unwrap();
        let decoded: IpcRequest = serde_json::from_str(&encoded).unwrap();
        assert_eq!(*req, decoded, "round-trip mismatch for: {encoded}");
    }

    fn roundtrip_response(resp: &IpcResponse) {
        let encoded = serde_json::to_string(resp).unwrap();
        let decoded: IpcResponse = serde_json::from_str(&encoded).unwrap();
        assert_eq!(*resp, decoded, "round-trip mismatch for: {encoded}");
    }

    #[test]
    fn ipc_request_open_round_trips() {
        let req = IpcRequest::Open(OpenParams {
            output: "hdmi".to_string(),
            drm_card: Some("/dev/dri/card1".to_string()),
            content_root: Some("/var/openmarquee/content".to_string()),
            rotation: 0,
        });
        roundtrip_request(&req);
        // Pin wire format (op + params shape).
        let encoded = serde_json::to_string(&req).unwrap();
        assert!(encoded.contains(r#""op":"open""#));
        assert!(encoded.contains(r#""output":"hdmi""#));
    }

    #[test]
    fn ipc_request_open_decodes_with_omitted_optional_fields() {
        // drm_card and content_root are Option<String> with
        // serde default; they should decode to None when
        // missing. rotation is i32 with serde default; absent
        // -> 0 (FYS bug 5: backward-compatible with pre-rotation
        // Python callers).
        let json = r#"{"op":"open","params":{"output":"hdmi"}}"#;
        let req: IpcRequest = serde_json::from_str(json).unwrap();
        match req {
            IpcRequest::Open(OpenParams { output, drm_card, content_root, rotation }) => {
                assert_eq!(output, "hdmi");
                assert!(drm_card.is_none());
                assert!(content_root.is_none());
                assert_eq!(rotation, 0);
            }
            other => panic!("expected Open, got {other:?}"),
        }
    }

    #[test]
    fn ipc_request_open_round_trips_with_rotation() {
        // FYS bug 5: rotation is part of the wire contract.
        let req = IpcRequest::Open(OpenParams {
            output: "hdmi".to_string(),
            drm_card: None,
            content_root: Some("/var/openmarquee/content".to_string()),
            rotation: 90,
        });
        roundtrip_request(&req);
        let encoded = serde_json::to_string(&req).unwrap();
        assert!(encoded.contains(r#""rotation":90"#));
    }

    #[test]
    fn ipc_request_begin_slide_round_trips() {
        let req = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(7),
            t0_ms: 12345,
            duration_ms: 5000,
        });
        roundtrip_request(&req);
    }

    #[test]
    fn ipc_request_advance_round_trips() {
        let req = IpcRequest::Advance(AdvanceParams { t_ms: 9876543 });
        roundtrip_request(&req);
    }

    #[test]
    fn ipc_request_begin_external_frames_round_trips() {
        // STREAM/VLC slice 2.5 — the push-frames op. The Python
        // RustRenderer encodes exactly this shape on the first
        // render_frame() call.
        let req = IpcRequest::BeginExternalFrames(BeginExternalFramesParams {
            width: 1920,
            height: 1080,
            pixel_format: ExternalPixelFormat::Rgb888,
        });
        roundtrip_request(&req);
        let encoded = serde_json::to_string(&req).unwrap();
        assert!(encoded.contains(r#""op":"begin_external_frames""#));
        assert!(encoded.contains(r#""width":1920"#));
        assert!(encoded.contains(r#""height":1080"#));
        assert!(encoded.contains(r#""pixel_format":"rgb888""#));
    }

    #[test]
    fn ipc_request_begin_external_frames_decodes_without_pixel_format() {
        // STREAM/VLC HW-decode: pixel_format has #[serde(default)]
        // so a producer that predates the field (today's RGB888 VLC
        // pumps, the future webpage BrowserSource) stays wire-
        // compatible — an omitted pixel_format decodes to Rgb888.
        let json = r#"{"op":"begin_external_frames","params":{"width":854,"height":480}}"#;
        let req: IpcRequest = serde_json::from_str(json).unwrap();
        match req {
            IpcRequest::BeginExternalFrames(p) => {
                assert_eq!(p.width, 854);
                assert_eq!(p.height, 480);
                assert_eq!(p.pixel_format, ExternalPixelFormat::Rgb888);
            }
            other => panic!("expected BeginExternalFrames, got {other:?}"),
        }
    }

    #[test]
    fn ipc_request_begin_external_frames_nv12_round_trips() {
        // STREAM/VLC HW-decode: the NV12 pump session. width/height
        // are the SOURCE video dims; the renderer cover-fit-scales.
        let req = IpcRequest::BeginExternalFrames(BeginExternalFramesParams {
            width: 1280,
            height: 720,
            pixel_format: ExternalPixelFormat::Nv12,
        });
        roundtrip_request(&req);
        let encoded = serde_json::to_string(&req).unwrap();
        assert!(encoded.contains(r#""pixel_format":"nv12""#));
    }

    #[test]
    fn ipc_request_begin_transition_round_trips() {
        let req = IpcRequest::BeginTransition(BeginTransitionParams {
            to_slide_id: uuid(9),
            to_duration_ms: 5000,
            kind: "fade".to_string(),
            transition_ms: 800,
            t0_ms: 1500,
        });
        roundtrip_request(&req);
    }

    #[test]
    fn ipc_request_capture_round_trips() {
        let req = IpcRequest::Capture(CaptureParams {
            path: "/tmp/snap.png".to_string(),
        });
        roundtrip_request(&req);
    }

    #[test]
    fn ipc_request_reconfigure_round_trips_with_partial_fields() {
        let req = IpcRequest::Reconfigure(ReconfigureParams {
            rotation: Some(180),
            brightness: None,
            gamma: Some(2.2),
        });
        roundtrip_request(&req);
    }

    #[test]
    fn ipc_request_close_round_trips() {
        let req = IpcRequest::Close;
        roundtrip_request(&req);
        let encoded = serde_json::to_string(&req).unwrap();
        // Close is a no-params op; serde renders it as a
        // tag-only object.
        assert!(encoded.contains(r#""op":"close""#));
    }

    #[test]
    fn ipc_response_ok_open_round_trips() {
        let resp = IpcResponse::Ok {
            result: OpResult::OpenOk { mode_w: 1024, mode_h: 768 },
        };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_advance_paint_slide_round_trips() {
        let resp = IpcResponse::Ok {
            result: OpResult::PaintSlide { slide_id: uuid(3), t_in_slide_ms: 250 },
        };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_advance_paint_transition_round_trips() {
        let resp = IpcResponse::Ok {
            result: OpResult::PaintTransition {
                from: uuid(1),
                to: uuid(2),
                kind: "wipe".to_string(),
                progress: 0.42,
            },
        };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_advance_slide_complete_round_trips() {
        let resp = IpcResponse::Ok {
            result: OpResult::SlideComplete { slide_id: uuid(5) },
        };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_advance_idle_round_trips() {
        let resp = IpcResponse::Ok { result: OpResult::Idle };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_capture_round_trips() {
        let resp = IpcResponse::Ok {
            result: OpResult::CaptureOk {
                path: "/tmp/snap.png".to_string(),
                bytes: 184320,
            },
        };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_ok_empty_round_trips() {
        let resp = IpcResponse::Ok { result: OpResult::Empty };
        roundtrip_response(&resp);
    }

    #[test]
    fn ipc_response_err_round_trips() {
        let resp = IpcResponse::Err {
            error: "begin_transition requires a current slide".to_string(),
        };
        roundtrip_response(&resp);
    }

    #[test]
    #[ignore]
    fn _print_wire_format_samples_for_proxy_development() {
        // Run via `cargo test _print_wire_format -- --ignored --nocapture`.
        // Used to confirm exact JSON shape for the Python proxy.
        let u = uuid(1);
        let v = uuid(2);
        let cases: Vec<(&str, String)> = vec![
            ("req_open", serde_json::to_string(&IpcRequest::Open(OpenParams {
                output: "hdmi".into(), drm_card: None,
                content_root: Some("/tmp".into()),
                rotation: 0,
            })).unwrap()),
            ("req_begin_slide", serde_json::to_string(&IpcRequest::BeginSlide(BeginSlideParams {
                slide_id: u, t0_ms: 100, duration_ms: 5000,
            })).unwrap()),
            ("req_advance", serde_json::to_string(&IpcRequest::Advance(AdvanceParams { t_ms: 500 })).unwrap()),
            ("req_close", serde_json::to_string(&IpcRequest::Close).unwrap()),
            ("resp_open_ok", serde_json::to_string(&IpcResponse::Ok {
                result: OpResult::OpenOk { mode_w: 1920, mode_h: 1080 },
            }).unwrap()),
            ("resp_paint_slide", serde_json::to_string(&IpcResponse::Ok {
                result: OpResult::PaintSlide { slide_id: u, t_in_slide_ms: 500 },
            }).unwrap()),
            ("resp_paint_transition", serde_json::to_string(&IpcResponse::Ok {
                result: OpResult::PaintTransition { from: u, to: v, kind: "fade".into(), progress: 0.42 },
            }).unwrap()),
            ("resp_slide_complete", serde_json::to_string(&IpcResponse::Ok {
                result: OpResult::SlideComplete { slide_id: u },
            }).unwrap()),
            ("resp_idle", serde_json::to_string(&IpcResponse::Ok { result: OpResult::Idle }).unwrap()),
            ("resp_capture_ok", serde_json::to_string(&IpcResponse::Ok {
                result: OpResult::CaptureOk { path: "/tmp/x.png".into(), bytes: 184 },
            }).unwrap()),
            ("resp_empty", serde_json::to_string(&IpcResponse::Ok { result: OpResult::Empty }).unwrap()),
            ("resp_err", serde_json::to_string(&IpcResponse::Err { error: "video TBD".into() }).unwrap()),
        ];
        for (label, json) in cases {
            println!("WIRE {}: {}", label, json);
        }
    }

    #[test]
    fn advance_command_to_op_result_maps_each_variant() {
        assert_eq!(
            advance_command_to_op_result(AdvanceCommand::Idle),
            OpResult::Idle
        );
        assert_eq!(
            advance_command_to_op_result(AdvanceCommand::PaintSlide {
                slide_id: uuid(1),
                t_in_slide_ms: 100,
            }),
            OpResult::PaintSlide { slide_id: uuid(1), t_in_slide_ms: 100 }
        );
        assert_eq!(
            advance_command_to_op_result(AdvanceCommand::PaintTransition {
                from: uuid(1),
                to: uuid(2),
                kind: "fade".to_string(),
                progress: 0.5,
            }),
            OpResult::PaintTransition {
                from: uuid(1),
                to: uuid(2),
                kind: "fade".to_string(),
                progress: 0.5,
            }
        );
        assert_eq!(
            advance_command_to_op_result(AdvanceCommand::SlideComplete {
                slide_id: uuid(7),
            }),
            OpResult::SlideComplete { slide_id: uuid(7) }
        );
    }
}
