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
use crate::lru::LruMap;
use crate::mp4_demux::Mp4Demuxer;
use crate::playback::{
    advance_command_to_op_result, AdvanceCommand, IpcRequest, IpcResponse, OpResult,
    OpenParams, PlaybackState,
};
// ExternalPixelFormat is consumed only by the Linux-only external-
// frame pump; cfg-gate the import so macOS test builds don't warn.
#[cfg(target_os = "linux")]
use crate::playback::ExternalPixelFormat;
#[cfg(target_os = "linux")]
use crate::hdmi_logic::FontCatalog;
#[cfg(target_os = "linux")]
use crate::v4l2;

/// QA H2 (2026-05-23): `V4L2_DECODER_PATH` + `VideoDecoderState` +
/// `prime_video_decoder` lifted to `crate::video_decode` so the
/// standalone `--play-reel` driver can dispatch them too. Re-export
/// the linux-gated names locally for backwards-compatible references
/// from existing call sites + tests in this file.
#[cfg(target_os = "linux")]
use crate::video_decode::{prime_video_decoder, VideoDecoderState, V4L2_DECODER_PATH};

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
    // r110 stage 2 (2026-06-11): snapshot of the
    // `hdmi::BAKE_VIDEO_OK_NONE_*` counters at the END of the
    // PRIOR `maybe_emit_summary` call. Used to compute per-30s-
    // window deltas for the `ipc.soak` line. Establishes the
    // r103.1 baseline for QA's H4 (r106 Ok(None)-steady-state-
    // lag) verification.
    last_bake_ok_none_eagain: u64,
    last_bake_ok_none_eos: u64,
    // `[perf]` r2: latch so the warn about a failing perf-stats
    // sidecar write (permission denied on a dev box, /var/openmarquee
    // missing, disk full, etc.) fires at most once per session
    // lifetime instead of every 30s window. The eprintln line is the
    // canonical operational signal — the JSON sidecar is an
    // operator-affordance, best-effort.
    perf_json_write_warned: bool,
}

/// Bound for `IpcPaintMetrics::paint_us_samples`. 2048 entries at
/// 8 bytes = 16 KB; comfortably fits within the Pi Zero memory
/// budget (§8.1) and gives 2.3× headroom over the expected
/// 30 fps × 30 s = 900 samples per window.
const PAINT_SAMPLE_CAP: usize = 2048;

/// `[perf]` r2 (2026-05-26): canonical path for the perf-stats JSON
/// sidecar written every 30s alongside the `ipc.soak` eprintln. Same
/// `/var/openmarquee/` parent as `settings.json` (see
/// `[[project_dev_pi_provisioned]]`). The backend's
/// `/api/playback/perf/stats` route reads this file and forwards the
/// content to the UI perf overlay.
const PERF_STATS_JSON_PATH: &str = "/var/openmarquee/perf-stats.json";

/// `[perf]` r2: shape of the perf-stats sidecar written by
/// `maybe_emit_summary`. Mirrors the keys of the `ipc.soak` eprintln
/// line for parity (operator can grep journalctl for the same field
/// names) and adds `timestamp_unix_s` so a downstream reader (backend
/// endpoint, UI overlay) can detect staleness against wall clock.
///
/// The shape is the wire contract with the backend's
/// `/api/playback/perf/stats` route and the UI's `perf-overlay`
/// module — changing a field name here requires updating both
/// consumers in lockstep.
#[derive(serde::Serialize)]
struct PerfStatsJson {
    window_s: u64,
    frames: u64,
    transitions: u64,
    fps_avg: f64,
    paint_us_avg: u64,
    paint_us_max: u64,
    paint_us_p99: u64,
    session_frames: u64,
    session_transitions: u64,
    frames_observed_total: u64,
    frames_over_budget_total: u64,
    timestamp_unix_s: u64,
}

/// `[perf]` r2: atomic write via `.tmp` + rename. The rename is
/// atomic on the same filesystem (POSIX), so the backend reader will
/// r97 (2026-06-09): predicate for the conditional deferred-preload
/// path in the `PreloadSlide` IPC arm. Returns `true` iff BOTH:
///   - at least one V4L2 decoder is currently live (contention is
///     real — the new decoder would have to share the codec VPU
///     with the existing one), AND
///   - the incoming preload's slide has a video background (so the
///     spawn would actually open a decoder, not just load items).
///
/// The 4-way truth table:
///
/// | active_decoder_count | bg_is_video | should_defer |
/// |----------------------|-------------|--------------|
/// | 0                    | false       | false        |
/// | 0                    | true        | false        |
/// | >= 1                 | false       | false        |
/// | >= 1                 | true        | **true**     |
///
/// The Pi Zero 2 W bcm2835-codec VPU ceiling for two simultaneous
/// 1080p30 decodes drove this. See `docs/hardware-ceilings.md` for
/// the empirical characterisation. Pure function so unit tests
/// cover the 4 combinations on every host (the `PreloadSlide` arm
/// is Linux-gated; this predicate is not).
pub fn should_defer_preload_for_codec_contention(
    active_decoder_count: usize,
    bg_is_video: bool,
) -> bool {
    active_decoder_count >= 1 && bg_is_video
}

/// r98 (2026-06-09): preload scheduling mode, set via
/// `OPENMARQUEE_PRELOAD_MODE` env var. Three modes — see
/// `parse_preload_mode` for parsing semantics and
/// `docs/hardware-ceilings.md` for the dual-1080p design-space
/// rationale.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PreloadMode {
    /// r97 behavior (default): skip preload when an active video
    /// decoder is live AND the incoming preload's bg is video. The
    /// Python playback loop fires PreloadSlide at the canonical 1.0s
    /// before slide-end; the Rust IPC arm short-circuits to defer.
    Defer,
    /// Force preload through (Rust skips the defer check). Python
    /// reads `OPENMARQUEE_PRELOAD_LEAD_MS` for the time-before-
    /// slide-end trigger. Pairs the rust + python sides to let QA
    /// sweep "what lead time gives the contended second decoder
    /// enough VPU cycles to have a first frame ready by
    /// BeginTransition?"
    Lead,
    /// Force preload through (Rust skips the defer check). Python
    /// fires PreloadSlide for slide N immediately after slide N-1's
    /// `begin_slide_load` event — the maximum possible lead time
    /// for any playlist structure. `OPENMARQUEE_PRELOAD_LEAD_MS` is
    /// IGNORED in this mode.
    Max,
}

/// r98 (2026-06-09): parse `OPENMARQUEE_PRELOAD_MODE` into the enum.
/// Accepts "defer" / "lead" / "max" (case-insensitive). `None` /
/// empty / unrecognised → warn + default to `Defer` (r97 behavior).
///
/// Parameter `raw` exists for unit-testability; production callers
/// pass `std::env::var("OPENMARQUEE_PRELOAD_MODE").ok().as_deref()`.
pub fn parse_preload_mode(raw: Option<&str>) -> PreloadMode {
    match raw {
        None => PreloadMode::Defer,
        Some(s) => {
            let normalized = s.trim();
            if normalized.is_empty() {
                return PreloadMode::Defer;
            }
            if normalized.eq_ignore_ascii_case("defer") {
                PreloadMode::Defer
            } else if normalized.eq_ignore_ascii_case("lead") {
                PreloadMode::Lead
            } else if normalized.eq_ignore_ascii_case("max") {
                PreloadMode::Max
            } else {
                eprintln!(
                    "warn: OPENMARQUEE_PRELOAD_MODE={normalized:?} not a \
                     recognised mode (expected defer/lead/max); using default \
                     'defer' (r97 behavior)"
                );
                PreloadMode::Defer
            }
        }
    }
}

/// r98 (2026-06-09): hot-path reader. The PreloadSlide IPC arm
/// consults this on every defer-eligible preload to decide whether
/// the r97 defer check should run. `std::env::var` is a libc
/// `getenv` shared-state lookup; per-call cost is negligible.
pub fn preload_mode() -> PreloadMode {
    let raw = std::env::var("OPENMARQUEE_PRELOAD_MODE").ok();
    parse_preload_mode(raw.as_deref())
}

/// never observe a partial JSON file even if the renderer crashes
/// mid-write. The `.tmp` is co-located in the same directory as the
/// target so the rename stays atomic.
fn write_perf_stats_json_atomic(
    path: &Path,
    json: &str,
) -> std::io::Result<()> {
    let mut tmp_str = path.as_os_str().to_owned();
    tmp_str.push(".tmp");
    let tmp_path = PathBuf::from(tmp_str);
    std::fs::write(&tmp_path, json)?;
    // If the rename fails (e.g. EXDEV from a cross-filesystem bind
    // mount, or a permission flip on the parent dir between write
    // and rename), best-effort remove the orphaned `.tmp` so it
    // doesn't pile up across retries. Ignore the removal result —
    // we're already returning an Err on the rename.
    if let Err(e) = std::fs::rename(&tmp_path, path) {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(e);
    }
    Ok(())
}

