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
#[cfg(target_os = "linux")]
use anyhow::Context;

use crate::content::{
    find_image_slide, find_text_slide, find_video_slide, video_slide_asset_path,
    ContentItem, SettingsWatcher,
};
use crate::mp4_demux::Mp4Demuxer;
use crate::playback::{
    advance_command_to_op_result, AdvanceCommand, IpcRequest, IpcResponse, OpResult,
    OpenParams, PlaybackState,
};
#[cfg(target_os = "linux")]
use crate::hdmi_logic::FontCatalog;
#[cfg(target_os = "linux")]
use crate::v4l2;

/// V4L2 piece 3c (2026-05-14): /dev/video10 is the bcm2835-codec
/// decode-side node on Raspberry Pi (verified via piece 1 inventory
/// and piece 2b live decode). Hardcoded for now; a future settings
/// surface might let operators override on different SoCs.
#[cfg(target_os = "linux")]
const V4L2_DECODER_PATH: &str = "/dev/video10";

/// Linux-only V4L2 H.264 decoder state cached alongside the
/// Mp4Demuxer for a VideoSlide. cache.load opens + primes the
/// decoder (format negotiation, REQBUFS, STREAMON, SPS+PPS+IDR
/// fed); piece 3d paint_slide consumes per-advance samples and
/// uploads decoded NV12 frames to GLES textures.
#[cfg(target_os = "linux")]
struct VideoDecoderState {
    decoder: v4l2::Decoder,
    /// Index of the next sample (in the demuxer's `samples` Vec)
    /// to feed on the next paint_slide / advance tick. cache.load
    /// primes by feeding sample 0 (the IDR + any pre-IDR NALs);
    /// after priming this is 1.
    next_sample_idx: usize,
    /// Number of frames successfully painted via the per-advance
    /// paint hook (piece 3e). Incremented in
    /// `paint_and_present_one_video_slide_frame` after a
    /// successful blit+swap. Used for first-frame logging +
    /// future slide-end frame-count diagnostics.
    frames_decoded: usize,
    /// Negotiated capture dimensions (may differ from the input
    /// dims via codec width/height adjustment). Used by piece 3e
    /// to size the GLES texture upload at exactly the codec's
    /// stride (avoids spurious right-edge garbage on non-aligned
    /// widths).
    capture_w: u32,
    capture_h: u32,
}

#[cfg(target_os = "linux")]
impl VideoDecoderState {
    /// Convenience for the paint hook's "did we make progress
    /// this tick?" check; returns the current frames_decoded
    /// counter (incremented by the paint helper on success).
    fn frames_decoded_for_log(&self) -> usize {
        self.frames_decoded
    }
}

/// Phase 9 Step 9a (2026-05-16) — IPC sidecar per-Advance paint
/// metrics for soak readiness. Aggregates PaintSlide + PaintTransition
/// wall-clock timings across a rolling window and emits one
/// journald-tail-friendly summary line every `summary_window_s`
/// seconds. Window stats reset on emit; cumulative stats live in
/// `session_*` fields for the across-soak view.
///
/// Format contract (single line, key=value, monitored by the soak
/// gate): `ipc.soak window_s=W frames=F transitions=T fps_avg=A
/// paint_us=avg/U/max/M paint_us_p99=P session_frames=SF
/// session_transitions=ST`. New fields go on the right; soak
/// parsers regex by key. Token "ipc.soak" is the anchor.
///
/// Phase D slice 1 (2026-05-17) added `paint_us_p99` after the
/// existing `paint_us=avg/U/max/M`. The parser-side p99 ≤ 33.33ms
/// gate lands separately in slice 2. Older parsers ignore the new
/// key (regex by key, not by position).
///
/// `record` is called from the IPC loop AFTER `run_paint_hook` on
/// successful PaintSlide/PaintTransition responses (failure responses
/// would skew avg/max with degenerate timings). `maybe_emit_summary`
/// is called once per loop iteration; cost is one Instant::elapsed +
/// branch when the window hasn't expired.
///
/// `paint_us_samples` is a bounded sample buffer sized at
/// `PAINT_SAMPLE_CAP` (2048; ~16 KB at 8 bytes/entry). At the
/// expected 30 fps × 30 s window = 900 samples we have ~2.3×
/// headroom for transition-burst paints without truncation drama.
/// On overflow we drop newly-arrived samples (cap-and-skip), which
/// turns a degenerate >68 fps window into "first-2048-paints" —
/// statistically stable for p99 since the surplus is uniformly
/// distributed in time.
///
/// `#[allow(dead_code)]` because the production users
/// (`run_inner_session` + `record` call sites) live under
/// `#[cfg(target_os = "linux")]` blocks; on macOS the struct
/// exists only for the cross-platform tests in `mod tests`.
#[allow(dead_code)]
struct IpcPaintMetrics {
    last_summary: std::time::Instant,
    summary_window_s: u64,
    // Window stats (reset on emit).
    frames: u64,
    transitions: u64,
    total_paint_us: u128,
    max_paint_us: u64,
    paint_us_samples: Vec<u64>,
    // Session-cumulative stats (never reset).
    session_frames: u64,
    session_transitions: u64,
}

/// Bound for `IpcPaintMetrics::paint_us_samples`. 2048 entries at
/// 8 bytes = 16 KB; comfortably fits within the Pi Zero memory
/// budget (§8.1) and gives 2.3× headroom over the expected
/// 30 fps × 30 s = 900 samples per window.
const PAINT_SAMPLE_CAP: usize = 2048;

#[allow(dead_code)]
enum IpcPaintKind {
    Slide,
    Transition,
}

#[allow(dead_code)]
impl IpcPaintMetrics {
    fn new() -> Self {
        Self {
            last_summary: std::time::Instant::now(),
            summary_window_s: 30,
            frames: 0,
            transitions: 0,
            total_paint_us: 0,
            max_paint_us: 0,
            paint_us_samples: Vec::with_capacity(PAINT_SAMPLE_CAP),
            session_frames: 0,
            session_transitions: 0,
        }
    }

    fn record(&mut self, kind: IpcPaintKind, elapsed_us: u64) {
        match kind {
            IpcPaintKind::Slide => {
                self.frames += 1;
                self.session_frames += 1;
            }
            IpcPaintKind::Transition => {
                self.transitions += 1;
                self.session_transitions += 1;
            }
        }
        self.total_paint_us = self.total_paint_us.saturating_add(elapsed_us as u128);
        if elapsed_us > self.max_paint_us {
            self.max_paint_us = elapsed_us;
        }
        if self.paint_us_samples.len() < PAINT_SAMPLE_CAP {
            self.paint_us_samples.push(elapsed_us);
        }
    }

