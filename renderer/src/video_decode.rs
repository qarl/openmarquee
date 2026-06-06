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

/// Cold-start prime: delegates to `prime_video_decoder_with_warmup`
/// with `PRIME_WARMUP_DEFAULT`. Kept as the public entry point so
/// existing call sites don't have to specify the count.
pub fn prime_video_decoder(dem: &Mp4Demuxer) -> Result<VideoDecoderState> {
    prime_video_decoder_with_warmup(dem, PRIME_WARMUP_DEFAULT)
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
