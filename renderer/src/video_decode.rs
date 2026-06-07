//! V4L2 H.264 decoder priming + per-slide state — shared between the
//! IPC sidecar (`ipc_main.rs`) and the standalone `--play-reel`
//! driver (`hdmi.rs`).
//!
//! QA H2 (2026-05-23): the standalone reel was holding Video slots
//! with a black-screen sleep because the IPC sidecar's V4L2 plumbing
//! lived as private items in `ipc_main.rs`. Lifted here so both
//! callers can dispatch identically; ipc_main now imports from
//! `crate::video_decode::*` and the reel does too.
//!
//! Linux-only by construction — V4L2 is a Linux kernel ABI. On
//! non-Linux the module compiles down to an empty cfg-guarded shell;
//! all callers gate their use sites by `#[cfg(target_os = "linux")]`
//! anyway, mirroring the rest of the V4L2 pieces.

#![cfg(target_os = "linux")]

use anyhow::{anyhow, Context, Result};

use crate::mp4_demux::Mp4Demuxer;
use crate::v4l2;

/// V4L2 piece 3c (2026-05-14): `/dev/video10` is the bcm2835-codec
/// decode-side node on Raspberry Pi (verified via piece 1 inventory
/// and piece 2b live decode). Hardcoded for now; a future settings
/// surface might let operators override on different SoCs.
pub const V4L2_DECODER_PATH: &str = "/dev/video10";

/// V4L2 H.264 decoder state cached per VideoSlide. The IPC sidecar
/// holds one per slide in `cache.video_decoders`; the standalone
/// reel driver constructs one per video slide and drops it at the
/// end of the slide's hold.
///
/// `next_sample_idx` is the index of the next sample (in the
/// demuxer's `samples` Vec) to feed on the next paint tick.
/// `prime_video_decoder` primes by feeding sample 0 (the IDR + any
/// pre-IDR NALs) so after a fresh prime this is `1`.
///
/// `frames_decoded` is incremented by
/// `paint_and_present_one_video_slide_frame` after a successful
/// blit+swap. Used for first-frame logging and end-of-slide
/// frame-count diagnostics.
///
/// `capture_w`/`capture_h` are the codec's negotiated capture
/// dimensions (may differ from the input dims via codec
/// width/height alignment). Caller-visible so the paint helper
/// sizes the GLES texture upload at exactly the codec's stride —
/// off-stride uploads produce spurious right-edge garbage on
/// non-aligned widths.
pub struct VideoDecoderState {
    pub decoder: v4l2::Decoder,
    pub next_sample_idx: usize,
    pub frames_decoded: usize,
    pub capture_w: u32,
    pub capture_h: u32,
}

impl VideoDecoderState {
    /// Convenience for "did we make progress this tick?" — returns
    /// the current `frames_decoded` counter (incremented by the
    /// paint helper on success).
    pub fn frames_decoded_for_log(&self) -> usize {
        self.frames_decoded
    }
}

/// Open + prime a V4L2 H.264 decoder against the Pi's bcm2835-codec
/// for a given `Mp4Demuxer`'s stream. Returns a `VideoDecoderState`
/// ready for the per-frame paint helper to drain.
///
/// Priming sequence (per bcm2835-codec / V4L2 M2M MPLANE recipe):
///   1. `Decoder::open("/dev/video10")`
///   2. `set_output_format(H264, w, h)` — compressed-in queue
///   3. `set_capture_format(NV12, w, h)` — decoded-out queue;
///      negotiated dims may differ from the request (codec rounds
///      to its alignment).
///   4. `allocate_buffers(OUTPUT, 4)` + `allocate_buffers(CAPTURE, 4)`
///   5. `start_streaming()` — STREAMON OUTPUT then CAPTURE
///   6. `feed(sps_pps_annexb + sample[0])` — one concatenated feed
///      (per the proven-working recipe in
///      `v4l2::tests::decode_test_fixture_320x240`; back-to-back
///      `feed` calls collide on OUTPUT buffer index 0).
///
/// Failure at any step bubbles a `WithContext` error; callers either
/// swallow to a black-hold + warn (the standalone reel) or emit
/// to the cache.load warn channel (the IPC sidecar).
///
/// V4L2 piece 4d (2026-05-14): opt-in DMA-BUF zero-copy path via
/// `OPENMARQUEE_RENDERER_DMABUF=1`. Piece 4e smoke shipped GREEN
/// (qa/captures/v4l2-piece4e-dmabuf-smoke-2026-05-14.md, 6.3× mean
/// / 9.1× p50 improvement vs MMAP), but the default remained MMAP
/// pending a separate flip decision. Set BEFORE `allocate_buffers`
/// so REQBUFS uses the right memory type.
// r73 (2026-06-06): warmup-count constants live at the binary
// crate root (`main.rs`) so cross-platform tests can pin them on
// the macOS dev host where the rest of this Linux-only module
// doesn't compile in. Re-exported here as `pub use` so existing
// `crate::video_decode::PRIME_WARMUP_*` call sites keep working.
pub use crate::{PRIME_WARMUP_DEFAULT, PRIME_WARMUP_FOR_PRELOAD};

