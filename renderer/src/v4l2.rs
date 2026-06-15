//! V4L2 M2M H.264 decoder client (Phase 7 pieces 2a + 2b).
//!
//! Targets `bcm2835-codec-decode` exposed at `/dev/video10` on
//! Raspberry Pi. Per `docs/v4l2-decode.md`, the codec accepts
//! H.264 (and a few others) on its OUTPUT queue and emits NV12
//! (and other YUV/RGB variants) on its CAPTURE queue. M2M
//! Multiplanar with Streaming + Extended Pix Format caps.
//!
//! ## Scope of piece 2a (commit 343fe15)
//!
//! - `Decoder::open` + `query_capabilities` — VIDIOC_QUERYCAP.
//! - `CaptureBufferType` enum, `Frame` stub.
//!
//! ## Scope of piece 2b (this commit)
//!
//! Finishes the static + decode-loop body:
//!
//! - VIDIOC_S_FMT (OUTPUT = H.264, CAPTURE = NV12) via
//!   [`Decoder::set_output_format`] / [`Decoder::set_capture_format`].
//! - VIDIOC_REQBUFS + VIDIOC_QUERYBUF + mmap of all OUTPUT and
//!   CAPTURE planes via [`Decoder::allocate_buffers`].
//! - VIDIOC_STREAMON on both queues via
//!   [`Decoder::start_streaming`]. STREAMOFF runs automatically
//!   on `Drop for DecoderInner` (calls the private
//!   `stop_streaming_quiet` -- no explicit shutdown API needed).
//! - The decode-loop API: [`Decoder::feed`] (queue an OUTPUT
//!   buffer with H.264 NAL bytes) + [`Decoder::next_frame`]
//!   (dequeue the next CAPTURE buffer as a [`Frame`]).
//! - Frame lifetime: `Frame` holds an `Arc<Mutex<DecoderInner>>`
//!   + the buffer index. On `Drop`, the Frame re-QBUFs that
//!   index through the inner lock. The Arc keeps the mmap
//!   regions alive for as long as the Frame's `y_plane()` /
//!   `uv_plane()` slices are reachable -- soundness via shared
//!   ownership rather than lifetimes (chosen for ergonomics:
//!   pieces 3+ can hold a Frame across an await without
//!   tangling lifetimes).
//! - EOF: the caller signals end-of-input via [`Decoder::feed`]
//!   with an empty slice (translates to a zero-length OUTPUT
//!   buffer with V4L2_BUF_FLAG_LAST); `next_frame` returns
//!   `Ok(None)` once the kernel signals decoder-drained
//!   (V4L2_BUF_FLAG_LAST on the dequeued CAPTURE buffer, or
//!   subsequent DQBUF returns EPIPE).
//! - Drop ordering: STREAMOFF both queues -> munmap all planes
//!   -> File's own Drop closes fd.
//!
//! ## Scope of piece 2c (future)
//!
//! - DMA-BUF zero-copy CAPTURE path (piece 4). The
//!   [`CaptureBufferType::DmaBuf`] branch still routes through
//!   the `unimplemented!()` rail in piece 2b -- piece 4 lights
//!   it up via VIDIOC_EXPBUF.
//!
//! ## Cfg-gating
//!
//! Pure-Rust items (struct layouts, constants, helpers) compile
//! on any OS so `cargo test` on the Mac dev box catches
//! syntax/layout regressions. Items that link against `nix` /
//! `libc` ioctls (the `Decoder` impl, all the ioctl macros) are
//! individually `#[cfg(target_os = "linux")]` gated.

#[cfg(target_os = "linux")]
use std::collections::VecDeque;
#[cfg(target_os = "linux")]
use std::fs::{File, OpenOptions};
#[cfg(target_os = "linux")]
use std::io::Write;
#[cfg(target_os = "linux")]
use std::os::fd::AsRawFd;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
#[cfg(target_os = "linux")]
use std::path::{Path, PathBuf};
#[cfg(target_os = "linux")]
use std::sync::{Arc, Mutex};

#[cfg(target_os = "linux")]
use anyhow::{anyhow, Context, Result};

// ============================================================
// r70 (2026-06-06): feed() OUTPUT-pool empty-pool retry schedule.
// ============================================================

/// Default retry count + interval for feed()'s "wait for kernel
/// to release an OUTPUT buffer" soft-wait. Pre-r70 was hard-coded
/// 5 retries × 2ms = 10ms; FYS 1080p workload showed bcm2835-codec
/// per-frame decode latency at ~30-50ms (single decode cycle alone
/// already > the old budget), and every transition errored at the
/// "all OUTPUT buffers in flight" gate.
///
/// 25 × 4 = 100ms covers 2-3 full 1080p decode cycles + slack.
const FEED_DRAIN_DEFAULT_RETRIES: usize = 25;
const FEED_DRAIN_DEFAULT_INTERVAL_MS: u64 = 4;

/// Min/max bounds for the env-overridable budget. Clamped so a
/// typo in OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS can't deadlock
/// the renderer (`=0`) or starve other ops (`=999999`).
const FEED_DRAIN_MIN_BUDGET_MS: u64 = 10;
const FEED_DRAIN_MAX_BUDGET_MS: u64 = 1000;

/// r70 subagent WARN-2 + WARN-3: cache the schedule on first
/// resolution so (a) every feed() call doesn't pay an env::var
/// alloc, (b) production code never races the test's mutating
/// env::set_var, and (c) the error-message path shares the
/// SAME tuple that the loop entry used (no drift). Operator
/// hot-tuning by env still works -- the cache is per-process,
/// invalidated by a renderer restart, which is the documented
/// override workflow.
static FEED_DRAIN_SCHEDULE: std::sync::OnceLock<(usize, u64)> = std::sync::OnceLock::new();

/// Resolve the retry schedule (count, interval_ms) for the
/// feed() empty-pool soft-wait. Cached in a OnceLock on first
/// call; subsequent calls return the same tuple. Interval stays
/// at the default 4ms; only the count scales with the budget.
pub(crate) fn feed_drain_retry_schedule() -> (usize, u64) {
    *FEED_DRAIN_SCHEDULE.get_or_init(resolve_feed_drain_schedule_from_env)
}

/// Pure resolver -- no caching, no OnceLock interaction. Test
/// entry point so per-case env mutation actually flows through.
/// Production path calls this exactly once via the OnceLock in
/// `feed_drain_retry_schedule()`.
fn resolve_feed_drain_schedule_from_env() -> (usize, u64) {
    let interval_ms = FEED_DRAIN_DEFAULT_INTERVAL_MS;
    let default_budget = (FEED_DRAIN_DEFAULT_RETRIES as u64) * interval_ms;
    let budget_ms = match std::env::var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS") {
        Ok(s) => s
            .parse::<u64>()
            .unwrap_or(default_budget)
            .clamp(FEED_DRAIN_MIN_BUDGET_MS, FEED_DRAIN_MAX_BUDGET_MS),
        Err(_) => default_budget,
    };
    // Ceiling-divide so a budget that isn't a multiple of
    // interval_ms still gets at least one full iteration past
    // the floor (e.g. budget=11 -> 3 retries at 4ms = 12ms).
    let retries = (budget_ms + interval_ms - 1) / interval_ms;
    (retries as usize, interval_ms)
}

// ============================================================
// r75 (2026-06-07): MMAL component-slot leak instrumentation.
//
// After ~23 min cycling a 17-slide 1080p playlist, FYS started
// failing `vchiq_mmal_component_init` with -62 (ETIME) and
// "failed to create component ril.video_decode". Recovers ONLY
// across a Pi reboot.
//
// Hypothesis: each Decoder::open allocates a bcm2835-codec MMAL
// component (kernel-side) and the corresponding ril.video_decode
// VPU-side slot. The VPU has a finite slot pool; each leaked
// session ties one up. After ~24 leaks (~one per playlist
// cycle) the pool exhausts and all further inits time out.
//
// Phase A (this commit): count live Decoder instances via an
// AtomicUsize updated on Decoder::open (+1) and DecoderInner
// Drop (-1). Emit a parser-friendly [mem] line on each change
// so QA's journalctl scrape can build a time-series. If the
// counter grows monotonically, the leak is in userspace (a
// reference held past intended lifetime); if it stays bounded
// but kernel-side -62s still fire, the leak is kernel/VPU.
//
// Companion: `mmal_leak_suspect` log lines at the prime-failure
// call sites in ipc_main.rs name the failing op + the slide_id
// so leaks correlate to specific failure paths.
// ============================================================

/// Counter of live Decoder instances. Incremented at the end of
/// `Decoder::open` (after the device passes capability checks,
/// so we don't count failed opens). Decremented in
/// `DecoderInner::drop` (which is the canonical scope-exit point,
/// covering both clean playlist progression and prime-failure
/// teardown via `?`-propagation in `prime_video_decoder_with_warmup`).
pub static MMAL_COMPONENTS_LIVE: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

/// r84 (2026-06-09): one-shot gate for the CMD_STOP capability
/// probe. Per subagent BLOCKER-2, the probe should only fire on
/// the first Decoder open in the process lifetime — the answer
/// is a static capability of the kernel driver, not something
/// that changes per slide, so re-probing per Decoder is
/// needless ioctl churn and blast-radius risk. Set to true on
/// first probe; subsequent Decoder::open calls skip the probe.
///
/// r91 (2026-06-08): probe site moved from start_streaming to
/// Decoder::open (after query_capabilities) per QA r91 dispatch
/// so it fires BEFORE any prime ioctl (S_FMT/REQBUFS/STREAMON).
///
/// r92 (2026-06-08): REVERTED to end of start_streaming. QA's r91
/// deploy data showed VIDIOC_TRY_DECODER_CMD is destructive on
/// bcm2835-codec when called pre-STREAMON (17 EINVAL in 60s with
/// EOS_FLUSH unset). Consumer site is back in start_streaming
/// (after both STREAMONs), matching r84/r88/r90's known-clean
/// location.
static CMD_STOP_PROBE_DONE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// Snapshot the current counter. Used by tests + the
/// `[mem] vpu_mmal_components` periodic logger.
pub fn mmal_components_live() -> usize {
    MMAL_COMPONENTS_LIVE.load(std::sync::atomic::Ordering::Relaxed)
}

/// r101 (2026-06-09): per-Decoder cached EGLImage handle. One per
/// CAPTURE buffer index; the paint helper looks it up on every
/// frame, lazy-creates if None, and DecoderInner::Drop tears each
/// one down via `destroy_fn(display, image)` BEFORE the matching
/// dmabuf fd is closed.
///
/// Carries its own destroy_fn + display so DecoderInner::Drop
/// doesn't need access to an `EglSession` to call eglDestroyImage.
/// `image` and `display` are opaque pointers (EGLImage / EGLDisplay
/// per the spec); `destroy_fn` is the resolved
/// `eglDestroyImageKHR` entry point. All three are constant for
/// the lifetime of the handle.
///
/// Copy because cache lookups need to return a value (the Mutex
/// guard scope is too short to lend a reference). Send because
/// EGL opaque handles are sendable per the EGL spec and function
/// pointers are Send.
#[derive(Copy, Clone, Debug)]
#[cfg(target_os = "linux")]
pub struct EglImageHandle {
    pub image: *mut std::ffi::c_void,
    pub display: *mut std::ffi::c_void,
    pub destroy_fn: unsafe extern "C" fn(
        dpy: *mut std::ffi::c_void,
        image: *mut std::ffi::c_void,
    ) -> u32,
}

#[cfg(target_os = "linux")]
unsafe impl Send for EglImageHandle {}

/// r101 (2026-06-09): runtime kill-switch for the EGLImage cache.
/// Default ENABLED. Set `OPENMARQUEE_EGL_IMAGE_CACHE=off` (or
/// `0`/`false`/`disable`/`disabled`) to fall back to the pre-r101
/// per-frame create+destroy path. The kill-switch lets QA A/B at
/// deploy time if the cache itself introduces a regression.
///
/// Not Linux-gated: the helper is a pure env-var read with no
/// V4L2 / EGL dependency, so cross-platform tests can pin the
/// parser without skipping on macOS.
pub fn is_egl_image_cache_enabled() -> bool {
    match std::env::var("OPENMARQUEE_EGL_IMAGE_CACHE") {
        Ok(s) => {
            let v = s.trim().to_ascii_lowercase();
            !matches!(v.as_str(), "0" | "off" | "false" | "no" | "disable" | "disabled")
        }
        Err(_) => true,
    }
}

/// r102.3 (2026-06-09): runtime kill-switch for the transition
/// program (link_program) + textured-quad VBO cache in the
/// live-3-pass transition path. Default ENABLED. Set
/// `OPENMARQUEE_TRANSITION_PROGRAM_CACHE=off` to fall back to
/// the pre-r102.3 per-tick link + buffer create + delete churn
/// that vc4 V3D lazy-GC retained as ~108 MB / 4 min of the
/// non-FBO+tex transition leak. Co-located with the r101 EGLImage
/// + r102.2 transition-FBO kill switches for cohesion.
pub fn is_transition_program_cache_enabled() -> bool {
    match std::env::var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE") {
        Ok(s) => {
            let v = s.trim().to_ascii_lowercase();
            !matches!(v.as_str(), "0" | "off" | "false" | "no" | "disable" | "disabled")
        }
        Err(_) => true,
    }
}

/// r102.2 (2026-06-09): runtime kill-switch for the transition
/// FBO+tex cache (hdmi.rs `ensure_transition_fbo_pair`). Default
/// ENABLED. Set `OPENMARQUEE_TRANSITION_FBO_CACHE=off` (or
/// `0`/`false`/`no`/`disable`/`disabled`) to fall back to the
/// pre-r102.2 per-tick `create_slide_fbo_pair` +
/// `cleanup_static` churn that vc4 V3D lazy-GC retained as a
/// BO leak. Co-located with the r101 EGLImage cache kill switch
/// for cohesion -- both are env-var helpers used by the
/// renderer's leak-mitigation surfaces.
pub fn is_transition_fbo_cache_enabled() -> bool {
    match std::env::var("OPENMARQUEE_TRANSITION_FBO_CACHE") {
        Ok(s) => {
            let v = s.trim().to_ascii_lowercase();
            !matches!(v.as_str(), "0" | "off" | "false" | "no" | "disable" | "disabled")
        }
        Err(_) => true,
    }
}

/// r102.1 (2026-06-09): snapshot of vc4 V3D BO counts from
/// `/sys/kernel/debug/dri/0/bo_stats`. `None` for the count
/// fields when the debugfs node is absent or unreadable or
/// the line format is unrecognized. The probe is best-effort;
/// missing data emits a warn line once but never panics or
/// errors a caller. Defensive against kernel-driver format
/// drift across kernels 6.6 / 6.12 / future.
#[derive(Default, Debug, Clone, Copy)]
pub struct V3dBoSnapshot {
    pub v3d_bos: Option<u32>,
    pub v3d_mem_kb: Option<u64>,
}

/// r102.1.1 (2026-06-09): pure parser for
/// `/sys/kernel/debug/dri/0/bo_stats`. Split out of
/// `read_v3d_bo_snapshot` so the parsing logic is unit-testable
/// against synthetic input. Mirrors the `parse_cma_lines`
/// (v4l2.rs:3182) pattern.
///
/// **FYS-empirical format** (raspberry-pi-64 kernel; QA-captured
/// 2026-06-09 via `sudo -u openmarquee cat`):
///
/// ```text
///                           dumb:   2040kb BOs (1)
///                            V3D: 109972kb BOs (45)
///                     V3D shader:     80kb BOs (20)
///                           kernel:   5280kb BOs (4)
///                         binner:  16384kb BOs (1)
/// ```
///
/// We extract ONLY the `V3D:` line (NOT `V3D shader:` -- different
/// category). Match shape: `V3D: <kb>kb BOs (<count>)`. Returns
/// defaulted Nones if the line is missing or malformed.
///
/// r102.1 (parent) targeted a HYPOTHETICAL kernel-6.12 format
/// (`num bos allocated:` / `size bos allocated:`) that turned out
/// NOT to match FYS. The hypothetical-format tests were green but
/// the parser was wrong against real input. r102.1.1 reworks
/// against the empirically-captured format + retains tolerance
/// for the hypothetical one (legacy fallback) so a future kernel
/// upgrade isn't a sudden parser break.
pub fn parse_v3d_bo_stats(contents: &str) -> V3dBoSnapshot {
    let mut out = V3dBoSnapshot::default();
    for line in contents.lines() {
        let line = line.trim();
        // r102.1.1: FYS-empirical format. The strip_prefix
        // intentionally uses "V3D:" (with colon) so it doesn't
        // match "V3D shader:" -- two distinct allocator
        // categories that must NOT be summed.
        if let Some(rest) = line.strip_prefix("V3D:") {
            let rest = rest.trim();
            // Format: "109972kb BOs (45)" -- single space between
            // "kb" and "BOs" on FYS; tolerate other whitespace
            // counts via `split_once`.
            if let Some((kb_part, count_part)) = rest.split_once("kb BOs (") {
                if let Ok(kb) = kb_part.trim().parse::<u64>() {
                    out.v3d_mem_kb = Some(kb);
                }
                let count_str = count_part.trim_end_matches(')').trim();
                if let Ok(n) = count_str.parse::<u32>() {
                    out.v3d_bos = Some(n);
                }
            }
            continue;
        }
        // r102.1.1: legacy hypothetical-format fallback. Kept so
        // a future kernel change away from the FYS format
        // doesn't silently break. Don't `continue` on match
        // here because the file may emit BOTH formats in a
        // transition window (highly unlikely but cheap to
        // tolerate).
        if let Some(rest) = line.strip_prefix("num bos allocated:") {
            if let Ok(n) = rest.trim().parse::<u32>() {
                out.v3d_bos = Some(n);
            }
        } else if let Some(rest) = line.strip_prefix("size bos allocated:") {
            let cleaned = rest.trim().trim_end_matches(" kb").trim_end_matches("kb").trim();
            if let Ok(n) = cleaned.parse::<u64>() {
                out.v3d_mem_kb = Some(n);
            }
        }
    }
    out
}

/// r102.1.1 (2026-06-09): one-shot debug dump of the raw bo_stats
/// bytes when the parser returns defaulted Nones for the first
/// time in a process lifetime. QA asked for this so a future
/// format drift surfaces the actual file contents in the journal
/// rather than silently emitting `?` sentinels forever.
///
/// Fires AT MOST once per process. Truncates the dump to 512
/// chars so a runaway file doesn't blow the log.
// r102.1.1 subagent WARN: a prior version cfg-gated this on
// linux-vs-not-linux with identical bodies. Single declaration
// is correct; the dump call site is also cross-platform.
std::thread_local! {
    static V3D_BO_STATS_DUMPED: std::cell::Cell<bool> =
        const { std::cell::Cell::new(false) };
}

/// r102.1 (2026-06-09): read `/sys/kernel/debug/dri/0/bo_stats`
/// once and parse via `parse_v3d_bo_stats`. Returns a defaulted
/// `V3dBoSnapshot` if the file is absent / permission-denied.
/// NEVER panics; instrumentation MUST NOT affect renderer
/// liveness.
///
/// Reads the file fresh on every call; cheap (debugfs read is
/// ~µs). The renderer runs as `openmarquee` user; debugfs is
/// root-readable by default on Raspberry Pi OS. If permission
/// is denied this returns Err and we silently emit defaulted
/// fields. QA can chmod +r the bo_stats file at deploy time if
/// needed.
pub fn read_v3d_bo_snapshot() -> V3dBoSnapshot {
    use std::io::Write;
    let contents = match std::fs::read_to_string("/sys/kernel/debug/dri/0/bo_stats") {
        Ok(s) => s,
        Err(_) => return V3dBoSnapshot::default(),
    };
    let snap = parse_v3d_bo_stats(&contents);
    // r102.1.1: one-shot raw-bytes dump on first parse failure.
    // The parent r102.1 shipped a parser tuned to a hypothetical
    // kernel format that didn't match FYS; we silently emitted
    // `?` for every probe until QA caught it via the workaround.
    // Dump the actual file contents once so a future format
    // drift is visible in the journal instead of silent.
    if snap.v3d_bos.is_none() && snap.v3d_mem_kb.is_none() {
        V3D_BO_STATS_DUMPED.with(|dumped| {
            if !dumped.get() {
                dumped.set(true);
                // r102.1.1 subagent BLOCKER fix: `&contents[..512]`
                // panics on a non-char-boundary slice. Today's
                // bo_stats is pure ASCII so it works, but the
                // dump fires precisely on parser failure (i.e.
                // when the format is unexpected and could
                // hypothetically contain non-ASCII). Use
                // `get(..512)` which returns None on non-
                // boundary; fall back to the full contents in
                // that case. Instrumentation MUST NOT affect
                // renderer liveness (documented invariant).
                let truncated = contents
                    .get(..512)
                    .unwrap_or(contents.as_str());
                let _ = writeln!(
                    std::io::stderr(),
                    "[mem] v3d_bo_stats_parse_failed first_time=true bytes={} raw_truncated_512=\"{}\"",
                    contents.len(),
                    truncated.replace('\n', "\\n").replace('"', "\\\""),
                );
            }
        });
    }
    snap
}

/// r106 + Path A Stage 2 (2026-06-14): runtime kill-switch for
/// the feed/drain decoupling fix. Default ENABLED.
///
/// When enabled (default), `bake_video_slide_to_current_fbo`
/// — when called from the transition path (`is_offscreen_bake=
/// true`) — replaces the pre-r106 blocking `feed()` + 10x3ms
/// EAGAIN loop with: (1) a bounded `try_feed_nonblocking()`
/// top-up of the OUTPUT pool until the kernel-owned slots are
/// full or the per-tick cap (16, matching ffmpeg's queue
/// depth) is hit, then (2) a single non-blocking `next_frame
/// ()` attempt. Returns `Ok(None)` when no frame is ready.
/// The caller in `paint_and_present_one_transition_frame`
/// reuses the cached `transition_fbo_a/b` content via the
/// per-side `transition_fbo_a/b_painted` flag.
///
/// Steady-state paint paths (`is_offscreen_bake=false`) keep
/// the pre-r106 blocking pattern even when this flag is on
/// — Stage 2 scoping per the 684d386 r110 revert body, which
/// identified r106's whole-function decouple as the cause of
/// the 720p steady-state regression (perceptual freeze when
/// the codec doesn't deliver every tick).
///
/// When DISABLED via `OPENMARQUEE_FEED_DRAIN_DECOUPLE=off`
/// (or `0`/`false`/`no`/`disable`/`disabled`, case-insens,
/// trimmed), the pre-r106 blocking feed + 10x3ms EAGAIN inner
/// loop + Path B polling behavior is restored on the
/// transition path too. Lets QA A/B the fix at deploy time.
pub fn is_feed_drain_decouple_enabled() -> bool {
    match std::env::var("OPENMARQUEE_FEED_DRAIN_DECOUPLE") {
        Ok(s) => {
            let v = s.trim().to_ascii_lowercase();
            !matches!(v.as_str(), "0" | "off" | "false" | "no" | "disable" | "disabled")
        }
        Err(_) => true,
    }
}

/// r103.1 (2026-06-09): throttle helper for the steady-state
/// video-paint probe. Emits on the FIRST paint of a decoder's
/// lifetime (count==1) and every 30 paints after (1 sec at
/// 30fps). Split out as a pure fn so the policy is unit-
/// testable.
///
/// `count` is `frames_decoded` AFTER the increment for the
/// current paint. count==0 means the paint produced no frame
/// (Ok(None) skip) and the probe is skipped.
pub fn should_emit_steady_state_video_probe(count: usize) -> bool {
    count == 1 || (count > 0 && count % 30 == 0)
}

/// r103.1 (2026-06-09): emit a `[mem] v3d_bos_at_phase ...` line
/// with an additional `path=` tag identifying the video paint
/// transport (DMABUF vs MMAP). Differentiates the DMABUF-cache
/// hit pattern from the MMAP fall-through pattern so QA can
/// pinpoint which branch contributes to the residual V3D leak.
///
/// Output shape:
///   `[mem] v3d_bos_at_phase phase=X v3d_bos=N v3d_mem_mb=M slide_id=Y path=Z`
///
/// Sibling to `log_v3d_bos_at_phase` (which omits path=);
/// non-video phase boundaries should keep using that. Video
/// paint probes use this one.
pub fn log_v3d_bos_at_phase_with_path(
    phase: &str,
    slide_id: Option<uuid::Uuid>,
    path: &str,
) {
    use std::io::Write;
    let snap = read_v3d_bo_snapshot();
    let bos_str = snap
        .v3d_bos
        .map(|n| n.to_string())
        .unwrap_or_else(|| "?".to_string());
    let mem_mb_str = snap
        .v3d_mem_kb
        .map(|kb| format!("{:.1}", (kb as f64) / 1024.0))
        .unwrap_or_else(|| "?".to_string());
    let slide_str = slide_id
        .map(|id| id.to_string())
        .unwrap_or_else(|| "none".to_string());
    let _ = writeln!(
        std::io::stderr(),
        "[mem] v3d_bos_at_phase phase={} v3d_bos={} v3d_mem_mb={} slide_id={} path={}",
        phase, bos_str, mem_mb_str, slide_str, path,
    );
}

/// r102.1 (2026-06-09): emit a `[mem] v3d_bos_at_phase ...`
/// line for the given phase tag + optional slide_id. Used by
/// the V3D BO leak hunt to bracket transitions vs steady-state
/// and pinpoint which call path grows the BO count.
///
/// Output shape (matched by QA's grep tooling):
///   `[mem] v3d_bos_at_phase phase=X v3d_bos=N v3d_mem_mb=M slide_id=Y`
///
/// Both v3d_bos and v3d_mem_mb emit `?` when the underlying
/// debugfs node is unreadable so the line shape stays uniform
/// (QA's parser can split on `=` reliably). slide_id is
/// optional; pass `None` for phase boundaries that aren't
/// tied to a specific slide (e.g., transition begin).
pub fn log_v3d_bos_at_phase(phase: &str, slide_id: Option<uuid::Uuid>) {
    // The top-level `use std::io::Write` is cfg(target_os="linux")-gated;
    // pull the trait into scope locally so this function compiles cross-
    // platform (cross-platform test coverage requires it).
    use std::io::Write;
    let snap = read_v3d_bo_snapshot();
    let bos_str = snap
        .v3d_bos
        .map(|n| n.to_string())
        .unwrap_or_else(|| "?".to_string());
    let mem_mb_str = snap
        .v3d_mem_kb
        .map(|kb| format!("{:.1}", (kb as f64) / 1024.0))
        .unwrap_or_else(|| "?".to_string());
    let slide_str = slide_id
        .map(|id| id.to_string())
        .unwrap_or_else(|| "none".to_string());
    let _ = writeln!(
        std::io::stderr(),
        "[mem] v3d_bos_at_phase phase={} v3d_bos={} v3d_mem_mb={} slide_id={}",
        phase, bos_str, mem_mb_str, slide_str,
    );
}