    fn maybe_emit_summary(&mut self) {
        let now = std::time::Instant::now();
        let elapsed = now.duration_since(self.last_summary);
        if elapsed.as_secs() < self.summary_window_s {
            return;
        }
        let total_calls = self.frames + self.transitions;
        let avg_us: u64 = if total_calls > 0 {
            (self.total_paint_us / total_calls as u128) as u64
        } else {
            0
        };
        let fps_avg = if elapsed.as_secs_f64() > 0.0 {
            total_calls as f64 / elapsed.as_secs_f64()
        } else {
            0.0
        };
        // Reuse profile.rs percentile math (already cross-platform
        // + unit-tested). Returns (sum, mean, p50, p95, p99, max);
        // we keep p99 for the soak gate. Empty sample slice returns
        // p99=0 -- correct "no data" sentinel for windows with no
        // successful paints.
        let (_, _, _, _, paint_us_p99, _) =
            crate::profile::summarize_samples(&self.paint_us_samples);
        eprintln!(
            "ipc.soak window_s={} frames={} transitions={} fps_avg={:.1} paint_us=avg/{}/max/{} paint_us_p99={} session_frames={} session_transitions={}",
            elapsed.as_secs(),
            self.frames,
            self.transitions,
            fps_avg,
            avg_us,
            self.max_paint_us,
            paint_us_p99,
            self.session_frames,
            self.session_transitions,
        );
        self.last_summary = now;
        self.frames = 0;
        self.transitions = 0;
        self.total_paint_us = 0;
        self.max_paint_us = 0;
        self.paint_us_samples.clear();
    }
}

/// Cached slide content keyed by UUID. Populated on BeginSlide
/// + BeginTransition; consumed by Advance's actual-paint path
/// in slice (d). Slice (c) populates the cache but doesn't
/// paint -- the cache is plumbed for slice (d) to pick up.
///
/// V4L2 piece 3b (2026-05-14): video_demuxers holds an
/// `Mp4Demuxer` per VideoSlide seen on BeginSlide. The MP4 box
/// parse runs once on first cache.load and is reused across
/// advance ticks. Piece 3c will add a parallel decoders map
/// holding `v4l2::Decoder` instances on Linux + wire the
/// demuxer's SPS/PPS/samples through the codec. Demuxer
/// instantiation is best-effort -- if `asset.mp4` is missing or
/// malformed, we log + continue with the cache populated only
/// for `items`; the paint_slide "video slides TBD" path still
/// triggers the Python proxy's PIL fallback.
struct SlideCache {
    items: std::collections::HashMap<uuid::Uuid, ContentItem>,
    /// Bug 1 (qarl 2026-05-16): item.json mtime per cached slide.
    /// `cache.load` short-circuits on `items.contains_key`, which means
    /// a content edit (text change, image re-upload, etc.) never reaches
    /// the running show — the sidecar serves the pre-edit cached copy
    /// forever. Stamping the on-disk mtime here lets `load` detect drift
    /// and evict before the cached copy is reused.
    item_mtimes: std::collections::HashMap<uuid::Uuid, std::time::SystemTime>,
    video_demuxers: std::collections::HashMap<uuid::Uuid, Mp4Demuxer>,
    /// V4L2 piece 3c: Linux-only V4L2 decoder per Video slide id.
    /// cache.load primes the decoder once on first encounter; piece
    /// 3d's paint_slide drains frames per advance tick.
    ///
    /// TODO(piece 4+): release decoder on cache eviction. Each
    /// primed decoder holds ~5 MB at 320x240 / ~20-25 MB at 1080p
    /// (4+4 buffers × per-plane size). On a 512 MB Pi Zero 2 W
    /// a ~10-slide playlist hits ~250 MB just for decoder buffers.
    /// LRU eviction (or reactive release on slide-leave) needed
    /// before production at scale.
    #[cfg(target_os = "linux")]
    video_decoders: std::collections::HashMap<uuid::Uuid, VideoDecoderState>,
}

impl SlideCache {
    fn new() -> Self {
        Self {
            items: std::collections::HashMap::new(),
            item_mtimes: std::collections::HashMap::new(),
            video_demuxers: std::collections::HashMap::new(),
            #[cfg(target_os = "linux")]
            video_decoders: std::collections::HashMap::new(),
        }
    }

    /// Bug 1 helper: drop every cached artifact for `item_id`. Called
    /// from `load` when the on-disk item.json mtime drifts so the
    /// next load() pass re-reads from disk (text, layout, asset path)
    /// AND re-primes the V4L2 decoder for the case where a Video
    /// slide's asset.mp4 also rotated.
    fn invalidate(&mut self, item_id: uuid::Uuid) {
        self.items.remove(&item_id);
        self.item_mtimes.remove(&item_id);
        self.video_demuxers.remove(&item_id);
        #[cfg(target_os = "linux")]
        self.video_decoders.remove(&item_id);
    }

    /// Try to load + cache a slide by UUID. content_root is
    /// required for the find_*_slide chain. Tries text -> image
    /// -> video. Returns Err with a message if all three return
    /// Ok(None) (unknown type) or any return Err.
    ///
    /// V4L2 piece 3b: on a successful Video load, also open
    /// the asset.mp4 + parse it into an Mp4Demuxer. Failure to
    /// open the MP4 logs a warning + leaves video_demuxers
    /// without an entry; downstream paint paths fall back via
    /// the existing "video slides TBD" wire.
    fn load(&mut self, content_root: &std::path::Path, item_id: uuid::Uuid) -> Result<()> {
        // Bug 1 (qarl 2026-05-16): on-disk mtime check defeats the
        // contains_key short-circuit when the operator edits a slide.
        // backend's PUT /api/content/{text-slides,images,videos}/{id}
        // rewrites <content_root>/<item_id>/item.json with a fresh
        // updated_at; that touches the file's mtime. Pre-fix the
        // sidecar served the pre-edit cached copy forever (no
        // invalidation IPC op, no mtime check). Stat() per BeginSlide
        // is microseconds — negligible vs slide rasterization.
        let item_json_path = content_root.join(item_id.to_string()).join("item.json");
        let on_disk_mtime = std::fs::metadata(&item_json_path)
            .ok()
            .and_then(|m| m.modified().ok());
        if self.items.contains_key(&item_id) {
            if self.item_mtimes.get(&item_id).copied() == on_disk_mtime {
                return Ok(());
            }
            eprintln!(
                "ipc: slide {item_id} item.json drifted on disk; refreshing cache"
            );
            self.invalidate(item_id);
        }
        let loaded = if let Some(s) = find_text_slide(content_root, item_id)? {
            self.items.insert(item_id, ContentItem::Text(s));
            true
        } else if let Some(s) = find_image_slide(content_root, item_id)? {
            self.items.insert(item_id, ContentItem::Image(s));
            true
        } else { false };
        if loaded {
            if let Some(m) = on_disk_mtime {
                self.item_mtimes.insert(item_id, m);
            }
            return Ok(());
        }
        if let Some(s) = find_video_slide(content_root, item_id)? {
            self.items.insert(item_id, ContentItem::Video(s));
            let asset_path = video_slide_asset_path(content_root, item_id);
            match Mp4Demuxer::open(&asset_path) {
                Ok(dem) => {
                    eprintln!(
                        "ipc: opened MP4 for video slide {} ({}x{}, {} samples)",
                        item_id, dem.width, dem.height, dem.samples.len()
                    );
                    // V4L2 piece 3c: on Linux, also prime the
                    // hardware decoder. Failure is best-effort
                    // (warn + fall through to PIL fallback via
                    // the "video slides TBD" wire). Mac: skip.
                    #[cfg(target_os = "linux")]
                    match prime_video_decoder(&dem) {
                        Ok(dec_state) => {
                            self.video_decoders.insert(item_id, dec_state);
                        }
                        Err(e) => {
                            eprintln!(
                                "ipc: warning -- failed to prime V4L2 decoder for video slide {}: {:#}",
                                item_id, e
                            );
                        }
                    }
                    self.video_demuxers.insert(item_id, dem);
                }
                Err(e) => {
                    eprintln!(
                        "ipc: warning -- failed to open MP4 {} for video slide {}: {:#}",
                        asset_path.display(), item_id, e
                    );
                }
            }
            if let Some(m) = on_disk_mtime {
                self.item_mtimes.insert(item_id, m);
            }
            return Ok(());
        }
        Err(anyhow!(
            "no item found for {item_id} under {} (type not text_slide / image / video)",
            content_root.display()
        ))
    }
}