// ====================================================================
// r78 Phase A (2026-06-08): MP4 sample classification probe.
//
// r77 telemetry from FYS: warmup=2 still produced frames_drained=0 on
// ALL 8 preloads in a 90s soak. r77 also showed that 4x'ing the drain
// budget (500ms -> 2000ms) didn't help either: input-starvation, not
// time-starvation.
//
// Dispatch hypothesis to verify (Phase A probe, no fix): the MP4's
// sample[0] may not be an IDR. H.264 NAL unit types (NAL header byte
// & 0x1f):
//   1  = non-IDR coded slice (P or B)
//   5  = IDR slice (random-access point)
//   7  = SPS
//   8  = PPS
// If sample 0 is type 1 (non-IDR) the decoder CANNOT decode it
// without seeking back to the prior IDR -- which the demuxer doesn't
// do today. Result: no decoded CAPTURE output regardless of how many
// warmup samples we feed.
//
// This probe parses the NAL header from each of the first N samples
// and logs the type so QA can see whether the demuxer is starting
// from an IDR.
// ====================================================================

/// r78 subagent BLOCKER-1 fix: x264 (and most H.264 encoders) prefix
/// the IDR slice with AUD (nal_unit_type=9) and/or SEI (type=6) NALs
/// concatenated into the same Annex-B sample. So `sample[0]`'s FIRST
/// NAL header byte is typically AUD/SEI, NOT the IDR. Reading only
/// the first NAL would report `is_idr=false` on every real MP4 and
/// give QA a false-positive demuxer indictment.
///
/// This walks ALL Annex-B NAL units inside the sample (same logic as
/// `mp4_demux.rs` uses for its own classification) and returns:
///   - `first_nal_type`: the FIRST NAL header byte's type (for context)
///   - `contains_idr`: whether ANY NAL in the sample is type 5 (IDR slice)
fn walk_h264_nal_types(sample: &[u8]) -> (Option<u8>, bool) {
    let mut first_nal_type: Option<u8> = None;
    let mut contains_idr = false;
    let mut i: usize = 0;
    while i + 4 <= sample.len() {
        // Recognize 4-byte (`00 00 00 01`) and 3-byte (`00 00 01`)
        // Annex-B start codes.
        let header_idx = if sample[i] == 0 && sample[i + 1] == 0 && sample[i + 2] == 0 && sample[i + 3] == 1 {
            i + 4
        } else if sample[i] == 0 && sample[i + 1] == 0 && sample[i + 2] == 1 {
            i + 3
        } else {
            i += 1;
            continue;
        };
        if header_idx >= sample.len() {
            break;
        }
        let nut = sample[header_idx] & 0x1f;
        if first_nal_type.is_none() {
            first_nal_type = Some(nut);
        }
        if nut == 5 {
            contains_idr = true;
        }
        i = header_idx + 1;
    }
    (first_nal_type, contains_idr)
}

/// r78 Phase A: emit a `[perf] preload_sample_dump` line per sample
/// classification so QA can see whether the demuxer is delivering a
/// decodable starting sample (one containing an IDR NAL).
fn log_first_samples_nal_types(label: &str, slide_id: &str, samples: &[Vec<u8>], max: usize) {
    for (i, s) in samples.iter().enumerate().take(max) {
        let (first_nal, contains_idr) = walk_h264_nal_types(s);
        let first_str = first_nal.map(|n| n.to_string()).unwrap_or_else(|| "unknown".into());
        eprintln!(
            "[perf] preload_sample_dump label={} slide_id={} i={} size={} first_nal_type={} contains_idr={}",
            label, slide_id, i, s.len(), first_str, contains_idr,
        );
    }
}

/// Cold-start prime: delegates to `prime_video_decoder_with_warmup`
/// with `PRIME_WARMUP_DEFAULT`. Kept as the public entry point so
/// existing call sites don't have to specify the count.
pub fn prime_video_decoder(dem: &Mp4Demuxer) -> Result<VideoDecoderState> {
    // r78 subagent BLOCKER-2: cross-check probe lives at the
    // synchronous BeginSlide call site in ipc_main.rs, NOT here.
    // This function is also reached from the standalone reel
    // (hdmi.rs:3123) and tests; firing the probe here would flood
    // unrelated journals + cargo test stderr, masking the actual
    // BeginSlide signal QA wants.
    prime_video_decoder_with_warmup(dem, PRIME_WARMUP_DEFAULT)
}

/// r78 Phase A: callable probe for the synchronous BeginSlide path's
/// cross-check. ipc_main.rs invokes this right before calling
/// `prime_video_decoder(&dem)` so QA gets one A/B line per
/// synchronous BeginSlide. Reel + tests don't call this so they
/// stay quiet.
pub fn log_synchronous_begin_slide_sample_dump(slide_id: uuid::Uuid, dem: &Mp4Demuxer) {
    log_first_samples_nal_types(
        "cold_start", &slide_id.to_string(), &dem.samples, 4,
    );
}