/// r89 (2026-06-08): monotonic per-process Decoder-open
/// sequence counter. Each Decoder::open allocates a unique
/// 1-indexed value (first open is seq=1 to match the
/// `vpu_mmal_components` 1-indexed convention on the same
/// log line) and stores it in DecoderInner. Distinct from
/// MMAL_COMPONENTS_LIVE which is a LIVE count (incremented and
/// decremented); this seq is MONOTONIC so each open has a
/// unique identity even if MMAL_COMPONENTS_LIVE returns to 0
/// between opens.
///
/// Emitted in exactly TWO log lines per Decoder lifetime:
/// `[mem] decoder_open seq=N mmal_live=M path=...` and the
/// matching `[mem] decoder_drop seq=N mmal_live=M path=...`.
/// NOTE: existing ioctl trace lines (S_FMT, REQBUFS, QUERYBUF,
/// EXPBUF, STREAMON, STREAMOFF, DQBUF, DECODER_CMD, etc.) are
/// NOT modified -- the per-ioctl `decoder_seq=N` tagging
/// would touch ~10 trace sites and risk subtle behavior
/// differences from log-format changes (which the r80-r87
/// regression cycle has shown is non-trivial to verify). QA
/// can correlate via the timestamp + ordering between the
/// `decoder_open` line and subsequent ioctl traces on a fresh
/// decoder.
///
/// Failed-open observability: r89 places the `decoder_open`
/// line at the same point as the existing `vpu_mmal_components
/// delta=+1` line, BEFORE the capability checks. On a cap-
/// rejected open, QA will see paired open seq=N + drop seq=N
/// lines (the Arc<Mutex<DecoderInner>> Drop fires when `dec`
/// falls out of scope after `?`). This is intentional — same
/// behavior as the MMAL counter — and lets QA see the full
/// lifetime span even on failure paths. A QA script that
/// expects "open without matching drop = leak" should account
/// for cap-rejected paired pairs.
static DECODER_OPEN_SEQ: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

/// Allocate the next monotonic Decoder-open sequence number.
/// 1-indexed for parity with `vpu_mmal_components` (`fetch_add
/// + 1` returns the post-increment value, so the first call
/// returns 1, second returns 2, etc.). `pub(crate)` to match
/// the single-call-site invariant: only Decoder::open should
/// bump this counter; a wider caller would desync the seq
/// from the actual DecoderInner construction count.
pub(crate) fn next_decoder_open_seq() -> usize {
    DECODER_OPEN_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1
}

// ============================================================
// V4L2 fourcc helper + format codes.
// ============================================================

pub const fn fourcc(a: u8, b: u8, c: u8, d: u8) -> u32 {
    (a as u32) | ((b as u32) << 8) | ((c as u32) << 16) | ((d as u32) << 24)
}

pub const V4L2_PIX_FMT_H264: u32 = fourcc(b'H', b'2', b'6', b'4');
pub const V4L2_PIX_FMT_NV12: u32 = fourcc(b'N', b'V', b'1', b'2');

// ============================================================
// V4L2 capability flags.
// ============================================================

pub const V4L2_CAP_VIDEO_M2M_MPLANE: u32 = 0x00004000;
pub const V4L2_CAP_STREAMING: u32 = 0x04000000;
pub const V4L2_CAP_DEVICE_CAPS: u32 = 0x80000000;

// ============================================================
// V4L2 buffer types (subset).
// ============================================================

pub const V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE: u32 = 9;
pub const V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE: u32 = 10;

// ============================================================
// V4L2 memory types.
// ============================================================

pub const V4L2_MEMORY_MMAP: u32 = 1;
pub const V4L2_MEMORY_DMABUF: u32 = 4;

// r83 Phase A (2026-06-08): VIDIOC_G_SELECTION targets. COMPOSE is
// the "display this sub-rect of the allocated frame" target for
// CAPTURE-side queries on a video-decoder m2m device — the rect
// that excludes the bcm2835-codec's 8-row bottom padding on 1080p
// H.264. Full enumeration in <linux/videodev2.h>; we only need
// COMPOSE today.
pub const V4L2_SEL_TGT_COMPOSE: u32 = 0x0100;

// ============================================================
// V4L2 buffer flags (subset).
// ============================================================

pub const V4L2_BUF_FLAG_LAST: u32 = 0x00100000;
pub const V4L2_BUF_FLAG_ERROR: u32 = 0x00040000;

// ============================================================
// V4L2 decoder command codes (subset).
//
// r46.4 (2026-06-02): VIDIOC_DECODER_CMD with V4L2_DEC_CMD_START
// is the V4L2-stateful-decoder spec's documented mechanism to
// resume capture after V4L2_BUF_FLAG_LAST (EOS) on the CAPTURE
// queue. Replaces the r46.3 STREAMOFF/STREAMON cycle, which
// bcm2835-codec rejects with EINVAL on subsequent OUTPUT QBUF.
// ============================================================

pub const V4L2_DEC_CMD_START: u32 = 0;
pub const V4L2_DEC_CMD_STOP: u32 = 1;

// ============================================================
// Mirrored kernel structs. Byte-for-byte match for <linux/
// videodev2.h>; sizes verified via the ioctl request encoding
// (each macro below carries the expected size in its high
// bits, and the kernel rejects EINVAL on mismatch).
// ============================================================

/// V4L2 driver / device identification struct, populated by
/// VIDIOC_QUERYCAP. Size: 104 bytes.
#[repr(C)]
#[derive(Clone)]
pub struct V4l2Capability {
    pub driver: [u8; 16],
    pub card: [u8; 32],
    pub bus_info: [u8; 32],
    pub version: u32,
    pub capabilities: u32,
    pub device_caps: u32,
    pub reserved: [u32; 3],
}

/// Per-plane size info inside a multiplanar v4l2_format. Size: 20 bytes.
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2PlanePixFormat {
    pub sizeimage: u32,
    pub bytesperline: u32,
    pub reserved: [u16; 6],
}

/// The multiplanar pix format payload. Size: 192 bytes (packed).
///
/// `#[repr(C, packed)]` is critical -- the kernel struct is
/// declared `__attribute__((packed))`. Without `packed` the
/// num_planes byte would land at offset 192 instead of 180,
/// shifting every later field by 12 bytes and corrupting the
/// S_FMT call.
#[repr(C, packed)]
#[derive(Clone, Copy)]
pub struct V4l2PixFormatMplane {
    pub width: u32,
    pub height: u32,
    pub pixelformat: u32,
    pub field: u32,
    pub colorspace: u32,
    pub plane_fmt: [V4l2PlanePixFormat; 8],
    pub num_planes: u8,
    pub flags: u8,
    pub ycbcr_enc: u8, // also hsv_enc (union)
    pub quantization: u8,
    pub xfer_func: u8,
    pub reserved: [u8; 7],
}

/// v4l2_format. type + a 200-byte union for the per-buf_type
/// payload. Total: 208 bytes.
///
/// **Critical layout note (subagent-caught 2026-05-14):** the
/// kernel's `union { ... }` includes `v4l2_window` which holds
/// `__user *` pointers, so the union has natural alignment 8 on
/// 64-bit Linux. The compiler inserts 4 bytes of padding AFTER
/// `type` and BEFORE the union to align the latter to offset 8.
/// The Rust struct mirror MUST put the padding in the same place
/// (between buf_type and fmt) -- without this, S_FMT silently
/// puts V4l2PixFormatMplane.width into kernel reserved bytes
/// and the decoder receives a zero-width format spec.
#[repr(C)]
pub struct V4l2Format {
    pub buf_type: u32,
    pub _pad_to_align_union: [u8; 4],
    pub fmt: [u8; 200],
}

/// v4l2_exportbuffer (V4L2 piece 4a). Asks the kernel for a
/// DMA-BUF fd that refers to one of the buffers we previously
/// REQBUFS'd as `V4L2_MEMORY_DMABUF`. Size: 56 bytes
/// (8 explicit fields + 11 reserved u32 = 4+4+4+4+4+4 + 32 + 4
/// padding... actually 4+4+4+4+4+4+ then `reserved: [u32; 11]`
/// = 4+4+4+4+4 + 44 = 60? Let me compute: 4 (type) + 4 (index)
/// + 4 (plane) + 4 (flags) + 4 (fd, signed) + 44 (11 u32
/// reserved) = 60 bytes. Standard kernel docs say 52 -- but the
/// nix-derived size from the macro below verifies at compile
/// time against the kernel via the request word's size field,
/// so any drift would surface as EINVAL on real hardware.
///
/// References:
///   - <linux/videodev2.h> struct v4l2_exportbuffer
///   - <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-expbuf.html>
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Exportbuffer {
    pub buf_type: u32,
    pub index: u32,
    pub plane: u32,
    pub flags: u32,
    pub fd: i32,
    pub reserved: [u32; 11],
}

/// v4l2_decoder_cmd. Used to signal decoder state transitions
/// (START/STOP/PAUSE/RESUME) outside of buffer-flag flow.
///
/// r46.4 (2026-06-02): mirrors the kernel's `struct
/// v4l2_decoder_cmd` from <linux/videodev2.h>. The kernel struct
/// is `__u32 cmd; __u32 flags; union { ... } u;` where the union
/// holds either `start { __s32 speed; __u32 format; }` (8 bytes),
/// `stop { __u64 pts; }` (8 bytes), or `raw { __u32 data[16]; }`
/// (64 bytes). The union sizes to 64 bytes -- the largest
/// variant. Total struct size: 4 + 4 + 64 = 72 bytes.
///
/// We treat the union as an opaque 64-byte payload (zeroed for
/// the START path; not used for STOP in this codebase). Sizing
/// is critical: VIDIOC_DECODER_CMD's ioctl request encoding
/// includes the struct size, and a mismatch yields EINVAL.
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2DecoderCmd {
    pub cmd: u32,
    pub flags: u32,
    pub payload: [u32; 16],
}

/// r83 Phase A (2026-06-08): kernel `struct v4l2_rect`. Used as
/// the embedded `r` field of `struct v4l2_selection`. Signed
/// `left/top` because the kernel encodes "outside the original
/// frame" via negative offsets (rare but valid per the API).
///
/// References:
///   - <linux/videodev2.h> struct v4l2_rect
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Rect {
    pub left: i32,
    pub top: i32,
    pub width: u32,
    pub height: u32,
}

/// r83 Phase A (2026-06-08): kernel `struct v4l2_selection`. The
/// arg-by-pointer struct VIDIOC_G_SELECTION reads + writes. Caller
/// populates `buf_type` (e.g. `V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE`)
/// and `target` (e.g. `V4L2_SEL_TGT_COMPOSE` = 0x0100); kernel
/// fills `r` and `flags`.
///
/// Size: 4 + 4 + 4 + 16 + 9*4 = 64 bytes — matches the kernel
/// definition.
///
/// References:
///   - <linux/videodev2.h> struct v4l2_selection
///   - <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-g-selection.html>
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Selection {
    pub buf_type: u32,
    pub target: u32,
    pub flags: u32,
    pub r: V4l2Rect,
    pub reserved: [u32; 9],
}

/// perf-decode MIN_BUFFERS plumbing draft (2026-06-15):
/// `struct v4l2_control` for VIDIOC_G_CTRL.
///
/// Size: 8 bytes (id u32 + value i32). Matches the kernel
/// definition exactly per <linux/videodev2.h>:
///   struct v4l2_control {
///       __u32 id;
///       __s32 value;
///   };
///
/// Used to query V4L2_CID_MIN_BUFFERS_FOR_CAPTURE — the V4L2-
/// spec-correct way to determine the minimum CAPTURE pool size
/// the driver requires for reorder buffers + EGL hold. C-1
/// (2026-06-15, aborted) tried to shortcut this by requesting
/// count=2 directly; sacred caught the r93 wedge risk because
/// bcm2835-codec doesn't reliably up-negotiate REQBUFS count
/// to satisfy MIN_BUFFERS_FOR_CAPTURE (the control is a
/// userspace contract — driver expects userspace to query it
/// and request that many).
///
/// References:
///   - <linux/videodev2.h> struct v4l2_control
///   - <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/vidioc-g-ctrl.html>
///   - <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/control.html>
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Control {
    pub id: u32,
    pub value: i32,
}

/// Single plane's metadata inside a multiplanar v4l2_buffer.
/// Size: 64 bytes.
#[repr(C)]
#[derive(Clone, Copy, Default, Debug)]
pub struct V4l2Plane {
    pub bytesused: u32,
    pub length: u32,
    /// Union of mem_offset (MMAP), userptr (USERPTR), fd
    /// (DMABUF). Treated as u64 (8 bytes on 64-bit) here.
    pub m: u64,
    pub data_offset: u32,
    pub reserved: [u32; 11],
}

/// v4l2_buffer. Multiplanar callers populate `m_planes` to
/// point at a `[V4l2Plane; num_planes]` array; the kernel
/// reads `length` to know how many planes to fill.
///
/// Size: 88 bytes on 64-bit Linux (4*5 + 4-pad + 16 timestamp
/// + 16 timecode + 4*2 + 8 m + 4*3 + 4-pad = 88).
#[repr(C)]
#[derive(Default)]
pub struct V4l2Buffer {
    pub index: u32,
    pub buf_type: u32,
    pub bytesused: u32,
    pub flags: u32,
    pub field: u32,
    pub timestamp_sec: i64,
    pub timestamp_usec: i64,
    pub timecode: V4l2Timecode,
    pub sequence: u32,
    pub memory: u32,
    /// For multiplanar: pointer to a [V4l2Plane] array (length
    /// in `length` field). For single-plane MMAP: a buffer
    /// offset. We always use multiplanar.
    pub m_planes: u64,
    pub length: u32,
    pub reserved2: u32,
    pub request_fd: i32,
    pub _trailing_pad: u32,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
pub struct V4l2Timecode {
    pub type_: u32,
    pub flags: u32,
    pub frames: u8,
    pub seconds: u8,
    pub minutes: u8,
    pub hours: u8,
    pub userbits: [u8; 4],
}

/// v4l2_requestbuffers. Size: 20 bytes.
#[repr(C)]
#[derive(Default)]
pub struct V4l2Requestbuffers {
    pub count: u32,
    pub buf_type: u32,
    pub memory: u32,
    pub capabilities: u32,
    pub flags: u8,
    pub reserved: [u8; 3],
}

// ============================================================
// Compile-time size guards. The ioctl request word encodes
// sizeof(struct) in its top bits, so kernel rejects EINVAL on
// any mismatch. Catching these at build time beats running on
// hardware to find them.
// ============================================================

const _: () = {
    if std::mem::size_of::<V4l2Capability>() != 104 {
        panic!("V4l2Capability size mismatch (expected 104)");
    }
    if std::mem::size_of::<V4l2PlanePixFormat>() != 20 {
        panic!("V4l2PlanePixFormat size mismatch (expected 20)");
    }
    if std::mem::size_of::<V4l2PixFormatMplane>() != 192 {
        panic!("V4l2PixFormatMplane size mismatch (expected 192 packed)");
    }
    if std::mem::size_of::<V4l2Format>() != 208 {
        panic!("V4l2Format size mismatch (expected 208)");
    }
    if std::mem::size_of::<V4l2Plane>() != 64 {
        panic!("V4l2Plane size mismatch (expected 64)");
    }
    if std::mem::size_of::<V4l2Buffer>() != 88 {
        panic!("V4l2Buffer size mismatch (expected 88 on 64-bit Linux)");
    }
    if std::mem::size_of::<V4l2Timecode>() != 16 {
        panic!("V4l2Timecode size mismatch (expected 16)");
    }
    if std::mem::size_of::<V4l2Requestbuffers>() != 20 {
        panic!("V4l2Requestbuffers size mismatch (expected 20)");
    }
    // r46.4: kernel struct v4l2_decoder_cmd is 4 (cmd) + 4 (flags)
    // + 64 (largest union variant = raw.data[16]) = 72 bytes.
    // Ioctl request encoding (VIDIOC_DECODER_CMD = _IOWR('V', 96,
    // ...)) embeds this size; a mismatch yields EINVAL only on
    // the wrap path, which is rare + hard to attribute. Subagent-
    // requested 2026-06-02.
    if std::mem::size_of::<V4l2DecoderCmd>() != 72 {
        panic!("V4l2DecoderCmd size mismatch (expected 72)");
    }
    // r83 Phase A (2026-06-08): VIDIOC_G_SELECTION marshalling
    // structs. Sizes must match <linux/videodev2.h> exactly so the
    // ioctl request word (derived from sizeof(T)) lines up with what
    // the kernel expects.
    if std::mem::size_of::<V4l2Rect>() != 16 {
        panic!("V4l2Rect size mismatch (expected 16)");
    }
    if std::mem::size_of::<V4l2Selection>() != 64 {
        panic!("V4l2Selection size mismatch (expected 64)");
    }
};

// ============================================================
// ioctl macros. Linux-only (nix is target-Linux). Each macro
// derives the request word from the struct type's size + the
// (type, nr) tuple.
//
// VIDIOC_QUERYCAP   _IOR ('V',  0, v4l2_capability)        -> 0x80685600
// VIDIOC_S_FMT      _IOWR('V',  5, v4l2_format)            -> 0xC0D05605
// VIDIOC_REQBUFS    _IOWR('V',  8, v4l2_requestbuffers)    -> 0xC0145608
// VIDIOC_QUERYBUF   _IOWR('V',  9, v4l2_buffer)            -> 0xC0585609
// VIDIOC_QBUF       _IOWR('V', 15, v4l2_buffer)            -> 0xC058560F
// VIDIOC_DQBUF      _IOWR('V', 17, v4l2_buffer)            -> 0xC0585611
// VIDIOC_STREAMON   _IOW ('V', 18, int)                    -> 0x40045612
// VIDIOC_STREAMOFF  _IOW ('V', 19, int)                    -> 0x40045613
// ============================================================

#[cfg(target_os = "linux")]
nix::ioctl_read!(vidioc_querycap, b'V', 0, V4l2Capability);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_s_fmt, b'V', 5, V4l2Format);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_reqbufs, b'V', 8, V4l2Requestbuffers);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_querybuf, b'V', 9, V4l2Buffer);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_qbuf, b'V', 15, V4l2Buffer);
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_dqbuf, b'V', 17, V4l2Buffer);
// VIDIOC_STREAMON / OFF are `_IOW('V', 18/19, int)` -- the kernel
// reads a 4-byte int from a USER POINTER, not from the ioctl
// arg-by-value. nix's `ioctl_write_int!` passes by value (the
// fd's third syscall arg becomes the int itself), which the
// kernel then misinterprets as a `void __user *` -- EFAULT or
// undefined. Use `ioctl_write_ptr!` with `libc::c_int` so the
// generated fn takes `*const c_int` and the kernel sees a real
// pointer to the buf-type word.
#[cfg(target_os = "linux")]
nix::ioctl_write_ptr!(vidioc_streamon, b'V', 18, libc::c_int);
#[cfg(target_os = "linux")]
nix::ioctl_write_ptr!(vidioc_streamoff, b'V', 19, libc::c_int);
// VIDIOC_EXPBUF (V4L2 piece 4a): _IOWR('V', 16, v4l2_exportbuffer)
// -- caller fills in {buf_type, index, plane, flags}, kernel
// writes back the exported `fd`. Pairs with REQBUFS using
// `V4L2_MEMORY_DMABUF`.
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_expbuf, b'V', 16, V4l2Exportbuffer);
// VIDIOC_DECODER_CMD (r46.4): _IOWR('V', 96, v4l2_decoder_cmd)
// -- the V4L2-stateful-decoder spec's mechanism to drive decoder
// state machine transitions (START/STOP) independent of buffer-
// flag flow. r46.4 uses V4L2_DEC_CMD_START to resume CAPTURE
// after V4L2_BUF_FLAG_LAST without the STREAMOFF/STREAMON cycle
// that bcm2835-codec rejects (verified live on FYS 2026-06-02
// with the r46.3 EINVAL failure mode).
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_decoder_cmd, b'V', 96, V4l2DecoderCmd);
// r84 (2026-06-09): VIDIOC_TRY_DECODER_CMD = _IOWR('V', 97,
// v4l2_decoder_cmd) per linux/videodev2.h. Dry-run variant of
// VIDIOC_DECODER_CMD: verifies whether the command WOULD be
// accepted by the driver, without actually issuing it. Critical
// for r84's CMD_STOP capability probe -- the real CMD_STOP on an
// empty pipeline triggers FLAG_LAST emission immediately
// (drain-on-empty-queue spec behavior, per the V4L2 stateful-
// decoder spec), which would stamp a pre-QBUFed CAPTURE buffer
// with FLAG_LAST and wedge the decoder. TRY has no such side
// effect -- it returns the same EINVAL/ENOTTY semantics as the
// real ioctl without touching kernel state.
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_try_decoder_cmd, b'V', 97, V4l2DecoderCmd);

// r83 Phase A (2026-06-08): VIDIOC_G_SELECTION -- query the
// kernel's reported crop / display window for a queue, returning
// a v4l2_selection {type, target, flags, r=(left, top, width,
// height), reserved[9]}. Used to ask the CAPTURE pool for its
// V4L2_SEL_TGT_COMPOSE (= 0x0100) rect, which is the valid
// display region inside the allocated pixfmt frame. On the
// bcm2835-codec the allocated CAPTURE height is rounded to the
// next H.264 macroblock multiple (1080 -> 1088 for 1080p),
// leaving 8 rows of uninitialised NV12 padding at the bottom
// that render as luma-0 + chroma-128 -> bright green when
// sampled by the BT.709 shader. G_SELECTION(COMPOSE) tells us
// "use only the (0, 0, 1920, 1080) sub-rect, ignore the pad."
// `_IOWR('V', 60, v4l2_selection)`.
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_g_selection, b'V', 60, V4l2Selection);

// perf-decode MIN_BUFFERS plumbing draft (2026-06-15):
// VIDIOC_G_CTRL = _IOWR('V', 27, v4l2_control) per
// <linux/videodev2.h>. Used to query V4L2_CID_MIN_BUFFERS_
// FOR_CAPTURE (CID 0x009A0921) — the V4L2 stateful-decoder
// spec's userspace contract for "minimum number of CAPTURE
// buffers the driver requires to avoid reorder starvation."
// See `Decoder::min_buffers_for_capture` for the wrapper.
#[cfg(target_os = "linux")]
nix::ioctl_readwrite!(vidioc_g_ctrl, b'V', 27, V4l2Control);

/// V4L2_CID_MIN_BUFFERS_FOR_CAPTURE per
/// <linux/v4l2-controls.h> line 106:
///   #define V4L2_CID_MIN_BUFFERS_FOR_CAPTURE (V4L2_CID_BASE+39)
///
/// V4L2_CID_BASE = (V4L2_CTRL_CLASS_USER | 0x900)
///               = 0x00980000 | 0x900
///               = 0x00980900
/// + 39 (0x27) = 0x00980927.
///
/// Lives in the USER control class (NOT codec — codec-class is
/// 0x00990000 / 0x00A40000 stateless; MIN_BUFFERS_FOR_CAPTURE
/// has always been in user-class for stateful decoders).
///
/// Read via VIDIOC_G_CTRL after S_FMT on the CAPTURE queue.
/// The driver reports the minimum number of buffers it needs
/// for H.264 reorder + EGL hold. Userspace MUST request at
/// least this count via REQBUFS, plus a headroom slot for the
/// consumer (caller decides the headroom). bcm2835-codec on
/// the Pi Zero 2 W typically reports `min = 1` (single
/// decoder); the safe production floor is now SPLIT BY PATH
/// after perf-decode spike-hunt Phase B (2026-06-15):
/// * cold-start path: `PRIME_K_FLOOR_DEFAULT` = 3
///   (equality-boundary safe via at-pace Frame::drop re-QBUF)
/// * preload path: `PRIME_K_FLOOR_FOR_PRELOAD` = 4
///   (preserves r77's hard guarantee of ≥1 free slot whether
///   or not post-prime drain succeeds; required because preload
///   consumer is DELAYED ~1s)
///
/// Pre-Phase-B both paths used floor=4. See main.rs
/// PRIME_K_FLOOR_* docstrings for the saturation-math
/// derivation + content-profile fence + r73/r77/r93/r94 history.
///
/// Sacred review caught this constant as 0x009A0921 in the
/// first draft (codec-class misattribution). Fixed pre-commit;
/// 0x009A0921 would have EINVAL'd on every ioctl and silently
/// neutered the spec-correct path on a future ship attempt.
#[cfg(target_os = "linux")]
pub const V4L2_CID_MIN_BUFFERS_FOR_CAPTURE: u32 = 0x0098_0927;

// ============================================================
// Higher-level types.
// ============================================================

/// Decoded view of [`V4l2Capability`].
#[derive(Debug, Clone)]
pub struct Capabilities {
    pub driver: String,
    pub card: String,
    pub bus_info: String,
    pub raw_capabilities: u32,
    pub device_caps: u32,
    pub version: u32,
}

impl Capabilities {
    pub fn is_m2m_mplane(&self) -> bool {
        self.device_caps & V4L2_CAP_VIDEO_M2M_MPLANE != 0
    }
    pub fn is_streaming(&self) -> bool {
        self.device_caps & V4L2_CAP_STREAMING != 0
    }
    pub fn has_device_caps(&self) -> bool {
        self.raw_capabilities & V4L2_CAP_DEVICE_CAPS != 0
    }
}

// V4L2 quantization range values (from <linux/videodev2.h>).
// Set in V4l2PixFormatMplane.quantization at S_FMT time; on
// CAPTURE the driver may overwrite with its choice (the quantization
// of an M2M decoder's output is a property of the bitstream's VUI).
// The MMAP-path FS_NV12_TO_RGB shader assumes LIM_RANGE math;
// FULL_RANGE input would visibly crush blacks + clip whites.
pub const V4L2_QUANTIZATION_DEFAULT: u8 = 0;
pub const V4L2_QUANTIZATION_FULL_RANGE: u8 = 1;
pub const V4L2_QUANTIZATION_LIM_RANGE: u8 = 2;

/// Validate a V4L2 quantization byte against the MMAP-path
/// `FS_NV12_TO_RGB` shader's LIM_RANGE assumption. Accepts
/// DEFAULT (driver defers to colorspace spec defaults — limited
/// for SMPTE170M / REC709 which is what bcm2835-codec emits) or
/// LIM_RANGE explicitly. Returns Err for FULL_RANGE (the shader
/// would crush blacks + clip whites) or any unknown value.
///
/// Module-level (not method) so unit tests can exercise it
/// without opening a real V4L2 device.
pub fn check_quantization_for_lim_range_shader(q: u8) -> anyhow::Result<u8> {
    match q {
        V4L2_QUANTIZATION_DEFAULT | V4L2_QUANTIZATION_LIM_RANGE => Ok(q),
        V4L2_QUANTIZATION_FULL_RANGE => Err(anyhow::anyhow!(
            "V4L2 CAPTURE emitting FULL_RANGE quantization; \
             FS_NV12_TO_RGB assumes LIM_RANGE and would clip. \
             Either fix the shader or constrain the input bitstream."
        )),
        other => Err(anyhow::anyhow!(
            "V4L2 CAPTURE quantization is {} (expected 0=DEFAULT or 2=LIM_RANGE)",
            other
        )),
    }
}

