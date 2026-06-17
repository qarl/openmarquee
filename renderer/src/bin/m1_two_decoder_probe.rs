//! M1 — headless TWO-decoder contention probe with pixel oracle.
//!
//! Per QA's M1 dispatch (2026-06-17): M0 proved single-decoder
//! park-and-resume is HEALTHY at every park duration 50 ms → 10 s,
//! killing the "parked decoder self-wedges from idle" hypothesis.
//! But M0 was narrow — it left two gaps M1 closes:
//!
//! 1. NO PIXEL ORACLE. M0's `fresh=30/30` only proved DQBUF
//!    returned kernel-valid buffers, NOT that pixels were
//!    correct/in-order/non-black — exactly the false-positive
//!    trap this project has hit before. M1 reads the Y-plane on
//!    every drained frame and runs `probe_oracle::PixelOracle` →
//!    catches "valid buffer, BLACK pixels" (Main/no-B encode
//!    requirement) AND "valid buffer, stuck pixels" (the frozen-
//!    incoming signature).
//!
//! 2. NO CONTENTION. M0 ran ONE decoder in isolation. The real
//!    transition freeze (r76: endpoint_b zero frames) is a
//!    TWO-decoder + shared-codec problem that M0 can't reproduce
//!    by construction. M1 stands up two decoders sharing the one
//!    bcm2835 codec.
//!
//! ## Shape (the real transition, isolated)
//!
//! - Decoder A ("outgoing"): open + prime, then drain STEADILY at
//!   ~30 fps cadence (1 feed + 1 next_frame per ~33 ms), running
//!   for the WHOLE test. Mimics the currently-playing slide.
//! - Decoder B ("incoming"): at `M1_B_OPEN_MS` (default 2 s),
//!   open + prime + drain-1 + PARK for `M0_PARK_MS` (default
//!   200 ms; sweep the same durations as M0); then RESUME while
//!   A is still draining.
//!
//! This is the transition moment: A live, B resuming, one shared
//! codec. The two MP4 paths come from `M1_VIDEO_A` and `M1_VIDEO_B`
//! env vars (different real reel videos avoid single-file caching
//! artifacts).
//!
//! ## Failure signatures captured (not just a VERDICT)
//!
//! - B on resume: persistent EAGAIN past the deadline → fresh<30
//!   = the real "endpoint_b zero frames."
//! - A once B opens/feeds: A's next_frame starts stalling/EAGAIN →
//!   "from-side starves → outgoing black."
//! - B's REQBUFS/EXPBUF errno WHILE A holds buffers → captured as
//!   the EXACT errno text in the prime err line (CMA exhaustion /
//!   codec-component limit).
//! - Combined A+B throughput vs 2×30 fps single-decoder rate →
//!   codec can't time-slice two sessions.
//! - Pixel oracle on EITHER side: black/stuck frames even when
//!   buffers are "valid."
//!
//! ## VERDICT classification
//!
//! HEALTHY only if BOTH sides:
//! - fresh_count >= TARGET_FRAMES
//! - pixel_ok == fresh_count (every drained buffer carried real,
//!   distinct, non-black pixels)
//! - 0 non-EAGAIN errors
//!
//! DEGRADED if both sides produced ≥15 fresh non-black frames but
//! one side missed the full target.
//!
//! WEDGED otherwise — A starved, B never resumed, or pixels were
//! garbage.
//!
//! ## Run recipe (on fireplacesign, backend stopped, manual)
//!
//!   sudo systemctl stop openmarquee-backend
//!   for park_ms in 50 200 1000 5000 10000; do
//!     M0_PARK_MS=$park_ms \
//!     M1_VIDEO_A=/var/openmarquee/content/<uuidA>/asset.mp4 \
//!     M1_VIDEO_B=/var/openmarquee/content/<uuidB>/asset.mp4 \
//!     /usr/local/bin/m1-two-decoder-probe
//!   done
//!   sudo systemctl start openmarquee-backend  # restore

// Non-Linux stub: V4L2 ioctls are Linux-only.
#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!(
        "m1-two-decoder-probe: V4L2 probe is Linux-only. Cross-build for \
         aarch64-unknown-linux-gnu and run on the Pi."
    );
}

#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_DEFAULT: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_WARMUP_FOR_PRELOAD: usize = 2;
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_DEFAULT: usize = PRIME_WARMUP_DEFAULT + 1; // 3
#[cfg(target_os = "linux")]
pub const PRIME_K_FLOOR_FOR_PRELOAD: usize = PRIME_WARMUP_FOR_PRELOAD + 2; // 4