pub fn prime_video_decoder_with_warmup(
    dem: &Mp4Demuxer,
    warmup_count_requested: usize,
) -> Result<VideoDecoderState> {
    use std::path::Path;
    use std::time::Instant;
    // r56 Phase A (2026-06-03): sub-phase timing for the prime
    // path. qarl observed per-transition stalls in a 4-video-slide
    // playlist; the "ipc: opened MP4" event lands AT the slide
    // boundary, suggesting decoder bring-up is synchronous on the
    // transition path. This single [perf] line breaks the cost
    // down so the eventual mitigation (pre-warm, kept-open,
    // first-frame cache, etc.) targets the right phase.
    let t_total = Instant::now();
    let path = Path::new(V4L2_DECODER_PATH);
    if !path.exists() {
        anyhow::bail!(
            "V4L2 decoder device {} does not exist (no codec driver loaded?)",
            V4L2_DECODER_PATH
        );
    }
    let t_device_open = Instant::now();
    let dec = v4l2::Decoder::open(path)
        .with_context(|| format!("open V4L2 decoder at {}", V4L2_DECODER_PATH))?;
    let device_open_us = t_device_open.elapsed().as_micros();
    let use_dmabuf = std::env::var("OPENMARQUEE_RENDERER_DMABUF")
        .ok()
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    if use_dmabuf {
        dec.set_capture_buffer_type(v4l2::CaptureBufferType::DmaBuf);
    }
    let w = dem.width as u32;
    let h = dem.height as u32;
    let t_s_fmt = Instant::now();
    let _out_fmt = dec
        .set_output_format(v4l2::V4L2_PIX_FMT_H264, w, h)
        .context("S_FMT OUTPUT (H264)")?;
    let cap_fmt = dec
        .set_capture_format(v4l2::V4L2_PIX_FMT_NV12, w, h)
        .context("S_FMT CAPTURE (NV12)")?;
    let s_fmt_us = t_s_fmt.elapsed().as_micros();
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
    let t_reqbufs = Instant::now();
    dec.allocate_buffers(v4l2::QueueDirection::Output, 4)
        .context("REQBUFS OUTPUT")?;
    dec.allocate_buffers(v4l2::QueueDirection::Capture, 4)
        .context("REQBUFS CAPTURE")?;
    let reqbufs_us = t_reqbufs.elapsed().as_micros();
    let t_streamon = Instant::now();
    dec.start_streaming().context("STREAMON")?;
    let streamon_us = t_streamon.elapsed().as_micros();
    // Feed the codec headers + first sample as a SINGLE concatenated
    // buffer. `v4l2::Decoder::feed` is single-shot-safe per its
    // docstring — back-to-back calls collide on OUTPUT buffer index 0
    // (the second feed clobbers the first before the kernel has
    // dequeued it). The proven-working recipe in
    // `v4l2::tests::decode_test_fixture_320x240` feeds the entire
    // Annex-B stream in one call; we mirror that.
    let first_sample = dem
        .samples
        .first()
        .ok_or_else(|| anyhow!("MP4 contains zero samples"))?;
    let header = dem.sps_pps_annexb();
    let mut primer: Vec<u8> = Vec::with_capacity(header.len() + first_sample.len());
    primer.extend_from_slice(&header);
    primer.extend_from_slice(first_sample);
    let t_primer = Instant::now();
    dec.feed(&primer).context("feed SPS+PPS+IDR primer")?;
    let primer_feed_us = t_primer.elapsed().as_micros();
    let mut next_sample_idx: usize = 1;

    // perf-night r5 (2026-05-26): warmup pre-feed -- push samples 1..N
    // into the decoder pipeline NOW so the kernel has B-frame lookahead
    // by the time the playback loop's first advance() ticks for this
    // slide. Without this, every video slide cold-started for ~10s
    // (operator-visible) because the decoder needs ~4 input samples
    // before its first decoded NV12 frame comes out. The PRE-r5 prime
    // fed only sample 0; advances 1..4 each fed one sample + waited the
    // 10ms EAGAIN budget = wasted ~10s per video slide start. r3
    // baseline journal: 'first frame painted (sample idx 4)' after a
    // 10670ms over-budget delta_ms.
    //
    // r57 (2026-06-04): the 4×6 ms `std::thread::sleep(6 ms)` calls
    // inside this loop have been removed. They were a workaround for
    // the pre-r48 single-OUTPUT-buffer race: feed() always used
    // buf_idx=0, so the second of two back-to-back feeds would QBUF
    // a buffer the kernel still owned and the ioctl returned EINVAL.
    // The sleeps gave the kernel ~6 ms to release the previous QBUF
    // before the next feed collided.
    //
    // r48 (2026-06-03) replaced the single-buffer hardcode with a
    // free-list rotation across all 4 OUTPUT buffers (`free_output_
    // slots: VecDeque` in DecoderInner; see v4l2.rs:618-634). Each
    // feed() now pop_front()s a different slot index, so back-to-
    // back feeds queue to different kernel-side slots and never
    // collide.
    //
    // r57 subagent (2026-06-04): warmup_count narrowed from 4 to 3
    // so the total feed sequence (1 primer + 3 warmup = 4) fits the
    // 4-slot OUTPUT pool with strict zero-wait math. Pre-fix the 5th
    // feed (4th warmup sample) depended on feed()'s 5×2 ms drain-
    // retry budget to find a slot, which is tight under CMA-pressured
    // worst-case kernel-side decode timing (~7-8 ms/sample). With
    // warmup_count=3 the worst case is strictly non-blocking. H.264
    // B-frame reorder distance is typically 3-4 frames, so 3 warmup
    // samples still fills the kernel's lookahead window — perf delta
    // vs warmup_count=4 is negligible in the steady-state advance
    // loop, which feeds one sample per tick from sample idx 4 onward.
    //
    // Pixel-perfect parity contract preserved: the samples fed and
    // their order are unchanged; only the timing of when sample N+1
    // arrives at the kernel changes (sooner). First-frame output is
    // bit-identical.
    let t_warmup = Instant::now();
    // r73 (2026-06-06): cap warmup against the OUTPUT pool size minus
    // one slot for the primer that already ran. Caller-requested count
    // is the upper bound; sample availability is the lower bound.
    let warmup_count = warmup_count_requested.min(dem.samples.len().saturating_sub(1));
    for _ in 0..warmup_count {
        let s = &dem.samples[next_sample_idx];
        match dec.feed(s) {
            Ok(()) => {
                next_sample_idx += 1;
            }
            Err(e) => {
                // Don't fail the whole prime on warmup error -- worst
                // case we revert to cold-start behavior. Log + bail
                // the warmup loop; caller still gets a usable decoder.
                eprintln!(
                    "warn: prime warmup feed sample {} failed: {} (continuing without warmup)",
                    next_sample_idx, e
                );
                break;
            }
        }
    }
    let warmup_us = t_warmup.elapsed().as_micros();

    let total_us = t_total.elapsed().as_micros();
    // r56 Phase A: one structured line per prime call. Keys ordered
    // by phase sequence; total_us includes the path-existence check
    // + dmabuf env-var read but those are sub-microsecond so total
    // ~= device_open + s_fmt + reqbufs + streamon + primer_feed
    // + warmup + assert_capture_quantization (the latter is
    // currently uninstrumented; ~few µs ioctl, lumped into
    // total - sum-of-named).
    eprintln!(
        "[perf] v4l2_prime device_open_us={} s_fmt_us={} reqbufs_us={} streamon_us={} primer_feed_us={} warmup_us={} total_us={} samples={} dims={}x{}",
        device_open_us,
        s_fmt_us,
        reqbufs_us,
        streamon_us,
        primer_feed_us,
        warmup_us,
        total_us,
        dem.samples.len(),
        dem.width,
        dem.height,
    );

    Ok(VideoDecoderState {
        decoder: dec,
        next_sample_idx,
        frames_decoded: 0,
        capture_w: cap_fmt.width,
        capture_h: cap_fmt.height,
    })
}

