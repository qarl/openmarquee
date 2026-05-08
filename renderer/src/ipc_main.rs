//! v1-spec-delta #9 (slice c, 2026-05-08) -- IPC sidecar
//! dispatcher. Reads JSON-line IpcRequest messages from stdin,
//! drives the playback state machine, writes JSON-line
//! IpcResponse messages to stdout. The 7-op contract per spec
//! §10.
//!
//! Slice (c) scope: dispatcher loop + state-machine
//! integration. Slide content loading + actual GL paint of
//! Advance's PaintSlide / PaintTransition results land in
//! slice (d). Capture + Reconfigure ship in slice (e).
//!
//! Lifecycle: the renderer process enters this loop after
//! parsing the --ipc-sidecar CLI flag. The OUTER loop reads
//! requests until the Open op arrives; once Open succeeds, an
//! INNER loop runs ops 2-7 inside a single with_egl_session
//! scope so DRM master + EGL context are held continuously
//! across Advance calls. Close exits the inner loop; the
//! process exits via the outer loop's `return`.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use anyhow::{anyhow, Result};

use crate::content::{find_image_slide, find_text_slide, find_video_slide, ContentItem};
use crate::playback::{
    advance_command_to_op_result, AdvanceCommand, IpcRequest, IpcResponse, OpResult,
    OpenParams, PlaybackState,
};
#[cfg(target_os = "linux")]
use crate::hdmi_logic::FontCatalog;

/// Cached slide content keyed by UUID. Populated on BeginSlide
/// + BeginTransition; consumed by Advance's actual-paint path
/// in slice (d). Slice (c) populates the cache but doesn't
/// paint -- the cache is plumbed for slice (d) to pick up.
struct SlideCache {
    items: std::collections::HashMap<uuid::Uuid, ContentItem>,
}

impl SlideCache {
    fn new() -> Self {
        Self {
            items: std::collections::HashMap::new(),
        }
    }

    /// Try to load + cache a slide by UUID. content_root is
    /// required for the find_*_slide chain. Tries text -> image
    /// -> video. Returns Err with a message if all three return
    /// Ok(None) (unknown type) or any return Err.
    fn load(&mut self, content_root: &std::path::Path, item_id: uuid::Uuid) -> Result<()> {
        if self.items.contains_key(&item_id) {
            return Ok(());
        }
        match find_text_slide(content_root, item_id)? {
            Some(s) => {
                self.items.insert(item_id, ContentItem::Text(s));
                return Ok(());
            }
            None => {}
        }
        match find_image_slide(content_root, item_id)? {
            Some(s) => {
                self.items.insert(item_id, ContentItem::Image(s));
                return Ok(());
            }
            None => {}
        }
        match find_video_slide(content_root, item_id)? {
            Some(s) => {
                self.items.insert(item_id, ContentItem::Video(s));
                return Ok(());
            }
            None => {}
        }
        Err(anyhow!(
            "no item found for {item_id} under {} (type not text_slide / image / video)",
            content_root.display()
        ))
    }
}

/// Emit a response to stdout as a single JSON line + flush.
/// stdout is line-buffered by default; explicit flush ensures
/// the caller never sees a partial line on a slow stdin read.
fn emit_response<W: Write>(writer: &mut W, resp: &IpcResponse) -> Result<()> {
    serde_json::to_writer(&mut *writer, resp)?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    Ok(())
}

fn ok_empty() -> IpcResponse {
    IpcResponse::Ok { result: OpResult::Empty }
}

fn err(msg: impl Into<String>) -> IpcResponse {
    IpcResponse::Err { error: msg.into() }
}