/// V4L2 piece 3c: open + prime an `v4l2::Decoder` against the
/// Pi's bcm2835-codec for a given Mp4Demuxer's stream. Returns a
/// `VideoDecoderState` ready for piece 3d to drain frames from.
///
/// Priming sequence (per bcm2835-codec / V4L2 M2M MPLANE recipe):
///   1. Decoder::open("/dev/video10")
///   2. set_output_format(H264, w, h) -- compressed-in queue
///   3. set_capture_format(NV12, w, h) -- decoded-out queue;
///      negotiated dims may differ from the request (codec
///      rounds to its alignment).
///   4. allocate_buffers(OUTPUT, 4) + allocate_buffers(CAPTURE, 4)
///   5. start_streaming() -- STREAMON OUTPUT then CAPTURE
///   6. feed(sps_pps_annexb) -- header NALs prepended once
///   7. feed(sample[0]) -- first sample (IDR + any pre-IDR NALs)
///
/// Failure at any step bubbles; the cache.load caller swallows
/// to eprintln + falls through to the "video slides TBD" PIL
/// fallback wire.
#[cfg(target_os = "linux")]
fn prime_video_decoder(dem: &Mp4Demuxer) -> Result<VideoDecoderState> {
    use std::path::Path;
    let path = Path::new(V4L2_DECODER_PATH);
    if !path.exists() {
        anyhow::bail!(
            "V4L2 decoder device {} does not exist (no codec driver loaded?)",
            V4L2_DECODER_PATH
        );
    }
    let dec = v4l2::Decoder::open(path)
        .with_context(|| format!("open V4L2 decoder at {}", V4L2_DECODER_PATH))?;
    // V4L2 piece 4d (2026-05-14): opt-in DMA-BUF zero-copy path
    // via env var. Piece 4e smoke shipped GREEN (qa/captures/
    // v4l2-piece4e-dmabuf-smoke-2026-05-14.md, 6.3× mean / 9.1× p50
    // improvement vs MMAP), but the default remained MMAP pending
    // a separate flip decision. Set BEFORE allocate_buffers so
    // REQBUFS uses the right memory type.
    let use_dmabuf = std::env::var("OPENMARQUEE_RENDERER_DMABUF")
        .ok()
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if use_dmabuf {
        dec.set_capture_buffer_type(v4l2::CaptureBufferType::DmaBuf);
    }
    let w = dem.width as u32;
    let h = dem.height as u32;
    let _out_fmt = dec
        .set_output_format(v4l2::V4L2_PIX_FMT_H264, w, h)
        .context("S_FMT OUTPUT (H264)")?;
    let cap_fmt = dec
        .set_capture_format(v4l2::V4L2_PIX_FMT_NV12, w, h)
        .context("S_FMT CAPTURE (NV12)")?;
    // Fail loud if the codec emits FULL_RANGE quantization — the
    // MMAP-path FS_NV12_TO_RGB shader does explicit LIM_RANGE
    // scaling and would crush blacks / clip whites. See
    // `qa/v1-spec-delta-2026-05-14.md` P1.
    let q = dec
        .assert_capture_quantization_compatible()
        .context("CAPTURE quantization compatibility")?;
    eprintln!(
        "v4l2 capture quantization: {} ({})",
        q,
        match q {
            v4l2::V4L2_QUANTIZATION_DEFAULT => "DEFAULT",
            v4l2::V4L2_QUANTIZATION_LIM_RANGE => "LIM_RANGE",
            _ => "?",
        }
    );
    dec.allocate_buffers(v4l2::QueueDirection::Output, 4)
        .context("REQBUFS OUTPUT")?;
    dec.allocate_buffers(v4l2::QueueDirection::Capture, 4)
        .context("REQBUFS CAPTURE")?;
    dec.start_streaming().context("STREAMON")?;
    // Feed the codec headers + first sample as a SINGLE
    // concatenated buffer. `v4l2::Decoder::feed` is single-shot-
    // safe per its docstring -- back-to-back calls collide on
    // OUTPUT buffer index 0 (the second feed clobbers the first
    // before the kernel has dequeued it). The proven-working
    // recipe in v4l2::tests::decode_test_fixture_320x240 feeds
    // the entire Annex-B stream in one call; we mirror that.
    let first_sample = dem
        .samples
        .first()
        .ok_or_else(|| anyhow!("MP4 contains zero samples"))?;
    let header = dem.sps_pps_annexb();
    let mut primer: Vec<u8> = Vec::with_capacity(header.len() + first_sample.len());
    primer.extend_from_slice(&header);
    primer.extend_from_slice(first_sample);
    dec.feed(&primer).context("feed SPS+PPS+IDR primer")?;
    Ok(VideoDecoderState {
        decoder: dec,
        next_sample_idx: 1,
        frames_decoded: 0,
        capture_w: cap_fmt.width,
        capture_h: cap_fmt.height,
    })
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

    // clippy::while_let_on_iterator suggests `for line in lines.by_ref()`,
    // but the inner Open handler below passes `&mut lines` to the inner
    // loop function — `for` + `by_ref()` would hold a borrow across that
    // call (E0499). Keep the while-let form for the outer dispatch.
    #[allow(clippy::while_let_on_iterator)]
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
    for line in lines.by_ref() {
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
        // v1-spec-delta #12 (slice b-3): IPC sidecar [mem]
        // emission. Mirrors the standalone reel's session=
        // open / per-N / session=close cadence using
        // BeginSlide as the natural slide-boundary anchor
        // (Advance fires per-frame, too noisy for slope test).
        crate::mem::log_mem_snapshot("session=open", Some(session.gpu_counters()));
        let mut begin_slide_count = 0_u32;
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        // Phase 9 Step 9a: soak readiness instrumentation.
        // Per-Advance paint timings aggregated into one journald-
        // friendly summary line every 30s. The §11 acceptance test
        // ("30 fps over 6h on FREE YOUR SIGN with shader transitions")
        // is gated by parsing these lines from journald.
        let mut paint_metrics = IpcPaintMetrics::new();
        // v1-spec-delta #10 (slice c): SettingsWatcher polls
        // /var/openmarquee/settings.json (canonical path on
        // dev Pi) for changes between IPC ticks. First check
        // emits the initial state which session.apply_settings
        // wires to the post-pass.
        let mut settings_watcher = SettingsWatcher::new(
            std::path::PathBuf::from("/var/openmarquee/settings.json"),
        );
        if let Some(initial) = settings_watcher.check() {
            session.apply_settings(initial);
        }
        while let Some(line) = lines.next() {
            // Opportunistic settings poll. Cheap stat() call
            // per iteration; the watcher returns None when
            // mtime is unchanged.
            if let Some(updated) = settings_watcher.check() {
                eprintln!(
                    "ipc_sidecar: settings.json changed (brightness={} gamma={:.2}); applying",
                    updated.brightness,
                    updated.gamma,
                );
                session.apply_settings(updated);
            }
            let line = line?;
            let req: IpcRequest = match serde_json::from_str(&line) {
                Ok(r) => r,
                Err(e) => {
                    emit_response(stdout, &err(format!("invalid request: {e}")))?;
                    continue;
                }
            };
            let is_close = matches!(req, IpcRequest::Close);
            let is_begin_slide = matches!(req, IpcRequest::BeginSlide(_));

            // v1-spec-delta #9 (slice e -- Capture wired now;
            // Reconfigure remains architectural-quality defer).
            // Capture intercepts here BEFORE handle_inner_request
            // because the standard dispatch returns "not yet
            // implemented" and we need session+gl to capture.
            if let IpcRequest::Capture(ref p) = req {
                let path = std::path::PathBuf::from(&p.path);
                let resp = capture_current_scene_to_png(
                    session, &cache, &state, fonts, content_root, &path,
                );
                emit_response(stdout, &resp)?;
                continue;
            }

            let resp = handle_inner_request(req, &mut state, &mut cache, content_root);

            // Phase 9 Step 9a: tag the paint kind (if any) BEFORE
            // run_paint_hook so we can wrap the call in wall-clock
            // timing. Non-paint responses (Open, BeginSlide, Idle,
            // etc.) skip the timing wrap entirely.
            let paint_kind = match &resp {
                IpcResponse::Ok { result: OpResult::PaintSlide { .. } } => {
                    Some(IpcPaintKind::Slide)
                }
                IpcResponse::Ok { result: OpResult::PaintTransition { .. } } => {
                    Some(IpcPaintKind::Transition)
                }
                _ => None,
            };
            let paint_start = paint_kind.as_ref().map(|_| std::time::Instant::now());

            // Linux paint hook: when the dispatcher returned a
            // PaintSlide / PaintTransition OpResult, fire the
            // actual GL paint. If paint errors, override the
            // response so the caller sees Err{message} rather
            // than a fake-success response.
            let resp = run_paint_hook(
                &resp,
                session,
                &card,
                &mut cache,
                fonts,
                Some(content_root),
            );

            // Phase 9 Step 9a: record per-Advance paint timing on
            // successful paint. Skipping failures keeps avg/max from
            // being skewed by error-path early returns (which carry
            // no paint work).
            if let (Some(kind), Some(t0)) = (paint_kind, paint_start) {
                if matches!(resp, IpcResponse::Ok { .. }) {
                    let elapsed_us = t0.elapsed().as_micros().min(u64::MAX as u128) as u64;
                    paint_metrics.record(kind, elapsed_us);
                }
            }

            emit_response(stdout, &resp)?;
            // Phase 9 Step 9a: 30s soak summary emit. Cheap when the
            // window hasn't expired (single Instant::elapsed + branch).
            paint_metrics.maybe_emit_summary();
            if is_begin_slide {
                crate::mem::log_mem_snapshot(
                    &format!("begin_slide={begin_slide_count}"),
                    Some(session.gpu_counters()),
                );
                begin_slide_count += 1;
            }
            if is_close {
                crate::mem::log_mem_snapshot("session=close", Some(session.gpu_counters()));
                break;
            }
        }
        Ok(())
    })
}