// ====================================================================
// r76 Phase B (2026-06-07): preload-path CAPTURE drain.
//
// r76 Phase A telemetry from FYS: 47/47 transitions showed
// `transition_endpoint_b_unconsumed` -- endpoint_b's V4L2 decoder
// produced ZERO frames within the entire transition window. Even
// though r73 dropped warmup_count to 1 (specifically so CAPTURE
// wouldn't saturate during the idle window), the preload worker
// never DQBUFs CAPTURE, so the kernel-side pipeline never
// progresses past the initial queue. Possible failure modes:
//
//   1. Single warmup sample is a B-frame depending on a future
//      P-frame -> kernel can only decode the IDR primer (1 frame),
//      then waits. bake_b's first next_frame() should still get
//      that 1 frame -- but if it doesn't, see below.
//
//   2. bcm2835-codec backpressure: after warmup sample queued, the
//      driver needs userspace to DQBUF SOMETHING before it'll
//      stream further. Without the drain in worker, by the time
//      transition fires the driver is in a wait-for-userspace
//      state. (Suspected primary cause -- matches QA addendum
//      "session reused but CAPTURE empty" hypothesis.)
//
//   3. The decode itself silently failed (no actual frames
//      produced). Drain telemetry surfaces this case.
//
// Fix: drain at least one CAPTURE frame in the worker, bounded by
// timeout. Frame::drop re-QBUFs the buffer back to the kernel
// pool, keeping the pipeline flowing. Emit a [perf]
// preload_handoff line with the drain outcome so QA's next round
// of data can pinpoint which failure mode we're in.
//
// Dispatch path #1 in the r76 Phase B brief: "Block the worker
// thread on it (it's a worker thread; that's fine -- the point is
// to have a frame in hand when the transition begins). Cap with a
// sensible timeout so a bad slide doesn't wedge preload forever."
// ====================================================================