/// `[perf]` r2: wall-clock unix epoch seconds for the sidecar
/// `timestamp_unix_s` field. `SystemTime` (NOT `Instant`) because
/// the consumer cares about absolute wall time for staleness
/// computation. Saturating-to-0 on a system clock anomaly (would
/// only happen if the Pi's RTC is unset and the system thinks it's
/// pre-1970 — rare; the field would read as 0 which the backend can
/// treat as "unknown freshness").
fn current_unix_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

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
            last_bake_ok_none_eagain: 0,
            last_bake_ok_none_eos: 0,
            perf_json_write_warned: false,
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

    /// `[perf]` r1 (2026-05-26) addendum: `frames_observed_total`
    /// and `frames_over_budget_total` snapshot the session-cumulative
    /// counters maintained on `EglSession::record_present` (hdmi.rs).
    /// Caller passes them in because the deadline-miss counter lives
    /// on the session, not on IpcPaintMetrics (the chokepoint is
    /// `commit_fb`, which is in hdmi.rs and doesn't see the IPC
    /// metrics struct). Both numbers are monotonically non-decreasing
    /// across the session; downstream can diff two consecutive
    /// summaries to compute a window-rate. New fields go on the right
    /// per the soak-parser regex-by-key convention.
    fn maybe_emit_summary(
        &mut self,
        frames_observed_total: u64,
        frames_over_budget_total: u64,
    ) {
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
        // + unit-tested). Empty sample slice returns p99_ns=0 --
        // correct "no data" sentinel for windows with no successful
        // paints. _ns-suffixed PhaseStats field names anchor the
        // unit-of-measure at the type level.
        let stats = crate::profile::summarize_samples(&self.paint_us_samples);
        // Cherry-pick fix (2026-05-26): code2's PerfStatsJson struct
        // creation below references `paint_us_p99` as a bare binding
        // but the prior code only inlined `stats.p99_ns` in the
        // eprintln. Bind it explicitly so both consumers share.
        let paint_us_p99 = stats.p99_ns;
        // r110 stage 2 (2026-06-11): per-30s-window deltas for
        // `bake_video_slide_to_current_fbo` Ok(None) returns.
        // `bake_ok_none_eagain` = the 10x3ms inner DQBUF loop
        // exhausted without a frame (lag indicator); `_eos` =
        // normal end-of-clip cycle event. On r103.1 baseline,
        // `_eagain` should be ≈ 0 in steady-state — non-zero
        // values would empirically validate QA's H4 (r106
        // Ok(None)-steady-state-lag) and tell us r106's
        // non-blocking pattern is necessary even at r103.1.
        // `crate::hdmi` is Linux-cfg-gated (links drm/gbm/EGL);
        // non-Linux dev builds report 0 for these counters, which
        // is correct because bake_video doesn't run there either.
        #[cfg(target_os = "linux")]
        let bake_ok_none_eagain_total = crate::hdmi::BAKE_VIDEO_OK_NONE_EAGAIN
            .load(std::sync::atomic::Ordering::Relaxed);
        #[cfg(target_os = "linux")]
        let bake_ok_none_eos_total = crate::hdmi::BAKE_VIDEO_OK_NONE_EOS
            .load(std::sync::atomic::Ordering::Relaxed);
        #[cfg(not(target_os = "linux"))]
        let bake_ok_none_eagain_total: u64 = 0;
        #[cfg(not(target_os = "linux"))]
        let bake_ok_none_eos_total: u64 = 0;
        let bake_ok_none_eagain_30s =
            bake_ok_none_eagain_total.saturating_sub(self.last_bake_ok_none_eagain);
        let bake_ok_none_eos_30s =
            bake_ok_none_eos_total.saturating_sub(self.last_bake_ok_none_eos);
        self.last_bake_ok_none_eagain = bake_ok_none_eagain_total;
        self.last_bake_ok_none_eos = bake_ok_none_eos_total;
        eprintln!(
            "ipc.soak window_s={} frames={} transitions={} fps_avg={:.1} paint_us=avg/{}/max/{} paint_us_p99={} session_frames={} session_transitions={} frames_observed_total={} frames_over_budget_total={} bake_ok_none_eagain_30s={} bake_ok_none_eos_30s={}",
            elapsed.as_secs(),
            self.frames,
            self.transitions,
            fps_avg,
            avg_us,
            self.max_paint_us,
            paint_us_p99,
            self.session_frames,
            self.session_transitions,
            frames_observed_total,
            frames_over_budget_total,
            bake_ok_none_eagain_30s,
            bake_ok_none_eos_30s,
        );

        // `[perf]` r2 (2026-05-26): write the same fields to the JSON
        // sidecar at /var/openmarquee/perf-stats.json so the backend's
        // /api/playback/perf/stats route can surface the data to the
        // operator-facing UI perf overlay. Atomic write (.tmp +
        // rename) — backend never reads a partial file.
        //
        // Best-effort: on dev boxes (path missing, perms wrong) the
        // write fails; we latch a single warn-line per session and
        // continue. The eprintln above is the canonical operational
        // signal — the JSON sidecar is an operator-affordance, not
        // load-bearing for renderer correctness.
        let perf_json = PerfStatsJson {
            window_s: elapsed.as_secs(),
            frames: self.frames,
            transitions: self.transitions,
            fps_avg,
            paint_us_avg: avg_us,
            paint_us_max: self.max_paint_us,
            paint_us_p99,
            session_frames: self.session_frames,
            session_transitions: self.session_transitions,
            frames_observed_total,
            frames_over_budget_total,
            timestamp_unix_s: current_unix_seconds(),
        };
        match serde_json::to_string(&perf_json) {
            Ok(json) => {
                let path = Path::new(PERF_STATS_JSON_PATH);
                if let Err(e) = write_perf_stats_json_atomic(path, &json) {
                    if !self.perf_json_write_warned {
                        eprintln!(
                            "warn: failed to write perf stats sidecar {}: {}; suppressing further warnings this session",
                            PERF_STATS_JSON_PATH, e,
                        );
                        self.perf_json_write_warned = true;
                    }
                }
            }
            Err(e) => {
                if !self.perf_json_write_warned {
                    eprintln!(
                        "warn: failed to serialize perf stats sidecar: {}; suppressing further warnings this session",
                        e,
                    );
                    self.perf_json_write_warned = true;
                }
            }
        }

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
/// for `items`; the paint_slide path falls through to the
/// UnsupportedSlide wire, raising the Python proxy's
/// `RustRendererUnsupportedSlideError` (the playback loop then
/// logs + skips, per `playback.py` post-DELETE-PIL contract).
/// LRU cap on `SlideCache.items` + `SlideCache.item_mtimes`. Pre-cap
/// these maps were insert-only -- every slide ever shown stayed
/// resident, so a long playlist or operator-edit churn over hours/days
/// grew memory without bound. Same latent-OOM shape as the V4L2
/// decoder leak fixed in FYS bug 9 (2026-05-20), which only protected
/// `video_demuxers` + `video_decoders` and left the `ContentItem`
/// bodies + mtime stamps unbounded.
///
/// 32 is ~4x typical demo playlist length (so an entire short
/// playlist stays warm), small enough that a pathological 100-slide
/// case bounds memory to ~32 ContentItems (a couple MB worst case for
/// Text slides with full body strings), and large enough that the
/// touch-on-get behavior of LruMap keeps the actively-cycling slides
/// resident even when intermittent BeginSlides bring in cold ones.
const SLIDE_CACHE_CAP: usize = 32;

struct SlideCache {
    items: LruMap<uuid::Uuid, ContentItem>,
    /// Bug 1 (qarl 2026-05-16): item.json mtime per cached slide.
    /// `cache.load` short-circuits on `items` membership, which means
    /// a content edit (text change, image re-upload, etc.) never reaches
    /// the running show — the sidecar serves the pre-edit cached copy
    /// forever. Stamping the on-disk mtime here lets `load` detect drift
    /// and evict before the cached copy is reused.
    item_mtimes: LruMap<uuid::Uuid, std::time::SystemTime>,
    video_demuxers: std::collections::HashMap<uuid::Uuid, Mp4Demuxer>,
    /// Bug 8 / Fix A (2026-05-17): video slide ids whose cache.load
    /// could NOT register a demuxer (multi-trak MP4, malformed file,
    /// missing asset, etc.). Subsequent BeginSlide/BeginTransition
    /// for these ids returns the UnsupportedSlide wire marker so
    /// Python's existing `RustRendererUnsupportedSlideError` rail
    /// catches them cleanly (log INFO + skip + continue) instead of
    /// failing later at paint_slide with a generic OpError that hot-
    /// spins the playback loop. Cleared by `invalidate()` so an
    /// item.json mtime drift (e.g. asset rotation) gets a fresh
    /// load attempt.
    video_skip: std::collections::HashSet<uuid::Uuid>,
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
    /// r65 (2026-06-05): in-flight async preload thread handles
    /// keyed by the slide_id the worker is priming. Populated by
    /// the PreloadSlide IPC handler, drained by
    /// `ensure_preload_complete` at the head of BeginSlide and
    /// BeginTransition (where the caller WILL touch the cache
    /// entries the worker produced).
    ///
    /// The worker thread runs `preload_in_worker` -- a Send-safe
    /// function that does the expensive bits (`Mp4Demuxer::open`
    /// + `prime_video_decoder` for Video / TextSlide-with-bg-video
    /// kinds) WITHOUT touching this SlideCache. On finish, the
    /// worker returns `PreloadArtifacts` (a Send bag of
    /// `ContentItem` + `Mp4Demuxer` + `VideoDecoderState`) which
    /// the main thread installs into items/demuxers/decoders.
    ///
    /// Pre-r65, PreloadSlide called cache.load synchronously on
    /// the main thread, blocking the IPC + paint loops for the
    /// 200-600 ms V4L2 bring-up time. qarl observed this as a
    /// "stall exactly 2 s before each transition" -- exactly
    /// when preload_lead_seconds=2.0 fired the backend's
    /// PreloadSlide. r65 offloads the work; main thread returns
    /// from PreloadSlide in microseconds.
    pending_preloads: std::collections::HashMap<
        uuid::Uuid,
        PreloadHandle,
    >,
    /// r110 c3.3.2 (2026-06-11): in-flight async recreate workers,
    /// keyed by the video_id being re-primed. The c3.3.1 BeginSlide
    /// predicate (poster_was_sourced_for + 1080p dimension gate)
    /// detects "decoder wedged during transition"; c3.3.2 spawns
    /// preload_in_worker into this map instead of running cache.load
    /// synchronously (which costs ~4s at 1080p and trips the backend
    /// IPC deadline → SIGTERM). The wedged decoder STAYS in
    /// video_decoders during the spin-up window so the slide-hold
    /// paint doesn't crash on missing-decoder; it produces Ok(None)
    /// which the paint path handles as "no new frame this tick" and
    /// the c3.2.2 poster-painted glass holds. When the worker
    /// finishes, `try_drain_finished_recreates` (called at the top
    /// of handle_inner_request) installs the fresh decoder,
    /// replacing the wedged one. First decoded frame == frame 0 ==
    /// poster (c3.1.1 BT.709 contract) → invisible handoff.
    #[cfg(target_os = "linux")]
    pending_recreates: std::collections::HashMap<
        uuid::Uuid,
        PreloadHandle,
    >,
}

/// r65 (2026-06-05): handle stored in `SlideCache.pending_preloads`
/// for an in-flight worker. The thread's JoinHandle yields a
/// `PreloadResult` on .join().
struct PreloadHandle {
    thread: std::thread::JoinHandle<PreloadResult>,
    enqueued_at: std::time::Instant,
}

/// r65: per-slide artifacts produced by `preload_in_worker`.
/// Send-safe so the worker can build them up + ship them across
/// the join boundary. Recursive TextSlide-with-bg-video preloads
/// pack the bg-video's artifacts into the same struct via the
/// flat Vec layout below.
struct PreloadArtifacts {
    /// (slide_id, ContentItem, optional mtime). Multiple entries
    /// when a TextSlide-with-bg-video preload also yielded the
    /// underlying VideoSlide.
    items: Vec<(uuid::Uuid, ContentItem, Option<std::time::SystemTime>)>,
    /// (slide_id, Mp4Demuxer). One per Video kind encountered.
    demuxers: Vec<(uuid::Uuid, Mp4Demuxer)>,
    /// Linux-only: (slide_id, VideoDecoderState). One per
    /// Video kind that primed successfully.
    #[cfg(target_os = "linux")]
    decoders: Vec<(uuid::Uuid, VideoDecoderState)>,
    /// Slide-ids whose load found a JSON but the demuxer / V4L2
    /// prime failed. Main thread inserts these into video_skip
    /// so the existing UnsupportedSlide rail picks them up.
    skip_marks: Vec<uuid::Uuid>,
}

type PreloadResult = anyhow::Result<PreloadArtifacts>;

impl SlideCache {
    fn new() -> Self {
        Self {
            items: LruMap::with_capacity(SLIDE_CACHE_CAP),
            item_mtimes: LruMap::with_capacity(SLIDE_CACHE_CAP),
            video_demuxers: std::collections::HashMap::new(),
            video_skip: std::collections::HashSet::new(),
            #[cfg(target_os = "linux")]
            video_decoders: std::collections::HashMap::new(),
            pending_preloads: std::collections::HashMap::new(),
            #[cfg(target_os = "linux")]
            pending_recreates: std::collections::HashMap::new(),
        }
    }

    /// Is the V4L2 decoder for `item_id` "satisfied" for re-prime
    /// purposes?
    ///
    /// Linux: true iff `video_decoders` holds a primed entry.
    /// Non-Linux: there is no `video_decoders` field and no decoder
    /// is ever primed, so this is a const `true` — "decoder
    /// missing" must NOT force a re-prime on a host that has no
    /// decoder concept (the demuxer-missing clause still applies
    /// everywhere). Returning `true` makes `video_reprime_needed`'s
    /// `!decoder_present` clause inert off-Linux.
    #[cfg(target_os = "linux")]
    fn has_video_decoder(&self, item_id: uuid::Uuid) -> bool {
        self.video_decoders.contains_key(&item_id)
    }
    #[cfg(not(target_os = "linux"))]
    fn has_video_decoder(&self, _item_id: uuid::Uuid) -> bool {
        true
    }

    /// FYS bug A follow-up (finding H1, 2026-05-21): decide whether a
    /// video slide whose lightweight `items`/`item_mtimes` entry is
    /// still cached nonetheless needs a full re-prime in `load`.
    ///
    /// Factored as a pure function of four booleans so it can be
    /// unit-tested on every host (the Linux-only `video_decoders`
    /// map is collapsed to a plain bool by `has_video_decoder`).
    ///
    /// A re-prime is needed when the slide is a video, is not
    /// skip-marked, AND either heavy artifact is gone:
    ///   * the `Mp4Demuxer` (evicted by `evict_other_video_state`), or
    ///   * the V4L2 `VideoDecoderState` (Linux). The original Bug A
    ///     fix only checked the demuxer — but `prime_video_decoder`
    ///     is best-effort: an `EBUSY` from the single-instance vc4
    ///     M2M codec (re-prime racing the kernel teardown after
    ///     `evict_other_video_state`) leaves the demuxer inserted
    ///     while `video_decoders` gets no entry. The next `load`
    ///     then saw demuxer-present, short-circuited, and `paint`
    ///     hard-errored on the absent decoder — the freeze recurred.
    fn video_reprime_needed(
        is_video: bool,
        is_skip_marked: bool,
        demuxer_present: bool,
        decoder_present: bool,
    ) -> bool {
        is_video && !is_skip_marked && (!demuxer_present || !decoder_present)
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
        self.video_skip.remove(&item_id);
        #[cfg(target_os = "linux")]
        self.video_decoders.remove(&item_id);
    }

    /// FYS bug 9 (2026-05-20): drop the V4L2 decoder + demuxer
    /// state for every video slide except `keep`.
    ///
    /// A V4L2 hardware decoder is a scarce resource — it holds
    /// ~8 frame-sized DMA buffers + a codec context. Before this,
    /// decoders were released only on item.json mtime-drift
    /// (`invalidate`), so a playlist cycling N video slides
    /// accumulated N open decoders — a latent OOM on a 426 MB Pi.
    ///
    /// Called from the BeginSlide handler so the previous video's
    /// decoder is freed before the next slide loads. Re-entry into
    /// the same video slide keeps its decoder (no re-prime churn).
    /// BeginTransition deliberately does NOT call this: a
    /// video->video transition needs both the from- and to-slide
    /// decoders live; the next BeginSlide (on the to-slide, after
    /// the transition completes) is what evicts the from-slide.
    fn evict_other_video_state(&mut self, keep_ids: &[uuid::Uuid]) {
        // r46.2 (2026-06-02): widened from a single `keep: Uuid` to
        // a slice. The single-keep shape was wrong for text-over-
        // video (SYSTEM_SPEC §5.10): a text slide with
        // `background_video_slide_id` references a bg video that's
        // NOT the slide_id itself, so passing `slide_id` evicted
        // the bg video's V4L2 demuxer + decoder on every
        // BeginSlide. The IPC text-arm + `ensure_bg_video_for_text_
        // slide` then re-primed the bg video each cycle, allocating
        // a fresh ~24 MB V4L2 buffer pool. Under CMA pressure (Pi
        // Zero 2 W 256 MB pool), the kernel-side dma_buf refcount
        // on the prior pool didn't release fast enough → cumulative
        // CMA leak → eventually the alloc failed → `Mp4Demuxer`
        // open succeeded but `prime_video_decoder` returned Err →
        // `UnsupportedSlide` wire marker → playback.py held the
        // last frame for 3s before retrying. Visible on FYS as the
        // text-over-video slide freezing ~80% of the time.
        //
        // The fix: callers pass `[slide_id, bg_video_id]` for text-
        // over-video slides so the bg video's state survives the
        // eviction. The next `cache.load(bg_id)` short-circuits
        // (items+mtime hit, demuxer + decoder present); no re-prime;
        // no fresh V4L2 alloc; CMA pressure stays bounded.
        //
        // For standalone-video slides (single keep id), behavior is
        // unchanged: `evict_other_video_state(&[slide_id])` retains
        // only the current slide's state. For multi-slide playlists
        // alternating different video bgs, the bg of the prior
        // slide gets evicted naturally (it's not in the new
        // keep_ids slice).
        self.video_demuxers.retain(|k, _| keep_ids.contains(k));
        #[cfg(target_os = "linux")]
        self.video_decoders.retain(|k, _| keep_ids.contains(k));
        // r102.1: V3D BO probe at evict_other_video_state exit.
        // Decoder Drops (and any associated GL state cleanup) have
        // just fired; this snapshot captures the post-eviction
        // baseline so QA can compare against the next BeginSlide
        // delta.
        #[cfg(target_os = "linux")]
        crate::v4l2::log_v3d_bos_at_phase("evict_other_video_state_exit", None);
    }

    /// r46 (2026-06-02): when `item_id` is a TextSlide with
    /// `background_video_slide_id` set (per SYSTEM_SPEC §5.10),
    /// recursively load the referenced VideoSlide so its
    /// `Mp4Demuxer` + V4L2 decoder are primed before the paint
    /// path needs them. Best-effort: if the bg video is missing/
    /// malformed, the paint path falls back to a no-op (last
    /// painted frame stays on screen, text composites on top).
    ///
    /// Idempotent: the recursive `self.load(bg_id)` short-circuits
    /// if the bg video is already in `items` AND its demuxer +
    /// decoder are still primed. After a prior
    /// `evict_other_video_state` (which clears the bg video on
    /// every BeginSlide that isn't the bg video itself), the
    /// recursive call falls through to re-prime via
    /// `video_reprime_needed`.
    fn ensure_bg_video_for_text_slide(
        &mut self,
        content_root: &std::path::Path,
        item_id: uuid::Uuid,
    ) -> Result<()> {
        let bg_id = match self.items.get(&item_id) {
            Some(ContentItem::Text(s)) => s.background_video_slide_id,
            _ => return Ok(()), // not a text slide -> nothing to do
        };
        let Some(bg_id) = bg_id else {
            return Ok(()); // text slide without bg video -> nothing to do
        };
        if bg_id == item_id {
            // Defensive: a TextSlide referencing itself as bg video
            // would infinite-loop the recursion. Python validator
            // catches this at save time; renderer treats it as a
            // no-op + warn.
            eprintln!(
                "ipc: warning -- text slide {} references itself as bg video; ignoring",
                item_id
            );
            return Ok(());
        }
        if let Err(e) = self.load(content_root, bg_id) {
            // Best-effort: log + carry on. The paint path will
            // detect the missing demuxer/decoder + skip the video
            // bake step (last frame stays on screen).
            eprintln!(
                "ipc: warning -- text slide {} references bg video {} but load failed: {:#}",
                item_id, bg_id, e
            );
        }
        Ok(())
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
    /// the existing UnsupportedSlide wire (Python:
    /// `RustRendererUnsupportedSlideError`).
    fn load(&mut self, content_root: &std::path::Path, item_id: uuid::Uuid) -> Result<()> {
        // r110 c3.3.2 second-pass subagent BLOCKER-3 fix: if an
        // async recreate worker is in flight for this id, do NOT
        // synchronously re-prime here. Without this guard,
        // BeginSlide's spawn block runs the wedged-decoder Drop,
        // spawns the worker, then THIS function's downstream
        // video_reprime_needed path would re-open Mp4Demuxer +
        // call prime_video_decoder synchronously (~4s at 1080p)
        // — racing the worker, blocking the IPC handler, and
        // SIGTERMing identically to c3.3.1.
        //
        // The PaintSlide gates at the dispatcher handle the
        // "decoder missing during spin-up" case by holding the
        // last-presented frame (c3.2.2 poster). The worker
        // produces fresh artifacts; try_drain_finished_recreates
        // installs them on a later IPC tick.
        //
        // Text-with-bg case (item_id is the text slide, bg is
        // the failed video): this guard fires when BeginSlide
        // calls cache.load(text_slide_id) → text slide loading
        // proceeds; the recursive ensure_bg_video_for_text_slide
        // → self.load(bg_id) hits THIS guard for the bg_id
        // because pending_recreates was set for bg_id. Returns
        // Ok early without sync prime.
        //
        // Pure Video case (item_id IS the failed video): guard
        // returns Ok early; the wedged decoder slot stays empty
        // until try_drain installs the worker's fresh decoder.
        #[cfg(target_os = "linux")]
        if self.pending_recreates.contains_key(&item_id) {
            eprintln!(
                "[perf] c3_3_2_load_skip_for_pending_recreate item_id={}",
                item_id,
            );
            return Ok(());
        }
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
        // LruMap.get touches access order, which is the right
        // semantic here: load() is the canonical "this slide is
        // being used right now" entry point, so a hit MUST mark
        // the entry warm before any subsequent eviction-on-insert.
        if self.items.get(&item_id).is_some() {
            if self.item_mtimes.get(&item_id).copied() == on_disk_mtime {
                // FYS bug A (2026-05-21): the items+mtime short-
                // circuit treats "item.json parsed" as "slide
                // fully loaded" — but a VIDEO slide is only loaded
                // if its Mp4Demuxer (+ V4L2 decoder) is also live.
                // Bug 9's evict_other_video_state drops the demuxer
                // + decoder on every slide change while leaving
                // `items`/`item_mtimes` intact, so a video slide
                // re-entered after eviction (every playlist
                // loop-back) short-circuited here and was never
                // re-primed — paint_slide / paint_transition then
                // failed for the rest of the run and the sign
                // froze. Fall through to re-open + re-prime when
                // the demuxer is gone. Skip-marked videos are
                // excepted: they intentionally have no demuxer and
                // must not retry-spam a known-bad asset.
                //
                // Finding H1 (2026-05-21): the demuxer-only check
                // above is INCOMPLETE — `prime_video_decoder` is
                // best-effort, so an EBUSY from the single-instance
                // vc4 M2M codec leaves the demuxer inserted but
                // `video_decoders` empty. `video_reprime_needed`
                // now also re-primes on a missing decoder entry
                // (Linux); `has_video_decoder` collapses the
                // Linux-only map to a bool so this stays one
                // expression on every host.
                let video_needs_reprime = Self::video_reprime_needed(
                    matches!(
                        self.items.get(&item_id),
                        Some(ContentItem::Video(_))
                    ),
                    self.video_skip.contains(&item_id),
                    self.video_demuxers.contains_key(&item_id),
                    self.has_video_decoder(item_id),
                );
                if !video_needs_reprime {
                    // r46 (2026-06-02): text slides may have a bg
                    // video reference that was evicted by the prior
                    // BeginSlide's `evict_other_video_state` call.
                    // Re-prime the bg video before short-circuiting.
                    // No-op for text slides without bg video, image
                    // slides, and video slides.
                    return self.ensure_bg_video_for_text_slide(content_root, item_id);
                }
            } else {
                eprintln!(
                    "ipc: slide {item_id} item.json drifted on disk; refreshing cache"
                );
                self.invalidate(item_id);
            }
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
            // r46 (2026-06-02): if this is a TextSlide with a
            // background_video_slide_id (per SYSTEM_SPEC §5.10), the
            // referenced VideoSlide needs its demuxer + V4L2 decoder
            // primed before the paint path needs them. Recursive
            // self.load handles items[bg_id]/demuxer/decoder priming.
            return self.ensure_bg_video_for_text_slide(content_root, item_id);
        }
        if let Some(s) = find_video_slide(content_root, item_id)? {
            self.items.insert(item_id, ContentItem::Video(s));
            let asset_path = video_slide_asset_path(content_root, item_id);
            // r56 Phase A (2026-06-03): measure the per-video-slide
            // synchronous load cost (Mp4Demuxer::open +
            // prime_video_decoder). qarl reports stalls at each new
            // transition in a 4-video playlist; the journal showed
            // "ipc: opened MP4" landing AT the slide boundary, which
            // is exactly this code path. The [perf] line below pairs
            // with the [perf] v4l2_prime line emitted from
            // video_decode.rs to give a full breakdown of where the
            // ms go (file open + ftyp+moov parse + sample table walk
            // vs V4L2 device open + S_FMT + REQBUFS + STREAMON +
            // primer feed + warmup).
            let t_load_total = std::time::Instant::now();
            let t_mp4_open = std::time::Instant::now();
            match Mp4Demuxer::open(&asset_path) {
                Ok(dem) => {
                    let mp4_open_us = t_mp4_open.elapsed().as_micros();
                    eprintln!(
                        "ipc: opened MP4 for video slide {} ({}x{}, {} samples)",
                        item_id, dem.width, dem.height, dem.samples.len()
                    );
                    // V4L2 piece 3c: on Linux, also prime the
                    // hardware decoder. Failure is best-effort
                    // (warn + fall through to the UnsupportedSlide
                    // wire — playback loop logs + skips per the
                    // post-DELETE-PIL contract). Mac: skip.
                    //
                    // r56 Phase A subagent (2026-06-03 WARN): time
                    // the prime call ONLY on Linux. On macOS the
                    // entire prime_video_decoder call is elided by
                    // cfg, so measuring around it would produce a
                    // structurally-zero prime_us field -- confusing
                    // for future log readers. Phase A's purpose is
                    // Pi-side data, so a separate (smaller) [perf]
                    // line on macOS isn't worth it -- we just skip
                    // emission there.
                    #[cfg(target_os = "linux")]
                    let prime_us = {
                        let t_prime = std::time::Instant::now();
                        // r75 subagent BLOCKER-2: snapshot BEFORE the
                        // prime call. The error-path log emits both
                        // `before` and `after` so QA can compute
                        // `after - before` per failed prime:
                        //   delta == 0: Drop fired, counter balanced
                        //               -> kernel/VPU slot leak
                        //   delta >  0: Drop did NOT fire on the
                        //               Decoder built before failure
                        //               -> userspace leak (an Arc
                        //               survived past intended scope)
                        // Pre-fix the post-prime snapshot alone could
                        // never distinguish these because Drop fires
                        // before the error bubbles up.
                        let counter_before = crate::v4l2::mmal_components_live();
                        // r78 Phase A: cross-check probe for the
                        // synchronous BeginSlide path. The dispatch
                        // wants to A/B this against the preload's
                        // preload_sample_dump line; if BOTH paths
                        // show the same first_nal_type +
                        // contains_idr classification, the demuxer
                        // is consistent across paths. r78 subagent
                        // BLOCKER-2: lives here NOT inside
                        // prime_video_decoder so reel + tests
                        // don't flood the journal.
                        crate::video_decode::log_synchronous_begin_slide_sample_dump(
                            item_id, &dem,
                        );
                        // r79 Phase A.5: SPS/PPS state probe so QA
                        // can falsify-or-confirm the "SPS/PPS not
                        // reaching the decoder" hypothesis from the
                        // r79 dispatch.
                        crate::video_decode::log_preload_decoder_config(
                            item_id, &dem, "cold_start",
                        );
                        match prime_video_decoder(&dem, "cold_start") {
                            Ok(dec_state) => {
                                self.video_decoders.insert(item_id, dec_state);
                            }
                            Err(e) => {
                                eprintln!(
                                    "ipc: warning -- failed to prime V4L2 decoder for video slide {}: {:#}",
                                    item_id, e
                                );
                                // r99: CMA snapshot + peer-decoder
                                // context appended so QA can see
                                // resource state at REQBUFS failure.
                                let r99_tail = format_mmal_leak_diagnostics_tail(
                                    Some(self), item_id,
                                );
                                eprintln!(
                                    "[mmal_leak_suspect] kind=video_decode \
                                     op={:?} slide_id={} \
                                     mmal_components_live_before={} \
                                     mmal_components_live_after={} \
                                     drop_path=automatic call_site=cache_load \
                                     {}",
                                    extract_top_context(&e),
                                    item_id,
                                    counter_before,
                                    crate::v4l2::mmal_components_live(),
                                    r99_tail,
                                );
                            }
                        }
                        t_prime.elapsed().as_micros()
                    };
                    self.video_demuxers.insert(item_id, dem);
                    let total_us = t_load_total.elapsed().as_micros();
                    #[cfg(target_os = "linux")]
                    eprintln!(
                        "[perf] video_load slide_id={} mp4_open_us={} prime_us={} total_us={}",
                        item_id, mp4_open_us, prime_us, total_us
                    );
                    #[cfg(not(target_os = "linux"))]
                    {
                        // Mac dev path: emit a minimal [perf] line
                        // without prime_us (V4L2 priming is Linux-
                        // only). Keeps the log line shape parseable
                        // by any aggregator that sees both.
                        let _ = mp4_open_us;
                        let _ = total_us;
                    }
                }
                Err(e) => {
                    eprintln!(
                        "ipc: warning -- failed to open MP4 {} for video slide {}: {:#}",
                        asset_path.display(), item_id, e
                    );
                    // Bug 8 / Fix A: record a skip marker so future
                    // BeginSlide for this id short-circuits to the
                    // UnsupportedSlide rail instead of letting the
                    // begin_slide accept + paint_slide fail with a
                    // generic OpError. Without this, a single bad
                    // MP4 hot-spins the Python loop at ~3.4 Hz with
                    // ERROR-level tracebacks (Bug 8 frozen-sign
                    // incident, 2026-05-17 @ 192.168.1.67).
                    self.video_skip.insert(item_id);
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

/// r65 (2026-06-05): worker-thread entry point. Builds a
/// `PreloadArtifacts` for a slide WITHOUT touching the shared
/// SlideCache (the worker doesn't own one). Recursive for
/// TextSlide-with-bg-video: the bg VideoSlide's artifacts are
/// appended to the same struct.
///
/// The expensive bits live here: `Mp4Demuxer::open` (file open +
/// ftyp/moov/sample-table parse) + `prime_video_decoder`
/// (V4L2 device open + S_FMT + REQBUFS + STREAMON + primer feed
/// + warmup). On Pi Zero 2 W the prime is the 200-600 ms cost
/// that the dispatch identified as blocking the main thread.
///
/// Errors at the JSON-parse or asset-resolution layer abort the
/// worker (and the caller's BeginSlide will fall through to the
/// normal cache.load path). A failed V4L2 prime is non-fatal:
/// the demuxer is shipped + a video_skip marker recorded so the
/// existing UnsupportedSlide rail picks it up.
fn preload_in_worker(
    content_root: &std::path::Path,
    item_id: uuid::Uuid,
) -> PreloadResult {
    let mut artifacts = PreloadArtifacts {
        items: Vec::new(),
        demuxers: Vec::new(),
        #[cfg(target_os = "linux")]
        decoders: Vec::new(),
        skip_marks: Vec::new(),
    };
    let mtime = std::fs::metadata(
        content_root.join(item_id.to_string()).join("item.json"),
    )
    .ok()
    .and_then(|m| m.modified().ok());
    if let Some(s) = find_text_slide(content_root, item_id)? {
        let bg_id = s.background_video_slide_id;
        artifacts.items.push((item_id, ContentItem::Text(s), mtime));
        if let Some(bg_id) = bg_id {
            if bg_id != item_id {
                // r65 subagent (NIT fix): mirror
                // ensure_bg_video_for_text_slide's best-effort
                // semantics (cache.load's path logs + continues
                // on a missing/malformed bg video so the text
                // slide still loads). Pre-fix the `?` would
                // propagate the bg failure up, ensure_preload_
                // complete would log + drop, and BeginSlide
                // would fall through to a synchronous cache.load
                // -- re-introducing the very 200-600 ms stall
                // r65 is trying to remove for the text slide
                // itself.
                match preload_in_worker(content_root, bg_id) {
                    Ok(bg) => {
                        artifacts.items.extend(bg.items);
                        artifacts.demuxers.extend(bg.demuxers);
                        #[cfg(target_os = "linux")]
                        artifacts.decoders.extend(bg.decoders);
                        artifacts.skip_marks.extend(bg.skip_marks);
                    }
                    Err(e) => {
                        eprintln!(
                            "ipc: preload_worker -- text slide {} references bg video {} but preload failed: {:#}",
                            item_id, bg_id, e,
                        );
                        // Text slide artifacts still ship; the
                        // bg failure surfaces via the existing
                        // UnsupportedSlide rail at paint time.
                    }
                }
            }
        }
        return Ok(artifacts);
    }
    if let Some(s) = find_image_slide(content_root, item_id)? {
        artifacts.items.push((item_id, ContentItem::Image(s), mtime));
        return Ok(artifacts);
    }
    if let Some(s) = find_video_slide(content_root, item_id)? {
        artifacts.items.push((item_id, ContentItem::Video(s), mtime));
        let asset_path = video_slide_asset_path(content_root, item_id);
        match Mp4Demuxer::open(&asset_path) {
            Ok(dem) => {
                #[cfg(target_os = "linux")]
                {
                    let t_prime = std::time::Instant::now();
                    // r73 (2026-06-06) / r77 (2026-06-07): preload
                    // worker uses warmup_count=PRIME_WARMUP_FOR_PRELOAD
                    // (currently 2; see main.rs:PRIME_WARMUP_FOR_PRELOAD
                    // docstring for the value's history and gated-bump
                    // protocol). The prime QBUFs primer + warmup
                    // samples; Phase B's drain consumes the first
                    // CAPTURE frame so the bcm2835-codec doesn't sit
                    // in wait-for-userspace during the idle window
                    // between preload finish and transition start.
                    // Cold-start callers (synchronous BeginSlide,
                    // standalone reel) still use the default
                    // warmup_count=3 for B-frame lookahead.
                    // r75 subagent BLOCKER-2: snapshot before prime
                    // so the failure log can emit before+after.
                    //
                    // r76 Phase B (2026-06-07): switched to
                    // `prime_video_decoder_for_preload` which adds a
                    // bounded CAPTURE drain after prime. r76 Phase A
                    // telemetry showed 47/47 transitions with
                    // endpoint_b NEVER delivering a frame -- prime
                    // succeeded but bcm2835-codec sat in a wait-
                    // for-userspace state with CAPTURE queued. The
                    // drain forces the first DQBUF in the worker
                    // (which is safe to block; that's its whole job)
                    // so the kernel pipeline is flowing when bake_b
                    // runs at transition start.
                    let counter_before = crate::v4l2::mmal_components_live();
                    match crate::video_decode::prime_video_decoder_for_preload(
                        &dem, item_id,
                    ) {
                        Ok(dec_state) => {
                            let prime_us = t_prime.elapsed().as_micros();
                            eprintln!(
                                "[perf] preload_worker_prime slide_id={} prime_us={}",
                                item_id, prime_us
                            );
                            artifacts.decoders.push((item_id, dec_state));
                        }
                        Err(e) => {
                            eprintln!(
                                "ipc: preload_worker -- failed to prime V4L2 decoder for video slide {}: {:#}",
                                item_id, e
                            );
                            // r75: same parser-friendly leak-suspect line
                            // as the synchronous BeginSlide site. call_site
                            // distinguishes the two paths.
                            // r99: same tail format as the cache_load
                            // site; cache is None here (worker thread
                            // doesn't hold SlideCache), so peer fields
                            // render as `?` placeholders.
                            let r99_tail = format_mmal_leak_diagnostics_tail(
                                None, item_id,
                            );
                            eprintln!(
                                "[mmal_leak_suspect] kind=video_decode \
                                 op={:?} slide_id={} \
                                 mmal_components_live_before={} \
                                 mmal_components_live_after={} \
                                 drop_path=automatic call_site=preload_worker \
                                 {}",
                                extract_top_context(&e),
                                item_id,
                                counter_before,
                                crate::v4l2::mmal_components_live(),
                                r99_tail,
                            );
                        }
                    }
                }
                artifacts.demuxers.push((item_id, dem));
            }
            Err(e) => {
                eprintln!(
                    "ipc: preload_worker -- failed to open MP4 {} for video slide {}: {:#}",
                    asset_path.display(), item_id, e
                );
                artifacts.skip_marks.push(item_id);
            }
        }
        return Ok(artifacts);
    }
    Err(anyhow!(
        "preload_in_worker: no item found for {} under {}",
        item_id, content_root.display(),
    ))
}

/// r65 subagent (WARN fix): drain pending_preloads handles older
/// than `max_age`. If a BeginSlide never arrives (operator
/// changed playlist between PreloadSlide(A) and BeginSlide(A)),
/// the artifacts -- including a primed ~5 MB V4L2 decoder --
/// leak in pending_preloads forever. Called opportunistically
/// from the BeginSlide and BeginTransition handlers (which run
/// at most once every few seconds), so the walk cost is amortized.
fn drain_stale_preloads(
    cache: &mut SlideCache,
    max_age: std::time::Duration,
) {
    let stale_ids: Vec<uuid::Uuid> = cache
        .pending_preloads
        .iter()
        .filter(|(_, h)| h.enqueued_at.elapsed() > max_age)
        .map(|(id, _)| *id)
        .collect();
    if stale_ids.is_empty() {
        return;
    }
    for id in stale_ids {
        if let Some(handle) = cache.pending_preloads.remove(&id) {
            // Join the worker so its artifacts drop cleanly (V4L2
            // decoder STREAMOFFs + munmaps from DecoderInner::Drop).
            // If the worker panicked, log + continue; we still want
            // to release our slot in pending_preloads.
            let age = handle.enqueued_at.elapsed();
            match handle.thread.join() {
                Ok(Ok(_)) => eprintln!(
                    "[perf] preload_stale_drained slide_id={} age_ms={}",
                    id,
                    age.as_millis(),
                ),
                Ok(Err(e)) => eprintln!(
                    "[perf] preload_stale_drained slide_id={} age_ms={} err={:#}",
                    id,
                    age.as_millis(),
                    e,
                ),
                Err(_) => eprintln!(
                    "[perf] preload_stale_drained slide_id={} age_ms={} thread_panicked",
                    id,
                    age.as_millis(),
                ),
            }
        }
    }
}

/// r65: if `slide_id` has a pending preload thread, join it and
/// install the artifacts into the main thread's `cache`. Called
/// from the BeginSlide and BeginTransition IPC handlers at the
/// TOP (before evict_other_video_state / cache.load) so the
/// existing logic short-circuits cleanly on the artifacts the
/// worker already produced.
///
/// Emits `[perf] begin_slide_wait slide_id=X wait_us=N` so QA
/// can see when BeginSlide had to block on an unfinished
/// preload. Steady state: wait_us ~= 0 µs (worker done long
/// before BeginSlide fires for the same id).
fn ensure_preload_complete(cache: &mut SlideCache, slide_id: uuid::Uuid) {
    let Some(handle) = cache.pending_preloads.remove(&slide_id) else {
        return;
    };
    let t_wait = std::time::Instant::now();
    let elapsed_since_enqueue = handle.enqueued_at.elapsed().as_micros();
    let join_result = handle.thread.join();
    let wait_us = t_wait.elapsed().as_micros();
    eprintln!(
        "[perf] begin_slide_wait slide_id={} wait_us={} time_since_enqueue_us={}",
        slide_id, wait_us, elapsed_since_enqueue,
    );
    match join_result {
        Ok(Ok(artifacts)) => {
            for (id, item, mtime) in artifacts.items {
                cache.items.insert(id, item);
                if let Some(m) = mtime {
                    cache.item_mtimes.insert(id, m);
                }
            }
            for (id, dem) in artifacts.demuxers {
                cache.video_demuxers.insert(id, dem);
            }
            #[cfg(target_os = "linux")]
            for (id, dec) in artifacts.decoders {
                cache.video_decoders.insert(id, dec);
            }
            for id in artifacts.skip_marks {
                cache.video_skip.insert(id);
            }
        }
        Ok(Err(e)) => {
            eprintln!(
                "ipc: preload worker for slide {} returned Err: {:#} (BeginSlide will fall through to synchronous cache.load)",
                slide_id, e
            );
        }
        Err(e) => {
            eprintln!(
                "ipc: preload worker thread for slide {} panicked: {:?} (BeginSlide will fall through to synchronous cache.load)",
                slide_id, e
            );
        }
    }
}

/// r110 c3.3.2 (2026-06-11): non-blocking drain of finished
/// `pending_recreates` workers. Called at the top of
/// handle_inner_request so any worker that finished since the
/// last IPC tick gets its fresh decoder installed before the
/// next handler runs.
///
/// Uses `JoinHandle::is_finished()` so workers in progress
/// stay in the map (we never block waiting for them — that's
/// the whole point of c3.3.2 vs c3.3.1).
///
/// When a worker is finished, joins it (cost ~0 since it's
/// done), installs the artifacts (replaces the wedged decoder
/// with the fresh one — Drop on the old runs at install
/// boundary, not on the IPC critical path), removes from the
/// map. Errors are logged + the wedged decoder stays for the
/// next try.
#[cfg(target_os = "linux")]
fn try_drain_finished_recreates(cache: &mut SlideCache) {
    let finished_ids: Vec<uuid::Uuid> = cache
        .pending_recreates
        .iter()
        .filter(|(_, handle)| handle.thread.is_finished())
        .map(|(id, _)| *id)
        .collect();
    for id in finished_ids {
        let Some(handle) = cache.pending_recreates.remove(&id) else {
            continue;
        };
        let t_install = std::time::Instant::now();
        let elapsed_since_enqueue = handle.enqueued_at.elapsed().as_micros();
        let join_result = handle.thread.join();
        let join_us = t_install.elapsed().as_micros();
        match join_result {
            Ok(Ok(artifacts)) => {
                for (aid, item, mtime) in artifacts.items {
                    cache.items.insert(aid, item);
                    if let Some(m) = mtime {
                        cache.item_mtimes.insert(aid, m);
                    }
                }
                for (aid, dem) in artifacts.demuxers {
                    cache.video_demuxers.insert(aid, dem);
                }
                for (aid, dec) in artifacts.decoders {
                    cache.video_decoders.insert(aid, dec);
                }
                for aid in artifacts.skip_marks {
                    cache.video_skip.insert(aid);
                }
                let install_us = t_install.elapsed().as_micros();
                eprintln!(
                    "[perf] c3_3_2_recreate_installed video_id={} time_since_enqueue_us={} join_us={} install_us={}",
                    id, elapsed_since_enqueue, join_us, install_us,
                );
            }
            Ok(Err(e)) => {
                eprintln!(
                    "[perf] c3_3_2_recreate_failed video_id={} time_since_enqueue_us={} err={:#}",
                    id, elapsed_since_enqueue, e,
                );
            }
            Err(e) => {
                eprintln!(
                    "[perf] c3_3_2_recreate_panicked video_id={} time_since_enqueue_us={} err={:?}",
                    id, elapsed_since_enqueue, e,
                );
            }
        }
    }
}

#[cfg(not(target_os = "linux"))]
fn try_drain_finished_recreates(_cache: &mut SlideCache) {}

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

/// r75 (2026-06-07): pull the OUTERMOST `.with_context` string out of
/// an anyhow error chain (e.g. "REQBUFS OUTPUT" or "S_FMT OUTPUT
/// (H264)") so the structured [mmal_leak_suspect] line can carry a
/// short op tag that QA can grep + bucket. Falls back to the full
/// Display of the root cause if the chain is empty or context-less.
fn extract_top_context(e: &anyhow::Error) -> String {
    // anyhow chains stack OUTERMOST-first, so the first iteration of
    // .chain() returns the outermost context which is exactly the
    // op-tag we attach via .context("REQBUFS OUTPUT") etc. in the
    // prime path.
    let mut iter = e.chain();
    iter.next()
        .map(|root| root.to_string())
        .unwrap_or_else(|| e.to_string())
}

/// r99 (2026-06-09): render the new diagnostics tail for the
/// `[mmal_leak_suspect]` log line. Captures CMA snapshot + peer-
/// decoder context + (when unavailable) explicit placeholders so
/// log parsers see a consistent field set.
///
/// Format pinned by `format_mmal_leak_diagnostics_tail_layout`
/// test; parsers should grep for `cma_used_mb=`, `cma_free_mb=`,
/// `cma_total_mb=`, `peer_decoder_count=`, `peer_decoder_dims=`.
///
/// When `/proc/meminfo` is missing (macOS) or CMA-disabled (rare),
/// the cma_* fields render as `cma_used_mb=? cma_free_mb=?
/// cma_total_mb=?` so a scanner can still split on them.
///
/// `peer_decoder_dims` is rendered as `[WxH,WxH,...]` with a fixed
/// stable order (by Uuid) — empty list `[]` when no peers exist.
#[cfg(target_os = "linux")]
fn format_mmal_leak_diagnostics_tail(
    cache: Option<&SlideCache>,
    failing_slide_id: uuid::Uuid,
) -> String {
    let cma = crate::v4l2::read_cma_snapshot_mb();
    let cma_str = match cma {
        Some(s) => format!(
            "cma_used_mb={} cma_free_mb={} cma_total_mb={}",
            s.used, s.free, s.total,
        ),
        None => "cma_used_mb=? cma_free_mb=? cma_total_mb=?".to_string(),
    };
    // The preload-worker emit site runs on a worker thread without
    // SlideCache access (the worker holds an `Arc<Mp4Demuxer>` not
    // the full cache); it passes `None` and gets `?` for peer
    // fields. The synchronous cache_load emit site passes
    // `Some(&self)` and gets the real peer enumeration. Either way
    // the field set is consistent so journal parsers can split on
    // `peer_decoder_count=` etc.
    let (peer_count_str, peer_dims_str) = match cache {
        Some(c) => {
            // Peer decoders are the OTHER live decoders that may be
            // consuming the CMA budget the failing prime wanted.
            // Filter the failing slide's own id out (in practice it
            // isn't in the map yet — the prime is what would have
            // inserted it — but the filter is correct independent
            // of insertion order).
            let mut peer_ids: Vec<uuid::Uuid> = c.video_decoders
                .keys()
                .copied()
                .filter(|id| *id != failing_slide_id)
                .collect();
            peer_ids.sort();
            let peer_count = peer_ids.len();
            let peer_dims: Vec<String> = peer_ids.iter()
                .filter_map(|id| c.video_decoders.get(id))
                .map(|d| format!("{}x{}", d.capture_w, d.capture_h))
                .collect();
            (peer_count.to_string(), format!("[{}]", peer_dims.join(",")))
        }
        None => ("?".to_string(), "[?]".to_string()),
    };
    format!(
        "{} peer_decoder_count={} peer_decoder_dims={}",
        cma_str, peer_count_str, peer_dims_str,
    )
}

/// Outer loop: read requests until Open arrives. Other ops
/// before Open return Err. After Open succeeds, dispatch
/// transfers to run_inner_loop which holds the EGL session.
pub fn run_ipc_sidecar() -> Result<()> {
    // r38d SIGUSR1 cache-dump handler. Best-effort install -- failure
    // here means the [cache-dump] surface is unavailable but rendering
    // proceeds normally. The handler itself is async-signal-safe (sets
    // an AtomicBool only); the actual dump happens in the inner loop
    // below at the start of each Advance iteration. See
    // qa/r38d-sigusr1-cache-dump-2026-06-02.md.
    if let Err(e) = crate::sigusr1::install_handler() {
        eprintln!("warn: SIGUSR1 cache-dump handler install failed: {e}; \
                   /cache-dump observability unavailable");
    }

    let stdin = std::io::stdin();
    let mut stdout = std::io::stdout().lock();
    let mut stdin_lock = stdin.lock();

    // One reusable String for the entire outer (pre-Open) loop. The
    // BufRead::lines() shape we used to use heap-allocated a fresh
    // String per message; reading into a cleared buffer keeps a
    // single allocation whose capacity grows once to the longest
    // message. The hot path is the inner loop (which has its own
    // buffer) -- this outer one matters less but keeps the signature
    // story consistent (no Lines<...> threading).
    let mut line = String::with_capacity(2048);
    loop {
        line.clear();
        match stdin_lock.read_line(&mut line) {
            Ok(0) => return Ok(()), // EOF
            Ok(_) => {}
            Err(e) => return Err(e.into()),
        }
        let req: IpcRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                emit_response(&mut stdout, &err(format!("invalid request: {e}")))?;
                continue;
            }
        };
        match req {
            IpcRequest::Open(params) => {
                match run_open_and_inner_loop(params, &mut stdin_lock, &mut stdout) {
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
}

/// Inner loop body invoked after Open succeeds. Slice (d)
/// branches on cfg(target_os = "linux"): on Linux, run the
/// inner loop inside with_egl_session so EglSession is held
/// across Advance calls + actual GL paint fires; on Mac
/// (cargo test only), run state-machine-only mode (slice c
/// behavior).
fn run_open_and_inner_loop<R, W>(
    params: OpenParams,
    stdin: &mut R,
    stdout: &mut W,
) -> Result<()>
where
    R: BufRead,
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
        return run_open_and_inner_loop_linux(params, stdin, stdout, &content_root);
    }
    #[cfg(not(target_os = "linux"))]
    {
        return run_open_and_inner_loop_state_only(stdin, stdout, &content_root);
    }
}

/// Mac / non-Linux build: state-machine-only inner loop. Used
/// by cargo test on the dev box where DRM isn't available.
/// Mirrors slice (c) behavior: emit placeholder OpenOk, run
/// the state machine, ignore paint hooks.
#[cfg(not(target_os = "linux"))]
fn run_open_and_inner_loop_state_only<R, W>(
    stdin: &mut R,
    stdout: &mut W,
    content_root: &Path,
) -> Result<()>
where
    R: BufRead,
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
    let mut line = String::with_capacity(2048);
    loop {
        line.clear();
        match stdin.read_line(&mut line) {
            Ok(0) => break, // EOF
            Ok(_) => {}
            Err(e) => return Err(e.into()),
        }
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

/// STREAM/VLC slice 2.5 — open the dedicated binary frame channel.
///
/// The Python backend (rust_renderer.py) creates a pipe, passes its
/// read end to this sidecar as an inherited FD, and tells us the FD
/// number via the OPENMARQUEE_FRAME_FD env var. Returns None when
/// the var is unset — a backend that predates 2.5 — in which case
/// begin_external_frames errors cleanly instead of painting.
#[cfg(target_os = "linux")]
fn open_external_frame_channel() -> Option<std::fs::File> {
    use std::os::fd::FromRawFd;
    let raw = std::env::var("OPENMARQUEE_FRAME_FD").ok()?;
    let fd: i32 = match raw.trim().parse() {
        Ok(n) if n >= 0 => n,
        _ => {
            eprintln!("warn: OPENMARQUEE_FRAME_FD={raw:?} is not a valid fd");
            return None;
        }
    };
    // SAFETY: the Python backend created this pipe and handed us the
    // read end as an inherited FD (subprocess pass_fds). We take
    // ownership for the sidecar's lifetime.
    Some(unsafe { std::fs::File::from_raw_fd(fd) })
}

/// STREAM/VLC slice 2.5 — the external-frame pump.
///
/// Runs AFTER the caller has already emitted the begin_external_
/// frames response — begin_external_frames is a normal request/
/// response op (an immediate ack), so this pump produces NO
/// response: it just paints until the end sentinel and returns to
/// the JSON-op loop.
///
/// Reads length-prefixed frames off the binary channel —
/// `[u32-BE length][payload]` — painting each one fullscreen until
/// a length of 0 (the end sentinel). A paint failure is persistent
/// (a GL/DRM fault), so it is logged ONCE and the pump then just
/// DRAINS the channel (no per-frame log flood) so the Python writer
/// never blocks; the held last frame stays on glass. The summary
/// line carries the frame-pacing numbers slice 9's live-fire parses.
///
/// STREAM/VLC HW-decode (2026-05-20): `pixel_format` is declared
/// once per pump session (one producer = one format). For `rgb888`
/// each frame is `width*height*3` bytes and `width`/`height` are
/// the panel dims; for `nv12` each frame is `width*height*3/2`
/// bytes and `width`/`height` are the SOURCE video dims (the
/// renderer cover-fit-scales onto the panel). The expected per-
/// frame byte size + the paint dispatch both branch on the format.
#[cfg(target_os = "linux")]
fn run_external_frame_pump(
    session: &mut crate::hdmi::EglSession,
    card: &crate::Card,
    reader: &mut std::fs::File,
    width: u32,
    height: u32,
    pixel_format: ExternalPixelFormat,
) {
    use std::io::Read;
    // Per-frame byte size depends on the declared format: RGB888 is
    // 3 bytes/px, NV12 is 1.5 bytes/px (Y plane + half-res UV).
    let frame_bytes = match pixel_format {
        ExternalPixelFormat::Rgb888 => (width as usize) * (height as usize) * 3,
        ExternalPixelFormat::Nv12 => (width as usize) * (height as usize) * 3 / 2,
    };
    let mut len_buf = [0u8; 4];
    let mut frame: Vec<u8> = vec![0u8; frame_bytes];
    let mut painted: u64 = 0;
    let mut skipped: u64 = 0;
    let mut paint_us_total: u64 = 0;
    let mut paint_us_max: u64 = 0;
    let mut paint_broken = false;
    loop {
        if let Err(e) = reader.read_exact(&mut len_buf) {
            // EOF / error before a length prefix: the Python writer
            // vanished without a sentinel (a crash). Leave pump-mode;
            // the JSON-op loop then hits its own stdin EOF and exits.
            eprintln!(
                "ipc_sidecar: external-frame channel closed after \
                 {painted} frames: {e}"
            );
            return;
        }
        let len = u32::from_be_bytes(len_buf) as usize;
        // Sanity cap. The channel is an internal trusted pipe, but a
        // corrupt length must not drive a multi-GB allocation. 64 MiB
        // comfortably covers 4K RGB888 (~25 MiB).
        const MAX_FRAME_BYTES: usize = 64 * 1024 * 1024;
        if len > MAX_FRAME_BYTES {
            eprintln!(
                "ipc_sidecar: external-frame length {len} exceeds the \
                 {MAX_FRAME_BYTES}-byte cap; leaving pump-mode"
            );
            return;
        }
        if len == 0 {
            eprintln!(
                "ipc_sidecar: external-frame pump done — {painted} painted, \
                 {skipped} skipped, avg {:.2}ms, max {:.2}ms",
                if painted > 0 {
                    paint_us_total as f64 / painted as f64 / 1000.0
                } else {
                    0.0
                },
                paint_us_max as f64 / 1000.0,
            );
            return;
        }
        if frame.len() != len {
            frame.resize(len, 0);
        }
        if let Err(e) = reader.read_exact(&mut frame) {
            eprintln!(
                "ipc_sidecar: external-frame channel closed mid-frame \
                 after {painted} frames: {e}"
            );
            return;
        }
        if len != frame_bytes || paint_broken {
            // Dimension desync, or a prior paint already failed: the
            // frame is fully drained so the pipe stays in sync — just
            // skip the paint.
            skipped += 1;
            continue;
        }
        let t0 = std::time::Instant::now();
        // Dispatch on the session's declared pixel format. RGB888 →
        // raw-RGB upload + FS_BLIT; NV12 → planar Y+UV upload +
        // cover-fit BT.709 NV12→RGB blit.
        let paint_res = match pixel_format {
            ExternalPixelFormat::Rgb888 => {
                crate::hdmi::paint_and_present_external_frame(
                    session, card, &frame, width, height,
                )
            }
            ExternalPixelFormat::Nv12 => {
                crate::hdmi::paint_and_present_external_nv12_frame(
                    session, card, &frame, width, height,
                )
            }
        };
        match paint_res {
            Ok(()) => {
                let us = t0.elapsed().as_micros().min(u64::MAX as u128) as u64;
                paint_us_total += us;
                paint_us_max = paint_us_max.max(us);
                painted += 1;
            }
            Err(e) => {
                eprintln!(
                    "ipc_sidecar: external-frame paint failed; holding last \
                     frame + draining until sentinel (further errors \
                     silenced): {e:#}"
                );
                paint_broken = true;
                skipped += 1;
            }
        }
    }
}

/// Linux build: open the DRM card, enter run_in_egl_session,
/// and run the inner loop inside the closure. Each Advance op
/// that produces PaintSlide / PaintTransition triggers an
/// actual GL paint via paint_and_present_one_frame_*. Errors
/// in paint surface as IpcResponse::Err{message}; the loop
/// continues so the caller can recover (e.g., re-BeginSlide
/// after a transient FBO failure).
#[cfg(target_os = "linux")]
fn run_open_and_inner_loop_linux<R, W>(
    params: OpenParams,
    stdin: &mut R,
    stdout: &mut W,
    content_root: &Path,
) -> Result<()>
where
    R: BufRead,
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

    // FYS bug 5 -- validate the display rotation. Settings.display_
    // rotation is constrained to {0,90,180,270} upstream, but a
    // malformed Open payload could still carry something else; treat
    // any out-of-set value as 0 (no rotation) and warn rather than
    // failing the whole open.
    let rotation = match params.rotation {
        0 | 90 | 180 | 270 => params.rotation,
        other => {
            eprintln!(
                "warn: ipc_sidecar open rotation={other} is not one of \
                 0/90/180/270; treating as 0"
            );
            0
        }
    };

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

    hdmi::run_in_egl_session(&card, rotation, |session| {
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
        // STREAM/VLC slice 2.5: the dedicated binary frame channel
        // (None when the backend predates 2.5). begin_external_frames
        // pumps RGB888 frames off this until its end sentinel.
        let mut frame_reader = open_external_frame_channel();
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
        // Reusable per-message buffer. Replaces a BufRead::lines()
        // iterator that heap-allocated a fresh String per message --
        // i.e. once per Advance at 30 Hz + every BeginSlide /
        // BeginTransition between paints. One persistent String for
        // the entire session; capacity grows once to the longest
        // message and stays there.
        let mut line = String::with_capacity(2048);
        loop {
            // r38d: drain a pending SIGUSR1 by emitting one
            // [cache-dump] line to stderr. take_pending() is a
            // single atomic swap (cheap, ~5 ns) -- safe to call
            // every iteration. Caps come from the canonical pub
            // consts so a future cap bump auto-flows here.
            if crate::sigusr1::take_pending() {
                let (bg_len, tex_len) = session.cma_dump_cache_lens();
                crate::sigusr1::emit_dump_line(
                    bg_len, crate::hdmi::IMAGE_BG_CACHE_CAPACITY,
                    tex_len,
                    crate::image_slide_tex::IMAGE_SLIDE_TEX_CACHE_CAPACITY,
                );
            }
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
            line.clear();
            match stdin.read_line(&mut line) {
                Ok(0) => break, // EOF
                Ok(_) => {}
                Err(e) => return Err(e.into()),
            }
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
            // Reconfigure landed as a partial op for QA H1
            // 2026-05-23: brightness + gamma applied in-place via
            // session.apply_settings; rotation rejected with a typed
            // error since DRM mode-change + EGL surface invalidation
            // remain deferred post-v1).
            //
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

            // QA H1 (2026-05-23): Reconfigure intercepts here too,
            // for the same reason — the standard dispatch has no
            // session, so brightness/gamma have no shader uniforms to
            // touch. validate_reconfigure builds the new Settings (or
            // a typed error); on success we route through the same
            // session.apply_settings sink that SettingsWatcher uses,
            // so the next frame paints with the new color profile.
            // Closes the IPC half of §3.4 / §6.3's ≤2s settings-apply
            // story — IPC-driven is INSTANT vs the file-poll path.
            if let IpcRequest::Reconfigure(ref p) = req {
                let resp = match crate::playback::validate_reconfigure(
                    session.current_settings(), p,
                ) {
                    Ok(updated) => {
                        session.apply_settings(updated);
                        ok_empty()
                    }
                    Err(e) => err(e.message()),
                };
                emit_response(stdout, &resp)?;
                continue;
            }

            // STREAM/VLC slice 2.5: begin_external_frames is
            // intercepted here (like Capture) BEFORE the standard
            // dispatch — it needs session + card + the binary frame
            // channel. It is a normal request/response op: ACK it
            // immediately, then run the pump loop (which produces no
            // further response) until the end sentinel returns us to
            // the JSON-op loop.
            if let IpcRequest::BeginExternalFrames(ref p) = req {
                match frame_reader.as_mut() {
                    None => {
                        emit_response(
                            stdout,
                            &err(
                                "begin_external_frames: no frame \
                                 channel (OPENMARQUEE_FRAME_FD unset)",
                            ),
                        )?;
                    }
                    Some(reader) => {
                        emit_response(stdout, &ok_empty())?;
                        run_external_frame_pump(
                            session, &card, reader,
                            p.width, p.height, p.pixel_format,
                        );
                    }
                }
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

            // `[perf]` r1 (2026-05-26): forward the IPC-known
            // paint-kind to the session so the deadline-miss
            // warn-log inside commit_fb can differentiate
            // "video glitching" (Slide, single-arm paint) from
            // "transition heavy" (Transition, dual-input shader)
            // without threading per-slide context through every
            // commit_fb caller. Non-paint responses set the flag
            // to false; the next over-budget warn (if any) sees
            // a clean baseline.
            session.set_in_transition(matches!(
                paint_kind,
                Some(IpcPaintKind::Transition)
            ));

            // Linux paint hook: when the dispatcher returned a
            // PaintSlide / PaintTransition OpResult, fire the
            // actual GL paint. If paint errors, override the
            // response so the caller sees Err{message} rather
            // than a fake-success response. resp is moved in (no
            // other readers post-paint_kind extraction above);
            // PaintTransition's kind String moves through the
            // hook and back into the returned response with zero
            // clones.
            let resp = run_paint_hook(
                resp,
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
            // `[perf]` r1 addendum: snapshot the session-cumulative
            // deadline-miss counters so the summary line carries the
            // new frames_observed_total + frames_over_budget_total
            // keys. The summary emitter prints them verbatim
            // (monotonically non-decreasing across the session);
            // downstream parsers diff consecutive windows to get a
            // per-window rate.
            paint_metrics.maybe_emit_summary(
                session.frames_observed_total(),
                session.frames_over_budget_total(),
            );
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
    // Round-18: peek (no LRU touch) -- (a) cache parameter is
    // `&SlideCache` (immutable) and LruMap::get takes &mut self
    // post-r8, so a `get` here would E0596; (b) Capture is a one-
    // off operator IPC op, not a per-frame paint -- touching the
    // LRU order on a snapshot would unhelpfully promote whatever
    // slide happens to be on-screen at capture time.
    let item = match cache.items.peek(&slide_id) {
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
    resp: IpcResponse,
    session: &mut crate::hdmi::EglSession,
    card: &crate::Card,
    cache: &mut SlideCache,
    fonts: Option<&FontCatalog>,
    content_root: Option<&Path>,
) -> IpcResponse {
    use crate::content::ContentItem;
    use crate::hdmi;

    // perf-night r1.1 hotfix (2026-05-26): IPC mode bypasses the
    // standalone slide-loop's record_phase/frame_complete wrappers
    // (those live in hdmi.rs's hold loops, exercised only by
    // --profile-frames). Without these wrappers, profile_dump only
    // captured commit_fb's internal phases and frames_remaining never
    // decremented (auto-end never fired). Wrap each arm here with a
    // phase tag + frame_complete so an IPC-driven session emits
    // useful per-frame stats.
    //
    // perf-night r3 (2026-05-26): also wrap the dispatch gap with
    // `paint_dispatch` -- captures cache lookup + kind discriminator
    // + validate_paint_slide_inputs cost BEFORE the heavy paint helper
    // call. r2's ipc_paint_total max=631ms was outside any sub-phase
    // sum, suggesting the outlier hides in this gap (slide-cache
    // get_mut + ContentItem clone borrow gymnastics).
    let t_hook = std::time::Instant::now();
    let t_dispatch = t_hook;

    // Move-by-value: destructure resp into an owned OpResult so the
    // success path can re-pack via field-init shorthand without
    // cloning. Previously this took &IpcResponse and returned
    // resp.clone() per arm, which for PaintTransition heap-allocated
    // a fresh kind: String each frame (~30 Hz steady-state).
    let result = match resp {
        IpcResponse::Ok { result } => result,
        // Pass through errors unchanged.
        e @ IpcResponse::Err { .. } => return e,
    };
    let out = match result {
        OpResult::PaintSlide { slide_id, t_in_slide_ms } => {
            // Clone the borrow shape we need so we can take a
            // mutable borrow on cache.video_decoders later for
            // the Video branch without re-entering the borrow.
            // Text/Image only need an immutable items lookup.
            let item_kind = match cache.items.get(&slide_id) {
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
                let item = cache.items.get(&slide_id).expect("checked above");
                if let Err(msg) = validate_paint_slide_inputs(item, content_root) {
                    return err(msg);
                }
            }
            match item_kind {
                "text" => {
                    // r46 (2026-06-02): SYSTEM_SPEC §5.10 -- TextSlide
                    // with background_video_slide_id routes to the
                    // text-over-video paint path that bakes a V4L2
                    // video frame into the FBO before compositing
                    // text layers on top. cache.load /
                    // ensure_bg_video_for_text_slide has already
                    // primed the bg video's demuxer + decoder.
                    //
                    // Mutex warn: if a hand-edited payload bypassed
                    // Python validator and set BOTH background_image
                    // _slide_id AND background_video_slide_id, video
                    // wins here (this dispatch fires first); the
                    // warn names both ids for diagnosis. The standard
                    // image-vs-pattern precedence in resolve_slide_bg
                    // (image wins) is preserved -- text-over-video
                    // is a different paint pipeline.
                    #[cfg(target_os = "linux")]
                    let bg_video_id = match cache.items.get(&slide_id) {
                        Some(ContentItem::Text(s)) => {
                            if s.background_video_slide_id.is_some()
                                && s.background_image_slide_id.is_some()
                            {
                                eprintln!(
                                    "ipc: r46 mutex warn -- slide {} has BOTH background_video_slide_id={:?} AND background_image_slide_id={:?}; Python validator should have rejected; video wins at this dispatcher",
                                    slide_id,
                                    s.background_video_slide_id,
                                    s.background_image_slide_id,
                                );
                            }
                            s.background_video_slide_id
                        }
                        _ => unreachable!("item_kind matched text"),
                    };
                    #[cfg(target_os = "linux")]
                    if let Some(bg_id) = bg_video_id {
                        // r110 c3.3.2 subagent BLOCKER-1 fix: if a
                        // recreate worker is in flight for this
                        // bg_video_id, short-circuit the paint
                        // BEFORE invoking the decoder. The c3.2.2
                        // poster painted at the last transition
                        // tick stays on glass (no swap = no
                        // overwrite). This is the "wedged decoder
                        // stays in cache; slide-hold paint gets
                        // Ok(None) → glass holds last frame" path
                        // promised by the c3.3.2 design, made
                        // safe by NOT actually invoking the
                        // wedged decoder (which would Err via
                        // feed() 100ms timeout → backend slide-
                        // skip rail → user never sees poster).
                        if cache.pending_recreates.contains_key(&bg_id) {
                            eprintln!(
                                "[perf] c3_3_2_paint_hold_for_recreate kind=text_over_video slide_id={} bg_video_id={} t_in_slide_ms={}",
                                slide_id, bg_id, t_in_slide_ms,
                            );
                            return IpcResponse::Ok {
                                result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                            };
                        }
                        // Pull bg-video state. If either is missing
                        // (best-effort load failure), warn + fall
                        // through to the standard text paint path so
                        // the slide still renders (text on solid bg).
                        let dem_present = cache.video_demuxers.contains_key(&bg_id);
                        let dec_present = cache.video_decoders.contains_key(&bg_id);
                        if dem_present && dec_present {
                            let dem = cache.video_demuxers
                                .get(&bg_id)
                                .expect("dem_present checked above");
                            let dec_state = cache.video_decoders
                                .get_mut(&bg_id)
                                .expect("dec_present checked above");
                            // r46.3 (2026-06-02): if the bg video decoder
                            // has reached end-of-stream (next_sample_idx
                            // wrapped past samples.len() during prior
                            // playback OR a FLAG_LAST mid-stream signal
                            // set capture_drained=true in V4L2 inner),
                            // re-prime via the existing canonical
                            // reprime_video_decoder_for_loop path
                            // (STREAMOFF + clear drained + STREAMON +
                            // re-feed SPS+PPS+IDR per its r46.3 update).
                            // Mirrors render_video_slide_in_session at
                            // hdmi.rs:3025-3034 (the standalone reel
                            // wrap-and-reprime pattern). Without this
                            // the r46.2 keep_ids memoization preserves
                            // a drained decoder; bake_video_slide_to_
                            // current_fbo's next_frame() returns Ok(None)
                            // forever; paint early-returns before
                            // swap+commit; FYS panel goes BLACK.
                            if dec_state.next_sample_idx >= dem.samples.len() {
                                if let Err(e) =
                                    crate::video_decode::reprime_video_decoder_for_loop(
                                        dec_state, dem,
                                    )
                                {
                                    return err(format!(
                                        "paint_slide (text-over-video): reprime_video_decoder_for_loop failed: {e:#}"
                                    ));
                                }
                            }
                            let item = cache.items.get(&slide_id)
                                .expect("checked above");
                            let slide = match item {
                                ContentItem::Text(s) => s,
                                _ => unreachable!("item_kind matched text"),
                            };
                            crate::profile::record_phase(
                                "paint_dispatch",
                                t_dispatch.elapsed().as_nanos() as u64,
                            );
                            if let Err(e) = hdmi::paint_and_present_one_text_over_video_slide_frame(
                                session,
                                card,
                                slide,
                                fonts,
                                content_root,
                                t_in_slide_ms,
                                &dem.samples,
                                &mut dec_state.next_sample_idx,
                                &mut dec_state.frames_decoded,
                                &dec_state.decoder,
                            ) {
                                return err(format!("paint_slide (text-over-video) failed: {e:#}"));
                            }
                            return IpcResponse::Ok {
                                result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                            };
                        }
                        eprintln!(
                            "ipc: text slide {} references bg video {} but demuxer={} decoder={}; falling back to text-only paint",
                            slide_id, bg_id, dem_present, dec_present
                        );
                    }
                    let item = cache.items.get(&slide_id).expect("checked above");
                    let slide = match item {
                        ContentItem::Text(s) => s,
                        _ => unreachable!("item_kind matched text"),
                    };
                    crate::profile::record_phase(
                        "paint_dispatch",
                        t_dispatch.elapsed().as_nanos() as u64,
                    );
                    if let Err(e) = hdmi::paint_and_present_one_frame_for_slide(
                        session,
                        card,
                        slide,
                        fonts,
                        content_root,
                        t_in_slide_ms,
                    ) {
                        return err(format!("paint_slide failed: {e:#}"));
                    }
                    IpcResponse::Ok {
                        result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                    }
                }
                "image" => {
                    // Validator above already enforced content_root
                    // presence; unwrap is safe here.
                    let cr = content_root.expect(
                        "validate_paint_slide_inputs guarantees content_root for Image",
                    );
                    let item = cache.items.get(&slide_id).expect("checked above");
                    let slide = match item {
                        ContentItem::Image(s) => s,
                        _ => unreachable!("item_kind matched image"),
                    };
                    crate::profile::record_phase(
                        "paint_dispatch",
                        t_dispatch.elapsed().as_nanos() as u64,
                    );
                    if let Err(e) = hdmi::paint_and_present_one_image_slide_frame(
                        session, card, slide, cr,
                    ) {
                        return err(format!("paint_slide (image) failed: {e:#}"));
                    }
                    IpcResponse::Ok {
                        result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                    }
                }
                "video" => {
                    // r110 c3.3.2 subagent BLOCKER-1 fix (pure
                    // video mirror of the bg-video gate above):
                    // if a recreate worker is in flight for this
                    // slide_id, short-circuit BEFORE invoking the
                    // (now-removed by BLOCKER-2 sync drop) decoder.
                    // Glass holds the c3.2.2 poster painted at
                    // the last transition tick.
                    #[cfg(target_os = "linux")]
                    if cache.pending_recreates.contains_key(&slide_id) {
                        eprintln!(
                            "[perf] c3_3_2_paint_hold_for_recreate kind=pure_video slide_id={} t_in_slide_ms={}",
                            slide_id, t_in_slide_ms,
                        );
                        return IpcResponse::Ok {
                            result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                        };
                    }
                    // V4L2 piece 3e: drive one frame of decode +
                    // upload + paint per advance tick. Requires
                    // the demuxer + decoder primed in cache.load.
                    let dem = match cache.video_demuxers.get(&slide_id) {
                        Some(d) => d,
                        None => {
                            return err(format!(
                                "paint_slide (video): no demuxer for slide {slide_id} (asset.mp4 missing or malformed at begin_slide?)"
                            ));
                        }
                    };
                    let dec_state = match cache.video_decoders.get_mut(&slide_id) {
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
                    crate::profile::record_phase(
                        "paint_dispatch",
                        t_dispatch.elapsed().as_nanos() as u64,
                    );
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
                    IpcResponse::Ok {
                        result: OpResult::PaintSlide { slide_id, t_in_slide_ms },
                    }
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
            let from_id = from;
            let to_id = to;

            // Determine endpoint kinds without holding borrows on
            // cache.items past the kind discriminator (so the
            // subsequent video-state lookups don't conflict).
            //
            // r50 (2026-06-03): 'b' kind = text-over-video
            // (TextSlide with background_video_slide_id per
            // SYSTEM_SPEC §5.10). Closes the §F.new gap from r46:
            // pre-r50 a TextSlide with bg_video_id matched 't' so
            // the transition painted the text on solid (bg dropped)
            // for the transition's duration. r50 routes 'b' to a
            // new endpoint that decodes the bg video frame +
            // composites text on top, identical to the steady-
            // state paint_and_present_one_text_over_video_slide_
            // frame path.
            let from_kind = match cache.items.peek(&from_id) {
                Some(ContentItem::Text(s)) => {
                    if s.background_video_slide_id.is_some() { 'b' } else { 't' }
                }
                Some(ContentItem::Image(_)) => 'i',
                Some(ContentItem::Video(_)) => 'v',
                None => return err(format!("paint_transition: from slide {from_id} not in cache")),
            };
            let to_kind = match cache.items.peek(&to_id) {
                Some(ContentItem::Text(s)) => {
                    if s.background_video_slide_id.is_some() { 'b' } else { 't' }
                }
                Some(ContentItem::Image(_)) => 'i',
                Some(ContentItem::Video(_)) => 'v',
                None => return err(format!("paint_transition: to slide {to_id} not in cache")),
            };

            // r50: resolve the V4L2 decoder lookup id per side.
            // For 'v' kind, the decoder is keyed by slide_id. For
            // 'b' kind, it's keyed by the TextSlide's
            // background_video_slide_id. Other kinds: None (no
            // decoder needed).
            let from_dec_id: Option<uuid::Uuid> = match cache.items.peek(&from_id) {
                Some(ContentItem::Text(s)) => s.background_video_slide_id,
                Some(ContentItem::Video(_)) => Some(from_id),
                _ => None,
            };
            let to_dec_id: Option<uuid::Uuid> = match cache.items.peek(&to_id) {
                Some(ContentItem::Text(s)) => s.background_video_slide_id,
                Some(ContentItem::Video(_)) => Some(to_id),
                _ => None,
            };

            // Same-decoder transitions would need two &mut to the
            // same `VideoDecoderState` entry — impossible in safe
            // Rust AND semantically wrong (two drains per Advance
            // call on the same decoder). r50: this check now
            // applies to BOTH 'v' and 'b' (or mixed); the conflict
            // is on the decoder lookup id, not the slide id.
            // Same-id text/text and image/image are still fine
            // (idempotent bakes).
            if let (Some(fid), Some(tid)) = (from_dec_id, to_dec_id) {
                if fid == tid {
                    return err(format!(
                        "paint_transition: same decoder id on both sides ({fid}) not supported \
                         (e.g. two text-over-video slides sharing one bg video)",
                    ));
                }
            }

            // Resolve V4L2 decoder state &muts up-front. Single
            // get_mut for single-decoder; `iter_mut` for dual-
            // decoder (Rust 1.85 lacks `HashMap::get_disjoint_mut`
            // — stable in 1.86). `iter_mut` yields disjoint &mut
            // per entry, which is what we need; safe-Rust
            // alternative to the unsafe raw-pointer dance.
            let (mut from_dec_state, mut to_dec_state): (
                Option<&mut VideoDecoderState>,
                Option<&mut VideoDecoderState>,
            ) = match (from_dec_id, to_dec_id) {
                (Some(fid), Some(tid)) => {
                    let mut a = None;
                    let mut b = None;
                    for (k, v) in cache.video_decoders.iter_mut() {
                        if *k == fid {
                            a = Some(v);
                        } else if *k == tid {
                            b = Some(v);
                        }
                        if a.is_some() && b.is_some() {
                            break;
                        }
                    }
                    if a.is_none() {
                        return err(format!(
                            "paint_transition: from decoder {fid} state missing \
                             (from_kind={from_kind})",
                        ));
                    }
                    if b.is_none() {
                        return err(format!(
                            "paint_transition: to decoder {tid} state missing \
                             (to_kind={to_kind})",
                        ));
                    }
                    (a, b)
                }
                (Some(fid), None) => {
                    let a = cache.video_decoders.get_mut(&fid);
                    if a.is_none() {
                        return err(format!(
                            "paint_transition: from decoder {fid} state missing \
                             (from_kind={from_kind})",
                        ));
                    }
                    (a, None)
                }
                (None, Some(tid)) => {
                    let b = cache.video_decoders.get_mut(&tid);
                    if b.is_none() {
                        return err(format!(
                            "paint_transition: to decoder {tid} state missing \
                             (to_kind={to_kind})",
                        ));
                    }
                    (None, b)
                }
                (None, None) => (None, None),
            };

            // r50 (2026-06-03): wrap-and-reprime check for each side
            // whose decoder needs it. Mirrors the steady-state paint
            // path at ipc_main.rs:~1687: when the userspace sample
            // counter has reached the end of the demuxer's sample
            // list, re-feed SPS+PPS+IDR (the canonical loop pattern)
            // BEFORE bake's call to feed() the next NAL. Without
            // this, a slide that played long enough to wrap once
            // would silently skip the SPS/PPS re-feed at the
            // transition boundary and the decoder would fail with
            // bcm2835's "feed sample 0 after EOS" error.
            //
            // The check runs against the resolved decoder lookup id
            // (from_dec_id / to_dec_id from above), which maps:
            //   - 'v' kind: slide_id
            //   - 'b' kind: TextSlide.background_video_slide_id
            //   - other:    None (no check needed)
            if let Some(fid) = from_dec_id {
                let dem = match cache.video_demuxers.get(&fid) {
                    Some(d) => d,
                    None => return err(format!(
                        "paint_transition: from demuxer {fid} missing for wrap-check",
                    )),
                };
                let dec = from_dec_state
                    .as_deref_mut()
                    .expect("from_dec_state set above for from_dec_id Some(_)");
                if dec.next_sample_idx >= dem.samples.len() {
                    if let Err(e) =
                        crate::video_decode::reprime_video_decoder_for_loop(dec, dem)
                    {
                        return err(format!(
                            "paint_transition: from decoder reprime_video_decoder_for_loop failed: {e:#}",
                        ));
                    }
                }
            }
            if let Some(tid) = to_dec_id {
                let dem = match cache.video_demuxers.get(&tid) {
                    Some(d) => d,
                    None => return err(format!(
                        "paint_transition: to demuxer {tid} missing for wrap-check",
                    )),
                };
                let dec = to_dec_state
                    .as_deref_mut()
                    .expect("to_dec_state set above for to_dec_id Some(_)");
                if dec.next_sample_idx >= dem.samples.len() {
                    if let Err(e) =
                        crate::video_decode::reprime_video_decoder_for_loop(dec, dem)
                    {
                        return err(format!(
                            "paint_transition: to decoder reprime_video_decoder_for_loop failed: {e:#}",
                        ));
                    }
                }
            }

            // Build TransitionEndpoints. ContentItem refs come from a
            // shared borrow on cache.items (field-disjoint from the
            // &mut video_decoders borrows above). Demuxer samples
            // come from a shared borrow on cache.video_demuxers.
            //
            // Round-18: endpoint_a + endpoint_b both borrow from
            // cache.items simultaneously (held through the paint call
            // below). Post-r8 LruMap::get takes &mut self for the LRU
            // touch -- two concurrent &mut self.items borrows would
            // E0499. So: (1) touch the LRU order ONCE per id via
            // dropped `get` calls (the borrow ends at the statement's
            // end, so the second touch is sequenced safely), (2) build
            // the actual endpoint borrows via `peek` (&self, no LRU
            // touch) so they can coexist. Net semantic: from_id + to_id
            // each get a single LRU touch per paint frame -- same as
            // pre-r8 except now explicitly sequenced.
            cache.items.get(&from_id);
            cache.items.get(&to_id);
            let endpoint_a = match cache.items.peek(&from_id) {
                Some(ContentItem::Text(s)) => {
                    if let Some(bg_id) = s.background_video_slide_id {
                        // r50: text-over-video. Decoder lookup id
                        // = bg_id (the referenced VideoSlide), NOT
                        // the text slide id. Demuxer also keyed by
                        // bg_id.
                        let demuxer = match cache.video_demuxers.get(&bg_id) {
                            Some(d) => d,
                            None => return err(format!(
                                "paint_transition: from text-over-video bg demuxer {bg_id} missing \
                                 (text slide {from_id} background_video_slide_id)",
                            )),
                        };
                        let dec_state = from_dec_state
                            .take()
                            .expect("from_dec_state set above for 'b' kind");
                        hdmi::TransitionEndpoint::TextOverVideo {
                            text_slide: s,
                            bg_samples: demuxer.samples.as_slice(),
                            bg_next_sample_idx: &mut dec_state.next_sample_idx,
                            bg_frames_decoded: &mut dec_state.frames_decoded,
                            bg_decoder: &dec_state.decoder,
                        }
                    } else {
                        hdmi::TransitionEndpoint::Text(s)
                    }
                }
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
            let endpoint_b = match cache.items.peek(&to_id) {
                Some(ContentItem::Text(s)) => {
                    if let Some(bg_id) = s.background_video_slide_id {
                        let demuxer = match cache.video_demuxers.get(&bg_id) {
                            Some(d) => d,
                            None => return err(format!(
                                "paint_transition: to text-over-video bg demuxer {bg_id} missing \
                                 (text slide {to_id} background_video_slide_id)",
                            )),
                        };
                        let dec_state = to_dec_state
                            .take()
                            .expect("to_dec_state set above for 'b' kind");
                        hdmi::TransitionEndpoint::TextOverVideo {
                            text_slide: s,
                            bg_samples: demuxer.samples.as_slice(),
                            bg_next_sample_idx: &mut dec_state.next_sample_idx,
                            bg_frames_decoded: &mut dec_state.frames_decoded,
                            bg_decoder: &dec_state.decoder,
                        }
                    } else {
                        hdmi::TransitionEndpoint::Text(s)
                    }
                }
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

            // r110 stage 3 commit 3.2.0 (2026-06-11) subagent
            // WARN-1 fix: poster-video ids are by construction
            // identical to the V4L2 decoder-lookup ids resolved
            // at `from_dec_id`/`to_dec_id` above (same Text-bg /
            // Video / _ dispatch on cache.items). Reuse them
            // verbatim so a future change to the bg-video
            // sourcing rule lands in ONE place. Pre-fix this
            // was a separate closure that duplicated the logic
            // — drift risk heading into c3.2.1/c3.2.2.
            let poster_a_video_id = from_dec_id;
            let poster_b_video_id = to_dec_id;
            if let Err(e) = hdmi::paint_and_present_one_transition_frame(
                session,
                card,
                endpoint_a,
                endpoint_b,
                fonts,
                content_root,
                &kind,
                progress,
                poster_a_video_id,
                poster_b_video_id,
            ) {
                return err(format!("paint_transition failed: {e:#}"));
            }
            // Re-pack: from/to are Copy (Uuid), progress is Copy
            // (f32), kind String moves back in without a heap alloc.
            // This is the zero-clone path that motivated the
            // move-by-value refactor.
            IpcResponse::Ok {
                result: OpResult::PaintTransition { from, to, kind, progress },
            }
        }
        // Non-paint OpResults: pass through unchanged.
        other => IpcResponse::Ok { result: other },
    };

    // perf-night r1.1 hotfix: emit per-IPC-paint phase + advance the
    // frame budget so capture auto-ends after N frames. Only fires on
    // Ok results -- error paths don't count toward the budget.
    if matches!(out, IpcResponse::Ok { .. }) {
        crate::profile::record_phase(
            "ipc_paint_total",
            t_hook.elapsed().as_nanos() as u64,
        );
        crate::profile::frame_complete();
    }
    out
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
    // r110 c3.3.2 (2026-06-11): non-blocking drain of any
    // in-flight c3.3.1 recreate workers that finished since
    // the last IPC tick. Installs fresh decoders, replacing
    // the wedged ones from the just-completed transition.
    // Cost: O(pending_recreates) is_finished() calls — bounded
    // at ~2 typical (one per dual-1080p transition endpoint),
    // each is_finished() is a non-blocking atomic load.
    try_drain_finished_recreates(cache);
    match req {
        IpcRequest::Open(_) => {
            err("Open already called; nested Open is not supported")
        }
        IpcRequest::BeginSlide(p) => {
            // r102.1: V3D BO probe at BeginSlide entry. Brackets
            // the slide-change boundary so QA can compare slide-
            // change delta vs transition-paint delta.
            crate::v4l2::log_v3d_bos_at_phase("begin_slide_load_entry", Some(p.slide_id));
            // r65 subagent (WARN fix): opportunistically drain
            // stale pending_preloads (e.g. PreloadSlide(A) fired
            // but operator changed playlist before A's slot).
            // 10 s is a comfortable envelope vs the 2 s preload
            // lead -- any handle older than 10 s reflects a
            // never-arriving BeginSlide. Walk is O(pending_preloads)
            // ≤ MAX_CONCURRENT_PRELOADS = 2.
            drain_stale_preloads(cache, std::time::Duration::from_secs(10));
            // r65 (2026-06-05): if an async preload for this
            // slide_id is still in flight, join it now + install
            // the artifacts BEFORE the existing eviction +
            // cache.load run. Steady state: wait_us ~= 0 µs
            // (worker finished long before BeginSlide fires for
            // the same id). Worst case: wait_us = the residual
            // V4L2 prime time we couldn't hide behind the slide
            // hold -- which is exactly the stall pre-r65 had,
            // now sequestered to a single observable [perf]
            // begin_slide_wait line.
            ensure_preload_complete(cache, p.slide_id);
            // FYS bug 9 (2026-05-20): release the previous video
            // slide's V4L2 decoder before loading this slide —
            // decoders were otherwise never freed on a normal
            // slide change and accumulated toward OOM.
            //
            // r46.2 (2026-06-02): when the new slide is a TextSlide
            // with `background_video_slide_id` (per SYSTEM_SPEC
            // §5.10), the bg video's V4L2 state must ALSO survive
            // the eviction or the IPC text-arm will re-prime it on
            // every BeginSlide (a fresh ~24 MB V4L2 pool alloc per
            // cycle). Under CMA pressure, the prior pool's kernel-
            // side dma_buf refs don't release fast enough → CMA
            // accumulates → eventually `prime_video_decoder` Err →
            // UnsupportedSlide → 3s held-last-frame retry. Looked
            // like the text-over-video slide was freezing ~80% of
            // the time. Looking up the bg_video_id here (BEFORE
            // eviction) and passing both ids to keep_ids preserves
            // the bg video state; the subsequent `cache.load(text_
            // id)` then short-circuits + `ensure_bg_video_for_text_
            // slide`'s recursive `cache.load(bg_id)` also short-
            // circuits (items+mtime hit, demuxer + decoder present
            // post-evict). No re-prime; no fresh V4L2 alloc; CMA
            // pressure stays bounded.
            //
            // The lookup tolerates a cache miss (first BeginSlide
            // for a never-seen slide: items[slide_id]=None →
            // bg_video_id=None → keep_ids=[slide_id] → standard
            // single-keep behavior; the bg video gets primed on
            // cache.load's recursive path as before). The lookup
            // also tolerates a non-Text item kind (an Image or
            // Video slide → bg_video_id=None → keep_ids=[slide_id]
            // → standard behavior).
            let bg_video_id = match cache.items.peek(&p.slide_id) {
                Some(ContentItem::Text(s)) => s.background_video_slide_id,
                _ => None,
            };
            // r110 stage 3 commit 3.3 (2026-06-11) simplified per QA
            // amended spec: at BeginSlide for B, if B's video
            // decoder is in failed/no-frame state (frames_decoded
            // == 0, the reloc-starved corpse from the dual-decode
            // overlap window), TEAR IT DOWN by NOT including it in
            // keep_ids — the subsequent evict_other_video_state +
            // cache.load creates a FRESH decoder primed from t=0.
            //
            // Single-1080p decode has rated capability + months of
            // steady-state history once it's the only instance
            // (per QA c3.3 directive). The poster (already on glass
            // from c3.2.2's bake_b path during the transition
            // window) covers the recreate+prime latency. First
            // decoded frame == frame 0 == the poster → invisible
            // handoff per c3.1.1 BT.709 limited contract.
            //
            // r110 stage 3 commit 3.3.1 (2026-06-11): REPLACES
            // c3.3's frames_decoded==0 heuristic with EXPLICIT
            // poster-sourced signal. c3.3 (f681f7e) fired on
            // every preloaded video slide because
            // video_decode.rs:825 + :1058 reset frames_decoded=0
            // on healthy preload — decoder_drop/REQBUFS storm
            // crashed FYS renderer + wedged bcm2835-codec
            // kernel-side. See feedback memory
            // "subagent-reset-then-check-inverted-logic".
            //
            // c3.3.1 contract: recreate IFF the just-completed
            // transition actually sourced from poster for this
            // video_id (signal written by c3.2.2 bake_a/bake_b
            // poster fast-paths via poster_source_event,
            // cleared at BeginTransition entry). 720p success
            // path: live decoder produced, poster fast-path
            // never entered → no recreate. 1080p wedge: poster
            // sourced → recreate. Pure helper
            // `should_recreate_decoder` factored to hdmi.rs
            // with host-portable unit tests (closes c3.3
            // subagent WARN-2).
            let is_pure_video = matches!(
                cache.items.peek(&p.slide_id),
                Some(ContentItem::Video(_))
            );
            // cfg-gated: hdmi module is Linux-only (no GL/V4L2
            // on macOS); on non-Linux dev builds the recreate
            // predicate is a no-op since the poster fast-path
            // never fires there either.
            #[cfg(target_os = "linux")]
            let pure_video_failed = crate::hdmi::should_recreate_decoder(
                is_pure_video,
                crate::hdmi::poster_was_sourced_for(p.slide_id),
            );
            #[cfg(not(target_os = "linux"))]
            let pure_video_failed = { let _ = is_pure_video; false };
            // r110 c3.3.1 subagent WARN-2 fix: mirror the
            // `bg_id != p.slide_id` self-reference guard from
            // the keep_ids dedup below. Defends against a Text
            // slide with background_video_slide_id == its own id
            // (validator-rejected per upstream contracts but
            // defended in `ensure_bg_video_for_text_slide`). With
            // the guard: even if such a slide slipped through,
            // bg_video_failed stays false → bg path not double-
            // counted in the recreate logic.
            #[cfg(target_os = "linux")]
            let bg_video_failed = match bg_video_id {
                Some(bg_id) if bg_id != p.slide_id => {
                    crate::hdmi::should_recreate_decoder(
                        true,
                        crate::hdmi::poster_was_sourced_for(bg_id),
                    )
                }
                _ => false,
            };
            #[cfg(not(target_os = "linux"))]
            let bg_video_failed = false;
            // r110 c3.3.2 (2026-06-11) — ASYNC recreate. c3.3.1
            // shipped synchronous evict+cache.load which cost
            // ~4s at 1080p (mp4_open 0.7s + prime 3.1s,
            // journal-measured on FYS) and tripped the backend
            // IPC deadline → SIGTERM. c3.3.2 instead SPAWNS
            // preload_in_worker for the failed video_id +
            // returns from BeginSlide in ms.
            //
            // The wedged decoder STAYS in cache.video_decoders
            // during the spin-up window (~3-4s). The slide-hold
            // paint will call into it and get Ok(None) / no new
            // frames; the existing "no frame this tick" handling
            // skips the paint, and the glass holds the last
            // frame painted by c3.2.2 — which IS the poster.
            // So glass shows the poster for the entire spin-up
            // window. When the worker finishes, the next
            // handle_inner_request entry calls
            // try_drain_finished_recreates which installs the
            // fresh decoder REPLACING the wedged one. First
            // decoded frame == frame 0 == poster (c3.1.1 BT.709
            // contract) → invisible handoff to live video.
            //
            // keep_ids is built WITHOUT the failed-decoder
            // omission (since we're NOT evicting them
            // synchronously). The standard evict + cache.load
            // run below short-circuits for the wedged decoder
            // (still in cache via items+demuxer+decoder all
            // present) — no synchronous re-prime cost.
            #[cfg(target_os = "linux")]
            {
                let mut recreate_ids: Vec<uuid::Uuid> = Vec::with_capacity(2);
                if pure_video_failed {
                    eprintln!(
                        "[perf] c3_3_2_recreate_spawn slide_id={} video_id={} kind=pure_video reason=poster_sourced_this_transition",
                        p.slide_id, p.slide_id,
                    );
                    recreate_ids.push(p.slide_id);
                }
                if bg_video_failed {
                    if let Some(bg_id) = bg_video_id {
                        eprintln!(
                            "[perf] c3_3_2_recreate_spawn slide_id={} video_id={} kind=text_over_video reason=poster_sourced_this_transition",
                            p.slide_id, bg_id,
                        );
                        recreate_ids.push(bg_id);
                    }
                }
                for video_id in recreate_ids {
                    // Don't spawn if there's already an in-flight
                    // recreate for this id (e.g., a back-to-back
                    // transition before the prior worker finished).
                    if cache.pending_recreates.contains_key(&video_id) {
                        eprintln!(
                            "[perf] c3_3_2_recreate_skip_already_pending video_id={}",
                            video_id,
                        );
                        continue;
                    }
                    // r110 c3.3.2 subagent BLOCKER-2 fix: drop the
                    // wedged decoder + demuxer SYNCHRONOUSLY here
                    // BEFORE spawning the worker. Otherwise the
                    // worker would REQBUFS a fresh decoder while
                    // the wedged one still holds the
                    // single-instance bcm2835-codec → likely the
                    // root cause of QA's REQBUFS-EINVAL pathology
                    // (Job 2). The Drop cost is what c3.3.1 paid
                    // synchronously anyway (~hundreds of ms for
                    // STREAMOFF + destroy_image per r101.x
                    // history); fits inside the SIGTERM budget
                    // (~2-3s) and leaves the kernel codec slot
                    // free for the worker's fresh REQBUFS.
                    //
                    // The PaintSlide gate from subagent BLOCKER-1
                    // fix uses pending_recreates (not decoder
                    // presence) as the "spin-up in progress"
                    // signal, so paint sees no-decoder and
                    // short-circuits to "hold last frame" (which
                    // is the c3.2.2 poster).
                    let t_drop = std::time::Instant::now();
                    cache.video_decoders.remove(&video_id);
                    cache.video_demuxers.remove(&video_id);
                    let drop_us = t_drop.elapsed().as_micros();
                    eprintln!(
                        "[perf] c3_3_2_wedged_decoder_dropped video_id={} drop_us={}",
                        video_id, drop_us,
                    );
                    let content_root_clone = content_root.to_path_buf();
                    let video_id_clone = video_id;
                    let t_enqueue = std::time::Instant::now();
                    let spawn_result = std::thread::Builder::new()
                        .name(format!("recreate-{video_id}"))
                        .spawn(move || -> PreloadResult {
                            let t_work = std::time::Instant::now();
                            let r = preload_in_worker(&content_root_clone, video_id_clone);
                            let work_us = t_work.elapsed().as_micros();
                            match &r {
                                Ok(_) => eprintln!(
                                    "[perf] c3_3_2_recreate_worker_done video_id={} work_us={}",
                                    video_id_clone, work_us,
                                ),
                                Err(e) => eprintln!(
                                    "[perf] c3_3_2_recreate_worker_failed video_id={} work_us={} err={:#}",
                                    video_id_clone, work_us, e,
                                ),
                            }
                            r
                        });
                    match spawn_result {
                        Ok(thread) => {
                            cache.pending_recreates.insert(
                                video_id,
                                PreloadHandle { thread, enqueued_at: t_enqueue },
                            );
                        }
                        Err(e) => {
                            eprintln!(
                                "[perf] c3_3_2_recreate_spawn_failed video_id={} err={:#} (slide will stay on poster until next BeginTransition resets)",
                                video_id, e,
                            );
                        }
                    }
                }
            }
            let mut keep_ids: Vec<uuid::Uuid> = Vec::with_capacity(2);
            keep_ids.push(p.slide_id);
            if let Some(bg_id) = bg_video_id {
                if bg_id != p.slide_id {
                    keep_ids.push(bg_id);
                }
            }
            cache.evict_other_video_state(&keep_ids);
            // r58 (2026-06-04): time the cache.load so QA can see
            // when a PreloadSlide pre-warm shaved the BeginSlide
            // critical path. A small us (≲ 1 ms) means short-
            // circuit on items+mtime+demuxer+decoder hit (pre-warm
            // landed); a large us (~70-270 ms range) means cold
            // load (pre-warm missed or wasn't sent).
            let t_load = std::time::Instant::now();
            if let Err(e) = cache.load(content_root, p.slide_id) {
                return err(format!("begin_slide load failed: {e:#}"));
            }
            let load_us = t_load.elapsed().as_micros();
            eprintln!(
                "[perf] begin_slide_load slide_id={} load_us={}",
                p.slide_id, load_us,
            );
            // Bug 8 / Fix A (2026-05-17): cache.load succeeded
            // populating ContentItem::Video, but the underlying
            // MP4 demuxer failed (asset.mp4 missing, malformed, or
            // carrying no H.264 video trak). The wire marker
            // "video slide unsupported (load failed)" matches
            // `_UNSUPPORTED_SLIDE_WIRE_MARKERS` in
            // backend/openmarquee/rendering/rust_renderer.py so
            // Python raises `RustRendererUnsupportedSlideError`,
            // which `_play_via_rust_ipc`'s existing handler skips
            // gracefully (log INFO + return False + outer-loop
            // continues to next item).
            if cache.video_skip.contains(&p.slide_id) {
                return err(format!(
                    "video slide unsupported (load failed): {} — \
                     asset.mp4 missing, malformed, or has no \
                     H.264 video trak",
                    p.slide_id
                ));
            }
            state.begin_slide(p.slide_id, p.t0_ms, p.duration_ms);
            ok_empty()
        }
        IpcRequest::BeginTransition(p) => {
            // r110 c3.3.1 (2026-06-11): clear the poster-sourced
            // signal at transition entry so it scopes exactly to
            // "this transition window." c3.2.2 bake_a/bake_b
            // poster fast-paths during this transition will set
            // it for any video_id whose poster sourced; c3.3.1's
            // BeginSlide predicate reads it after the transition
            // ends.
            #[cfg(target_os = "linux")]
            crate::hdmi::clear_poster_source_record();
            // r102.1: V3D BO probe at BeginTransition entry.
            // Brackets the transition kick-off so QA can sum
            // begin->paint_entry->paint_exit deltas across
            // a single transition.
            crate::v4l2::log_v3d_bos_at_phase("begin_transition_load_entry", Some(p.to_slide_id));
            // r76 Phase A (2026-06-07): record the begin_transition
            // timestamp so the FIRST successful endpoint_b bake
            // inside paint_and_present_one_transition_frame can
            // emit the gap metric. Done BEFORE ensure_preload_complete
            // so we measure from the actual call entry (which is
            // what qarl sees: "transition begins" wall-clock).
            // r76 subagent WARN-2: thread the from_id (current
            // slide's id) into the marker so the metric line
            // names both endpoints per the dispatch's ask.
            let from_id_for_metric = state.current.as_ref().map(|c| c.slide_id);
            crate::hdmi_logic::record_transition_begin_for_endpoint_b_metric(
                from_id_for_metric, p.to_slide_id,
            );
            // r65 (2026-06-05): join any in-flight async preload
            // for the to-slide BEFORE cache.load so the load
            // short-circuits on the artifacts the worker
            // produced. Identical semantics to the BeginSlide
            // path above; emits the same [perf]
            // begin_slide_wait line.
            ensure_preload_complete(cache, p.to_slide_id);
            // r58 (2026-06-04): time the to-slide cache.load so QA
            // can see PreloadSlide wins on the transition path too.
            // Same heuristic as begin_slide_load above.
            let t_load = std::time::Instant::now();
            if let Err(e) = cache.load(content_root, p.to_slide_id) {
                return err(format!("begin_transition load failed: {e:#}"));
            }
            let load_us = t_load.elapsed().as_micros();
            eprintln!(
                "[perf] begin_transition_load slide_id={} load_us={}",
                p.to_slide_id, load_us,
            );
            // Hardening C3 / M1 (2026-05-21): also re-prime the
            // FROM-slide. The transition paint path fetches the
            // from-endpoint demuxer / decoder with a HARD ERROR if
            // absent — yet a from-slide whose decoder got evicted
            // (single-instance vc4 M2M codec, Bug 9 slide-change
            // eviction) never gets re-primed here. `cache.load`
            // short-circuits cheaply when the slide is already
            // primed, so this is near-free in the common case and
            // mirrors the C1/H1 re-prime fix for the to-slide.
            // The from-slide id comes from PlaybackState.current
            // (begin_transition itself derives it the same way and
            // errors below if there's no current slide).
            if let Some(from_id) = state.current.as_ref().map(|c| c.slide_id) {
                if let Err(e) = cache.load(content_root, from_id) {
                    return err(format!("begin_transition load failed: {e:#}"));
                }
            }
            // Bug 8 / Fix A: same skip-marker check as BeginSlide,
            // applied to the to-slide of a transition.
            if cache.video_skip.contains(&p.to_slide_id) {
                return err(format!(
                    "video slide unsupported (load failed): {} — \
                     asset.mp4 missing, malformed, or has no \
                     H.264 video trak",
                    p.to_slide_id
                ));
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
        IpcRequest::Reconfigure(p) => {
            // QA H1 (2026-05-23) — partial implementation: brightness
            // + gamma applied in-place; rotation deferred.
            //
            // The HDMI inner loop intercepts Reconfigure BEFORE this
            // standard dispatch (sibling pattern to Capture at the
            // top of `run_open_and_inner_loop_linux`) so brightness /
            // gamma route into `session.apply_settings`. Reaching
            // this fallback means reconfigure was sent to a state-
            // only sidecar with no render session: rotation still
            // rejects via the typed error; brightness / gamma have no
            // shader to update so we surface a typed "needs HDMI
            // session" error instead of silently ACK'ing.
            match crate::playback::validate_reconfigure(
                &crate::content::Settings::default(), &p,
            ) {
                Err(e) => err(e.message()),
                Ok(_) if p.brightness.is_some() || p.gamma.is_some() => {
                    err(
                        "reconfigure: brightness/gamma require the \
                         HDMI render session — this state-only \
                         sidecar build has no shader uniforms to \
                         update; use the HDMI sidecar or update \
                         settings.json directly"
                    )
                }
                Ok(_) => ok_empty(),
            }
        }
        IpcRequest::BeginExternalFrames(_) => {
            // The Linux HDMI inner loop intercepts this op before
            // handle_inner_request (see run_open_and_inner_loop_
            // linux) and runs the frame pump. Reaching here means
            // begin_external_frames was sent to a non-HDMI / state-
            // only sidecar build, which has no frame channel.
            err("begin_external_frames requires the Linux HDMI sidecar")
        }
        IpcRequest::Close => {
            state.reset();
            ok_empty()
        }
        IpcRequest::ProfileStart(p) => {
            // perf-night r1 (2026-05-26): replace --profile-frames at
            // process start. enable() is idempotent-by-overwrite: a
            // second call mid-capture resets the budget to the new N
            // and starts a fresh sample window. The hot loop checks
            // is_enabled()/frames_remaining() so no flag-wiring
            // beyond this is needed.
            //
            // Subagent-flagged trap (r1 review): profile::enable(0)
            // would set frames_remaining=0 immediately, and the
            // backend's poll loop tests for "frames_remaining=0" in
            // the dump text to decide ready -- so a frames=0 start
            // produces ready=True with zero samples. The HTTP layer
            // enforces ge=1, but the IPC layer is its own contract;
            // mirror the Python proxy guard at rust_renderer.py.
            if p.frames == 0 {
                return err("profile_start: frames must be > 0");
            }
            crate::profile::enable(p.frames);
            ok_empty()
        }
        IpcRequest::ProfileDump => {
            // perf-night r1: pure read of the global sample store.
            // dump_text handles all three states (disabled / no
            // samples yet / has samples). Caller polls until the
            // body's first line shows frames_remaining=0.
            IpcResponse::Ok {
                result: OpResult::ProfileDumpOk {
                    text: crate::profile::dump_text(),
                },
            }
        }
        IpcRequest::PreloadSlide(p) => {
            // r58 (2026-06-04): pre-warm a slide's cache state ahead
            // of BeginSlide so the V4L2 decoder bring-up cost
            // (~70-270 ms per r56 measurement) happens off the
            // transition critical path.
            //
            // Implementation: just `cache.load(slide_id)`. For text-
            // over-video slides, cache.load recurses through
            // `ensure_bg_video_for_text_slide` which loads the bg
            // VideoSlide as well -- so PreloadSlide for a TextSlide
            // pre-warms BOTH the text slide and its bg video. For
            // bare Video slides, cache.load primes the decoder
            // directly. For Image / Text-only slides, cache.load
            // populates items + reads the JSON; cheap.
            //
            // Idempotent: a duplicate PreloadSlide on the same id
            // short-circuits at cache.load's items+mtime+demuxer+
            // decoder check (assuming evict_other_video_state didn't
            // fire between the two calls).
            //
            // Errors are non-fatal from the backend's POV. The
            // backend logs + continues; the slide hits the same
            // failure on its actual BeginSlide and the existing
            // UnsupportedSlide rail picks it up.
            //
            // We DO NOT evict here. evict_other_video_state runs
            // ONLY on BeginSlide, which is the "this slide is now
            // the current slide" anchor. PreloadSlide is a hint that
            // a slide MIGHT be the next current — the actual
            // eviction stays with BeginSlide where the lifecycle
            // already lives. During the overlap window between
            // PreloadSlide and BeginSlide, both the current slide's
            // and the preloaded slide's V4L2 decoders are live —
            // identical CMA shape to the existing BeginTransition
            // window (which also has both pools live). Peak does
            // not grow; the 2-pool window widens by ~500 ms.
            let preload_id = p.slide_id;
            let t_enqueue = std::time::Instant::now();
            // Already loaded? Short-circuit at near-zero cost.
            // The same items+demuxer+decoder check cache.load
            // uses; we replicate inline so we don't even build
            // mtime info on the common idempotent path.
            let already_loaded = cache.items.peek(&preload_id).is_some()
                && cache.video_demuxers.contains_key(&preload_id)
                && {
                    #[cfg(target_os = "linux")]
                    {
                        cache.has_video_decoder(preload_id)
                    }
                    #[cfg(not(target_os = "linux"))]
                    {
                        true
                    }
                };
            if already_loaded {
                let us = t_enqueue.elapsed().as_micros();
                // r65 subagent (BLOCKER fix): emit `us=N` alongside
                // the variant-specific key so r64's perf_stats
                // parser (which keys on `us`) records a meaningful
                // sample for each preload event. The variant key
                // disambiguates the SHAPE of the cost; `us` is
                // the value any aggregator that scans the
                // canonical key still sees.
                eprintln!(
                    "[perf] preload slide_id={} us={} cached_us={}",
                    preload_id, us, us,
                );
                return ok_empty();
            }
            // r65 subagent (WARN fix): unbounded spawn cap.
            // bcm2835-codec supports at most 4 concurrent
            // decoder instances; the natural transition envelope
            // is current-slide + next-preload = 2. Cap at 2 to
            // leave headroom for a transition that begins while
            // a preload is still in flight. When at capacity,
            // drop the new preload + let BeginSlide hit the
            // synchronous cache.load path (same cost shape as
            // pre-r65).
            const MAX_CONCURRENT_PRELOADS: usize = 2;
            if cache.pending_preloads.len() >= MAX_CONCURRENT_PRELOADS
                && !cache.pending_preloads.contains_key(&preload_id)
            {
                let us = t_enqueue.elapsed().as_micros();
                eprintln!(
                    "[perf] preload slide_id={} us={} dropped_us={} reason=capacity",
                    preload_id, us, us,
                );
                return ok_empty();
            }
            // Already in flight? No-op return (the existing
            // worker will finish; subsequent BeginSlide will
            // join it). Per-dispatch edge case #1 "Two
            // PreloadSlides in flight back-to-back".
            if cache.pending_preloads.contains_key(&preload_id) {
                let us = t_enqueue.elapsed().as_micros();
                eprintln!(
                    "[perf] preload slide_id={} us={} in_flight_us={}",
                    preload_id, us, us,
                );
                return ok_empty();
            }
            // r97 (2026-06-09): conditional deferred preload for
            // dual-video codec contention. The bcm2835-codec H.264
            // block on Pi Zero 2 W class hardware (BCM2710 VPU at
            // roughly half the clock of a Pi 4) cannot sustain two
            // simultaneous 1080p30 decodes — empirically (FYS
            // 2026-06-09) the second decoder gets ~0 frames during
            // its 500ms preload window while the first decoder is
            // still streaming, then catches up after the first
            // STREAMOFFs ~4 seconds later. By that time the
            // transition window has blown past + endpoint_b is black.
            //
            // The Linux bcm2835-codec driver enforces NO concurrency
            // cap; the limit lives in the closed-source VPU
            // firmware's H.264 scheduler. See
            // `docs/hardware-ceilings.md` for the full empirical
            // characterisation, including why we do NOT bake "1080p
            // is forever banned" into platform-agnostic code paths
            // (Pi 4 / 5 class hardware lifts the ceiling).
            //
            // Defer trigger: video-bg slide preload arriving while
            // an active video decoder already exists in the cache.
            // The subsequent BeginSlide will hit cache.load
            // synchronously AFTER `evict_other_video_state` runs,
            // which STREAMOFFs the previous decoder + opens a fresh
            // one. bake_b's Path B polling (r94) then catches the
            // first frame ~150ms in. Visible cost: slide-B's first
            // ~150ms of the transition is endpoint-A held (or the
            // existing skip degrade), instead of the catastrophic
            // marker_overwritten failure mode.
            //
            // We rely on `cache.items.peek` to identify a video-bg
            // slide without doing IO. In steady-state operation the
            // items LRU is hot for the current playlist; cold-start
            // preloads (items not yet cached) take the normal
            // spawn path — conservative for the bug at hand.
            #[cfg(target_os = "linux")]
            {
                let active_decoder_count = cache.video_decoders.len();
                let bg_is_video = match cache.items.peek(&preload_id) {
                    Some(crate::content::ContentItem::Video(_)) => true,
                    Some(crate::content::ContentItem::Text(s)) => {
                        s.background_video_slide_id.is_some()
                    }
                    Some(crate::content::ContentItem::Image(_)) => false,
                    None => false,
                };
                // r98 (2026-06-09): preload-mode gate. The r97 defer
                // logic runs only when OPENMARQUEE_PRELOAD_MODE is
                // 'defer' (default). 'lead' and 'max' modes force
                // preload through so QA can sweep the dual-1080p
                // experiment design space — the Python side governs
                // WHEN preload fires (time-based vs event-driven);
                // the Rust side just stops short-circuiting it.
                // When the mode is not 'defer', the r97
                // `preload_deferred_for_codec_contention` probe does
                // NOT fire because the deferral branch is bypassed.
                if preload_mode() == PreloadMode::Defer
                    && should_defer_preload_for_codec_contention(
                        active_decoder_count,
                        bg_is_video,
                    )
                {
                    let us = t_enqueue.elapsed().as_micros();
                    // List the active decoder ids so QA can correlate
                    // which decoder #1 is the contention partner.
                    let active_decoder_ids: Vec<String> = cache
                        .video_decoders
                        .keys()
                        .map(|id| id.to_string())
                        .collect();
                    eprintln!(
                        "[perf] preload_deferred_for_codec_contention slide_id={} \
                         active_decoder_count={} active_decoder_ids=[{}] deferral_us={}",
                        preload_id,
                        active_decoder_count,
                        active_decoder_ids.join(","),
                        us,
                    );
                    // Mirror the canonical preload_handoff shape with
                    // was_deferred=true so log-scanners can
                    // distinguish frames_drained=0-from-deferral from
                    // frames_drained=0-from-codec-contention (the
                    // smoking-gun line that drove this fix).
                    eprintln!(
                        "[perf] preload_handoff slide_id={} frames_drained=0 \
                         prime_only_us=0 drain_us=0 budget_ms=0 was_deferred=true",
                        preload_id,
                    );
                    eprintln!(
                        "[perf] preload slide_id={} us={} deferred_us={} \
                         reason=codec_contention",
                        preload_id, us, us,
                    );
                    return ok_empty();
                }
            }
            // r65 core: spawn worker. The main thread returns
            // immediately (just the cheap spawn cost). The worker
            // does the V4L2 prime + Mp4Demuxer::open in
            // parallel; ensure_preload_complete at the next
            // BeginSlide / BeginTransition for the same id joins
            // it and installs the artifacts.
            let content_root_clone = content_root.to_path_buf();
            let preload_id_clone = preload_id;
            let spawn_result = std::thread::Builder::new()
                .name(format!("preload-{preload_id}"))
                .spawn(move || -> PreloadResult {
                    let t_work = std::time::Instant::now();
                    let r = preload_in_worker(&content_root_clone, preload_id_clone);
                    let work_us = t_work.elapsed().as_micros();
                    match &r {
                        Ok(_) => eprintln!(
                            "[perf] preload_worker slide_id={} work_us={}",
                            preload_id_clone, work_us,
                        ),
                        Err(e) => eprintln!(
                            "[perf] preload_worker slide_id={} failed_us={} err={:#}",
                            preload_id_clone, work_us, e,
                        ),
                    }
                    r
                });
            match spawn_result {
                Ok(thread) => {
                    cache.pending_preloads.insert(
                        preload_id,
                        PreloadHandle { thread, enqueued_at: t_enqueue },
                    );
                    let queued_us = t_enqueue.elapsed().as_micros();
                    // BLOCKER fix: emit `us=N` alongside
                    // `queued_us=N` for r64 parser compatibility.
                    eprintln!(
                        "[perf] preload slide_id={} us={} queued_us={}",
                        preload_id, queued_us, queued_us,
                    );
                    ok_empty()
                }
                Err(e) => {
                    // Spawn failure (OS thread limit etc.) is
                    // rare; fall back to synchronous load so
                    // BeginSlide doesn't hit a cold path later.
                    eprintln!(
                        "ipc: preload spawn failed for {}: {:#}; falling back to synchronous cache.load",
                        preload_id, e,
                    );
                    let t_sync = std::time::Instant::now();
                    match cache.load(content_root, preload_id) {
                        Ok(()) => {
                            let sync_us = t_sync.elapsed().as_micros();
                            eprintln!(
                                "[perf] preload slide_id={} us={} sync_us={} (spawn failed)",
                                preload_id, sync_us, sync_us,
                            );
                            ok_empty()
                        }
                        Err(e2) => {
                            err(format!("preload_slide load failed: {e2:#}"))
                        }
                    }
                }
            }
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

    // ============================================================
    // r99 (2026-06-09): the diagnostics tail formatter for the
    // [mmal_leak_suspect] line. Adds CMA + peer-decoder context so
    // QA can see resource state at REQBUFS failure. Tests pin the
    // field shape so journal parsers can grep `cma_used_mb=` etc.
    // ============================================================

    #[test]
    #[cfg(target_os = "linux")]
    fn format_mmal_leak_diagnostics_tail_layout_without_cache() {
        // Worker-thread path: cache=None. CMA snapshot may or may
        // not be available depending on host; assert the field
        // PREFIXES are present so the format is parseable either
        // way.
        let tail = super::format_mmal_leak_diagnostics_tail(None, uuid(1));
        assert!(tail.contains("cma_used_mb="), "tail={tail:?}");
        assert!(tail.contains("cma_free_mb="), "tail={tail:?}");
        assert!(tail.contains("cma_total_mb="), "tail={tail:?}");
        // No cache -> peer fields render as placeholders.
        assert!(tail.contains("peer_decoder_count=?"), "tail={tail:?}");
        assert!(tail.contains("peer_decoder_dims=[?]"), "tail={tail:?}");
    }

    #[test]
    fn mmal_leak_suspect_emit_site_calls_diagnostics_tail() {
        // Source-grep regression-lock: both emit sites MUST invoke
        // format_mmal_leak_diagnostics_tail. A refactor that drops
        // the call would silently strip the r99 fields from the
        // log line.
        let src = include_str!("ipc_main.rs");
        // 2 production call sites + N test references.
        let prod_calls = src.matches("format_mmal_leak_diagnostics_tail(").count();
        // 2 prod (cache_load + preload_worker) + this test's own
        // reference + the test name above + 1 in the doc comment.
        // The exact count drifts as tests grow; the LOWER bound is
        // what matters.
        assert!(
            prod_calls >= 2,
            "expected at least 2 format_mmal_leak_diagnostics_tail call sites \
             (cache_load + preload_worker); got {prod_calls}",
        );
    }

    // ============================================================
    // r75 (2026-06-07): MMAL leak-suspect log -- extract_top_context.
    //
    // The [mmal_leak_suspect] line carries the outermost anyhow
    // `.with_context` string as the `op` tag so QA can grep + bucket
    // failures by failing op (REQBUFS OUTPUT, S_FMT OUTPUT, etc.)
    // without parsing the full chain. These tests pin the extractor
    // so a future anyhow API change can't silently lose the tag.
    // ============================================================

    #[test]
    fn extract_top_context_returns_outermost_context() {
        // Realistic shape from the prime path: bail!()/Err()-fail at
        // the ioctl layer, .context(...) at the V4L2 wrapper layer,
        // .context(...) again at prime_video_decoder_with_warmup.
        // The OUTER context is what we want for the QA grep tag.
        use anyhow::Context;
        let inner: anyhow::Error = anyhow::anyhow!("EINVAL");
        let middle: anyhow::Result<()> = Err::<(), _>(inner).context("REQBUFS OUTPUT");
        let outer: anyhow::Result<()> = middle.context("prime V4L2 decoder");
        let err = outer.unwrap_err();
        // anyhow's chain() yields the outermost first.
        let tag = extract_top_context(&err);
        assert_eq!(
            tag, "prime V4L2 decoder",
            "extract_top_context MUST return the outermost .context() tag"
        );
    }

    #[test]
    fn extract_top_context_returns_root_message_on_bare_anyhow_error() {
        // r75 subagent NIT-5: the prior name implied this exercised
        // the `unwrap_or_else` fallback branch, but anyhow::chain()
        // always yields the root error as the first element so that
        // branch is unreachable today. What this test actually pins:
        // a bare `anyhow!()` (no `.with_context()` chain) still
        // returns a useful op-tag (the bare message), so QA's grep
        // doesn't see `op=""`. The "fallback" in the function body
        // is purely defensive against a future anyhow change that
        // could yield an empty chain.
        let err: anyhow::Error = anyhow::anyhow!("VIDIOC_REQBUFS EINVAL");
        let tag = extract_top_context(&err);
        assert_eq!(tag, "VIDIOC_REQBUFS EINVAL");
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
            rotation: 0,
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
        assert!(cache.items.get(&uuid(1)).is_some());
        // State should reflect the slide.
        assert!(state.current.is_some());
        assert_eq!(state.current.as_ref().unwrap().slide_id, uuid(1));
    }

    #[test]
    fn handle_begin_slide_emits_unsupported_marker_when_video_demuxer_fails() {
        // Bug 8 / Fix A regression-lock. A VideoSlide whose
        // item.json is valid but whose asset.mp4 is missing
        // (or malformed -- the failure path is the same) must
        // result in BeginSlide returning an err carrying the
        // wire marker `_UNSUPPORTED_SLIDE_WIRE_MARKERS` recognizes
        // so Python's catch promotes to RustRendererUnsupportedSlideError
        // rather than the generic OpError that hot-spun the loop.
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let id = uuid(7);
        let dir = td.path().join(id.to_string());
        std::fs::create_dir_all(&dir).unwrap();
        // Valid video item.json, but no asset.mp4 alongside -- the
        // Mp4Demuxer::open call in cache.load returns Err, which
        // populates `video_skip` with `id`.
        let item_json = format!(
            r##"{{
              "schema_version": 3,
              "item": {{
                "type": "video",
                "id": "07070707-0707-0707-0707-070707070707",
                "name": "missing-asset",
                "duration_ms": 5000,
                "transition": "cut",
                "transition_ms": 500
              }}
            }}"##
        );
        std::fs::write(dir.join("item.json"), item_json).unwrap();
        let req = IpcRequest::BeginSlide(BeginSlideParams {
            slide_id: id,
            t0_ms: 0,
            duration_ms: 5000,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(
                    error.contains("video slide unsupported (load failed)"),
                    "expected UnsupportedSlide wire marker, got: {error}"
                );
            }
            other => panic!("expected Err with UnsupportedSlide marker, got {other:?}"),
        }
        // Skip marker recorded.
        assert!(cache.video_skip.contains(&id));
        // State NOT updated -- we returned err BEFORE state.begin_slide.
        assert!(state.current.is_none());
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

    /// Hardening C3 / M1 (2026-05-21): `BeginTransition` must
    /// re-prime the FROM-slide too, not just the to-slide. A
    /// from-slide whose video demuxer/decoder was evicted (Bug 9
    /// slide-change eviction, single-instance vc4 M2M codec) would
    /// otherwise reach the transition paint path with no demuxer
    /// and hard-error. This mirrors the C1/H1 to-slide re-prime
    /// test style: evict the from-slide's demuxer, then assert
    /// `BeginTransition` re-opens it.
    #[test]
    fn handle_begin_transition_reprimes_evicted_from_slide() {
        let video_fixture = {
            let mut p = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            p.push("tests");
            p.push("fixtures");
            p.push("test_320x240.mp4");
            p
        };
        let td = tempfile::TempDir::new().unwrap();
        // From-slide: a video.
        let id_from = uuid(1);
        let dir_from = td.path().join(id_from.to_string());
        std::fs::create_dir_all(&dir_from).unwrap();
        std::fs::write(
            dir_from.join("item.json"),
            r##"{
              "schema_version": 3,
              "item": {
                "type": "video",
                "id": "01010101-0101-0101-0101-010101010101",
                "name": "from-vid",
                "duration_ms": 2000,
                "transition": "cut",
                "transition_ms": 500
              }
            }"##,
        )
        .unwrap();
        std::fs::copy(&video_fixture, dir_from.join("asset.mp4")).unwrap();
        // To-slide: a plain text slide.
        let id_to = uuid(2);
        let dir_to = td.path().join(id_to.to_string());
        std::fs::create_dir_all(&dir_to).unwrap();
        std::fs::write(dir_to.join("item.json"), SAMPLE_TEXT_ITEM_FOR_UUID_1).unwrap();

        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        // Begin on the from-slide so it becomes `state.current`.
        let resp_begin = handle_inner_request(
            IpcRequest::BeginSlide(BeginSlideParams {
                slide_id: id_from,
                t0_ms: 0,
                duration_ms: 2000,
            }),
            &mut state,
            &mut cache,
            td.path(),
        );
        assert_eq!(resp_begin, IpcResponse::Ok { result: OpResult::Empty });
        assert!(
            cache.video_demuxers.contains_key(&id_from),
            "from-slide demuxer primed by BeginSlide",
        );
        // Simulate the Bug 9 slide-change eviction of the
        // from-slide's video state.
        cache.evict_other_video_state(&[uuid(9)]);
        assert!(
            !cache.video_demuxers.contains_key(&id_from),
            "from-slide demuxer evicted",
        );
        assert!(
            state.current.as_ref().map(|c| c.slide_id) == Some(id_from),
            "from-slide is still state.current after eviction",
        );
        // BeginTransition to the text to-slide must re-prime the
        // evicted from-slide's demuxer (M1 fix).
        let resp = handle_inner_request(
            IpcRequest::BeginTransition(BeginTransitionParams {
                to_slide_id: id_to,
                to_duration_ms: 5000,
                kind: "fade".to_string(),
                transition_ms: 800,
                t0_ms: 1000,
            }),
            &mut state,
            &mut cache,
            td.path(),
        );
        assert_eq!(resp, IpcResponse::Ok { result: OpResult::Empty });
        assert!(
            cache.video_demuxers.contains_key(&id_from),
            "BeginTransition must RE-PRIME the evicted from-slide demuxer (M1)",
        );
        assert!(state.pending.is_some());
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
    fn handle_reconfigure_rotation_returns_typed_unsupported_field_error() {
        // QA H1 (2026-05-23): rotation is the deferred arm — typed
        // error in BOTH the HDMI inner-loop interception and the
        // state-only standard dispatch tested here. The exact prefix
        // is part of the Python-facing wire contract.
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
                assert!(
                    error.starts_with(
                        "reconfigure: unsupported field 'rotation'"
                    ),
                    "expected the typed unsupported-field prefix; got: {error}"
                );
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_reconfigure_brightness_in_state_only_build_returns_typed_error() {
        // State-only sidecar (Mac unit-test build) has no render
        // session, so brightness/gamma have no shader to update.
        // The standard dispatch surfaces a typed "needs HDMI" error
        // instead of silently ACK'ing. The HDMI inner loop's
        // interception (covered by Pi-side integration) is what
        // actually applies the change in production.
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::Reconfigure(crate::playback::ReconfigureParams {
            rotation: None,
            brightness: Some(0.5),
            gamma: None,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(
                    error.contains("require the HDMI render session"),
                    "expected the state-only-build error; got: {error}"
                );
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_profile_start_rejects_zero_frames() {
        // r1 review (subagent finding #4): frames=0 would set
        // frames_remaining=0 immediately, and the backend ready-check
        // greps for "frames_remaining=0" -> dump returns ready=True
        // with zero captured samples (silent bad behavior). HTTP
        // layer Pydantic gate handles HTTP callers; this guard is
        // the IPC-contract twin.
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::ProfileStart(
            crate::playback::ProfileStartParams { frames: 0 },
        );
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Err { error } => {
                assert!(
                    error.starts_with("profile_start: frames must be > 0"),
                    "expected the zero-frames guard message; got: {error}"
                );
            }
            other => panic!("expected Err, got {other:?}"),
        }
    }

    #[test]
    fn handle_profile_start_then_dump_round_trips() {
        // perf-night r1 (2026-05-26): profile_start enables the global
        // profile; profile_dump returns the text snapshot. State-only
        // sidecar dispatches both through handle_inner_request (no HDMI
        // interception needed -- pure-data ops on the global sample
        // store). The dump's body shape locks the contract the FastAPI
        // perf endpoint relies on (`frames_remaining=N` first line).
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();

        let start_req = IpcRequest::ProfileStart(
            crate::playback::ProfileStartParams { frames: 42 },
        );
        let start_resp = handle_inner_request(start_req, &mut state, &mut cache, td.path());
        assert_eq!(start_resp, IpcResponse::Ok { result: OpResult::Empty });

        let dump_req = IpcRequest::ProfileDump;
        let dump_resp = handle_inner_request(dump_req, &mut state, &mut cache, td.path());
        match dump_resp {
            IpcResponse::Ok { result: OpResult::ProfileDumpOk { text } } => {
                // No frames have been completed yet -- enable but no
                // samples. Body shape: "profile: no samples
                // (frames_remaining=N)" since the global sample store
                // is empty until a record_phase fires.
                assert!(
                    text.starts_with("profile:") || text.contains("frames_remaining="),
                    "dump_text did not match either branch: {text:?}"
                );
            }
            other => panic!("expected Ok(ProfileDumpOk), got {other:?}"),
        }
    }

    #[test]
    fn handle_reconfigure_no_op_in_state_only_build_succeeds() {
        // A reconfigure carrying no fields at all (all None) is a
        // valid no-op and should ACK with ok_empty even from the
        // state-only sidecar — there's nothing to apply and nothing
        // to reject.
        let mut state = PlaybackState::new();
        let mut cache = SlideCache::new();
        let td = tempfile::TempDir::new().unwrap();
        let req = IpcRequest::Reconfigure(crate::playback::ReconfigureParams {
            rotation: None,
            brightness: None,
            gamma: None,
        });
        let resp = handle_inner_request(req, &mut state, &mut cache, td.path());
        match resp {
            IpcResponse::Ok { result } => {
                assert_eq!(result, OpResult::Empty);
            }
            other => panic!("expected Ok(Empty), got {other:?}"),
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
    /// Video; PaintSlide-side rejection (was "video slides TBD")
    /// is removed. Capture-side rejection remains at
    /// validate_capture_inputs (line 1424) with the distinct
    /// "Capture: VideoSlide capture not implemented" marker. The
    /// actual per-advance decode/paint is run by run_paint_hook
    /// against SlideCache.video_{decoders,demuxers}; the validator
    /// no longer gates Video. This test pins the new contract --
    /// any regression here would silently flip Video back to the
    /// UnsupportedSlide wire rail (playback loop log + skip per
    /// post-DELETE-PIL contract).
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

    /// QA code-quality loop v2 round 3 tripwire: SlideCache.items +
    /// item_mtimes are LRU-capped at SLIDE_CACHE_CAP, so a long
    /// playlist or operator-edit churn over hours can't grow them
    /// without bound. Pre-cap they were insert-only HashMaps -- same
    /// latent-OOM shape as the V4L2 decoder leak fixed in FYS bug 9.
    ///
    /// Test: load SLIDE_CACHE_CAP + 4 distinct text slides; verify
    /// `items` stays at the cap and the oldest entries (those not
    /// recently touched) are evicted. Uses a real disk roundtrip
    /// (matching cache_load_short_circuits_when_mtime_unchanged
    /// style) so the production load() path is the test subject,
    /// not just the underlying LruMap (which has its own tests in
    /// lru.rs).
    #[test]
    fn cache_load_evicts_oldest_when_over_capacity() {
        let td = tempfile::TempDir::new().unwrap();
        let mut cache = SlideCache::new();
        let n = SLIDE_CACHE_CAP + 4;
        let mut ids = Vec::with_capacity(n);
        for i in 0..n {
            let id = uuid(i as u8);
            ids.push(id);
            let dir = td.path().join(id.to_string());
            std::fs::create_dir_all(&dir).unwrap();
            std::fs::write(
                dir.join("item.json"),
                format!(
                    r##"{{
                      "schema_version": 3,
                      "item": {{
                        "type": "text_slide",
                        "id": "{}",
                        "name": "slot-{}",
                        "duration_ms": 5000,
                        "text_layers": [],
                        "background_color": "#222222",
                        "background_pattern": null,
                        "transition": "cut",
                        "transition_ms": 500
                      }}
                    }}"##,
                    id, i
                ),
            )
            .unwrap();
            cache.load(td.path(), id).expect("cache.load");
        }
        // Cap holds: no growth past SLIDE_CACHE_CAP regardless of
        // how many distinct slides loaded.
        assert_eq!(cache.items.len(), SLIDE_CACHE_CAP);
        assert_eq!(cache.item_mtimes.len(), SLIDE_CACHE_CAP);
        // Oldest 4 slides evicted (loaded first, never re-touched).
        for evicted in ids.iter().take(4) {
            assert!(
                cache.items.get(evicted).is_none(),
                "id {evicted} should have been LRU-evicted",
            );
        }
        // Most-recent SLIDE_CACHE_CAP slides resident.
        for retained in ids.iter().skip(4) {
            assert!(
                cache.items.get(retained).is_some(),
                "id {retained} should still be cached",
            );
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
        assert!(cache.items.get(&id).is_some(), "Video item must be in items");
        let dem = cache.video_demuxers.get(&id)
            .expect("Mp4Demuxer must be in video_demuxers when asset present");
        assert_eq!(dem.width, 320);
        assert_eq!(dem.height, 240);
        assert!(!dem.samples.is_empty());
    }

    /// FYS bug A (2026-05-21): after evict_other_video_state drops a
    /// video slide's Mp4Demuxer (Bug 9's per-slide-change eviction),
    /// a later cache.load for that slide must RE-OPEN the demuxer —
    /// not short-circuit on the still-present `items` entry. The
    /// pre-fix items+mtime short-circuit left an evicted video
    /// demuxer-less forever, so every playlist loop-back froze the
    /// sign (paint_slide / paint_transition had no decoder state).
    #[test]
    fn cache_load_reprimes_video_demuxer_after_eviction() {
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
        let fixture = {
            let mut p = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
            p.push("tests");
            p.push("fixtures");
            p.push("test_320x240.mp4");
            p
        };
        std::fs::copy(&fixture, dir.join("asset.mp4")).unwrap();
        let mut cache = SlideCache::new();
        cache.load(td.path(), id).expect("first load");
        assert!(
            cache.video_demuxers.contains_key(&id),
            "demuxer present after the first load",
        );
        // Simulate Bug 9's slide-change eviction: a BeginSlide on
        // some OTHER slide evicts this video's demuxer + decoder.
        cache.evict_other_video_state(&[uuid(6)]);
        assert!(
            !cache.video_demuxers.contains_key(&id),
            "demuxer evicted by evict_other_video_state",
        );
        assert!(
            cache.items.get(&id).is_some(),
            "the lightweight `items` entry deliberately survives eviction",
        );
        // The playlist loop-back: cache.load for the evicted slide
        // (item.json unchanged on disk) must RE-OPEN the demuxer
        // rather than short-circuit on the surviving `items` entry.
        cache.load(td.path(), id).expect("re-load after eviction");
        let dem = cache.video_demuxers.get(&id).expect(
            "demuxer must be RE-OPENED on cache.load after eviction (FYS bug A)",
        );
        assert_eq!(dem.width, 320);
        assert_eq!(dem.height, 240);
        assert!(!dem.samples.is_empty());
    }

    /// r46.2 (2026-06-02): text-over-video freeze fix.
    /// `evict_other_video_state` now accepts a slice of keep ids
    /// (was a single Uuid). For text-over-video slides, the caller
    /// passes `[text_slide_id, bg_video_id]` so the bg video's
    /// V4L2 state survives the eviction → no re-prime → no CMA
    /// pressure spike. This test pins the multi-keep semantics.
    #[test]
    fn evict_other_video_state_preserves_multiple_keep_ids() {
        let mut cache = SlideCache::new();
        let text_id = uuid(11);
        let bg_id = uuid(12);
        let stale_id = uuid(13);
        // Seed three demuxers (mock dummy entries). We don't need
        // real Mp4Demuxer state for the eviction-set test — just
        // observe which keys retain() drops.
        // Dummy demuxers — all fields are pub so we can construct
        // a minimal struct without parsing an MP4 file.
        let mk_dummy = || crate::mp4_demux::Mp4Demuxer {
            sps: vec![],
            pps: vec![],
            samples: vec![],
            width: 0,
            height: 0,
        };
        cache.video_demuxers.insert(text_id, mk_dummy());
        cache.video_demuxers.insert(bg_id, mk_dummy());
        cache.video_demuxers.insert(stale_id, mk_dummy());
        // Multi-keep: text + bg both survive, stale is dropped.
        cache.evict_other_video_state(&[text_id, bg_id]);
        assert!(
            cache.video_demuxers.contains_key(&text_id),
            "text slide demuxer preserved by multi-keep"
        );
        assert!(
            cache.video_demuxers.contains_key(&bg_id),
            "bg video demuxer preserved by multi-keep (r46.2 freeze fix)"
        );
        assert!(
            !cache.video_demuxers.contains_key(&stale_id),
            "stale demuxer dropped"
        );
        // Empty-keep slice drops everything (defensive sanity).
        cache.evict_other_video_state(&[]);
        assert!(cache.video_demuxers.is_empty(), "empty keep slice drops all");
    }

    /// Finding H1 (2026-05-21): the FYS bug A re-prime predicate
    /// must ALSO fire when the demuxer is present but the V4L2
    /// decoder entry is gone — `prime_video_decoder` is best-effort,
    /// so an EBUSY from the single-instance vc4 M2M codec leaves a
    /// demuxer-present / decoder-absent state. The pre-fix predicate
    /// checked only the demuxer, so the next `cache.load`
    /// short-circuited and `paint` then hard-errored on the missing
    /// decoder — the Bug A freeze recurred.
    ///
    /// This exercises the pure `video_reprime_needed` function so it
    /// runs on every host (the Linux-only `video_decoders` map is
    /// already collapsed to the `decoder_present` bool). It FAILS
    /// against the pre-fix predicate: pre-fix had no decoder clause,
    /// so demuxer-present + decoder-absent yielded `false` (no
    /// re-prime); the fix's `!decoder_present` clause yields `true`.
    #[test]
    fn video_reprime_needed_fires_when_decoder_missing() {
        // The H1 bug state: a video, not skip-marked, demuxer
        // present, decoder absent. Pre-fix => false; fixed => true.
        assert!(
            SlideCache::video_reprime_needed(
                /* is_video */ true,
                /* is_skip_marked */ false,
                /* demuxer_present */ true,
                /* decoder_present */ false,
            ),
            "demuxer-present + decoder-absent must need a re-prime (finding H1)",
        );

        // The original Bug A state still re-primes: demuxer gone.
        assert!(
            SlideCache::video_reprime_needed(true, false, false, false),
            "demuxer-absent must need a re-prime (original FYS bug A)",
        );
        assert!(
            SlideCache::video_reprime_needed(true, false, false, true),
            "demuxer-absent needs a re-prime even if a decoder lingers",
        );

        // Fully primed: both artifacts live => short-circuit, no
        // re-prime. (Off-Linux `has_video_decoder` returns true, so
        // `decoder_present` is effectively always true there — this
        // is the steady-state playlist-loop path on every host.)
        assert!(
            !SlideCache::video_reprime_needed(true, false, true, true),
            "demuxer + decoder both present must NOT re-prime",
        );

        // Skip-marked videos must never retry-spam a known-bad
        // asset, regardless of demuxer/decoder absence.
        assert!(
            !SlideCache::video_reprime_needed(true, true, false, false),
            "skip-marked video must not re-prime",
        );
        assert!(
            !SlideCache::video_reprime_needed(true, true, true, false),
            "skip-marked video must not re-prime on a missing decoder",
        );

        // Non-video slides never need a video re-prime.
        assert!(
            !SlideCache::video_reprime_needed(false, false, false, false),
            "non-video slide must not trigger a video re-prime",
        );
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
        assert!(cache.items.get(&id).is_some(), "Video item must be in items");
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
    /// UnsupportedSlide wire rail (Python raises
    /// `RustRendererUnsupportedSlideError`; playback loop log +
    /// skip post-DELETE-PIL) stays usable.
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
        assert!(cache.items.get(&id).is_some(), "Video item still in items");
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
        crate::profile::summarize_samples(samples).p99_ns
    }

    #[test]
    fn paint_metrics_p99_reflects_spike_tier() {
        // 900-sample window: 890 fast paints (1000us = 1ms) + 10 slow
        // paints (50000us = 50ms). Spike count (10) strictly exceeds
        // the top-1% boundary (9) so under nearest-rank, 0-indexed
        // percentile math the p99 lands ON A SPIKE -- not on the last
        // fast paint at the borderline.
        //
        // (Pre-2026-05-25 history: this test was 891+9 and read p99
        // = 50000 because the buggy `s[(n*pct)/100]` formula was
        // 1-position inflated -- it returned s[891] = first spike.
        // Under the correct nearest-rank `s[ceil(pct/100*n)-1]` for
        // n=900, p99 idx = 890; with the prior 891+9 split that would
        // be the LAST fast paint = 1000us. Adding one more spike
        // pushes the boundary into the spike tier where the test
        // intent lives.)
        let mut m = IpcPaintMetrics::new();
        for _ in 0..890 {
            m.record(IpcPaintKind::Slide, 1000);
        }
        for _ in 0..10 {
            m.record(IpcPaintKind::Slide, 50_000);
        }
        let p99 = p99_of(&m.paint_us_samples);
        assert_eq!(
            p99, 50_000,
            "p99 of 890x1ms + 10x50ms must report the spike tier, not the base; \
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
        // sensible p99 without panicking on the percentile index.
        let mut m = IpcPaintMetrics::new();
        for v in [10_u64, 20, 30, 40, 50] {
            m.record(IpcPaintKind::Slide, v);
        }
        // n=5, nearest-rank p99 idx = ceil(99/100 * 5) - 1
        //                            = ceil(4.95) - 1 = 5 - 1 = 4
        // -> sorted[4] = 50us. Same answer as the pre-2026-05-25
        // buggy formula gave for this borderline case; the bug
        // surfaced at larger n where the off-by-one had room to
        // grow.
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

    // `[perf]` r2 (2026-05-26) — PerfStatsJson wire-shape tests. The
    // backend's /api/playback/perf/stats route and the UI's
    // perf-overlay both consume this shape; renaming a field here is
    // a breaking change for both consumers.

    #[test]
    fn perf_stats_json_serializes_all_documented_keys() {
        // Wire-contract test: every key the backend + UI rely on must
        // be present in the serialized output. Use a recognizable
        // sentinel value per field so a rename or accidental field
        // removal shows up as a missing-key assert.
        let p = super::PerfStatsJson {
            window_s: 30,
            frames: 900,
            transitions: 50,
            fps_avg: 29.8,
            paint_us_avg: 5000,
            paint_us_max: 33000,
            paint_us_p99: 28000,
            session_frames: 12000,
            session_transitions: 600,
            frames_observed_total: 18000,
            frames_over_budget_total: 234,
            timestamp_unix_s: 1748275200,
        };
        let json = serde_json::to_string(&p).expect("serialize must succeed");
        // Each key must appear verbatim. The wire contract is the
        // backend reads these field names directly into a Pydantic
        // model + the UI reads them by key from the JSON response.
        for key in [
            "\"window_s\":30",
            "\"frames\":900",
            "\"transitions\":50",
            "\"fps_avg\":29.8",
            "\"paint_us_avg\":5000",
            "\"paint_us_max\":33000",
            "\"paint_us_p99\":28000",
            "\"session_frames\":12000",
            "\"session_transitions\":600",
            "\"frames_observed_total\":18000",
            "\"frames_over_budget_total\":234",
            "\"timestamp_unix_s\":1748275200",
        ] {
            assert!(
                json.contains(key),
                "expected key fragment {} missing in JSON: {}",
                key,
                json,
            );
        }
    }

    #[test]
    fn perf_stats_json_fps_avg_finite_for_zero_window() {
        // Guards against serde_json's NaN/Infinity panic. The
        // production maybe_emit_summary already pre-clamps fps_avg to
        // 0.0 when elapsed is 0, but this test pins that a 0.0
        // serializes cleanly (not "NaN" or "Infinity").
        let p = super::PerfStatsJson {
            window_s: 0,
            frames: 0,
            transitions: 0,
            fps_avg: 0.0,
            paint_us_avg: 0,
            paint_us_max: 0,
            paint_us_p99: 0,
            session_frames: 0,
            session_transitions: 0,
            frames_observed_total: 0,
            frames_over_budget_total: 0,
            timestamp_unix_s: 0,
        };
        let json = serde_json::to_string(&p).expect("zero-window serialize must succeed");
        assert!(json.contains("\"fps_avg\":0.0"), "fps_avg should be 0.0: {}", json);
        // Defense in depth: confirm no Infinity/NaN sentinels leak in.
        assert!(!json.contains("Infinity"), "no Infinity tokens: {}", json);
        assert!(!json.contains("NaN"), "no NaN tokens: {}", json);
    }

    #[test]
    fn perf_stats_json_atomic_write_round_trips() {
        // End-to-end test for the .tmp+rename path. Uses a temp dir
        // (tempfile not in deps; use a deterministic per-process path
        // under std::env::temp_dir).
        let mut path = std::env::temp_dir();
        path.push(format!(
            "om-perf-r2-roundtrip-{}-{}.json",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
        ));
        // Best-effort cleanup at end.
        let _guard = scopeguard_like::cleanup_at_drop(path.clone());

        let payload = r#"{"frames_observed_total":42}"#;
        super::write_perf_stats_json_atomic(&path, payload)
            .expect("atomic write must succeed on temp_dir");
        let read = std::fs::read_to_string(&path).expect("read back must succeed");
        assert_eq!(read, payload);

        // .tmp must NOT linger after the rename.
        let mut tmp = path.as_os_str().to_owned();
        tmp.push(".tmp");
        let tmp_path = std::path::PathBuf::from(tmp);
        assert!(
            !tmp_path.exists(),
            ".tmp must be renamed away, not lingering: {:?}",
            tmp_path,
        );
    }

    // Tiny RAII cleanup so the round-trip test doesn't leak temp
    // files across test runs. Inlined here rather than pulling in
    // the `scopeguard` crate as a dev-dep for a single use.
    mod scopeguard_like {
        pub struct Guard {
            path: std::path::PathBuf,
        }
        impl Drop for Guard {
            fn drop(&mut self) {
                let _ = std::fs::remove_file(&self.path);
            }
        }
        pub fn cleanup_at_drop(path: std::path::PathBuf) -> Guard {
            Guard { path }
        }
    }

    // ============================================================
    // r97 (2026-06-09): conditional deferred-preload predicate.
    //
    // The 4-way truth table for `should_defer_preload_for_codec_
    // contention(active_decoder_count, bg_is_video)`:
    //
    // | active_decoder_count | bg_is_video | should_defer |
    // |----------------------|-------------|--------------|
    // | 0                    | false       | false        |
    // | 0                    | true        | false        |
    // | >= 1                 | false       | false        |
    // | >= 1                 | true        | true         |
    //
    // See docs/hardware-ceilings.md for the empirical
    // characterisation that drove the rule.
    // ============================================================

    #[test]
    fn should_defer_preload_table_driven_4_combinations() {
        // The canonical 4 combinations.
        assert!(
            !super::should_defer_preload_for_codec_contention(0, false),
            "(no active decoder, solid bg) must NOT defer",
        );
        assert!(
            !super::should_defer_preload_for_codec_contention(0, true),
            "(no active decoder, video bg) must NOT defer — first video slide entering, \
             or arriving from a solid-bg slide; no contention to worry about",
        );
        assert!(
            !super::should_defer_preload_for_codec_contention(1, false),
            "(active decoder, solid bg) must NOT defer — preload won't open a decoder anyway",
        );
        assert!(
            super::should_defer_preload_for_codec_contention(1, true),
            "(active decoder, video bg) must defer — this is the dual-1080p contention case",
        );
    }

    #[test]
    fn should_defer_preload_scales_with_decoder_count() {
        // The contention surface grows with concurrent decoders;
        // the predicate stays "true" for any non-zero count.
        for n in 1usize..=4 {
            assert!(
                super::should_defer_preload_for_codec_contention(n, true),
                "{n} active decoders + video bg must defer",
            );
            assert!(
                !super::should_defer_preload_for_codec_contention(n, false),
                "{n} active decoders + solid bg must NOT defer",
            );
        }
    }

    #[test]
    fn defer_call_site_is_present_in_preload_arm() {
        // Source-grep regression-lock: the `PreloadSlide` IPC arm
        // MUST invoke `should_defer_preload_for_codec_contention`.
        // A future refactor that drops the call (or renames the
        // predicate without updating the call site) would silently
        // re-introduce the codec-contention failure mode.
        let src = include_str!("ipc_main.rs");
        // The call lives inside a `#[cfg(target_os = "linux")]`
        // block in the `IpcRequest::PreloadSlide(p) => { ... }`
        // arm; this assertion is layout-agnostic.
        assert!(
            src.contains("should_defer_preload_for_codec_contention("),
            "PreloadSlide arm must call should_defer_preload_for_codec_contention(...)",
        );
        // Pair-check: the `preload_deferred_for_codec_contention`
        // probe must be emitted, so the journal carries a clear
        // signal when the defer fires.
        assert!(
            src.contains("preload_deferred_for_codec_contention"),
            "PreloadSlide arm must emit `preload_deferred_for_codec_contention` probe",
        );
        // And the `was_deferred=true` branch must mirror the
        // canonical preload_handoff shape so log-scanners see
        // every preload as a handoff event (deferred or not).
        assert!(
            src.contains("was_deferred=true"),
            "PreloadSlide deferred branch must emit a preload_handoff with was_deferred=true",
        );
    }

    // ============================================================
    // r98 (2026-06-09): OPENMARQUEE_PRELOAD_MODE env-var parsing.
    //
    // Three modes (case-insensitive):
    //   - "defer" / unset / empty            -> PreloadMode::Defer   (r97 behavior)
    //   - "lead" / "Lead" / "LEAD"           -> PreloadMode::Lead
    //   - "max"  / "Max"  / "MAX"            -> PreloadMode::Max
    //   - anything else                      -> PreloadMode::Defer + warn
    // ============================================================

    #[test]
    fn parse_preload_mode_default_when_unset() {
        // Default is r97 behavior (defer). Unset env = None => Defer.
        assert_eq!(super::parse_preload_mode(None), super::PreloadMode::Defer);
    }

    #[test]
    fn parse_preload_mode_recognises_defer() {
        for raw in ["defer", "Defer", "DEFER", "DeFeR"] {
            assert_eq!(
                super::parse_preload_mode(Some(raw)),
                super::PreloadMode::Defer,
                "raw={raw:?} should parse as Defer",
            );
        }
    }

    #[test]
    fn parse_preload_mode_recognises_lead() {
        for raw in ["lead", "Lead", "LEAD", "LeAd"] {
            assert_eq!(
                super::parse_preload_mode(Some(raw)),
                super::PreloadMode::Lead,
                "raw={raw:?} should parse as Lead",
            );
        }
    }

    #[test]
    fn parse_preload_mode_recognises_max() {
        for raw in ["max", "Max", "MAX", "MaX"] {
            assert_eq!(
                super::parse_preload_mode(Some(raw)),
                super::PreloadMode::Max,
                "raw={raw:?} should parse as Max",
            );
        }
    }

    #[test]
    fn parse_preload_mode_garbage_falls_back_to_default() {
        // Unrecognised inputs should warn but never crash; safer
        // default is r97 behavior (Defer). Includes empty-string +
        // whitespace-only + true/false/0/1 (the old r98-draft
        // booleans shouldn't accidentally parse as a mode) + typos.
        for raw in [
            "", "   ", "true", "false", "1", "0", "deferred", "ahead",
            "maximum", "leed", "null",
        ] {
            assert_eq!(
                super::parse_preload_mode(Some(raw)),
                super::PreloadMode::Defer,
                "raw={raw:?} should fall back to Defer",
            );
        }
    }

    #[test]
    fn parse_preload_mode_handles_whitespace_around_value() {
        // Defensive: shell drop-in env files sometimes leave a
        // trailing newline or space. `trim()` should normalise.
        assert_eq!(super::parse_preload_mode(Some("  defer\n")), super::PreloadMode::Defer);
        assert_eq!(super::parse_preload_mode(Some(" lead ")), super::PreloadMode::Lead);
        assert_eq!(super::parse_preload_mode(Some("\tmax\t")), super::PreloadMode::Max);
    }

    #[test]
    fn preload_mode_call_site_is_present_in_preload_arm() {
        // Source-grep regression-lock: the `PreloadSlide` IPC arm
        // MUST gate the defer call behind
        // `preload_mode() == PreloadMode::Defer`. A refactor that
        // drops the gate would mean QA can't bypass the defer for
        // the 1080p experiment via OPENMARQUEE_PRELOAD_MODE.
        let src = include_str!("ipc_main.rs");
        assert!(
            src.contains("preload_mode() == PreloadMode::Defer"),
            "PreloadSlide arm must gate defer behind \
             `preload_mode() == PreloadMode::Defer` to honor OPENMARQUEE_PRELOAD_MODE",
        );
        // The mode check must come BEFORE
        // should_defer_preload_for_codec_contention so the && short-
        // circuit bypasses the contention predicate entirely when
        // mode != Defer.
        let mode_idx = src.find("preload_mode() == PreloadMode::Defer");
        let defer_idx = src.rfind("should_defer_preload_for_codec_contention(\n");
        match (mode_idx, defer_idx) {
            (Some(k), Some(d)) => assert!(
                k < d,
                "mode check must appear BEFORE the defer predicate so the \
                 && short-circuit bypasses contention check in lead/max modes \
                 (k={k} d={d})",
            ),
            _ => panic!(
                "expected both preload_mode() == PreloadMode::Defer and \
                 should_defer_preload_for_codec_contention(\\n in ipc_main.rs source",
            ),
        }
    }

    #[test]
    fn preload_handoff_normal_path_carries_was_deferred_false() {
        // Source-grep regression-lock for video_decode.rs: all five
        // `preload_handoff` emit sites must carry
        // `was_deferred=false` so QA can distinguish:
        //   - frames_drained=0 + was_deferred=true (graceful r97
        //     defer)
        //   - frames_drained=0 + was_deferred=false (codec actually
        //     stalled — the failure mode that started this fix)
        let src = include_str!("video_decode.rs");
        // The format string fragment we expect at every site.
        let needle = "[perf] preload_handoff slide_id={} frames_drained={} \
                      prime_only_us={} drain_us={} budget_ms={} was_deferred=false";
        // 5 emit sites in the prime_video_decoder_for_preload code
        // path (one per branch: pre-r85 fast path, r85 EOS-flush
        // branch + its inner fallbacks, etc.).
        let count = src.matches(needle).count();
        assert_eq!(
            count, 5,
            "expected 5 preload_handoff emit sites carrying was_deferred=false in \
             video_decode.rs; found {count}",
        );
    }
}