/// v1-spec-delta #9 (slice e -- Capture op) -- capture the
/// current scene to a PNG using the slice 11 primitives. Re-
/// paints the current slide into the EGL window surface (so
/// the captured PNG matches the most-recent rendered state),
/// reads back via capture_fbo_to_rgba, encodes via
/// rgba_to_png_bytes, writes to disk.
///
/// This re-paint is deliberate: at the IPC tick, the EGL
/// surface holds the LAST swap_buffers content, but we
/// don't track which slide that corresponds to. Re-painting
/// from current state guarantees the snapshot reflects what
/// the caller's most recent Advance produced. Idle / no-
/// current-slide returns Err.
#[cfg(target_os = "linux")]
fn capture_current_scene_to_png(
    session: &mut crate::hdmi::EglSession,
    cache: &SlideCache,
    state: &PlaybackState,
    fonts: Option<&FontCatalog>,
    content_root: &Path,
    png_path: &Path,
) -> IpcResponse {
    use crate::content::ContentItem;
    use crate::hdmi;
    use crate::hdmi_logic::rgba_to_png_bytes;

    let slide = match &state.current {
        Some(c) => c,
        None => return err("Capture: no current slide (begin_slide first?)"),
    };
    let slide_id = slide.slide_id;
    let item = match cache.items.get(&slide_id) {
        Some(i) => i,
        None => return err(format!("Capture: slide {slide_id} not in cache")),
    };
    // Re-paint into the EGL window surface (no commit_fb -- this
    // is offscreen for capture). Then read back.
    //
    // QA-direct (2026-05-13 sidecar feature-gaps slice): ImageSlide
    // case routes through paint_one_image_slide_for_capture, mirror
    // of paint_one_for_capture for the static-PNG case. VideoSlide
    // still TBD (needs V4L2 M2M + dmabuf import).
    // QA-direct (2026-05-13 sidecar error-paths slice):
    // validate inputs via pure-Rust helper first so the
    // Python proxy's error-class dispatch sees a stable
    // wire-format string.
    if let Err(msg) = validate_capture_inputs(item) {
        return err(msg);
    }
    let (mode_w, mode_h) = hdmi::egl_session_mode_size(session);
    let t_in_slide_ms = 0_u64;
    let paint_res = match item {
        ContentItem::Text(text_slide) => hdmi::paint_one_for_capture(
            session,
            text_slide,
            fonts,
            Some(content_root),
            t_in_slide_ms,
        ),
        ContentItem::Image(image_slide) => hdmi::paint_one_image_slide_for_capture(
            session,
            image_slide,
            content_root,
        ),
        ContentItem::Video(_) => {
            // Unreachable: validator rejects Video.
            return err("Capture: VideoSlide capture not implemented (image + text only)");
        }
    };
    if let Err(e) = paint_res {
        return err(format!("Capture: paint failed: {e:#}"));
    }

    let rgba = match hdmi::capture_fbo_to_rgba(session.gl(), None, mode_w, mode_h) {
        Ok(b) => b,
        Err(e) => return err(format!("Capture: read_pixels failed: {e:#}")),
    };
    let png_bytes = match rgba_to_png_bytes(&rgba, mode_w, mode_h) {
        Ok(b) => b,
        Err(e) => return err(format!("Capture: png encode failed: {e:#}")),
    };
    let bytes = png_bytes.len() as u64;
    if let Err(e) = std::fs::write(png_path, &png_bytes) {
        return err(format!("Capture: write {} failed: {e:#}", png_path.display()));
    }
    IpcResponse::Ok {
        result: OpResult::CaptureOk {
            path: png_path.display().to_string(),
            bytes,
        },
    }
}

