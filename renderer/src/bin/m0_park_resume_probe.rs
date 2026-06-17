//! M0 — headless V4L2 decoder park-and-resume probe.
//!
//! Per QA's M0 dispatch (2026-06-17):
//!   "Can the bcm2835/V4L2 decoder be opened → primed → produce
//!    1 CAPTURE frame → sit IDLE for a SHORT time → then resume
//!    and keep producing fresh frames cleanly? And where is the
//!    park-duration cliff?"
//!
//! This is the cheapest gate for the "warm the incoming decoder
//! early, park it, resume at the transition" idea: if SHORT park
//! (~200 ms) wedges the decoder, the whole approach is dead. If
//! short park is HEALTHY but a 5 s park wedges, we've found the
//! cliff and can budget the lead time accordingly.
//!
//! ## Reuse pattern
//!
//! Imports `crate::v4l2`, `crate::mp4_demux`, `crate::video_decode`
//! via `#[path]` so the lifecycle is the EXACT same one the
//! preload workers use (`prime_video_decoder_for_preload` +
//! `drain_one_capture_for_preload_with_detail`). No reimplementation
//! — this binary is purely a harness around the existing modules.
//!
//! The four PRIME_* constants are re-declared at this crate's
//! root because `video_decode.rs` does `pub use crate::{...}` to
//! pull them in; with this binary as a separate crate root, the
//! constants must live here. Values match `main.rs` (Phase B
//! warmup=2; PRIME_K_FLOOR_FOR_PRELOAD=4 = r77 hard guarantee).
//!
//! ## Run recipe (on fireplacesign, backend stopped, manual)
//!
//!   sudo systemctl stop openmarquee-backend
//!   for park_ms in 50 200 1000 5000 10000; do
//!     M0_PARK_MS=$park_ms /usr/local/bin/m0-park-resume-probe \
//!       /var/openmarquee/content/<uuid>/asset.mp4
//!   done
//!   sudo systemctl start openmarquee-backend  # restore
//!
//! ## VERDICT classification
//!
//! - HEALTHY: 30/30 fresh frames drained post-resume + 0
//!   non-EAGAIN errors. Park duration is safe at this M0_PARK_MS.
//! - DEGRADED: ≥ 15 fresh frames but not all 30 (decoder produces
//!   some output but with stalls / errors). Park is marginal.
//! - WEDGED: < 15 fresh frames. Park exceeded the cliff.
//!
//! EAGAIN polls are counted separately because they're expected
//! during the first few DQBUF attempts after resume (kernel needs
//! the freshly fed OUTPUT samples to traverse the codec before
//! CAPTURE frames are ready).

// Non-Linux stub: M0 probe needs V4L2 ioctls (Linux-only); compile a
// no-op main on macOS dev hosts so `cargo build` succeeds on the dev
// machine. The aarch64 cross-build produces the real binary.
#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!(
        "m0-park-resume-probe: V4L2 probe is Linux-only. Cross-build for \
         aarch64-unknown-linux-gnu and run on the Pi."
    );
}

// Re-declared so `crate::v4l2`, `crate::mp4_demux`, `crate::video_decode`
// resolve at this binary's crate root. main.rs has the same shape
// for the production binary; this stub mirrors only the V4L2
// lifecycle path.
#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_DEFAULT: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_FOR_PRELOAD: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_DEFAULT: usize = PRIME_WARMUP_DEFAULT + 1; // 3
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_FOR_PRELOAD: usize = PRIME_WARMUP_FOR_PRELOAD + 2; // 4 (r77 hard guarantee)

#[cfg(target_os = "linux")]
#[path = "../v4l2.rs"]
mod v4l2;
#[cfg(target_os = "linux")]
#[path = "../mp4_demux.rs"]
mod mp4_demux;
#[cfg(target_os = "linux")]
#[path = "../video_decode.rs"]
mod video_decode;
// frame_pacing carries `mark_renderer_startup` / `since_renderer_startup_ms`
// used by v4l2.rs telemetry. Empty no-op in this probe context.
#[cfg(target_os = "linux")]
#[path = "../frame_pacing.rs"]
mod frame_pacing;