/// Default drain budget for the preload-path CAPTURE warmup.
/// 500ms is generous against the bcm2835-codec ~30-50ms per-1080p-
/// frame decode rate; we'd typically see the IDR delivered within
/// the first 50ms. Anything past 200ms suggests the warmup sample
/// can't decode (failure mode #1 above) and the drain falls back
/// to "wait + log". Env override
/// OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS for QA hot-tune.
const PRELOAD_CAPTURE_DRAIN_DEFAULT_MS: u64 = 500;
const PRELOAD_CAPTURE_DRAIN_MIN_MS: u64 = 50;
const PRELOAD_CAPTURE_DRAIN_MAX_MS: u64 = 2000;

/// Resolve the drain budget. Env override clamped to a safe range
/// so a typo can't deadlock the worker or zero out the wait.
fn preload_capture_drain_budget_ms() -> u64 {
    match std::env::var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS") {
        Ok(s) => s
            .parse::<u64>()
            .unwrap_or(PRELOAD_CAPTURE_DRAIN_DEFAULT_MS)
            .clamp(PRELOAD_CAPTURE_DRAIN_MIN_MS, PRELOAD_CAPTURE_DRAIN_MAX_MS),
        Err(_) => PRELOAD_CAPTURE_DRAIN_DEFAULT_MS,
    }
}

/// Drain at least one CAPTURE frame from the primed decoder, bounded
/// by the configured budget. Returns the number of frames actually
/// drained (0 on timeout means kernel never delivered a frame --
/// useful diagnostic). Frame::drop re-QBUFs the buffer back to the
/// kernel pool, keeping the pipeline flowing.
///
/// Only drains the FIRST frame, then exits -- the dispatch goal is
/// to unblock kernel-side pipeline progression, not to fully empty
/// CAPTURE. Subsequent frames stay queued for bake_b to consume.
/// r78 Phase A telemetry: per-drain accumulators so the
/// `preload_handoff_drain_detail` line can answer the dispatch's
/// "how many EAGAINs while kernel did nothing? any non-EAGAIN
/// errors? did the kernel signal EOS?" probe.
#[derive(Default)]
pub struct DrainDetail {
    pub eagain_polls: usize,
    pub other_errs: usize,
    pub eos_seen: bool,
}

/// Public drain that delegates to the detail-recording variant with
/// a throwaway detail. Kept for any future caller that doesn't care
/// about the detail.
pub fn drain_one_capture_for_preload(
    dec_state: &mut VideoDecoderState,
    budget_ms: u64,
) -> usize {
    let mut detail = DrainDetail::default();
    drain_one_capture_for_preload_with_detail(dec_state, budget_ms, &mut detail)
}