#[cfg(target_os = "linux")]
#[path = "../v4l2.rs"]
mod v4l2;
#[cfg(target_os = "linux")]
#[path = "../mp4_demux.rs"]
mod mp4_demux;
#[cfg(target_os = "linux")]
#[path = "../video_decode.rs"]
mod video_decode;
#[cfg(target_os = "linux")]
#[path = "../frame_pacing.rs"]
mod frame_pacing;
#[cfg(target_os = "linux")]
#[path = "../probe_oracle.rs"]
mod probe_oracle;

#[cfg(target_os = "linux")]
use anyhow::{anyhow, Result};
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

#[cfg(target_os = "linux")]
fn main() -> Result<()> {
    frame_pacing::mark_renderer_startup();

    // Env config.
    let park_ms: u64 = std::env::var("M0_PARK_MS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(200);
    let b_open_ms: u64 = std::env::var("M1_B_OPEN_MS")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(2000);

    let video_a_path = std::env::var("M1_VIDEO_A").map_err(|_| {
        anyhow!(
            "M1_VIDEO_A env var required (path to outgoing asset.mp4)"
        )
    })?;
    let video_b_path = std::env::var("M1_VIDEO_B").map_err(|_| {
        anyhow!(
            "M1_VIDEO_B env var required (path to incoming asset.mp4 — distinct from A)"
        )
    })?;
    let video_a_path = std::path::PathBuf::from(video_a_path);
    let video_b_path = std::path::PathBuf::from(video_b_path);
    if !video_a_path.is_file() {
        return Err(anyhow!("M1_VIDEO_A not found at {}", video_a_path.display()));
    }
    if !video_b_path.is_file() {
        return Err(anyhow!("M1_VIDEO_B not found at {}", video_b_path.display()));
    }

    eprintln!(
        "[m1] start A={} B={} park_ms={} b_open_ms={}",
        video_a_path.display(),
        video_b_path.display(),
        park_ms,
        b_open_ms,
    );

    // ---- Phase 1: open + prime A (the outgoing slide) ----------------------
    let t_a_warm = Instant::now();
    let dem_a = mp4_demux::Mp4Demuxer::open(&video_a_path)
        .map_err(|e| anyhow!("Mp4Demuxer::open A failed: {:#}", e))?;
    let slide_a_id = uuid::Uuid::from_bytes([
        0x4d, 0x31, 0xaa, 0xaa, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]);
    let mut state_a = video_decode::prime_video_decoder_for_preload(&dem_a, slide_a_id)
        .map_err(|e| anyhow!("prime A failed: {:#}", e))?;
    let a_warm_us = t_a_warm.elapsed().as_micros();
    eprintln!(
        "[m1] A primed warm_us={} samples={} w={} h={}",
        a_warm_us,
        dem_a.samples.len(),
        dem_a.width,
        dem_a.height,
    );

    // Per-side counters + pixel oracle.
    let mut a_oracle = probe_oracle::PixelOracle::new();
    let mut a_fresh: u32 = 0;
    let mut a_eagain: u32 = 0;
    let mut a_other_errs: u32 = 0;
    let mut a_samples_fed: usize = 0;
    let mut b_oracle = probe_oracle::PixelOracle::new();
    let mut b_fresh: u32 = 0;
    let mut b_eagain: u32 = 0;
    let mut b_einval: u32 = 0;
    let mut b_epipe: u32 = 0;
    let mut b_other_errs: u32 = 0;
    let mut b_samples_fed: usize = 0;
    let mut b_warm_us: u128 = 0;
    let mut b_resume_us: u128 = 0;
    let mut b_open_errno: String = String::new();

    // A's frames-drained snapshot at the moment B's resume completes
    // — used to compute A's throughput "during" B's resume.
    let mut a_fresh_at_b_resume_start: u32 = 0;
    let mut a_fresh_at_b_resume_end: u32 = 0;

    // Phase machine.
    enum Phase {
        ASoloPreOpen,    // A draining, B not yet opened
        BOpening,        // about to open B (one-shot)
        BParked,         // B opened+primed+drained-1; sleeping
        BResuming,       // B feeding+DQBUFing toward TARGET
        Drain,           // B target reached; A continues briefly then end
    }
    let mut phase = Phase::ASoloPreOpen;
    let t_start = Instant::now();
    let b_open_deadline = t_start + Duration::from_millis(b_open_ms);
    let mut park_until: Option<Instant> = None;
    let mut b_resume_start: Option<Instant> = None;
    let mut state_b: Option<video_decode::VideoDecoderState> = None;
    let dem_b = mp4_demux::Mp4Demuxer::open(&video_b_path)
        .map_err(|e| anyhow!("Mp4Demuxer::open B failed: {:#}", e))?;
    eprintln!(
        "[m1] B mp4 parsed (open deferred to phase) samples={} w={} h={}",
        dem_b.samples.len(), dem_b.width, dem_b.height,
    );
    let slide_b_id = uuid::Uuid::from_bytes([
        0x4d, 0x31, 0xbb, 0xbb, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    ]);

    const TARGET_FRAMES: u32 = 30;
    const A_MIN_FRAMES_POST_B_RESUME: u32 = 30; // A must keep producing through B's resume
    const TICK_NS: u64 = 33_333_333; // 30 fps
    const TOTAL_DEADLINE_MS: u64 = 20_000;
    let total_deadline = t_start + Duration::from_millis(TOTAL_DEADLINE_MS);

    while Instant::now() < total_deadline {
        let tick_start = Instant::now();

        // ---- A: always feed + DQBUF one frame per tick. ------------------
        if state_a.next_sample_idx < dem_a.samples.len() {
            match state_a.decoder.feed(&dem_a.samples[state_a.next_sample_idx]) {
                Ok(()) => {
                    state_a.next_sample_idx += 1;
                    a_samples_fed += 1;
                }
                Err(e) => {
                    let s = format!("{:#}", e);
                    if s.contains("EAGAIN") {
                        a_eagain += 1;
                    } else {
                        a_other_errs += 1;
                        eprintln!("[m1] A feed err: {}", s);
                    }
                }
            }
        }
        match state_a.decoder.next_frame() {
            Ok(Some(frame)) => {
                a_fresh += 1;
                state_a.frames_decoded += 1;
                a_oracle.check(frame.y_plane());
            }
            Ok(None) => {
                eprintln!("[m1] A EOS at fresh={}", a_fresh);
            }
            Err(e) => {
                let s = format!("{:#}", e);
                if s.contains("EAGAIN") {
                    a_eagain += 1;
                } else {
                    a_other_errs += 1;
                    eprintln!("[m1] A dqbuf err: {}", s);
                }
            }
        }

        // ---- B: phase-machine driven. -----------------------------------
        match phase {
            Phase::ASoloPreOpen => {
                if Instant::now() >= b_open_deadline {
                    phase = Phase::BOpening;
                }
            }
            Phase::BOpening => {
                // The critical contention point — B's open + REQBUFS +
                // EXPBUF + STREAMON + initial feed happens WHILE A holds
                // its allocated CAPTURE/OUTPUT pools and is actively
                // submitting OUTPUT samples.
                eprintln!(
                    "[m1] OPENING B at t={} ms a_fresh_so_far={}",
                    t_start.elapsed().as_millis(),
                    a_fresh,
                );
                let t_b_open = Instant::now();
                match video_decode::prime_video_decoder_for_preload(&dem_b, slide_b_id) {
                    Ok(sb) => {
                        state_b = Some(sb);
                        b_warm_us = t_b_open.elapsed().as_micros();
                        eprintln!(
                            "[m1] B primed warm_us={} (B parking for {} ms)",
                            b_warm_us, park_ms,
                        );
                        park_until = Some(Instant::now() + Duration::from_millis(park_ms));
                        phase = Phase::BParked;
                    }
                    Err(e) => {
                        // Capture the EXACT errno text per QA's "house rule".
                        let s = format!("{:#}", e);
                        b_open_errno = s.clone();
                        eprintln!("[m1] B prime FAILED: {}", s);
                        b_other_errs += 1;
                        // Move to Drain — there's no B to resume.
                        phase = Phase::Drain;
                    }
                }
            }
            Phase::BParked => {
                if let Some(p_until) = park_until {
                    if Instant::now() >= p_until {
                        eprintln!(
                            "[m1] B PARK end → RESUME at t={} ms a_fresh_so_far={}",
                            t_start.elapsed().as_millis(),
                            a_fresh,
                        );
                        b_resume_start = Some(Instant::now());
                        a_fresh_at_b_resume_start = a_fresh;
                        phase = Phase::BResuming;
                    }
                }
            }
            Phase::BResuming => {
                // Feed one + DQBUF one per tick. Mirror M0's resume loop.
                if let Some(state) = state_b.as_mut() {
                    if state.next_sample_idx < dem_b.samples.len() {
                        match state.decoder.feed(&dem_b.samples[state.next_sample_idx]) {
                            Ok(()) => {
                                state.next_sample_idx += 1;
                                b_samples_fed += 1;
                            }
                            Err(e) => {
                                let s = format!("{:#}", e);
                                if s.contains("EAGAIN") {
                                    b_eagain += 1;
                                } else if s.contains("EINVAL") {
                                    b_einval += 1;
                                } else if s.contains("EPIPE") {
                                    b_epipe += 1;
                                } else {
                                    b_other_errs += 1;
                                    eprintln!("[m1] B feed err: {}", s);
                                }
                            }
                        }
                    }
                    match state.decoder.next_frame() {
                        Ok(Some(frame)) => {
                            if b_fresh == 0 {
                                b_resume_us = b_resume_start
                                    .map(|t| t.elapsed().as_micros())
                                    .unwrap_or(0);
                            }
                            b_fresh += 1;
                            state.frames_decoded += 1;
                            b_oracle.check(frame.y_plane());
                        }
                        Ok(None) => {
                            eprintln!("[m1] B EOS at b_fresh={}", b_fresh);
                            a_fresh_at_b_resume_end = a_fresh;
                            phase = Phase::Drain;
                        }
                        Err(e) => {
                            let s = format!("{:#}", e);
                            if s.contains("EAGAIN") {
                                b_eagain += 1;
                            } else if s.contains("EINVAL") {
                                b_einval += 1;
                            } else if s.contains("EPIPE") {
                                b_epipe += 1;
                            } else {
                                b_other_errs += 1;
                                eprintln!("[m1] B dqbuf err: {}", s);
                            }
                        }
                    }
                    if b_fresh >= TARGET_FRAMES {
                        a_fresh_at_b_resume_end = a_fresh;
                        phase = Phase::Drain;
                    }
                } else {
                    // Defensive — should never happen (BParked sets state_b).
                    phase = Phase::Drain;
                }
            }
            Phase::Drain => {
                // Let A get a few more frames so we have throughput
                // data POST-B-resume. End once A reaches its
                // post-resume minimum or we hit the total deadline.
                let post = a_fresh - a_fresh_at_b_resume_end;
                if post >= A_MIN_FRAMES_POST_B_RESUME {
                    break;
                }
            }
        }

        // 30 fps cadence: sleep until the next tick.
        let elapsed = tick_start.elapsed().as_nanos() as u64;
        if elapsed < TICK_NS {
            std::thread::sleep(Duration::from_nanos(TICK_NS - elapsed));
        }
    }

    // ---- VERDICT ----------------------------------------------------------
    let a_during_b_resume = a_fresh_at_b_resume_end.saturating_sub(a_fresh_at_b_resume_start);
    let b_succeeded = !b_open_errno.is_empty()
        || (b_fresh >= TARGET_FRAMES
            && b_oracle.pixel_ok == b_fresh
            && b_oracle.black == 0
            && b_einval == 0
            && b_epipe == 0
            && b_other_errs == 0);
    let a_clean = a_other_errs == 0 && a_oracle.pixel_ok == a_fresh && a_oracle.black == 0;
    let a_kept_producing = a_during_b_resume >= 10; // at least ~10 frames decoded by A during B's resume
    let verdict = if b_open_errno.is_empty() && b_succeeded && a_clean && a_kept_producing {
        "HEALTHY"
    } else if b_fresh >= 15 && a_fresh >= 30 && b_oracle.pixel_ok >= 15 {
        "DEGRADED"
    } else {
        "WEDGED"
    };

    println!(
        "[m1] PARK_MS={} B_OPEN_MS={} \
         a_warm_us={} a_fresh={} a_samples_fed={} a_eagain={} a_other_errs={} {} \
         b_warm_us={} b_resume_us={} b_fresh={}/{} b_samples_fed={} \
         b_eagain={} b_einval={} b_epipe={} b_other_errs={} {} \
         a_during_b_resume={} b_open_errno=\"{}\" VERDICT={}",
        park_ms,
        b_open_ms,
        a_warm_us,
        a_fresh,
        a_samples_fed,
        a_eagain,
        a_other_errs,
        a_oracle.report("a_y"),
        b_warm_us,
        b_resume_us,
        b_fresh,
        TARGET_FRAMES,
        b_samples_fed,
        b_eagain,
        b_einval,
        b_epipe,
        b_other_errs,
        b_oracle.report("b_y"),
        a_during_b_resume,
        b_open_errno,
        verdict,
    );

    Ok(())
}