/// Pure-Rust input validators for the IPC paint/capture
/// ops. These pin the wire-format error strings that the
/// Python proxy matches on for error-class dispatch. Kept
/// separate from the GL-dependent paint helpers so cargo
/// tests can exercise them on Mac without an EGL session.
///
/// QA-direct (2026-05-13 sidecar error-paths slice): every
/// string returned by these validators is a stable contract
/// surface. Don't reword without bumping the Python proxy's
/// error-class dispatch in lock-step.
#[cfg(any(target_os = "linux", test))]
fn validate_paint_slide_inputs(
    item: &ContentItem,
    content_root: Option<&Path>,
) -> Result<(), &'static str> {
    match item {
        ContentItem::Text(_) => Ok(()),
        ContentItem::Image(_) => {
            if content_root.is_none() {
                Err("paint_slide: image_slide requires content_root (--content-root)")
            } else {
                Ok(())
            }
        }
        // V4L2 piece 3e: Video is now a first-class paint target.
        // Capture-side still emits the
        // "VideoSlide capture not implemented" marker (separate
        // arc — Video thumbnails / screenshots are out of scope
        // for the V4L2 paint work). The Python proxy classifier
        // matches on that distinct substring so paint_slide
        // failures (which would be a hard render bug) don't get
        // misclassified as the deferred Capture path. The actual
        // decoder + demuxer must be in SlideCache.video_
        // {decoders, demuxers} for the paint hook to succeed; if
        // they're missing (e.g., asset.mp4 absent / malformed /
        // codec absent at cache.load time), the paint hook returns
        // its own honest error which the Python proxy treats as a
        // hard render failure (no fallback).
        ContentItem::Video(_) => Ok(()),
    }
}