/// r78 Phase A: detail-recording variant. Same logic; accumulates
/// per-poll outcomes into `detail` so the caller can emit them on
/// a separate parser-friendly perf line.
pub fn drain_one_capture_for_preload_with_detail(
    dec_state: &mut VideoDecoderState,
    budget_ms: u64,
    detail: &mut DrainDetail,
) -> usize {
    use std::time::{Duration, Instant};
    let deadline = Instant::now() + Duration::from_millis(budget_ms);
    let mut drained = 0usize;
    // r76 subagent BLOCKER: next_frame()'s actual contract per
    // v4l2.rs:1786-1798 is:
    //   Ok(Some(frame))  -- DQBUF succeeded, frame available
    //   Ok(None)         -- EPIPE / FLAG_LAST = end-of-stream
    //   Err("would block (EAGAIN)") -- kernel hasn't decoded yet
    //   Err(other ioctl) -- real decode error
    // The pre-fix match swapped Ok(None) and Err(EAGAIN) which
    // would have caused the drain to bail in microseconds with
    // drained=0 on every poll -- a no-op against the exact
    // failure mode this fix targets. Canonical pattern mirrors
    // hdmi.rs:7678-7688's bake_video_slide_to_current_fbo loop.
    while Instant::now() < deadline {
        match dec_state.decoder.next_frame() {
            Ok(Some(frame)) => {
                // Frame::drop re-QBUFs the CAPTURE buffer back to
                // the kernel pool via inner.lock() + VIDIOC_QBUF
                // (v4l2.rs:868+). We don't use the frame's pixels
                // -- the operator sees the NEXT decoded frame
                // (sample 1's output instead of sample 0/IDR), a
                // ~1/30s shift at 30fps; typically imperceptible
                // on continuous-motion content.
                drop(frame);
                dec_state.frames_decoded += 1;
                drained += 1;
                break;
            }
            Ok(None) => {
                // EPIPE / end-of-stream. The decoder has signaled
                // V4L2_BUF_FLAG_LAST; no more frames will come.
                // Nothing to drain; bail.
                detail.eos_seen = true;
                break;
            }
            Err(e) if e.to_string().contains("EAGAIN") => {
                // Kernel hasn't decoded yet. Short sleep then
                // retry. 5ms poll is intentionally coarser than
                // hdmi.rs:7685's 3ms -- worker latency is not
                // user-visible so we save a few syscalls.
                detail.eagain_polls += 1;
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(e) => {
                detail.other_errs += 1;
                // Real decode error (DQBUF returned out-of-range
                // index, FLAG_ERROR on the buffer, etc.). Log so
                // QA can see failure mode #3 ("decode silently
                // failed") in the [perf] stream. Subagent WARN-3
                // fix: pre-fix this was a blanket Err(_) => break
                // that swallowed all diagnostic signal.
                eprintln!(
                    "[perf] preload_handoff_drain_err err={:#}",
                    e
                );
                break;
            }
        }
    }
    drained
}

/// Preload-path prime: primes the decoder via
/// `prime_video_decoder_with_warmup(PRIME_WARMUP_FOR_PRELOAD)` then
/// drains the first CAPTURE frame to unblock the kernel-side
/// pipeline. Emits `[perf] preload_handoff slide_id=... frames_drained=... drain_us=... budget_ms=...`
/// so QA can see whether the drain succeeded (frames_drained=1) or
/// timed out (frames_drained=0 -- decoder produced nothing in the
/// budget, points at a downstream root cause).
///
/// `slide_id` is just for the log; the function doesn't use it for
/// any V4L2 logic.
pub fn prime_video_decoder_for_preload(
    dem: &Mp4Demuxer,
    slide_id: uuid::Uuid,
) -> Result<VideoDecoderState> {
    use std::time::Instant;
    // r78 Phase A probe: log first-4-sample classification BEFORE
    // priming so QA can see whether sample 0 is an IDR (the
    // dispatch's top hypothesis for why frames_drained=0).
    log_first_samples_nal_types(
        "preload", &slide_id.to_string(), &dem.samples, 4,
    );
    // r76 subagent WARN-4: emit a separate `prime_only_us` BEFORE
    // the drain so QA dashboards that historically tracked
    // `prime_us` as bring-up cost stay accurate. The outer
    // preload_worker_prime line in ipc_main.rs measures
    // prime + drain (up to +500ms-2000ms env-overridden); the
    // inner prime_only_us isolates the V4L2 bring-up cost.
    //
    // Note: if `prime_only_us` is non-zero in the perf line below,
    // STREAMON succeeded (it's inside prime_video_decoder_with_warmup
    // and the `?` would propagate STREAMON errors). r78 dispatch
    // explicitly asked "did STREAMON even run successfully?" -- yes,
    // implicitly proved by prime_only_us being a real number.
    let t_prime_only = Instant::now();
    let mut state = prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)?;
    let prime_only_us = t_prime_only.elapsed().as_micros();
    let budget_ms = preload_capture_drain_budget_ms();
    let t_drain = Instant::now();
    let mut detail = DrainDetail::default();
    let drained = drain_one_capture_for_preload_with_detail(&mut state, budget_ms, &mut detail);
    let drain_us = t_drain.elapsed().as_micros();
    eprintln!(
        "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={}",
        slide_id, drained, prime_only_us, drain_us, budget_ms,
    );
    // r78 Phase A: separate detail line so the existing
    // preload_handoff line stays parser-stable for QA's existing
    // greps. eagain_polls=N + other_errs=N answers the dispatch's
    // probe #2 "how many EAGAINs while the kernel did nothing?".
    eprintln!(
        "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={}",
        slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen,
    );
    Ok(state)
}

/// Re-feed the SPS+PPS+IDR headers + first sample to wrap the
/// decoder back to the start of the stream. Used by the standalone
/// reel driver when a video's `next_sample_idx` reaches
/// `dem.samples.len()` and we want to keep painting for the rest of
/// the slide's hold (looping the video).
///
/// On success, `state.next_sample_idx` is reset to 1 (matching the
/// post-prime invariant). On failure (decoder rejected the re-feed,
/// stream errored mid-flight), the error bubbles and the caller
/// hard-stops the loop for this slide.
///
/// Why a separate function from `prime_video_decoder`: prime opens a
/// fresh decoder; this re-feeds an already-open one. Cheaper than
/// re-opening on every wrap.
pub fn reprime_video_decoder_for_loop(
    state: &mut VideoDecoderState,
    dem: &Mp4Demuxer,
) -> Result<()> {
    // r46.4 (2026-06-02): resume the V4L2 stateful decoder
    // BEFORE re-feeding the primer. Pre-r46.2 the IPC sidecar
    // dropped the decoder on every BeginSlide so it was always
    // fresh; the standalone reel path only loops within a single
    // hold so the decoder never reaches V4L2_BUF_FLAG_LAST.
    // r46.2's keep_ids memoization preserves the decoder across
    // BeginSlide for text-over-video same-slide loops -- and a
    // long-enough slide will reach EOS, setting
    // capture_drained=true in DecoderInner, which makes all
    // subsequent next_frame() return Ok(None) forever. The bare-
    // IDR feed below would NOT recover from that state because
    // feeding output buffers doesn't clear the drained flag.
    //
    // r46.3 shipped reset_for_replay (STREAMOFF + STREAMON +
    // re-QBUF). That cycle is rejected by bcm2835-codec on
    // subsequent OUTPUT QBUF with EINVAL (verified live on FYS
    // 2026-06-02 r46.3 deploy). r46.4 replaces it with
    // resume_after_eos which issues VIDIOC_DECODER_CMD with
    // V4L2_DEC_CMD_START -- the V4L2 stateful-decoder spec's
    // documented mechanism for post-EOS resume. Does not touch
    // streaming/buffer state; just clears the kernel's EOS
    // marker on CAPTURE.
    state
        .decoder
        .resume_after_eos()
        .context("resume_after_eos before re-priming SPS+PPS+IDR")?;
    let first_sample = dem
        .samples
        .first()
        .ok_or_else(|| anyhow!("MP4 contains zero samples"))?;
    let header = dem.sps_pps_annexb();
    let mut primer: Vec<u8> = Vec::with_capacity(header.len() + first_sample.len());
    primer.extend_from_slice(&header);
    primer.extend_from_slice(first_sample);
    state
        .decoder
        .feed(&primer)
        .context("re-feed SPS+PPS+IDR primer for video loop")?;
    state.next_sample_idx = 1;
    Ok(())
}