/// Outer loop: read requests until Open arrives. Other ops
/// before Open return Err. After Open succeeds, dispatch
/// transfers to run_inner_loop which holds the EGL session.
pub fn run_ipc_sidecar() -> Result<()> {
    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    let stdin_lock = stdin.lock();
    let mut lines = stdin_lock.lines();

    while let Some(line) = lines.next() {
        let line = line?;
        let req: IpcRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                emit_response(&mut stdout, &err(format!("invalid request: {e}")))?;
                continue;
            }
        };
        match req {
            IpcRequest::Open(params) => {
                match run_open_and_inner_loop(params, &mut lines, &mut stdout) {
                    Ok(()) => return Ok(()),
                    Err(e) => {
                        emit_response(&mut stdout, &err(format!("open failed: {e:#}")))?;
                        // Stay in outer loop -- caller may retry
                        // Open with corrected params.
                    }
                }
            }
            IpcRequest::Close => {
                // Close before Open: a no-op success per the
                // permissive end-of-life shape (caller may
                // close without opening if init fails).
                emit_response(&mut stdout, &ok_empty())?;
                return Ok(());
            }
            _ => {
                emit_response(
                    &mut stdout,
                    &err("expected Open before other ops"),
                )?;
            }
        }
    }
    Ok(())
}

/// Inner loop body invoked after Open succeeds. Slice (d)
/// branches on cfg(target_os = "linux"): on Linux, run the
/// inner loop inside with_egl_session so EglSession is held
/// across Advance calls + actual GL paint fires; on Mac
/// (cargo test only), run state-machine-only mode (slice c
/// behavior).
fn run_open_and_inner_loop<I, W>(
    params: OpenParams,
    lines: &mut I,
    stdout: &mut W,
) -> Result<()>
where
    I: Iterator<Item = std::io::Result<String>>,
    W: Write,
{
    if params.output != "hdmi" {
        return Err(anyhow!(
            "output {:?} not supported; only hdmi",
            params.output
        ));
    }
    let content_root = params
        .content_root
        .as_ref()
        .map(PathBuf::from)
        .ok_or_else(|| anyhow!("content_root is required for IPC sidecar mode"))?;
    if !content_root.exists() {
        return Err(anyhow!(
            "content_root {} does not exist",
            content_root.display()
        ));
    }

    #[cfg(target_os = "linux")]
    {
        return run_open_and_inner_loop_linux(params, lines, stdout, &content_root);
    }
    #[cfg(not(target_os = "linux"))]
    {
        return run_open_and_inner_loop_state_only(lines, stdout, &content_root);
    }
}

/// Mac / non-Linux build: state-machine-only inner loop. Used
/// by cargo test on the dev box where DRM isn't available.
/// Mirrors slice (c) behavior: emit placeholder OpenOk, run
/// the state machine, ignore paint hooks.
#[cfg(not(target_os = "linux"))]
fn run_open_and_inner_loop_state_only<I, W>(
    lines: &mut I,
    stdout: &mut W,
    content_root: &Path,
) -> Result<()>
where
    I: Iterator<Item = std::io::Result<String>>,
    W: Write,
{
    emit_response(
        stdout,
        &IpcResponse::Ok {
            result: OpResult::OpenOk { mode_w: 1024, mode_h: 768 },
        },
    )?;
    let mut state = PlaybackState::new();
    let mut cache = SlideCache::new();
    while let Some(line) = lines.next() {
        let line = line?;
        let req: IpcRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                emit_response(stdout, &err(format!("invalid request: {e}")))?;
                continue;
            }
        };
        let is_close = matches!(req, IpcRequest::Close);
        let resp = handle_inner_request(req, &mut state, &mut cache, content_root);
        emit_response(stdout, &resp)?;
        if is_close {
            break;
        }
    }
    Ok(())
}