#[cfg(target_os = "linux")]
use anyhow::{anyhow, Result};
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
fn main() -> Result<()> {
    // Mark startup so v4l2.rs's `since_restart_ms` telemetry isn't 0.
    frame_pacing::mark_renderer_startup();

    let park_ms: u64 = std::env::var("M0_PARK_MS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(200);

    let asset_path = std::env::args().nth(1).ok_or_else(|| {
        anyhow!(
            "usage: m0-park-resume-probe <asset.mp4>\n\
             env: M0_PARK_MS=<ms> (default 200)"
        )
    })?;
    let asset_path = std::path::PathBuf::from(asset_path);
    if !asset_path.is_file() {
        return Err(anyhow!("asset.mp4 not found at {}", asset_path.display()));
    }

    eprintln!(
        "[m0] start asset={} park_ms={}",
        asset_path.display(),
        park_ms,
    );

    // ---- Phase 1: warm setup ----------------------------------------------
    // Open MP4 + prime via the SAME path the preload worker uses.
    let t_warm = Instant::now();
    let dem = mp4_demux::Mp4Demuxer::open(&asset_path)
        .map_err(|e| anyhow!("Mp4Demuxer::open failed: {:#}", e))?;
    // Fixed UUID for the probe — uuid crate is included without the
    // "v4" feature in renderer/Cargo.toml; slide_id is purely cosmetic
    // for telemetry attribution so a known sentinel works.
    let slide_id = uuid::Uuid::from_bytes([
        0x4d, 0x30, 0xde, 0xad, 0xbe, 0xef, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]);
    eprintln!(
        "[m0] mp4_demux ok samples={} w={} h={} slide_id={}",
        dem.samples.len(),
        dem.width,
        dem.height,
        slide_id,
    );

    let mut state = video_decode::prime_video_decoder_for_preload(&dem, slide_id)
        .map_err(|e| anyhow!("prime_video_decoder_for_preload failed: {:#}", e))?;
    let warm_setup_us = t_warm.elapsed().as_micros();
    eprintln!(
        "[m0] warm setup done warm_setup_us={} frames_decoded={} next_sample_idx={}",
        warm_setup_us,
        state.frames_decoded,
        state.next_sample_idx,
    );

    // ---- Phase 2: park -----------------------------------------------------
    eprintln!("[m0] PARK begin sleeping {} ms", park_ms);
    std::thread::sleep(Duration::from_millis(park_ms));
    eprintln!("[m0] PARK end");

    // ---- Phase 3: resume + measure ----------------------------------------
    // Drive the same loop the bake hot path drives: feed samples,
    // DQBUF CAPTURE frames, count fresh ones, track EAGAIN/EINVAL/EPIPE
    // signatures. The post-resume race we're probing is: does
    // bcm2835 produce frames within a reasonable window after
    // M0_PARK_MS of idle?
    const TARGET_FRAMES: u32 = 30;
    const RESUME_DEADLINE_MS: u64 = 5000;
    let t_resume = Instant::now();
    let resume_deadline = t_resume + Duration::from_millis(RESUME_DEADLINE_MS);

    let mut first_fresh_us: Option<u128> = None;
    let mut fresh_count: u32 = 0;
    let mut eagain_polls: u32 = 0;
    let mut einval_errs: u32 = 0;
    let mut epipe_errs: u32 = 0;
    let mut other_errs: u32 = 0;
    let mut samples_fed: usize = 0;
    let mut eos_seen = false;

    while fresh_count < TARGET_FRAMES && Instant::now() < resume_deadline {
        // Feed the next sample if we have one. The decoder's
        // OUTPUT pool can hold a few in flight; we feed
        // opportunistically and let the kernel pace us via the
        // feed()'s internal QBUF retry.
        if state.next_sample_idx < dem.samples.len() {
            let sample_idx = state.next_sample_idx;
            let sample = &dem.samples[sample_idx];
            match state.decoder.feed(sample) {
                Ok(()) => {
                    state.next_sample_idx += 1;
                    samples_fed += 1;
                }
                Err(e) => {
                    let s = format!("{:#}", e);
                    eprintln!("[m0] feed err sample={} err={}", sample_idx, s);
                    if s.contains("EAGAIN") {
                        eagain_polls += 1;
                    } else if s.contains("EINVAL") {
                        einval_errs += 1;
                    } else if s.contains("EPIPE") {
                        epipe_errs += 1;
                    } else {
                        other_errs += 1;
                    }
                    // Don't advance next_sample_idx on err — retry.
                    // But break out of the feed attempt for this iteration
                    // and try a DQBUF instead.
                }
            }
        }

        // Try to drain a CAPTURE frame. next_frame() is the same
        // entrypoint the production paint loop uses.
        match state.decoder.next_frame() {
            Ok(Some(_frame)) => {
                if first_fresh_us.is_none() {
                    first_fresh_us = Some(t_resume.elapsed().as_micros());
                }
                fresh_count += 1;
                state.frames_decoded += 1;
                // Frame drops at end of this scope → re-QBUFs
                // automatically, keeping the pool drained.
            }
            Ok(None) => {
                eos_seen = true;
                eprintln!("[m0] EOS seen at fresh_count={}", fresh_count);
                break;
            }
            Err(e) => {
                let s = format!("{:#}", e);
                if s.contains("EAGAIN") {
                    eagain_polls += 1;
                } else if s.contains("EINVAL") {
                    einval_errs += 1;
                    eprintln!("[m0] dqbuf EINVAL: {}", s);
                } else if s.contains("EPIPE") {
                    epipe_errs += 1;
                    eprintln!("[m0] dqbuf EPIPE: {}", s);
                } else {
                    other_errs += 1;
                    eprintln!("[m0] dqbuf err: {}", s);
                }
                // Brief backoff so we don't spin too hard.
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    }

    let resume_us = first_fresh_us.unwrap_or_else(|| t_resume.elapsed().as_micros());
    let total_resume_us = t_resume.elapsed().as_micros();

    // ---- Phase 4: VERDICT ------------------------------------------------
    let verdict = if fresh_count >= TARGET_FRAMES && einval_errs == 0 && epipe_errs == 0 && other_errs == 0 {
        "HEALTHY"
    } else if fresh_count >= 15 {
        "DEGRADED"
    } else {
        "WEDGED"
    };

    println!(
        "[m0] PARK_MS={} warm_us={} resume_us={} total_resume_us={} \
         fresh={}/{} eagain={} einval={} epipe={} errors={} \
         samples_fed={} eos_seen={} VERDICT={}",
        park_ms,
        warm_setup_us,
        resume_us,
        total_resume_us,
        fresh_count,
        TARGET_FRAMES,
        eagain_polls,
        einval_errs,
        epipe_errs,
        other_errs,
        samples_fed,
        eos_seen,
        verdict,
    );

    // Decoder drops at end of main → r101 destroy-image-BEFORE-
    // close-fd path runs, MMAL component count decrements, clean
    // teardown.
    Ok(())
}