#[cfg(test)]
mod tests {
    //! Shape tests for the post-extraction module surface. The deep
    //! `v4l2::tests::decode_test_fixture_320x240` integration test
    //! exercises the actual decode flow end-to-end (Linux + bcm2835-
    //! codec gated). These tests cover the cross-platform shape of
    //! the new module (constants, error paths reachable without a
    //! real V4L2 device).
    use super::*;

    // r73 (2026-06-06): warmup constants + preload-wiring regression
    // tests live at the binary crate root (main.rs) so they run on
    // all platforms, not just Linux where THIS test module compiles
    // in. See main.rs near the PRIME_WARMUP_* constants.

    // ============================================================
    // r76 Phase B (2026-06-07): preload CAPTURE drain budget.
    //
    // r76 Phase A telemetry showed endpoint_b never delivering a
    // frame -- the bcm2835-codec sits in a wait-for-userspace
    // state after prime. The drain in the worker thread triggers
    // the first DQBUF so the kernel pipeline progresses. These
    // tests pin the budget resolver semantics. The V4L2 drain
    // itself needs real hardware (gated tests in v4l2::tests::
    // decode_test_fixture_320x240).
    // ============================================================

    #[test]
    fn preload_capture_drain_budget_default_is_500ms() {
        // Pin the default so a future change has to be deliberate.
        // SAFETY: test owns the env var; other tests in this module
        // don't touch it.
        unsafe { std::env::remove_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS") };
        assert_eq!(preload_capture_drain_budget_ms(), 500);
    }

    #[test]
    fn preload_capture_drain_budget_env_override_clamps_and_round_trips() {
        // Consolidated so the shared env doesn't race with parallel
        // siblings (same pattern as r70's feed_drain test).
        // SAFETY: test owns this env var entirely.

        // Inside-range override round-trips.
        unsafe { std::env::set_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS", "1000") };
        assert_eq!(preload_capture_drain_budget_ms(), 1000);

        unsafe { std::env::set_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS", "100") };
        assert_eq!(preload_capture_drain_budget_ms(), 100);

        // =0 clamps to floor (50ms). A typo can't deadlock by
        // making every drain timeout-skip immediately.
        unsafe { std::env::set_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS", "0") };
        assert_eq!(preload_capture_drain_budget_ms(), PRELOAD_CAPTURE_DRAIN_MIN_MS);

        // =999999 clamps to ceiling (2000ms). A typo can't wedge
        // the worker thread for ~17 minutes.
        unsafe { std::env::set_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS", "999999") };
        assert_eq!(preload_capture_drain_budget_ms(), PRELOAD_CAPTURE_DRAIN_MAX_MS);

        // Garbage falls back to default.
        unsafe { std::env::set_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS", "notanumber") };
        assert_eq!(preload_capture_drain_budget_ms(), 500);

        unsafe { std::env::remove_var("OPENMARQUEE_PRELOAD_CAPTURE_DRAIN_MS") };
    }

    // ============================================================
    // r78 Phase A (2026-06-08): NAL classifier pin.
    //
    // The probe's diagnostic value hinges on classifying sample 0
    // correctly. If a future demuxer change emits 3-byte start
    // codes (`00 00 01`) instead of 4-byte (`00 00 00 01`) the
    // classifier MUST still pick up the NAL header at the right
    // offset, otherwise QA's `is_idr` field lies and the probe is
    // useless.
    // ============================================================

    #[test]
    fn walk_h264_nal_types_idr_alone_4byte() {
        // 4-byte start code + IDR-only (nal_unit_type=5 -> byte 0x65).
        let sample = vec![0, 0, 0, 1, 0x65, 0xab, 0xcd];
        let (first, contains_idr) = walk_h264_nal_types(&sample);
        assert_eq!(first, Some(5));
        assert!(contains_idr);
    }

    #[test]
    fn walk_h264_nal_types_aud_then_idr() {
        // r78 subagent BLOCKER-1 pin: x264 prefixes the IDR slice
        // with an AUD (type=9, header byte 0x09) and/or SEI NAL.
        // Pre-fix the classifier would have returned `first_nal=9
        // is_idr=false` -- giving QA a false-positive demuxer
        // indictment. Post-fix: walks all NALs, finds the IDR,
        // contains_idr=true.
        let sample = vec![
            // AUD: 4-byte start code + header 0x09 + payload byte.
            0, 0, 0, 1, 0x09, 0x10,
            // IDR slice: 4-byte start code + header 0x65 + payload.
            0, 0, 0, 1, 0x65, 0xab, 0xcd,
        ];
        let (first, contains_idr) = walk_h264_nal_types(&sample);
        assert_eq!(first, Some(9), "first_nal_type MUST be AUD (9), not IDR");
        assert!(contains_idr, "MUST find IDR even though AUD prefixes it -- this is the BLOCKER-1 case");
    }

    #[test]
    fn walk_h264_nal_types_sei_then_idr_3byte_start_codes() {
        // Some encoders use 3-byte start codes mid-sample. Defensive.
        let sample = vec![
            0, 0, 1, 0x06, 0x05, // SEI prefix
            0, 0, 0, 1, 0x65, 0xab, // IDR (4-byte start)
        ];
        let (first, contains_idr) = walk_h264_nal_types(&sample);
        assert_eq!(first, Some(6));
        assert!(contains_idr);
    }

    #[test]
    fn walk_h264_nal_types_non_idr_only() {
        // Non-IDR slice (type=1). contains_idr=false. Useful for
        // sample 1+ in the dump.
        let sample = vec![0, 0, 0, 1, 0x61, 0x9a];
        let (first, contains_idr) = walk_h264_nal_types(&sample);
        assert_eq!(first, Some(1));
        assert!(!contains_idr);
    }

    #[test]
    fn walk_h264_nal_types_empty_returns_none_no_idr() {
        let (first, contains_idr) = walk_h264_nal_types(&[]);
        assert_eq!(first, None);
        assert!(!contains_idr);
    }

    #[test]
    fn walk_h264_nal_types_no_start_codes_returns_none_no_idr() {
        // Random bytes with no start-code pattern: classifier
        // reports `first_nal_type=unknown` rather than
        // misinterpreting byte 0 as a NAL header.
        let (first, contains_idr) = walk_h264_nal_types(&[0xff, 0x01, 0x02, 0x03]);
        assert_eq!(first, None);
        assert!(!contains_idr);
    }

    #[test]
    fn v4l2_decoder_path_constant_matches_pi_bcm2835_codec_node() {
        // V4L2 piece 3c invariant — `/dev/video10` is the
        // bcm2835-codec decode-side node on Raspberry Pi OS. The
        // standalone reel + IPC sidecar BOTH reach it via this
        // constant after the H2 extraction; if a future SoC needs a
        // different path, this is the one place to flip.
        assert_eq!(V4L2_DECODER_PATH, "/dev/video10");
    }

    #[test]
    fn prime_video_decoder_returns_typed_error_when_device_absent() {
        // The bcm2835-codec node `/dev/video10` is absent on hosts
        // without the kernel module loaded (CI Linux, Mac dev under
        // Linux cross-build, etc.). `prime_video_decoder` returns a
        // human-readable typed error so callers (the IPC sidecar's
        // cache.load + the standalone reel's video arm) can log
        // and degrade gracefully.
        //
        // This test runs on any Linux box where /dev/video10 doesn't
        // exist — most CI runners + the dev Pi when the codec
        // module hasn't been loaded yet.
        if std::path::Path::new(V4L2_DECODER_PATH).exists() {
            // Skip on hosts that DO have the codec node — those
            // run the deep `v4l2::tests::decode_test_fixture_*`
            // integration tests instead.
            eprintln!(
                "skipping: {} present (codec node loaded)",
                V4L2_DECODER_PATH,
            );
            return;
        }
        // Construct a minimal Mp4Demuxer-shaped value via the
        // public test fixture. The MP4 test fixture lives in
        // mp4_demux::tests; reuse it.
        let fixture_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/test_320x240.mp4");
        if !fixture_path.exists() {
            // No fixture on this host — silently pass; the deeper
            // v4l2 integration covers the priming path.
            eprintln!(
                "skipping: 320x240.mp4 fixture not on disk at {}",
                fixture_path.display(),
            );
            return;
        }
        let dem = crate::mp4_demux::Mp4Demuxer::open(&fixture_path)
            .expect("open 320x240.mp4 test fixture");
        let err = prime_video_decoder(&dem)
            .expect_err("/dev/video10 absent should fail prime");
        let msg = format!("{err:#}");
        assert!(
            msg.contains(V4L2_DECODER_PATH),
            "error should name the decoder path; got: {msg}"
        );
        assert!(
            msg.contains("does not exist") || msg.contains("no codec driver"),
            "error should explain the device-absent failure; got: {msg}"
        );
    }
}