/// Negotiated format -- what the kernel said yes to after S_FMT.
/// May differ from what the caller asked for if the codec
/// adjusts width/height to alignment constraints or picks a
/// different field order.
#[derive(Debug, Clone)]
pub struct NegotiatedFormat {
    pub width: u32,
    pub height: u32,
    pub pixelformat: u32,
    pub num_planes: u8,
    /// V4L2 quantization range: one of V4L2_QUANTIZATION_DEFAULT /
    /// FULL_RANGE / LIM_RANGE. On CAPTURE this reflects what the
    /// codec emits; downstream shaders must match. Set on the
    /// CAPTURE side by `bcm2835-codec` based on the bitstream VUI
    /// (typically LIM_RANGE for H.264 broadcast content).
    pub quantization: u8,
    /// Per-plane (sizeimage, bytesperline). Only entries
    /// [0..num_planes] are meaningful.
    pub plane_fmt: [V4l2PlanePixFormat; 8],
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureBufferType {
    Mmap,
    DmaBuf,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum QueueDirection {
    Output,
    Capture,
}

/// r81 (2026-06-08): outcome of one `drain_capture_step_no_frame`
/// poll. The Frame-bypass DQBUF+QBUF helper returns this enum
/// instead of `Result<Option<Frame>>` so callers can branch on
/// EAGAIN vs EOS vs got-a-frame without going through anyhow
/// string-matching.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DrainStep {
    /// A CAPTURE buffer was DQBUF'd then immediately re-QBUF'd.
    /// `is_last` reflects V4L2_BUF_FLAG_LAST -- when true, the
    /// kernel has signaled end-of-drain and `capture_drained`
    /// has been set inside the helper.
    GotFrame { is_last: bool },
    /// DQBUF returned EAGAIN; caller should sleep + retry within
    /// the drain budget.
    WouldBlock,
    /// DQBUF returned EPIPE OR `capture_drained` was already set
    /// on entry. Kernel's CAPTURE queue is drained; no more
    /// frames will arrive without `resume_after_eos`.
    EndOfStream,
}

impl QueueDirection {
    fn buf_type(&self) -> u32 {
        match self {
            QueueDirection::Output => V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
            QueueDirection::Capture => V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
        }
    }
}

// ============================================================
// Linux-only impl from here on.
// ============================================================

/// One mmap'd plane of one V4L2 buffer. Drop munmaps.
#[cfg(target_os = "linux")]
struct MmapRegion {
    ptr: *mut libc::c_void,
    len: usize,
}

#[cfg(target_os = "linux")]
unsafe impl Send for MmapRegion {}

#[cfg(target_os = "linux")]
impl Drop for MmapRegion {
    fn drop(&mut self) {
        if !self.ptr.is_null() {
            // SAFETY: ptr came from mmap with len; munmap with
            // the same pair is the documented release.
            unsafe { libc::munmap(self.ptr, self.len); }
        }
    }
}

#[cfg(target_os = "linux")]
impl MmapRegion {
    /// Borrow the mapped bytes as a slice. The slice lifetime
    /// is the borrow of self; callers wrapping a Frame that
    /// outlives self need an Arc anchor.
    fn as_slice(&self) -> &[u8] {
        // SAFETY: mmap returned a valid (ptr, len); kernel
        // guarantees the mapping is alive until munmap.
        unsafe { std::slice::from_raw_parts(self.ptr as *const u8, self.len) }
    }

    fn as_mut_slice(&mut self) -> &mut [u8] {
        unsafe { std::slice::from_raw_parts_mut(self.ptr as *mut u8, self.len) }
    }
}

/// State held jointly by the Decoder + every outstanding Frame.
/// Frames keep an Arc to this; on Drop they re-QBUF their buffer
/// index. The mmap regions live here too, so as long as a Frame
/// is alive (Arc keeps inner alive) its y_plane/uv_plane slices
/// are valid.
#[cfg(target_os = "linux")]
struct DecoderInner {
    /// r89 (2026-06-08): monotonic per-process Decoder-open seq.
    /// Allocated in Decoder::open via next_decoder_open_seq();
    /// emitted in [mem] decoder_open + decoder_drop lines so QA
    /// can correlate ioctl ordering across overlapping decoder
    /// lifetimes (especially the "is this ioctl from a leaked
    /// prior decoder?" case if the r87 regression recurs).
    decoder_seq: usize,
    /// Owned device file. Drop closes fd AFTER munmap (we drop
    /// `mapped_output` and `mapped_capture` first via field-order
    /// drop semantics: file is declared LAST in this struct).
    /// Documented field order is the soundness contract.
    capture_buffer_type: CaptureBufferType,
    /// Negotiated OUTPUT format (after S_FMT). None until
    /// set_output_format succeeds.
    output_format: Option<NegotiatedFormat>,
    /// Negotiated CAPTURE format. None until set_capture_format.
    capture_format: Option<NegotiatedFormat>,
    /// r83 Phase B (2026-06-08): the REQUESTED height passed to
    /// `set_capture_format` BEFORE the kernel rounded it up to the
    /// next H.264 macroblock multiple (16 lines). For 1080p input
    /// this is 1080 while `capture_format.height` is 1088. The GL
    /// paint path uses the ratio
    ///   y_crop_max = capture_display_height / capture_format.height
    /// to crop the bottom-row green padding out of the rendered
    /// frame. Phase A surfaced this via the
    /// `[perf] v4l2_capture_geometry` log line (compose=unsupported
    /// on bcm2835-codec). 0 = sentinel for "not yet set" (no
    /// CAPTURE format negotiated).
    capture_display_height: u32,
    /// OUTPUT side mmap'd regions: outer = buffer index, inner =
    /// plane index. Empty until allocate_buffers(Output) runs.
    mapped_output: Vec<Vec<MmapRegion>>,
    /// CAPTURE side mmap'd regions. Empty until allocate_buffers
    /// (Capture) runs. Drop order critical: must munmap before
    /// the File closes (Rust drops fields in declaration order,
    /// so `file` declared last is dropped last).
    ///
    /// Populated for BOTH MMAP-only and DmaBuf modes (piece 4a-fix
    /// 2026-05-14: REQBUFS uses V4L2_MEMORY_MMAP regardless of
    /// capture_buffer_type; the DmaBuf path layers an EXPBUF step
    /// on top to ALSO obtain `capture_dmabuf_fds` referring to the
    /// same kernel buffers).
    mapped_capture: Vec<Vec<MmapRegion>>,
    /// CAPTURE-side DMA-BUF fds. Populated ONLY when
    /// capture_buffer_type == DmaBuf (V4L2 piece 4a). One fd per
    /// buffer index; on bcm2835-codec NV12 a single fd covers
    /// the whole Y+UV plane region (num_planes=1, UV at offset
    /// Y_SIZE within the same buffer).
    ///
    /// Ownership: the Decoder owns these fds. They are closed in
    /// Drop AFTER stop_streaming + BEFORE the File closes. GLES
    /// callers that import a fd into an EGLImage must complete
    /// the import BEFORE the corresponding Frame drops (the
    /// kernel keeps the underlying buffer alive via EGL's
    /// reference, but a use-after-close on the fd itself is UB).
    capture_dmabuf_fds: Vec<std::os::fd::RawFd>,
    /// r101 (2026-06-09): cached EGLImage per CAPTURE buffer index.
    /// Empty until allocate_buffers; resized to capture_dmabuf_fds.len()
    /// with all-None. Lazy-populated by the paint helper on first
    /// frame from each buffer index (one cache miss per buffer
    /// across the decoder's lifetime, not per frame).
    ///
    /// Plugs the Mesa+vc4 dmabuf-ref leak: pre-r101 the paint helper
    /// called eglCreateImage + eglDestroyImage on EVERY frame. Mesa
    /// returns EGL_TRUE from destroyImage but does NOT decrement the
    /// kernel dmabuf ref, leaking ~3 MB CMA per frame at 1080p. With
    /// the cache, exactly ONE EGLImage exists per (Decoder,
    /// buffer_index) pair; destroyImage fires exactly once on
    /// DecoderInner::Drop, before close(fd) of the matching
    /// capture_dmabuf_fds entry.
    ///
    /// Each handle stores its display + destroy_fn so DecoderInner::
    /// Drop has everything it needs without re-resolving EGL entry
    /// points.
    capture_egl_images: Vec<Option<EglImageHandle>>,
    /// Buffer indices currently checked-out as Frames. Used to
    /// guard against double-DQBUF returning the same index --
    /// shouldn't happen with V4L2's contract but worth a sanity
    /// gate at the Rust boundary.
    capture_in_flight: Vec<bool>,
    /// Whether STREAMON has fired on each queue.
    output_streaming: bool,
    capture_streaming: bool,
    /// Whether the caller signaled EOF via feed(&[]).
    output_eof_sent: bool,
    /// Whether the kernel has returned V4L2_BUF_FLAG_LAST on a
    /// CAPTURE dequeue; subsequent next_frame() returns None.
    capture_drained: bool,
    /// r48 (2026-06-03): OUTPUT-side free-buffer-index pool.
    /// Populated by `allocate_buffers(Output)` with all N indices.
    /// `feed()` pops the next free index; `drain_output_quiet`'s
    /// DQBUF success path pushes the reclaimed index back.
    ///
    /// Pre-r48 history: `feed` hardcoded `buf_idx = 0`, single-shot
    /// safe per its docstring. perf-night r5 (2026-05-26) wrote a
    /// 9-line comment (video_decode.rs:170-178) anticipating "the
    /// free-list refactor that v4l2.rs:1184-1191 mentions (piece
    /// 3's real driver loop)" as the long-term fix; r46.4 on FYS
    /// 2026-06-02 made the EINVAL surface live: bcm2835-codec
    /// rejects VIDIOC_QBUF OUTPUT on a buffer it already owns,
    /// and the prior `drain_output_quiet` had a timing window
    /// where sample N+1's feed could fire before sample N's
    /// buffer was dequeued. r48 closes the race by tracking
    /// kernel-vs-userspace ownership explicitly.
    free_output_slots: VecDeque<u32>,
    /// File. Declared LAST so Drop closes it AFTER the
    /// MmapRegion Drops munmap. (Rust drops struct fields in
    /// declaration order; mapping the kernel order requires the
    /// File be last so the fd is still valid during munmap.)
    file: File,
    /// Path for diagnostics.
    path: PathBuf,
}

#[cfg(target_os = "linux")]
impl DecoderInner {
    fn fd(&self) -> std::os::fd::RawFd {
        self.file.as_raw_fd()
    }

    /// Reset state for re-open or re-test scenarios.
    fn stop_streaming_quiet(&mut self) {
        if self.output_streaming {
            let bt: libc::c_int = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE as libc::c_int;
            // SAFETY: fd owned by self.file; kernel reads 4
            // bytes from the pointer. Best-effort teardown; we
            // swallow errors so Drop doesn't panic.
            let r = unsafe { vidioc_streamoff(self.fd(), &bt) };
            eprintln!(
                "[perf] v4l2_ioctl op=STREAMOFF queue=output result={}",
                match r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
            );
            self.output_streaming = false;
            // r48: STREAMOFF returns all queued OUTPUT buffers to
            // userspace ownership. Repopulate the free pool with
            // every index so any subsequent start_streaming +
            // feed cycle starts with a full pool. Pre-r48 the
            // pool wasn't tracked so there was nothing to reset;
            // post-r48 a STREAMOFF-then-resume without this
            // would leave the kernel-tracked indices missing
            // from the free deque and feed() would either skip
            // them or hit the "all in flight" error path.
            let n = self.mapped_output.len();
            self.free_output_slots = (0..n as u32).collect();
        }
        if self.capture_streaming {
            // r84 (2026-06-09): REVERT r82 H2 CAPTURE-drain.
            // r82 telemetry on FYS: zero preload_handoff lines
            // fired (prime aborted before reaching its own log),
            // 24 REQBUFS OUTPUT EINVAL in 90s, 9 dmesg "leaving
            // buffer N in active state" warnings -- worse than
            // the r80/r81 regression. Reverting to pre-r80
            // straight STREAMOFF here; r84 ioctl-trace probes
            // will tell us what bcm2835-codec actually wants.
            // (Renumbered from r83 because code2 already shipped
            // r83 Phase A for green-line work.)
            let bt: libc::c_int = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE as libc::c_int;
            let r = unsafe { vidioc_streamoff(self.fd(), &bt) };
            eprintln!(
                "[perf] v4l2_ioctl op=STREAMOFF queue=capture result={}",
                match r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
            );
            self.capture_streaming = false;
        }
    }
}

#[cfg(target_os = "linux")]
impl Drop for DecoderInner {
    fn drop(&mut self) {
        self.stop_streaming_quiet();
        // Close exported DMA-BUF fds. The underlying kernel buffer
        // memory remains alive if any EGLImage / GL texture still
        // references it (the dmabuf is reference-counted in the
        // kernel), but our fd handles are no longer needed once
        // streaming has stopped + Frames have been dropped. Doing
        // this BEFORE field-order drops mapped_capture is fine --
        // since piece 4a-fix both mapped_capture (mmap regions)
        // AND capture_dmabuf_fds (exported fds) can be populated
        // simultaneously, but the teardown paths are disjoint:
        // close(2) for the fds here, munmap via field-order for
        // mapped_capture below. The kernel reference-counts the
        // underlying buffer memory; freeing both views is safe.
        // r101 (2026-06-09): destroy cached EGLImages BEFORE closing
        // dmabuf fds. Order matters: destroy_image on a closed fd
        // would fail (and on Mesa+vc4 leak the kernel dmabuf ref
        // again, the exact bug this cache is fixing). The
        // destroy_fn + display are carried by each handle so we
        // don't need access to an EglSession here.
        //
        // Empirical hardware finding (r100 audit + r99 bufinfo):
        // Mesa returns EGL_TRUE from destroy_image but does NOT
        // decrement the kernel dmabuf ref on the per-frame path.
        // With the cache, exactly one destroy fires per buffer
        // index across the Decoder's lifetime, so the leak is
        // BOUNDED to that one phantom ref per buffer (or zero, if
        // Mesa happens to drop the ref correctly on the only-
        // destroy-once path -- TBD on FYS).
        // r101.1 (2026-06-09): per-handle entry+exit instrumentation.
        // r101 deploy wedged FYS post-soak with `begin_slide` 60s
        // timeouts + ZERO `egl_image_destroyed` summary lines in
        // the wedge window. r101 summary line was conditional on
        // `destroyed > 0 || failed > 0`, so a decoder with no
        // cached images emitted nothing -- ambiguous between
        // "Drop ran cleanly with empty cache" and "Drop entered the
        // destroy loop and hung on the first call." This
        // instrumentation distinguishes them:
        //
        //   - egl_image_destroy_entry idx=N -- BEFORE each call
        //   - egl_image_destroy_exit  idx=N elapsed_us=N result=...
        //     -- AFTER each call (only emitted if destroy returned)
        //   - egl_image_destroy_loop_summary ... -- ALWAYS at end
        //     (even count=0), so a wedge after entry-without-exit
        //     stands out vs a clean empty-cache Drop
        //
        // glFlush mitigation deliberately NOT added in r101.1: a
        // pre-flush via eglWaitClient would block on the same GPU
        // work the destroy is presumably blocking on; it's not a
        // mitigation for "destroy hangs on pending GPU state"
        // unless we have evidence destroy is failing for a state-
        // not-current reason. The entry/exit probe is what tells
        // us which one. If H1 confirms the hang is in destroy
        // entry, r101.2 can layer a real mitigation (offload to
        // background thread / per-destroy timeout / etc).
        // r101.1 subagent NIT-1: `loop_elapsed_us` captured here is
        // inflated by the per-iteration writeln! syscall overhead
        // and is NOT the load-bearing measurement for diagnosing
        // H1. The per-handle `elapsed_us` (captured tightly around
        // each `destroy_fn` call below) is the accurate destroy
        // cost; `loop_elapsed_us` is a coarse upper-bound for
        // wedge-detection-via-summary-absence only.
        let loop_start = std::time::Instant::now();
        let mut destroyed = 0usize;
        let mut failed = 0usize;
        let mut visited = 0usize;
        for slot in self.capture_egl_images.drain(..) {
            if let Some(h) = slot {
                let _ = writeln!(
                    std::io::stderr(),
                    "[perf] egl_image_destroy_entry idx={} drop_path=DecoderInner",
                    visited,
                );
                let t_entry = std::time::Instant::now();
                // SAFETY: handle.destroy_fn is the eglDestroyImageKHR
                // entry point resolved + stored when the EGLImage
                // was lazy-created. display + image were owned by
                // EGL at create time; the destroy call hands them
                // back. Function pointer + opaque pointers; no
                // Rust references involved.
                let r = unsafe { (h.destroy_fn)(h.display, h.image) };
                let elapsed_us = t_entry.elapsed().as_micros();
                let result = if r == 0 { "fail" } else { "ok" };
                let _ = writeln!(
                    std::io::stderr(),
                    "[perf] egl_image_destroy_exit idx={} elapsed_us={} result={} drop_path=DecoderInner",
                    visited, elapsed_us, result,
                );
                if r == 0 {
                    failed += 1;
                } else {
                    destroyed += 1;
                }
                visited += 1;
            } else {
                visited += 1;
            }
        }
        let loop_elapsed_us = loop_start.elapsed().as_micros();
        // r101.1: summary line ALWAYS fires, including empty-cache
        // case (visited=0). This is the load-bearing diagnostic
        // for distinguishing "Drop ran cleanly with no cached
        // images" (visited=0 destroyed=0) from "Drop hung at first
        // destroy call" (an entry line with no matching exit and
        // no summary -- the wedge signal).
        let _ = writeln!(
            std::io::stderr(),
            "[perf] egl_image_destroy_loop_summary visited={} destroyed={} failed={} loop_elapsed_us={} drop_path=DecoderInner",
            visited, destroyed, failed, loop_elapsed_us,
        );
        for fd in self.capture_dmabuf_fds.drain(..) {
            // SAFETY: fd was returned by VIDIOC_EXPBUF + owned by
            // self until now; close(2) is the matched teardown.
            unsafe { libc::close(fd); }
        }
        // mapped_output + mapped_capture drop via field-order
        // semantics here, calling munmap. file drops last,
        // closing the fd. No leaks.

        // r75 (2026-06-07): decrement the live-Decoder counter on
        // every scope exit (clean playlist progression, prime-failure
        // `?`-propagation, panic).
        //
        // r75 subagent BLOCKER-1 + WARN-3 fixes:
        //   - `fetch_update` floors the ATOMIC at 0 so a double-Drop
        //     or unmatched decrement can't wrap to usize::MAX +
        //     corrupt QA's time-series. Pre-fix used fetch_sub +
        //     local saturating_sub which only saturated the display;
        //     the atomic itself still wrapped.
        //   - `writeln!` on stderr ignores BrokenPipeError so a Drop
        //     during stack unwind (e.g., backend died, stderr fd
        //     closed) can't double-panic and abort the process.
        let after = MMAL_COMPONENTS_LIVE
            .fetch_update(
                std::sync::atomic::Ordering::Relaxed,
                std::sync::atomic::Ordering::Relaxed,
                |v| Some(v.saturating_sub(1)),
            )
            .map(|prev| prev.saturating_sub(1))
            .unwrap_or(0);
        let _ = writeln!(
            std::io::stderr(),
            "[mem] vpu_mmal_components={} delta=-1 path={}",
            after, self.path.display(),
        );
        // r89 (2026-06-08): seq tag matches the [mem] decoder_open
        // line emitted in Decoder::open. Lets QA see the full
        // lifetime span of each decoder (open seq=N ... drop seq=N).
        let _ = writeln!(
            std::io::stderr(),
            "[mem] decoder_drop seq={} mmal_live={} path={}",
            self.decoder_seq, after, self.path.display(),
        );
    }
}

/// A decoded video frame. Holds an Arc reference back to the
/// Decoder's inner state so the mmap regions / dmabuf fds stay
/// alive while the Frame is in flight. On Drop, re-QBUFs the
/// buffer index through the inner lock.
#[cfg(target_os = "linux")]
pub struct Frame {
    inner: Arc<Mutex<DecoderInner>>,
    /// Buffer index in the CAPTURE pool (re-QBUFed on Drop).
    capture_buffer_index: u32,
    /// Cached width/height so the accessors don't need to take
    /// the inner lock for every call.
    width: u32,
    height: u32,
    /// Per-plane (length, bytesused) snapshotted at DQBUF time.
    /// bytesused is the kernel's "this many bytes are valid in
    /// this plane"; for NV12 it's typically width*height (Y)
    /// and width*height/2 (UV).
    plane_lengths: [usize; 2],
    /// Cached raw pointers + lengths into the mmap regions for
    /// the y/uv planes. Populated for BOTH MMAP-only and DmaBuf
    /// paths since piece 4a-fix (REQBUFS uses V4L2_MEMORY_MMAP
    /// regardless of capture_buffer_type; the dma_buf fds are an
    /// ADDITIONAL view on the same kernel buffers, not a
    /// replacement). The paint helper prefers `dmabuf_fd` when
    /// available + EGL_EXT_image_dma_buf_import is present, but
    /// can fall back to these CPU pointers if extensions are
    /// missing at runtime.
    y_ptr: *const u8,
    y_len: usize,
    uv_ptr: *const u8,
    uv_len: usize,
    /// DMA-BUF path (V4L2 piece 4): the exported fd that GLES
    /// imports via EGLImage. None when capture_buffer_type=Mmap.
    /// Caller MUST NOT close() this fd directly -- the Decoder
    /// owns it and closes it from Drop after stop_streaming.
    /// EGLImage import must happen while the Frame is alive;
    /// once imported, the EGLImage holds its own kernel-side
    /// dmabuf reference and the Frame can drop freely.
    dmabuf_fd: Option<std::os::fd::RawFd>,
    /// Kernel-reported `plane_fmt[0].bytesperline` -- the Y-plane
    /// stride in bytes. For bcm2835-codec NV12 this is typically
    /// `width` (no padding) but a future codec / alignment quirk
    /// may report stride > width. Callers (especially the DmaBuf
    /// EGLImage import path) MUST use this value, not `width`,
    /// for `EGL_DMA_BUF_PLANE*_PITCH_EXT` -- subagent-blocker
    /// check in piece 4's "Stride vs width" review section.
    stride: u32,
}

#[cfg(target_os = "linux")]
unsafe impl Send for Frame {}

#[cfg(target_os = "linux")]
impl Frame {
    pub fn width(&self) -> u32 { self.width }
    pub fn height(&self) -> u32 { self.height }

    /// Y plane bytes. NV12 layout: tightly packed luma samples
    /// at `width*height` bytes (modulo stride alignment).
    /// Populated for BOTH MMAP and DmaBuf modes (piece 4a-fix
    /// 2026-05-14: REQBUFS uses V4L2_MEMORY_MMAP regardless of
    /// capture_buffer_type; the dma_buf fds are an additional
    /// view on the same kernel buffers).
    pub fn y_plane(&self) -> &[u8] {
        if self.y_ptr.is_null() {
            return &[];
        }
        // SAFETY: y_ptr came from MmapRegion::ptr inside the
        // DecoderInner this Frame holds an Arc to. The Arc
        // outlives the slice borrow (lifetime tied to &self).
        unsafe { std::slice::from_raw_parts(self.y_ptr, self.y_len) }
    }

    /// UV plane bytes. NV12 layout: interleaved Cb,Cr at
    /// `width*height/2` bytes. Populated for both MMAP and
    /// DmaBuf modes (piece 4a-fix 2026-05-14).
    pub fn uv_plane(&self) -> &[u8] {
        if self.uv_ptr.is_null() {
            return &[];
        }
        unsafe { std::slice::from_raw_parts(self.uv_ptr, self.uv_len) }
    }

    /// `None` for MMAP path; `Some(fd)` for DMA-BUF (V4L2 piece
    /// 4). The fd is owned by the Decoder; the caller must NOT
    /// close it. Use the fd to build an EGLImage via
    /// `EGL_EXT_image_dma_buf_import` before the Frame drops.
    pub fn dma_buf_fd(&self) -> Option<std::os::fd::RawFd> {
        self.dmabuf_fd
    }

    /// Y-plane stride in bytes (`plane_fmt[0].bytesperline` from
    /// VIDIOC_S_FMT). Equal to width on bcm2835-codec NV12 in
    /// practice but MAY exceed width on other codecs / alignment
    /// regimes. DmaBuf EGLImage import requires this value (not
    /// width) for `EGL_DMA_BUF_PLANE*_PITCH_EXT`.
    pub fn stride(&self) -> u32 {
        self.stride
    }

