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
pub fn prime_video_decoder(dem: &Mp4Demuxer, caller: &'static str) -> Result<VideoDecoderState> {
    // r91 (2026-06-08) probe: per QA r91 dispatch, explicit
    // caller tag so QA can distinguish cold_start from reel
    // (both use warmup_count=3). The synchronous BeginSlide path
    // passes "cold_start"; the standalone reel passes "reel";
    // tests pass "test".
    eprintln!(
        "[perf] prime_entry caller={} warmup_count={}",
        caller, PRIME_WARMUP_DEFAULT,
    );
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

/// r79 Phase A.5 (2026-06-08): emit the SPS/PPS state at prime time
/// so QA can confirm parameter sets ARE reaching the decoder. The
/// dispatch's primary Phase B hypothesis was "SPS/PPS not reaching
/// the decoder" -- this probe falsifies or confirms it instantly.
///
/// Code reading already shows the prime path in
/// `prime_video_decoder_with_warmup` prepends `dem.sps_pps_annexb()`
/// (line ~283) to sample 0 before feeding. But the FYS behavior
/// (kernel idle, eagain_polls=92-98, zero output) is consistent with
/// "no SPS/PPS reached the decoder" -- which would happen if either:
///   - the demuxer's `dem.sps` or `dem.pps` were empty for the FYS
///     MP4s specifically (avcC parsing edge case)
///   - the primer-feed buffer exceeded the OUTPUT plane size and
///     feed() silently truncated (it doesn't -- it returns Err, but
///     the diagnostic helps falsify this)
///
/// Emits: `[perf] preload_decoder_config slide_id=... label=...
/// has_sps=... sps_len=... has_pps=... pps_len=... sample_count=...
/// sample_0_size=... primer_size=...`.
///
/// `primer_size` = `sps_pps_annexb()` total + sample[0] size, which
/// is exactly what `dec.feed()` receives at the primer-feed call
/// site. If this exceeds plane_max the kernel rejects with EINVAL
/// (visible as a non-zero `other_errs` in
/// `preload_handoff_drain_detail`, but emit it explicitly here so
/// QA doesn't have to correlate).
pub fn log_preload_decoder_config(slide_id: uuid::Uuid, dem: &Mp4Demuxer, label: &str) {
    let has_sps = !dem.sps.is_empty();
    let has_pps = !dem.pps.is_empty();
    let sample_0_size = dem.samples.first().map(|s| s.len()).unwrap_or(0);
    // sps_pps_annexb() prepends `00 00 00 01` start codes before
    // each parameter set; the total wire-format size is
    // 4 + sps.len() + 4 + pps.len() per mp4_demux.rs:407-414.
    let sps_pps_annexb_size = 4 + dem.sps.len() + 4 + dem.pps.len();
    let primer_size = sps_pps_annexb_size + sample_0_size;
    eprintln!(
        "[perf] preload_decoder_config slide_id={} label={} \
         has_sps={} sps_len={} has_pps={} pps_len={} \
         sample_count={} sample_0_size={} primer_size={}",
        slide_id, label, has_sps, dem.sps.len(), has_pps, dem.pps.len(),
        dem.samples.len(), sample_0_size, primer_size,
    );
}

pub fn prime_video_decoder_with_warmup(
    dem: &Mp4Demuxer,
    warmup_count_requested: usize,
) -> Result<VideoDecoderState> {
    use std::path::Path;
    use std::time::Instant;
    // r90 (2026-06-08) probe: per QA r90 dispatch's instrumentation-
    // only ask. Fires UNCONDITIONALLY (not gated by EOS_FLUSH),
    // so QA can compare OFF vs ON trace patterns directly. The
    // warmup_count_requested distinguishes preload (=2) vs
    // cold-start (=3) call sites without threading slide_id
    // through the signature.
    eprintln!(
        "[perf] prime_with_warmup_entered warmup_count={} mp4_w={} mp4_h={} samples={}",
        warmup_count_requested, dem.width, dem.height, dem.samples.len(),
    );
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
    let dmabuf_env = std::env::var("OPENMARQUEE_RENDERER_DMABUF").ok();
    let use_dmabuf = dmabuf_env
        .as_deref()
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);
    // r87 (2026-06-09): surface the DMA env-var state per prime.
    // r86's kill-switch revealed that EOS_FLUSH=1 systemd drop-ins
    // MAY have been setting other env vars too; this probe lets
    // QA verify exactly which mode the running process sees,
    // side-by-side with preload_eos_flush_mode. Cost: one eprintln
    // per prime (microseconds; absorbed by stderr buffer).
    //
    // r87 subagent WARN-1: labelled `prime_dmabuf_mode` (not
    // `preload_dmabuf_mode`) because `prime_video_decoder_with_warmup`
    // is also reached from cold-start `prime_video_decoder` +
    // standalone reel paths -- the `preload_*` prefix would
    // mislead QA's A/B grep into matching unrelated primes.
    eprintln!(
        "[perf] prime_dmabuf_mode use_dmabuf={} env_raw={:?}",
        use_dmabuf, dmabuf_env,
    );
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

/// r86 (2026-06-09): runtime kill-switch for the r85 EOS-flush
/// body. Set `OPENMARQUEE_EOS_FLUSH=1` (or
/// "true"/"yes"/"on"/"enable"/"enabled") to enable. Default is
/// DISABLED -- r86 deploy == r84 baseline (no EOS-flush;
/// transitions look like cuts; video plays).
///
/// Per QA r86 dispatch constraint: "A kill-switch env var would
/// let me A/B at deploy time. Consider adding one to bypass the
/// new code via runtime config — fast roll-back is more
/// valuable than always-on fix."
///
/// Resolved on every `prime_video_decoder_for_preload` call (no
/// caching) so a fresh-fork process inherits the current env-var
/// value at spawn time. Per subagent NIT-4: env-var changes made
/// via `systemctl set-environment OPENMARQUEE_EOS_FLUSH=1` only
/// take effect on the NEXT renderer subprocess fork, which today
/// requires `systemctl restart openmarquee-backend`. Per-call
/// resolution still lets QA toggle by setting env + restarting
/// without rebuilding/redeploying the binary.
fn is_eos_flush_enabled() -> bool {
    match std::env::var("OPENMARQUEE_EOS_FLUSH") {
        Ok(s) => {
            let v = s.trim().to_ascii_lowercase();
            matches!(v.as_str(), "1" | "true" | "yes" | "on" | "enable" | "enabled")
        }
        Err(_) => false,
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
    /// r106 (2026-06-10): cumulative count of samples top-up-fed
    /// during the drain window. Pre-r106 the drain loop fed
    /// nothing here -- the codec input pool stayed at the 3 AUs
    /// from prime time. r106's top-up keeps feeding whenever
    /// `try_feed_nonblocking` returns true, pushing the codec
    /// past its dual-1080p contention threshold. Emitted on the
    /// `preload_handoff_drain_detail` perf line for QA visibility.
    pub eagain_topup_fed_count: usize,
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
/// r106 (2026-06-10): non-blocking top-up feed helper.
/// Feeds samples from `dem.samples[next_sample_idx..]` into the
/// decoder's OUTPUT pool until either:
/// - the pool has no free slot (returns Ok, count of fed
///   samples >= 0),
/// - the demuxer's samples are exhausted (no wrap-around;
///   caller handles wrap via reprime if needed),
/// - a feed call returns Err (propagated).
///
/// Used by the r106 decoupled feed-drain pattern so each
/// transition tick keeps every active decoder's input pool
/// well-stocked without blocking. Returns Ok(fed_count) on
/// success.
#[cfg(target_os = "linux")]
pub fn top_up_feed_nonblocking(
    dec_state: &mut VideoDecoderState,
    dem: &Mp4Demuxer,
) -> Result<usize> {
    let mut fed = 0usize;
    while dec_state.next_sample_idx < dem.samples.len() {
        let sample = &dem.samples[dec_state.next_sample_idx];
        match dec_state.decoder.try_feed_nonblocking(sample) {
            Ok(true) => {
                dec_state.next_sample_idx += 1;
                fed += 1;
            }
            Ok(false) => {
                // Pool full. Stop here; caller will retry on the
                // next tick.
                break;
            }
            Err(e) => {
                return Err(e).context(format!(
                    "top_up_feed_nonblocking: feed sample {} failed",
                    dec_state.next_sample_idx,
                ));
            }
        }
    }
    Ok(fed)
}

pub fn drain_one_capture_for_preload_with_detail(
    dec_state: &mut VideoDecoderState,
    budget_ms: u64,
    detail: &mut DrainDetail,
) -> usize {
    // r106 (2026-06-10): when no Mp4Demuxer is provided, fall
    // back to the original drain-only behavior (pre-r106). This
    // overload preserves backward compatibility for tests + any
    // future caller that genuinely wants drain-only.
    drain_one_capture_for_preload_with_detail_and_topup(
        dec_state, budget_ms, detail, None,
    )
}

/// r106 (2026-06-10): drain variant that ALSO tops up the
/// decoder's input pool on every EAGAIN poll. The root cause
/// of the dual-1080p wedge was that the pre-r106 preload path
/// fed exactly 3 AUs (primer + warmup=2) then drained a starved
/// pipeline -- the firmware can't decode 1080p with so few
/// inputs under contention. Per QA's live-fire proof, ffmpeg
/// succeeds because it keeps ~16 AUs queued.
///
/// When `dem` is `Some`, each EAGAIN iteration calls
/// `top_up_feed_nonblocking` to push more samples into OUTPUT
/// slots that have freed up since the last attempt. Returns the
/// same `drained` count (0 or 1) -- the drain semantics are
/// unchanged; only the feed cadence improves.
pub fn drain_one_capture_for_preload_with_detail_and_topup(
    dec_state: &mut VideoDecoderState,
    budget_ms: u64,
    detail: &mut DrainDetail,
    #[cfg_attr(not(target_os = "linux"), allow(unused_variables))]
    dem: Option<&Mp4Demuxer>,
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
                // r106 (2026-06-10): top-up feed on every EAGAIN
                // poll when the demuxer is available. The pre-
                // r106 path looped here for 503ms feeding
                // ZERO samples; ffmpeg succeeds by keeping ~16
                // AUs queued. We push as many samples as the
                // OUTPUT pool will accept this iteration --
                // typically 0-4 samples depending on how many
                // slots the kernel released since the last
                // try_feed_nonblocking call.
                #[cfg(target_os = "linux")]
                if let Some(dem) = dem {
                    match top_up_feed_nonblocking(dec_state, dem) {
                        Ok(fed) => {
                            if fed > 0 {
                                detail.eagain_topup_fed_count += fed;
                            }
                        }
                        Err(err) => {
                            // Don't bail the whole drain; the
                            // err might be transient. Log + keep
                            // trying.
                            eprintln!(
                                "[perf] preload_topup_feed_err err={:#}",
                                err
                            );
                        }
                    }
                }
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
    // r91 (2026-06-08) probe per QA dispatch: caller-tagged
    // prime_entry. Fires regardless of EOS_FLUSH gate.
    eprintln!(
        "[perf] prime_entry caller=preload_worker warmup_count={}",
        PRIME_WARMUP_FOR_PRELOAD,
    );
    log_first_samples_nal_types(
        "preload", &slide_id.to_string(), &dem.samples, 4,
    );
    log_preload_decoder_config(slide_id, dem, "preload");

    // r90 (2026-06-08): RE-ENABLE r86 kill-switch + r85 EOS-flush
    // body INSIDE the gate (no new code added to the body itself --
    // it's the SAME r85 body that was previously dead_code).
    //
    // Per QA r90 dispatch's instrumentation-only constraint:
    // - Default OFF (EOS_FLUSH unset) = r84-baseline = clean
    // - Setting OPENMARQUEE_EOS_FLUSH=1 enables the body, which QA
    //   has data-proved reproduces the regression
    // - The new prime_with_warmup_entered probe (in
    //   prime_video_decoder_with_warmup at line 254-263) fires
    //   UNCONDITIONALLY, so QA can A/B the OFF vs ON traces and
    //   see exactly where they diverge
    //
    // r85→r84 diff code-review came up EMPTY for any DMABUF
    // change (r89 ship); my analysis says the bug shouldn't exist,
    // but QA's fresh-boot reproduction proves it does. r90 adds
    // the entry probe so the next round has more data to bisect
    // on, and re-enables the path so QA can reproduce at will.

    let eos_flush_enabled = is_eos_flush_enabled();
    eprintln!(
        "[perf] preload_eos_flush_mode slide_id={} enabled={}",
        slide_id, eos_flush_enabled,
    );

    if !eos_flush_enabled {
        // r84-baseline (default): prime + one-shot drain + emit.
        let t_prime_only = Instant::now();
        let mut state = prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)?;
        let prime_only_us = t_prime_only.elapsed().as_micros();
        let budget_ms = preload_capture_drain_budget_ms();
        let t_drain = Instant::now();
        let mut detail = DrainDetail::default();
        // r106 (2026-06-10): when feed-drain decouple is enabled
        // (default), thread the Mp4Demuxer through so the drain
        // loop tops up the codec input pool on every EAGAIN
        // iteration. Pre-r106 path fed exactly 3 AUs and then
        // sat polling a starved decoder for 503ms; r106 keeps
        // pushing samples as long as OUTPUT slots free up.
        let decouple_enabled = crate::v4l2::is_feed_drain_decouple_enabled();
        let drained = if decouple_enabled {
            drain_one_capture_for_preload_with_detail_and_topup(
                &mut state, budget_ms, &mut detail, Some(dem),
            )
        } else {
            drain_one_capture_for_preload_with_detail(
                &mut state, budget_ms, &mut detail,
            )
        };
        let drain_us = t_drain.elapsed().as_micros();
        eprintln!(
            "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={} was_deferred=false decouple={}",
            slide_id, drained, prime_only_us, drain_us, budget_ms, decouple_enabled,
        );
        eprintln!(
            "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={} eagain_topup_fed_count={}",
            slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen, detail.eagain_topup_fed_count,
        );
        return Ok(state);
    }

    // r85 EOS-flush body (gated by OPENMARQUEE_EOS_FLUSH=1).
    //
    // r90 (2026-06-08) probe: per QA r90 dispatch's explicit ask
    // for `[perf] eos_flush_branch_entered slide_id=N
    // path=preload_worker|cold_start`. Fires at the VERY FIRST
    // line of the EOS-flush-gated body so QA can confirm exactly
    // which cycle's prime is the first one to take the bad branch.
    // path=preload_worker because prime_video_decoder_for_preload
    // is ONLY called from preload_in_worker (the sync BeginSlide
    // path uses prime_video_decoder, not _for_preload).
    eprintln!(
        "[perf] eos_flush_branch_entered slide_id={} path=preload_worker",
        slide_id,
    );

    let t_prime_only = Instant::now();
    let mut state = prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)?;
    let prime_only_us = t_prime_only.elapsed().as_micros();

    let budget_ms = preload_capture_drain_budget_ms();
    let t_drain = Instant::now();
    let mut detail = DrainDetail::default();
    let drained = drain_after_signal_eos(&mut state, budget_ms, &mut detail);
    let drain_us = t_drain.elapsed().as_micros();

    // r80 BLOCKER-2 fix preserved: bail cleanly on budget exhaust.
    if !detail.eos_seen {
        let resume_result = state.decoder.resume_after_eos();
        let resume_ok = resume_result.is_ok();
        eprintln!(
            "[perf] preload_resume_after_eos slide_id={} cmd_start_ok={} eos_seen={} context=bail_path",
            slide_id, resume_ok, detail.eos_seen,
        );
        let _ = resume_result;

        let reason = if detail.other_errs > 0 {
            "drain_ioctl_error"
        } else {
            "kernel_never_signaled_FLAG_LAST"
        };
        eprintln!(
            "[perf] preload_handoff_drain_budget_exhausted slide_id={} budget_ms={} \
             eagain_polls={} other_errs={} frames_drained={} reason={}",
            slide_id, budget_ms, detail.eagain_polls, detail.other_errs, drained, reason,
        );
        eprintln!(
            "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={} was_deferred=false",
            slide_id, drained, prime_only_us, drain_us, budget_ms,
        );
        eprintln!(
            "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={}",
            slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen,
        );
        anyhow::bail!(
            "preload drain budget exhausted ({}ms) without kernel FLAG_LAST; \
             slide will fall back to synchronous prime at BeginSlide",
            budget_ms
        );
    }

    let resume_result = state.decoder.resume_after_eos();
    eprintln!(
        "[perf] preload_resume_after_eos slide_id={} cmd_start_ok={} eos_seen={} context=success_path",
        slide_id, resume_result.is_ok(), detail.eos_seen,
    );
    resume_result.context("r85: resume_after_eos before re-priming SPS+PPS+IDR")?;

    let first_sample = dem.samples.first()
        .ok_or_else(|| anyhow::anyhow!("r85: MP4 contains zero samples"))?;
    let header = dem.sps_pps_annexb();
    let mut primer = Vec::with_capacity(header.len() + first_sample.len());
    primer.extend_from_slice(&header);
    primer.extend_from_slice(first_sample);
    state.decoder.feed(&primer)
        .context("r85: re-feed SPS+PPS+IDR primer after drain-resume")?;
    state.next_sample_idx = 1;
    let warmup_count = PRIME_WARMUP_FOR_PRELOAD.min(dem.samples.len().saturating_sub(1));
    for _ in 0..warmup_count {
        let s = &dem.samples[state.next_sample_idx];
        match state.decoder.feed(s) {
            Ok(()) => state.next_sample_idx += 1,
            Err(e) => {
                eprintln!(
                    "warn: r85 post-resume warmup feed sample {} failed: {:#} \
                     (continuing without full lookahead)",
                    state.next_sample_idx, e,
                );
                break;
            }
        }
    }
    state.frames_decoded = 0;

    eprintln!(
        "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={} was_deferred=false",
        slide_id, drained, prime_only_us, drain_us, budget_ms,
    );
    eprintln!(
        "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={}",
        slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen,
    );
    Ok(state)
}

/// r83: previous r80-r82 preload body kept for reference under
/// `#[allow(dead_code)]`. Re-enable + adapt when ioctl-trace +
/// CMD_STOP probe data justifies a specific drain shape.
#[allow(dead_code)]
fn prime_video_decoder_for_preload_r82_disabled(
    dem: &Mp4Demuxer,
    slide_id: uuid::Uuid,
) -> Result<VideoDecoderState> {
    use std::time::Instant;
    log_first_samples_nal_types(
        "preload", &slide_id.to_string(), &dem.samples, 4,
    );
    log_preload_decoder_config(slide_id, dem, "preload");

    // r80 Phase B (2026-06-08): V4L2 m2m drain-and-resume cycle.
    //
    // r79 telemetry on FYS empirically REFUTED QA's "SPS/PPS not
    // reaching the decoder" hypothesis:
    //   has_sps=true sps_len=36, has_pps=true pps_len=4
    //   primer_size=173-237 KB (well under plane_max ~1-4 MB)
    //   sample[0] contains an IDR
    //   eagain_polls=92-98 (kernel idle, not slow)
    //   other_errs=0, eos_seen=false
    //
    // bcm2835-codec received full SPS+PPS+IDR+warmup samples, no
    // errors, but emitted zero decoded CAPTURE frames over 500ms.
    // The V4L2 stateful-decoder spec (per
    // <https://www.kernel.org/doc/html/latest/userspace-api/media/v4l/dev-decoder.html>)
    // documents that the kernel holds frames in its reorder buffer
    // until either (a) more input samples arrive to flush them
    // (lookahead) or (b) userspace explicitly signals end-of-stream
    // via V4L2_DEC_CMD_STOP or feed(&[]) with V4L2_BUF_FLAG_LAST.
    //
    // r76/r77 already explored (a): warmup_count=1 then =2 still
    // produced frames_drained=0. r80 ships (b): the canonical
    // V4L2 m2m drain-resume cycle.
    //
    // Sequence:
    //   1. prime_video_decoder_with_warmup (existing): open, S_FMT,
    //      REQBUFS, STREAMON, feed primer (SPS+PPS+sample[0]),
    //      feed warmup samples.
    //   2. feed(&[]): signals EOS via empty QBUF with FLAG_LAST.
    //      Sets output_eof_sent=true.
    //   3. Drain loop: DQBUF CAPTURE frames until kernel signals
    //      end-of-drain via FLAG_LAST (returned by next_frame as
    //      Ok(None)). Drop the frames; their Drop re-QBUFs the
    //      CAPTURE buffer back to the kernel pool.
    //   4. resume_after_eos: V4L2_DEC_CMD_START to clear EOS state
    //      kernel-side + clear capture_drained + output_eof_sent
    //      in our wrapper.
    //   5. Re-feed primer (SPS+PPS+sample[0]) so the pipeline is
    //      ready for playback's first bake (matching the post-prime
    //      contract: next_sample_idx=1, decoder fresh on IDR).
    //
    // After this cycle the kernel has been forced to flush its
    // reorder buffer (proving it CAN emit decoded frames) AND has
    // been reset to a clean state for playback. The drained frames
    // are discarded -- they were decoded but we don't keep them.
    // Bake's first call after handoff will:
    //   - drain_output_quiet (no-op, free pool already full)
    //   - feed sample[1]
    //   - next_frame -> DQBUFs the IDR-decoded frame (kernel
    //     should emit it during the new primer's decode)
    // Operator sees frame 0 (the IDR) at handoff.
    //
    // If frames_drained=0 STILL post-r80, the kernel doesn't even
    // flush on EOS -- which would point at a deeper bcm2835-codec
    // configuration issue. r79's diagnostic line stays in place
    // so we'd see that immediately.

    let t_prime_only = Instant::now();
    let mut state = prime_video_decoder_with_warmup(dem, PRIME_WARMUP_FOR_PRELOAD)?;
    let prime_only_us = t_prime_only.elapsed().as_micros();

    let budget_ms = preload_capture_drain_budget_ms();
    let t_drain = Instant::now();
    let mut detail = DrainDetail::default();
    let drained = drain_after_signal_eos(&mut state, budget_ms, &mut detail);
    let drain_us = t_drain.elapsed().as_micros();

    // r80 subagent BLOCKER-2 + WARN-5: graceful degradation when
    // the kernel never emits FLAG_LAST. If drain budget exhausts
    // with `eos_seen=false`, the wrapper's `capture_drained` is
    // still false; `resume_after_eos` short-circuits (v4l2.rs:1510)
    // and the decoder is left in mid-EOS state -- the kernel
    // believes a CMD_STOP is pending but no FLAG_LAST arrived. The
    // subsequent re-prime would queue OUTPUT samples against a
    // half-stopped queue with undefined behavior. (Pre-r82 with
    // feed(&[]) the failure mode was a "feed() called after EOF"
    // guard trip at v4l2.rs:1613 because feed(&[]) set
    // output_eof_sent=true; r82 H1's CMD_STOP doesn't set that
    // wrapper flag, so the failure mode is different but the
    // remedy is the same: bail.) Better: detect this case, emit a
    // distinct diagnostic line, and return Err early. The caller
    // (preload_in_worker) already handles failed preload by
    // logging + falling back to the synchronous cache.load on
    // first BeginSlide, which uses `prime_video_decoder` (without
    // the drain cycle). Cold-start path eventually paints because
    // bake feeds one more sample per tick -- cumulative input
    // flushes the reorder buffer naturally
    // even without an EOS signal.
    // r81 subagent WARN-2: probe the resume sequence BEFORE the
    // budget-exhaust bail so QA sees cmd_start_ok=... in the
    // FAILURE case (the one we most need the diagnostic for).
    // Probe is conditional on having issued the EOS signal at all
    // (other_errs==0 means r82's signal_eos_via_cmd_stop succeeded),
    // so we don't probe a no-op resume.
    let cmd_start_ok = if detail.other_errs == 0 {
        // resume_after_eos no-ops if capture_drained=false (i.e.
        // we never saw FLAG_LAST). Call it anyway to record what
        // the ioctl would do; in the no-op case "ok" reflects the
        // gate, not a real CMD_START issuance.
        let result = state.decoder.resume_after_eos();
        let ok = result.is_ok();
        eprintln!(
            "[perf] preload_resume_after_eos slide_id={} cmd_start_ok={} eos_seen={}",
            slide_id, ok, detail.eos_seen,
        );
        if !detail.eos_seen {
            // We're going to bail; suppress the Err since the bail
            // below carries the user-facing failure context.
            result.ok();
        } else {
            result.context("r81: resume_after_eos before re-priming SPS+PPS+IDR")?;
        }
        ok
    } else {
        eprintln!(
            "[perf] preload_resume_after_eos slide_id={} cmd_start_ok=skipped reason=eos_feed_failed",
            slide_id,
        );
        false
    };
    let _ = cmd_start_ok; // consumed by the perf line above

    if !detail.eos_seen {
        // r81 subagent WARN-5: differentiate the budget-exhaust
        // reason. Pre-fix the label was always
        // `kernel_never_signaled_FLAG_LAST` but on a
        // QBUF-after-DQBUF failure the kernel actually signaled
        // nothing because we erred out earlier. Branch on
        // other_errs to attribute correctly.
        let reason = if detail.other_errs > 0 {
            "drain_ioctl_error"
        } else {
            "kernel_never_signaled_FLAG_LAST"
        };
        eprintln!(
            "[perf] preload_handoff_drain_budget_exhausted slide_id={} budget_ms={} \
             eagain_polls={} other_errs={} frames_drained={} \
             reason={}",
            slide_id, budget_ms, detail.eagain_polls, detail.other_errs, drained, reason,
        );
        eprintln!(
            "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={} was_deferred=false",
            slide_id, drained, prime_only_us, drain_us, budget_ms,
        );
        eprintln!(
            "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={}",
            slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen,
        );
        // The decoder state is now: capture queued but not drained,
        // output_eof_sent=true. It's unrecoverable without the full
        // STOP+drain+START cycle that we just failed. Drop the
        // VideoDecoderState (its Drop closes the V4L2 fd, kernel
        // releases the MMAL component) and surface the failure so
        // the caller falls back cleanly.
        anyhow::bail!(
            "preload drain budget exhausted ({}ms) without kernel FLAG_LAST; \
             slide will fall back to synchronous prime at BeginSlide",
            budget_ms
        );
    }

    // Step 4 + 5: re-prime + restore lookahead. (resume_after_eos
    // was already issued + probed in the pre-bail branch above so
    // QA's diagnostic landed even on failed runs.)
    // r80 WARN-3 fix preserved: re-feed warmup samples after the
    // primer to restore kernel B-frame lookahead.
    let first_sample = dem.samples.first()
        .ok_or_else(|| anyhow::anyhow!("r80: MP4 contains zero samples"))?;
    let header = dem.sps_pps_annexb();
    let mut primer = Vec::with_capacity(header.len() + first_sample.len());
    primer.extend_from_slice(&header);
    primer.extend_from_slice(first_sample);
    state.decoder.feed(&primer)
        .context("r80: re-feed SPS+PPS+IDR primer after drain-resume")?;
    state.next_sample_idx = 1;
    // r80 subagent WARN-3 fix: re-feed warmup samples too so the
    // kernel's lookahead pipeline matches the pre-drain state. The
    // primer alone leaves bake's first call needing to feed more
    // samples before the kernel emits, regressing first-frame
    // latency to pre-r5 cold-start (~10s visible) when bcm2835-codec
    // needs ~3-4 input samples of lookahead.
    let warmup_count = PRIME_WARMUP_FOR_PRELOAD.min(dem.samples.len().saturating_sub(1));
    for _ in 0..warmup_count {
        let s = &dem.samples[state.next_sample_idx];
        match state.decoder.feed(s) {
            Ok(()) => state.next_sample_idx += 1,
            Err(e) => {
                eprintln!(
                    "warn: r80 post-resume warmup feed sample {} failed: {:#} \
                     (continuing without full lookahead)",
                    state.next_sample_idx, e,
                );
                break;
            }
        }
    }

    // r80 subagent BLOCKER-1 fix: the drained frames in step 3 were
    // NOT frames the operator ever sees -- they were decoded into a
    // CAPTURE buffer, drained for the drain-resume cycle, and the
    // buffer re-QBUF'd via Frame::drop. The frames_decoded counter
    // is read by hdmi.rs:3729 (was_first gate for r62's first-frame
    // texture cache fast path); leaving it at N>0 would dead-end
    // the r62 cache_eligible check on EVERY post-r80 preloaded
    // slide, undoing r62's first-frame-paint win on FYS. Reset to
    // 0 so the post-r80 state matches what a fresh
    // prime_video_decoder_with_warmup would have produced.
    state.frames_decoded = 0;

    eprintln!(
        "[perf] preload_handoff slide_id={} frames_drained={} prime_only_us={} drain_us={} budget_ms={} was_deferred=false",
        slide_id, drained, prime_only_us, drain_us, budget_ms,
    );
    eprintln!(
        "[perf] preload_handoff_drain_detail slide_id={} eagain_polls={} other_errs={} eos_seen={}",
        slide_id, detail.eagain_polls, detail.other_errs, detail.eos_seen,
    );
    Ok(state)
}

/// r80 Phase B: signal EOS via empty feed, then drain CAPTURE
/// buffers until the kernel emits V4L2_BUF_FLAG_LAST (which
/// `next_frame` surfaces as `Ok(None)`). Drops every drained
/// Frame so its `Drop` re-QBUFs the buffer back to the kernel.
/// Returns the count drained.
///
/// Distinct from `drain_one_capture_for_preload` (the pre-r80
/// "wait for one frame then exit" function) because we MUST
/// drain ALL pending frames to reach the FLAG_LAST signal --
/// otherwise the kernel stays in mid-drain state and
/// `resume_after_eos` would EBUSY.
fn drain_after_signal_eos(
    state: &mut VideoDecoderState,
    budget_ms: u64,
    detail: &mut DrainDetail,
) -> usize {
    use std::time::{Duration, Instant};
    // Step 2: signal EOS via VIDIOC_DECODER_CMD V4L2_DEC_CMD_STOP.
    //
    // r80/r81 used `feed(&[])` (empty OUTPUT QBUF with
    // V4L2_BUF_FLAG_LAST). r81 telemetry on FYS proved this
    // doesn't work on bcm2835-codec: 96/96 preloads ended with
    // eos_seen=false, kernel never emitted FLAG_LAST on CAPTURE.
    // The `videobuf2 driver bug: stop_streaming ... buffer N in
    // active state` warning then fired during teardown,
    // corrupting device state for the next slide's REQBUFS.
    //
    // r82 H1 (2026-06-08): switch to VIDIOC_DECODER_CMD CMD_STOP.
    // This is the canonical V4L2 m2m stateful decoder API per
    // kernel docs dev-decoder.html. Unlike feed(&[]) this doesn't
    // set output_eof_sent in our wrapper, so the post-drain
    // re-prime works without needing resume_after_eos to clear
    // the flag (though resume_after_eos is still issued to clear
    // the kernel-side EOS state via CMD_START).
    if let Err(e) = state.decoder.signal_eos_via_cmd_stop() {
        eprintln!("[perf] preload_handoff_drain_err err=cmd_stop: {:#}", e);
        detail.other_errs += 1;
        return 0;
    }
    // Step 3: drain CAPTURE until FLAG_LAST.
    //
    // r81 (2026-06-08): use the Frame-bypass DQBUF+QBUF helper at
    // v4l2.rs:drain_capture_step_no_frame. r80 used next_frame ->
    // drop(Frame) which fired Frame::drop's re-QBUF in a separate
    // ioctl AFTER the drain loop completed -- bcm2835-codec
    // appeared to corrupt its internal buffer accounting on this
    // timing, causing REQBUFS OUTPUT to EINVAL on the NEXT slide's
    // fresh decoder fd. r81 does DQBUF+QBUF as a single atomic
    // step (raw ioctls inline, no Frame, no Drop), eliminating the
    // window where the buffer is "in userspace's hands but not
    // re-QBUF'd yet."
    let deadline = Instant::now() + Duration::from_millis(budget_ms);
    let mut drained = 0usize;
    while Instant::now() < deadline {
        match state.decoder.drain_capture_step_no_frame() {
            Ok(crate::v4l2::DrainStep::GotFrame { is_last }) => {
                drained += 1;
                if is_last {
                    detail.eos_seen = true;
                    break;
                }
            }
            Ok(crate::v4l2::DrainStep::WouldBlock) => {
                detail.eagain_polls += 1;
                std::thread::sleep(Duration::from_millis(5));
            }
            Ok(crate::v4l2::DrainStep::EndOfStream) => {
                detail.eos_seen = true;
                break;
            }
            Err(e) => {
                detail.other_errs += 1;
                eprintln!("[perf] preload_handoff_drain_err err={:#}", e);
                break;
            }
        }
    }
    drained
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
        let err = prime_video_decoder(&dem, "test")
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