/// Linux build: open the DRM card, enter run_in_egl_session,
/// and run the inner loop inside the closure. Each Advance op
/// that produces PaintSlide / PaintTransition triggers an
/// actual GL paint via paint_and_present_one_frame_*. Errors
/// in paint surface as IpcResponse::Err{message}; the loop
/// continues so the caller can recover (e.g., re-BeginSlide
/// after a transient FBO failure).
#[cfg(target_os = "linux")]
fn run_open_and_inner_loop_linux<I, W>(
    params: OpenParams,
    lines: &mut I,
    stdout: &mut W,
    content_root: &Path,
) -> Result<()>
where
    I: Iterator<Item = std::io::Result<String>>,
    W: Write,
{
    use crate::hdmi;
    use crate::Card;

    let card_path = match params.drm_card.as_deref() {
        Some(p) => Path::new(p).to_path_buf(),
        None => {
            // Same scan order as the standalone CLI: card1
            // before card0.
            let candidates = [Path::new("/dev/dri/card1"), Path::new("/dev/dri/card0")];
            candidates
                .iter()
                .find(|p| p.exists())
                .map(|p| p.to_path_buf())
                .ok_or_else(|| anyhow!("no /dev/dri/card{{0,1}} found"))?
        }
    };
    let card = Card::open(&card_path)
        .map_err(|e| anyhow!("DRM open {} failed: {e:#}", card_path.display()))?;

    // Font catalog -- needed by paint_slide for the text-layer
    // rasterization. Use the same defaults as the standalone
    // CLI.
    let catalog = FontCatalog::new(
        std::path::PathBuf::from("/opt/openmarquee/ui/fonts"),
        "Anton".to_string(),
    );
    let fonts: Option<&FontCatalog> = if catalog.fallback_available() {
        Some(&catalog)
    } else {
        eprintln!(
            "warn: ipc_sidecar font catalog at /opt/openmarquee/ui/fonts can't load fallback Anton; rendering bg only"
        );
        None
    };

    hdmi::run_in_egl_session(&card, |session| {
        let (mw, mh) = hdmi::egl_session_mode_size(session);
        emit_response(
            stdout,
            &IpcResponse::Ok {
                result: OpResult::OpenOk { mode_w: mw, mode_h: mh },
            },
        )?;
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        while let Some(line) = lines.next() {
            let line = line?;
            let req: IpcRequest = match serde_json::from_str(&line) {
                Ok(r) => r,
                Err(e) => {
                    emit_response(stdout, &err(format!("invalid request: {e}")))?;
                    continue;
                }
            };
            let is_close = matches!(req, IpcRequest::Close);
            let resp = handle_inner_request(req, &mut state, &mut cache, content_root);

            // Linux paint hook: when the dispatcher returned a
            // PaintSlide / PaintTransition OpResult, fire the
            // actual GL paint. If paint errors, override the
            // response so the caller sees Err{message} rather
            // than a fake-success response.
            let resp = run_paint_hook(
                &resp,
                session,
                &card,
                &cache,
                fonts,
                Some(content_root),
            );

            emit_response(stdout, &resp)?;
            if is_close {
                break;
            }
        }
        Ok(())
    })
}

/// Linux paint hook: translate PaintSlide / PaintTransition
/// OpResults into actual paint_and_present_one_frame_* calls.
/// Returns the original response on success, or an Err
/// response on paint failure. State machine + cache state are
/// already updated; this hook only paints.
#[cfg(target_os = "linux")]
fn run_paint_hook(
    resp: &IpcResponse,
    session: &mut crate::hdmi::EglSession,
    card: &crate::Card,
    cache: &SlideCache,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
) -> IpcResponse {
    use crate::content::ContentItem;
    use crate::hdmi;

    let result = match resp {
        IpcResponse::Ok { result } => result,
        // Pass through errors unchanged.
        IpcResponse::Err { .. } => return resp.clone(),
    };
    match result {
        OpResult::PaintSlide { slide_id, t_in_slide_ms } => {
            let item = match cache.items.get(slide_id) {
                Some(i) => i,
                None => {
                    return err(format!(
                        "paint_slide: slide {slide_id} not in cache (begin_slide first?)"
                    ));
                }
            };
            match item {
                ContentItem::Text(slide) => {
                    if let Err(e) = hdmi::paint_and_present_one_frame_for_slide(
                        session,
                        card,
                        slide,
                        fonts,
                        content_root,
                        *t_in_slide_ms,
                    ) {
                        return err(format!("paint_slide failed: {e:#}"));
                    }
                    resp.clone()
                }
                _ => err("paint_slide: only text slides supported in slice (d); image/video TBD"),
            }
        }
        OpResult::PaintTransition { from, to, kind, progress } => {
            let from_item = match cache.items.get(from) {
                Some(ContentItem::Text(s)) => s,
                Some(_) => return err("paint_transition: from non-text slide TBD"),
                None => return err(format!("paint_transition: from slide {from} not in cache")),
            };
            let to_item = match cache.items.get(to) {
                Some(ContentItem::Text(s)) => s,
                Some(_) => return err("paint_transition: to non-text slide TBD"),
                None => return err(format!("paint_transition: to slide {to} not in cache")),
            };
            if let Err(e) = hdmi::paint_and_present_one_transition_frame(
                session,
                card,
                from_item,
                to_item,
                fonts,
                content_root,
                kind,
                *progress,
            ) {
                return err(format!("paint_transition failed: {e:#}"));
            }
            resp.clone()
        }
        // Non-paint OpResults: pass through unchanged.
        _ => resp.clone(),
    }
}