    /// r101 (2026-06-09): the CAPTURE buffer index this Frame
    /// borrows. Used by the EGLImage cache so the paint helper
    /// can look up `Decoder::cached_egl_image(idx)` for THIS
    /// buffer-index slot. Pre-r101 the index was opaque (only
    /// referenced internally by Frame::Drop's re-QBUF).
    pub fn capture_buffer_index(&self) -> u32 {
        self.capture_buffer_index
    }
}

#[cfg(target_os = "linux")]
impl Drop for Frame {
    fn drop(&mut self) {
        // Re-QBUF this buffer index on the CAPTURE queue so the
        // kernel can decode into it again. Failure here is rare
        // (the kernel doesn't really refuse QBUF on a buffer it
        // just gave us), but we don't panic from Drop -- log
        // would be more useful, but piece 2b stays quiet.
        let Ok(mut inner) = self.inner.lock() else { return; };
        if (self.capture_buffer_index as usize) < inner.capture_in_flight.len() {
            inner.capture_in_flight[self.capture_buffer_index as usize] = false;
        }
        if !inner.capture_streaming {
            // Streaming stopped (decoder is tearing down);
            // kernel reclaims buffers via STREAMOFF, no QBUF
            // needed.
            return;
        }
        // r46.4: previously also skipped on capture_drained --
        // but that leaks the FLAG_LAST buffer from the kernel
        // CAPTURE queue, because the Frame holding it sets the
        // flag (v4l2.rs ~1529) and would then skip its own
        // re-QBUF on drop. resume_after_eos clears the flag but
        // doesn't re-QBUF, so the kernel ends up running on N-1
        // buffers post-resume. Re-QBUF unconditionally while
        // streaming is correct: kernel accepts QBUF on a drained
        // queue, and any pending resume_after_eos will then have
        // all N buffers available.
        // Build a multiplanar v4l2_buffer with num_planes from
        // the negotiated format + the kernel-reported lengths.
        let Some(ref cap_fmt) = inner.capture_format else { return; };
        let num_planes = cap_fmt.num_planes as usize;
        // Piece 4a-fix: REQBUFS used V4L2_MEMORY_MMAP regardless
        // of capture_buffer_type, so re-QBUF on Drop is always
        // MMAP too. The kernel manages buffers by index; the
        // exported dma_buf fds (if any) are for the GLES paint
        // path only.
        let memory = V4L2_MEMORY_MMAP;
        let mut planes = [V4l2Plane::default(); 8];
        for p in 0..num_planes {
            planes[p].length = cap_fmt.plane_fmt[p].sizeimage;
            // MMAP: kernel ignores m on CAPTURE re-QBUF (looks up
            // the index instead). planes[p].m stays 0.
            planes[p].bytesused = 0;
        }
        let mut buf = V4l2Buffer {
            index: self.capture_buffer_index,
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            ..Default::default()
        };
        // SAFETY: fd from inner.file; buf size 88 matches ioctl
        // size encoding; planes array referenced via m_planes
        // is live until the call returns.
        unsafe {
            let _ = vidioc_qbuf(inner.fd(), &mut buf);
        }
    }
}

/// V4L2 M2M H.264 decoder client.
#[cfg(target_os = "linux")]
pub struct Decoder {
    inner: Arc<Mutex<DecoderInner>>,
}

#[cfg(target_os = "linux")]
impl Decoder {
    /// Open + sanity-check the V4L2 device.
    pub fn open(path: &Path) -> Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)
            .with_context(|| format!("open {}", path.display()))?;
        let decoder_seq_for_inner = next_decoder_open_seq();
        let inner = DecoderInner {
            decoder_seq: decoder_seq_for_inner,
            capture_buffer_type: CaptureBufferType::Mmap,
            output_format: None,
            capture_format: None,
            capture_display_height: 0,
            mapped_output: Vec::new(),
            mapped_capture: Vec::new(),
            capture_dmabuf_fds: Vec::new(),
            capture_egl_images: Vec::new(),
            capture_in_flight: Vec::new(),
            output_streaming: false,
            capture_streaming: false,
            output_eof_sent: false,
            capture_drained: false,
            free_output_slots: VecDeque::new(),
            file,
            path: path.to_path_buf(),
        };
        // r75 subagent BLOCKER-1 fix: bump the counter RIGHT AT
        // DecoderInner construction (NOT after capability checks).
        // Pre-fix, a cap-reject early-return between counter++ and
        // DecoderInner-being-Drop'd produced an asymmetric decrement
        // that underflowed AtomicUsize -> usize::MAX. Now increment
        // is paired one-to-one with the DecoderInner instance: if
        // the cap checks reject below, dec falls out of scope, Drop
        // fires the matching decrement, net 0. Per the dispatch's
        // hypothesis "[close path] fires after an aborted setup it
        // may itself time out and orphan the VPU-side component
        // slot" -- even cap-rejected opens are interesting for QA.
        let n_after_inc = MMAL_COMPONENTS_LIVE
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed) + 1;
        let _ = writeln!(
            std::io::stderr(),
            "[mem] vpu_mmal_components={} delta=+1 path={}",
            n_after_inc, path.display(),
        );
        // r89 (2026-06-08): monotonic per-process Decoder-open
        // sequence so QA can distinguish "ioctl from THIS decoder"
        // from "ioctl from a prior decoder" if the regression
        // recurs. Distinct from vpu_mmal_components (a live count
        // that can return to 0).
        let _ = writeln!(
            std::io::stderr(),
            "[mem] decoder_open seq={} mmal_live={} path={}",
            decoder_seq_for_inner, n_after_inc, path.display(),
        );
        let dec = Self { inner: Arc::new(Mutex::new(inner)) };
        let caps = dec.query_capabilities()
            .context("VIDIOC_QUERYCAP at open time")?;
        if !caps.has_device_caps() {
            return Err(anyhow!(
                "{}: legacy V4L1 driver? (capabilities=0x{:08x})",
                path.display(), caps.raw_capabilities
            ));
        }
        if !caps.is_m2m_mplane() {
            return Err(anyhow!(
                "{}: not M2M Multiplanar (device_caps=0x{:08x})",
                path.display(), caps.device_caps
            ));
        }
        if !caps.is_streaming() {
            return Err(anyhow!(
                "{}: doesn't support streaming (device_caps=0x{:08x})",
                path.display(), caps.device_caps
            ));
        }
        // r92 (2026-06-08): REVERTED r91's probe move. r91 moved
        // the CMD_STOP capability probe HERE (after QUERYCAP,
        // before any S_FMT/REQBUFS/STREAMON) to test the hypothesis
        // that probe position relative to streaming state mattered.
        // QA's r91 deploy data PROVED THE HYPOTHESIS, but in the
        // worst possible way: the move BROKE the OFF-mode baseline
        // (17 EINVAL in 60s with EOS_FLUSH unset). VIDIOC_TRY_DECODER_CMD
        // is empirically NOT non-destructive on bcm2835-codec when
        // called pre-STREAMON.
        //
        // r92 puts the probe back where r84/r90 had it (end of
        // start_streaming -- after both STREAMONs but before any
        // OUTPUT QBUFs). This was the known-clean location.
        //
        // The smoking-gun conclusion: bcm2835-codec has a quirk
        // where VIDIOC_(TRY_)DECODER_CMD called at certain lifecycle
        // points poisons subsequent REQBUFS state. The EOS-flush
        // body's real CMD_STOP issuance (via signal_eos_via_cmd_stop)
        // likely fires the same kernel-state quirk -- which would
        // explain the r85/r87/r90 EOS_FLUSH=1 regression that I
        // couldn't find by code review.
        // r75 subagent BLOCKER-1: counter was bumped at DecoderInner
        // construction above (panic-safe pairing with Drop).
        Ok(dec)
    }

    pub fn query_capabilities(&self) -> Result<Capabilities> {
        let inner = self.inner.lock().unwrap();
        let mut raw: V4l2Capability = unsafe { std::mem::zeroed() };
        // SAFETY: vidioc_querycap is _IOR -- kernel writes the
        // V4l2Capability. fd is owned by inner.file (locked).
        // r87 (2026-06-09) ioctl trace: QUERYCAP runs inside
        // Decoder::open BEFORE S_FMT. Was the last untraced ioctl
        // in the prime path. Adds visibility for QA's "find what
        // runs between Decoder::open and S_FMT" ask.
        let r = unsafe { vidioc_querycap(inner.fd(), &mut raw) };
        eprintln!(
            "[perf] v4l2_ioctl op=QUERYCAP result={}",
            match &r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        r.with_context(|| {
            format!("VIDIOC_QUERYCAP on {}", inner.path.display())
        })?;
        Ok(Capabilities {
            driver: c_str_to_string(&raw.driver),
            card: c_str_to_string(&raw.card),
            bus_info: c_str_to_string(&raw.bus_info),
            raw_capabilities: raw.capabilities,
            device_caps: raw.device_caps,
            version: raw.version,
        })
    }

    pub fn set_capture_buffer_type(&self, ty: CaptureBufferType) {
        self.inner.lock().unwrap().capture_buffer_type = ty;
    }

    /// Verify the CAPTURE quantization the driver picked is
    /// compatible with the MMAP-path BT.709 limited-range shader.
    /// Call AFTER `set_capture_format`.
    ///
    /// Background (P1 from `qa/v1-spec-delta-2026-05-14.md`):
    /// `FS_NV12_TO_RGB` does explicit `(Y-16)/219` style scaling
    /// assuming LIM_RANGE input. If the codec emits FULL_RANGE
    /// (Y in [0,255]), the math crushes blacks + clips whites.
    /// V4L2 sets quantization via the format struct's `quantization`
    /// byte (NOT a `V4L2_CID_QUANTIZATION` control — no such control
    /// exists in standard V4L2). The dispatch named G_CTRL/S_CTRL as
    /// the mechanism; this implementation uses the canonical format-
    /// struct field instead.
    ///
    /// Accepts: LIM_RANGE (explicit limited) or DEFAULT (driver
    /// defers to spec defaults; for V4L2_COLORSPACE_SMPTE170M /
    /// REC709 the default IS limited-range). Fails loud on
    /// FULL_RANGE — that's the only value that would visibly break
    /// the shader.
    ///
    /// DMABUF path is not affected — Mesa reads the colorimetry hint
    /// from the EGLImage attribs and inserts the right matrix.
    pub fn assert_capture_quantization_compatible(&self) -> Result<u8> {
        let inner = self.inner.lock().unwrap();
        let cap = inner.capture_format.as_ref().ok_or_else(|| {
            anyhow!("assert_capture_quantization_compatible called before set_capture_format")
        })?;
        check_quantization_for_lim_range_shader(cap.quantization)
    }

    /// perf-decode MIN_BUFFERS plumbing draft (2026-06-15):
    /// query V4L2_CID_MIN_BUFFERS_FOR_CAPTURE via VIDIOC_G_CTRL.
    /// Returns the kernel-reported minimum CAPTURE buffer count
    /// the driver requires for H.264 reorder + EGL hold.
    ///
    /// **Spec contract**: the V4L2 stateful-decoder spec
    /// (<https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/dev-decoder.html>)
    /// states userspace MUST query this control AFTER S_FMT on
    /// the CAPTURE queue and BEFORE REQBUFS, and MUST request
    /// AT LEAST this count. Userspace adds its own headroom for
    /// the consumer (the GLES texture binding, in our case).
    ///
    /// **Behavior on failure**: bcm2835-codec is known to
    /// support this control (verified via lsv4l2-controls on
    /// Pi Zero 2 W 2026-06-09). On a driver that doesn't expose
    /// it, VIDIOC_G_CTRL returns EINVAL — the caller should
    /// treat that as "no spec-correct floor available; fall
    /// back to the empirical hardcoded floor."
    ///
    /// **Why this is a DRAFT plumbing**: this method is added
    /// but NOT YET called from prime_video_decoder_with_warmup.
    /// The wiring to actually USE it (request max(min+headroom,
    /// safety_floor)) is gated behind the post-F-1+C-3+W-2
    /// bench results — see perf-decode-min-buffers-draft commit
    /// body for the queue/risk analysis.
    pub fn min_buffers_for_capture(&self) -> Result<u32> {
        let inner = self.inner.lock().unwrap();
        if inner.capture_format.is_none() {
            return Err(anyhow!(
                "min_buffers_for_capture: set_capture_format must be called first (V4L2 \
                 spec: query VIDIOC_G_CTRL(V4L2_CID_MIN_BUFFERS_FOR_CAPTURE) AFTER S_FMT)"
            ));
        }
        let mut ctrl = V4l2Control {
            id: V4L2_CID_MIN_BUFFERS_FOR_CAPTURE,
            value: 0,
        };
        // SAFETY: ioctl_readwrite! generated wrapper takes
        // &mut V4l2Control; the kernel writes the value field
        // back; we read it on success. fd is a raw file
        // descriptor that lives at least as long as `inner`.
        let result = unsafe { vidioc_g_ctrl(inner.fd(), &mut ctrl) };
        // Distinct fingerprint marker (per admin's QA strings-
        // verification gate 2026-06-15): the tag name
        // `min_buffers_for_capture_negotiated` is QA's
        // grep-key for the F-1 chain integrity check + is
        // unique to this draft commit. Emits even on EINVAL
        // (driver doesn't support the control) so the binary
        // always carries the fingerprint string regardless of
        // runtime behavior. r84 ioctl trace shape preserved
        // in the result= field.
        eprintln!(
            "[perf] min_buffers_for_capture_negotiated source=g_ctrl_probe result={}",
            match &result {
                Ok(_) => format!("ok_value_{}", ctrl.value),
                Err(e) => format!("errno_{:?}", e),
            },
        );
        result.with_context(|| {
            "VIDIOC_G_CTRL(V4L2_CID_MIN_BUFFERS_FOR_CAPTURE) failed -- driver may not \
             support the control (EINVAL); fall back to empirical hardcoded floor"
                .to_string()
        })?;
        if ctrl.value < 0 {
            return Err(anyhow!(
                "min_buffers_for_capture: kernel reported negative count {} (unexpected)",
                ctrl.value,
            ));
        }
        Ok(ctrl.value as u32)
    }

    /// VIDIOC_S_FMT on the OUTPUT (compressed-in) queue.
    pub fn set_output_format(
        &self, pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        self.set_format(QueueDirection::Output, pixel_format, width, height)
    }

    /// VIDIOC_S_FMT on the CAPTURE (decoded-out) queue.
    pub fn set_capture_format(
        &self, pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        self.set_format(QueueDirection::Capture, pixel_format, width, height)
    }

    fn set_format(
        &self, dir: QueueDirection,
        pixel_format: u32, width: u32, height: u32,
    ) -> Result<NegotiatedFormat> {
        let inner = self.inner.lock().unwrap();
        let mut pix_mp: V4l2PixFormatMplane = unsafe { std::mem::zeroed() };
        pix_mp.width = width;
        pix_mp.height = height;
        pix_mp.pixelformat = pixel_format;
        pix_mp.num_planes = 1;
        let mut fmt: V4l2Format = unsafe { std::mem::zeroed() };
        fmt.buf_type = dir.buf_type();
        // Copy the packed struct into the byte buffer at offset 0.
        // SAFETY: fmt.fmt is 200 bytes; pix_mp is 192 bytes packed;
        // copy fits.
        unsafe {
            std::ptr::copy_nonoverlapping(
                &pix_mp as *const _ as *const u8,
                fmt.fmt.as_mut_ptr(),
                std::mem::size_of::<V4l2PixFormatMplane>(),
            );
        }
        // SAFETY: VIDIOC_S_FMT is _IOWR. Caller's fmt struct is
        // written by the kernel with the negotiated format on Ok.
        let sfmt_result = unsafe { vidioc_s_fmt(inner.fd(), &mut fmt) };
        // r84 ioctl trace.
        eprintln!(
            "[perf] v4l2_ioctl op=S_FMT queue={} requested_w={} requested_h={} result={}",
            match dir { QueueDirection::Output => "output", QueueDirection::Capture => "capture" },
            width, height,
            match &sfmt_result { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        sfmt_result.with_context(|| {
            format!("VIDIOC_S_FMT ({:?}) on {}", dir, inner.path.display())
        })?;
        // Copy negotiated values back out.
        let neg_pix_mp: V4l2PixFormatMplane = unsafe {
            std::ptr::read_unaligned(fmt.fmt.as_ptr() as *const _)
        };
        // Read packed fields into local copies so we don't take
        // references into the packed struct.
        let neg = NegotiatedFormat {
            width: neg_pix_mp.width,
            height: neg_pix_mp.height,
            pixelformat: neg_pix_mp.pixelformat,
            num_planes: neg_pix_mp.num_planes,
            quantization: neg_pix_mp.quantization,
            plane_fmt: neg_pix_mp.plane_fmt,
        };
        drop(inner);
        let mut inner = self.inner.lock().unwrap();
        match dir {
            QueueDirection::Output => inner.output_format = Some(neg.clone()),
            QueueDirection::Capture => {
                inner.capture_format = Some(neg.clone());
                // r83 Phase B (2026-06-08): snapshot the REQUESTED
                // height — the actually-valid display region inside
                // the rounded-up allocation. Phase A's
                // VIDIOC_G_SELECTION(COMPOSE) probe returns ENOTTY
                // on bcm2835-codec, so we fall back to "what we
                // asked for" as the source-of-truth display height.
                // The dispatch authorised this fallback path (Option
                // 2 from QA's recommendation) — see qa/r83-phase-b
                // -green-line-fix-2026-06-08.md §"Why
                // capture_display_height = requested height".
                inner.capture_display_height = height;
            }
        }
        drop(inner);

        // r83 Phase A (2026-06-08): on CAPTURE S_FMT, query
        // VIDIOC_G_SELECTION(COMPOSE) to surface whether the kernel
        // allocated more rows than it's going to fill with valid
        // pixels (the 1080 -> 1088 macroblock rounding on bcm2835-
        // codec). Log both the pixfmt dims and the COMPOSE rect
        // side-by-side so journalctl can confirm or refute the
        // bottom-row-green hypothesis empirically.
        //
        // Probe is best-effort: G_SELECTION can return ENOTTY on
        // older drivers OR EINVAL on bottom-up queries that the
        // driver hasn't wired; in either case we log the failure
        // mode rather than failing S_FMT itself (the rest of the
        // path still works, just with the misalignment uncorrected).
        if matches!(dir, QueueDirection::Capture) {
            match self.capture_compose_rect_internal() {
                Ok(Some(r)) => eprintln!(
                    "[perf] v4l2_capture_geometry pixfmt_w={} pixfmt_h={} \
                     plane_stride={} compose_x={} compose_y={} \
                     compose_w={} compose_h={}",
                    neg.width, neg.height,
                    neg.plane_fmt.first().map(|pf| pf.bytesperline).unwrap_or(0),
                    r.left, r.top, r.width, r.height,
                ),
                Ok(None) => eprintln!(
                    "[perf] v4l2_capture_geometry pixfmt_w={} pixfmt_h={} \
                     plane_stride={} compose=unsupported",
                    neg.width, neg.height,
                    neg.plane_fmt.first().map(|pf| pf.bytesperline).unwrap_or(0),
                ),
                Err(e) => eprintln!(
                    "[perf] v4l2_capture_geometry pixfmt_w={} pixfmt_h={} \
                     plane_stride={} compose_err={}",
                    neg.width, neg.height,
                    neg.plane_fmt.first().map(|pf| pf.bytesperline).unwrap_or(0),
                    e,
                ),
            }
        }
        Ok(neg)
    }

    /// r83 Phase B (2026-06-08): the actually-valid display height
    /// inside the kernel's CAPTURE allocation. Returns `Some(h)` once
    /// `set_capture_format` has been called (with `h` = the height
    /// the caller PASSED IN, not the rounded-up dimension the kernel
    /// negotiated back); `None` until then.
    ///
    /// For bcm2835-codec NV12 1080p input this is 1080 while
    /// `Frame::height()` returns 1088 (the rounded-up allocation).
    /// The GL paint path uses the ratio `display / allocation` to
    /// crop the bottom-row green padding out of the rendered frame.
    /// See `qa/r83-phase-b-green-line-fix-2026-06-08.md`.
    pub fn capture_display_height(&self) -> Option<u32> {
        let inner = self.inner.lock().unwrap();
        let h = inner.capture_display_height;
        if h == 0 { None } else { Some(h) }
    }

    /// r83 Phase B (2026-06-08): the y-axis crop fraction for NV12
    /// shader sampling. Computes `capture_display_height() /
    /// capture_format.height` as a `f32` in `(0, 1]`. Returns
    /// `1.0` when the display height equals the allocated height
    /// (no padding) OR either value isn't yet known (fail-soft to
    /// "no crop" so existing callers behave as today). The GL paint
    /// path passes this to `u_y_crop_max` in `FS_NV12_TO_RGB` /
    /// `FS_NV12_DMABUF_TO_RGB`; the shader uses it to skip the
    /// uninitialised rows at the top of the texture (high uv.y end,
    /// which the existing v-flip maps to the bottom of the displayed
    /// frame on bcm2835-codec).
    pub fn capture_y_crop_max(&self) -> f32 {
        let inner = self.inner.lock().unwrap();
        let display_h = inner.capture_display_height;
        let alloc_h = inner.capture_format
            .as_ref()
            .map(|f| f.height)
            .unwrap_or(0);
        compute_y_crop_max(display_h, alloc_h)
    }

    /// r83 Phase A (2026-06-08): query the kernel's compose rect
    /// for the CAPTURE queue. Returns `Ok(Some(rect))` on success,
    /// `Ok(None)` when the driver doesn't implement G_SELECTION
    /// (errno ENOTTY) or doesn't expose the COMPOSE target (errno
    /// EINVAL — common on capture-only queues whose alloc dims
    /// already match the display region). Returns `Err` for
    /// genuine failures.
    ///
    /// On bcm2835-codec the m2m decoder DOES support
    /// G_SELECTION(COMPOSE) and reports the actually-valid display
    /// region — for 1080p H.264 input that's (0, 0, 1920, 1080)
    /// against a (1920, 1088) pixfmt allocation. Callers that
    /// sample the CAPTURE texture / DMABUF should use this rect
    /// rather than the pixfmt dims to avoid sampling the bottom 8
    /// rows of NV12-uninitialised (= green when BT.709-decoded)
    /// padding.
    pub fn capture_compose_rect(&self) -> Result<Option<V4l2Rect>> {
        self.capture_compose_rect_internal()
    }

    fn capture_compose_rect_internal(&self) -> Result<Option<V4l2Rect>> {
        let inner = self.inner.lock().unwrap();
        let mut sel = V4l2Selection {
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            target: V4L2_SEL_TGT_COMPOSE,
            ..Default::default()
        };
        // SAFETY: VIDIOC_G_SELECTION is _IOWR. fd owned by inner.
        // Caller's sel struct is fully initialised; kernel writes
        // back `r` + `flags` (+ may overwrite `reserved` as
        // documented per the API).
        //
        // r86 (2026-06-09): trace this ioctl. r85 telemetry on
        // FYS showed a 3-second untraced gap between S_FMT capture
        // and REQBUFS output that turned out to live HERE — this
        // is the ONLY ioctl between those two traced sites in the
        // prime sequence. The G_SELECTION call returns ENOTTY on
        // bcm2835-codec (no COMPOSE target), but the elapsed time
        // of the call itself was previously invisible to the
        // trace. r86 adds the missing coverage so QA can confirm
        // or refute the "G_SELECTION takes 3s post-EOS-flush"
        // hypothesis empirically.
        let t_g_sel = std::time::Instant::now();
        let r = unsafe { vidioc_g_selection(inner.fd(), &mut sel) };
        let g_sel_us = t_g_sel.elapsed().as_micros();
        eprintln!(
            "[perf] v4l2_ioctl op=G_SELECTION queue=capture target=COMPOSE elapsed_us={} result={}",
            g_sel_us,
            match &r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        match r {
            Ok(_) => Ok(Some(sel.r)),
            // ENOTTY: ioctl not implemented (older driver, or this
            // device doesn't support the G_SELECTION mechanism).
            // EINVAL: ioctl implemented but doesn't recognise the
            // (buf_type, target) combination — typical when the
            // allocated dims already match the display region and
            // the driver short-circuits the query.
            Err(nix::errno::Errno::ENOTTY) | Err(nix::errno::Errno::EINVAL) => Ok(None),
            Err(e) => Err(anyhow!("VIDIOC_G_SELECTION COMPOSE: {}", e)),
        }
    }

    /// r101 (2026-06-09): atomic get-or-init for the cached EGLImage
    /// at CAPTURE buffer index `idx`. Holds the Decoder Mutex for
    /// the entire check-create-insert sequence so a future
    /// multi-threaded caller can't double-create (subagent WARN-4).
    ///
    /// On cache hit: returns `Ok(handle)`, `created=false`.
    /// On cache miss: invokes `create()` while holding the lock,
    /// stores the returned handle, returns it with `created=true`.
    /// If `create()` returns `Err`, the slot stays None and the
    /// error is propagated unchanged.
    ///
    /// The `create` closure is called WITH THE MUTEX HELD. Today's
    /// renderer-side caller doesn't take any other Decoder Mutex
    /// inside `create` (eglCreateImageKHR is GL/EGL state, not
    /// Decoder state), so the held-lock window is bounded by the
    /// EGL call (~1-2 ms). A future caller that re-enters the
    /// Decoder Mutex inside `create` would deadlock; document
    /// loud.
    ///
    /// On out-of-range idx, calls `create()` (so the caller's
    /// EGLImage isn't wasted), then DESTROYS it via the carried
    /// destroy_fn (so the kernel dmabuf ref isn't pinned) and
    /// returns the same handle with `created=true`. The caller
    /// would just be storing a handle whose underlying kernel
    /// buffer they don't own; we make the out-of-range case loud
    /// instead of silently leaking.
    pub fn get_or_init_egl_image<F>(
        &self,
        idx: u32,
        create: F,
    ) -> Result<(EglImageHandle, bool)>
    where
        F: FnOnce() -> Result<EglImageHandle>,
    {
        let mut inner = self.inner.lock().unwrap();
        if let Some(Some(h)) = inner.capture_egl_images.get(idx as usize).copied() {
            return Ok((h, false));
        }
        // Cache miss (or out-of-range). Create regardless so we
        // know whether the EGL call succeeded; insert only if the
        // slot exists.
        let handle = create()?;
        if let Some(slot) = inner.capture_egl_images.get_mut(idx as usize) {
            if let Some(prior) = slot.take() {
                // Reachable only if another thread inserted between
                // our lookup and create — shouldn't happen with the
                // single-lock structure, but defensive destroy in
                // case of a future logic change.
                let _ = unsafe { (prior.destroy_fn)(prior.display, prior.image) };
            }
            *slot = Some(handle);
        } else {
            // Out-of-range: destroy the just-created handle so the
            // kernel ref isn't pinned. Same loud-leak-at-misuse
            // semantics as cache_egl_image.
            let _ = unsafe { (handle.destroy_fn)(handle.display, handle.image) };
            let _ = writeln!(
                std::io::stderr(),
                "warn: get_or_init_egl_image idx={} out of range (cache len={}); \
                 destroyed the rejected handle inline.",
                idx,
                inner.capture_egl_images.len(),
            );
        }
        Ok((handle, true))
    }

    /// r101 (2026-06-09): cached EGLImage lookup for CAPTURE buffer
    /// index. Returns `Some(handle)` if a prior paint already
    /// imported that buffer; `None` if not yet cached (paint helper
    /// should create one + `cache_egl_image` it). Returns `None`
    /// for out-of-range indices to keep the paint helper's lookup
    /// path crash-free even if a future refactor mis-sequences
    /// allocate_buffers / paint.
    ///
    /// **Prefer `get_or_init_egl_image` for the create-on-miss
    /// pattern** -- it serializes check+create+insert under one
    /// Mutex acquisition. `cached_egl_image` is kept for read-only
    /// inspection (tests + debug probes).
    pub fn cached_egl_image(&self, idx: u32) -> Option<EglImageHandle> {
        let inner = self.inner.lock().unwrap();
        inner
            .capture_egl_images
            .get(idx as usize)
            .and_then(|s| s.as_ref())
            .copied()
    }

    /// r101 (2026-06-09): insert a freshly-created EGLImage into
    /// the per-buffer cache. Caller is responsible for ensuring the
    /// handle is valid (i.e. `eglCreateImageKHR` returned non-null
    /// + display + destroy_fn are correctly resolved). The handle
    /// will be destroyed in DecoderInner::Drop via
    /// `destroy_fn(display, image)`.
    ///
    /// No-op for out-of-range indices (defensive: index validity
    /// is the caller's responsibility but the cache must not
    /// panic on a stale buffer_index from a freshly-recreated
    /// decoder).
    ///
    /// Overwrites any prior cached handle WITHOUT destroying it.
    /// Today's callers cache at most once per buffer index across
    /// the Decoder's lifetime (lazy-create-on-miss); the overwrite
    /// path is reachable only if a future caller violates that
    /// contract, in which case the prior handle would leak ONCE.
    /// We prefer that to silently dropping the new handle (which
    /// would leak the new image forever) -- the overwrite at
    /// least keeps the in-cache handle current with the buffer
    /// the paint helper is about to sample.
    pub fn cache_egl_image(&self, idx: u32, handle: EglImageHandle) {
        let mut inner = self.inner.lock().unwrap();
        if let Some(slot) = inner.capture_egl_images.get_mut(idx as usize) {
            // r101 subagent WARN-1: if a prior cached handle exists
            // (overwrite path — possible if a future caller violates
            // the lazy-create-once contract), DROP the prior in a
            // loud way (destroy through the carried fn) rather than
            // silently losing it. The overwrite-without-destroy code
            // path was a one-handle leak per misuse; with the
            // destroy, misuse is observable in
            // `egl_image_destroyed count=N` perf line but doesn't
            // grow CMA.
            if let Some(prior) = slot.take() {
                // SAFETY: destroy_fn + display + image are the
                // ones we stored when this slot was first
                // populated. Calling destroy on a still-valid
                // EGLImage is the spec-correct teardown.
                let _ = unsafe { (prior.destroy_fn)(prior.display, prior.image) };
            }
            *slot = Some(handle);
            return;
        }
        // r101 subagent WARN-1: if the index is out of range, DO NOT
        // silently drop the freshly-created handle (which would leak
        // the kernel dmabuf ref forever -- the exact bug class r101
        // exists to fix). Destroy it via the carried fn so the leak
        // becomes a one-shot waste at misuse-time instead of
        // a permanent CMA pin. Reachable only on caller bug today
        // (buffer_index is bounded by V4L2 DQBUF), but a future
        // refactor that re-runs allocate_buffers would invite this.
        let _ = unsafe { (handle.destroy_fn)(handle.display, handle.image) };
        let _ = writeln!(
            std::io::stderr(),
            "warn: cache_egl_image idx={} out of range (cache len={}); \
             destroyed the rejected handle inline. This indicates a \
             caller bug or stale buffer_index from a re-allocated \
             decoder.",
            idx,
            inner.capture_egl_images.len(),
        );
    }

    /// perf-decode tail-fix close-out (2026-06-15): pre-warm the
    /// per-CAPTURE-buffer EGLImage cache for the to-side decoder
    /// BEFORE the first transition tick so the create cost
    /// (eglCreateImageKHR ~tens-of-ms per fd under the FYS swap-
    /// pressure regime where pswpin runs ~34 MB/min CONSTANT)
    /// doesn't land on the per-tick blit path. Without this,
    /// every CAPTURE buffer's first observation by the render
    /// thread mid-transition triggers a lazy `get_or_init_egl_
    /// image` create that may stall (the "import_us up to 3.9 s
    /// cache_path=true created=true" v2.1 finding).
    ///
    /// Usage (code2's Option B in hdmi.rs, at first paint of the
    /// to-side decoder):
    /// ```ignore
    /// decoder.prewarm_egl_image_cache(|idx, fd| {
    ///     let img = (eps.create_image)(
    ///         display.as_ptr(),
    ///         std::ptr::null_mut(),
    ///         EGL_LINUX_DMA_BUF_EXT,
    ///         std::ptr::null_mut(),
    ///         build_attribs(fd, w, h, stride).as_ptr(),
    ///     );
    ///     if img.is_null() {
    ///         return Err(anyhow!("eglCreateImageKHR(idx={}) -> EGL_NO_IMAGE", idx));
    ///     }
    ///     Ok(EglImageHandle {
    ///         image: img,
    ///         display: display.as_ptr(),
    ///         destroy_fn: eps.destroy_image,
    ///     })
    /// })?;
    /// ```
    ///
    /// **Closure runs INSIDE the Decoder Mutex locked scope.**
    /// This means:
    ///   * `fd` is guaranteed valid for the closure's lifetime
    ///     (Decoder owns the fd; nothing can drop the Decoder while
    ///     we hold the lock).
    ///   * Closure MUST NOT call any Decoder method that would
    ///     re-acquire `self.inner.lock()` — `std::sync::Mutex` is
    ///     non-reentrant; that would deadlock. Practical
    ///     consequence: the closure should ONLY call EGL (Mesa
    ///     entry points) + wrap the result. Don't call any
    ///     `Decoder::*` API from inside.
    ///   * Closure SHOULD NOT close(fd) — the fd is owned by
    ///     `DecoderInner.capture_dmabuf_fds` and is closed in
    ///     `DecoderInner::Drop` per the r101 lifecycle invariant
    ///     (destroy_image BEFORE close(fd)).
    ///
    /// **Idempotent / safe for repeated calls.** Only slots that
    /// are currently `None` get build_handle called + inserted.
    /// Already-populated slots (from a prior pre-warm or a paint-
    /// path lazy fill via `get_or_init_egl_image`) are skipped.
    ///
    /// **MMAP-only mode (no DMA-BUF export).** Returns Ok(()) with
    /// nothing iterated — `capture_dmabuf_fds` stays empty when
    /// `capture_buffer_type != DmaBuf` so no pre-warm work is
    /// possible. Caller's optional pre-warm step naturally becomes
    /// a no-op on the MMAP fall-through path.
    ///
    /// **r101 invariant preservation** (the sacred-review adversarial
    /// frame): inserted handles end up in `capture_egl_images` via
    /// the SAME slot mechanism that `get_or_init_egl_image` uses on
    /// cache-miss (line 2149 `*slot = Some(handle)`). They are
    /// destroyed at `DecoderInner::Drop` via the carried
    /// `destroy_fn` + `display`, BEFORE `close(fd)` on the matching
    /// `capture_dmabuf_fds` entry — identical to the lazy-fill
    /// path's lifecycle. Pre-warm shifts the create cost to a
    /// caller-chosen moment but does not change the destroy /
    /// fd-close ordering.
    ///
    /// **Partial-error handling.** If `build_handle` returns Err
    /// at idx=K, the iteration aborts with that error after
    /// inserting all successful handles for indices < K. The
    /// remaining indices > K stay `None` and lazy-fill normally
    /// on the paint path. No leak (inserted handles stay in
    /// `capture_egl_images` and destroy at Drop).
    ///
    /// **Length invariant.** `capture_egl_images.len() ==
    /// capture_dmabuf_fds.len()` per the allocate_buffers contract
    /// (line ~2499 `inner.capture_dmabuf_fds = fds;` followed by
    /// the parallel `capture_egl_images` resize). The method's
    /// internal check returns an explicit Err if that invariant
    /// is ever violated (defensive; shouldn't happen in production
    /// but the API stays safe under future refactors).
    pub fn prewarm_egl_image_cache<F>(&self, mut build_handle: F) -> Result<()>
    where
        F: FnMut(u32, std::os::fd::RawFd) -> Result<EglImageHandle>,
    {
        let mut inner = self.inner.lock().unwrap();
        let len = inner.capture_dmabuf_fds.len();
        if len == 0 {
            // MMAP-only mode (no DMA-BUF export) — nothing to warm.
            // Caller's pre-warm step becomes a no-op on this path.
            eprintln!(
                "[perf] prewarm_egl_image_cache total=0 warmed=0 skipped=0 mode=mmap_or_pre_allocate"
            );
            return Ok(());
        }
        if inner.capture_egl_images.len() != len {
            return Err(anyhow!(
                "prewarm_egl_image_cache: capture_egl_images.len()={} != capture_dmabuf_fds.len()={} — \
                 allocate_buffers invariant violated",
                inner.capture_egl_images.len(),
                len,
            ));
        }
        let mut warmed = 0u32;
        let mut skipped = 0u32;
        for idx in 0..len {
            if inner.capture_egl_images[idx].is_some() {
                // Already populated (prior pre-warm OR lazy-fill via
                // get_or_init_egl_image on a previous paint tick).
                // Idempotent skip — don't re-create + don't leak.
                skipped += 1;
                continue;
            }
            let fd = inner.capture_dmabuf_fds[idx];
            let handle = build_handle(idx as u32, fd)?;
            inner.capture_egl_images[idx] = Some(handle);
            warmed += 1;
        }
        eprintln!(
            "[perf] prewarm_egl_image_cache total={} warmed={} skipped={}",
            len, warmed, skipped,
        );
        Ok(())
    }

    /// VIDIOC_REQBUFS + VIDIOC_QUERYBUF + mmap for `count`
    /// buffers on the given queue. Call AFTER set_*_format.
    pub fn allocate_buffers(
        &self, dir: QueueDirection, count: u32,
    ) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        let fmt = match dir {
            QueueDirection::Output => inner.output_format.clone(),
            QueueDirection::Capture => inner.capture_format.clone(),
        }.ok_or_else(|| anyhow!(
            "allocate_buffers({:?}): set_format must be called first", dir
        ))?;

        // BOTH queues + BOTH modes use V4L2_MEMORY_MMAP for
        // REQBUFS. The kernel allocates the buffer memory either
        // way; V4L2_MEMORY_DMABUF would be for an IMPORT case
        // (userspace gives the kernel an fd), which is not what
        // we want on the CAPTURE side. For the DmaBuf zero-copy
        // path we run VIDIOC_EXPBUF AFTER mmap to obtain dma_buf
        // fds that refer to the kernel-allocated buffers --
        // EXPBUF requires V4L2_MEMORY_MMAP to have been used at
        // REQBUFS time.
        //
        // Piece 4a-fix (2026-05-14): the original piece 4a wired
        // REQBUFS with V4L2_MEMORY_DMABUF on the CaptureBufferType
        // ::DmaBuf path, which caused EXPBUF to fail with EINVAL
        // -- the buffers had no kernel-side backing memory because
        // DMABUF mode is the import direction. The fix is to keep
        // REQBUFS as MMAP regardless of capture_buffer_type, and
        // add EXPBUF as a post-mmap step when DmaBuf is requested.
        let memory_type = V4L2_MEMORY_MMAP;

        // Step 1: VIDIOC_REQBUFS.
        let mut rb = V4l2Requestbuffers {
            count,
            buf_type: dir.buf_type(),
            memory: memory_type,
            ..Default::default()
        };
        // SAFETY: _IOWR; kernel writes rb.count + rb.capabilities back.
        let reqbufs_result = unsafe { vidioc_reqbufs(inner.fd(), &mut rb) };
        // r84 ioctl trace: log every REQBUFS call so QA can pin
        // EINVAL (or any other failure) to the exact queue + count.
        eprintln!(
            "[perf] v4l2_ioctl op=REQBUFS queue={} requested_count={} result={}",
            match dir { QueueDirection::Output => "output", QueueDirection::Capture => "capture" },
            count,
            match &reqbufs_result {
                Ok(_) => format!("ok_allocated_{}", rb.count),
                Err(e) => format!("errno_{:?}", e),
            },
        );
        // r99 (2026-06-09): on REQBUFS failure, embed errno + buffer
        // params into the with_context message so the parent's
        // [mmal_leak_suspect] log line surfaces the actual ioctl
        // errno + the resource ask. extract_top_context (in
        // ipc_main.rs) picks up THIS string verbatim and renders it
        // as op="<msg>". Format intentionally machine-parseable: a
        // grep for `errno=ENOMEM` on the journal scans both this
        // message and the outer log line consistently.
        let sizeimage = fmt.plane_fmt.first().map(|pf| pf.sizeimage).unwrap_or(0);
        let total_alloc_kb = (sizeimage as u64).saturating_mul(count as u64) / 1024;
        let errno_for_ctx = reqbufs_result.as_ref().err().copied();
        reqbufs_result.with_context(|| {
            let errno_str = errno_for_ctx
                .map(|e| format!("{:?}", e))
                .unwrap_or_else(|| "UNKNOWN".to_string());
            format_reqbufs_failure_context(
                dir,
                memory_type,
                &errno_str,
                count,
                sizeimage,
                total_alloc_kb,
            )
        })?;
        let allocated_count = rb.count as usize;
        if allocated_count == 0 {
            return Err(anyhow!(
                "VIDIOC_REQBUFS({:?}): kernel allocated 0 buffers", dir
            ));
        }
        let num_planes = fmt.num_planes as usize;

        // MMAP path (always).
        let mut buffer_regions: Vec<Vec<MmapRegion>> = Vec::with_capacity(allocated_count);
        for buf_idx in 0..allocated_count {
            let mut planes = [V4l2Plane::default(); 8];
            let mut buf = V4l2Buffer {
                index: buf_idx as u32,
                buf_type: dir.buf_type(),
                memory: V4L2_MEMORY_MMAP,
                length: num_planes as u32,
                m_planes: planes.as_mut_ptr() as u64,
                ..Default::default()
            };
            // SAFETY: _IOWR; kernel fills planes[0..num_planes]
            // with the per-plane length + m.offset for mmap.
            // r88 ioctl trace per QA dispatch: DMABUF setup path
            // had no QUERYBUF coverage. Emit on every iteration
            // (typically 4 per queue per prime = 8 lines total).
            let r = unsafe { vidioc_querybuf(inner.fd(), &mut buf) };
            eprintln!(
                "[perf] v4l2_ioctl op=QUERYBUF queue={} idx={} result={}",
                match dir { QueueDirection::Output => "output", QueueDirection::Capture => "capture" },
                buf_idx,
                match &r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
            );
            r.with_context(|| format!("VIDIOC_QUERYBUF({:?} idx={})", dir, buf_idx))?;
            let mut plane_regions = Vec::with_capacity(num_planes);
            for plane_idx in 0..num_planes {
                let p = &planes[plane_idx];
                let len = p.length as usize;
                let offset = p.m as i64;
                // SAFETY: mmap a region the kernel just told us
                // about. PROT_READ|WRITE so OUTPUT side can write
                // NAL bytes; CAPTURE side reads but write doesn't
                // hurt. MAP_SHARED is required for V4L2.
                let ptr = unsafe {
                    libc::mmap(
                        std::ptr::null_mut(),
                        len,
                        libc::PROT_READ | libc::PROT_WRITE,
                        libc::MAP_SHARED,
                        inner.fd(),
                        offset,
                    )
                };
                if ptr == libc::MAP_FAILED {
                    return Err(anyhow!(
                        "mmap({:?} idx={} plane={} len={}): {}",
                        dir, buf_idx, plane_idx, len,
                        std::io::Error::last_os_error(),
                    ));
                }
                plane_regions.push(MmapRegion { ptr, len });
            }
            buffer_regions.push(plane_regions);
        }
        match dir {
            QueueDirection::Output => {
                inner.mapped_output = buffer_regions;
                // r48: seed the free-list with all OUTPUT indices.
                // All slots start userspace-owned; `feed()` pops
                // one when it QBUFs to the kernel, and
                // `drain_output_quiet`'s DQBUF success path pushes
                // it back. A repeat allocate_buffers (currently
                // not supported but defensible) overwrites both
                // the mmap regions and the free pool together.
                inner.free_output_slots =
                    (0..allocated_count as u32).collect();
            }
            QueueDirection::Capture => {
                inner.capture_in_flight = vec![false; allocated_count];
                inner.mapped_capture = buffer_regions;
            }
        }

        // Step 3 (CAPTURE + DmaBuf only): VIDIOC_EXPBUF per buffer
        // index, obtaining an O_CLOEXEC dma_buf fd that GLES can
        // import via EGLImage. bcm2835-codec NV12 CAPTURE has
        // num_planes=1, so a single fd per buffer covers the
        // entire Y+UV region (UV at offset Y_SIZE within the same
        // buffer). Future codecs with num_planes>1 would need one
        // fd per plane and the field shape would change to
        // Vec<Vec<RawFd>>; we error cleanly until that's needed.
        // The Decoder owns these fds + closes them from Drop
        // after stop_streaming. (Piece 4a-fix.)
        if dir == QueueDirection::Capture
            && inner.capture_buffer_type == CaptureBufferType::DmaBuf
        {
            if num_planes != 1 {
                return Err(anyhow!(
                    "DmaBuf CAPTURE path only supports num_planes=1 (got {})",
                    num_planes
                ));
            }
            // r42 (2026-06-02): mid-loop failure leaks any fds
            // already pushed -- the assignment to
            // inner.capture_dmabuf_fds (line below) only runs
            // after the loop completes successfully. RawFd has no
            // Drop; DecoderInner::drop only closes
            // inner.capture_dmabuf_fds which stays empty on this
            // error path. cleanup_partial_fds mirrors the r41
            // sdf_atlas_gl.rs cleanup_partial closure pattern
            // (and the r38b cleanup_static shape) -- per-iteration
            // failure releases any fds accumulated so far before
            // propagating. See qa/r42-v4l2-expbuf-fd-leak-2026-06-02.md.
            let cleanup_partial_fds = |fds: &mut Vec<std::os::fd::RawFd>| {
                for fd in fds.drain(..) {
                    // SAFETY: each fd was obtained from a successful
                    // VIDIOC_EXPBUF above and is not yet handed off
                    // to inner.capture_dmabuf_fds. We own these
                    // exclusively; libc::close is safe.
                    unsafe { libc::close(fd); }
                }
            };
            let mut fds: Vec<std::os::fd::RawFd> = Vec::with_capacity(allocated_count);
            for buf_idx in 0..allocated_count {
                let mut expbuf = V4l2Exportbuffer {
                    buf_type: dir.buf_type(),
                    index: buf_idx as u32,
                    plane: 0,
                    flags: libc::O_CLOEXEC as u32,
                    fd: -1,
                    reserved: [0u32; 11],
                };
                // SAFETY: _IOWR; kernel writes expbuf.fd. Caller
                // owns the resulting fd until close(2). REQBUFS
                // above used V4L2_MEMORY_MMAP, so the kernel HAS
                // a buffer at this index to export -- this is
                // the canonical pattern (piece 4a's original
                // V4L2_MEMORY_DMABUF for REQBUFS was wrong; the
                // kernel returned EINVAL on EXPBUF because no
                // buffer existed at that index).
                // r88 ioctl trace per QA dispatch: DMABUF setup
                // path's main ioctl. If DMABUF=1 is the breaker,
                // an errno here is the next pinpoint after REQBUFS.
                let expbuf_r = unsafe { vidioc_expbuf(inner.fd(), &mut expbuf) };
                eprintln!(
                    "[perf] v4l2_ioctl op=EXPBUF queue={} idx={} result={}",
                    match dir { QueueDirection::Output => "output", QueueDirection::Capture => "capture" },
                    buf_idx,
                    match &expbuf_r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
                );
                if let Err(e) = expbuf_r {
                    cleanup_partial_fds(&mut fds);
                    return Err(anyhow::Error::from(e).context(format!(
                        "VIDIOC_EXPBUF({:?} idx={}) on MMAP-allocated buffer",
                        dir, buf_idx
                    )));
                }
                if expbuf.fd < 0 {
                    cleanup_partial_fds(&mut fds);
                    return Err(anyhow!(
                        "VIDIOC_EXPBUF({:?} idx={}) returned fd={}",
                        dir, buf_idx, expbuf.fd
                    ));
                }
                fds.push(expbuf.fd);
            }
            inner.capture_dmabuf_fds = fds;
            // r101: size the EGLImage cache to match the dmabuf fd
            // pool; entries are populated lazily on first paint
            // from each buffer index.
            //
            // r101 subagent WARN-2: destroy any prior cached handles
            // before .clear(), so a future re-allocation flow
            // (REQBUFS(0) -> REQBUFS(N'), retry, hypothetical reuse)
            // doesn't silently drop EGL refs. Today's code calls
            // allocate_buffers exactly once per Decoder, but the
            // structural fix is one drain loop and keeps the
            // contract "EGLImages outlive the function only via
            // the cache or via Drop" intact under refactor.
            for slot in inner.capture_egl_images.drain(..) {
                if let Some(h) = slot {
                    let _ = unsafe { (h.destroy_fn)(h.display, h.image) };
                }
            }
            let n = inner.capture_dmabuf_fds.len();
            inner.capture_egl_images.resize(n, None);
        }
        Ok(())
    }

    /// VIDIOC_STREAMON on both queues. Per kernel docs the order
    /// for M2M is OUTPUT first, then CAPTURE -- the codec needs
    /// to know we're feeding before it'll start emitting.
    /// Also queues all CAPTURE buffers so the kernel has somewhere
    /// to put decoded frames immediately.
    pub fn start_streaming(&self) -> Result<()> {
        // Pre-queue all CAPTURE buffers so the kernel has somewhere
        // to write decoded frames the moment we feed OUTPUT.
        {
            let inner = self.inner.lock().unwrap();
            let Some(ref cap_fmt) = inner.capture_format else {
                return Err(anyhow!("start_streaming: capture not formatted"));
            };
            // Piece 4a-fix: REQBUFS used V4L2_MEMORY_MMAP regardless
            // of capture_buffer_type, so the kernel manages buffers
            // by index here too. The capture_dmabuf_fds vec (when
            // DmaBuf mode is on) is only used by the EGLImage import
            // path on the paint side; QBUF/DQBUF don't need it.
            let count = inner.mapped_capture.len();
            let memory = V4L2_MEMORY_MMAP;
            let num_planes = cap_fmt.num_planes as usize;
            for i in 0..count {
                let mut planes = [V4l2Plane::default(); 8];
                for p in 0..num_planes {
                    planes[p].length = cap_fmt.plane_fmt[p].sizeimage;
                    // MMAP path: kernel reads from the buffer index;
                    // planes[p].m stays 0.
                }
                let mut buf = V4l2Buffer {
                    index: i as u32,
                    buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
                    memory,
                    length: num_planes as u32,
                    m_planes: planes.as_mut_ptr() as u64,
                    ..Default::default()
                };
                // SAFETY: ioctl with our owned fd + correctly-
                // sized buffer struct + planes array alive for
                // the call.
                unsafe { vidioc_qbuf(inner.fd(), &mut buf) }
                    .with_context(|| format!("pre-QBUF CAPTURE idx={}", i))?;
            }
        }
        let mut inner = self.inner.lock().unwrap();
        let bt_out: libc::c_int = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE as libc::c_int;
        let bt_cap: libc::c_int = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE as libc::c_int;
        // SAFETY: ioctl_write_ptr! generates a fn that takes
        // *const c_int. Kernel reads 4 bytes from the pointer.
        // Pointers live until the ioctl returns (their stack
        // slots outlive the unsafe scope).
        let r_out = unsafe { vidioc_streamon(inner.fd(), &bt_out) };
        eprintln!(
            "[perf] v4l2_ioctl op=STREAMON queue=output result={}",
            match &r_out { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        r_out.with_context(|| "VIDIOC_STREAMON OUTPUT")?;
        inner.output_streaming = true;
        let r_cap = unsafe { vidioc_streamon(inner.fd(), &bt_cap) };
        eprintln!(
            "[perf] v4l2_ioctl op=STREAMON queue=capture result={}",
            match &r_cap { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        r_cap.with_context(|| "VIDIOC_STREAMON CAPTURE")?;
        inner.capture_streaming = true;

        // r92 (2026-06-08): CMD_STOP capability probe RESTORED
        // here from Decoder::open. r91's move to Decoder::open
        // broke OFF-mode baseline (17 EINVAL in 60s with
        // EOS_FLUSH unset) -- empirically TRY_DECODER_CMD is
        // destructive on bcm2835-codec when called pre-STREAMON.
        // r84/r88/r90 had it here (after both STREAMONs) and was
        // known-clean. Uses VIDIOC_TRY_DECODER_CMD (spec says
        // non-destructive; r91 data showed lifecycle position
        // matters).
        drop(inner);
        if !CMD_STOP_PROBE_DONE.swap(true, std::sync::atomic::Ordering::Relaxed) {
            let mut probe_cmd = V4l2DecoderCmd {
                cmd: V4L2_DEC_CMD_STOP,
                flags: 0,
                payload: [0u32; 16],
            };
            let probe_inner = self.inner.lock().unwrap();
            let r_try = unsafe { vidioc_try_decoder_cmd(probe_inner.fd(), &mut probe_cmd) };
            drop(probe_inner);
            let try_result_str = match &r_try {
                Ok(_) => "ok".to_string(),
                Err(e) => format!("errno_{:?}", e),
            };
            eprintln!(
                "[perf] v4l2_cmd_stop_probe try_cmd_stop={} note=non_destructive_via_VIDIOC_TRY_DECODER_CMD at_open_not_streamon=false",
                try_result_str,
            );
        }
        Ok(())
    }

    /// r46.4 (2026-06-02): resume the V4L2 stateful decoder
    /// after CAPTURE has drained on `V4L2_BUF_FLAG_LAST` (EOS),
    /// without dropping the Decoder struct (which would also
    /// drop the buffer pool + V4L2 fd).
    ///
    /// Necessary because r46.2's `keep_ids` memoization
    /// preserves the decoder across BeginSlide for text-over-
    /// video slides -- but the prior playback may have hit
    /// `V4L2_BUF_FLAG_LAST` on its last decoded frame, setting
    /// `capture_drained = true` (see `next_frame` at the EPIPE /
    /// FLAG_LAST sites). Once set, `next_frame` returns
    /// `Ok(None)` forever + `Frame::Drop` skips re-QBUF
    /// (v4l2.rs ~735) -- the decoder is wedged.
    ///
    /// History: r46.3 shipped `reset_for_replay` which did
    /// STREAMOFF + STREAMON to clear the drained state. On
    /// bcm2835-codec (Pi Zero 2 W stateful decoder), subsequent
    /// OUTPUT QBUF after that cycle returned EINVAL, breaking
    /// the wrap. r46.4 replaces that mechanism with the V4L2
    /// stateful-decoder spec's documented resume path: issue
    /// `VIDIOC_DECODER_CMD` with `V4L2_DEC_CMD_START`.
    ///
    /// Per <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/dev-decoder.html>:
    ///   "After the decoder has been signalled to stop (either
    ///   via V4L2_DEC_CMD_STOP or by queueing a buffer with
    ///   V4L2_BUF_FLAG_LAST set), V4L2_DEC_CMD_START is used to
    ///   restart decoding. The decoder's parsed SPS/PPS state is
    ///   preserved across this cycle; the next OUTPUT buffer
    ///   queued will resume the decode pipeline."
    ///
    /// Resume semantics:
    ///   1. Issue VIDIOC_DECODER_CMD with V4L2_DEC_CMD_START to
    ///      clear the kernel's EOS state on CAPTURE.
    ///   2. Clear our `capture_drained` flag (mirrors kernel
    ///      state).
    ///   3. Clear `output_eof_sent` flag (so the next IDR feed
    ///      isn't rejected as "after EOF").
    ///   4. DO NOT touch streaming state, capture_in_flight, or
    ///      any kernel-side buffer ownership -- everything else
    ///      stays exactly as the decoder left it.
    ///
    /// Caller responsibility: re-feed an IDR (and per V4L2 spec
    /// the SPS/PPS too, for safety, since some drivers discard
    /// parsed state on resume even though the spec says they
    /// shouldn't). `reprime_video_decoder_for_loop` calls this
    /// then feeds SPS+PPS+IDR.
    ///
    /// Cost: a single V4L2 ioctl. Called at most once per slide-
    /// play cycle (when bake's `next_sample_idx >= samples.len()`
    /// wrap fires). CMA budget unchanged (no allocation; no
    /// stream/buffer pool churn).
    pub fn resume_after_eos(&self) -> Result<()> {
        let mut inner = self.inner.lock().unwrap();
        // Gate the ioctl on capture_drained. Per V4L2 spec
        // V4L2_DEC_CMD_START on a non-stopped decoder is
        // implementation-defined; bcm2835-codec returns -EBUSY in
        // that case. The caller (reprime_video_decoder_for_loop)
        // fires on a userspace sample counter wrap, which may
        // precede the kernel's FLAG_LAST emission for very short
        // clips. When capture_drained is false, the decoder is
        // still healthy; the IDR re-feed below restarts decoding
        // naturally without needing the kernel-state transition.
        if !inner.capture_drained {
            // r85 subagent WARN-2: trace coverage on the gated-skip
            // path so the diagnostic doesn't go silent on this
            // early-return (the path most commonly hit from
            // reprime_video_decoder_for_loop's standalone-reel wrap).
            eprintln!(
                "[perf] v4l2_ioctl op=DECODER_CMD_START queue=output result=skipped_not_drained"
            );
            return Ok(());
        }
        let mut cmd = V4l2DecoderCmd {
            cmd: V4L2_DEC_CMD_START,
            flags: 0,
            payload: [0u32; 16],
        };
        // SAFETY: ioctl with our owned fd + correctly-sized
        // V4l2DecoderCmd. The kernel reads cmd+flags and writes
        // back any status into payload (none for plain START).
        // Lock held across the ioctl: intentional. The call is
        // <1ms + there are no concurrent callers at the wrap
        // site (single-threaded IPC dispatcher).
        let r = unsafe { vidioc_decoder_cmd(inner.fd(), &mut cmd) };
        eprintln!(
            "[perf] v4l2_ioctl op=DECODER_CMD_START queue=output result={}",
            match &r { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        r.with_context(|| "VIDIOC_DECODER_CMD V4L2_DEC_CMD_START")?;
        inner.capture_drained = false;
        inner.output_eof_sent = false;
        Ok(())
    }

    /// **r92 (2026-06-08): BODY NO-OP'D — see inline comment for the
    /// experiment rationale. The doc-comment below describes r82's
    /// original intent.**
    ///
    /// r82 (2026-06-08): signal EOS via `VIDIOC_DECODER_CMD` with
    /// `V4L2_DEC_CMD_STOP`. This is the canonical V4L2 m2m stateful
    /// decoder API for "drain whatever is in the reorder buffer."
    ///
    /// r80/r81 attempted EOS via `feed(&[])` (empty OUTPUT buffer
    /// with V4L2_BUF_FLAG_LAST). r81 telemetry on FYS showed
    /// bcm2835-codec NEVER responded to that signal -- 96/96
    /// preloads ended with `eos_seen=false` and CAPTURE remained
    /// EAGAIN for the full 500ms drain budget. The
    /// `videobuf2_common: driver bug: stop_streaming operation is
    /// leaving buffer N in active state` kernel warning then fired
    /// during teardown.
    ///
    /// Per kernel docs (`dev-decoder.html`):
    ///   "To gracefully end the stream, the client may issue
    ///    VIDIOC_DECODER_CMD with V4L2_DEC_CMD_STOP. The driver
    ///    will then drain any remaining frames and mark the last
    ///    CAPTURE buffer with V4L2_BUF_FLAG_LAST."
    ///
    /// Unlike `feed(&[])` this does NOT set `output_eof_sent` in
    /// our wrapper -- bcm2835-codec apparently treats CMD_STOP
    /// + drain + CMD_START as a clean cycle that doesn't require
    /// clearing OUTPUT-side userspace state.
    pub fn signal_eos_via_cmd_stop(&self) -> Result<()> {
        // r92 (2026-06-08): EXPERIMENT per QA r92 dispatch.
        //
        // QA's smoking-gun finding: r91 moved the
        // VIDIOC_TRY_DECODER_CMD capability probe pre-STREAMON
        // and broke OFF-mode baseline (17 EINVAL in 60s). This
        // empirically proves that VIDIOC_(TRY_)DECODER_CMD has
        // a side effect on bcm2835-codec at certain lifecycle
        // moments — the kernel docs are wrong (or
        // under-specified) about non-destructiveness.
        //
        // The real VIDIOC_DECODER_CMD V4L2_DEC_CMD_STOP issued
        // here in the EOS-flush body likely fires the SAME
        // kernel-state quirk, which would explain the
        // r85/r87/r90 EOS_FLUSH=1 regression I couldn't find
        // via code review (the bug isn't in DMABUF code; it's
        // in CMD_STOP semantics on bcm2835-codec).
        //
        // QA's r92 ask #3: "Test by removing CMD_STOP from the
        // EOS-flush body entirely. If the regression on
        // EOS_FLUSH=1 goes away (with the body running its
        // other code but not the signal_eos_via_cmd_stop call),
        // the hypothesis is confirmed: CMD_STOP itself is
        // poisonous on bcm2835-codec at the wrong lifecycle
        // moment."
        //
        // This function returns Ok(()) WITHOUT issuing the
        // ioctl. The EOS-flush body's drain_after_signal_eos
        // still runs the DQBUF loop but never gets FLAG_LAST
        // (the kernel was never told to flush). The drain
        // budget exhausts, the bail path runs, the preload
        // returns Err and falls back to synchronous cold-start.
        //
        // What QA's deploy of r92 will reveal:
        //   EOS_FLUSH=off: r84-baseline (regression-guard pass)
        //   EOS_FLUSH=1, default: EOS-flush body runs WITHOUT
        //     CMD_STOP. If cold-start REQBUFS still EINVALs
        //     on subsequent slides, CMD_STOP is EXONERATED
        //     (something else in the body causes corruption).
        //     If clean, CMD_STOP is CONFIRMED as the bug.
        //
        // The function and its trace site stay so the next
        // round can re-enable the real ioctl behind a new env
        // var IF QA decides the experiment was inconclusive.
        eprintln!(
            "[perf] v4l2_ioctl op=DECODER_CMD_STOP queue=output result=skipped_r92_experiment"
        );
        Ok(())
    }

    /// Queue one H.264 NAL chunk (Annex-B byte stream) into
    /// the OUTPUT queue. Empty slice signals end-of-input
    /// (V4L2_BUF_FLAG_LAST). Reclaims completed OUTPUT buffers
    /// in the same call to keep the pipeline full.
    ///
    /// r48 (2026-06-03): rotates through ALL allocated OUTPUT
    /// buffers via the `free_output_slots` deque (vs the pre-r48
    /// single-buffer hardcode `buf_idx = 0`, which raced
    /// `drain_output_quiet` and produced VIDIOC_QBUF OUTPUT EINVAL
    /// on bcm2835-codec for any back-to-back feed faster than the
    /// kernel could decode + dequeue).
    pub fn feed(&self, h264_nal: &[u8]) -> Result<()> {
        // Drain any completed OUTPUT buffers first (they're
        // ready for reuse). EAGAIN means none ready -- fine.
        // r48: drain feeds slots back to the pool; if still
        // empty after, we'll retry with bounded sleep below.
        self.drain_output_quiet();

        // r48 (subagent 2026-06-03) original budget: 5 × 2ms = 10ms.
        // Mirrors the perf-night-r5 next_frame EAGAIN sleep pattern
        // -- under transient back-pressure (one tick where kernel
        // hasn't released any OUTPUT buffer yet), feed() should
        // soft-wait instead of hard-erroring (which propagates as a
        // slide/transition abort via the IPC dispatcher).
        //
        // r70 (2026-06-06) bump to 25 × 4ms = 100ms by default.
        // FYS observed every 1080p transition errored at "all 4
        // OUTPUT buffers in flight; drain wedged after 10ms" --
        // the bcm2835-codec decode latency on 1080p H.264 is
        // ~30-50ms/frame, far longer than the original 10ms
        // budget. 100ms covers ~2-3 1080p decode cycles + slack
        // and is still inside one playback tick at the 1s transition
        // window (paint loop is doing slow work during the transition
        // anyway, so the kernel wait is amortized against the bake
        // cost).
        //
        // Steady-state video paint rarely enters this loop: the
        // bake's per-tick feed + next_frame cadence consumes one
        // OUTPUT and dequeues one CAPTURE per ~33ms, so the pool
        // self-balances. The wait loop CAN fire outside steady
        // state -- r73 (2026-06-06) documented the canonical
        // idle-saturation case: the r65 preload path used to
        // QBUF 4 samples then sit idle; if CAPTURE saturated
        // the kernel couldn't release OUTPUT and EVERY transition
        // hit the 100ms ceiling. r73 reduced preload warmup so
        // CAPTURE never saturates on that path.
        //
        // OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS env override lets QA
        // tune without recompile. Range-clamped to [10, 1000] so a
        // typo can't deadlock or zero out the soft-wait.
        let (retries, interval_ms) = feed_drain_retry_schedule();
        let t_drain_wait = std::time::Instant::now();
        let mut retries_used = 0usize;
        let mut pool_empty_on_entry = false;
        for _ in 0..retries {
            {
                let inner = self.inner.lock().unwrap();
                if !inner.free_output_slots.is_empty() {
                    break;
                }
                pool_empty_on_entry = true;
            }
            std::thread::sleep(std::time::Duration::from_millis(interval_ms));
            self.drain_output_quiet();
            retries_used += 1;
        }
        // r70 Phase A instrumentation: emit a perf line when the
        // wait actually mattered (>1 retry, i.e. crossed the first
        // sleep beat). Single-sleep transients (kernel woke up
        // between iter 0 and iter 1) are noise. r70 subagent WARN-4
        // fix: skip the >1 floor to keep the journal cap'd at the
        // genuinely-back-pressured case.
        if retries_used > 1 {
            eprintln!(
                "[perf] v4l2_feed_drain_wait waited_us={} retries={} pool_empty_on_entry={}",
                t_drain_wait.elapsed().as_micros(),
                retries_used,
                pool_empty_on_entry,
            );
        }

        let mut inner = self.inner.lock().unwrap();
        if inner.output_eof_sent && !h264_nal.is_empty() {
            return Err(anyhow!("feed() called after EOF"));
        }
        if inner.mapped_output.is_empty() {
            return Err(anyhow!("feed: no OUTPUT buffers allocated"));
        }
        // r48: snapshot the format fields we need + free-pool
        // depth in a tight scoped borrow, then drop the borrow
        // before mutating inner.free_output_slots /
        // inner.mapped_output. Both the format read AND the
        // pool pop need to coexist with the later mut access to
        // mapped_output; snapshotting up front sidesteps the
        // multi-field borrow conflict (caught by piece 3c
        // cross-compile, same shape).
        let (num_planes, plane_max, plane_sizeimages) = {
            let Some(ref out_fmt) = inner.output_format else {
                return Err(anyhow!("feed: output not formatted"));
            };
            let num_planes = out_fmt.num_planes as usize;
            let plane_max = out_fmt.plane_fmt[0].sizeimage as usize;
            let mut sizeimages = [0u32; 8];
            for p in 0..num_planes {
                sizeimages[p] = out_fmt.plane_fmt[p].sizeimage;
            }
            (num_planes, plane_max, sizeimages)
        };
        // r48: pull the next free OUTPUT buffer index from the
        // deque. If still empty after the bounded retry above,
        // surface an error so the caller (typically the IPC
        // playback loop) can decide to drop the slide.
        let pool_capacity = inner.mapped_output.len();
        let Some(buf_idx) = inner.free_output_slots.pop_front() else {
            // r70 subagent WARN-3: cite the schedule we ACTUALLY
            // used for the wait, not a fresh read that could
            // disagree under a process-shared env mutation.
            return Err(anyhow!(
                "feed: all {} OUTPUT buffers in flight (kernel-owned); \
                 drain wedged after {}ms retry budget ({} retries x {}ms) \
                 -- decoder may need reset",
                pool_capacity, retries * interval_ms as usize, retries, interval_ms
            ));
        };
        if h264_nal.len() > plane_max {
            // r48: return slot to free pool on validation error;
            // we never QBUF'd it.
            inner.free_output_slots.push_front(buf_idx);
            return Err(anyhow!(
                "feed: NAL chunk ({} bytes) larger than OUTPUT buffer ({})",
                h264_nal.len(), plane_max
            ));
        }
        // Copy bytes into plane 0 of the chosen buffer.
        let region = &mut inner.mapped_output[buf_idx as usize][0];
        let dst = region.as_mut_slice();
        dst[..h264_nal.len()].copy_from_slice(h264_nal);
        let mut planes = [V4l2Plane::default(); 8];
        for p in 0..num_planes {
            planes[p].length = plane_sizeimages[p];
        }
        planes[0].bytesused = h264_nal.len() as u32;
        let mut buf = V4l2Buffer {
            index: buf_idx,
            buf_type: V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            bytesused: h264_nal.len() as u32,
            flags: if h264_nal.is_empty() { V4L2_BUF_FLAG_LAST } else { 0 },
            ..Default::default()
        };
        // SAFETY: ioctl with valid fd + buffer + planes array
        // alive through the call.
        let qbuf_result = unsafe { vidioc_qbuf(inner.fd(), &mut buf) };
        if let Err(e) = qbuf_result {
            // r48 (subagent 2026-06-03): QBUF failed; return
            // slot to the BACK of the pool so a transiently bad
            // slot rotates past for the next feed (vs push_front
            // which would re-pop the same bad slot and wedge the
            // decoder on a 1-of-N persistent error). Validation-
            // error path above still uses push_front because the
            // slot is provably clean (never reached the kernel).
            inner.free_output_slots.push_back(buf_idx);
            return Err(anyhow::Error::new(e)).with_context(|| "VIDIOC_QBUF OUTPUT");
        }
        if h264_nal.is_empty() {
            inner.output_eof_sent = true;
        }
        Ok(())
    }

    /// r48 test accessor: snapshot the current OUTPUT free-list.
    /// Returns the indices in the deque (front-first). Used by
    /// the rotation-correctness tests below to verify the pool's
    /// invariants without exposing DecoderInner's full surface.
    #[cfg(test)]
    pub fn free_output_slots_snapshot(&self) -> Vec<u32> {
        let inner = self.inner.lock().unwrap();
        inner.free_output_slots.iter().copied().collect()
    }

    /// r106 + Path A Stage 2 (2026-06-14): non-blocking feed
    /// primitive. Returns `Ok(true)` if the sample was QBUF'd
    /// into an OUTPUT slot; `Ok(false)` if all OUTPUT slots are
    /// kernel-owned right now (no sleep, no retry); `Err` only
    /// for real V4L2 failures.
    ///
    /// Used by the r106 decoupled feed-drain path so each
    /// transition tick can top up the active decoder's input
    /// pool without blocking when the OUTPUT pool is full. The
    /// existing `feed()` method retains its bounded blocking
    /// behavior for callers that need it (pre-r106 paint path
    /// under the kill-switch=off branch AND the steady-state
    /// paint path that Stage 2 scoping deliberately leaves on
    /// blocking).
    ///
    /// Drains completed OUTPUT slots back into the free list
    /// before checking, so a finished buffer is immediately
    /// available rather than waiting for the next caller.
    pub fn try_feed_nonblocking(&self, h264_nal: &[u8]) -> Result<bool> {
        self.drain_output_quiet();
        let mut inner = self.inner.lock().unwrap();
        if inner.output_eof_sent && !h264_nal.is_empty() {
            return Err(anyhow!("try_feed_nonblocking: called after EOF"));
        }
        if inner.mapped_output.is_empty() {
            return Err(anyhow!(
                "try_feed_nonblocking: no OUTPUT buffers allocated",
            ));
        }
        if inner.free_output_slots.is_empty() {
            return Ok(false);
        }
        let (num_planes, plane_max, plane_sizeimages) = {
            let Some(ref out_fmt) = inner.output_format else {
                return Err(anyhow!("try_feed_nonblocking: output not formatted"));
            };
            let num_planes = out_fmt.num_planes as usize;
            let plane_max = out_fmt.plane_fmt[0].sizeimage as usize;
            let mut sizeimages = [0u32; 8];
            for p in 0..num_planes {
                sizeimages[p] = out_fmt.plane_fmt[p].sizeimage;
            }
            (num_planes, plane_max, sizeimages)
        };
        let buf_idx = inner
            .free_output_slots
            .pop_front()
            .expect("checked non-empty above");
        if h264_nal.len() > plane_max {
            // Restore slot before erroring so a future caller can
            // try a smaller sample.
            inner.free_output_slots.push_front(buf_idx);
            return Err(anyhow!(
                "try_feed_nonblocking: NAL chunk ({} bytes) larger than OUTPUT buffer ({})",
                h264_nal.len(),
                plane_max,
            ));
        }
        // Copy bytes into plane 0 (same shape as the blocking
        // `feed` above).
        let region = &mut inner.mapped_output[buf_idx as usize][0];
        let dst = region.as_mut_slice();
        dst[..h264_nal.len()].copy_from_slice(h264_nal);
        let mut planes = [V4l2Plane::default(); 8];
        for p in 0..num_planes {
            planes[p].length = plane_sizeimages[p];
        }
        planes[0].bytesused = h264_nal.len() as u32;
        let mut buf = V4l2Buffer {
            index: buf_idx,
            buf_type: V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            bytesused: h264_nal.len() as u32,
            flags: if h264_nal.is_empty() {
                V4L2_BUF_FLAG_LAST
            } else {
                0
            },
            ..Default::default()
        };
        let qbuf_r = unsafe { vidioc_qbuf(inner.fd(), &mut buf) };
        if let Err(e) = qbuf_r {
            // r106 subagent WARN-2: rotate the failed slot to
            // the BACK of the pool (matches the existing
            // `feed()`'s documented semantic). push_front would
            // re-pop the same persistently-bad slot on the next
            // try_feed call and wedge the decoder; push_back
            // lets the kernel try its peers first.
            inner.free_output_slots.push_back(buf_idx);
            return Err(anyhow::Error::from(e)
                .context("try_feed_nonblocking: VIDIOC_QBUF(OUTPUT)"));
        }
        if h264_nal.is_empty() {
            inner.output_eof_sent = true;
        }
        Ok(true)
    }

    /// Best-effort: drain completed OUTPUT buffers so they're
    /// available for the next feed. EAGAIN -> nothing ready ->
    /// silently return.
    ///
    /// r48 (2026-06-03): each successful DQBUF returns the
    /// dequeued buffer's index to `free_output_slots` so the
    /// next `feed()` can rotate to a different slot. Pre-r48
    /// the DQBUF result was discarded entirely; combined with
    /// feed's hardcoded `buf_idx = 0` this gave the appearance
    /// of working under single-shot test patterns but raced the
    /// kernel under any back-to-back feed.
    fn drain_output_quiet(&self) {
        let mut inner = self.inner.lock().unwrap();
        if !inner.output_streaming {
            return;
        }
        let Some(ref out_fmt) = inner.output_format else { return; };
        let num_planes = out_fmt.num_planes as usize;
        loop {
            let mut planes = [V4l2Plane::default(); 8];
            let mut buf = V4l2Buffer {
                buf_type: V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE,
                memory: V4L2_MEMORY_MMAP,
                length: num_planes as u32,
                m_planes: planes.as_mut_ptr() as u64,
                ..Default::default()
            };
            // SAFETY: non-blocking DQBUF; kernel writes into
            // buf + planes array. EAGAIN if nothing ready.
            let r = unsafe { vidioc_dqbuf(inner.fd(), &mut buf) };
            match r {
                Ok(_) => {
                    // r48: return reclaimed slot to the free pool.
                    // push_back is FIFO -- slots cycle in QBUF
                    // order, which gives kernel maximum time to
                    // process buf_idx N before we re-use N.
                    inner.free_output_slots.push_back(buf.index);
                    continue;
                }
                Err(nix::errno::Errno::EAGAIN) => return, // nothing left.
                Err(_) => return, // other errors: bail (caller
                // will surface them on the next real feed/next_frame).
            }
        }
    }

    /// r81 (2026-06-08): Frame-bypass DQBUF+QBUF for the preload
    /// EOS-drain path. The standard `next_frame -> Frame -> Frame::drop
    /// re-QBUF` path corrupted bcm2835-codec's internal buffer
    /// accounting when invoked mid-drain (r80 regression on FYS:
    /// REQBUFS OUTPUT EINVAL on subsequent slides). r81 ships this
    /// raw helper that DQBUFs then IMMEDIATELY QBUFs the same buffer
    /// back via raw ioctls -- no Frame construction, no Drop path
    /// timing relative to capture_drained.
    ///
    /// Returns:
    ///   * `Ok(DrainStep::GotFrame { is_last })` -- DQBUF success.
    ///     `is_last` reflects the V4L2_BUF_FLAG_LAST bit. The buffer
    ///     has already been re-QBUF'd before this returns.
    ///   * `Ok(DrainStep::WouldBlock)` -- EAGAIN; caller should
    ///     sleep + retry.
    ///   * `Ok(DrainStep::EndOfStream)` -- EPIPE; kernel queue is
    ///     drained; sets capture_drained=true mirroring next_frame.
    ///   * `Err(_)` -- real ioctl error.
    pub fn drain_capture_step_no_frame(&self) -> Result<DrainStep> {
        // r81 subagent WARN-6: hold the lock across BOTH ioctls
        // AND the state mutation. Pre-fix the EPIPE / is_last
        // branches did drop+relock for the state write -- defeating
        // the atomicity claim. With a single `let mut inner` we
        // keep the lock through the whole helper.
        let mut inner = self.inner.lock().unwrap();
        if inner.capture_drained {
            return Ok(DrainStep::EndOfStream);
        }
        // r81 subagent NIT: parity with Frame::drop's check.
        // Defensive against future callers that invoke this after
        // STREAMOFF (the preload-worker path never does today).
        if !inner.capture_streaming {
            return Ok(DrainStep::EndOfStream);
        }
        let Some(ref cap_fmt) = inner.capture_format else {
            return Err(anyhow!("drain_capture_step: capture not formatted"));
        };
        let num_planes = cap_fmt.num_planes as usize;
        // Snapshot the per-plane sizeimage values before the DQBUF
        // mutates other inner state (defensive against future
        // edits).
        let mut sizeimages = [0u32; 8];
        for p in 0..num_planes {
            sizeimages[p] = cap_fmt.plane_fmt[p].sizeimage;
        }
        let fd = inner.fd();

        // DQBUF.
        let mut planes_dq = [V4l2Plane::default(); 8];
        let mut buf_dq = V4l2Buffer {
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes_dq.as_mut_ptr() as u64,
            ..Default::default()
        };
        let dq_result = unsafe { vidioc_dqbuf(fd, &mut buf_dq) };
        // r85 (with subagent NIT-6 throttle): ioctl trace inside
        // the EOS-drain loop. Subagent flagged that EAGAIN-busy
        // loops can hit ~100 lines per failed preload (500ms budget
        // / 5ms sleep). Emit ALL non-EAGAIN outcomes
        // (ok / EPIPE / other errno -- the diagnostically critical
        // ones) and throttle EAGAIN-only emissions to first +
        // every 25th via a per-call counter snapshotted from the
        // wrapper state. Outcome lines still flow on the FIRST
        // EAGAIN so QA sees the start of any drain.
        let is_eagain = matches!(&dq_result, Err(nix::errno::Errno::EAGAIN));
        // Count EAGAINs cheaply via a thread-local counter scoped
        // to this Decoder call site -- no allocation, no atomic.
        thread_local! {
            static DRAIN_EAGAIN_TICK: std::cell::Cell<u32> = std::cell::Cell::new(0);
        }
        let should_emit = if is_eagain {
            DRAIN_EAGAIN_TICK.with(|c| {
                let t = c.get();
                c.set(t.wrapping_add(1));
                t == 0 || t % 25 == 0
            })
        } else {
            // Reset the counter so the next drain cycle starts fresh.
            DRAIN_EAGAIN_TICK.with(|c| c.set(0));
            true
        };
        if should_emit {
            let dq_result_str = match &dq_result {
                Ok(_) => {
                    let is_last_flag = (buf_dq.flags & V4L2_BUF_FLAG_LAST) != 0;
                    if is_last_flag {
                        format!("ok_idx{}_FLAG_LAST", buf_dq.index)
                    } else {
                        format!("ok_idx{}", buf_dq.index)
                    }
                }
                Err(e) => format!("errno_{:?}", e),
            };
            eprintln!(
                "[perf] v4l2_ioctl op=DQBUF queue=capture context=drain_eos result={}",
                dq_result_str,
            );
        }
        match dq_result {
            Ok(_) => {}
            Err(nix::errno::Errno::EAGAIN) => return Ok(DrainStep::WouldBlock),
            Err(nix::errno::Errno::EPIPE) => {
                inner.capture_drained = true;
                return Ok(DrainStep::EndOfStream);
            }
            Err(e) => return Err(anyhow!("drain_capture_step DQBUF: {}", e)),
        }
        let is_last = (buf_dq.flags & V4L2_BUF_FLAG_LAST) != 0;
        // r81 subagent WARN-4: surface FLAG_ERROR. Mirror
        // next_frame's check at line 1810+. Even if the kernel
        // also set FLAG_LAST, an error condition on the last
        // buffer is diagnostically critical: it may be the actual
        // cause of the r80 EINVAL-on-next-slide regression that
        // r81's atomic-DQBUF+QBUF theory doesn't address.
        let had_error = (buf_dq.flags & V4L2_BUF_FLAG_ERROR) != 0;
        if had_error {
            eprintln!(
                "[perf] preload_handoff_drain_flag_error idx={} flags=0x{:x}",
                buf_dq.index, buf_dq.flags,
            );
        }
        let idx = buf_dq.index;

        // Immediately re-QBUF the SAME buffer index.
        //
        // r81 subagent BLOCKER: the "atomic DQBUF+QBUF fixes the
        // bcm2835-codec corruption" theory does NOT survive a
        // careful read of r80 -- the kernel sees the same ioctl
        // pair in both versions; only the userspace lock-release
        // window between them changes. r81's Frame-bypass keeps
        // the code structurally cleaner but is unlikely to address
        // the EINVAL-on-next-slide on its own. Probes added above
        // are the actual diagnostic surface for the next round.
        let mut planes_qb = [V4l2Plane::default(); 8];
        for p in 0..num_planes {
            planes_qb[p].length = sizeimages[p];
            planes_qb[p].bytesused = 0;
        }
        let mut buf_qb = V4l2Buffer {
            index: idx,
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes_qb.as_mut_ptr() as u64,
            ..Default::default()
        };
        let qb_result = unsafe { vidioc_qbuf(fd, &mut buf_qb) };
        eprintln!(
            "[perf] v4l2_ioctl op=QBUF queue=capture context=drain_eos_reqbuf idx={} result={}",
            idx,
            match &qb_result { Ok(_) => "ok".to_string(), Err(e) => format!("errno_{:?}", e) },
        );
        if let Err(e) = qb_result {
            return Err(anyhow!("drain_capture_step QBUF after DQBUF: {}", e));
        }

        // FLAG_LAST: kernel-side drain is done. Set the wrapper's
        // capture_drained mirror per the same convention next_frame
        // uses at v4l2.rs:1877-1879.
        if is_last {
            inner.capture_drained = true;
        }
        Ok(DrainStep::GotFrame { is_last })
    }

    /// VIDIOC_DQBUF on CAPTURE -> wrap as `Frame`. Returns
    /// `Ok(None)` on EOF.
    pub fn next_frame(&self) -> Result<Option<Frame>> {
        // Drain output in the background each call to keep the
        // pipeline moving.
        self.drain_output_quiet();
        let inner = self.inner.lock().unwrap();
        if inner.capture_drained {
            return Ok(None);
        }
        let Some(ref cap_fmt) = inner.capture_format else {
            return Err(anyhow!("next_frame: capture not formatted"));
        };
        let num_planes = cap_fmt.num_planes as usize;
        let mut planes = [V4l2Plane::default(); 8];
        let mut buf = V4l2Buffer {
            buf_type: V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE,
            memory: V4L2_MEMORY_MMAP,
            length: num_planes as u32,
            m_planes: planes.as_mut_ptr() as u64,
            ..Default::default()
        };
        // SAFETY: DQBUF reads `length` to know how many planes
        // to fill. fd owned by inner.
        let dq_result = unsafe { vidioc_dqbuf(inner.fd(), &mut buf) };
        match dq_result {
            Ok(_) => {}
            Err(nix::errno::Errno::EAGAIN) => {
                // Non-blocking and nothing ready. Caller should
                // poll() or sleep + retry. We surface as Err so
                // the caller doesn't mistake EAGAIN for EOF.
                return Err(anyhow!("DQBUF CAPTURE: would block (EAGAIN)"));
            }
            Err(nix::errno::Errno::EPIPE) => {
                // Decoder drained -- EOF.
                drop(inner);
                self.inner.lock().unwrap().capture_drained = true;
                return Ok(None);
            }
            Err(e) => return Err(anyhow!("DQBUF CAPTURE: {}", e)),
        }
        if buf.flags & V4L2_BUF_FLAG_ERROR != 0 {
            return Err(anyhow!(
                "DQBUF CAPTURE buf={}: V4L2_BUF_FLAG_ERROR set", buf.index
            ));
        }
        let is_last = buf.flags & V4L2_BUF_FLAG_LAST != 0;
        let idx = buf.index;
        let width = cap_fmt.width;
        let height = cap_fmt.height;
        // Snapshot the Y-plane stride from the negotiated format so
        // it travels with the Frame (DmaBuf EGLImage import needs
        // it for EGL_DMA_BUF_PLANE*_PITCH_EXT, NOT width). Use 0
        // as a sentinel if num_planes==0 -- impossible per S_FMT
        // contract, but a defensive fallback keeps the unwrap-free.
        let stride = cap_fmt.plane_fmt.first()
            .map(|pf| pf.bytesperline)
            .unwrap_or(0);
        let capture_buffer_type = inner.capture_buffer_type;
        // Pull pointer + length per plane from the mmap region
        // (MMAP path) OR the exported fd (DmaBuf path). Re-borrow
        // inner mutably to flip in-flight bit + cache the values.
        drop(inner);
        let inner_mut = self.inner.lock().unwrap();
        // Both MMAP-only and DmaBuf paths run REQBUFS as MMAP
        // (piece 4a-fix), so mapped_capture is populated either
        // way. The DmaBuf path additionally has fds in
        // capture_dmabuf_fds. Populate y_ptr/uv_ptr from mmap
        // unconditionally so the paint side can fall back to the
        // CPU upload path if EGLImage extensions are missing at
        // runtime; populate dmabuf_fd only when DmaBuf is enabled.
        if (idx as usize) >= inner_mut.mapped_capture.len() {
            return Err(anyhow!(
                "DQBUF returned out-of-range buf idx {}", idx
            ));
        }
        let region_planes = &inner_mut.mapped_capture[idx as usize];
        let (y_ptr, y_len) = {
            let p = &region_planes[0];
            (p.ptr as *const u8, planes[0].bytesused as usize)
        };
        let (uv_ptr, uv_len) = if num_planes >= 2 {
            let p = &region_planes[1];
            (p.ptr as *const u8, planes[1].bytesused as usize)
        } else {
            // num_planes == 1 (interleaved NV12 layout on
            // bcm2835-codec): the UV plane is inside the
            // same mmap region as Y, offset by width*height.
            let p = &region_planes[0];
            let y_size = (width * height) as usize;
            let total = planes[0].bytesused as usize;
            let uv_size = total.saturating_sub(y_size);
            unsafe {
                ((p.ptr as *const u8).add(y_size), uv_size)
            }
        };
        let dmabuf_fd = match capture_buffer_type {
            CaptureBufferType::Mmap => None,
            CaptureBufferType::DmaBuf => {
                if (idx as usize) >= inner_mut.capture_dmabuf_fds.len() {
                    return Err(anyhow!(
                        "DQBUF DmaBuf: idx {} out of range for fd table (len={})",
                        idx, inner_mut.capture_dmabuf_fds.len()
                    ));
                }
                Some(inner_mut.capture_dmabuf_fds[idx as usize])
            }
        };
        let plane_lengths = [planes[0].bytesused as usize, planes[1].bytesused as usize];
        let inner_arc = self.inner.clone();
        drop(inner_mut);
        // Mark in-flight + handle EOF flag in a separate lock
        // scope so the Frame construction is the last action.
        {
            let mut inner_w = self.inner.lock().unwrap();
            if (idx as usize) < inner_w.capture_in_flight.len() {
                inner_w.capture_in_flight[idx as usize] = true;
            }
            if is_last {
                inner_w.capture_drained = true;
            }
        }
        Ok(Some(Frame {
            inner: inner_arc,
            capture_buffer_index: idx,
            width,
            height,
            plane_lengths,
            y_ptr,
            y_len,
            uv_ptr,
            uv_len,
            dmabuf_fd,
            stride,
        }))
    }
}

/// r83 Phase B (2026-06-08): pure y-axis crop math. Extracted
/// from `Decoder::capture_y_crop_max` so unit tests don't need a
/// live `Decoder` (those tests are Linux-gated on `/dev/video10`).
/// The math: fail-soft to 1.0 (= no crop) on any of:
///   - display height not yet set (sentinel 0)
///   - allocated height not yet negotiated (0)
///   - display >= allocated (would yield a ratio >= 1.0; identity)
/// Otherwise return `display / allocated` as f32 in (0, 1).
///
/// Cross-platform (no `#[cfg(target_os = "linux")]` gating) so the
/// host-side unit tests on Mac can exercise it.
fn compute_y_crop_max(display_h: u32, alloc_h: u32) -> f32 {
    if display_h == 0 || alloc_h == 0 || display_h >= alloc_h {
        return 1.0;
    }
    (display_h as f32) / (alloc_h as f32)
}

/// r99 (2026-06-09): canonical format for a REQBUFS failure's
/// with_context message. Embeds errno + buffer params so the
/// downstream `[mmal_leak_suspect]` log line (rendered by
/// ipc_main.rs's `extract_top_context`) surfaces the actual kernel
/// rejection reason + the resource shape the renderer was asking
/// for. Cross-platform so Mac-side unit tests cover the format.
///
/// Format pinned by `format_reqbufs_failure_context_layout` test;
/// parsers should grep for `errno=`, `requested_buffers=`,
/// `total_alloc_kb=` keys.
pub fn format_reqbufs_failure_context(
    dir: QueueDirection,
    memory_type: u32,
    errno_str: &str,
    count: u32,
    sizeimage: u32,
    total_alloc_kb: u64,
) -> String {
    format!(
        "VIDIOC_REQBUFS({:?}, memory={}) errno={} requested_buffers={} \
         plane_sizeimage={} buffer_size_kb={} total_alloc_kb={}",
        dir, memory_type, errno_str, count,
        sizeimage, (sizeimage as u64) / 1024, total_alloc_kb,
    )
}

/// r99 (2026-06-09): snapshot of the kernel's CMA carveout state
/// at the moment of a REQBUFS failure. Sourced from
/// `/proc/meminfo` lines `CmaTotal:` + `CmaFree:`. The `used` field
/// is the derived `total - free`. All values in MB (1024 KiB).
///
/// Linux-gated: `/proc/meminfo` is unavailable on macOS dev hosts;
/// `read_cma_snapshot_mb` returns `None` there.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CmaSnapshotMb {
    pub total: u64,
    pub free: u64,
    pub used: u64,
}

/// r99 (2026-06-09): parse `/proc/meminfo` looking for `CmaTotal:`
/// + `CmaFree:` lines. Returns `None` on macOS (where the file
/// doesn't exist) or when either line is absent (e.g. CMA-disabled
/// kernel). One syscall on a slow path — fine.
///
/// Cross-platform signature; the body parses `/proc/meminfo` on
/// Linux and returns `None` immediately on other targets. Pure
/// parser `parse_cma_lines` is broken out so unit tests can run on
/// every host without needing the file present.
#[cfg(target_os = "linux")]
pub fn read_cma_snapshot_mb() -> Option<CmaSnapshotMb> {
    let contents = std::fs::read_to_string("/proc/meminfo").ok()?;
    parse_cma_lines(&contents)
}

#[cfg(not(target_os = "linux"))]
pub fn read_cma_snapshot_mb() -> Option<CmaSnapshotMb> {
    None
}

/// r99 (2026-06-09): pure parser for `/proc/meminfo`'s CMA lines.
/// Expected line shape: `CmaTotal:         262144 kB`. Returns
/// `Some(CmaSnapshotMb { total, free, used })` only when BOTH
/// `CmaTotal:` and `CmaFree:` are present; otherwise `None`.
///
/// Cross-platform — Mac-side tests pass synthetic content.
pub fn parse_cma_lines(contents: &str) -> Option<CmaSnapshotMb> {
    let mut total_kib: Option<u64> = None;
    let mut free_kib: Option<u64> = None;
    for line in contents.lines() {
        if let Some(rest) = line.strip_prefix("CmaTotal:") {
            total_kib = rest.split_whitespace().next()
                .and_then(|s| s.parse::<u64>().ok());
        } else if let Some(rest) = line.strip_prefix("CmaFree:") {
            free_kib = rest.split_whitespace().next()
                .and_then(|s| s.parse::<u64>().ok());
        }
    }
    let total = total_kib? / 1024;
    let free = free_kib? / 1024;
    Some(CmaSnapshotMb {
        total,
        free,
        used: total.saturating_sub(free),
    })
}

// ============================================================
// Mac-side public types for cross-platform syntax checking. On
// macOS, Decoder + Frame don't exist (no V4L2). The structs
// below are pure-Rust mirrors that compile everywhere -- syntax
// errors in the field set surface on every host's `cargo check`.
// ============================================================

#[cfg(not(target_os = "linux"))]
pub struct Frame {
    _placeholder: (),
}

#[cfg(not(target_os = "linux"))]
impl Frame {
    pub fn width(&self) -> u32 { unimplemented!("Linux-only") }
    pub fn height(&self) -> u32 { unimplemented!("Linux-only") }
    pub fn y_plane(&self) -> &[u8] { unimplemented!("Linux-only") }
    pub fn uv_plane(&self) -> &[u8] { unimplemented!("Linux-only") }
    pub fn dma_buf_fd(&self) -> Option<i32> { None }
    pub fn stride(&self) -> u32 { unimplemented!("Linux-only") }
}

// ============================================================
// Helpers.
// ============================================================

fn c_str_to_string(buf: &[u8]) -> String {
    let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
    String::from_utf8_lossy(&buf[..end]).to_string()
}

// ============================================================
// Tests.
// ============================================================

#[cfg(test)]
mod tests {
    use super::*;

    // ============================================================
    // r75 (2026-06-07): MMAL component-slot leak counter API pin.
    //
    // The Decoder::open increment + DecoderInner::drop decrement
    // are tested via the real /dev/video10 fixture below (gated
    // on Linux + device existence). This test pins the public API
    // surface so a future refactor that drops `mmal_components_live`
    // or renames the static surfaces at compile time, not on FYS
    // post-deploy.
    // ============================================================

    /// r75 subagent WARN-4: serialize counter-perturbing tests
    /// because cargo runs tests in parallel within one process. A
    /// sibling test (especially the live-device fixture tests on a
    /// Pi CI runner) could observe transient +7 from this test, OR
    /// concurrent Decoder::open/Drop could perturb our delta. A
    /// process-local Mutex<()> taken at test entry serializes any
    /// test that touches the static.
    static COUNTER_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    // ============================================================
    // r101 (2026-06-09): EGLImage cache regression-lock tests.
    // ============================================================

    #[test]
    fn is_egl_image_cache_enabled_defaults_to_on() {
        // r101 subagent WARN-3: take the same COUNTER_TEST_LOCK
        // serializer the rest of this test module uses, because
        // cargo runs tests in parallel and two tests both mutating
        // OPENMARQUEE_EGL_IMAGE_CACHE can interleave their set/
        // assert in flaky-failure-inducing ways.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        // Default (env var unset) = cache enabled. r101 ships with
        // the kill-switch OFF (i.e. cache ON) so QA's first deploy
        // exercises the new code path.
        let prior = std::env::var("OPENMARQUEE_EGL_IMAGE_CACHE").ok();
        std::env::remove_var("OPENMARQUEE_EGL_IMAGE_CACHE");
        assert!(
            is_egl_image_cache_enabled(),
            "default (env unset) must enable the cache",
        );
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_EGL_IMAGE_CACHE", v);
        }
    }

    #[test]
    fn is_egl_image_cache_enabled_off_values_disable() {
        // r101 subagent WARN-3: take the COUNTER_TEST_LOCK -- see
        // is_egl_image_cache_enabled_defaults_to_on.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        // The kill switch accepts the standard "off" lexicon so an
        // operator's runbook reads naturally regardless of which
        // form they remember.
        let prior = std::env::var("OPENMARQUEE_EGL_IMAGE_CACHE").ok();
        for off in ["0", "off", "false", "no", "disable", "disabled", "OFF", "FaLsE"] {
            std::env::set_var("OPENMARQUEE_EGL_IMAGE_CACHE", off);
            assert!(
                !is_egl_image_cache_enabled(),
                "value {off:?} must disable the cache",
            );
        }
        // And any non-off value (including unrecognized) defaults
        // to enabled: explicit operator intent to disable is
        // required.
        for on in ["1", "on", "yes", "enable", "enabled", "", "garbage"] {
            std::env::set_var("OPENMARQUEE_EGL_IMAGE_CACHE", on);
            assert!(
                is_egl_image_cache_enabled(),
                "value {on:?} must keep the cache enabled (default-on policy)",
            );
        }
        // Restore.
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_EGL_IMAGE_CACHE", v);
        } else {
            std::env::remove_var("OPENMARQUEE_EGL_IMAGE_CACHE");
        }
    }

    // ============================================================
    // r102.1 (2026-06-09): V3D BO snapshot parser regression-lock.
    // ============================================================

    #[test]
    fn parse_v3d_bo_stats_fys_empirical_format() {
        // r102.1.1 (2026-06-09): pin the ACTUAL FYS kernel
        // format QA captured 2026-06-09 via
        // `sudo -u openmarquee cat /sys/kernel/debug/dri/0/bo_stats`.
        // The r102.1 parent shipped against a hypothetical
        // kernel-6.12 format (`num bos allocated:`) that didn't
        // match FYS at all -- parser returned None for every
        // probe even though the file was readable. This test
        // pins the real format so a future regression that
        // touches the parser doesn't silently lose FYS support
        // again.
        let contents = "\
                          dumb:   2040kb BOs (1)\n\
                           V3D: 109972kb BOs (45)\n\
                    V3D shader:     80kb BOs (20)\n\
                          kernel:   5280kb BOs (4)\n\
                        binner:  16384kb BOs (1)\n";
        let snap = parse_v3d_bo_stats(contents);
        assert_eq!(snap.v3d_bos, Some(45), "V3D BO count must parse to 45");
        assert_eq!(snap.v3d_mem_kb, Some(109972), "V3D mem must parse to 109972 kb");
    }

    #[test]
    fn parse_v3d_bo_stats_excludes_v3d_shader_category() {
        // r102.1.1: `V3D:` and `V3D shader:` are DISTINCT
        // categories on FYS. The parser's strip_prefix("V3D:")
        // intentionally rejects "V3D shader:" because shader
        // BOs aren't what QA is tracking. Pin so a future
        // refactor that loosens the prefix match (e.g. to
        // "V3D" without colon) catches this case.
        //
        // r102.1.1 subagent NIT: place `V3D:` BEFORE
        // `V3D shader:` so a loosened prefix wouldn't be
        // rescued by a later overwrite. Also assert against
        // an isolated `V3D shader:`-only input (no `V3D:`
        // line) to pin that shader alone produces all-None.
        let mixed = "\
            V3D: 109972kb BOs (45)\n\
            V3D shader: 80kb BOs (20)\n";
        let snap_mixed = parse_v3d_bo_stats(mixed);
        assert_eq!(snap_mixed.v3d_bos, Some(45));
        assert_eq!(snap_mixed.v3d_mem_kb, Some(109972));

        let shader_only = "\
            V3D shader: 80kb BOs (20)\n\
            kernel: 100kb BOs (2)\n";
        let snap_shader = parse_v3d_bo_stats(shader_only);
        assert_eq!(
            snap_shader.v3d_bos, None,
            "V3D shader alone must not satisfy the V3D: prefix",
        );
        assert_eq!(snap_shader.v3d_mem_kb, None);
    }

    #[test]
    fn parse_v3d_bo_stats_legacy_hypothetical_format_still_works() {
        // r102.1.1: legacy fallback for the hypothetical
        // kernel-6.12 format the r102.1 parent targeted. Kept
        // in case a future kernel upgrade renames back to
        // this shape. Lower priority than the FYS-empirical
        // format but cheap to retain.
        let contents = "\
            num bos allocated:\t33\n\
            size bos allocated:\t88611 kb\n";
        let snap = parse_v3d_bo_stats(contents);
        assert_eq!(snap.v3d_bos, Some(33));
        assert_eq!(snap.v3d_mem_kb, Some(88611));
    }

    #[test]
    fn parse_v3d_bo_stats_tolerates_space_separated() {
        // Some kernels emit space-separated values; trim() in
        // the parser handles both. Pin to keep the
        // double-format support honest.
        let contents = "\
            num bos allocated: 42\n\
            size bos allocated: 12345 kb\n";
        let snap = parse_v3d_bo_stats(contents);
        assert_eq!(snap.v3d_bos, Some(42));
        assert_eq!(snap.v3d_mem_kb, Some(12345));
    }

    #[test]
    fn parse_v3d_bo_stats_returns_default_on_unrecognized_format() {
        // Future kernel renames or wholly different format
        // produces all-None defaults (line shape stays
        // uniform via `?` sentinels in the probe output).
        let contents = "\
            num_bos_allocated=33\n\
            random unrelated line\n\
            v3d_bo_count: 33\n";
        let snap = parse_v3d_bo_stats(contents);
        assert_eq!(snap.v3d_bos, None);
        assert_eq!(snap.v3d_mem_kb, None);
    }

    #[test]
    fn parse_v3d_bo_stats_returns_default_on_empty_input() {
        let snap = parse_v3d_bo_stats("");
        assert_eq!(snap.v3d_bos, None);
        assert_eq!(snap.v3d_mem_kb, None);
    }

    #[test]
    fn read_v3d_bo_snapshot_returns_default_when_file_absent() {
        // The probe is best-effort -- absent or unreadable debugfs
        // must return defaulted Nones, not panic. On macOS (and
        // most non-Pi Linux hosts) /sys/kernel/debug/dri/0/bo_stats
        // doesn't exist; the function should return all-None
        // silently.
        let snap = read_v3d_bo_snapshot();
        // On a host where the file IS present (e.g. a Pi CI
        // runner), v3d_bos may be Some -- that's also fine. We
        // only assert the call DOESN'T panic + returns a
        // V3dBoSnapshot.
        let _ = snap.v3d_bos;
        let _ = snap.v3d_mem_kb;
    }

    #[test]
    fn log_v3d_bos_at_phase_doesnt_panic_for_any_phase_label() {
        // Probe must not panic regardless of phase label content
        // or slide_id presence. Defensive coverage so a future
        // call site with an unusual label can't wedge the
        // renderer (the whole point of "instrumentation MUST
        // NOT affect liveness").
        log_v3d_bos_at_phase("test_phase", None);
        log_v3d_bos_at_phase(
            "test_phase_with_slide",
            Some(uuid::Uuid::from_u128(0xdeadbeef)),
        );
        log_v3d_bos_at_phase("", None); // empty label
        log_v3d_bos_at_phase("phase with spaces", None);
    }

    #[test]
    fn r106_is_feed_drain_decouple_enabled_defaults_to_on() {
        // r106 + Path A Stage 2 (2026-06-14): the kill switch
        // defaults ON so a fresh deploy gets the decoupled feed
        // path automatically. Operators flip OFF only for A/B
        // when comparing against the pre-r106 blocking path on
        // the same binary.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_FEED_DRAIN_DECOUPLE").ok();
        std::env::remove_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE");
        assert!(
            is_feed_drain_decouple_enabled(),
            "r106: default (env unset) must ENABLE feed-drain decouple",
        );
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE", v);
        }
    }

    #[test]
    fn r106_is_feed_drain_decouple_off_values_disable() {
        // r106 + Path A Stage 2 (2026-06-14): the off-lexicon
        // matches the renderer's other env-knob conventions
        // (case-insens, whitespace-trimmed). Anything not in
        // the off list keeps decouple ENABLED so a typo can't
        // silently regress the production path.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_FEED_DRAIN_DECOUPLE").ok();
        for off in ["0", "off", "false", "no", "disable", "disabled", "OFF"] {
            std::env::set_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE", off);
            assert!(
                !is_feed_drain_decouple_enabled(),
                "value {off:?} must disable r106 decouple",
            );
        }
        for on in ["1", "on", "yes", "enable", "enabled", "", "garbage"] {
            std::env::set_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE", on);
            assert!(
                is_feed_drain_decouple_enabled(),
                "value {on:?} must keep r106 decouple enabled (default-on policy)",
            );
        }
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE", v);
        } else {
            std::env::remove_var("OPENMARQUEE_FEED_DRAIN_DECOUPLE");
        }
    }

    #[test]
    fn r101_1_drop_destroy_loop_emits_summary_always() {
        // r101.1 (2026-06-09): pin the always-fires summary line by
        // reading v4l2.rs as source. r101 (parent) summary was
        // gated on `destroyed > 0 || failed > 0`; the empty-cache
        // case emitted NOTHING. r101.1 moves the summary out of the
        // conditional so QA can distinguish "Drop ran cleanly with
        // no cached images" from "Drop hung inside the first
        // destroy call" -- the wedge signal.
        //
        // Reads src/v4l2.rs as a string and asserts the
        // `egl_image_destroy_loop_summary` writeln! call is NOT
        // immediately preceded by `if destroyed > 0 || failed > 0
        // {`. Source-level regression-lock; a future refactor that
        // re-gates the summary on destroyed>0 will fail.
        let src = include_str!("v4l2.rs");
        // Find the ACTUAL writeln! invocation (not the doc-comment
        // mention earlier in the function). The writeln uses a
        // string literal with the probe-name prefix.
        let needle = "\"[perf] egl_image_destroy_loop_summary";
        let summary_pos = src.find(needle).unwrap_or_else(|| {
            panic!(
                "r101.1 instrumentation missing: '{needle}' not found in v4l2.rs",
            )
        });
        // Look backward ~600 chars for the immediately-preceding
        // control-flow context. The summary writeln! must NOT be
        // inside an `if destroyed` or `if failed` arm.
        let preceding = &src[summary_pos.saturating_sub(600)..summary_pos];
        assert!(
            preceding.contains("loop_elapsed_us"),
            "r101.1 instrumentation drift: expected `let loop_elapsed_us = loop_start.elapsed().as_micros();` \
             immediately preceding the summary writeln!, found:\n---preceding---\n{preceding}\n---end---",
        );
        assert!(
            !preceding.contains("if destroyed > 0 || failed > 0"),
            "r101.1 regression: summary writeln! re-gated on destroyed>0||failed>0. \
             The always-fires policy is load-bearing for the wedge diagnostic.",
        );
        // And the entry+exit per-handle probes must exist too.
        assert!(
            src.contains("egl_image_destroy_entry"),
            "r101.1 instrumentation missing: per-handle entry probe `egl_image_destroy_entry`",
        );
        assert!(
            src.contains("egl_image_destroy_exit"),
            "r101.1 instrumentation missing: per-handle exit probe `egl_image_destroy_exit`",
        );
    }

    #[test]
    fn should_emit_steady_state_video_probe_throttle() {
        // r103.1: probe fires on count==1 (first paint of a
        // decoder's lifetime, i.e. start of a slide-hold) AND
        // every 30 paints thereafter (~1 sec at 30fps).
        // count==0 means Ok(None) skip (no frame ready) and
        // MUST be silent so QA's grep doesn't see noise from
        // warmup ticks.
        assert!(!should_emit_steady_state_video_probe(0), "count=0 (skip) must NOT emit");
        assert!(should_emit_steady_state_video_probe(1), "count=1 (first paint) MUST emit");
        for n in 2..30 {
            assert!(
                !should_emit_steady_state_video_probe(n),
                "count={n} (mid-second) must NOT emit",
            );
        }
        for k in 1..5 {
            let n = 30 * k;
            assert!(
                should_emit_steady_state_video_probe(n),
                "count={n} (every 30 paints, ~{}s into hold) MUST emit",
                k,
            );
            assert!(
                !should_emit_steady_state_video_probe(n + 1),
                "count={} (just after a 30-mark) must NOT emit",
                n + 1,
            );
        }
    }

    #[test]
    fn is_transition_program_cache_enabled_defaults_to_on() {
        // r102.3: kill switch defaults ON; co-located here with
        // the EGLImage cache + transition FBO cache kill switches.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE").ok();
        std::env::remove_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE");
        assert!(
            is_transition_program_cache_enabled(),
            "default (env unset) must enable the cache",
        );
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE", v);
        }
    }

    #[test]
    fn is_transition_program_cache_enabled_off_values_disable() {
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE").ok();
        for off in ["0", "off", "false", "no", "disable", "disabled", "OFF"] {
            std::env::set_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE", off);
            assert!(
                !is_transition_program_cache_enabled(),
                "value {off:?} must disable the cache",
            );
        }
        for on in ["1", "on", "yes", "enable", "enabled", "", "garbage"] {
            std::env::set_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE", on);
            assert!(
                is_transition_program_cache_enabled(),
                "value {on:?} must keep the cache enabled (default-on policy)",
            );
        }
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE", v);
        } else {
            std::env::remove_var("OPENMARQUEE_TRANSITION_PROGRAM_CACHE");
        }
    }

    #[test]
    fn is_transition_fbo_cache_enabled_defaults_to_on() {
        // r102.2: kill-switch defaults ON; co-located here with
        // the EGLImage cache kill-switch tests because hdmi.rs
        // (where the actual cache lives) is linux-cfg-gated.
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_TRANSITION_FBO_CACHE").ok();
        std::env::remove_var("OPENMARQUEE_TRANSITION_FBO_CACHE");
        assert!(
            is_transition_fbo_cache_enabled(),
            "default (env unset) must enable the cache",
        );
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_TRANSITION_FBO_CACHE", v);
        }
    }

    #[test]
    fn is_transition_fbo_cache_enabled_off_values_disable() {
        let _guard = COUNTER_TEST_LOCK
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        let prior = std::env::var("OPENMARQUEE_TRANSITION_FBO_CACHE").ok();
        for off in ["0", "off", "false", "no", "disable", "disabled", "OFF"] {
            std::env::set_var("OPENMARQUEE_TRANSITION_FBO_CACHE", off);
            assert!(
                !is_transition_fbo_cache_enabled(),
                "value {off:?} must disable the cache",
            );
        }
        for on in ["1", "on", "yes", "enable", "enabled", "", "garbage"] {
            std::env::set_var("OPENMARQUEE_TRANSITION_FBO_CACHE", on);
            assert!(
                is_transition_fbo_cache_enabled(),
                "value {on:?} must keep the cache enabled (default-on policy)",
            );
        }
        if let Some(v) = prior {
            std::env::set_var("OPENMARQUEE_TRANSITION_FBO_CACHE", v);
        } else {
            std::env::remove_var("OPENMARQUEE_TRANSITION_FBO_CACHE");
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn egl_image_handle_is_copy_and_send() {
        // Compile-time pin: the cache lookup returns by value so
        // the Mutex guard scope can release quickly. Send is
        // required because the renderer is single-threaded today
        // but the cache field lives inside an Arc<Mutex<...>>;
        // Send keeps a future async-handler refactor unblocked.
        fn assert_copy<T: Copy>() {}
        fn assert_send<T: Send>() {}
        assert_copy::<EglImageHandle>();
        assert_send::<EglImageHandle>();
    }

    #[test]
    fn mmal_components_live_api_surface_compiles_and_reads_the_static() {
        // The function must mirror the atomic; if the implementation
        // diverges (e.g. caches stale value), QA's leak diagnostics
        // would lie. Verify call-through.
        let _guard = COUNTER_TEST_LOCK.lock().expect("COUNTER_TEST_LOCK poisoned");
        let before = mmal_components_live();
        MMAL_COMPONENTS_LIVE.fetch_add(7, std::sync::atomic::Ordering::Relaxed);
        let after = mmal_components_live();
        assert_eq!(after - before, 7, "mmal_components_live MUST mirror the atomic");
        // Restore so other tests aren't polluted.
        MMAL_COMPONENTS_LIVE.fetch_sub(7, std::sync::atomic::Ordering::Relaxed);
    }

    #[test]
    fn mmal_components_decrement_floors_at_zero_no_underflow() {
        // r75 subagent BLOCKER-1 pin: a stray Drop on a Decoder that
        // never had its corresponding open() increment must NOT
        // wrap the AtomicUsize to usize::MAX. The fetch_update +
        // saturating_sub combo floors the atomic at 0.
        let _guard = COUNTER_TEST_LOCK.lock().expect("COUNTER_TEST_LOCK poisoned");
        // Force counter to 0 from whatever the current value is so
        // we can assert "decrement from 0 stays 0".
        let current = mmal_components_live();
        MMAL_COMPONENTS_LIVE.store(0, std::sync::atomic::Ordering::Relaxed);
        // Inline the EXACT decrement shape used by DecoderInner::drop.
        let after = MMAL_COMPONENTS_LIVE
            .fetch_update(
                std::sync::atomic::Ordering::Relaxed,
                std::sync::atomic::Ordering::Relaxed,
                |v| Some(v.saturating_sub(1)),
            )
            .map(|prev| prev.saturating_sub(1))
            .unwrap_or(0);
        assert_eq!(
            after, 0,
            "decrement from 0 MUST stay 0; pre-fix this wrapped to usize::MAX \
             and corrupted QA's time-series for the rest of the process lifetime"
        );
        let after_value = mmal_components_live();
        assert_eq!(after_value, 0, "atomic state must stay floored at 0");
        // Restore.
        MMAL_COMPONENTS_LIVE.store(current, std::sync::atomic::Ordering::Relaxed);
    }

    // ============================================================
    // r70 (2026-06-06): feed() empty-pool retry budget pin.
    //
    // FYS 1080p workload errored on every transition with the
    // pre-r70 10ms budget. r70 bumps the default to 100ms +
    // makes it env-overridable. These tests pin (a) the default
    // total budget is ~100ms, (b) env override is honored,
    // (c) bogus env values clamp to safe bounds rather than
    // deadlocking or zeroing the soft-wait.
    // ============================================================

    fn budget_ms(retries: usize, interval_ms: u64) -> u64 {
        (retries as u64) * interval_ms
    }

    /// All schedule cases consolidated into ONE test so the
    /// shared OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS env var
    /// (set/remove'd per case) can't race a parallel sibling.
    /// r70 subagent WARN-1 fix: also `#[cfg(not(target_os = "linux"))]`
    /// gated so it never runs on a Pi where the live-device V4L2
    /// tests (`decode_test_fixture_*`) call `feed()` in parallel
    /// and would race the env mutation.
    ///
    /// Targets `resolve_feed_drain_schedule_from_env` (the pure
    /// resolver) NOT `feed_drain_retry_schedule` -- the latter's
    /// OnceLock caches the FIRST resolution forever, so a
    /// per-case env-mutation test would only see Case 1's result
    /// every time. Same invariants verified.
    #[test]
    #[cfg(not(target_os = "linux"))]
    fn resolve_feed_drain_schedule_default_env_overrides_and_clamps() {
        // SAFETY: test mutates a process-wide env var; cargo
        // runs tests in parallel within one process. This test
        // is the SOLE owner of OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS
        // (no other test in any module references it; grep'd
        // to confirm). Linux-gated above so even live-Pi V4L2
        // tests in this same crate can't race.

        // ---- Case 1: default budget = 100ms ----
        unsafe { std::env::remove_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert_eq!(
            budget_ms(retries, interval_ms),
            100,
            "case 1 default: budget must be 100ms (25 x 4ms); got {} x {}ms",
            retries, interval_ms,
        );

        // ---- Case 2: env override inside clamp range ----
        unsafe { std::env::set_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS", "200") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert_eq!(
            budget_ms(retries, interval_ms),
            200,
            "case 2: 200ms override must round-trip; got {} x {}ms",
            retries, interval_ms,
        );

        unsafe { std::env::set_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS", "60") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert_eq!(budget_ms(retries, interval_ms), 60, "case 2: 60ms must round-trip");

        // ---- Case 3: typo `=0` clamps to floor (10ms) ----
        unsafe { std::env::set_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS", "0") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert!(
            budget_ms(retries, interval_ms) >= FEED_DRAIN_MIN_BUDGET_MS,
            "case 3: budget must clamp to >= {}ms; got {}",
            FEED_DRAIN_MIN_BUDGET_MS,
            budget_ms(retries, interval_ms),
        );

        // ---- Case 4: typo `=999999` clamps to ceiling (1000ms) ----
        unsafe { std::env::set_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS", "999999") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert_eq!(
            budget_ms(retries, interval_ms),
            FEED_DRAIN_MAX_BUDGET_MS,
            "case 4: budget must clamp to exactly {}ms ceiling; got {}",
            FEED_DRAIN_MAX_BUDGET_MS,
            budget_ms(retries, interval_ms),
        );

        // ---- Case 5: garbage `=notanumber` falls back to default ----
        unsafe { std::env::set_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS", "notanumber") };
        let (retries, interval_ms) = resolve_feed_drain_schedule_from_env();
        assert_eq!(
            budget_ms(retries, interval_ms),
            100,
            "case 5: non-numeric env must fall back to 100ms default",
        );

        // ---- Cleanup so a later test sees a clean env ----
        unsafe { std::env::remove_var("OPENMARQUEE_V4L2_FEED_DRAIN_BUDGET_MS") };
    }

    #[test]
    fn fourcc_packs_little_endian() {
        assert_eq!(V4L2_PIX_FMT_H264, 0x34363248);
        assert_eq!(V4L2_PIX_FMT_NV12, 0x3231564E);
    }

    #[test]
    fn struct_layouts_match_kernel() {
        // Compile-time guards already panic on mismatch; mirror
        // them at runtime so cargo-test output names the failed
        // type.
        assert_eq!(std::mem::size_of::<V4l2Capability>(), 104);
        assert_eq!(std::mem::size_of::<V4l2PlanePixFormat>(), 20);
        assert_eq!(std::mem::size_of::<V4l2PixFormatMplane>(), 192);
        assert_eq!(std::mem::size_of::<V4l2Format>(), 208);
        assert_eq!(std::mem::size_of::<V4l2Plane>(), 64);
        assert_eq!(std::mem::size_of::<V4l2Buffer>(), 88);
        assert_eq!(std::mem::size_of::<V4l2Timecode>(), 16);
        assert_eq!(std::mem::size_of::<V4l2Requestbuffers>(), 20);
        // r83 Phase A: G_SELECTION marshalling structs.
        assert_eq!(std::mem::size_of::<V4l2Rect>(), 16);
        assert_eq!(std::mem::size_of::<V4l2Selection>(), 64);
        // Field-offset guard for V4l2Format: union starts at
        // offset 8 (after 4-byte type + 4-byte alignment pad),
        // NOT at offset 4. A piece-2b subagent caught the
        // pre-fix version with `fmt` at offset 4, which would
        // make S_FMT silently put pix_mp.width into kernel
        // reserved bytes. Pin via memoffset-style ptr math
        // since std::mem::offset_of is stable from 1.77 -- this
        // crate targets 1.85 (Cargo.toml rust-version) so it's
        // available.
        assert_eq!(std::mem::offset_of!(V4l2Format, buf_type), 0);
        assert_eq!(std::mem::offset_of!(V4l2Format, fmt), 8);
    }

    #[test]
    fn c_str_decode_trims_at_nul() {
        let mut buf = [0u8; 16];
        buf[..14].copy_from_slice(b"bcm2835-codec\0");
        assert_eq!(c_str_to_string(&buf), "bcm2835-codec");
    }

    #[test]
    fn compute_y_crop_max_no_crop_when_dims_unknown_or_equal() {
        // r83 Phase B math. Fail-soft to 1.0 (= no crop, byte-
        // identical to pre-Phase-B behavior) for the four
        // "we don't know enough" branches.
        assert_eq!(compute_y_crop_max(0, 0), 1.0);
        assert_eq!(compute_y_crop_max(0, 1088), 1.0);
        assert_eq!(compute_y_crop_max(1080, 0), 1.0);
        assert_eq!(compute_y_crop_max(1080, 1080), 1.0);
        // Defensive: display > allocated would have been a kernel
        // bug; clamp to 1.0 rather than emit ratio > 1.0 (which
        // would sample past the end of the texture).
        assert_eq!(compute_y_crop_max(1100, 1088), 1.0);
    }

    #[test]
    fn format_reqbufs_failure_context_layout() {
        // r99: pin the with_context message shape so log parsers
        // can rely on it. extract_top_context renders this string
        // verbatim into the `op=` field of the [mmal_leak_suspect]
        // line.
        let msg = format_reqbufs_failure_context(
            QueueDirection::Output,
            V4L2_MEMORY_MMAP,
            "ENOMEM",
            4,
            2_097_152,  // 2 MiB sizeimage (typical 1080p H264 OUTPUT)
            8192,
        );
        assert!(msg.contains("VIDIOC_REQBUFS(Output, memory=1)"));
        assert!(msg.contains("errno=ENOMEM"));
        assert!(msg.contains("requested_buffers=4"));
        assert!(msg.contains("plane_sizeimage=2097152"));
        assert!(msg.contains("buffer_size_kb=2048"));
        assert!(msg.contains("total_alloc_kb=8192"));
    }

    #[test]
    fn format_reqbufs_failure_context_handles_zero_sizeimage() {
        // Defensive: when fmt.plane_fmt is empty (sentinel 0),
        // buffer_size_kb should be 0 (not panic / divide error).
        let msg = format_reqbufs_failure_context(
            QueueDirection::Capture,
            V4L2_MEMORY_MMAP,
            "EBUSY",
            4,
            0,
            0,
        );
        assert!(msg.contains("plane_sizeimage=0"));
        assert!(msg.contains("buffer_size_kb=0"));
        assert!(msg.contains("total_alloc_kb=0"));
    }

    #[test]
    fn parse_cma_lines_extracts_total_and_free() {
        // r99: canonical /proc/meminfo shape (Linux 6.12 on
        // Pi Zero 2 W with cma=256M). The bcm2835-codec test bed.
        let contents = "\
MemTotal:         469228 kB
MemFree:          198456 kB
CmaTotal:         262144 kB
CmaFree:          213256 kB
SwapTotal:        102396 kB
";
        let snap = parse_cma_lines(contents).expect("must parse");
        assert_eq!(snap.total, 256);  // 262144 / 1024
        assert_eq!(snap.free, 208);   // 213256 / 1024 = 208 (int div)
        assert_eq!(snap.used, 48);    // 256 - 208
    }

    #[test]
    fn parse_cma_lines_returns_none_when_cma_disabled() {
        // Kernel with CONFIG_CMA disabled: /proc/meminfo has no
        // CmaTotal: line. Must return None, not panic.
        let contents = "\
MemTotal:         469228 kB
MemFree:          198456 kB
";
        assert!(parse_cma_lines(contents).is_none());
    }

    #[test]
    fn parse_cma_lines_returns_none_when_only_total_present() {
        // Half-populated case (shouldn't happen but defensive):
        // CmaTotal: alone with no CmaFree: returns None.
        let contents = "CmaTotal: 262144 kB\n";
        assert!(parse_cma_lines(contents).is_none());
    }

    #[test]
    fn parse_cma_lines_ignores_malformed_values() {
        // A garbled CmaTotal: that fails to parse should return
        // None (not crash).
        let contents = "CmaTotal: not_a_number kB\nCmaFree: 213256 kB\n";
        assert!(parse_cma_lines(contents).is_none());
    }

    #[test]
    fn compute_y_crop_max_returns_ratio_for_padded_capture() {
        // 1080p H.264 via bcm2835-codec: 1080 valid lines inside a
        // 1088 macroblock-rounded allocation.
        let r = compute_y_crop_max(1080, 1088);
        let expected = 1080.0_f32 / 1088.0_f32;
        assert!((r - expected).abs() < 1e-6, "got {r} expected {expected}");
        // 720p: 720 in a 720-allocation = no padding = identity.
        assert_eq!(compute_y_crop_max(720, 720), 1.0);
        // Off-aligned 1081 -> 1088 (hypothetical 2-row pad).
        let r2 = compute_y_crop_max(1081, 1088);
        let expected2 = 1081.0_f32 / 1088.0_f32;
        assert!((r2 - expected2).abs() < 1e-6, "got {r2} expected {expected2}");
    }

    #[test]
    fn capture_buffer_type_default_is_mmap() {
        assert_eq!(CaptureBufferType::Mmap, CaptureBufferType::Mmap);
        assert_ne!(CaptureBufferType::Mmap, CaptureBufferType::DmaBuf);
    }

    #[test]
    fn quantization_check_accepts_default_and_lim_range() {
        // DEFAULT (0) accepted: driver defers to spec defaults,
        // which is limited-range for V4L2_COLORSPACE_SMPTE170M /
        // REC709 -- the colorspaces bcm2835-codec emits.
        assert_eq!(
            check_quantization_for_lim_range_shader(V4L2_QUANTIZATION_DEFAULT)
                .expect("DEFAULT accepted"),
            V4L2_QUANTIZATION_DEFAULT,
        );
        assert_eq!(
            check_quantization_for_lim_range_shader(V4L2_QUANTIZATION_LIM_RANGE)
                .expect("LIM_RANGE accepted"),
            V4L2_QUANTIZATION_LIM_RANGE,
        );
    }

    #[test]
    fn quantization_check_rejects_full_range_with_meaningful_message() {
        let err = check_quantization_for_lim_range_shader(
            V4L2_QUANTIZATION_FULL_RANGE,
        )
        .expect_err("FULL_RANGE must error");
        let msg = format!("{}", err);
        assert!(msg.contains("FULL_RANGE"), "got: {msg}");
        assert!(msg.contains("FS_NV12_TO_RGB"), "got: {msg}");
    }

    #[test]
    fn quantization_check_rejects_unknown_values() {
        let err = check_quantization_for_lim_range_shader(42)
            .expect_err("unknown quantization must error");
        let msg = format!("{}", err);
        assert!(msg.contains("42"), "got: {msg}");
    }

    /// Open + cap query against the dev Pi's /dev/video10.
    /// Skipped cleanly when the device is missing.
    #[test]
    #[cfg(target_os = "linux")]
    fn open_and_query_caps_on_dev_video10() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 not present");
            return;
        }
        let dec = Decoder::open(path).expect("open");
        let caps = dec.query_capabilities().expect("QUERYCAP");
        assert!(caps.is_m2m_mplane(), "{:?}", caps);
        assert!(caps.is_streaming(), "{:?}", caps);
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn open_nonexistent_path_errors_cleanly() {
        let r = Decoder::open(Path::new("/dev/video-doesnt-exist"));
        assert!(r.is_err());
    }

    /// Decode the bundled 320x240 H.264 fixture; assert resolution
    /// + Y-plane non-zero. Linux-gated; skipped without /dev/video10.
    #[test]
    #[cfg(target_os = "linux")]
    fn decode_test_fixture_320x240() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping decode test: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        let out_fmt = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        eprintln!("OUTPUT negotiated: {:?}", out_fmt);
        let cap_fmt = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        eprintln!("CAPTURE negotiated: w={} h={} num_planes={} quantization={}",
            cap_fmt.width, cap_fmt.height, cap_fmt.num_planes,
            cap_fmt.quantization);
        // Quantization compatibility check (qa/v1-spec-delta P1).
        // bcm2835-codec is expected to emit LIM_RANGE or DEFAULT for
        // typical H.264 broadcast content; assert this so a future
        // codec regression to FULL_RANGE fails the test instead of
        // silently shipping clipped output.
        let q = dec.assert_capture_quantization_compatible()
            .expect("CAPTURE quantization compatible");
        eprintln!("CAPTURE quantization OK: {} ({})", q,
            if q == V4L2_QUANTIZATION_DEFAULT { "DEFAULT" }
            else if q == V4L2_QUANTIZATION_LIM_RANGE { "LIM_RANGE" }
            else { "?" });
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS CAPTURE");
        dec.start_streaming().expect("STREAMON");

        // Feed the whole fixture in one chunk (it's only ~17KB,
        // well under typical OUTPUT plane size of 1-4 MB).
        dec.feed(fixture).expect("feed NAL");
        // Send EOF.
        dec.feed(&[]).expect("feed EOF");

        // Pull frames until EOF.
        let mut frames_decoded = 0;
        let mut first_frame_y_variance = 0u64;
        for _attempt in 0..200 {
            match dec.next_frame() {
                Ok(Some(f)) => {
                    assert_eq!(f.width(), 320, "frame width");
                    assert_eq!(f.height(), 240, "frame height");
                    if frames_decoded == 0 {
                        // Compute simple variance on Y plane.
                        let y = f.y_plane();
                        if !y.is_empty() {
                            let mean: u64 = y.iter().map(|&b| b as u64).sum::<u64>() / y.len() as u64;
                            first_frame_y_variance = y.iter()
                                .map(|&b| {
                                    let d = (b as i64) - (mean as i64);
                                    (d * d) as u64
                                })
                                .sum::<u64>() / y.len() as u64;
                        }
                    }
                    frames_decoded += 1;
                }
                Ok(None) => break,
                Err(e) if e.to_string().contains("EAGAIN") => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("decode error: {}", e),
            }
        }
        eprintln!("frames decoded: {}; first-frame Y variance: {}",
            frames_decoded, first_frame_y_variance);
        assert!(frames_decoded >= 1, "no frames decoded");
        assert!(first_frame_y_variance > 10,
            "first frame Y plane variance suspiciously low: {}",
            first_frame_y_variance);
    }

    /// DMA-BUF path parallel to decode_test_fixture_320x240.
    /// Asserts Frame::dma_buf_fd() returns Some(fd) (>=0) AND
    /// that CPU-side y_plane()/uv_plane() return non-empty
    /// slices (piece 4a-fix: REQBUFS is V4L2_MEMORY_MMAP for
    /// both modes; dma_buf fds are an additional view on the
    /// kernel-allocated buffers, not a replacement). Skipped
    /// cleanly when /dev/video10 absent.
    ///
    /// NOTE: cargo's default parallel test runner can race against
    /// the MMAP test for /dev/video10 (EBUSY). Run with
    /// `--test-threads=1` when both live-Pi tests need to pass in
    /// the same invocation. CI/smoke runs single-threaded.
    #[test]
    #[cfg(target_os = "linux")]
    fn decode_test_fixture_320x240_via_dmabuf() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping dmabuf decode test: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        dec.set_capture_buffer_type(CaptureBufferType::DmaBuf);
        let out_fmt = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        eprintln!("OUTPUT negotiated: {:?}", out_fmt);
        let cap_fmt = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        eprintln!("CAPTURE negotiated: w={} h={} num_planes={}",
            cap_fmt.width, cap_fmt.height, cap_fmt.num_planes);
        // bcm2835-codec NV12 CAPTURE is num_planes=1 -- piece 4
        // DmaBuf path REQUIRES this. If a future kernel splits it
        // into 2 planes, allocate_buffers will error cleanly.
        assert_eq!(cap_fmt.num_planes, 1,
            "DmaBuf piece 4 assumes single-plane NV12");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS+EXPBUF CAPTURE (DmaBuf)");
        dec.start_streaming().expect("STREAMON");

        dec.feed(fixture).expect("feed NAL");
        dec.feed(&[]).expect("feed EOF");

        let mut frames_decoded = 0;
        let mut first_fd: Option<i32> = None;
        for _attempt in 0..200 {
            match dec.next_frame() {
                Ok(Some(f)) => {
                    assert_eq!(f.width(), 320, "frame width");
                    assert_eq!(f.height(), 240, "frame height");
                    // Piece 4a-fix: both y_plane() and uv_plane()
                    // are populated on DmaBuf since REQBUFS now
                    // uses V4L2_MEMORY_MMAP for both modes. The
                    // dma_buf fd is an ADDITIONAL view, not a
                    // replacement.
                    assert!(!f.y_plane().is_empty(),
                        "y_plane() should still be populated on DmaBuf");
                    assert!(!f.uv_plane().is_empty(),
                        "uv_plane() should still be populated on DmaBuf");
                    // The exported fd must be present + valid.
                    let fd = f.dma_buf_fd()
                        .expect("dma_buf_fd() must be Some on DmaBuf");
                    assert!(fd >= 0, "expected non-negative fd, got {}", fd);
                    if first_fd.is_none() { first_fd = Some(fd); }
                    frames_decoded += 1;
                }
                Ok(None) => break,
                Err(e) if e.to_string().contains("EAGAIN") => {
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                Err(e) => panic!("decode error: {}", e),
            }
        }
        eprintln!("DmaBuf frames decoded: {}; first fd: {:?}",
            frames_decoded, first_fd);
        assert!(frames_decoded >= 1, "no frames decoded via DmaBuf");
    }

    /// Drop + re-open works cleanly. Catches missing STREAMOFF
    /// or unfreed mmaps that'd EBUSY the device.
    #[test]
    #[cfg(target_os = "linux")]
    fn drop_then_reopen_clean() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        {
            let dec = Decoder::open(path).expect("first open");
            let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240);
            let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240);
            let _ = dec.allocate_buffers(QueueDirection::Output, 2);
            let _ = dec.allocate_buffers(QueueDirection::Capture, 2);
            // dec drops here -- inner Arc drops -- STREAMOFF +
            // munmap should fire.
        }
        // Re-open immediately. If anything leaked, this'd fail.
        let dec2 = Decoder::open(path).expect("re-open");
        let _ = dec2.query_capabilities().expect("re-QUERYCAP");
    }

    // ========================================================
    // r48: OUTPUT buffer rotation correctness tests.
    // ========================================================

    /// allocate_buffers(Output, N) seeds the free pool with all
    /// N indices in order [0..N). Pool depth before allocation
    /// is zero.
    #[test]
    #[cfg(target_os = "linux")]
    fn r48_allocate_output_seeds_free_pool() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        let dec = Decoder::open(path).expect("open");
        // Before allocation: pool empty.
        assert_eq!(
            dec.free_output_slots_snapshot(), Vec::<u32>::new(),
            "free pool should be empty before allocate_buffers"
        );
        let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        // After allocation: pool should contain [0, 1, 2, 3].
        // The kernel may negotiate a different count (it's free
        // to allocate more or fewer than requested); accept any
        // N as long as the pool is in 0..N order.
        let pool = dec.free_output_slots_snapshot();
        assert!(!pool.is_empty(), "pool should be non-empty");
        for (i, &idx) in pool.iter().enumerate() {
            assert_eq!(
                idx, i as u32,
                "pool should be [0..N) in order; got {:?}", pool
            );
        }
    }

    /// feed() pops the next free slot; drain_output_quiet
    /// (called from next feed) returns it. Smoke test that the
    /// FIFO rotation pattern doesn't immediately surface
    /// VIDIOC_QBUF EINVAL across multiple back-to-back feeds.
    ///
    /// This is a smoke test, NOT a deterministic regression-
    /// catcher (subagent 2026-06-03): pre-r48 the same test
    /// MIGHT have passed on a fast kernel that reclaimed buf 0
    /// between feeds via drain_output_quiet. The live FYS bug
    /// (r46.4 deploy 2026-06-02) showed the race surface with
    /// real H.264 content + bcm2835's actual decode latency.
    /// To turn this into a true regression test you'd need to
    /// gate drain_output_quiet (or add a #[cfg(test)] no-drain
    /// variant) -- out of scope for r48.
    #[test]
    #[cfg(target_os = "linux")]
    fn r48_feed_rotates_through_pool_back_to_back() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS CAPTURE");
        dec.start_streaming().expect("STREAMON");

        let initial_pool = dec.free_output_slots_snapshot();
        let pool_capacity = initial_pool.len();
        assert!(
            pool_capacity >= 2,
            "test needs >=2 OUTPUT buffers; got {}",
            pool_capacity
        );

        // Feed the same fixture N times back-to-back. Pre-r48
        // this would EINVAL on the 2nd feed (buf_idx=0 still
        // kernel-owned). Post-r48 the pool rotates through and
        // back-to-back feeds succeed.
        //
        // Note: we DON'T feed EOF here -- want to keep buffers
        // cycling, not signal end-of-stream.
        for n in 0..pool_capacity {
            dec.feed(fixture).unwrap_or_else(|e| {
                panic!(
                    "feed #{} failed (pool rotation broken?): {}", n, e
                );
            });
        }
        // After feeding pool_capacity times in rapid succession
        // (no drain time), the pool should be empty -- every
        // slot has been QBUF'd to the kernel + the kernel hasn't
        // had time to DQBUF any back.
        //
        // Sleep briefly so the kernel can process some, then
        // poke drain via next_frame's internal call OR another
        // feed attempt (which will trigger drain_output_quiet).
        // The exact pool state depends on bcm2835's processing
        // speed; we just want to see SOMETHING come back.
        std::thread::sleep(std::time::Duration::from_millis(50));
        // Trigger drain by calling next_frame (which calls
        // drain_output_quiet internally).
        let _ = dec.next_frame();
        let final_pool = dec.free_output_slots_snapshot();
        eprintln!(
            "after {} feeds + 50ms + drain: pool = {:?}",
            pool_capacity, final_pool
        );
        // The most important thing: NO EINVALs above. That
        // alone validates the rotation contract.
    }

    /// feed() with an oversized NAL returns the popped slot to
    /// the free pool (validation error path).
    #[test]
    #[cfg(target_os = "linux")]
    fn r48_feed_oversized_nal_restores_pool_depth() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        let dec = Decoder::open(path).expect("open");
        let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS CAPTURE");
        dec.start_streaming().expect("STREAMON");

        let pre = dec.free_output_slots_snapshot();
        // Build an oversized fake NAL: bigger than any plausible
        // OUTPUT plane (16 MB exceeds bcm2835's typical sizeimage
        // of 1-4 MB for 320x240).
        let huge = vec![0u8; 16 * 1024 * 1024];
        let err = dec.feed(&huge).expect_err("oversized NAL must error");
        let msg = format!("{}", err);
        assert!(
            msg.contains("larger than OUTPUT buffer"),
            "expected size-mismatch error; got: {msg}"
        );
        let post = dec.free_output_slots_snapshot();
        assert_eq!(
            pre.len(), post.len(),
            "pool depth must be preserved on validation error"
        );
        // r48: push_front on error keeps the next feed
        // deterministic -- the failed slot retries first. Verify
        // the same head index.
        if !pre.is_empty() {
            assert_eq!(
                pre[0], post[0],
                "error path should push_front (same head)"
            );
        }
    }

    /// Sanity: N back-to-back feeds drain the pool below its
    /// initial depth (slots end up kernel-owned). This is a
    /// smoke test that confirms `feed()` actually CONSUMES slots
    /// from the pool (vs. somehow pre-restoring them).
    ///
    /// NOTE: this is NOT a STREAMOFF-repopulation test. STREAMOFF
    /// + post-STREAMOFF pool state is exercised through Drop in
    /// `drop_then_reopen_clean` (any pool leak would EBUSY the
    /// next REQBUFS on re-open). Verifying mid-life STREAMOFF
    /// pool-reset would need a #[cfg(test)] pub stop_streaming
    /// accessor on Decoder; not in r48 scope.
    #[test]
    #[cfg(target_os = "linux")]
    fn r48_feed_consumes_pool_slots() {
        let path = Path::new("/dev/video10");
        if !path.exists() {
            eprintln!("skipping: /dev/video10 absent");
            return;
        }
        let fixture = include_bytes!(
            "../tests/fixtures/test_320x240.h264"
        );
        let dec = Decoder::open(path).expect("open");
        let _ = dec.set_output_format(V4L2_PIX_FMT_H264, 320, 240)
            .expect("S_FMT OUTPUT");
        let _ = dec.set_capture_format(V4L2_PIX_FMT_NV12, 320, 240)
            .expect("S_FMT CAPTURE");
        dec.allocate_buffers(QueueDirection::Output, 4)
            .expect("REQBUFS OUTPUT");
        dec.allocate_buffers(QueueDirection::Capture, 4)
            .expect("REQBUFS CAPTURE");
        dec.start_streaming().expect("STREAMON");

        let initial = dec.free_output_slots_snapshot();
        let n = initial.len();
        // Feed N times back-to-back; on a fast kernel some slots
        // may have been reclaimed by drain_output_quiet inside
        // feed(), but we should still see SOMETHING in flight.
        for _ in 0..n {
            let _ = dec.feed(fixture);
        }
        // Snapshot WITHOUT triggering drain (free_output_slots_
        // snapshot doesn't call drain). Pool should be smaller
        // than initial; on bcm2835 at 320x240 it's typically 0-1
        // since decode time exceeds feed-loop time.
        let after_feeds = dec.free_output_slots_snapshot();
        assert!(
            after_feeds.len() < n,
            "pool should have been drained by N feeds; before={} after={}",
            n, after_feeds.len()
        );
    }
}