#[cfg(any(target_os = "linux", test))]
fn validate_capture_inputs(item: &ContentItem) -> Result<(), &'static str> {
    match item {
        ContentItem::Text(_) | ContentItem::Image(_) => Ok(()),
        ContentItem::Video(_) => {
            Err("Capture: VideoSlide capture not implemented (image + text only)")
        }
    }
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
    cache: &mut SlideCache,
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
            // Clone the borrow shape we need so we can take a
            // mutable borrow on cache.video_decoders later for
            // the Video branch without re-entering the borrow.
            // Text/Image only need an immutable items lookup.
            let item_kind = match cache.items.get(slide_id) {
                Some(ContentItem::Text(_)) => "text",
                Some(ContentItem::Image(_)) => "image",
                Some(ContentItem::Video(_)) => "video",
                None => {
                    return err(format!(
                        "paint_slide: slide {slide_id} not in cache (begin_slide first?)"
                    ));
                }
            };
            // QA-direct (2026-05-13 sidecar error-paths slice):
            // validate inputs via the pure-Rust helper first.
            // Pins the wire-format errors for the Python proxy's
            // error-class dispatch.
            {
                let item = cache.items.get(slide_id).expect("checked above");
                if let Err(msg) = validate_paint_slide_inputs(item, content_root) {
                    return err(msg);
                }
            }
            match item_kind {
                "text" => {
                    let item = cache.items.get(slide_id).expect("checked above");
                    let slide = match item {
                        ContentItem::Text(s) => s,
                        _ => unreachable!("item_kind matched text"),
                    };
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
                "image" => {
                    // Validator above already enforced content_root
                    // presence; unwrap is safe here.
                    let cr = content_root.expect(
                        "validate_paint_slide_inputs guarantees content_root for Image",
                    );
                    let item = cache.items.get(slide_id).expect("checked above");
                    let slide = match item {
                        ContentItem::Image(s) => s,
                        _ => unreachable!("item_kind matched image"),
                    };
                    if let Err(e) = hdmi::paint_and_present_one_image_slide_frame(
                        session, card, slide, cr,
                    ) {
                        return err(format!("paint_slide (image) failed: {e:#}"));
                    }
                    resp.clone()
                }
                "video" => {
                    // V4L2 piece 3e: drive one frame of decode +
                    // upload + paint per advance tick. Requires
                    // the demuxer + decoder primed in cache.load.
                    let dem = match cache.video_demuxers.get(slide_id) {
                        Some(d) => d,
                        None => {
                            return err(format!(
                                "paint_slide (video): no demuxer for slide {slide_id} (asset.mp4 missing or malformed at begin_slide?)"
                            ));
                        }
                    };
                    let dec_state = match cache.video_decoders.get_mut(slide_id) {
                        Some(d) => d,
                        None => {
                            return err(format!(
                                "paint_slide (video): no V4L2 decoder for slide {slide_id} (codec absent at begin_slide?)"
                            ));
                        }
                    };
                    // Borrow gymnastics: dec_state holds a Decoder
                    // by value; we need &Decoder for the paint call
                    // but also &mut on next_sample_idx +
                    // frames_decoded. Take the indices by &mut and
                    // the decoder by & via splitting the borrow.
                    let frames_decoded_before = dec_state.frames_decoded_for_log();
                    if let Err(e) = hdmi::paint_and_present_one_video_slide_frame(
                        session,
                        card,
                        &dem.samples,
                        &mut dec_state.next_sample_idx,
                        &mut dec_state.frames_decoded,
                        &dec_state.decoder,
                    ) {
                        return err(format!("paint_slide (video) failed: {e:#}"));
                    }
                    if dec_state.frames_decoded > frames_decoded_before {
                        // First-frame log only to avoid spam.
                        if dec_state.frames_decoded == 1 {
                            eprintln!(
                                "ipc: paint_slide (video) {slide_id}: first frame painted (sample idx {})",
                                dec_state.next_sample_idx
                            );
                        }
                    }
                    resp.clone()
                }
                _ => unreachable!("item_kind from match above"),
            }
        }
        OpResult::PaintTransition { from, to, kind, progress } => {
            // Phase 8 slice 6 (2026-05-16): build TransitionEndpoint
            // per-kind from cache state. Video endpoints route their
            // V4L2 decoder state from `cache.video_decoders` +
            // demuxer samples from `cache.video_demuxers` into
            // `TransitionEndpoint::Video`. The dispatcher inside
            // `paint_and_present_one_transition_frame` then forwards
            // into `SlideBakeInputs::Video` and the slice-2 bake
            // helper drains one V4L2 sample per call (Option D
            // cadence per `feedback_motion_through_transitions_
            // required`: video plays through the transition).
            let from_id = *from;
            let to_id = *to;

            // Determine endpoint kinds without holding borrows on
            // cache.items past the kind discriminator (so the
            // subsequent video-state lookups don't conflict).
            let from_kind = match cache.items.get(&from_id) {
                Some(ContentItem::Text(_)) => 't',
                Some(ContentItem::Image(_)) => 'i',
                Some(ContentItem::Video(_)) => 'v',
                None => return err(format!("paint_transition: from slide {from_id} not in cache")),
            };
            let to_kind = match cache.items.get(&to_id) {
                Some(ContentItem::Text(_)) => 't',
                Some(ContentItem::Image(_)) => 'i',
                Some(ContentItem::Video(_)) => 'v',
                None => return err(format!("paint_transition: to slide {to_id} not in cache")),
            };

            // Same-id Video/Video transitions would need two &mut to
            // the same `VideoDecoderState` entry — impossible in safe
            // Rust AND semantically wrong (two drains per Advance
            // call on the same decoder). Bail explicitly. Same-id
            // text/text and image/image are fine (idempotent bakes).
            if from_kind == 'v' && to_kind == 'v' && from_id == to_id {
                return err(format!(
                    "paint_transition: same-id video→video transition not supported (slide_id={from_id})",
                ));
            }

            // Resolve V4L2 decoder state &muts up-front for video
            // endpoints. Single get_mut for single-video; `iter_mut`
            // for dual-video (Rust 1.85 lacks
            // `HashMap::get_disjoint_mut` — stable in 1.86).
            // `iter_mut` yields disjoint &mut per entry, which is
            // what we need; safe-Rust alternative to the unsafe
            // raw-pointer dance.
            let (mut from_dec_state, mut to_dec_state): (
                Option<&mut VideoDecoderState>,
                Option<&mut VideoDecoderState>,
            ) = match (from_kind, to_kind) {
                ('v', 'v') => {
                    let mut a = None;
                    let mut b = None;
                    for (k, v) in cache.video_decoders.iter_mut() {
                        if *k == from_id {
                            a = Some(v);
                        } else if *k == to_id {
                            b = Some(v);
                        }
                        if a.is_some() && b.is_some() {
                            break;
                        }
                    }
                    if a.is_none() {
                        return err(format!(
                            "paint_transition: from video {from_id} decoder state missing",
                        ));
                    }
                    if b.is_none() {
                        return err(format!(
                            "paint_transition: to video {to_id} decoder state missing",
                        ));
                    }
                    (a, b)
                }
                ('v', _) => {
                    let a = cache.video_decoders.get_mut(&from_id);
                    if a.is_none() {
                        return err(format!(
                            "paint_transition: from video {from_id} decoder state missing",
                        ));
                    }
                    (a, None)
                }
                (_, 'v') => {
                    let b = cache.video_decoders.get_mut(&to_id);
                    if b.is_none() {
                        return err(format!(
                            "paint_transition: to video {to_id} decoder state missing",
                        ));
                    }
                    (None, b)
                }
                _ => (None, None),
            };

            // Build TransitionEndpoints. ContentItem refs come from a
            // shared borrow on cache.items (field-disjoint from the
            // &mut video_decoders borrows above). Demuxer samples
            // come from a shared borrow on cache.video_demuxers.
            let endpoint_a = match cache.items.get(&from_id) {
                Some(ContentItem::Text(s)) => hdmi::TransitionEndpoint::Text(s),
                Some(ContentItem::Image(s)) => hdmi::TransitionEndpoint::Image(s),
                Some(ContentItem::Video(_)) => {
                    let demuxer = match cache.video_demuxers.get(&from_id) {
                        Some(d) => d,
                        None => return err(format!(
                            "paint_transition: from video {from_id} demuxer missing",
                        )),
                    };
                    let dec_state =
                        from_dec_state.take().expect("from_dec_state set above for 'v' kind");
                    hdmi::TransitionEndpoint::Video {
                        samples: demuxer.samples.as_slice(),
                        next_sample_idx: &mut dec_state.next_sample_idx,
                        frames_decoded: &mut dec_state.frames_decoded,
                        decoder: &dec_state.decoder,
                    }
                }
                None => unreachable!("from_id presence verified above"),
            };
            let endpoint_b = match cache.items.get(&to_id) {
                Some(ContentItem::Text(s)) => hdmi::TransitionEndpoint::Text(s),
                Some(ContentItem::Image(s)) => hdmi::TransitionEndpoint::Image(s),
                Some(ContentItem::Video(_)) => {
                    let demuxer = match cache.video_demuxers.get(&to_id) {
                        Some(d) => d,
                        None => return err(format!(
                            "paint_transition: to video {to_id} demuxer missing",
                        )),
                    };
                    let dec_state =
                        to_dec_state.take().expect("to_dec_state set above for 'v' kind");
                    hdmi::TransitionEndpoint::Video {
                        samples: demuxer.samples.as_slice(),
                        next_sample_idx: &mut dec_state.next_sample_idx,
                        frames_decoded: &mut dec_state.frames_decoded,
                        decoder: &dec_state.decoder,
                    }
                }
                None => unreachable!("to_id presence verified above"),
            };

            if let Err(e) = hdmi::paint_and_present_one_transition_frame(
                session,
                card,
                endpoint_a,
                endpoint_b,
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
    use crate::content::{ImageSlide, TextSlide, VideoSlide};
    use crate::playback::{
        AdvanceParams, BeginSlideParams, BeginTransitionParams, IpcRequest,
        IpcResponse, OpResult,
    };
    use uuid::Uuid;

    fn uuid(n: u8) -> Uuid {
        Uuid::from_bytes([n; 16])
    }

    fn text_item() -> ContentItem {
        let s: TextSlide = serde_json::from_str(
            r#"{"id":"01010101-0101-0101-0101-010101010101"}"#,
        )
        .unwrap();
        ContentItem::Text(s)
    }

    fn image_item() -> ContentItem {
        let s: ImageSlide = serde_json::from_str(
            r#"{"id":"02020202-0202-0202-0202-020202020202","name":"img"}"#,
        )
        .unwrap();
        ContentItem::Image(s)
    }

    fn video_item() -> ContentItem {
        let s: VideoSlide = serde_json::from_str(
            r#"{"id":"03030303-0303-0303-0303-030303030303","name":"vid"}"#,
        )
        .unwrap();
        ContentItem::Video(s)
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

    // ---- Validator tests (sidecar error-paths slice, 2026-05-13) ----
    //
    // These tests pin the wire-format error strings the Python
    // proxy matches on for error-class dispatch. Any rewording
    // here is a contract break -- bump the Python side in
    // lock-step. The validators are pure-Rust and Mac-testable;
    // the GL-dependent paint helpers live in hdmi.rs (Linux-
    // only) and are exercised via the on-Pi smoke run.

    #[test]
    fn validate_paint_slide_text_with_or_without_content_root_ok() {
        let item = text_item();
        let cr = std::path::PathBuf::from("/tmp/whatever");
        assert!(validate_paint_slide_inputs(&item, Some(&cr)).is_ok());
        assert!(validate_paint_slide_inputs(&item, None).is_ok());
    }

    #[test]
    fn validate_paint_slide_image_with_content_root_ok() {
        let item = image_item();
        let cr = std::path::PathBuf::from("/tmp/whatever");
        assert!(validate_paint_slide_inputs(&item, Some(&cr)).is_ok());
    }

    #[test]
    fn validate_paint_slide_image_missing_content_root_errs_with_stable_string() {
        let item = image_item();
        let err_msg = validate_paint_slide_inputs(&item, None).unwrap_err();
        assert_eq!(
            err_msg,
            "paint_slide: image_slide requires content_root (--content-root)",
            "wire-format error must match exactly (Python proxy dispatches on it)"
        );
    }

    /// V4L2 piece 3e: validate_paint_slide_inputs now accepts
    /// Video (was: rejected with "video slides TBD"). The actual
    /// per-advance decode/paint is run by run_paint_hook against
    /// SlideCache.video_{decoders,demuxers}; the validator no
    /// longer gates Video. This test pins the new contract --
    /// any regression here would silently flip Video back into
    /// the Python proxy's PIL-fallback path.
    #[test]
    fn validate_paint_slide_video_now_accepted() {
        let item = video_item();
        let cr = std::path::PathBuf::from("/tmp/whatever");
        assert!(validate_paint_slide_inputs(&item, Some(&cr)).is_ok());
        // No content_root: still ok -- Video uses asset.mp4 which
        // is resolved via the begin_slide content_root, not the
        // paint-time one.
        assert!(validate_paint_slide_inputs(&item, None).is_ok());
    }

    /// Bug 1 (qarl 2026-05-16): when the on-disk item.json mtime drifts
    /// from the cached value, the next `cache.load` evicts + re-reads
    /// the slide. Pre-fix the `contains_key` short-circuit served the
    /// stale cached copy forever.
    #[test]
    fn cache_load_refreshes_on_item_json_mtime_drift() {
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(7);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        let json_path = dir.join("item.json");
        // First write: name="v1".
        std::fs::write(
            &json_path,
            r##"{
              "schema_version": 3,
              "item": {
                "type": "text_slide",
                "id": "07070707-0707-0707-0707-070707070707",
                "name": "v1",
                "duration_ms": 5000,
                "text_layers": [],
                "background_color": "#222222",
                "background_pattern": null,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("first load");
        match cache.items.get(&id).expect("cached after first load") {
            crate::content::ContentItem::Text(s) => assert_eq!(s.name, "v1"),
            _ => panic!("expected Text"),
        }
        // Rewrite item.json with a different mtime + payload. Sleep
        // long enough that filesystems with second-resolution mtime
        // (HFS+, FAT32) still record a delta.
        std::thread::sleep(std::time::Duration::from_millis(1100));
        std::fs::write(
            &json_path,
            r##"{
              "schema_version": 3,
              "item": {
                "type": "text_slide",
                "id": "07070707-0707-0707-0707-070707070707",
                "name": "v2",
                "duration_ms": 5000,
                "text_layers": [],
                "background_color": "#222222",
                "background_pattern": null,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        // Pre-bug-1 the cache would short-circuit and serve v1.
        cache.load(td.path(), id).expect("second load after edit");
        match cache.items.get(&id).expect("still cached") {
            crate::content::ContentItem::Text(s) => {
                assert_eq!(s.name, "v2", "mtime drift must trigger refresh");
            }
            _ => panic!("expected Text"),
        }
    }

    /// Bug 1 sibling: a second `cache.load` with NO disk change must
    /// still short-circuit (no spurious re-reads). Guards against a
    /// regression where the mtime-aware path always refreshes.
    #[test]
    fn cache_load_short_circuits_when_mtime_unchanged() {
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(8);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("item.json"),
            r##"{
              "schema_version": 3,
              "item": {
                "type": "text_slide",
                "id": "08080808-0808-0808-0808-080808080808",
                "name": "stable",
                "duration_ms": 5000,
                "text_layers": [],
                "background_color": "#222222",
                "background_pattern": null,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("first load");
        let mtime_after_first = cache.item_mtimes.get(&id).copied();
        // Load again without touching disk. The cached entry stays put.
        cache.load(td.path(), id).expect("second load");
        assert_eq!(cache.item_mtimes.get(&id).copied(), mtime_after_first);
        match cache.items.get(&id).unwrap() {
            crate::content::ContentItem::Text(s) => assert_eq!(s.name, "stable"),
            _ => panic!("expected Text"),
        }
    }

    /// V4L2 piece 3b: when a VideoSlide loads + the asset.mp4 is
    /// present + parses cleanly, the SlideCache stores an
    /// Mp4Demuxer indexed by slide id. Piece 3c will consume it
    /// at paint_slide time.
    #[test]
    fn cache_load_video_populates_demuxer_when_asset_present() {
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(3);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("item.json"),
            r##"{
              "schema_version": 3,
              "item": {
                "type": "video",
                "id": "03030303-0303-0303-0303-030303030303",
                "name": "vid",
                "duration_ms": 2000,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        // Copy the committed MP4 fixture into the temp item dir.
        let fixture = {
            let mut p = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            p.push("tests");
            p.push("fixtures");
            p.push("test_320x240.mp4");
            p
        };
        std::fs::copy(&fixture, dir.join("asset.mp4")).unwrap();
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("cache.load");
        assert!(cache.items.contains_key(&id), "Video item must be in items");
        let dem = cache.video_demuxers.get(&id)
            .expect("Mp4Demuxer must be in video_demuxers when asset present");
        assert_eq!(dem.width, 320);
        assert_eq!(dem.height, 240);
        assert!(!dem.samples.is_empty());
    }

    /// V4L2 piece 3c (Linux-gated): when the dev Pi V4L2 codec
    /// is present, cache.load also primes the v4l2::Decoder and
    /// records it in video_decoders. Skipped without
    /// /dev/video10. Exercises the full open-format-buffers-
    /// stream-on-feed-headers path on real hardware.
    #[test]
    #[cfg(target_os = "linux")]
    fn cache_load_video_primes_v4l2_decoder_on_linux() {
        if !std::path::Path::new(V4L2_DECODER_PATH).exists() {
            eprintln!("skipping: {} absent (no codec driver)", V4L2_DECODER_PATH);
            return;
        }
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(5);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("item.json"),
            r##"{
              "schema_version": 3,
              "item": {
                "type": "video",
                "id": "05050505-0505-0505-0505-050505050505",
                "name": "vid-linux",
                "duration_ms": 2000,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        let fixture = {
            let mut p = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            p.push("tests");
            p.push("fixtures");
            p.push("test_320x240.mp4");
            p
        };
        std::fs::copy(&fixture, dir.join("asset.mp4")).unwrap();
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("cache.load");
        assert!(cache.items.contains_key(&id), "Video item must be in items");
        assert!(cache.video_demuxers.contains_key(&id), "Demuxer must be in video_demuxers");
        let dec_state = cache.video_decoders.get(&id)
            .expect("Decoder must be primed in video_decoders on Linux");
        assert_eq!(dec_state.capture_w, 320, "negotiated capture width");
        assert_eq!(dec_state.capture_h, 240, "negotiated capture height");
        assert_eq!(dec_state.next_sample_idx, 1,
            "priming should have consumed sample 0; next_sample_idx = 1");
    }

    /// V4L2 piece 3b: when asset.mp4 is missing or malformed,
    /// cache.load still succeeds (Video item is in items),
    /// video_demuxers is left empty for that id, and the
    /// "video slides TBD" PIL-fallback path stays usable.
    #[test]
    fn cache_load_video_tolerates_missing_asset() {
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(4);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("item.json"),
            r##"{
              "schema_version": 3,
              "item": {
                "type": "video",
                "id": "04040404-0404-0404-0404-040404040404",
                "name": "vid-no-asset",
                "duration_ms": 2000,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        // Deliberately no asset.mp4.
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("cache.load should succeed without asset");
        assert!(cache.items.contains_key(&id), "Video item still in items");
        assert!(!cache.video_demuxers.contains_key(&id),
            "video_demuxers must be empty when asset missing");
    }

    #[test]
    fn validate_capture_text_and_image_ok() {
        assert!(validate_capture_inputs(&text_item()).is_ok());
        assert!(validate_capture_inputs(&image_item()).is_ok());
    }

    #[test]
    fn validate_capture_video_errs_with_stable_string() {
        let err_msg = validate_capture_inputs(&video_item()).unwrap_err();
        assert_eq!(
            err_msg,
            "Capture: VideoSlide capture not implemented (image + text only)",
            "wire-format error must match exactly"
        );
    }

    // Phase 8 slice 5 (2026-05-16): the previous 6 validator-level
    // tests (`validate_paint_transition_text_to_text_ok` etc.) are
    // gone alongside `validate_paint_transition_endpoints`. The
    // gate is now removed in production: image/text/image-image
    // endpoint pairs route fully through the Rust shader
    // transition; video endpoints still bail in
    // `hdmi::resolve_transition_endpoint` with a message
    // containing "non-text slide TBD" (the
    // `_UNSUPPORTED_SLIDE_WIRE_MARKERS` substring from
    // backend/openmarquee/rendering/rust_renderer.py:227-230).
    // Slice 6 removes that bail when the V4L2 decoder state is
    // wired into SlideBakeInputs::Video.
    //
    // No unit test for the bail substring this slice because
    // `crate::hdmi` is `#[cfg(target_os = "linux")]`-gated at the
    // module level (main.rs L18-19) and exposing
    // `resolve_transition_endpoint` (or `TransitionEndpointData`,
    // whose `bg: BgKind` would also need to surface) widens
    // pub(crate) for a slice-5-only test. The marker contract is
    // documented inline at the bail site in hdmi.rs +
    // ipc_main.rs's PaintTransition handler comment. Slice 6
    // deletes the bail, so any test asserting its substring would
    // delete with it — net churn.

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

    // Phase D slice 1 (2026-05-17) — IpcPaintMetrics p99 sampling.

    #[test]
    fn paint_metrics_records_into_sample_buffer() {
        let mut m = IpcPaintMetrics::new();
        assert!(m.paint_us_samples.is_empty());
        m.record(IpcPaintKind::Slide, 1000);
        m.record(IpcPaintKind::Transition, 2000);
        m.record(IpcPaintKind::Slide, 3000);
        assert_eq!(m.paint_us_samples, vec![1000, 2000, 3000]);
        assert_eq!(m.frames, 2);
        assert_eq!(m.transitions, 1);
        assert_eq!(m.session_frames, 2);
        assert_eq!(m.session_transitions, 1);
    }

    #[test]
    fn paint_metrics_caps_sample_buffer_at_capacity() {
        // Cap-and-drop: pushing past PAINT_SAMPLE_CAP must not grow
        // unbounded. Session counters keep counting; samples ignore
        // the overflow. This is the documented degenerate-burst
        // behavior.
        let mut m = IpcPaintMetrics::new();
        let extra = 50;
        for i in 0..(PAINT_SAMPLE_CAP + extra) {
            m.record(IpcPaintKind::Slide, (i as u64) + 1);
        }
        assert_eq!(m.paint_us_samples.len(), PAINT_SAMPLE_CAP);
        // First PAINT_SAMPLE_CAP samples are retained (first-N
        // policy); overflow drops the late arrivals.
        assert_eq!(m.paint_us_samples[0], 1);
        assert_eq!(m.paint_us_samples[PAINT_SAMPLE_CAP - 1], PAINT_SAMPLE_CAP as u64);
        // Session counter sees all records, including dropped ones.
        assert_eq!(m.session_frames as usize, PAINT_SAMPLE_CAP + extra);
    }

    /// Compute the p99 of a sample slice using the same percentile
    /// math as the production code. Mirrors profile.rs::summarize_
    /// samples so the test is independent of any future refactor of
    /// the wrapper (it would catch a wire-up regression even if the
    /// inner math changed).
    fn p99_of(samples: &[u64]) -> u64 {
        let (_, _, _, _, p99, _) = crate::profile::summarize_samples(samples);
        p99
    }

    #[test]
    fn paint_metrics_p99_reflects_spike_tier() {
        // 900-sample window per the dispatch spec: 891 fast paints
        // (1000us = 1ms) + 9 slow paints (50000us = 50ms). p99 must
        // land on the spike tier (>= 50000) because the top 1% of
        // 900 samples = 9 entries, all of which are the spike.
        let mut m = IpcPaintMetrics::new();
        for _ in 0..891 {
            m.record(IpcPaintKind::Slide, 1000);
        }
        for _ in 0..9 {
            m.record(IpcPaintKind::Slide, 50_000);
        }
        let p99 = p99_of(&m.paint_us_samples);
        assert_eq!(
            p99, 50_000,
            "p99 of 891x1ms + 9x50ms must report the spike tier, not the base; \
             a regression that under-counts spikes would show p99 in the 1ms range"
        );
    }

    #[test]
    fn paint_metrics_p99_handles_empty_window() {
        // Empty sample slice -> summarize_samples returns all
        // zeros. The emitted line shows paint_us_p99=0, which the
        // soak parser treats as the "no data" sentinel (the avg/max
        // fields also degenerate to 0 in this case via the existing
        // `total_calls > 0` guard).
        let m = IpcPaintMetrics::new();
        assert!(m.paint_us_samples.is_empty());
        assert_eq!(p99_of(&m.paint_us_samples), 0);
    }

    #[test]
    fn paint_metrics_p99_underfull_window() {
        // Small windows (e.g. first emission after process start
        // with only a handful of paints) must still compute a
        // sensible p99 without panicking on the .min(n-1) index.
        let mut m = IpcPaintMetrics::new();
        for v in [10_u64, 20, 30, 40, 50] {
            m.record(IpcPaintKind::Slide, v);
        }
        // 5 samples, p99 index = (5 * 99 / 100).min(4) = 4 -> 50us.
        assert_eq!(p99_of(&m.paint_us_samples), 50);
    }

    #[test]
    fn paint_metrics_record_does_not_skew_avg_or_max_with_zero_samples() {
        // Defensive: recording a zero-us paint (degenerate but
        // representable) must update sample buffer and counters
        // without breaking the avg/max invariants used by
        // maybe_emit_summary.
        let mut m = IpcPaintMetrics::new();
        m.record(IpcPaintKind::Slide, 0);
        m.record(IpcPaintKind::Slide, 100);
        assert_eq!(m.paint_us_samples, vec![0, 100]);
        assert_eq!(m.max_paint_us, 100);
        assert_eq!(m.total_paint_us, 100);
    }
}