/// Per-request dispatch. Returns the response to emit. State-
/// machine ops update `state` + `cache`; non-state ops return
/// errors (slice c scope).
fn handle_inner_request(
    req: IpcRequest,
    state: &mut PlaybackState,
    cache: &mut SlideCache,
    content_root: &std::path::Path,
) -> IpcResponse {
    match req {
        IpcRequest::Open(_) => {
            err("Open already called; nested Open is not supported")
        }
        IpcRequest::BeginSlide(p) => {
            if let Err(e) = cache.load(content_root, p.slide_id) {
                return err(format!("begin_slide load failed: {e:#}"));
            }
            state.begin_slide(p.slide_id, p.t0_ms, p.duration_ms);
            ok_empty()
        }
        IpcRequest::BeginTransition(p) => {
            if let Err(e) = cache.load(content_root, p.to_slide_id) {
                return err(format!("begin_transition load failed: {e:#}"));
            }
            match state.begin_transition(
                p.to_slide_id,
                p.to_duration_ms,
                &p.kind,
                p.transition_ms,
                p.t0_ms,
            ) {
                Ok(()) => ok_empty(),
                Err(e) => err(format!("begin_transition: {e}")),
            }
        }
        IpcRequest::Advance(p) => {
            // Slice (c): return the AdvanceCommand-derived
            // OpResult without painting. Slice (d) wires the
            // actual paint_slide / paint_transition calls that
            // turn the OpResult into pixels-on-screen.
            let cmd = state.advance(p.t_ms);
            IpcResponse::Ok {
                result: advance_command_to_op_result(cmd),
            }
        }
        IpcRequest::Capture(_) => {
            err("Capture not yet implemented (slice e)")
        }
        IpcRequest::Reconfigure(_) => {
            err("Reconfigure not yet implemented (slice e)")
        }
        IpcRequest::Close => {
            state.reset();
            ok_empty()
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::playback::{
        AdvanceParams, BeginSlideParams, BeginTransitionParams, IpcRequest,
        IpcResponse, OpResult,
    };
    use uuid::Uuid;

    fn uuid(n: u8) -> Uuid {
        Uuid::from_bytes([n; 16])
    }

    fn handle_with_text_slide_fixture(
        req: IpcRequest,
        state: &mut PlaybackState,
        cache: &mut SlideCache,
    ) -> IpcResponse {
        // Build a tempdir fixture with a known slide so
        // BeginSlide can load. Reuses content.rs SAMPLE_TEXT_
        // ITEM shape via the JSON literal below.
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(1);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("item.json"), SAMPLE_TEXT_ITEM_FOR_UUID_1).unwrap();
        handle_inner_request(req, state, cache, td.path())
    }

    const SAMPLE_TEXT_ITEM_FOR_UUID_1: &str = r##"{
  "schema_version": 3,
  "item": {
    "type": "text_slide",
    "id": "01010101-0101-0101-0101-010101010101",
    "name": "test",
    "duration_ms": 5000,
    "text_layers": [],
    "background_color": "#222222",
    "background_pattern": null,
    "transition": "cut",
    "transition_ms": 500
  }
}"##;

    #[test]
    fn handle_open_in_inner_loop_returns_already_open_error() {
        // Open during the inner loop is an error -- the outer
        // loop has already opened.
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::Open(OpenParams {
            output: "hdmi".to_string(),
            drm_card: None,
            content_root: Some(td.path().to_str().unwrap().to_string()),
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(error.contains("already called"), "got: {error}");
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_begin_slide_loads_cache_and_updates_state() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let req = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(1),
            t0_ms: 100,
            duration_ms: 5000,
        });
        let resp = handle_with_text_slide_fixture(req, &mut state, &mut cache);
        assert_eq!(resp, IpcResponse::Ok { result: OpResult::Empty });
        // Cache should have the slide loaded.
        assert!(cache.items.contains_key(&uuid(1)));
        // State should reflect the slide.
        assert!(state.current.is_some());
        assert_eq!(state.current.as_ref().unwrap().slide_id, uuid(1));
    }

    #[test]
    fn handle_begin_slide_errors_on_missing_content() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(99),
            t0_ms: 0,
            duration_ms: 5000,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(error.contains("begin_slide load failed"), "got: {error}");
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_advance_returns_paint_slide_after_begin_slide() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        // First begin_slide.
        let req_begin = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(1),
            t0_ms: 100,
            duration_ms: 5000,
        });
        let _ = handle_with_text_slide_fixture(req_begin, &mut state, &mut cache);
        // Then advance.
        let req_adv = IpcRequest::Advance(AdvanceParams { t_ms: 500 });
        let td = tempfile::TempDir::new().unwrap();
        let resp = handle_inner_request(req_adv, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Ok {
                result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
            } => {
                assert_eq!(slide_id, uuid(1));
                assert_eq!(t_in_slide_ms, 400);
            }
            other => panic!("expected PaintSlide, got {other:?}"),
        }
    }

    #[test]
    fn handle_begin_transition_loads_to_slide_and_drives_state() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        // Need a current slide first.
        let req_begin = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(1),
            t0_ms: 0,
            duration_ms: 5000,
        });
        let _ = handle_with_text_slide_fixture(req_begin, &mut state, &mut cache);
        // Transition to another slide. We need the content
        // root to have the to_slide as well; reuse the same
        // fixture writer.
        let td = tempfile::TempDir::new().unwrap();
        let id_a = uuid(1);
        let dir_a = td.path().join(id_a.to_string());
        std::fs::create_dir_all(&dir_a).unwrap();
        std::fs::write(dir_a.join("item.json"), SAMPLE_TEXT_ITEM_FOR_UUID_1).unwrap();
        let id_b = uuid(2);
        let dir_b = td.path().join(id_b.to_string());
        std::fs::create_dir_all(&dir_b).unwrap();
        std::fs::write(dir_b.join("item.json"), SAMPLE_TEXT_ITEM_FOR_UUID_1).unwrap();
        let req = IpcRequest::BeginTransition(BeginTransitionParams {
            to_slide_id: id_b,
            to_duration_ms: 5000,
            kind: "fade".to_string(),
            transition_ms: 800,
            t0_ms: 1000,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        assert_eq!(resp, IpcResponse::Ok { result: OpResult::Empty });
        assert!(state.pending.is_some());
        assert_eq!(state.pending.as_ref().unwrap().to_slide.slide_id, id_b);
    }

    #[test]
    fn handle_capture_returns_not_yet_implemented() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::Capture(crate::playback::CaptureParams {
            path: "/tmp/x.png".to_string(),
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(error.contains("Capture not yet implemented"));
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_reconfigure_returns_not_yet_implemented() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::Reconfigure(crate::playback::ReconfigureParams {
            rotation: Some(180),
            brightness: None,
            gamma: None,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(error.contains("Reconfigure not yet implemented"));
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_close_resets_state() {
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        // Stage some state.
        let req_begin = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: uuid(1),
            t0_ms: 0,
            duration_ms: 5000,
        });
        let _ = handle_with_text_slide_fixture(req_begin, &mut state, &mut cache);
        assert!(state.current.is_some());
        // Close.
        let td = tempfile::TempDir::new().unwrap();
        let resp = handle_inner_request(IpcRequest::Close, &mut state, &mut cache, td.path());
        assert_eq!(resp, IpcResponse::Ok { result: OpResult::Empty });
        assert!(state.current.is_none());
        // Cache survives close (caller could re-open without
        // re-loading) -- not a behavior contract, but the
        // current shape preserves it.
    }
}
